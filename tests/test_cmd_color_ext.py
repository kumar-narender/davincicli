"""Tests for dava/commands/color_ext.py: node, version, colorgroup, gallery and the new color/still actions."""

import inspect
import json
import os
import re

import pytest

from dava import cli, connect
from dava.commands import color_ext
from dava.registry import leaf_parsers
from recorder import Rec, rec_resolve

HAS_STUB = os.path.isfile(connect.stub_path() or "")

# Distinct stand-ins for the resolve.CACHE_* constants (the real values are Resolve's business).
CONSTANTS = {"CACHE_AUTO_ENABLED": 10, "CACHE_ENABLED": 11, "CACHE_DISABLED": 12}


def run(capsys, resolve, *argv):
    """Run dava with --json; return (exit code, parsed stdout or None, stderr)."""
    code = cli.main(["--json", *argv], resolve=resolve)
    out, err = capsys.readouterr()
    return code, (json.loads(out) if out.strip() else None), err


def usage_error(resolve, *argv):
    with pytest.raises(SystemExit) as exc:
        cli.main(list(argv), resolve=resolve)
    return exc.value.code


def make_graph(label, nodes=2):
    """Node 1 has a LUT, two tools and auto cache; the other nodes are empty with cache off."""
    return Rec(label, GetNumNodes=nodes, GetNodeLabel=lambda n: f"{label}-n{n}",
               GetLUT=lambda n: "Film/Rec709.cube" if n == 1 else "",
               GetToolsInNode=lambda n: ["Corrector", "LUT"] if n == 1 else [],
               GetNodeCacheMode=lambda n: CONSTANTS["CACHE_AUTO_ENABLED"] if n == 1 else CONSTANTS["CACHE_DISABLED"])


def make_item(label, name, uid, graphs):
    return Rec(label, GetName=name, GetUniqueId=uid,
               GetNodeGraph=lambda *layer: graphs.get(layer[0] if layer else 1),
               GetVersionNameList=lambda kind: {0: ["Version 1", "Warm"], 1: ["Remote A"]}[kind],
               GetCurrentVersion={"versionName": "Warm", "versionType": 0},
               GetColorGroup=None, GetTrackTypeAndIndex=["video", 1], GetStart=86400.0)


@pytest.fixture
def world():
    """Track 1 has two clips (two node layers each); color groups 'Day' and 'Night'; a gallery with
    still albums 'Stills 1' (current, three stills) and 'Looks', and PowerGrade album 'PowerGrade 1'."""
    graphs = {name: make_graph(name) for name in ("g1", "g1L2", "g2", "g2L2", "tl", "day-pre", "day-post",
                                                  "night-pre", "night-post")}
    item1 = make_item("item1", "shot_010", "item-1", {1: graphs["g1"], 2: graphs["g1L2"]})
    item2 = make_item("item2", "shot_020", "item-2", {1: graphs["g2"], 2: graphs["g2L2"]})
    resolve, parts = rec_resolve(items=[item1, item2])
    for name, value in CONSTANTS.items():
        setattr(resolve, name, value)
    timeline = parts["timeline"].set(GetNodeGraph=graphs["tl"])

    day = Rec("day", GetName="Day", GetPreClipNodeGraph=graphs["day-pre"], GetPostClipNodeGraph=graphs["day-post"],
              GetClipsInTimeline=lambda *timeline: [item2])
    night = Rec("night", GetName="Night", GetPreClipNodeGraph=graphs["night-pre"],
                GetPostClipNodeGraph=graphs["night-post"], GetClipsInTimeline=lambda *timeline: [])
    groups = [day, night]

    def add_group(name):
        group = Rec(f"new:{name}", GetName=name)
        groups.append(group)
        return group

    project = parts["project"].set(GetColorGroupsList=lambda: list(groups), AddColorGroup=add_group)

    stills = [Rec(f"s{n}") for n in (1, 2, 3)]
    labels = {"s1": "wide", "s2": "close", "s3": ""}
    stills_1 = Rec("album1", GetStills=lambda: list(stills), GetLabel=lambda still: labels[still.label])
    looks = Rec("album2", GetStills=lambda: [], GetLabel=lambda still: "")
    pg_still = Rec("pgs1")
    powergrade = Rec("pg1", GetStills=lambda: [pg_still], GetLabel=lambda still: "teal")
    still_albums, pg_albums = [stills_1, looks], [powergrade]
    names = {"album1": "Stills 1", "album2": "Looks", "pg1": "PowerGrade 1"}

    def create(albums, label, default):
        def make():
            album = Rec(label, GetStills=lambda: [])
            names[label] = default
            albums.append(album)
            return album
        return make

    gallery = parts["gallery"].set(
        GetGalleryStillAlbums=lambda: list(still_albums), GetGalleryPowerGradeAlbums=lambda: list(pg_albums),
        GetAlbumName=lambda album: names[album.label],
        SetAlbumName=lambda album, name: names.__setitem__(album.label, name) is None,
        GetCurrentStillAlbum=stills_1,
        CreateGalleryStillAlbum=create(still_albums, "new-still", "Stills 3"),
        CreateGalleryPowerGradeAlbum=create(pg_albums, "new-pg", "PowerGrade 2"))

    recs = [resolve, parts["pm"], project, timeline, gallery, item1, item2, day, night,
            stills_1, looks, powergrade, *stills, *graphs.values()]
    return {"resolve": resolve, "project": project, "timeline": timeline, "gallery": gallery,
            "item1": item1, "item2": item2, "graphs": graphs, "day": day, "night": night, "groups": groups,
            "stills_1": stills_1, "looks": looks, "powergrade": powergrade, "stills": stills, "pg_still": pg_still,
            "names": names, "still_albums": still_albums, "pg_albums": pg_albums, "recs": recs}


