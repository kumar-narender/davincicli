import json
import os
import re

import pytest

from dava import cli
from dava.cli import EXPORT_FORMATS, LUT_EXPORT_TYPES, format_table, main
from fakes import FakeResolve


@pytest.fixture
def resolve():
    return FakeResolve()


def run(resolve, capsys, *argv):
    code = main(list(argv), resolve=resolve)
    out, err = capsys.readouterr()
    return code, out, err


def test_status_json(resolve, capsys):
    code, out, _ = run(resolve, capsys, "--json", "status")
    assert code == 0
    assert json.loads(out) == {
        "product": "DaVinci Resolve Studio",
        "version": "21.1.0",
        "page": "edit",
        "project": "Promo",
        "timeline": "Edit v1",
    }


def test_status_without_project(resolve, capsys):
    resolve.pm.current = None
    code, out, _ = run(resolve, capsys, "--json", "status")
    assert code == 0
    assert json.loads(out)["project"] is None


def test_page_switch(resolve, capsys):
    code, out, _ = run(resolve, capsys, "page", "color")
    assert code == 0
    assert resolve.page == "color"
    assert out.strip() == "color"


def test_page_rejects_unknown_name(resolve, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["page", "bogus"], resolve=resolve)
    assert exc.value.code == 2


def test_project_open_missing_fails(resolve, capsys):
    code, _, err = run(resolve, capsys, "project", "open", "Nope")
    assert code == 1
    assert "Could not open project 'Nope'" in err


def test_project_create_duplicate_fails(resolve, capsys):
    code, _, err = run(resolve, capsys, "project", "create", "Doc")
    assert code == 1
    assert "Could not create project" in err


def test_project_settings_filters_keys(resolve, capsys):
    code, out, _ = run(resolve, capsys, "--json", "project", "settings", "timelineFrameRate")
    assert code == 0
    assert json.loads(out) == {"timelineFrameRate": "24"}


def test_project_settings_unknown_key(resolve, capsys):
    code, _, err = run(resolve, capsys, "project", "settings", "nope")
    assert code == 1
    assert "nope" in err


def test_commands_need_open_project(resolve, capsys):
    resolve.pm.current = None
    code, _, err = run(resolve, capsys, "timeline", "list")
    assert code == 1
    assert "No project is open" in err


def test_timeline_list_marks_current(resolve, capsys):
    code, out, _ = run(resolve, capsys, "--json", "timeline", "list")
    rows = json.loads(out)
    assert code == 0
    assert [r["name"] for r in rows] == ["Edit v1", "Edit v2"]
    assert [r["current"] for r in rows] == ["*", ""]


def test_timeline_use_and_unknown(resolve, capsys):
    assert run(resolve, capsys, "timeline", "use", "Edit v2")[0] == 0
    assert resolve.pm.current.current.name == "Edit v2"
    code, _, err = run(resolve, capsys, "timeline", "use", "Missing")
    assert code == 1
    assert "No timeline named 'Missing'" in err


def test_timeline_create_duplicate_fails(resolve, capsys):
    assert run(resolve, capsys, "timeline", "create", "Edit v3")[0] == 0
    assert run(resolve, capsys, "timeline", "create", "Edit v3")[0] == 1


def test_timeline_export_passes_constants(resolve, capsys, tmp_path):
    target = tmp_path / "cut.edl"
    code, _, _ = run(resolve, capsys, "timeline", "export", str(target), "-f", "edl", "-t", "Edit v2")
    assert code == 0
    exports = resolve.pm.current.timelines[1].exports
    assert exports == [(str(target), FakeResolve.EXPORT_EDL, FakeResolve.EXPORT_NONE)]


def test_media_import_and_append(resolve, capsys, tmp_path):
    clip = tmp_path / "shot.mov"
    clip.write_bytes(b"")
    code, out, _ = run(resolve, capsys, "--json", "media", "import", str(clip), "--append")
    pool = resolve.pm.current.media_pool
    assert code == 0
    assert json.loads(out) == ["shot.mov"]
    assert pool.imported == [{"FilePath": str(clip)}]
    assert len(pool.appended) == 1


def test_media_import_missing_path(resolve, capsys, tmp_path):
    code, _, err = run(resolve, capsys, "media", "import", str(tmp_path / "nope.mov"))
    assert code == 1
    assert "not found" in err
    assert resolve.pm.current.media_pool.imported == []


def test_media_list_all_walks_folders(resolve, capsys):
    code, out, _ = run(resolve, capsys, "--json", "media", "list", "--all")
    assert code == 0
    assert json.loads(out) == [
        {"folder": "Master", "clip": "a.mov"},
        {"folder": "Master/Audio", "clip": "vo.wav"},
    ]


