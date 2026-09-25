"""Regression tests for the findings of the adversarial review of the bridge, CLI and MCP server."""

import json
import os
import subprocess
import sys

import pytest

from dava import bridge
from dava.bridge import Session, resolve_ref
from dava.cli import main
from dava.connect import ResolveError, stub_path
from dava.mcp import Server, serve
from dava.spec import Spec
from fakes import FakeColorGroup, FakeFolder, FakeResolve, FakeTimeline

MINI = os.path.join(os.path.dirname(__file__), "data", "mini.pyi")
REPO = os.path.dirname(os.path.dirname(__file__))


@pytest.fixture
def spec():
    with open(MINI) as handle:
        return Spec(handle.read(), MINI)


@pytest.fixture
def resolve():
    return FakeResolve()


@pytest.fixture
def session(resolve, spec):
    return Session(resolve=resolve, spec=spec)


@pytest.fixture(autouse=True)
def mini_spec(monkeypatch, spec):
    monkeypatch.setattr(bridge, "load_spec", lambda: spec)
    monkeypatch.delenv("DAVA_READ_ONLY", raising=False)


def project(resolve):
    return resolve.pm.current


def run(resolve, capsys, *argv):
    code = main(list(argv), resolve=resolve)
    out, err = capsys.readouterr()
    return code, out, err


def tool(server, name, arguments):
    reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": name, "arguments": arguments}})
    return reply


# --- handles ------------------------------------------------------------------


def test_auto_handles_never_overwrite_user_handles(session, resolve):
    items = project(resolve).timelines[0].tracks[1]
    bridge.call(session, "item:video:1:2", "GetNodeGraph", save_as="g2")
    bridge.call(session, "item:video:1:1", "GetNodeGraph")  # auto handle
    bridge.call(session, "item:video:1:1", "GetNodeGraph")  # auto handle
    bridge.call(session, "$g2", "SetLUT", ["1", "x.cube"], raw=True)
    assert items[1].graph.luts[1] == "x.cube"
    assert items[0].graph.luts[1] == ""
    session.handles["h3"] = ("taken", "Object")  # a later auto handle must skip a taken name
    session._counter = 2
    out = bridge.call(session, "item:video:1:1", "GetNodeGraph")
    assert out["result"]["$ref"] == "$h4"


@pytest.mark.parametrize("name, message", [("h2", "reserved"), ("a::b", "letters"), ("1x", "letters"), (5, "string")])
def test_bad_handle_names_are_rejected_before_calling(session, resolve, name, message):
    with pytest.raises(ResolveError, match=message):
        bridge.call(session, "timeline", "SetName", ["New"], raw=True, save_as=name)
    assert project(resolve).timelines[0].name == "Edit v1"


def test_as_on_non_object_result_is_refused_before_calling(session, resolve):
    with pytest.raises(ResolveError, match="--as needs a method that returns one object"):
        bridge.call(session, "timeline", "SetName", ["New"], raw=True, save_as="x")
    assert project(resolve).timelines[0].name == "Edit v1"


# --- refs -----------------------------------------------------------------------


def test_names_are_matched_exactly_including_spaces(session, resolve):
    timelines = project(resolve).timelines
    timelines.append(FakeTimeline("Final"))
    timelines.append(FakeTimeline("Final "))
    assert resolve_ref(session, "timeline:Final ")[0] is timelines[-1]
    assert resolve_ref(session, "timeline:Final")[0] is timelines[-2]


def test_colorgroup_refs_round_trip_or_fall_back_to_handles(session, resolve):
    groups = project(resolve).color_groups
    groups.extend([FakeColorGroup("Warm::postgraph"), FakeColorGroup("Dup"), FakeColorGroup("Dup")])
    assert bridge.describe_object(session, groups[0], "ColorGroup")["$ref"] == "colorgroup:Warm"
    assert bridge.describe_object(session, groups[1], "ColorGroup")["$ref"].startswith("$h")
    assert bridge.describe_object(session, groups[2], "ColorGroup")["$ref"].startswith("$h")
    assert resolve_ref(session, "colorgroup@2")[0] is groups[1]
    with pytest.raises(ResolveError, match="colorgroup@N"):
        resolve_ref(session, "colorgroup:Dup")