def calls_named(recs, *names):
    return [(rec.label, method, args) for rec in recs for method, args in rec.calls if method in names]


# ---------------------------------------------------------------------------
# Registration: every command is classified, read-only flags are honest, API names are real

EXPECTED_READ_ONLY = {
    ("node", "list"): True, ("node", "lut"): False, ("node", "enable"): False, ("node", "disable"): False,
    ("node", "cache"): "callable", ("node", "apply-drx"): False, ("node", "reset-grades"): False,
    ("node", "arri-cdl-lut"): False, ("node", "reset-colors"): False,
    ("color", "copy"): False, ("color", "refresh-luts"): False,
    ("version", "list"): True, ("version", "current"): True, ("version", "add"): False,
    ("version", "load"): False, ("version", "delete"): False, ("version", "rename"): False,
    ("colorgroup", "list"): True, ("colorgroup", "create"): False, ("colorgroup", "delete"): False,
    ("colorgroup", "rename"): False, ("colorgroup", "assign"): False, ("colorgroup", "unassign"): False,
    ("colorgroup", "show"): True, ("colorgroup", "clips"): True,
    ("gallery", "list"): True, ("gallery", "show"): True, ("gallery", "create"): False,
    ("gallery", "rename"): False, ("gallery", "use"): False,
    ("still", "label"): False, ("still", "delete"): False, ("still", "import"): False,
}


def color_ext_leaves():
    leaves = {}
    for words, parser, _ in leaf_parsers(cli.build_parser()):
        func = parser.get_default("func")
        if func is not None and func.__module__ == color_ext.__name__:
            leaves[words] = parser
    return leaves


def test_every_command_is_registered_with_the_expected_read_only_flag():
    leaves = color_ext_leaves()
    assert set(leaves) == set(EXPECTED_READ_ONLY)
    for words, expected in EXPECTED_READ_ONLY.items():
        read_only = leaves[words].get_default("read_only")
        if expected == "callable":
            assert callable(read_only), words
        else:
            assert read_only is expected, words


def test_node_cache_is_read_only_only_without_a_mode():
    read_only = color_ext_leaves()[("node", "cache")].get_default("read_only")
    parser = cli.build_parser()
    assert read_only(parser.parse_args(["node", "cache", "1"])) is True
    assert read_only(parser.parse_args(["node", "cache", "1", "off"])) is False


READ_ONLY_RUNS = [
    ("node", "list"), ("node", "list", "-L", "2"), ("node", "list", "-G", "timeline"),
    ("node", "list", "-G", "pre", "--group", "Day"), ("node", "list", "-G", "post", "--group", "Night"),
    ("node", "cache", "1"), ("node", "cache", "2", "-G", "timeline"),
    ("version", "list"), ("version", "current", "-i", "2"),
    ("colorgroup", "list"), ("colorgroup", "show"), ("colorgroup", "clips", "Day"),
    ("gallery", "list"), ("gallery", "show"), ("gallery", "show", "@2"),
    ("gallery", "show", "PowerGrade 1", "--powergrade"),
]


@pytest.mark.parametrize("argv", READ_ONLY_RUNS, ids=" ".join)
def test_read_only_commands_run_in_read_only_mode_and_only_call_getters(capsys, world, argv):
    code, out, err = run(capsys, world["resolve"], "--read-only", *argv)
    assert code == 0, err
    assert out is not None
    called = {method for rec in world["recs"] for method, _ in rec.calls}
    assert called and all(re.match(r"(Get|Is|Has)[A-Z]", method) for method in called), called


@pytest.mark.parametrize("argv", [
    ("node", "lut", "1", "x.cube"), ("node", "enable", "1"), ("node", "cache", "1", "on"),
    ("node", "reset-grades", "-G", "timeline"), ("color", "refresh-luts"),
    ("version", "add", "v2"), ("colorgroup", "create", "Dusk"), ("gallery", "use", "Looks"),
    ("still", "label", "1", "hero"),
], ids=" ".join)
def test_changing_commands_are_refused_in_read_only_mode(capsys, world, argv):
    code, out, err = run(capsys, world["resolve"], "--read-only", *argv)
    assert code == 1
    assert "read-only mode is on" in err
    assert all(not rec.calls for rec in world["recs"])


@pytest.mark.skipif(not HAS_STUB, reason="Resolve scripting API definition not installed")
def test_module_uses_only_methods_and_constants_of_the_api_definition():
    from dava.spec import load_spec

    spec = load_spec()
    used = set(re.findall(r"\.([A-Z][A-Za-z]+)\(", inspect.getsource(color_ext)))
    known = {name for cls in spec.classes.values() for name in cls.methods}
    assert {"SetNodeCacheMode", "GetPreClipNodeGraph", "RenameVersionByName", "ImportStills"} <= used
    assert used - known == set()
    assert set(color_ext.CACHE_MODES.values()) == {c for c, alias in spec.constants.items() if alias == "CacheMode"}


# ---------------------------------------------------------------------------
# node


def test_node_list_reports_every_node_of_every_clip_graph(capsys, world):
    code, out, _ = run(capsys, world["resolve"], "node", "list")
    assert code == 0
    assert out[0] == {"graph": "clip", "clip": 1, "name": "shot_010", "layer": 1, "node": 1, "label": "g1-n1",
                      "lut": "Film/Rec709.cube", "tools": "Corrector, LUT", "cache": "auto"}
    assert out[1]["cache"] == "off" and out[1]["tools"] == "" and out[1]["node"] == 2
    assert [(row["clip"], row["label"]) for row in out] == [(1, "g1-n1"), (1, "g1-n2"), (2, "g2-n1"), (2, "g2-n2")]
    assert world["item1"].called("GetNodeGraph") == [()]


