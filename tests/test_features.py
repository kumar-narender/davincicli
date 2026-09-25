import json
import subprocess

import pytest

from dava import cli
from dava.cli import main
from fakes import FakeClip, FakeFolder, FakeResolve


@pytest.fixture
def resolve():
    return FakeResolve()


@pytest.fixture
def items(resolve):
    return resolve.pm.current.timelines[0].tracks[1]


def run(resolve, capsys, *argv):
    code = main(list(argv), resolve=resolve)
    out, err = capsys.readouterr()
    return code, out, err


# --- color ------------------------------------------------------------------


def test_color_nodes_lists_every_node_of_every_clip(resolve, capsys):
    code, out, _ = run(resolve, capsys, "--json", "color", "nodes")
    rows = json.loads(out)
    assert code == 0
    assert [(r["clip"], r["node"]) for r in rows] == [(1, 1), (1, 2), (2, 1), (2, 2)]
    assert rows[0]["tools"] == "Primary"


def test_color_lut_applies_to_selected_clip_only(resolve, items, capsys):
    code, _, _ = run(resolve, capsys, "color", "lut", "Film/Kodak.cube", "-N", "2", "-i", "2")
    assert code == 0
    assert resolve.pm.current.lut_refreshes == 1
    assert items[0].graph.luts[2] == ""
    assert items[1].graph.luts[2] == "Film/Kodak.cube"


def test_color_lut_rejects_bad_node_and_clip(resolve, capsys):
    code, _, err = run(resolve, capsys, "color", "lut", "x.cube", "-N", "3")
    assert code == 1
    assert "Node 3 is out of range" in err
    code, _, err = run(resolve, capsys, "color", "lut", "x.cube", "-i", "9")
    assert code == 1
    assert "Clip index 9 is out of range" in err


def test_color_lut_reports_resolve_failure(resolve, capsys):
    code, _, err = run(resolve, capsys, "color", "lut", "unknown.3dl")
    assert code == 1
    assert "Could not set LUT" in err


def test_color_on_empty_track(resolve, capsys):
    code, _, err = run(resolve, capsys, "color", "nodes", "-T", "2")
    assert code == 1
    assert "Video track 2" in err


def test_color_cdl_builds_payload(resolve, items, capsys):
    code, _, _ = run(resolve, capsys, "color", "cdl", "--slope", "1.1  1.0 0.9", "--saturation", "0.8", "-i", "1")
    assert code == 0
    assert items[0].cdls == [{"NodeIndex": 1, "Slope": "1.1 1.0 0.9", "Saturation": 0.8}]
    assert items[1].cdls == []


def test_color_cdl_validation(resolve, items, capsys):
    code, _, err = run(resolve, capsys, "color", "cdl", "--power", "1 1")
    assert code == 1
    assert "three values" in err
    code, _, err = run(resolve, capsys, "color", "cdl")
    assert code == 1
    assert "at least one" in err
    assert items[0].cdls == []


def test_color_drx_and_reset(resolve, items, capsys, tmp_path):
    drx = tmp_path / "look.drx"
    drx.write_text("")
    assert run(resolve, capsys, "color", "drx", str(drx), "-k", "source-tc")[0] == 0
    assert items[0].graph.drx == [(str(drx), 1)]
    assert items[1].graph.drx == [(str(drx), 1)]
    code, _, err = run(resolve, capsys, "color", "drx", str(tmp_path / "missing.drx"))
    assert code == 1
    assert "not found" in err
    assert run(resolve, capsys, "color", "reset", "-i", "2")[0] == 0
    assert (items[0].graph.reset, items[1].graph.reset) == (False, True)


def test_color_export_lut_requires_index(resolve, items, capsys, tmp_path):
    with pytest.raises(SystemExit):
        main(["color", "export-lut", "out.cube"], resolve=resolve)
    capsys.readouterr()
    code, _, _ = run(resolve, capsys, "color", "export-lut", str(tmp_path / "g.cube"), "-i", "1", "-s", "65")
    assert code == 0
    assert items[0].lut_exports == [(FakeResolve.EXPORT_LUT_65PTCUBE, str(tmp_path / "g.cube"))]


# --- stills -----------------------------------------------------------------


def test_still_list_all_albums_and_one(resolve, capsys):
    code, out, _ = run(resolve, capsys, "--json", "still", "list")
    assert code == 0
    assert [(r["album"], r["label"]) for r in json.loads(out)] == [
        ("Stills 1", "1.1.1"), ("Stills 1", "1.2.1"), ("Looks", "warm")
    ]
    _, out, _ = run(resolve, capsys, "--json", "still", "list", "-a", "Looks")
    assert len(json.loads(out)) == 1
    code, _, err = run(resolve, capsys, "still", "list", "-a", "Nope")
    assert code == 1
    assert "No still album named 'Nope'" in err


