"""A minimal Model Context Protocol server: dava's API bridge and commands as tools over stdio.

JSON-RPC 2.0 messages, one per line, on stdin/stdout. stdout carries only protocol messages: while the
server runs, file descriptor 1 is pointed at stderr (so native code and child processes cannot write
into the stream) and protocol messages go to a private duplicate of the original stdout.

Tools:
- core: resolve_api_search, resolve_api_describe, resolve_inspect, resolve_get, resolve_call,
  resolve_exec, dava_command
- full (default) adds one typed tool per dava CLI command, e.g. dava_timeline_list or dava_render_start,
  generated from the argument parser so it always matches the CLI.
"""

import argparse
import contextlib
import io
import json
import os
import shlex
import sys
import traceback

from . import __version__, bridge, scripting
from .bridge import REF_HELP, Session
from .connect import ResolveError
from .registry import all_parsers, leaf_parsers
from .remote import ConnectionLost, RemoteObject

# Newest first. A client asking for a version not listed here gets the newest one (the lifecycle rule
# of the 2025-11-25 spec). Clients on 2026-07-28 first probe server/discover; the -32601 reply makes
# them fall back to initialize, which is what Claude Code uses for stdio servers.
PROTOCOL_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]

INSTRUCTIONS = f"""\
dava drives a running DaVinci Resolve through its scripting API.

Workflow: find the method with resolve_api_search, read its exact signature and argument types with
resolve_api_describe, then call it with resolve_get (read-only Get*/Is*/Has* methods) or resolve_call
(anything else). resolve_inspect shows what a reference points at (and lists the live methods of Fusion
objects). The dava_* tools are the higher-level dava commands (one tool per command); dava_command runs any
command from an argv list. resolve_exec runs Python against the live session for anything else.
Calls run one at a time; a long command (e.g. rendering with wait) blocks the next call until it ends.

{REF_HELP}"""

_REF_ARGS = {
    "ref": {"type": "string", "description": "Object reference, e.g. 'timeline', 'clip:a.mov', 'item:video:1:2::graph'."},
    "method": {"type": "string", "description": "API method name, e.g. 'GetName' or 'AddMarker'."},
    "args": {"type": "array", "description": "Positional arguments as JSON values.", "items": {}},
    "kwargs": {"type": "object", "description": "Arguments by parameter name, as JSON values."},
    "save_as": {"type": "string", "description": "Keep a single-object result as $NAME for later calls in this session."},
}

# CLI commands that get no generated tool: covered by a core tool, or meaningless over MCP.
_NOT_GENERATED = {("batch",), ("mcp",), ("call",), ("inspect",), ("exec",)}
_NOT_GENERATED_GROUPS = {"api"}

_JSON_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict}


def _tool(name, title, description, properties, required, read_only):
    return {
        "name": name,
        "title": title,
        "description": description,
        "inputSchema": {"type": "object", "properties": properties, "required": required, "additionalProperties": False},
        "annotations": {
            "title": title,
            "readOnlyHint": read_only,
            "destructiveHint": not read_only,
            "idempotentHint": read_only,
            "openWorldHint": False,
        },
    }


def validate(arguments, schema, where="arguments"):
    """Check tool arguments against the subset of JSON Schema the tool definitions use."""
    expected = schema.get("type")
    if expected:
        python_type = _JSON_TYPES[expected]
        is_bool = isinstance(arguments, bool)
        if not isinstance(arguments, python_type) or (is_bool and expected in ("integer", "number")):
            raise ResolveError(f"{where} must be {expected}, got {json.dumps(arguments)}.")
    if "enum" in schema and arguments not in schema["enum"]:
        raise ResolveError(f"{where} must be one of {', '.join(map(str, schema['enum']))}, got {arguments!r}.")
    if "minimum" in schema and arguments < schema["minimum"]:
        raise ResolveError(f"{where} must be at least {schema['minimum']}, got {arguments}.")
    if expected == "object" and "properties" in schema:
        missing = [key for key in schema.get("required", []) if key not in arguments]
        if missing:
            raise ResolveError(f"Missing required argument(s): {', '.join(missing)}.")
        extra = sorted(set(arguments) - set(schema["properties"]))
        if extra and schema.get("additionalProperties") is False:
            raise ResolveError(f"Unknown argument(s): {', '.join(extra)}. Allowed: {', '.join(schema['properties'])}.")
        for key, value in arguments.items():
            if key in schema["properties"]:
                validate(value, schema["properties"][key], key)
    if expected == "array" and schema.get("items"):
        for index, value in enumerate(arguments):
            validate(value, schema["items"], f"{where}[{index}]")