def test_album_positions_and_refs(session, resolve):
    gallery = project(resolve).gallery
    assert resolve_ref(session, "album@2")[0] is gallery.albums[1]
    assert bridge.describe_object(session, gallery.albums[1], "GalleryStillAlbum")["$ref"] == "album:Looks"


def test_graph_separator_is_strict(session):
    for bad in ("item:video:1:2::graph@2", "item:video:1:2::graph#2"):
        with pytest.raises(ResolveError, match="not a valid child"):
            resolve_ref(session, bad)


def test_comp_names_win_over_indexes(session, resolve):
    item = project(resolve).timelines[0].tracks[1][0]
    item.comps = ["2", "Composition 1"]
    assert resolve_ref(session, "item::comp:2")[0].name == "2"
    assert resolve_ref(session, "item::comp@2")[0].name == "Composition 1"
    with pytest.raises(ResolveError, match="no Fusion composition"):
        resolve_ref(session, "item::comp:²")


def test_null_intermediate_is_a_clean_error(session):
    session.handles["it"] = (None, "TimelineItem")
    with pytest.raises(ResolveError, match="'\\$it' is null; cannot take ::graph"):
        resolve_ref(session, "$it::graph")


def test_item_refs_share_one_index_per_call(session, resolve):
    timeline = project(resolve).timelines[0]
    items = timeline.tracks[1]
    counts = {"n": 0}
    for item in items:
        original = item.GetUniqueId
        item.GetUniqueId = lambda original=original: (counts.__setitem__("n", counts["n"] + 1), original())[1]
    refs = [f"item#{i.uid}" for i in items] * 20
    bridge.call(session, "timeline", "DeleteClips", [("json", refs)])
    assert len(timeline.deleted[0]) == 40
    assert counts["n"] <= len(items) * 2  # one index build, not one scan per ref


def test_clip_path_ref_does_not_walk_the_pool(session, resolve):
    root = project(resolve).media_pool.root
    calls = {"n": 0}
    original = root.GetClipList
    root.GetClipList = lambda: (calls.__setitem__("n", calls["n"] + 1), original())[1]
    resolve_ref(session, "clip:/Master/Audio/vo.wav")
    assert calls["n"] == 0


def test_ambiguity_hint_avoids_unresolvable_paths(session, resolve):
    root = project(resolve).media_pool.root
    root.subfolders.append(FakeFolder("Day 1/2", ["a.mov"]))
    with pytest.raises(ResolveError, match="contain '/'; use clip#ID"):
        resolve_ref(session, "clip:a.mov")


# --- coercion -------------------------------------------------------------------


def test_numeric_constants_are_validated(session, resolve, tmp_path):
    out = str(tmp_path / "x.edl")
    bridge.call(session, "timeline", "Export", [out, str(FakeResolve.EXPORT_EDL), "EXPORT_NONE"], raw=True)
    with pytest.raises(ResolveError, match="not the value of any TimelineExportType"):
        bridge.call(session, "timeline", "Export", [("json", out), ("json", 999), ("json", 0.0)])


def test_int_from_float_text(session, resolve):
    bridge.call(session, "timeline", "AddMarker", ["48.0", "Blue", "n", "", "1.0"], raw=True)
    assert 48 in project(resolve).timelines[0].markers


def test_json_looking_text_for_str_parameters(session, resolve):
    bridge.call(session, "timeline", "SetName", ["[1]"], raw=True)
    assert project(resolve).timelines[0].name == "[1]"


@pytest.mark.skipif(not os.path.isfile(stub_path() or ""), reason="Resolve scripting SDK not installed")
def test_functional_typeddict_fields_keep_docs():
    with open(stub_path(), encoding="utf-8") as handle:
        spec = Spec(handle.read())
    assert "Super Scale" in spec.typeddicts["ClipProperties"].fields["Super Scale"][2]


