import json
import os

import pytest

from dava import bridge
from dava.cli import main
from dava.spec import Spec
from fakes import FakeResolve

MINI = os.path.join(os.path.dirname(__file__), "data", "mini.pyi")


@pytest.fixture(autouse=True)
def mini_spec(monkeypatch):
    with open(MINI) as handle:
        spec = Spec(handle.read(), MINI)
    monkeypatch.setattr(bridge, "load_spec", lambda: spec)
    monkeypatch.delenv("DAVA_READ_ONLY", raising=False)
    return spec


@pytest.fixture
def resolve():
    return FakeResolve()


def run(resolve, capsys, *argv):
    code = main(list(argv), resolve=resolve)
    out, err = capsys.readouterr()
    return code, out, err


def test_call_prints_json_result(resolve, capsys):
    code, out, _ = run(resolve, capsys, "call", "timeline:Edit v2", "GetName")
    assert code == 0
    assert json.loads(out) == {"ref": "timeline:Edit v2", "method": "Timeline.GetName", "result": "Edit v2"}


def test_call_argument_forms(resolve, capsys):
    code, _, err = run(resolve, capsys, "call", "timeline", "AddMarker", "10", "color=Red", "name=hi",
                       "note=", "duration:=3")
    assert code == 0, err
    assert resolve.pm.current.timelines[0].markers[10]["duration"] == 3
    code, _, _ = run(resolve, capsys, "call", "timeline", "AddMarker", "--args", '[11, "Blue", "a", "b", 1]')
    assert code == 0
    code, _, _ = run(resolve, capsys, "call", "timeline", "AddMarker", "12", "--kwargs",
                     '{"color": "Green", "name": "", "note": "", "duration": 1}')
    assert code == 0
    assert sorted(resolve.pm.current.timelines[0].markers) == [10, 11, 12]


def test_call_positional_with_equals_needs_json(resolve, capsys):
    # 'a=b' looks like NAME=VALUE; a literal string with '=' goes through --args.
    code, _, err = run(resolve, capsys, "call", "timeline", "SetName", "a=b")
    assert code == 1
    assert "no parameter 'a'" in err
    assert run(resolve, capsys, "call", "timeline", "SetName", "--args", '["a=b"]')[0] == 0
    assert resolve.pm.current.timelines[0].name == "a=b"


def test_call_bad_json(resolve, capsys):
    code, _, err = run(resolve, capsys, "call", "timeline", "AddMarker", "--args", "[1,")
    assert code == 1
    assert "--args: invalid JSON" in err


def test_call_dry_run_and_read_only(resolve, capsys, monkeypatch):
    code, out, _ = run(resolve, capsys, "call", "timeline", "SetName", "X", "--dry-run")
    assert code == 0
    assert json.loads(out)["args"] == ["X"]
    assert resolve.pm.current.timelines[0].name == "Edit v1"
    code, _, err = run(resolve, capsys, "--read-only", "call", "timeline", "SetName", "X")
    assert code == 1
    assert "read-only" in err
    monkeypatch.setenv("DAVA_READ_ONLY", "1")
    code, _, err = run(resolve, capsys, "call", "timeline", "SetName", "X")
    assert code == 1
    assert run(resolve, capsys, "call", "timeline", "GetName")[0] == 0
    assert resolve.pm.current.timelines[0].name == "Edit v1"


@pytest.mark.parametrize("argv", [
    ["project", "create", "X"], ["timeline", "create", "X"], ["page", "color"], ["meta", "color", "a.mov", "Teal"],
    ["render", "start"], ["quit"], ["color", "reset"],
])
def test_read_only_blocks_mutating_commands(resolve, capsys, argv):
    code, _, err = run(resolve, capsys, "--read-only", *argv)
    assert code == 1
    assert "read-only mode is on" in err


@pytest.mark.parametrize("argv", [
    ["status"], ["page"], ["timeline", "list"], ["meta", "color", "a.mov"], ["render", "jobs"], ["api", "classes"],
])
def test_read_only_allows_reads(resolve, capsys, argv):
    code, _, err = run(resolve, capsys, "--read-only", *argv)
    assert code == 0, err


def test_batch_read_only_checks_every_line_first(resolve, capsys, tmp_path):
    script = tmp_path / "b.dava"
    script.write_text("timeline list\ntimeline create X\n")
    code, _, err = run(resolve, capsys, "--read-only", "batch", str(script))
    assert code == 1
    assert "b.dava:2" in err
    assert [t.name for t in resolve.pm.current.timelines] == ["Edit v1", "Edit v2"]


def test_batch_shares_handles(resolve, capsys, tmp_path):
    script = tmp_path / "b.dava"
    script.write_text(
        "call item:video:1:2 GetNodeGraph --as g\n"
        "call $g SetLUT 1 Film/Look.cube\n"
        "call $g GetNumNodes\n"
    )
    code, out, err = run(resolve, capsys, "--json", "batch", str(script))
    assert code == 0, err
    lines = [json.loads(line) for line in out.splitlines()]
    assert lines[0]["saved_as"] == "$g"
    assert lines[2]["result"] == 2
    assert resolve.pm.current.timelines[0].tracks[1][1].graph.luts[1] == "Film/Look.cube"


def test_inspect(resolve, capsys):
    code, out, _ = run(resolve, capsys, "inspect", "colorgroup:Warm::postgraph")
    info = json.loads(out)
    assert code == 0
    assert info["$type"] == "Graph"
    assert "Graph.SetLUT(nodeIndex: int, lutPath: str) -> bool" in info["methods"]


def test_api_commands_need_no_resolve(capsys, monkeypatch):
    monkeypatch.setattr("dava.cli.get_resolve", lambda: pytest.fail("api commands must not connect"))
    assert main(["api", "classes"]) == 0
    assert "Timeline" in capsys.readouterr().out
    assert main(["--json", "api", "methods", "graph"]) == 0
    methods = json.loads(capsys.readouterr().out)
    assert {m["method"]: m["read_only"] for m in methods} == {"GetNumNodes": True, "SetLUT": False}
    assert main(["--json", "api", "show", "AppendClipInfo"]) == 0
    assert json.loads(capsys.readouterr().out)["fields"][0]["name"] == "mediaPoolItem"
    assert main(["--json", "api", "constants", "EXPORT_"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 4
    assert main(["api", "refs"]) == 0
    assert "colorgroup:NAME" in capsys.readouterr().out
    assert main(["--json", "api", "dump"]) == 0
    assert "Timeline.AddMarker" in json.loads(capsys.readouterr().out)["methods"]
    assert main(["api", "search", "nothing-matches-this"]) == 1
    assert main(["api", "methods", "Fusion"]) == 0
    assert "opaque" in capsys.readouterr().out
    assert main(["api", "methods", "Nope"]) == 1


def test_missing_stub_is_a_clear_error(capsys, monkeypatch):
    from dava.connect import ResolveError

    def missing():
        raise ResolveError("API definition not found")

    monkeypatch.setattr(bridge, "load_spec", missing)
    assert main(["api", "classes"]) == 1
    assert "API definition not found" in capsys.readouterr().err