def test_still_grab_single_and_all(resolve, capsys):
    album = resolve.pm.current.gallery.current
    assert run(resolve, capsys, "still", "grab")[0] == 0
    assert album.stills[-1].label == "grabbed"
    code, out, _ = run(resolve, capsys, "still", "grab", "--all", "middle")
    assert code == 0
    assert "Grabbed 2 still(s)" in out
    assert album.stills[-1].label == "grab-2"


def test_still_export_creates_dir(resolve, capsys, tmp_path):
    target = tmp_path / "stills"
    code, _, _ = run(resolve, capsys, "still", "export", str(target), "-a", "Looks", "-f", "png")
    assert code == 0
    assert target.is_dir()
    assert resolve.pm.current.gallery.albums[1].exports == [(1, str(target), "still", "png")]


# --- metadata ---------------------------------------------------------------


def test_meta_get_set_roundtrip(resolve, capsys):
    assert run(resolve, capsys, "meta", "set", "vo.wav", "Scene=12", "Take=3")[0] == 0
    code, out, _ = run(resolve, capsys, "--json", "meta", "get", "vo.wav", "Scene", "Take", "Shot")
    assert code == 0
    assert json.loads(out) == {"Scene": "12", "Take": "3", "Shot": ""}


def test_meta_third_party_is_separate(resolve, capsys):
    assert run(resolve, capsys, "meta", "set", "a.mov", "vendor=x", "--third-party")[0] == 0
    _, out, _ = run(resolve, capsys, "--json", "meta", "get", "a.mov")
    assert "vendor" not in json.loads(out)
    _, out, _ = run(resolve, capsys, "--json", "meta", "get", "a.mov", "--third-party")
    assert json.loads(out) == {"vendor": "x"}


def test_meta_set_rejects_malformed_pair(resolve, capsys):
    code, _, err = run(resolve, capsys, "meta", "set", "a.mov", "Scene")
    assert code == 1
    assert "KEY=VALUE" in err


def test_meta_unknown_and_ambiguous_clip(resolve, capsys):
    code, _, err = run(resolve, capsys, "meta", "get", "nope.mov")
    assert code == 1
    assert "No media pool clip named" in err
    resolve.pm.current.media_pool.root.subfolders.append(FakeFolder("B-roll", ["a.mov"]))
    code, _, err = run(resolve, capsys, "meta", "get", "a.mov")
    assert code == 1
    assert "Master, Master/B-roll" in err


def test_meta_props_and_read_only(resolve, capsys):
    code, out, _ = run(resolve, capsys, "--json", "meta", "props", "a.mov", "FPS")
    assert json.loads(out) == {"FPS": "24"}
    code, _, err = run(resolve, capsys, "meta", "set-prop", "a.mov", "FPS=25")
    assert code == 1
    assert "FPS" in err
    assert run(resolve, capsys, "meta", "set-prop", "a.mov", "Clip Name=hero")[0] == 0


def test_meta_color(resolve, capsys):
    clip = resolve.pm.current.media_pool.root.clips[0]
    assert run(resolve, capsys, "meta", "color", "a.mov", "Teal")[0] == 0
    assert clip.color == "Teal"
    _, out, _ = run(resolve, capsys, "meta", "color", "a.mov")
    assert out.strip() == "Teal"
    assert run(resolve, capsys, "meta", "color", "a.mov", "none")[0] == 0
    assert clip.color == ""


# --- fusion -----------------------------------------------------------------


def test_fusion_lifecycle(resolve, items, capsys, tmp_path):
    comp = tmp_path / "title.comp"
    comp.write_text("")
    assert run(resolve, capsys, "fusion", "add", "-i", "1")[0] == 0
    assert run(resolve, capsys, "fusion", "import", str(comp), "-i", "1")[0] == 0
    assert run(resolve, capsys, "fusion", "rename", "title.comp", "Title", "-i", "1")[0] == 0
    _, out, _ = run(resolve, capsys, "--json", "fusion", "list")
    assert json.loads(out)[0]["comps"] == "Composition 1, Title"

    out_file = tmp_path / "exported.comp"
    assert run(resolve, capsys, "fusion", "export", str(out_file), "-c", "2", "-i", "1")[0] == 0
    assert out_file.read_text() == "Title"
    code, _, err = run(resolve, capsys, "fusion", "export", str(out_file), "-c", "3", "-i", "1")
    assert code == 1
    assert "out of range" in err

    assert run(resolve, capsys, "fusion", "delete", "Title", "-i", "1")[0] == 0
    assert items[0].comps == ["Composition 1"]
    assert run(resolve, capsys, "fusion", "delete", "Title", "-i", "1")[0] == 1


# --- render all timelines ---------------------------------------------------