# -- tools generated from the CLI ------------------------------------------------

_TYPE_NAMES = {"int": "integer", "positive_int": "integer", "float": "number", "non_negative_float": "number"}


def _option_flag(action):
    longs = [o for o in action.option_strings if o.startswith("--")]
    return longs[0] if longs else action.option_strings[0]


def _action_schema(action):
    if isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction)):
        schema = {"type": "boolean"}
    else:
        type_name = getattr(action.type, "__name__", "")
        item = {"type": _TYPE_NAMES.get(type_name, "string")}
        if type_name == "positive_int":
            item["minimum"] = 1
        elif type_name == "non_negative_float":
            item["minimum"] = 0
        if action.choices:
            item["enum"] = list(action.choices)
        many = isinstance(action, argparse._AppendAction) or action.nargs in ("*", "+") or (
            isinstance(action.nargs, int) and action.nargs > 1)
        schema = {"type": "array", "items": item} if many else item
    if action.help and action.help != argparse.SUPPRESS:
        schema["description"] = action.help
    if action.default not in (None, False, [], argparse.SUPPRESS):
        schema["default"] = action.default
    return schema


def _parser_actions(parser):
    return [a for a in parser._actions
            if not isinstance(a, (argparse._HelpAction, argparse._SubParsersAction, argparse._VersionAction))]


def cli_tools(parser, read_only_server, read_only_commands):
    """One tool per CLI command: (tool definition, command words, leaf parser)."""
    tools = []
    for words, leaf, help_text in leaf_parsers(parser):
        if words in _NOT_GENERATED or words[0] in _NOT_GENERATED_GROUPS:
            continue
        declared = leaf._defaults.get("read_only")
        if declared is None:
            read_only = leaf._defaults.get("func") in read_only_commands
        else:
            read_only = declared is True
        # A read-only server offers only tools that can run without changing Resolve.
        if read_only_server and not (read_only or callable(declared)):
            continue
        properties, required = {}, []
        for action in _parser_actions(leaf):
            properties[action.dest] = _action_schema(action)
            if not action.option_strings and action.nargs not in ("?", "*"):
                required.append(action.dest)
            elif action.option_strings and getattr(action, "required", False):
                required.append(action.dest)
        name = "dava_" + "_".join(w.replace("-", "_") for w in words)
        description = f"dava {' '.join(words)}: {help_text or leaf.description or ''}".rstrip(": ")
        if callable(declared):
            description += " (changes Resolve only for some arguments)"
        # On a read-only server every offered tool is read-only: mutating arguments are refused at run time.
        tools.append((_tool(name, f"dava {' '.join(words)}", description, properties, required,
                            read_only or read_only_server), words, leaf))
    return tools