def test_node_list_layer_asks_for_that_layer(capsys, world):
    code, out, _ = run(capsys, world["resolve"], "node", "list", "-L", "2", "-i", "2")
    assert code == 0
    assert world["item2"].called("GetNodeGraph") == [(2,)]
    assert world["item1"].called("GetNodeGraph") == []
    assert [row["label"] for row in out] == ["g2L2-n1", "g2L2-n2"]
    assert {row["layer"] for row in out} == {2}


def test_node_list_missing_layer_is_an_actionable_error(capsys, world):
    code, _, err = run(capsys, world["resolve"], "node", "list", "-L", "3", "-i", "1")
    assert code == 1
    assert "Clip 1 'shot_010' (layer 3): Resolve returned no node graph" in err
    assert "nodeStackLayers" in err


def test_node_list_timeline_graph(capsys, world):
    code, out, _ = run(capsys, world["resolve"], "node", "list", "--graph", "timeline")
    assert code == 0
    assert world["timeline"].called("GetNodeGraph") == [()]
    assert out[0]["graph"] == "timeline" and out[0]["name"] == "Edit v1" and out[0]["label"] == "tl-n1"
    assert "clip" not in out[0]


@pytest.mark.parametrize("which, graph", [("pre", "night-pre"), ("post", "night-post")])
def test_node_list_color_group_graphs(capsys, world, which, graph):
    code, out, _ = run(capsys, world["resolve"], "node", "list", "-G", which, "--group", "Night")
    assert code == 0
    assert [row["label"] for row in out] == [f"{graph}-n1", f"{graph}-n2"]
    assert out[0]["graph"] == which and out[0]["name"] == "Night"
    assert world["day"].called("GetPreClipNodeGraph") == [] and world["day"].called("GetPostClipNodeGraph") == []


@pytest.mark.parametrize("argv, message", [
    (("-G", "pre"), "--graph pre needs --group NAME"),
    (("--group", "Day"), "--group only applies with --graph pre or --graph post"),
    (("-G", "timeline", "-i", "1"), "-i/--index cannot be used with --graph timeline"),
    (("-G", "timeline", "-T", "2"), "-T/--track cannot be used with --graph timeline"),
    (("-G", "post", "--group", "Day", "-t", "Edit v1"), "-t/--timeline cannot be used with --graph post"),
    (("-G", "pre", "--group", "Day", "-L", "2", "-T", "3"), "-L/--layer and -T/--track cannot be used with --graph pre"),
])
def test_node_graph_option_errors_are_found_before_any_resolve_call(capsys, world, argv, message):
    code, _, err = run(capsys, world["resolve"], "node", "reset-grades", *argv)
    assert code == 1
    assert message in err
    assert all(not rec.calls for rec in world["recs"])


def test_node_graph_unknown_group(capsys, world):
    code, _, err = run(capsys, world["resolve"], "node", "list", "-G", "pre", "--group", "Dusk")
    assert code == 1
    assert "No color group named 'Dusk' (color groups: 'Day', 'Night')" in err


def test_node_graph_duplicate_group_names_are_refused(capsys, world):
    world["groups"].append(Rec("day2", GetName="Day"))
    code, _, err = run(capsys, world["resolve"], "node", "list", "-G", "pre", "--group", "Day")
    assert code == 1
    assert "2 color groups are named 'Day'" in err


def test_node_lut_refreshes_then_sets_the_lut_on_the_chosen_clip(capsys, world):
    project, graphs = world["project"], world["graphs"]
    code, out, _ = run(capsys, world["resolve"], "node", "lut", "2", "Film/Look.cube", "-i", "2")
    assert code == 0
    assert project.called("RefreshLUTList") == [()]
    assert graphs["g2"].called("SetLUT") == [(2, "Film/Look.cube")]
    assert graphs["g1"].called("SetLUT") == []
    assert out == [{"graph": "clip", "clip": 2, "name": "shot_020", "layer": 1, "node": 2, "lut": "Film/Look.cube"}]


def test_node_lut_on_a_color_group_post_graph(capsys, world):
    code, _, _ = run(capsys, world["resolve"], "node", "lut", "1", "a.cube", "-G", "post", "--group", "Day")
    assert code == 0
    assert world["graphs"]["day-post"].called("SetLUT") == [(1, "a.cube")]


def test_node_lut_checks_the_node_number_before_changing_anything(capsys, world):
    code, _, err = run(capsys, world["resolve"], "node", "lut", "3", "a.cube")
    assert code == 1
    assert "Node 3 is out of range: Clip 1 'shot_010' (layer 1) has 2 node(s)." in err
    assert calls_named(world["recs"], "SetLUT", "RefreshLUTList") == []


def test_node_lut_refused_reports_earlier_changes_and_the_lut_hint(capsys, world):
    world["graphs"]["g2"].set(SetLUT=False)
    code, _, err = run(capsys, world["resolve"], "node", "lut", "1", "a.cube")
    assert code == 1
    assert "Clip 2 'shot_020' (layer 1): Resolve could not set LUT 'a.cube' on node 1." in err
    assert "1 earlier change(s) were already applied." in err
    assert "LUT folders" in err
    assert world["graphs"]["g1"].called("SetLUT") == [(1, "a.cube")]


def test_node_lut_refresh_failure(capsys, world):
    world["project"].set(RefreshLUTList=False)
    code, _, err = run(capsys, world["resolve"], "node", "lut", "1", "a.cube")
    assert code == 1
    assert "could not refresh its LUT list" in err
    assert calls_named(world["recs"], "SetLUT") == []


@pytest.mark.parametrize("action, enabled", [("enable", True), ("disable", False)])
def test_node_enable_and_disable(capsys, world, action, enabled):
    code, out, _ = run(capsys, world["resolve"], "node", action, "2", "1", "2", "-G", "timeline")
    assert code == 0
    assert world["graphs"]["tl"].called("SetNodeEnabled") == [(2, enabled), (1, enabled)]
    assert [(row["node"], row["enabled"]) for row in out] == [(2, enabled), (1, enabled)]