# --- CLI ------------------------------------------------------------------------


def test_batch_keeps_hash_inside_refs(resolve, capsys, tmp_path):
    script = tmp_path / "b.dava"
    script.write_text("call 'timeline#id-Edit v2' GetName   # trailing comment\n# whole-line comment\n")
    code, out, err = run(resolve, capsys, "--json", "batch", str(script))
    assert code == 0, err
    assert json.loads(out.splitlines()[0])["result"] == "Edit v2"


def test_batch_line_read_only_flag_is_honoured(resolve, capsys, tmp_path):
    script = tmp_path / "b.dava"
    script.write_text("--read-only timeline create X\n")
    code, _, err = run(resolve, capsys, "batch", str(script))
    assert code == 1 and "read-only" in err
    assert "X" not in [t.name for t in project(resolve).timelines]


@pytest.mark.parametrize("value, blocked", [("off", False), ("OFF", False), ("n", False), ("f", False),
                                            ("1", True), ("yes", True), ("anything", True)])
def test_read_only_env_words(resolve, capsys, monkeypatch, value, blocked):
    monkeypatch.setenv("DAVA_READ_ONLY", value)
    code, _, _ = run(resolve, capsys, "timeline", "create", "Env")
    assert (code == 1) is blocked


def test_batch_missing_file(resolve, capsys, tmp_path):
    code, _, err = run(resolve, capsys, "batch", str(tmp_path / "nope.dava"))
    assert code == 1 and "Cannot read batch file" in err


def test_call_argument_conflicts(resolve, capsys):
    code, _, err = run(resolve, capsys, "call", "timeline", "SetName", "x", "--args", '["y"]')
    assert code == 1 and "either inline or with --args" in err
    code, _, err = run(resolve, capsys, "call", "timeline", "SetName", "timelineName=x", "--kwargs",
                       '{"timelineName": "y"}')
    assert code == 1 and "both inline and in --kwargs" in err


def test_batch_json_keep_going_prints_error_lines(resolve, capsys, tmp_path):
    script = tmp_path / "b.dava"
    script.write_text("timeline use Missing\ntimeline list\n")
    code, out, _ = run(resolve, capsys, "--json", "batch", str(script), "-k")
    lines = [json.loads(line) for line in out.splitlines()]
    assert code == 1
    assert lines[0]["line"] == 1 and "Missing" in lines[0]["error"]
    assert lines[1][0]["name"] == "Edit v1"


@pytest.mark.parametrize("argv", [["render", "start", "--wait", "--interval", "-1"], ["api", "search", "x", "--limit", "0"]])
def test_invalid_numbers_are_usage_errors(resolve, argv):
    with pytest.raises(SystemExit) as exc:
        main(argv, resolve=resolve)
    assert exc.value.code == 2


def test_broken_pipe_is_quiet():
    result = subprocess.run(f"{sys.executable} -m dava api refs | head -1", shell=True, cwd=REPO,
                            capture_output=True, text=True)
    assert "Traceback" not in result.stderr


# --- MCP ------------------------------------------------------------------------


@pytest.mark.parametrize("arguments, message", [
    ({"ref": "timeline", "method": "SetName", "args": {"a": 1}}, "args must be array"),
    ({"ref": "timeline", "method": "SetName", "args": ["x"], "save_as": 5}, "save_as must be string"),
    ({"ref": "timeline", "method": "SetName", "args": ["x"], "dry_run": "false"}, "dry_run must be boolean"),
    ({"ref": "timeline", "method": "SetName", "args": ["x"], "bogus": 1}, "Unknown argument"),
])
def test_mcp_argument_validation(session, resolve, arguments, message):
    reply = tool(Server(session=session), "resolve_call", arguments)
    result = reply["result"]
    assert result["isError"] and message in result["content"][0]["text"]
    assert project(resolve).timelines[0].name == "Edit v1"


