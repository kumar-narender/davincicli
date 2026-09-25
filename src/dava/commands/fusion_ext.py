"""dava insert, and dava fusion actions for compositions and their tools.

insert: generators, titles and empty Fusion compositions at the playhead (Timeline.Insert*IntoTimeline).
Resolve puts them on V1 at the playhead and shifts the clips after it to the right.

fusion comps/load/cache: the compositions of one timeline item (Resolve API).
fusion tools/inputs/get/set/animate/add-tool/connect/delete-tool/lua: tools inside a composition. These
use the Fusion scripting API on the composition objects Resolve hands out; that API is not in the Resolve
API definition, so every such call is declared in FUSION_API_METHODS and each edit runs inside
comp.Lock() / comp.Unlock().
"""

import argparse
import contextlib
import json
import math
import os

from .. import templates
from ..bridge import describe_object, resolve_ref, serialize
from ..connect import ResolveError
from ..helpers import current_project, find_timeline, select_one_item

# Fusion scripting API methods used here; they are not in the Resolve API definition.
FUSION_API_METHODS = {"GetToolList", "GetAttrs", "GetInputList", "GetInput", "SetInput", "AddModifier",
                      "AddTool", "ConnectInput", "Delete", "Execute", "Lock", "Unlock"}

# AddTool x/y value that lets Fusion place the tool itself.
AUTO_POSITION = -32768

GENERATORS = {
    "standard": lambda timeline, name: timeline.InsertGeneratorIntoTimeline(name),
    "fusion": lambda timeline, name: timeline.InsertFusionGeneratorIntoTimeline(name),
    "ofx": lambda timeline, name: timeline.InsertOFXGeneratorIntoTimeline(name),
}
TITLES = {
    "standard": lambda timeline, name: timeline.InsertTitleIntoTimeline(name),
    "fusion": lambda timeline, name: timeline.InsertFusionTitleIntoTimeline(name),
}
# Names of Resolve's built-in (non-Fusion) generators and titles, for error hints.
BUILT_IN = {
    "generator": "Solid Color, 10 Step, Four Color Gradient, Grey Scale, SMPTE Color Bar, Window",
    "title": "Text, Scroll, Left Lower Third, Middle Lower Third, Right Lower Third",
}
MODIFIERS = ("BezierSpline", "XYPath")

PLAYHEAD_NOTE = "Resolve puts it on V1 at the playhead and shifts later clips right"


# ---------------------------------------------------------------------------
# insert


def _prepare(resolve, args):
    """Make the timeline current and move the playhead to --at; return the timeline."""
    project = current_project(resolve)
    timeline = find_timeline(project, args.timeline)
    if not project.SetCurrentTimeline(timeline):
        raise ResolveError(f"Could not make {timeline.GetName()!r} the current timeline.")
    if args.at and not timeline.SetCurrentTimecode(args.at):
        raise ResolveError(f"Could not move the playhead to {args.at!r} (expected a timecode such as "
                           "01:00:05:00 inside the timeline).")
    return timeline


def _not_inserted(what, kind, name):
    message = f"Resolve could not insert {kind} {what} {name!r}."
    if kind == "fusion":
        words = name.split()
        similar = templates.list_templates(kind=what + "s", search=words[0]) if words else []
        names = ", ".join(row["name"] for row in similar[:8])
        return ResolveError(message + (f" Similar {what}s: {names}." if names
                                       else f" See 'dava templates list {what}s'."))
    if kind == "ofx":
        return ResolveError(message + " Use the name the Effects library shows under OpenFX; the plugin "
                                      "must be installed.")
    return ResolveError(message + f" Built-in {what}s: {BUILT_IN[what]}. For Fusion {what}s use --kind fusion.")


def _inserted(args, item, **extra):
    """The new item's REF plus where Resolve put it: [track type, track number], start frame, duration."""
    info = describe_object(args.session, item, "TimelineItem")
    info.update(extra)
    info.update({"track": _plain(args.session, item.GetTrackTypeAndIndex()),
                 "start": item.GetStart(), "duration": item.GetDuration()})
    return info