def cli_argv(words, leaf, arguments):
    """Turn validated tool arguments back into a dava argv."""
    options, positionals = [], []
    for action in _parser_actions(leaf):
        if action.dest not in arguments:
            continue
        value = arguments[action.dest]
        values = value if isinstance(value, list) else [value]
        text = [v if isinstance(v, str) else json.dumps(v) for v in values]
        if action.option_strings:
            flag = _option_flag(action)
            if isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction)):
                if value:
                    options.append(flag)
            elif isinstance(action, argparse._AppendAction):
                for item in text:
                    options.extend([flag, item])
            elif isinstance(value, list):
                options.extend([flag, *text])
            else:
                # FLAG=VALUE keeps values that start with '-' from being read as options.
                options.append(f"{flag}={text[0]}")
        else:
            positionals.extend(text)
    # '--' keeps positional values that start with '-' from being read as options.
    return list(words) + options + (["--", *positionals] if positionals else [])


class Server:
    def __init__(self, read_only=False, session=None, toolset="full"):
        self.read_only = read_only
        self.session = session or Session()
        self._own_connection = session is None
        self.toolset = toolset
        self._tools = None
        self._generated = {}
        self._schemas = {}

    # -- tool definitions ------------------------------------------------------

    def _core_tools(self):
        tools = [
            _tool("resolve_api_search", "Search the Resolve API",
                  "Search method names and docs, typed dicts and resolve.* constants of the installed "
                  "Resolve scripting API. Does not need Resolve running.",
                  {"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "default": 30}},
                  ["query"], read_only=True),
            _tool("resolve_api_describe", "Describe a Resolve API name",
                  "Exact signature, parameter types and docs for 'Class.Method', or the fields of a typed dict, "
                  "the values of an alias, or a class's methods. Pass 'refs' for the reference syntax.",
                  {"name": {"type": "string"}}, ["name"], read_only=True),
            _tool("resolve_inspect", "Inspect a Resolve object",
                  "Resolve a reference and return its type, name, stable ref and available methods "
                  "(for Fusion objects: the live members and attributes).",
                  {"ref": _REF_ARGS["ref"]}, ["ref"], read_only=True),
            _tool("resolve_get", "Call a read-only Resolve method",
                  "Call a Get*/Is*/Has* method on the referenced object. Object results come back as "
                  "{\"$ref\", \"$type\"} that can be passed as ref or as arguments to later calls.",
                  dict(_REF_ARGS), ["ref", "method"], read_only=True),
        ]
        if not self.read_only:
            tools.append(_tool(
                "resolve_call", "Call any Resolve method",
                "Call any API method on the referenced object; arguments are checked against the API "
                "definition first. Mutating methods that return False are reported as errors. "
                "Set dry_run to only validate. Set unchecked to call methods missing from the definition.",
                {**_REF_ARGS, "dry_run": {"type": "boolean", "default": False},
                 "unchecked": {"type": "boolean", "default": False}},
                ["ref", "method"], read_only=False))
            tools.append(_tool(
                "resolve_exec", "Run Python in the Resolve session",
                "Run a Python snippet with resolve, pm, project, mediapool, timeline, fusion and ref(REF) bound; "
                "assign the value to return to `result`. For anything the other tools cannot express.",
                {"code": {"type": "string"}}, ["code"], read_only=False))
        tools.append(_tool(
            "dava_command", "Run a dava CLI command",
            "Run one dava command, given as an argv list without the leading 'dava', e.g. "
            "[\"render\", \"start\", \"--wait\"] or [\"timeline\", \"export\", \"/tmp/cut.edl\", \"-f\", \"edl\"]. "
            "Pass [\"--help\"] or [\"<command>\", \"--help\"] for usage." +
            (" Read-only mode: only commands that do not change Resolve are allowed." if self.read_only else ""),
            {"argv": {"type": "array", "items": {"type": "string"}},
             "command": {"type": "string", "description": "Alternative to argv: one shell-quoted string."}},
            [], read_only=self.read_only))
        return tools

    def _build(self):
        from .cli import READ_ONLY_COMMANDS, build_parser

        generated = cli_tools(build_parser(), self.read_only, READ_ONLY_COMMANDS) if self.toolset == "full" else []
        self._generated = {tool["name"]: (words, leaf) for tool, words, leaf in generated}
        self._tools = self._core_tools() + [tool for tool, _, _ in generated]
        self._schemas = {tool["name"]: tool["inputSchema"] for tool in self._tools}

    def tools(self):
        if self._tools is None:
            self._build()
        return self._tools

    # -- tool implementations --------------------------------------------------

    def _call_api(self, arguments, read_only):
        return bridge.call(
            self.session, arguments["ref"], arguments["method"],
            positional=[("json", v) for v in arguments.get("args") or []],
            named={k: ("json", v) for k, v in (arguments.get("kwargs") or {}).items()},
            unchecked=arguments.get("unchecked", False) and not read_only,
            read_only=read_only or self.read_only,
            dry_run=arguments.get("dry_run", False),
            save_as=arguments.get("save_as"),
        )

    def _run_argv(self, argv):
        from .cli import build_parser, cmd_batch, cmd_mcp, ensure_allowed

        if argv[:1] == ["dava"]:
            argv = argv[1:]
        output, errors = io.StringIO(), io.StringIO()
        parser = build_parser()
        for each in all_parsers(parser):
            each.color = False  # Python 3.14 argparse: no ANSI escapes in tool text
        try:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                args = parser.parse_args(argv)
        except SystemExit as exc:
            if exc.code == 0:  # --help
                return output.getvalue().rstrip()
            raise ResolveError(errors.getvalue().strip() or f"invalid command: {argv}") from exc
        if args.func in (cmd_batch, cmd_mcp):
            raise ResolveError(f"'{argv[0]}' is not available as a tool; call the tools directly instead.")
        args.read_only_mode = self.read_only or args.read_only_flag
        args.session = self.session
        ensure_allowed(args)
        resolve = self.session.resolve if getattr(args, "needs_resolve", True) else None
        return args.func(resolve, args)

    def _command(self, arguments):
        if "argv" in arguments:
            argv = arguments["argv"]
        elif "command" in arguments:
            try:
                argv = shlex.split(arguments["command"])
            except ValueError as exc:
                raise ResolveError(f"command: {exc}") from exc
        else:
            raise ResolveError("Give argv (a list of strings) or command (a string).")
        if not argv:
            raise ResolveError("The command is empty.")
        return self._run_argv(argv)

    def call_tool(self, name, arguments):
        """Return a tools/call result, or None for an unknown tool."""
        self.tools()
        handlers = {
            "resolve_api_search": lambda a: self.session.spec.search(a["query"], limit=a.get("limit", 30)),
            "resolve_api_describe": lambda a: REF_HELP if a["name"] == "refs" else self.session.spec.describe(a["name"]),
            "resolve_inspect": lambda a: bridge.inspect(self.session, a["ref"]),
            "resolve_get": lambda a: self._call_api(a, read_only=True),
            "dava_command": self._command,
        }
        if not self.read_only:
            handlers["resolve_call"] = lambda a: self._call_api(a, read_only=False)
            handlers["resolve_exec"] = lambda a: scripting.run(self.session, a["code"], "<resolve_exec>")
        for tool_name, (words, leaf) in self._generated.items():
            handlers[tool_name] = lambda a, w=words, p=leaf: self._run_argv(cli_argv(w, p, a))
        if name not in handlers:
            return None
        if arguments is None:
            arguments = {}
        try:
            validate(arguments, self._schemas[name])
            if self._own_connection and not isinstance(self.session._resolve, RemoteObject):
                # Reconnect for every call so a restarted Resolve is picked up. A bridge connection is
                # kept instead: its object ids (and so the session's $handles) live as long as it does.
                self.session._resolve = None
            result = handlers[name](arguments)
        except ConnectionLost as exc:
            self.session._resolve = None  # reconnect on the next call
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        except ResolveError as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        text = result if isinstance(result, str) else json.dumps(result, indent=2, default=str)
        return {"content": [{"type": "text", "text": text}], "isError": False}

    # -- JSON-RPC --------------------------------------------------------------

    def handle(self, message):
        """Return the response for one JSON-RPC message, or None for notifications."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            msg_id = message.get("id") if isinstance(message, dict) else None
            return _error(msg_id if _valid_id(msg_id) else None, -32600, "Invalid Request")
        method = message["method"]
        if "id" not in message:
            return None  # notifications/initialized, notifications/cancelled, ...
        msg_id = message["id"]
        if not _valid_id(msg_id):
            return _error(None, -32600, "Invalid Request: id must be a string or an integer")
        params = message.get("params", {})
        if not isinstance(params, dict):
            return _error(msg_id, -32602, "Invalid params: params must be an object")

        if method == "initialize":
            requested = params.get("protocolVersion")
            version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return _result(msg_id, {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "dava", "title": "dava (DaVinci Resolve)", "version": __version__},
                "instructions": INSTRUCTIONS,
            })
        if method == "ping":
            return _result(msg_id, {})
        if method == "tools/list":
            return _result(msg_id, {"tools": self.tools()})
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments")
            if not isinstance(name, str):
                return _error(msg_id, -32602, "Invalid params: name must be a string")
            if arguments is not None and not isinstance(arguments, dict):
                return _error(msg_id, -32602, "Invalid params: arguments must be an object")
            outcome = self.call_tool(name, arguments)
            if outcome is None:
                return _error(msg_id, -32602, f"Unknown tool: {name}")
            return _result(msg_id, outcome)
        return _error(msg_id, -32601, f"Method not found: {method}")


def _valid_id(msg_id):
    return isinstance(msg_id, str) or (isinstance(msg_id, int) and not isinstance(msg_id, bool))


def _result(msg_id, result):
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _reject_constant(name):
    raise ValueError(f"{name} is not valid JSON")


@contextlib.contextmanager
def _protocol_stdout():
    """Yield a text stream on the real stdout, with fd 1 pointed at stderr for everything else."""
    sys.stdout.flush()
    saved = os.dup(1)
    os.dup2(2, 1)
    stream = os.fdopen(os.dup(saved), "w", encoding="utf-8", newline="\n")
    try:
        with contextlib.redirect_stdout(sys.stderr):
            yield stream
    finally:
        stream.close()
        sys.stdout.flush()
        os.dup2(saved, 1)
        os.close(saved)


def serve(read_only=False, stdin=None, stdout=None, session=None, toolset="full"):
    """Serve until stdin closes. stdin yields bytes or str lines; stdout defaults to the real stdout."""
    server = Server(read_only=read_only, session=session, toolset=toolset)
    source = stdin if stdin is not None else sys.stdin.buffer

    def run(out):
        def send(payload):
            out.write(json.dumps(payload, default=str, allow_nan=False) + "\n")
            out.flush()

        for raw in source:
            try:
                line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
                if not line.strip():
                    continue
                message = json.loads(line, parse_constant=_reject_constant)
            except ValueError as exc:  # UnicodeDecodeError, JSONDecodeError, NaN/Infinity
                send(_error(None, -32700, f"Parse error: {exc}"))
                continue
            if isinstance(message, list):
                # JSON-RPC batches were removed from MCP in protocol 2025-06-18.
                send(_error(None, -32600, "Invalid Request: batch messages are not supported"))
                continue
            try:
                response = server.handle(message)
            except Exception as exc:  # a dava bug must not kill the server; report it to the client
                traceback.print_exc(file=sys.stderr)
                msg_id = message.get("id") if isinstance(message, dict) else None
                response = _error(msg_id if _valid_id(msg_id) else None, -32603,
                                  f"Internal error: {type(exc).__name__}: {exc}")
            if response is not None:
                send(response)

    if stdout is not None:
        with contextlib.redirect_stdout(sys.stderr):
            run(stdout)
    else:
        with _protocol_stdout() as out:
            run(out)