def test_node_disable_refused(capsys, world):
    world["graphs"]["g1"].set(SetNodeEnabled=False)
    code, _, err = run(capsys, world["resolve"], "node", "disable", "1", "-i", "1")
    assert code == 1
    assert "Resolve could not disable node 1" in err


def test_node_numbers_must_be_positive(world):
    assert usage_error(world["resolve"], "node", "enable", "0") == 2
    assert usage_error(world["resolve"], "node", "list", "-L", "0") == 2


def test_node_cache_shows_the_mode_as_a_word(capsys, world):
    code, out, _ = run(capsys, world["resolve"], "node", "cache", "1", "-i", "1")
    assert code == 0
    assert out == [{"graph": "clip", "clip": 1, "name": "shot_010", "layer": 1, "node": 1, "cache": "auto"}]
    code, out, _ = run(capsys, world["resolve"], "node", "cache", "2", "-i", "2")
    assert code == 0
    assert out == [{"graph": "clip", "clip": 2, "name": "shot_020", "layer": 1, "node": 2, "cache": "off"}]
    assert world["graphs"]["g2"].called("GetNodeCacheMode") == [(2,)]
    assert calls_named(world["recs"], "SetNodeCacheMode") == []


@pytest.mark.parametrize("mode, constant", [("auto", "CACHE_AUTO_ENABLED"), ("on", "CACHE_ENABLED"),
                                            ("off", "CACHE_DISABLED")])
def test_node_cache_sets_the_resolve_constant(capsys, world, mode, constant):
    code, out, _ = run(capsys, world["resolve"], "node", "cache", "2", mode, "-G", "pre", "--group", "Day")
    assert code == 0
    assert world["graphs"]["day-pre"].called("SetNodeCacheMode") == [(2, CONSTANTS[constant])]
    assert out == [{"graph": "pre", "name": "Day", "node": 2, "cache": mode}]


def test_node_cache_refused(capsys, world):
    world["graphs"]["tl"].set(SetNodeCacheMode=False)
    code, _, err = run(capsys, world["resolve"], "node", "cache", "1", "on", "-G", "timeline")
    assert code == 1
    assert "The timeline graph of 'Edit v1': Resolve could not set the cache mode of node 1 to on." in err


def test_node_cache_unknown_mode_is_a_usage_error(world):
    assert usage_error(world["resolve"], "node", "cache", "1", "sometimes") == 2


def test_node_apply_drx_to_a_color_group_graph(capsys, world, tmp_path):
    drx = tmp_path / "look.drx"
    drx.write_text("x")
    code, out, _ = run(capsys, world["resolve"], "node", "apply-drx", str(drx), "-k", "source-tc",
                       "-G", "post", "--group", "Night")
    assert code == 0
    assert world["graphs"]["night-post"].called("ApplyGradeFromDRX") == [(str(drx), 1)]
    assert out[0]["drx"] == str(drx) and out[0]["keyframes"] == "source-tc"


def test_node_apply_drx_default_mode_and_missing_file(capsys, world, tmp_path):
    missing = tmp_path / "nope.drx"
    code, _, err = run(capsys, world["resolve"], "node", "apply-drx", str(missing))
    assert code == 1
    assert f"DRX file not found: {missing}" in err
    drx = tmp_path / "look.drx"
    drx.write_text("x")
    world["graphs"]["g2"].set(ApplyGradeFromDRX=False)
    code, _, err = run(capsys, world["resolve"], "node", "apply-drx", str(drx))
    assert code == 1
    assert world["graphs"]["g1"].called("ApplyGradeFromDRX") == [(str(drx), 0)]
    assert "Clip 2 'shot_020' (layer 1): Resolve could not apply the grade" in err


@pytest.mark.parametrize("action, method", [("reset-grades", "ResetAllGrades"), ("arri-cdl-lut", "ApplyArriCdlLut")])
def test_graph_wide_actions(capsys, world, action, method):
    code, out, _ = run(capsys, world["resolve"], "node", action, "-L", "2")
    assert code == 0
    assert world["graphs"]["g1L2"].called(method) == [()] and world["graphs"]["g2L2"].called(method) == [()]
    assert world["graphs"]["g1"].called(method) == []
    assert len(out) == 2
    world["graphs"]["tl"].set(**{method: False})
    code, _, err = run(capsys, world["resolve"], "node", action, "-G", "timeline")
    assert code == 1
    assert "The timeline graph of 'Edit v1': Resolve could not" in err


def test_node_reset_colors(capsys, world):
    code, out, _ = run(capsys, world["resolve"], "node", "reset-colors", "-i", "2")
    assert code == 0
    assert world["item2"].called("ResetAllNodeColors") == [()]
    assert world["item1"].called("ResetAllNodeColors") == []
    assert out == [{"clip": 2, "name": "shot_020", "reset": "node colors"}]
    world["item1"].set(ResetAllNodeColors=False)
    code, _, err = run(capsys, world["resolve"], "node", "reset-colors")
    assert code == 1
    assert "Clip 1 'shot_010': Resolve could not reset the node colors." in err


# ---------------------------------------------------------------------------
# color copy / refresh-luts


@pytest.fixture
def three_clips(world):
    item3 = make_item("item3", "shot_030", "item-3", {1: make_graph("g3")})
    other = make_item("other", "b_010", "item-b", {1: make_graph("gb")})
    tracks = {1: [world["item1"], world["item2"], item3], 2: [other]}
    world["timeline"].set(GetItemListInTrack=lambda kind, index: tracks.get(index, []) if kind == "video" else [])
    world["item3"], world["other"] = item3, other
    return world