def test_render_add_all_timelines_names_outputs(resolve, capsys, tmp_path):
    code, out, _ = run(resolve, capsys, "--json", "render", "add", "--all-timelines", "-n", "promo", "-o", str(tmp_path))
    rows = json.loads(out)
    jobs = resolve.pm.current.jobs
    assert code == 0
    assert [r["timeline"] for r in rows] == ["Edit v1", "Edit v2"]
    assert [j["TimelineName"] for j in jobs] == ["Edit v1", "Edit v2"]
    assert [j["OutputFilename"] for j in jobs] == ["promo_Edit v1.mov", "promo_Edit v2.mov"]


def test_render_add_timeline_and_all_are_exclusive(resolve, capsys):
    with pytest.raises(SystemExit):
        main(["render", "add", "-t", "Edit v1", "--all-timelines"], resolve=resolve)


# --- launch / quit / batch --------------------------------------------------


class FakeModule:
    def __init__(self, ready_after):
        self.calls = 0
        self.ready_after = ready_after

    def scriptapp(self, name):
        self.calls += 1
        return FakeResolve() if self.calls > self.ready_after else None


class FakeProcess:
    pid = 4242
    returncode = None

    def poll(self):
        return self.returncode


def test_launch_starts_headless_and_waits(monkeypatch, capsys, tmp_path):
    app = tmp_path / "Resolve"
    app.write_text("")
    started = []
    monkeypatch.setenv("RESOLVE_APP", str(app))
    monkeypatch.setattr(cli, "load_script_module", lambda: FakeModule(ready_after=2))
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: started.append(cmd) or FakeProcess())
    code = main(["launch", "--nogui", "--interval", "0"])
    out = capsys.readouterr().out
    assert code == 0
    assert started == [[str(app), "-nogui"]]
    assert "headless (pid 4242)" in out


def test_launch_skips_when_running(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_script_module", lambda: FakeModule(ready_after=0))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("must not start Resolve"))
    assert main(["launch"]) == 0
    assert "already running" in capsys.readouterr().out


def test_launch_reports_early_exit(monkeypatch, capsys, tmp_path):
    app = tmp_path / "Resolve"
    app.write_text("")
    process = FakeProcess()
    process.returncode = 3
    monkeypatch.setenv("RESOLVE_APP", str(app))
    monkeypatch.setattr(cli, "load_script_module", lambda: FakeModule(ready_after=99))
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: process)
    assert main(["launch", "--interval", "0"]) == 1
    assert "exited during startup with code 3" in capsys.readouterr().err


def test_quit(resolve, capsys):
    assert run(resolve, capsys, "quit")[0] == 0
    assert resolve.quit is True


def test_batch_runs_commands_in_order(resolve, capsys, tmp_path):
    script = tmp_path / "jobs.dava"
    script.write_text(
        "# prep\n"
        "timeline create 'Edit v3'\n"
        "\n"
        "timeline use 'Edit v3'\n"
        "marker add 10 -n start  # trailing comment\n"
    )
    code, out, err = run(resolve, capsys, "batch", str(script))
    project = resolve.pm.current
    assert code == 0
    assert project.current.name == "Edit v3"
    assert 10 in project.current.markers
    assert "$ dava timeline use 'Edit v3'" in err
    assert "Created timeline Edit v3" in out


def test_batch_validates_all_lines_before_running(resolve, capsys, tmp_path):
    script = tmp_path / "jobs.dava"
    script.write_text("timeline create New\ntimeline frobnicate\n")
    code, _, err = run(resolve, capsys, "batch", str(script))
    assert code == 1
    assert "jobs.dava:2: invalid command" in err
    assert [t.name for t in resolve.pm.current.timelines] == ["Edit v1", "Edit v2"]


def test_batch_stops_or_keeps_going(resolve, capsys, tmp_path):
    script = tmp_path / "jobs.dava"
    script.write_text("timeline use Missing\ntimeline create After\n")
    code, _, err = run(resolve, capsys, "batch", str(script))
    assert code == 1
    assert "jobs.dava:1: No timeline named 'Missing'" in err
    assert "After" not in [t.name for t in resolve.pm.current.timelines]

    code, _, err = run(resolve, capsys, "batch", str(script), "--keep-going")
    assert code == 1
    assert "1 of 2 batch command(s) failed" in err
    assert "After" in [t.name for t in resolve.pm.current.timelines]


def test_batch_rejects_nesting(resolve, capsys, tmp_path):
    script = tmp_path / "jobs.dava"
    script.write_text("launch --nogui\n")
    code, _, err = run(resolve, capsys, "batch", str(script))
    assert code == 1
    assert "cannot be used inside a batch" in err


def test_batch_json_output(resolve, capsys, tmp_path):
    script = tmp_path / "jobs.dava"
    script.write_text("timeline list\ntimeline create New\n")
    code, out, _ = run(resolve, capsys, "--json", "batch", str(script))
    lines = out.splitlines()
    assert code == 0
    assert len(lines) == 2
    assert json.loads(lines[0])[0]["name"] == "Edit v1"
    assert json.loads(lines[1]) == "Created timeline New"