def test_mcp_search_limit_validated(session):
    reply = tool(Server(session=session), "resolve_api_search", {"query": "marker", "limit": 0})
    assert reply["result"]["isError"]


@pytest.mark.parametrize("bad_id", [None, True, 1.5, {"a": 1}])
def test_mcp_invalid_ids_are_not_executed(session, resolve, bad_id):
    reply = Server(session=session).handle({"jsonrpc": "2.0", "id": bad_id, "method": "tools/call",
                                            "params": {"name": "resolve_call",
                                                       "arguments": {"ref": "timeline", "method": "SetName", "args": ["Z"]}}})
    assert reply["error"]["code"] == -32600 and reply["id"] is None
    assert project(resolve).timelines[0].name == "Edit v1"


def test_mcp_params_must_be_objects(session):
    server = Server(session=session)
    assert server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": [1]})["error"]["code"] == -32602
    reply = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "x", "arguments": [1]}})
    assert reply["error"]["code"] == -32602


def test_mcp_bad_bytes_and_nan_do_not_kill_the_server(session):
    import io
    stdin = [b'\xff\xfe not utf-8\n', b'{"jsonrpc": "2.0", "id": NaN, "method": "ping"}\n',
             json.dumps({"jsonrpc": "2.0", "id": 3, "method": "ping"}).encode() + b"\n"]
    stdout = io.StringIO()
    serve(stdin=stdin, stdout=stdout, session=session)
    replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [r.get("error", {}).get("code") for r in replies] == [-32700, -32700, None]
    assert replies[2]["result"] == {}


def test_mcp_native_writes_cannot_reach_the_protocol_stream(tmp_path):
    script = tmp_path / "noisy.py"
    script.write_text(
        "import os, sys\n"
        "from dava import mcp\n"
        "def noisy(self, name, arguments):\n"
        "    os.write(1, b'native noise\\n')\n"
        "    return {'content': [{'type': 'text', 'text': 'ok'}], 'isError': False}\n"
        "mcp.Server.call_tool = noisy\n"
        "mcp.serve()\n"
    )
    message = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "x", "arguments": {}}})
    result = subprocess.run([sys.executable, str(script)], input=message + "\n", capture_output=True, text=True, cwd=REPO)
    assert result.stdout.splitlines() == [json.dumps({"jsonrpc": "2.0", "id": 1, "result": {
        "content": [{"type": "text", "text": "ok"}], "isError": False}})]
    assert "native noise" in result.stderr


def test_mcp_help_has_no_ansi_colours(session, monkeypatch):
    monkeypatch.setenv("FORCE_COLOR", "1")
    reply = tool(Server(session=session), "dava_command", {"argv": ["timeline", "--help"]})
    assert "\x1b[" not in reply["result"]["content"][0]["text"]


def test_generated_tools_follow_the_cli(session, resolve):
    server = Server(session=session)
    tools = {t["name"]: t for t in server.tools()}
    assert tools["dava_timeline_list"]["annotations"]["readOnlyHint"] is True
    assert tools["dava_timeline_create"]["annotations"]["readOnlyHint"] is False
    schema = tools["dava_marker_add"]["inputSchema"]
    assert schema["required"] == ["frame"] and schema["properties"]["color"]["enum"][0] == "Blue"
    reply = tool(server, "dava_marker_add", {"frame": 12, "color": "Red", "name": "-dash"})
    assert not reply["result"]["isError"], reply
    assert project(resolve).timelines[0].markers[12]["name"] == "-dash"
    ro_names = {t["name"] for t in Server(session=session, read_only=True).tools()}
    assert "dava_timeline_create" not in ro_names and "dava_page" in ro_names and "dava_timeline_list" in ro_names


def test_generated_tool_positional_starting_with_dash(session, resolve):
    reply = tool(Server(session=session), "dava_timeline_create", {"name": "-cut"})
    assert not reply["result"]["isError"], reply
    assert project(resolve).timelines[-1].name == "-cut"