def test_color_copy_to_listed_clips(capsys, three_clips):
    w = three_clips
    code, out, _ = run(capsys, w["resolve"], "color", "copy", "-i", "3", "--to", "1", "2", "1")
    assert code == 0
    assert w["item3"].called("CopyGrades") == [([w["item1"], w["item2"]],)]
    assert out == {"from": 3, "name": "shot_030", "track": 1, "to": [1, 2]}


def test_color_copy_to_all_other_clips(capsys, three_clips):
    w = three_clips
    code, out, _ = run(capsys, w["resolve"], "color", "copy", "-i", "2", "--to-all")
    assert code == 0
    assert w["item2"].called("CopyGrades") == [([w["item1"], w["item3"]],)]
    assert out["to"] == [1, 3]


def test_color_copy_to_another_track(capsys, three_clips):
    w = three_clips
    code, out, _ = run(capsys, w["resolve"], "color", "copy", "-i", "1", "--to-track", "2", "--to-all")
    assert code == 0
    assert w["item1"].called("CopyGrades") == [([w["other"]],)]
    assert out == {"from": 1, "name": "shot_010", "track": 2, "to": [1]}


@pytest.mark.parametrize("argv, message", [
    (("-i", "1", "--to", "1"), "Clip 1 is the source; leave it out of --to."),
    (("-i", "1", "--to", "4"), "Target clip 4 is out of range; track 1 has 3 clip(s)."),
    (("-i", "9", "--to", "1"), "Clip index 9 is out of range; track 1 has 3 clip(s)."),
    (("-i", "1", "--to-track", "3", "--to-all"), "Video track 3 of 'Edit v1' has no clips."),
])
def test_color_copy_errors(capsys, three_clips, argv, message):
    code, _, err = run(capsys, three_clips["resolve"], "color", "copy", *argv)
    assert code == 1
    assert message in err
    assert calls_named(three_clips["recs"] + [three_clips["item3"]], "CopyGrades") == []


def test_color_copy_to_all_with_a_single_clip_track(capsys, world):
    world["timeline"].set(GetItemListInTrack=lambda kind, index: [world["item1"]] if index == 1 else [])
    code, _, err = run(capsys, world["resolve"], "color", "copy", "-i", "1", "--to-all")
    assert code == 1
    assert "Track 1 has no other clip to copy the grade to." in err


def test_color_copy_refused(capsys, three_clips):
    three_clips["item1"].set(CopyGrades=False)
    code, _, err = run(capsys, three_clips["resolve"], "color", "copy", "-i", "1", "--to", "3")
    assert code == 1
    assert "Resolve could not copy the grade of clip 1 'shot_010' to clip(s) 3 of track 1." in err


def test_color_copy_needs_a_target_and_a_source(world):
    assert usage_error(world["resolve"], "color", "copy", "-i", "1") == 2
    assert usage_error(world["resolve"], "color", "copy", "--to", "2") == 2
    assert usage_error(world["resolve"], "color", "copy", "-i", "1", "--to", "2", "--to-all") == 2


def test_color_refresh_luts(capsys, world):
    code, out, _ = run(capsys, world["resolve"], "color", "refresh-luts")
    assert code == 0 and out == "Refreshed the LUT list"
    assert world["project"].called("RefreshLUTList") == [()]
    world["project"].set(RefreshLUTList=False)
    code, _, err = run(capsys, world["resolve"], "color", "refresh-luts")
    assert code == 1 and "could not refresh its LUT list" in err


# ---------------------------------------------------------------------------
# version


def test_version_list_marks_the_loaded_version(capsys, world):
    code, out, _ = run(capsys, world["resolve"], "version", "list", "-i", "1")
    assert code == 0
    assert world["item1"].called("GetVersionNameList") == [(0,), (1,)]
    assert [(row["type"], row["version"], row["current"]) for row in out] == [
        ("local", "Version 1", ""), ("local", "Warm", "*"), ("remote", "Remote A", "")]


def test_version_current(capsys, world):
    world["item2"].set(GetCurrentVersion={"versionName": "Remote A", "versionType": 1})
    code, out, _ = run(capsys, world["resolve"], "version", "current")
    assert code == 0
    assert out == [{"clip": 1, "name": "shot_010", "version": "Warm", "type": "local"},
                   {"clip": 2, "name": "shot_020", "version": "Remote A", "type": "remote"}]


def test_version_current_missing_is_an_error(capsys, world):
    world["item1"].set(GetCurrentVersion={})
    code, _, err = run(capsys, world["resolve"], "version", "current", "-i", "1")
    assert code == 1
    assert "Clip 1 'shot_010': Resolve reported no current grade version." in err
    world["item2"].set(GetCurrentVersion=None)
    code, _, err = run(capsys, world["resolve"], "version", "list", "-i", "2")
    assert code == 1
    assert "Clip 2 'shot_020': Resolve reported no current grade version." in err


@pytest.mark.parametrize("action, method", [("add", "AddVersion"), ("load", "LoadVersionByName"),
                                            ("delete", "DeleteVersionByName")])
@pytest.mark.parametrize("flag, code_", [((), 0), (("--remote",), 1)])
def test_version_actions_pass_name_and_type(capsys, world, action, method, flag, code_):
    code, out, _ = run(capsys, world["resolve"], "version", action, "Night look", "-i", "2", *flag)
    assert code == 0
    assert world["item2"].called(method) == [("Night look", code_)]
    assert world["item1"].called(method) == []
    assert out[0]["type"] == ("remote" if code_ else "local")


