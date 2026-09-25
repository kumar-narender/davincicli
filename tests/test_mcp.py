import io
import json
import os
import sys

import pytest

from dava.bridge import Session
from dava.mcp import PROTOCOL_VERSIONS, Server, serve
from dava.spec import Spec
from fakes import FakeResolve

MINI = os.path.join(os.path.dirname(__file__), "data", "mini.pyi")


@pytest.fixture
def resolve():
    return FakeResolve()


@pytest.fixture
def session(resolve):
    with open(MINI) as handle:
        return Session(resolve=resolve, spec=Spec(handle.read(), MINI))


def rpc(server, method, params=None, msg_id=1):
    message = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        message["params"] = params
    return server.handle(message)


def tool(server, name, arguments):
    result = rpc(server, "tools/call", {"name": name, "arguments": arguments})["result"]
    text = result["content"][0]["text"]
    return result["isError"], text


def test_initialize_negotiates_version(session):
    server = Server(session=session)
    result = rpc(server, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"]["name"] == "dava"
    assert "resolve_api_search" in result["instructions"]
    newest = rpc(server, "initialize", {"protocolVersion": "1999-01-01"})["result"]["protocolVersion"]
    assert newest == PROTOCOL_VERSIONS[0]


def test_notifications_unknown_methods_and_ping(session):
    server = Server(session=session)
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert rpc(server, "ping")["result"] == {}
    assert rpc(server, "server/discover")["error"]["code"] == -32601
    assert server.handle({"id": 3, "method": "ping"})["error"]["code"] == -32600
    assert rpc(server, "tools/call", {"name": "nope", "arguments": {}})["error"]["code"] == -32602


def test_tools_list_and_read_only_mode(session):
    core = [t["name"] for t in rpc(Server(session=session, toolset="core"), "tools/list")["result"]["tools"]]
    assert core == ["resolve_api_search", "resolve_api_describe", "resolve_inspect", "resolve_get",
                    "resolve_call", "resolve_exec", "dava_command"]
    full = [t["name"] for t in rpc(Server(session=session), "tools/list")["result"]["tools"]]
    assert full[:len(core)] == core
    assert {"dava_timeline_list", "dava_render_start", "dava_title_add"} <= set(full)
    ro = Server(session=session, read_only=True)
    tools = {t["name"]: t for t in rpc(ro, "tools/list")["result"]["tools"]}
    assert "resolve_call" not in tools
    assert all(t["annotations"]["readOnlyHint"] for t in tools.values())
    for t in tools.values():
        assert t["inputSchema"]["type"] == "object"
    assert rpc(ro, "tools/call", {"name": "resolve_call", "arguments": {}})["error"]["code"] == -32602


def test_api_tools(session):
    server = Server(session=session)
    is_error, text = tool(server, "resolve_api_search", {"query": "marker"})
    assert not is_error
    assert "Timeline.AddMarker" in text
    is_error, text = tool(server, "resolve_api_describe", {"name": "Timeline.Export"})
    assert "TimelineExportType" in json.loads(text)["types"]
    is_error, text = tool(server, "resolve_api_describe", {"name": "refs"})
    assert "item:TYPE:TRACK:N" in text
    is_error, text = tool(server, "resolve_api_describe", {})
    assert is_error and "Missing required argument(s): name" in text


def test_get_and_call(session, resolve):
    server = Server(session=session)
    is_error, text = tool(server, "resolve_get", {"ref": "timeline", "method": "GetName"})
    assert not is_error and json.loads(text)["result"] == "Edit v1"
    is_error, text = tool(server, "resolve_get", {"ref": "timeline", "method": "SetName", "args": ["X"]})
    assert is_error and "read-only" in text
    is_error, text = tool(server, "resolve_call", {"ref": "timeline", "method": "AddMarker",
                                                   "kwargs": {"frameId": 7, "color": "Red", "name": "n",
                                                              "note": "", "duration": 1}})
    assert not is_error, text
    assert 7 in resolve.pm.current.timelines[0].markers
    is_error, text = tool(server, "resolve_call", {"ref": "timeline", "method": "SetName", "args": ["Y"],
                                                   "dry_run": True})
    assert not is_error and resolve.pm.current.timelines[0].name == "Edit v1"


def test_handles_persist_across_tool_calls(session, resolve):
    server = Server(session=session)
    tool(server, "resolve_get", {"ref": "item:video:1:1", "method": "GetNodeGraph", "save_as": "g"})
    is_error, text = tool(server, "resolve_call", {"ref": "$g", "method": "SetLUT", "args": [2, "x.cube"]})
    assert not is_error, text
    assert resolve.pm.current.timelines[0].tracks[1][0].graph.luts[2] == "x.cube"


def test_dava_command_tool(session, resolve):
    server = Server(session=session)
    is_error, text = tool(server, "dava_command", {"argv": ["timeline", "list"]})
    assert not is_error
    assert json.loads(text)[0]["name"] == "Edit v1"
    is_error, text = tool(server, "dava_command", {"command": "dava marker add 30 -n 'from ai'"})
    assert not is_error, text
    assert resolve.pm.current.timelines[0].markers[30]["name"] == "from ai"
    is_error, text = tool(server, "dava_command", {"argv": ["--help"]})
    assert not is_error and "usage: dava" in text
    is_error, text = tool(server, "dava_command", {"argv": ["timeline", "frob"]})
    assert is_error and "invalid choice" in text
    is_error, text = tool(server, "dava_command", {"argv": ["batch", "x"]})
    assert is_error and "not available as a tool" in text
    is_error, text = tool(server, "dava_command", {})
    assert is_error


def test_dava_command_respects_read_only(session, resolve):
    server = Server(session=session, read_only=True)
    is_error, text = tool(server, "dava_command", {"argv": ["timeline", "create", "X"]})
    assert is_error and "read-only" in text
    is_error, _ = tool(server, "dava_command", {"argv": ["status"]})
    assert not is_error


def test_serve_keeps_stdout_clean(session, resolve, capsys):
    """Only JSON-RPC lines reach stdout, even when a command prints."""
    project = resolve.pm.current
    job = project.AddRenderJob()
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "dava_command", "arguments": {"argv": ["render", "start", job, "--wait", "--interval", "0"]}}},
    ]
    stdin = io.StringIO("\n".join(json.dumps(line) for line in lines) + "\n\nnot json\n[1, 2]\n")
    stdout = io.StringIO()
    serve(stdin=stdin, stdout=stdout, session=session)
    written = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [m.get("id") for m in written] == [1, 2, None, None]
    assert written[1]["result"]["isError"] is False
    assert written[2]["error"]["code"] == -32700
    assert written[3]["error"]["code"] == -32600
    assert all("\n" not in line for line in stdout.getvalue().splitlines())


def test_internal_error_does_not_kill_server(session, monkeypatch):
    monkeypatch.setattr(Server, "tools", lambda self: 1 / 0)
    stdin = io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 9, "method": "tools/list"}) + "\n"
                        + json.dumps({"jsonrpc": "2.0", "id": 10, "method": "ping"}) + "\n")
    stdout = io.StringIO()
    serve(stdin=stdin, stdout=stdout, session=session)
    first, second = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert first["error"]["code"] == -32603 and "ZeroDivisionError" in first["error"]["message"]
    assert second["result"] == {}


def test_stray_prints_go_to_stderr(session, monkeypatch, capsys):
    def noisy(self, name, arguments):
        print("stray output")
        return {"content": [{"type": "text", "text": "ok"}], "isError": False}

    monkeypatch.setattr(Server, "call_tool", noisy)
    stdin = io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                    "params": {"name": "x", "arguments": {}}}) + "\n")
    stdout = io.StringIO()
    serve(stdin=stdin, stdout=stdout, session=session)
    assert "stray output" not in stdout.getvalue()
    assert "stray output" in capsys.readouterr().err
    assert sys.stdout is not None
