"""End-to-end test of the Lua bridge: the real bridge.lua runs in Lua 5.1 with Resolve's console sandbox
(no io, require, os.remove/rename/execute), a mock fusion:Execute writes Resolve-style log lines, and the
Python client talks to it exactly as it would to Resolve."""

import os
import threading
import time

import pytest

from dava import bridge as dava_bridge
from dava.bridge import Session
from dava.luabridge import LuaBridgeClient, lua_literal, request_stop
from dava.remote import BridgeError

lupa = pytest.importorskip("lupa")
from lupa import lua51  # noqa: E402

BRIDGE_LUA = os.path.join(os.path.dirname(dava_bridge.__file__), "bridge.lua")

MOCK_RESOLVE = r"""
local function object(methods)
  -- Resolve API objects are userdata, not tables.
  local proxy = newproxy(true)
  getmetatable(proxy).__index = methods
  return proxy
end
local markers = {}
local timeline_methods = {}
local timeline = object(timeline_methods)
timeline_methods.GetName = function(self) return "Edit v1" end
timeline_methods.AddMarker = function(self, frame, color, name, note, duration)
  if markers[frame] then return false end
  markers[frame] = {color = color, name = name, note = note, duration = duration}
  return true
end
timeline_methods.GetMarkers = function(self) return markers end
timeline_methods.Echo = function(self, ...) return {n = select("#", ...), ...} end
timeline_methods.Same = function(self, other) return other == self end
timeline_methods.Long = function(self) return string.rep("Hello \226\128\148 ", 400) end
timeline_methods.Fail = function(self) error("boom", 0) end
local project = object({
  GetName = function(self) return "My Project" end,
  GetCurrentTimeline = function(self) return timeline end,
  GetUniqueId = function(self) return "p-1" end,
})
local pm = object({GetCurrentProject = function(self) return project end})
resolve = object({
  SCALE_FILL = 3,
  GetProductName = function(self) return "DaVinci Resolve" end,
  GetVersionString = function(self) return "21.1.0" end,
  GetProjectManager = function(self) return pm end,
  _Secret = function(self) return "hidden" end,
})
"""