def test_version_rename_argument_order(capsys, world):
    code, out, _ = run(capsys, world["resolve"], "version", "rename", "Warm", "Warmer")
    assert code == 0
    assert world["item1"].called("RenameVersionByName") == [("Warm", "Warmer", 0)]
    assert world["item2"].called("RenameVersionByName") == [("Warm", "Warmer", 0)]
    assert out[0] == {"clip": 1, "name": "shot_010", "type": "local", "old": "Warm", "version": "Warmer"}


@pytest.mark.parametrize("argv, method, message", [
    (("add", "v2"), "AddVersion", "could not add local version 'v2'"),
    (("load", "v2", "-r"), "LoadVersionByName", "could not load remote version 'v2'"),
    (("delete", "v2"), "DeleteVersionByName", "could not delete local version 'v2'"),
    (("rename", "a", "b"), "RenameVersionByName", "could not rename local version 'a' to 'b'"),
])
def test_version_actions_refused(capsys, world, argv, method, message):
    world["item2"].set(**{method: False})
    code, _, err = run(capsys, world["resolve"], "version", *argv)
    assert code == 1
    assert f"Clip 2 'shot_020': Resolve {message}." in err
    assert "1 earlier change(s) were already applied." in err


# ---------------------------------------------------------------------------
# colorgroup


def test_colorgroup_list(capsys, world):
    code, out, _ = run(capsys, world["resolve"], "colorgroup", "list")
    assert code == 0
    assert out == [{"number": 1, "group": "Day"}, {"number": 2, "group": "Night"}]


def test_colorgroup_create_returns_a_ref(capsys, world):
    code, out, _ = run(capsys, world["resolve"], "colorgroup", "create", "Dusk")
    assert code == 0
    assert world["project"].called("AddColorGroup") == [("Dusk",)]
    assert out == {"$type": "ColorGroup", "name": "Dusk", "$ref": "colorgroup:Dusk"}


def test_colorgroup_create_refuses_a_taken_name_and_reports_failure(capsys, world):
    code, _, err = run(capsys, world["resolve"], "colorgroup", "create", "Day")
    assert code == 1 and "A color group named 'Day' already exists." in err
    assert world["project"].called("AddColorGroup") == []
    world["project"].set(AddColorGroup=None)
    code, _, err = run(capsys, world["resolve"], "colorgroup", "create", "Dusk")
    assert code == 1 and "Resolve could not create color group 'Dusk'." in err


def test_colorgroup_delete(capsys, world):
    code, out, _ = run(capsys, world["resolve"], "colorgroup", "delete", "Night")
    assert code == 0
    assert world["project"].called("DeleteColorGroup") == [(world["night"],)]
    assert "Deleted color group Night" in out
    world["project"].set(DeleteColorGroup=False)
    code, _, err = run(capsys, world["resolve"], "colorgroup", "delete", "Day")
    assert code == 1 and "Resolve could not delete color group 'Day'." in err


def test_colorgroup_delete_unknown(capsys, world):
    code, _, err = run(capsys, world["resolve"], "colorgroup", "delete", "Dusk")
    assert code == 1
    assert "No color group named 'Dusk'" in err and "dava colorgroup create" in err
    assert world["project"].called("DeleteColorGroup") == []


def test_colorgroup_rename(capsys, world):
    code, _, _ = run(capsys, world["resolve"], "colorgroup", "rename", "Day", "Morning")
    assert code == 0
    assert world["day"].called("SetName") == [("Morning",)]
    code, _, err = run(capsys, world["resolve"], "colorgroup", "rename", "Day", "Night")
    assert code == 1 and "A color group named 'Night' already exists." in err
    world["day"].set(SetName=False)
    code, _, err = run(capsys, world["resolve"], "colorgroup", "rename", "Day", "Noon")
    assert code == 1 and "Resolve could not rename color group 'Day' to 'Noon'." in err


def test_colorgroup_assign(capsys, world):
    code, out, _ = run(capsys, world["resolve"], "colorgroup", "assign", "Night")
    assert code == 0
    assert world["item1"].called("AssignToColorGroup") == [(world["night"],)]
    assert world["item2"].called("AssignToColorGroup") == [(world["night"],)]
    assert [row["group"] for row in out] == ["Night", "Night"]
    world["item1"].set(AssignToColorGroup=False)
    code, _, err = run(capsys, world["resolve"], "colorgroup", "assign", "Day", "-i", "1")
    assert code == 1 and "Clip 1 'shot_010': Resolve could not assign the clip to color group 'Day'." in err


def test_colorgroup_unassign_skips_ungrouped_clips(capsys, world):
    world["item2"].set(GetColorGroup=world["day"])
    code, out, _ = run(capsys, world["resolve"], "colorgroup", "unassign")
    assert code == 0
    assert world["item1"].called("RemoveFromColorGroup") == []
    assert world["item2"].called("RemoveFromColorGroup") == [()]
    assert out == [{"clip": 1, "name": "shot_010", "group": "", "removed": False},
                   {"clip": 2, "name": "shot_020", "group": "Day", "removed": True}]


def test_colorgroup_unassign_refused(capsys, world):
    world["item2"].set(GetColorGroup=world["day"], RemoveFromColorGroup=False)
    code, _, err = run(capsys, world["resolve"], "colorgroup", "unassign", "-i", "2")
    assert code == 1 and "Clip 2 'shot_020': Resolve could not remove the clip from color group 'Day'." in err


def test_colorgroup_show_and_clips(capsys, world):
    world["item2"].set(GetColorGroup=world["day"])
    code, out, _ = run(capsys, world["resolve"], "colorgroup", "show")
    assert code == 0
    assert [row["group"] for row in out] == ["", "Day"]
    code, out, _ = run(capsys, world["resolve"], "colorgroup", "clips", "Day")
    assert code == 0
    assert world["day"].called("GetClipsInTimeline") == [(world["timeline"],)]
    assert out == [{"name": "shot_020", "track_type": "video", "track": 1, "start": 86400.0, "$ref": "item#item-2"}]


