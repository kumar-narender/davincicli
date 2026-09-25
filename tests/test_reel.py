import json
import os

import pytest

from dava import fusiontext
from dava.cli import main
from dava.connect import ResolveError
from dava.helpers import apply_render_settings
from dava.luabridge import LuaBridgeClient, UNSAFE_METHODS
from recorder import Rec, rec_resolve

DEFAULT_COMP = """Composition {
	Tools = {
		MediaIn1 = Loader {
			Inputs = {
				GlobalOut = Input { Value = 119, },
			},
		},
		MediaOut1 = Saver {
			Inputs = {
				Index = Input { Value = "0", },
				Input = Input {
					SourceOp = "MediaIn1",
					Source = "Output",
				}
			},
		}
	},
}
"""


# --- fusiontext -------------------------------------------------------------------


def test_overlay_comp_routes_text_to_output():
    comp = fusiontext.overlay_comp(DEFAULT_COMP, 'HELLO\nWORLD "x"', "morph")
    out_block = comp[comp.index("MediaOut1 = Saver"):]
    assert 'SourceOp = "Blur1"' in out_block and 'SourceOp = "MediaIn1"' not in out_block
    assert 'Value = "HELLO\\nWORLD \\"x\\""' in comp
    assert "Follower1 = StyledTextFollower" in comp and "Text1 = TextPlus" in comp
    assert "MediaIn1 = Loader" in comp  # kept so Resolve accepts the comp


def test_overlay_comp_scale_and_center():
    comp = fusiontext.overlay_comp(DEFAULT_COMP, "HI", "caption", center=(0.3, 0.2), scale=2.0)
    assert "Center = Input { Value = { 0.3, 0.2 }, }" in comp
    assert "[12] = { 0.2," in comp  # caption size 0.1 doubled


def test_overlay_comp_rejects_unknown_shapes():
    with pytest.raises(ValueError):
        fusiontext.overlay_comp("Composition { }", "x")


# --- render settings and the Lua bridge guard ----------------------------------------


def test_apply_render_settings_reports_each_rejected_key():
    project = Rec("project", SetRenderSettings=lambda settings: "SelectAllFrames" not in settings)
    rejected = apply_render_settings(project, {"SelectAllFrames": False, "MarkIn": 1, "MarkOut": 2})
    assert rejected == ["SelectAllFrames"]
    assert [args[0] for args in project.called("SetRenderSettings")] == [
        {"SelectAllFrames": False}, {"MarkIn": 1}, {"MarkOut": 2}]


def test_lua_bridge_refuses_calls_that_hang_resolve(tmp_path):
    client = LuaBridgeClient.__new__(LuaBridgeClient)
    with pytest.raises(ResolveError, match="hangs Resolve"):
        client.request(op="call", target=3, method="GetInput", args=["StyledText"])
    assert "GetInput" in UNSAFE_METHODS


# --- overlay text -------------------------------------------------------------------


def overlay_resolve(tmp_path):
    imported = []

    def export(path, index):
        with open(path, "w") as handle:
            handle.write(DEFAULT_COMP)
        return True

    def import_comp(path):
        imported.append(open(path).read())
        return Rec("comp")

    carrier_item = Rec("carrier", GetDuration=120, ExportFusionComp=export, ImportFusionComp=import_comp,
                       GetUniqueId="item-carrier", GetName="hero.jpg")
    still = Rec("still", GetName="hero.jpg", GetUniqueId="clip-hero",
                GetClipProperty=lambda *a: {"Type": "Still"} if not a else "Still")
    resolve, parts = rec_resolve(clips=[still])
    tracks = {"n": 3}
    parts["timeline"].set(GetTrackCount=lambda kind: tracks["n"] if kind == "video" else 0,
                          AddTrack=lambda kind: tracks.__setitem__("n", tracks["n"] + 1) or True,
                          GetSettings={"timelineFrameRate": 24})
    parts["mediapool"].set(AppendToTimeline=lambda infos: [carrier_item])
    return resolve, parts, carrier_item, imported


def test_overlay_text_places_carrier_and_imports_comp(tmp_path, capsys, monkeypatch):
    from dava.commands import overlay
    monkeypatch.setattr(overlay, "TMP_DIR", str(tmp_path))
    resolve, parts, item, imported = overlay_resolve(tmp_path)
    code = main(["overlay", "text", "HELLO\\nWORLD", "--at", "00:00:01:00"], resolve=resolve)
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    info = parts["mediapool"].called("AppendToTimeline")[0][0][0]
    assert info["trackIndex"] == 4 and info["recordFrame"] == 86400 + 24 and info["mediaType"] == 1
    assert out["track"] == 4 and out["offset"] == 24
    assert item.called("SetProperties") == [({"DynamicZoomEnabled": False},)]
    assert len(imported) == 1 and 'Value = "HELLO\\nWORLD"' in imported[0]


def test_overlay_text_fails_when_resolve_refuses_the_comp(tmp_path, capsys, monkeypatch):
    from dava.commands import overlay
    monkeypatch.setattr(overlay, "TMP_DIR", str(tmp_path))
    resolve, parts, item, imported = overlay_resolve(tmp_path)
    item.set(ImportFusionComp=None)
    code = main(["overlay", "text", "HI"], resolve=resolve)
    assert code == 1
    assert "refused the generated overlay" in capsys.readouterr().err