def cmd_insert_generator(resolve, args):
    timeline = _prepare(resolve, args)
    item = GENERATORS[args.kind](timeline, args.name)
    if not item:
        raise _not_inserted("generator", args.kind, args.name)
    return _inserted(args, item, kind=args.kind, generator=args.name)


def cmd_insert_title(resolve, args):
    timeline = _prepare(resolve, args)
    item = TITLES[args.kind](timeline, args.name)
    if not item:
        raise _not_inserted("title", args.kind, args.name)
    return _inserted(args, item, kind=args.kind, title=args.name)


def cmd_insert_fusion_comp(resolve, args):
    timeline = _prepare(resolve, args)
    item = timeline.InsertFusionCompositionIntoTimeline()
    if not item:
        raise ResolveError(f"Resolve could not insert a Fusion composition into {timeline.GetName()!r}.")
    return _inserted(args, item, kind="fusion composition")


# ---------------------------------------------------------------------------
# Targets: the timeline item and composition an action works on


def _target(resolve, args):
    """(object, class name) that --ref names, or the clip -t/-T/-i select."""
    if args.ref is not None:
        if args.timeline is not None or args.track is not None:
            raise ResolveError("-t/-T select a clip together with -i; do not combine them with --ref.")
        return resolve_ref(args.session, args.ref)
    if args.track is None:
        args.track = 1
    return select_one_item(resolve, args), "TimelineItem"


def _item(resolve, args):
    obj, cls = _target(resolve, args)
    if cls != "TimelineItem":
        raise ResolveError(f"--ref must name a timeline item (e.g. item#ID or item:video:2:1), got a {cls}.")
    return obj


def _item_comp(item, which):
    """(composition, 1-based index) of a timeline item for --comp NAME|N (default: the first one)."""
    names = list(item.GetFusionCompNameList() or [])
    comp, index = None, None
    if which is None:
        index = 1
        comp = item.GetFusionCompByIndex(index)
    elif which in names:
        index = names.index(which) + 1
        comp = item.GetFusionCompByName(which)
    elif which.isascii() and which.isdigit() and int(which) >= 1:
        index = int(which)
        comp = item.GetFusionCompByIndex(index)
    if not comp:
        if which is None:
            raise ResolveError(f"{item.GetName()!r} has no Fusion composition. Add one with 'dava fusion add'.")
        raise ResolveError(f"{item.GetName()!r} has no Fusion composition {which!r}. "
                           f"Its compositions: {', '.join(names) or 'none'}.")
    return comp, index


def _comp(resolve, args):
    """(composition, REF of it) from --ref, or from -t/-T/-i with --comp."""
    obj, cls = _target(resolve, args)
    if cls == "FusionComp":
        if args.comp is not None:
            raise ResolveError("--comp picks a composition of a timeline item; this --ref already names one.")
        return obj, args.ref
    if cls != "TimelineItem":
        raise ResolveError("--ref must name a timeline item or a Fusion composition (e.g. item#ID, "
                           f"item:video:2:1::comp@2, fusion::comp), got a {cls}.")
    comp, index = _item_comp(obj, args.comp)
    return comp, f"item#{obj.GetUniqueId()}::comp@{index}"


@contextlib.contextmanager
def _locked(comp):
    """Lock the composition for an edit (no dialogs, one refresh) and always unlock it."""
    comp.Lock()
    try:
        yield
    finally:
        comp.Unlock()


def _ordered(table):
    """Fusion returns lists as {1: a, 2: b}; give them back as a list in key order."""
    if isinstance(table, dict):
        return [table[key] for key in sorted(table)]
    return list(table or [])


def _tools(comp, regid=None, selected=False):
    return _ordered(comp.GetToolList(selected, regid) if regid else comp.GetToolList(selected))


def _tool_name(tool):
    return tool.GetAttrs()["TOOLS_Name"]