def test_colorgroup_clips_unknown_timeline(capsys, world):
    code, _, err = run(capsys, world["resolve"], "colorgroup", "clips", "Day", "-t", "Other")
    assert code == 1 and "No timeline named 'Other'" in err


# ---------------------------------------------------------------------------
# gallery


def test_gallery_list(capsys, world):
    code, out, _ = run(capsys, world["resolve"], "gallery", "list")
    assert code == 0
    assert out == [
        {"kind": "still", "number": 1, "album": "Stills 1", "stills": 3, "current": "*"},
        {"kind": "still", "number": 2, "album": "Looks", "stills": 0, "current": ""},
        {"kind": "powergrade", "number": 1, "album": "PowerGrade 1", "stills": 1, "current": ""},
    ]
    code, out, _ = run(capsys, world["resolve"], "gallery", "list", "-k", "powergrade")
    assert [row["album"] for row in out] == ["PowerGrade 1"]


def test_gallery_show_defaults_to_the_current_album(capsys, world):
    code, out, _ = run(capsys, world["resolve"], "gallery", "show")
    assert code == 0
    assert out == [{"album": "Stills 1", "still": 1, "label": "wide"}, {"album": "Stills 1", "still": 2, "label": "close"},
                   {"album": "Stills 1", "still": 3, "label": ""}]


def test_powergrade_album_must_be_named_the_way_the_command_takes_it(capsys, world):
    # 'gallery show' takes the album as an argument; the still commands take -a.
    code, _, err = run(capsys, world["resolve"], "gallery", "show", "--powergrade")
    assert code == 1 and "Name the PowerGrade album with the ALBUM argument" in err
    code, _, err = run(capsys, world["resolve"], "still", "label", "1", "x", "--powergrade")
    assert code == 1 and "Name the PowerGrade album with -a NAME" in err
    assert calls_named(world["recs"], "SetLabel", "GetStills") == []


def test_no_current_album_is_named_the_way_the_command_takes_it(capsys, world):
    world["gallery"].set(GetCurrentStillAlbum=None)
    code, _, err = run(capsys, world["resolve"], "gallery", "show")
    assert code == 1 and "There is no current still album; name one with the ALBUM argument" in err
    code, _, err = run(capsys, world["resolve"], "still", "delete", "1")
    assert code == 1 and "There is no current still album; name one with -a NAME" in err
    assert calls_named(world["recs"], "DeleteStills") == []


def test_gallery_commands_without_a_gallery(capsys, world):
    world["project"].set(GetGallery=None)
    code, _, err = run(capsys, world["resolve"], "gallery", "list")
    assert code == 1 and "Resolve returned no gallery for the current project." in err


@pytest.mark.parametrize("album, message", [
    ("@3", "There are 2 still album(s); there is no still album @3."),
    ("Nope", "No still album named 'Nope' (still albums: 'Stills 1', 'Looks')."),
])
def test_gallery_album_lookup_errors(capsys, world, album, message):
    code, _, err = run(capsys, world["resolve"], "gallery", "show", album)
    assert code == 1 and message in err


def test_gallery_album_duplicate_names_ask_for_a_number(capsys, world):
    world["names"]["album2"] = "Stills 1"
    code, _, err = run(capsys, world["resolve"], "gallery", "use", "Stills 1")
    assert code == 1 and "2 still albums are named 'Stills 1'; pick one by number: @1, @2." in err
    code, _, _ = run(capsys, world["resolve"], "gallery", "use", "@2")
    assert code == 0
    assert world["gallery"].called("SetCurrentStillAlbum") == [(world["looks"],)]


def test_gallery_create_still_album_with_a_name(capsys, world):
    code, out, _ = run(capsys, world["resolve"], "gallery", "create", "Selects")
    assert code == 0
    new = world["still_albums"][-1]
    assert world["gallery"].called("CreateGalleryStillAlbum") == [()]
    assert world["gallery"].called("SetAlbumName") == [(new, "Selects")]
    assert out == {"kind": "still", "album": "Selects", "$ref": "album:Selects"}


def test_gallery_create_powergrade_album_keeps_the_default_name(capsys, world):
    code, out, _ = run(capsys, world["resolve"], "gallery", "create", "--powergrade")
    assert code == 0
    assert world["gallery"].called("CreateGalleryPowerGradeAlbum") == [()]
    assert world["gallery"].called("CreateGalleryStillAlbum") == []
    assert world["gallery"].called("SetAlbumName") == []
    assert out == {"kind": "powergrade", "album": "PowerGrade 2", "$ref": "powergrade:PowerGrade 2"}


def test_gallery_create_without_a_reported_name_gives_no_ref(capsys, world):
    names = world["names"]
    world["gallery"].set(GetAlbumName=lambda album: None if album.label == "new-still" else names[album.label])
    code, out, err = run(capsys, world["resolve"], "gallery", "create")
    assert code == 0, err
    assert world["gallery"].called("CreateGalleryStillAlbum") == [()]
    assert out == {"kind": "still", "album": None}


def test_gallery_create_errors(capsys, world):
    code, _, err = run(capsys, world["resolve"], "gallery", "create", "Looks")
    assert code == 1 and "A still album named 'Looks' already exists." in err
    assert world["gallery"].called("CreateGalleryStillAlbum") == []
    world["gallery"].set(CreateGalleryPowerGradeAlbum=None)
    code, _, err = run(capsys, world["resolve"], "gallery", "create", "--powergrade")
    assert code == 1 and "Resolve could not create a PowerGrade album." in err
    world["gallery"].set(SetAlbumName=False)
    code, _, err = run(capsys, world["resolve"], "gallery", "create", "Selects")
    assert code == 1 and "Created still album 'Stills 3', but Resolve refused to rename it to 'Selects'." in err