# --- reel build ---------------------------------------------------------------------


def test_reel_build_stacks_shots_and_renders(tmp_path, capsys, monkeypatch):
    from dava.commands import overlay, reel
    monkeypatch.setattr(overlay, "TMP_DIR", str(tmp_path))
    resolve, parts, carrier, imported = overlay_resolve(tmp_path)
    resolve.set(SCALE_FILL=3, DYNAMIC_ZOOM_EASE_IN_AND_OUT=4)
    clips = {name: Rec(name, GetName=name, GetUniqueId="c-" + name) for name in ("hero.jpg", "a.jpg", "b.mov")}
    parts["root"].set(GetClipList=list(clips.values()))
    placed = []

    def append(infos):
        info = infos[0]
        name = info["mediaPoolItem"].GetName()
        item = Rec("item-" + name, GetDuration=58 if "startFrame" in info else 120, GetName=name)
        placed.append((name, info, item))
        return [item] if name != "hero.jpg" or len(placed) == 1 else [carrier]

    parts["mediapool"].set(AppendToTimeline=append)
    parts["project"].set(GetTimelineCount=1, SetCurrentTimeline=True, AddRenderJob="job-1", StartRendering=True,
                         SetCurrentRenderFormatAndCodec=True)
    parts["timeline"].set(GetName="Reel", SetSettings=True)
    spec = {
        "timeline": "Reel", "resolution": [1080, 1920], "project": "Promo", "clear": True,
        "intro": {"clip": "hero.jpg", "frames": 72, "title": "HELLO"},
        "shots": [{"clip": "a.jpg", "frames": 26, "transition": "Crash Zoom", "caption": "BELLS"},
                  {"clip": "b.mov", "in": 62, "frames": 60, "transition": "Glow", "grade": False, "zoom": False}],
        "outro": {"text": "NAMASTE", "frames": 64},
        "grade": {"Slope": "1.1 1 0.9", "Offset": "0 0 0", "Power": "1 1 1", "Saturation": 1.3},
        "render": {"dir": str(tmp_path), "name": "Reel"},
    }
    path = tmp_path / "reel.json"
    path.write_text(json.dumps(spec))
    code = main(["reel", "build", str(path)], resolve=resolve)
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    shots = [(name, info["trackIndex"], info["recordFrame"] - 86400) for name, info, _ in placed[:3]]
    assert shots == [("hero.jpg", 1, 0), ("a.jpg", 2, 72), ("b.mov", 3, 98)]
    video_info = placed[2][1]
    assert (video_info["startFrame"], video_info["endFrame"], video_info["mediaType"]) == (62, 121, 1)
    a_item = placed[1][2]
    assert a_item.called("AddTransition") == [({"type": "Crash Zoom", "category": "fusion", "position": "start",
                                                "alignment": "right", "duration": 10},)]
    assert a_item.called("SetCDL")[0][0]["Saturation"] == 1.3
    b_item = placed[2][2]
    assert b_item.called("SetCDL") == []  # already graded footage keeps its grade
    assert b_item.called("SetProperties") == [({"Scaling": 3},)]  # and no dynamic zoom
    assert len(imported) == 3  # title, caption, outro
    assert out["frames"] == 98 + 64  # render ends OUTRO frames after the last shot starts
    markout = [a[0] for a in parts["project"].called("SetRenderSettings") if "MarkOut" in a[0]]
    assert markout == [{"MarkOut": 86400 + 98 + 63}]
    assert out["render_job"] == "job-1"


def test_reel_spec_errors(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{")
    resolve, _ = rec_resolve()
    assert main(["reel", "build", str(bad)], resolve=resolve) == 1
    assert "Cannot read reel spec" in capsys.readouterr().err
    empty = tmp_path / "empty.json"
    empty.write_text("{}")
    assert main(["reel", "build", str(empty)], resolve=resolve) == 1
    assert "non-empty 'shots'" in capsys.readouterr().err


def test_reel_build_changes_nothing_when_a_clip_is_missing(tmp_path, capsys):
    resolve, parts = rec_resolve()
    parts["timeline"].set(GetName="Reel")
    spec = tmp_path / "s.json"
    spec.write_text(json.dumps({"timeline": "Reel", "clear": True, "shots": [{"clip": "missing.jpg"}]}))
    assert main(["reel", "build", str(spec)], resolve=resolve) == 1
    assert "Nothing was changed" in capsys.readouterr().err
    assert parts["timeline"].called("DeleteClips") == [] and parts["timeline"].called("SetSettings") == []


def test_reel_build_refuses_other_project_and_non_empty_timeline(tmp_path, capsys):
    resolve, parts = rec_resolve()
    parts["timeline"].set(GetName="Reel")
    spec = tmp_path / "s.json"
    spec.write_text(json.dumps({"project": "Other", "timeline": "Reel", "shots": [{"clip": "a.mov"}]}))
    assert main(["reel", "build", str(spec)], resolve=resolve) == 1
    assert "open 'Other' first" in capsys.readouterr().err
    spec.write_text(json.dumps({"timeline": "Reel", "shots": [{"clip": "a.mov"}]}))
    assert main(["reel", "build", str(spec)], resolve=resolve) == 1
    assert "already has 2 clip(s)" in capsys.readouterr().err
    assert parts["timeline"].called("DeleteClips") == []