@pytest.fixture
def lua_bridge(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    lua_dir = tmp_path / ".dava" / "lua"
    lua_dir.mkdir(parents=True)
    log = tmp_path / "davinci_resolve.log"
    log.write_bytes(b"0x1 | Main | INFO | start\n")

    runtime = lua51.LuaRuntime(encoding=None)
    g = runtime.globals()

    def write_log(message):
        with open(log, "ab") as handle:
            handle.write(b"0x17a467000    | Fusion | ERROR | 2026-09-25 12:00:00,000 | " + message + b"\n")

    g.py_log = write_log
    g.py_exists = lambda path: os.path.exists(path)
    g.py_sleep = lambda seconds: time.sleep(seconds)
    runtime.execute(MOCK_RESOLVE)
    runtime.execute("""
      bmd = {fileexists = function(p) return py_exists(p) end, wait = function(s) py_sleep(s) end}
      fusion = {Execute = function(self, code)
        local ok, err = pcall(loadstring(code))
        py_log(err)
      end}
      io = nil; require = nil; os.remove = nil; os.rename = nil; os.execute = nil
    """)
    source = open(BRIDGE_LUA, "rb").read()
    errors = []

    def run():
        try:
            runtime.execute(source)
        except Exception as exc:  # surface Lua errors in the test instead of a silent thread death
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    client = LuaBridgeClient(lua_dir=str(lua_dir), log_path=str(log), hello_timeout=5, call_timeout=5)
    yield client, thread, lua_dir, errors
    request_stop(str(lua_dir), wait=0.5)
    thread.join(5)
    assert not thread.is_alive()
    assert errors == []


def test_calls_objects_and_types(lua_bridge):
    client, *_ = lua_bridge
    root = client.proxy(0)
    assert root.GetProductName() == "DaVinci Resolve"
    timeline = root.GetProjectManager().GetCurrentProject().GetCurrentTimeline()
    assert timeline.GetName() == "Edit v1"
    assert timeline.AddMarker(48, "Red", "fix", "", 1) is True
    assert timeline.AddMarker(48, "Red", "fix", "", 1) is False
    assert timeline.GetMarkers() == {48: {"color": "Red", "name": "fix", "note": "", "duration": 1}}


def test_arguments_round_trip(lua_bridge):
    client, *_ = lua_bridge
    timeline = client.proxy(0).GetProjectManager().GetCurrentProject().GetCurrentTimeline()
    echoed = timeline.Echo("a\"b\\c\nd", 1.5, [1, 2], {"k": "v", 7: True}, None, "Hello — प")
    assert echoed == {"n": 6, 1: "a\"b\\c\nd", 2: 1.5, 3: [1, 2], 4: {"k": "v", 7: True}, 6: "Hello — प"}
    assert timeline.Same(timeline) is True  # objects passed back resolve to the same Lua object


def test_long_responses_are_chunked(lua_bridge):
    client, *_ = lua_bridge
    timeline = client.proxy(0).GetProjectManager().GetCurrentProject().GetCurrentTimeline()
    text = timeline.Long()
    assert text == "Hello — " * 400  # multi-byte characters split across chunks reassemble


def test_errors_and_private_names(lua_bridge):
    client, *_ = lua_bridge
    root = client.proxy(0)
    timeline = root.GetProjectManager().GetCurrentProject().GetCurrentTimeline()
    with pytest.raises(BridgeError, match="boom"):
        timeline.Fail()
    with pytest.raises(BridgeError, match="no method Nope"):
        timeline.Nope()
    with pytest.raises(BridgeError, match="not callable"):
        client.request(op="call", target=0, method="_Secret", args=[])
    assert root.GetVersionString() == "21.1.0"  # the loop survives errors


def test_dir_lists_public_methods(lua_bridge):
    client, *_ = lua_bridge
    names = dir(client.proxy(0))
    assert "GetProjectManager" in names and "_Secret" not in names


def test_dava_calls_through_the_lua_bridge(lua_bridge):
    client, *_ = lua_bridge
    session = Session(resolve=client.proxy(0))
    out = dava_bridge.call(session, "project", "GetName", unchecked=True)
    assert out["result"] == "My Project"


def test_lua_literal_escapes():
    assert lua_literal("a\"b\\\n") == '"a\\034b\\092\\010"'
    assert lua_literal({"$o": 3}) == '{["\\036o"]=3}'.replace("\\036", "$")
    assert lua_literal([None, True]) == "{DAVA_NIL,true}"
    with pytest.raises(Exception):
        lua_literal(float("nan"))


def test_normalize_resolve_lua_tables():
    from dava.luabridge import normalize

    assert normalize({1: "a", 2: "b", "__flags": 4194304}) == ["a", "b"]
    assert normalize({"__flags": 4194304}) == []
    assert normalize({48: {"color": "Red"}}) == {48: {"color": "Red"}}  # a real dict keeps int keys
    assert normalize({"k": {1: "x", "__flags": 4194304}}) == {"k": ["x"]}


def test_constants_are_values(lua_bridge):
    client, *_ = lua_bridge
    root = client.proxy(0)
    assert root.SCALE_FILL == 3
    assert root.SCALE_FILL == 3  # cached


def test_reply_written_just_before_log_rotation_is_not_lost(tmp_path):
    """Resolve renames davinci_resolve.log into its archive and starts a new file; a reply that landed in
    the old file after we started waiting must still be found, and so must one in the new file."""
    log = tmp_path / "davinci_resolve.log"
    log.write_bytes(b"start\n")
    client = LuaBridgeClient.__new__(LuaBridgeClient)
    client.log_path = str(log)
    offset = client._log_size()
    found = {}

    def wait():
        found["reply"] = client._await(7, offset, timeout=5)

    thread = threading.Thread(target=wait)
    thread.start()
    time.sleep(0.2)
    rotated = tmp_path / "archived.log"
    os.rename(log, rotated)
    with open(rotated, "ab") as old:  # the reply went into the file that was just archived
        old.write(b'x | Fusion | ERROR | t | DAVA-RESP 7 1/1 {"seq":7,"result":"old file"}\n')
    log.write_bytes(b"new log\n")
    thread.join(5)
    assert found["reply"] == {"seq": 7, "result": "old file"}

    found.clear()
    offset = client._log_size()
    thread = threading.Thread(target=lambda: found.setdefault("reply", client._await(8, offset, timeout=5)))
    thread.start()
    time.sleep(0.2)
    os.rename(log, tmp_path / "archived2.log")
    log.write_bytes(b'y | DAVA-RESP 8 1/2 {"seq":8,"res\nz | DAVA-RESP 8 2/2 ult":"new file"}\n')
    thread.join(5)
    assert found["reply"] == {"seq": 8, "result": "new file"}