def _find_tools(comp, names):
    """The tools with these names, from one listing of the composition (each tool costs a GetAttrs call)."""
    tools = {_tool_name(tool): tool for tool in _tools(comp)}
    missing = [name for name in names if name not in tools]
    if missing:
        raise ResolveError(f"The composition has no tool named {' or '.join(map(repr, missing))}. Its tools: "
                           f"{', '.join(tools) or 'none'} (see 'dava fusion tools').")
    return [tools[name] for name in names]


def _find_tool(comp, name):
    return _find_tools(comp, [name])[0]


# ---------------------------------------------------------------------------
# Values


def _fusion_value(value, where):
    """A JSON value as SetInput wants it: lists become points {1: x, 2: y}, true/false become 1/0."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, float) and not math.isfinite(value):
        raise ResolveError(f"{where}: {value} is not a finite number.")
    if isinstance(value, (int, float, str)):
        return value
    if isinstance(value, list):
        if not value or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value):
            raise ResolveError(f"{where}: a list must hold numbers, e.g. [0.5, 0.5] for a point.")
        return {index: _fusion_value(v, where) for index, v in enumerate(value, start=1)}
    if isinstance(value, dict):
        # isascii: int() refuses other Unicode digits such as superscripts, which isdigit accepts.
        return {int(k) if k.isascii() and k.isdigit() else k: _fusion_value(v, f"{where}.{k}")
                for k, v in value.items()}
    raise ResolveError(f"{where}: null is not a Fusion input value.")


def _parse_value(text, as_string, where):
    """Command-line text -> input value: JSON when it parses (numbers, lists, quoted text), else the text."""
    if as_string:
        return text
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return text
    except ValueError as exc:  # valid JSON Python refuses, e.g. an integer of more than 4300 digits
        raise ResolveError(f"{where}: {exc}.") from None
    return _fusion_value(value, where)


def _plain(session, value):
    """A Fusion value as JSON: points {1: x, 2: y} become [x, y]; objects become refs."""
    if isinstance(value, dict):
        keys = list(value)
        numeric = all(isinstance(k, (int, float)) and not isinstance(k, bool) for k in keys)
        if keys and numeric and sorted(keys) == list(range(1, len(keys) + 1)):
            return [_plain(session, value[k]) for k in sorted(keys)]
        return {str(k): _plain(session, v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(session, v) for v in value]
    return serialize(session, value)


class _FiniteFloat(argparse.Action):
    """Store --time/--pos numbers (type=float), refusing nan and inf: not JSON, and nothing Fusion takes.

    An action rather than a type function keeps type=float, which 'dava mcp' turns into a JSON number.
    """

    def __call__(self, parser, namespace, values, option_string=None):
        for value in values if isinstance(values, list) else [values]:
            if not math.isfinite(value):
                parser.error(f"argument {option_string}: expected a finite number, got {value}")  # exits 2
        setattr(namespace, self.dest, values)


def _frame(text, where):
    try:
        frame = float(text)
    except ValueError:
        raise ResolveError(f"{where}: frame must be a number, got {text!r}.") from None
    if not math.isfinite(frame):
        raise ResolveError(f"{where}: frame must be a finite number, got {text!r}.")
    return int(frame) if frame.is_integer() else frame


def _keyframes(pairs, as_string):
    """FRAME=VALUE pairs -> [(frame, value)] sorted by frame."""
    keys = {}
    for pair in pairs:
        frame_text, sep, value_text = pair.partition("=")
        if not sep or not frame_text.strip():
            raise ResolveError(f"Expected FRAME=VALUE, got {pair!r}.")
        frame = _frame(frame_text, pair)
        if frame in keys:
            raise ResolveError(f"Frame {frame} is given more than once.")
        keys[frame] = _parse_value(value_text, as_string, f"frame {frame}")
    return sorted(keys.items())


# ---------------------------------------------------------------------------
# fusion: compositions of a timeline item (Resolve API)


def cmd_comps(resolve, args):
    item = _item(resolve, args)
    count = item.GetFusionCompCount()
    names = list(item.GetFusionCompNameList() or [])
    ref = f"item#{item.GetUniqueId()}"
    return {"item": item.GetName(), "ref": ref, "count": count,
            "comps": [{"index": i, "name": name, "ref": f"{ref}::comp@{i}"} for i, name in enumerate(names, start=1)]}


def cmd_load(resolve, args):
    item = _item(resolve, args)
    if not item.LoadFusionCompByName(args.name):
        names = ", ".join(item.GetFusionCompNameList() or []) or "none"
        raise ResolveError(f"Could not load composition {args.name!r} on {item.GetName()!r}. "
                           f"Its compositions: {names}.")
    return {"item": item.GetName(), "loaded": args.name}


def cmd_cache(resolve, args):
    item = _item(resolve, args)
    if args.value is not None and not item.SetFusionOutputCache(args.value):
        raise ResolveError(f"Resolve rejected Fusion output cache value {args.value!r} for {item.GetName()!r}. "
                           "The API documents auto, enabled or disabled; 'dava fusion cache' without a value "
                           "shows the spelling Resolve uses.")
    return {"item": item.GetName(), "fusion_output_cache": item.GetIsFusionOutputCacheEnabled()}


# ---------------------------------------------------------------------------
# fusion: tools of a composition (Fusion scripting API)


def cmd_tools(resolve, args):
    comp, ref = _comp(resolve, args)
    rows = []
    for tool in _tools(comp, args.type, args.selected):
        attrs = tool.GetAttrs()
        name = attrs["TOOLS_Name"]
        rows.append({"name": name, "type": attrs["TOOLS_RegID"], "ref": f"{ref}::tool:{name}"})
    comp_attrs = comp.GetAttrs() or {}
    return {"comp": ref, "render_range": [comp_attrs.get("COMPN_RenderStart"), comp_attrs.get("COMPN_RenderEnd")],
            "tools": rows}


def cmd_inputs(resolve, args):
    comp, _ = _comp(resolve, args)
    tool = _find_tool(comp, args.tool)
    rows = []
    for fusion_input in _ordered(tool.GetInputList()):
        attrs = fusion_input.GetAttrs()
        row = {"id": attrs["INPS_ID"], "name": attrs.get("INPS_Name"), "type": attrs.get("INPS_DataType")}
        if args.search is None or args.search.lower() in f"{row['id']} {row['name']}".lower():
            rows.append(row)
    if not rows:
        raise ResolveError(f"No input of {args.tool} matches {args.search!r}." if args.search
                           else f"Fusion reported no inputs for {args.tool}.")
    return rows


def cmd_get(resolve, args):
    comp, _ = _comp(resolve, args)
    tool = _find_tool(comp, args.tool)
    value = tool.GetInput(args.input) if args.time is None else tool.GetInput(args.input, args.time)
    if value is None:
        raise ResolveError(f"{args.tool} returned no value for input {args.input!r}; list its inputs with "
                           f"'dava fusion inputs {args.tool}'.")
    return {"tool": args.tool, "input": args.input, "time": args.time, "value": _plain(args.session, value)}


def cmd_set(resolve, args):
    comp, _ = _comp(resolve, args)
    tool = _find_tool(comp, args.tool)
    value = _parse_value(args.value, args.string, "VALUE")
    with _locked(comp):
        if args.time is None:
            result = tool.SetInput(args.input, value)
        else:
            result = tool.SetInput(args.input, value, args.time)
    # Fusion's SetInput returns nothing on success; only an explicit False is a refusal.
    if result is False:
        raise ResolveError(f"Fusion rejected {args.input}={args.value!r} on {args.tool} (wrong input name or "
                           f"value type? see 'dava fusion inputs {args.tool}').")
    return {"tool": args.tool, "input": args.input, "time": args.time, "value": _plain(args.session, value)}


def cmd_animate(resolve, args):
    comp, _ = _comp(resolve, args)
    tool = _find_tool(comp, args.tool)
    keys = _keyframes(args.keyframes, args.string)
    with _locked(comp):
        if not args.keys_only and tool.AddModifier(args.input, args.modifier) is False:
            raise ResolveError(f"Fusion could not animate {args.tool}.{args.input} with a {args.modifier} "
                               f"(wrong input name? see 'dava fusion inputs {args.tool}').")
        rejected = [frame for frame, value in keys if tool.SetInput(args.input, value, frame) is False]
    if rejected:
        raise ResolveError(f"Fusion rejected the {args.input} key(s) at frame(s) {', '.join(map(str, rejected))}.")
    return {"tool": args.tool, "input": args.input, "modifier": None if args.keys_only else args.modifier,
            "keys": [{"frame": frame, "value": _plain(args.session, value)} for frame, value in keys]}


def cmd_add_tool(resolve, args):
    comp, ref = _comp(resolve, args)
    x, y = args.pos if args.pos else (AUTO_POSITION, AUTO_POSITION)
    with _locked(comp):
        tool = comp.AddTool(args.regid, x, y)
    if not tool:
        raise ResolveError(f"Fusion could not add a {args.regid!r} tool. Give a tool RegID such as Background, "
                           "Blur, Merge, Transform or TextPlus ('dava fusion tools' shows the RegIDs in use).")
    name = _tool_name(tool)
    return {"name": name, "type": args.regid, "ref": f"{ref}::tool:{name}"}


def cmd_connect(resolve, args):
    comp, _ = _comp(resolve, args)
    if args.disconnect:
        tool, source = _find_tool(comp, args.tool), None
    else:
        tool, source = _find_tools(comp, [args.tool, args.source])
    with _locked(comp):
        result = tool.ConnectInput(args.input, source)
    if result is False:
        action = "disconnect" if args.disconnect else f"connect {args.source} to"
        raise ResolveError(f"Fusion could not {action} {args.tool}.{args.input} (wrong input name? see "
                           f"'dava fusion inputs {args.tool}').")
    return {"tool": args.tool, "input": args.input, "source": args.source}


def cmd_delete_tool(resolve, args):
    comp, ref = _comp(resolve, args)
    names = list(dict.fromkeys(args.tools))  # a name given twice is deleted once
    tools = _find_tools(comp, names)
    with _locked(comp):
        for tool in tools:
            tool.Delete()
    remaining = [_tool_name(tool) for tool in _tools(comp)]
    kept = [name for name in names if name in remaining]
    if kept:
        raise ResolveError(f"Fusion did not delete {', '.join(kept)}.")
    return {"comp": ref, "deleted": names, "tools": remaining}


def cmd_lua(resolve, args):
    code = args.code
    if args.file is not None:  # -f "" is a missing file, not "no file": never run Execute(None)
        try:
            with open(args.file, encoding="utf-8") as handle:
                code = handle.read()
        except (OSError, UnicodeDecodeError) as exc:
            raise ResolveError(f"Cannot read Lua file {args.file!r}: {exc}") from exc
    comp, ref = _comp(resolve, args)
    with _locked(comp):
        result = comp.Execute(code)
    if result is False:
        raise ResolveError("Fusion reported that the script failed.")
    return {"comp": ref, "result": _plain(args.session, result)}


# ---------------------------------------------------------------------------
# Parsers


def _item_args(p):
    """Pick one timeline item: -i INDEX (with -t/-T) or --ref REF."""
    p.add_argument("-t", "--timeline", help="timeline name (default: current); used with -i")
    p.add_argument("-T", "--track", type=int, help="video track number (default: 1); used with -i")
    which = p.add_mutually_exclusive_group(required=True)
    which.add_argument("-i", "--index", type=int, help="clip number on the track, from 1")
    which.add_argument("-r", "--ref", metavar="REF",
                       help="timeline item REF instead of -i, e.g. item#ID (from 'dava insert') or "
                            "timeline:Edit::item:video:2:1; see 'dava api refs'")


def _comp_args(p):
    """Pick one composition: a clip (-i or --ref) and --comp, or --ref naming the composition itself."""
    p.add_argument("-t", "--timeline", help="timeline name (default: current); used with -i")
    p.add_argument("-T", "--track", type=int, help="video track number (default: 1); used with -i")
    which = p.add_mutually_exclusive_group(required=True)
    which.add_argument("-i", "--index", type=int, help="clip number on the track, from 1")
    which.add_argument("-r", "--ref", metavar="REF",
                       help="timeline item or composition REF instead of -i, e.g. item#ID (from 'dava insert'), "
                            "item:video:2:1::comp@2, or fusion::comp (the one open on the Fusion page)")
    p.add_argument("-c", "--comp", metavar="NAME|N",
                   help="composition name or number, from 1, of the clip (default: its first composition)")


def _value_args(p):
    p.add_argument("--string", action="store_true",
                   help="pass values as text as they are (no JSON parsing), e.g. for the text '42'")


def _insert_args(p):
    p.add_argument("--at", metavar="TIMECODE", help="move the playhead here first, e.g. 01:00:05:00")
    p.add_argument("-t", "--timeline", help="timeline name (default: current)")


def register(registry):
    help_text = f"insert generators, titles and Fusion compositions at the playhead ({PLAYHEAD_NOTE})"
    p = registry.action("insert", "generator", f"insert a generator: standard, Fusion or OFX ({PLAYHEAD_NOTE})",
                        group_help=help_text)
    p.add_argument("name", help="generator name, e.g. 'Solid Color' (standard) or 'Noise Gradient' (Fusion)")
    p.add_argument("-k", "--kind", choices=sorted(GENERATORS), default="standard",
                   help="standard: built-in generators; fusion: Fusion generators ('dava templates list "
                        "generators'); ofx: OpenFX generators (default: standard)")
    _insert_args(p)
    p.set_defaults(func=cmd_insert_generator, read_only=False, always_json=True)

    p = registry.action("insert", "title", f"insert a title without setting its text ({PLAYHEAD_NOTE}); "
                                           "for a Fusion title with text use 'dava title add'")
    p.add_argument("name", help="title name, e.g. 'Left Lower Third' (standard) or 'Text+' (Fusion)")
    p.add_argument("-k", "--kind", choices=sorted(TITLES), default="standard",
                   help="standard: built-in titles; fusion: Fusion titles ('dava templates list titles') "
                        "(default: standard)")
    _insert_args(p)
    p.set_defaults(func=cmd_insert_title, read_only=False, always_json=True)

    p = registry.action("insert", "fusion-comp", f"insert an empty Fusion composition clip ({PLAYHEAD_NOTE})")
    _insert_args(p)
    p.set_defaults(func=cmd_insert_fusion_comp, read_only=False, always_json=True)

    p = registry.action("fusion", "comps", "list one clip's compositions with their numbers and REFs")
    _item_args(p)
    p.set_defaults(func=cmd_comps, read_only=True, always_json=True)

    p = registry.action("fusion", "load", "make a named composition the clip's active composition")
    p.add_argument("name", help="composition name (see 'dava fusion comps')")
    _item_args(p)
    p.set_defaults(func=cmd_load, read_only=False)

    p = registry.action("fusion", "cache", "show or set a clip's 'Render Cache Fusion Output' mode")
    p.add_argument("value", nargs="?",
                   help="new mode, passed to Resolve as is (the API documents auto, enabled or disabled); "
                        "omit to show the current mode")
    _item_args(p)
    p.set_defaults(func=cmd_cache, read_only=lambda a: a.value is None)

    fusion_api = "Fusion scripting API"
    p = registry.action("fusion", "tools", f"list the tools of a clip's composition: name, RegID, REF ({fusion_api})")
    p.add_argument("--type", metavar="REGID", help="only tools of this RegID, e.g. TextPlus")
    p.add_argument("--selected", action="store_true",
                   help="only the tools selected in the Fusion page (useful with --ref fusion::comp)")
    _comp_args(p)
    p.set_defaults(func=cmd_tools, read_only=True, always_json=True)

    p = registry.action("fusion", "inputs", f"list the inputs of a tool: id, name, data type ({fusion_api})")
    p.add_argument("tool", help="tool name, e.g. Text1 (see 'dava fusion tools')")
    p.add_argument("-s", "--search", help="only inputs whose id or name contains this text")
    _comp_args(p)
    p.set_defaults(func=cmd_inputs, read_only=True)

    p = registry.action("fusion", "get", f"read a tool input ({fusion_api} GetInput; the Lua bridge refuses it "
                                         "because it hangs the free edition: there use 'dava fusion export')")
    p.add_argument("tool", help="tool name, e.g. Text1")
    p.add_argument("input", help="input id, e.g. StyledText, Center, Size (see 'dava fusion inputs')")
    p.add_argument("--time", type=float, action=_FiniteFloat, metavar="FRAME",
                   help="composition frame to read an animated input at")
    _comp_args(p)
    p.set_defaults(func=cmd_get, read_only=True)

    p = registry.action("fusion", "set", f"set a tool input ({fusion_api}): a number, text, or a JSON list "
                                         "for points, e.g. [0.5,0.5]")
    p.add_argument("tool", help="tool name, e.g. Text1")
    p.add_argument("input", help="input id, e.g. StyledText, Center, Size (see 'dava fusion inputs')")
    p.add_argument("value", help="JSON number, list ([x,y] becomes a Fusion point) or true/false (1/0); any "
                                 "other text is sent as text. Negative numbers work as is; put '--' before "
                                 "other values that start with '-'")
    p.add_argument("--time", type=float, action=_FiniteFloat, metavar="FRAME",
                   help="composition frame; sets a key only if the input is animated ('dava fusion animate')")
    _value_args(p)
    _comp_args(p)
    p.set_defaults(func=cmd_set, read_only=False)

    p = registry.action("fusion", "animate", f"keyframe a tool input at composition frames ({fusion_api}: "
                                             "AddModifier, then SetInput per key)")
    p.add_argument("tool", help="tool name, e.g. Text1")
    p.add_argument("input", help="input id, e.g. Size or Center")
    p.add_argument("keyframes", nargs="+", metavar="FRAME=VALUE",
                   help="keys such as 0=0.05 24=0.1, or 0=[0.5,0.2] for points; values as in 'dava fusion set'")
    p.add_argument("-m", "--modifier", choices=MODIFIERS, default="BezierSpline",
                   help="animation modifier to attach first (default: BezierSpline; XYPath suits points like Center)")
    p.add_argument("--keys-only", action="store_true",
                   help="the input is already animated: only set the keys, attach no new modifier")
    _value_args(p)
    _comp_args(p)
    p.set_defaults(func=cmd_animate, read_only=False)

    p = registry.action("fusion", "add-tool", f"add a tool to a composition by RegID ({fusion_api})")
    p.add_argument("regid", metavar="REGID", help="tool RegID, e.g. Background, Blur, Merge, Transform, TextPlus")
    p.add_argument("--pos", nargs=2, type=float, action=_FiniteFloat, metavar=("X", "Y"),
                   help="position in the node view (default: Fusion places the tool)")
    _comp_args(p)
    p.set_defaults(func=cmd_add_tool, read_only=False, always_json=True)

    p = registry.action("fusion", "connect", f"connect another tool's output to a tool input, or disconnect "
                                             f"the input ({fusion_api})")
    p.add_argument("tool", help="tool that receives, e.g. Merge1")
    p.add_argument("input", help="input id, e.g. Background, Foreground, Input")
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("source", nargs="?", help="tool whose output to connect, e.g. Text1")
    source.add_argument("--disconnect", action="store_true", help="disconnect the input instead")
    _comp_args(p)
    p.set_defaults(func=cmd_connect, read_only=False)

    p = registry.action("fusion", "delete-tool", f"delete tools from a composition ({fusion_api})")
    p.add_argument("tools", nargs="+", metavar="TOOL", help="tool name, e.g. Blur1")
    _comp_args(p)
    p.set_defaults(func=cmd_delete_tool, read_only=False, always_json=True)

    p = registry.action("fusion", "lua", f"run Lua code in a composition ({fusion_api} comp.Execute)")
    code = p.add_mutually_exclusive_group(required=True)
    code.add_argument("code", nargs="?", help="Lua code")
    code.add_argument("-f", "--file", help="read the Lua code from this file")
    _comp_args(p)
    p.set_defaults(func=cmd_lua, read_only=False, always_json=True)