def test_marker_add_and_list(resolve, capsys):
    assert run(resolve, capsys, "marker", "add", "48", "-c", "Red", "-n", "fix")[0] == 0
    code, _, err = run(resolve, capsys, "marker", "add", "48")
    assert code == 1
    assert "frame 48" in err
    code, out, _ = run(resolve, capsys, "--json", "marker", "list")
    assert json.loads(out) == [
        {"frame": 48, "color": "Red", "duration": 1, "note": "", "name": "fix", "customData": ""}
    ]


def test_render_add_unknown_preset(resolve, capsys):
    code, _, err = run(resolve, capsys, "render", "add", "-p", "Nope")
    assert code == 1
    assert "render preset 'Nope'" in err
    assert resolve.pm.current.jobs == []


def test_render_add_start_wait(resolve, capsys, tmp_path):
    code, out, _ = run(
        resolve, capsys, "render", "add", "-p", "H.264 Master", "-o", str(tmp_path), "-n", "promo", "-t", "Edit v2"
    )
    project = resolve.pm.current
    job_id = out.strip()
    assert code == 0
    assert project.preset == "H.264 Master"
    assert project.render_settings == {"TargetDir": str(tmp_path), "CustomName": "promo"}
    assert project.jobs[0]["TimelineName"] == "Edit v2"

    code, out, _ = run(resolve, capsys, "--json", "render", "start", "--wait", "--interval", "0")
    rows = json.loads(out)
    assert code == 0
    assert project.started == [job_id]
    assert rows[0]["status"] == "Complete"
    assert rows[0]["output"] == str(tmp_path / "promo.mov")


def test_render_wait_reports_failure(resolve, capsys):
    project = resolve.pm.current
    job_id = project.AddRenderJob()

    def fail():
        project.job_status[job_id] = {"JobStatus": "Failed", "CompletionPercentage": 10, "Error": "disk full"}
        return False

    project.IsRenderingInProgress = fail
    code, _, err = run(resolve, capsys, "render", "start", job_id, "--wait")
    assert code == 1
    assert "disk full" in err


def test_render_start_empty_queue(resolve, capsys):
    code, _, err = run(resolve, capsys, "render", "start")
    assert code == 1
    assert "queue is empty" in err


def test_format_table_aligns_columns():
    text = format_table([{"a": "1", "bb": "x"}, {"a": "222", "bb": ""}])
    assert text.splitlines() == ["a    bb", "1    x", "222"]


STUB = "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/DaVinciResolveScript.pyi"


@pytest.mark.skipif(not os.path.exists(STUB), reason="Resolve scripting SDK not installed")
def test_export_constants_exist_in_resolve_api():
    stub = open(STUB).read()
    for type_name, subtype_name in EXPORT_FORMATS.values():
        assert f"\t{type_name}: TimelineExportType" in stub
        assert f"\t{subtype_name}: TimelineExportSubtype" in stub


@pytest.mark.skipif(not os.path.exists(STUB), reason="Resolve scripting SDK not installed")
def test_lut_export_constants_exist_in_resolve_api():
    stub = open(STUB).read()
    for name in LUT_EXPORT_TYPES.values():
        assert f"\t{name}: ExportLutType" in stub


# Standard-library names that look like API calls to the scan below.
STDLIB_CALLS = {"ArgumentParser", "ArgumentTypeError", "Popen", "StringIO", "RawDescriptionHelpFormatter", "ZipFile",
                "DefaultSelector", "TemporaryDirectory", "ArgumentError", "ThreadPoolExecutor"}


@pytest.mark.skipif(not os.path.exists(STUB), reason="Resolve scripting SDK not installed")
def test_every_called_resolve_method_exists_in_api():
    """Every obj.Method( call in dava's sources is in the API definition, or declared as Fusion API.

    The Fusion scripting API is not in the definition; modules that use it list those names in a
    module-level FUSION_API_METHODS set, so each unverifiable call is declared where it is made.
    """
    import importlib
    import pathlib

    stub = open(STUB).read()
    root = pathlib.Path(cli.__file__).parent
    # Classes and functions dava defines itself are not API calls.
    own = {n for path in root.rglob("*.py") for n in re.findall(r"^\s*(?:class|def) (\w+)", path.read_text(), re.M)}
    missing = {}
    for path in sorted(root.rglob("*.py")):
        if path.name == "__main__.py":  # importing it would run the CLI
            continue
        module_name = "dava." + ".".join(path.relative_to(root).with_suffix("").parts)
        module = importlib.import_module(module_name.removesuffix(".__init__"))
        declared = getattr(module, "FUSION_API_METHODS", set())
        called = set(re.findall(r"\.([A-Z][A-Za-z0-9]+)\(", path.read_text()))
        unknown = sorted(n for n in called - STDLIB_CALLS - declared - own if f"def {n}(" not in stub)
        if unknown:
            missing[path.name] = unknown
    assert missing == {}