def test_gallery_rename(capsys, world):
    code, out, _ = run(capsys, world["resolve"], "gallery", "rename", "PowerGrade 1", "Teal", "--powergrade")
    assert code == 0
    assert world["gallery"].called("SetAlbumName") == [(world["powergrade"], "Teal")]
    assert out == "Renamed PowerGrade album PowerGrade 1 to Teal"
    code, _, err = run(capsys, world["resolve"], "gallery", "rename", "@1", "Looks")
    assert code == 1 and "A still album named 'Looks' already exists." in err
    world["gallery"].set(SetAlbumName=False)
    code, _, err = run(capsys, world["resolve"], "gallery", "rename", "Looks", "Grades")
    assert code == 1 and "Resolve could not rename still album 'Looks' to 'Grades'." in err


def test_gallery_use(capsys, world):
    code, out, _ = run(capsys, world["resolve"], "gallery", "use", "Looks")
    assert code == 0 and out == "Current album: Looks"
    assert world["gallery"].called("SetCurrentStillAlbum") == [(world["looks"],)]
    world["gallery"].set(SetCurrentStillAlbum=False)
    code, _, err = run(capsys, world["resolve"], "gallery", "use", "PowerGrade 1", "--powergrade")
    assert code == 1 and "Resolve could not make PowerGrade album 'PowerGrade 1' the current album." in err


# ---------------------------------------------------------------------------
# still label / delete / import


def test_still_label(capsys, world):
    code, out, _ = run(capsys, world["resolve"], "still", "label", "2", "hero")
    assert code == 0
    assert world["stills_1"].called("SetLabel") == [(world["stills"][1], "hero")]
    assert out == {"album": "Stills 1", "still": 2, "label": "hero"}
    code, _, err = run(capsys, world["resolve"], "still", "label", "1", "x", "-a", "Looks")
    assert code == 1 and "Still 1 is out of range: album 'Looks' has 0 still(s)." in err
    world["stills_1"].set(SetLabel=False)
    code, _, err = run(capsys, world["resolve"], "still", "label", "1", "x")
    assert code == 1 and "Resolve could not set the label of still 1 in album 'Stills 1'." in err


def test_still_delete_numbers_and_all(capsys, world):
    stills = world["stills"]
    code, out, _ = run(capsys, world["resolve"], "still", "delete", "3", "1", "3")
    assert code == 0
    assert world["stills_1"].called("DeleteStills") == [([stills[2], stills[0]],)]
    assert out == {"album": "Stills 1", "deleted": 2}
    code, out, _ = run(capsys, world["resolve"], "still", "delete", "--all", "-a", "PowerGrade 1", "--powergrade")
    assert code == 0 and out == {"album": "PowerGrade 1", "deleted": 1}
    assert world["powergrade"].called("DeleteStills") == [([world["pg_still"]],)]
    assert world["stills_1"].called("DeleteStills") == [([stills[2], stills[0]],)]


@pytest.mark.parametrize("argv, message", [
    ((), "Give the numbers of the stills to delete, or --all (not both)."),
    (("1", "--all"), "Give the numbers of the stills to delete, or --all (not both)."),
    (("4",), "Still 4 is out of range: album 'Stills 1' has 3 still(s)."),
    (("--all", "-a", "Looks"), "Album 'Looks' has no stills."),
])
def test_still_delete_errors(capsys, world, argv, message):
    code, _, err = run(capsys, world["resolve"], "still", "delete", *argv)
    assert code == 1 and message in err
    assert calls_named(world["recs"], "DeleteStills") == []


def test_still_delete_refused(capsys, world):
    world["stills_1"].set(DeleteStills=False)
    code, _, err = run(capsys, world["resolve"], "still", "delete", "1")
    assert code == 1 and "Resolve could not delete 1 still(s) from album 'Stills 1'." in err


def test_still_import(capsys, world, tmp_path):
    files = [tmp_path / "a.dpx", tmp_path / "b.drx"]
    for path in files:
        path.write_text("x")
    looks_stills = []
    world["looks"].set(GetStills=lambda: list(looks_stills),
                       ImportStills=lambda paths: looks_stills.extend(paths) is None)
    code, out, _ = run(capsys, world["resolve"], "still", "import", *map(str, files), "-a", "Looks")
    assert code == 0
    assert world["looks"].called("ImportStills") == [([str(files[0]), str(files[1])],)]
    assert out == {"album": "Looks", "files": 2, "added": 2, "stills": 2}


def test_relative_file_paths_reach_resolve_as_absolute_paths(capsys, world, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    folder = tmp_path.resolve()
    (folder / "look.drx").write_text("x")
    (folder / "a.dpx").write_text("x")
    code, _, err = run(capsys, world["resolve"], "node", "apply-drx", "look.drx", "-i", "1")
    assert code == 0, err
    assert world["graphs"]["g1"].called("ApplyGradeFromDRX") == [(str(folder / "look.drx"), 0)]
    code, _, err = run(capsys, world["resolve"], "still", "import", "a.dpx")
    assert code == 0, err
    assert world["stills_1"].called("ImportStills") == [([str(folder / "a.dpx")],)]


def test_still_import_errors(capsys, world, tmp_path):
    missing = tmp_path / "missing.dpx"
    code, _, err = run(capsys, world["resolve"], "still", "import", str(missing))
    assert code == 1 and f"File(s) not found: {missing}" in err
    assert calls_named(world["recs"], "ImportStills") == []
    present = tmp_path / "a.dpx"
    present.write_text("x")
    world["stills_1"].set(ImportStills=False)
    code, _, err = run(capsys, world["resolve"], "still", "import", str(present))
    assert code == 1 and "Resolve could not import 1 file(s) into album 'Stills 1'" in err
