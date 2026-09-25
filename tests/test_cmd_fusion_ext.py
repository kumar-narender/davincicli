"""Tests for dava insert and the fusion actions of dava/commands/fusion_ext.py (recording fakes)."""

import json
import os
import re

import pytest

from dava import cli, connect
from dava.commands import fusion_ext
from dava.registry import leaf_parsers
from recorder import Rec, rec_resolve

HAS_STUB = os.path.isfile(connect.stub_path() or "")


def run(capsys, resolve, *argv):
    """Run dava with --json; return (exit code, parsed stdout or None, stderr)."""
    code = cli.main(["--json", *argv], resolve=resolve)
    out, err = capsys.readouterr()
    return code, (json.loads(out) if out.strip() else None), err


def usage_error(resolve, *argv):
    with pytest.raises(SystemExit) as exc:
        cli.main(list(argv), resolve=resolve)
    return exc.value.code


def make_tool(name, regid, log=None, **returns):
    tool = Rec(name, GetAttrs={"TOOLS_Name": name, "TOOLS_RegID": regid}, **returns)
    if log is not None:
        tool.set(SetInput=lambda *a: log.append(("SetInput", a)),
                 AddModifier=lambda *a: log.append(("AddModifier", a)),
                 ConnectInput=lambda *a: log.append(("ConnectInput", a)))
    return tool


def make_comp(tools, label="comp", log=None, selected=()):
    """A composition whose GetToolList returns {1.0: tool, ...} like Fusion, filtered by selection and RegID."""
    def tool_list(only_selected, regid=None):
        pool = [t for t in tools if t in selected] if only_selected else tools
        chosen = [t for t in pool if regid is None or t.returns["GetAttrs"]["TOOLS_RegID"] == regid]
        return {float(i): t for i, t in enumerate(chosen, start=1)}

    comp = Rec(label, GetToolList=tool_list, GetAttrs={"COMPN_RenderStart": 0.0, "COMPN_RenderEnd": 119.0})
    if log is not None:
        comp.set(Lock=lambda: log.append("Lock"), Unlock=lambda: log.append("Unlock"))
    return comp


@pytest.fixture
def world():
    """Track 1: a plain clip (no compositions) and a title with two compositions."""
    log = []
    text = make_tool("Text1", "TextPlus", log)
    merge = make_tool("Merge1", "Merge", log)
    out = make_tool("MediaOut1", "MediaOut", log)
    tools = [text, merge, out]
    comp = make_comp(tools, log=log)
    glow = make_comp([make_tool("Glow1", "SoftGlow")], label="glow", log=log)
    comps = {1: comp, 2: glow}
    plain = Rec("plain", GetName="shot_010", GetUniqueId="item-1", GetFusionCompNameList=[],
                GetFusionCompCount=0, GetFusionCompByIndex=None, GetFusionCompByName=None)
    title = Rec("title", GetName="Text+", GetUniqueId="item-2", GetFusionCompNameList=["Composition 1", "Glow"],
                GetFusionCompCount=2, GetFusionCompByIndex=lambda i: comps.get(i),
                GetFusionCompByName=lambda n: {"Composition 1": comp, "Glow": glow}.get(n))
    resolve, parts = rec_resolve(items=[plain, title])
    return {"resolve": resolve, "parts": parts, "timeline": parts["timeline"], "project": parts["project"],
            "plain": plain, "title": title, "comp": comp, "glow": glow, "tools": tools, "text": text,
            "merge": merge, "log": log}


def all_calls(*recs):
    return [name for rec in recs for name, _ in rec.calls]


# ---------------------------------------------------------------------------
# insert


@pytest.mark.parametrize("kind, method", [
    ("standard", "InsertGeneratorIntoTimeline"),
    ("fusion", "InsertFusionGeneratorIntoTimeline"),
    ("ofx", "InsertOFXGeneratorIntoTimeline"),
])
def test_insert_generator_calls_the_method_of_its_kind(capsys, world, kind, method):
    timeline = world["timeline"]
    new = Rec("new", GetName="Solid Color", GetUniqueId="new-1", GetStart=86448.0, GetDuration=120.0,
              GetTrackTypeAndIndex=("video", 1))
    timeline.set(**{method: new})
    code, out, _ = run(capsys, world["resolve"], "insert", "generator", "Solid Color", "--kind", kind,
                       "--at", "01:00:02:00")
    assert code == 0
    assert out == {"$type": "TimelineItem", "$ref": "item#new-1", "name": "Solid Color", "kind": kind,
                   "generator": "Solid Color", "track": ["video", 1], "start": 86448.0, "duration": 120.0}
    assert new.called("GetTrackTypeAndIndex") == [()]
    assert world["project"].called("SetCurrentTimeline") == [(timeline,)]
    inserts = [(name, args) for name, args in timeline.calls if name.startswith("Insert")]
    assert inserts == [(method, ("Solid Color",))]
    names = [name for name, _ in timeline.calls]
    assert names.index("SetCurrentTimecode") < names.index(method)
    assert timeline.called("SetCurrentTimecode") == [("01:00:02:00",)]


def test_insert_generator_defaults_to_standard_without_moving_the_playhead(capsys, world):
    timeline = world["timeline"]
    timeline.set(InsertGeneratorIntoTimeline=Rec("new", GetName="Window", GetUniqueId="new-1"))
    code, out, _ = run(capsys, world["resolve"], "insert", "generator", "Window")
    assert code == 0 and out["kind"] == "standard"
    assert timeline.called("InsertGeneratorIntoTimeline") == [("Window",)]
    assert timeline.called("SetCurrentTimecode") == []


@pytest.mark.parametrize("kind, method", [
    ("standard", "InsertTitleIntoTimeline"),
    ("fusion", "InsertFusionTitleIntoTimeline"),
])
def test_insert_title_calls_the_method_of_its_kind(capsys, world, kind, method):
    timeline = world["timeline"]
    timeline.set(**{method: Rec("new", GetName="Scroll", GetUniqueId="new-2", GetStart=86400.0, GetDuration=150.0)})
    code, out, _ = run(capsys, world["resolve"], "insert", "title", "Scroll", "-k", kind)
    assert code == 0
    assert out["$ref"] == "item#new-2" and out["title"] == "Scroll" and out["kind"] == kind
    assert [(n, a) for n, a in timeline.calls if n.startswith("Insert")] == [(method, ("Scroll",))]


def test_insert_fusion_comp(capsys, world):
    timeline = world["timeline"]
    timeline.set(InsertFusionCompositionIntoTimeline=Rec("new", GetName="Fusion Composition", GetUniqueId="new-3",
                                                         GetStart=86400.0, GetDuration=120.0,
                                                         GetTrackTypeAndIndex=["video", 2]))
    code, out, _ = run(capsys, world["resolve"], "insert", "fusion-comp", "--at", "01:00:00:00")
    assert code == 0
    assert out["$ref"] == "item#new-3" and out["kind"] == "fusion composition" and out["track"] == ["video", 2]
    assert timeline.called("InsertFusionCompositionIntoTimeline") == [()]


def test_insert_on_a_named_timeline_makes_it_current(capsys, world):
    timeline = world["timeline"]
    timeline.set(InsertTitleIntoTimeline=Rec("new", GetName="Text", GetUniqueId="new-4"))
    code, _, _ = run(capsys, world["resolve"], "insert", "title", "Text", "-t", "Edit v1")
    assert code == 0
    assert world["project"].called("SetCurrentTimeline") == [(timeline,)]
    code, _, err = run(capsys, world["resolve"], "insert", "title", "Text", "-t", "Nope")
    assert code == 1 and "No timeline named 'Nope'" in err


def test_insert_failure_lists_built_in_names(capsys, world):
    world["timeline"].set(InsertGeneratorIntoTimeline=None, InsertTitleIntoTimeline=None)
    code, out, err = run(capsys, world["resolve"], "insert", "generator", "Solid Colour")
    assert code == 1 and out is None
    assert "could not insert standard generator 'Solid Colour'" in err and "Solid Color" in err
    code, _, err = run(capsys, world["resolve"], "insert", "title", "Lower Third")
    assert code == 1 and "Left Lower Third" in err


def test_insert_fusion_failure_suggests_similar_templates(capsys, world, monkeypatch):
    seen = []

    def fake_list(kind=None, search=None):
        seen.append((kind, search))
        return [{"name": "Lower Third Clean"}, {"name": "Lower Third Bold"}]

    monkeypatch.setattr(fusion_ext.templates, "list_templates", fake_list)
    world["timeline"].set(InsertFusionTitleIntoTimeline=None, InsertFusionGeneratorIntoTimeline=None)
    code, _, err = run(capsys, world["resolve"], "insert", "title", "Lower Thrid", "--kind", "fusion")
    assert code == 1 and "Similar titles: Lower Third Clean, Lower Third Bold." in err
    code, _, err = run(capsys, world["resolve"], "insert", "generator", "Lower", "--kind", "fusion")
    assert code == 1 and "Similar generators:" in err
    assert seen == [("titles", "Lower"), ("generators", "Lower")]


def test_insert_ofx_and_comp_failures(capsys, world):
    world["timeline"].set(InsertOFXGeneratorIntoTimeline=None, InsertFusionCompositionIntoTimeline=None)
    code, _, err = run(capsys, world["resolve"], "insert", "generator", "Glow", "--kind", "ofx")
    assert code == 1 and "OpenFX" in err
    code, _, err = run(capsys, world["resolve"], "insert", "fusion-comp")
    assert code == 1 and "could not insert a Fusion composition into 'Edit v1'" in err


def test_insert_stops_when_the_playhead_cannot_move(capsys, world):
    timeline = world["timeline"]
    timeline.set(SetCurrentTimecode=False)
    code, _, err = run(capsys, world["resolve"], "insert", "title", "Text", "--at", "99:00:00:00")
    assert code == 1 and "Could not move the playhead to '99:00:00:00'" in err
    assert not [n for n, _ in timeline.calls if n.startswith("Insert")]


def test_insert_stops_when_the_timeline_cannot_be_made_current(capsys, world):
    world["project"].set(SetCurrentTimeline=False)
    code, _, err = run(capsys, world["resolve"], "insert", "fusion-comp")
    assert code == 1 and "Could not make 'Edit v1' the current timeline" in err
    assert world["timeline"].called("InsertFusionCompositionIntoTimeline") == []


def test_insert_usage_errors(world):
    assert usage_error(world["resolve"], "insert", "generator", "X", "--kind", "bogus") == 2
    assert usage_error(world["resolve"], "insert", "title") == 2


def test_insert_is_refused_in_read_only_mode(capsys, world):
    code = cli.main(["--read-only", "insert", "title", "Text"], resolve=world["resolve"])
    assert code == 1 and "read-only" in capsys.readouterr().err
    assert world["timeline"].calls == []


# ---------------------------------------------------------------------------
# fusion comps / load / cache


def test_comps_lists_compositions_with_refs(capsys, world):
    code, out, _ = run(capsys, world["resolve"], "fusion", "comps", "-i", "2")
    assert code == 0
    assert out == {"item": "Text+", "ref": "item#item-2", "count": 2, "comps": [
        {"index": 1, "name": "Composition 1", "ref": "item#item-2::comp@1"},
        {"index": 2, "name": "Glow", "ref": "item#item-2::comp@2"}]}
    assert world["title"].called("GetFusionCompCount") == [()]


def test_comps_accepts_an_item_ref(capsys, world):
    code, out, _ = run(capsys, world["resolve"], "fusion", "comps", "--ref", "item#item-2")
    assert code == 0 and out["count"] == 2
    code, out, _ = run(capsys, world["resolve"], "fusion", "comps", "--ref", "item:video:1:1")
    assert code == 0 and out == {"item": "shot_010", "ref": "item#item-1", "count": 0, "comps": []}


def test_comps_refuses_refs_to_other_objects(capsys, world):
    code, _, err = run(capsys, world["resolve"], "fusion", "comps", "--ref", "clip:a.mov")
    assert code == 1 and "--ref must name a timeline item" in err and "MediaPoolItem" in err


def test_load_by_name(capsys, world):
    code, out, _ = run(capsys, world["resolve"], "fusion", "load", "Glow", "-i", "2")
    assert code == 0 and out == {"item": "Text+", "loaded": "Glow"}
    assert world["title"].called("LoadFusionCompByName") == [("Glow",)]


def test_load_failure_lists_the_compositions(capsys, world):
    world["title"].set(LoadFusionCompByName=None)
    code, _, err = run(capsys, world["resolve"], "fusion", "load", "Nope", "-i", "2")
    assert code == 1
    assert "Could not load composition 'Nope' on 'Text+'. Its compositions: Composition 1, Glow." in err


def test_cache_shows_the_mode(capsys, world):
    world["title"].set(GetIsFusionOutputCacheEnabled="Auto")
    code, out, _ = run(capsys, world["resolve"], "--read-only", "fusion", "cache", "-i", "2")
    assert code == 0 and out == {"item": "Text+", "fusion_output_cache": "Auto"}
    assert world["title"].called("SetFusionOutputCache") == []


def test_cache_sets_then_reads_back(capsys, world):
    title = world["title"]
    title.set(GetIsFusionOutputCacheEnabled="On")
    code, out, _ = run(capsys, world["resolve"], "fusion", "cache", "On", "-i", "2")
    assert code == 0 and out["fusion_output_cache"] == "On"
    names = [n for n, _ in title.calls]
    assert title.called("SetFusionOutputCache") == [("On",)]
    assert names.index("SetFusionOutputCache") < names.index("GetIsFusionOutputCacheEnabled")


def test_cache_rejected_value_is_an_error(capsys, world):
    world["title"].set(SetFusionOutputCache=False)
    code, out, err = run(capsys, world["resolve"], "fusion", "cache", "sometimes", "-i", "2")
    assert code == 1 and out is None and "rejected Fusion output cache value 'sometimes'" in err


def test_cache_set_is_refused_in_read_only_mode(capsys, world):
    code, _, err = run(capsys, world["resolve"], "--read-only", "fusion", "cache", "On", "-i", "2")
    assert code == 1 and "read-only" in err
    assert world["title"].called("SetFusionOutputCache") == []


# ---------------------------------------------------------------------------
# Picking a composition


def test_tools_lists_names_types_and_refs(capsys, world):
    code, out, _ = run(capsys, world["resolve"], "fusion", "tools", "-i", "2")
    assert code == 0
    assert out == {"comp": "item#item-2::comp@1", "render_range": [0.0, 119.0], "tools": [
        {"name": "Text1", "type": "TextPlus", "ref": "item#item-2::comp@1::tool:Text1"},
        {"name": "Merge1", "type": "Merge", "ref": "item#item-2::comp@1::tool:Merge1"},
        {"name": "MediaOut1", "type": "MediaOut", "ref": "item#item-2::comp@1::tool:MediaOut1"}]}
    assert world["comp"].called("GetToolList") == [(False,)]
    assert world["title"].called("GetFusionCompByIndex") == [(1,)]


def test_tools_filters_by_regid(capsys, world):
    code, out, _ = run(capsys, world["resolve"], "fusion", "tools", "-i", "2", "--type", "Merge")
    assert code == 0 and [t["name"] for t in out["tools"]] == ["Merge1"]
    assert world["comp"].called("GetToolList") == [(False, "Merge")]


def test_tools_selected_lists_only_the_selection(capsys, world):
    world["parts"]["fusion"].set(GetCurrentComp=make_comp(world["tools"], selected=[world["merge"]]))
    code, out, _ = run(capsys, world["resolve"], "fusion", "tools", "--ref", "fusion::comp", "--selected")
    assert code == 0 and out["tools"] == [{"name": "Merge1", "type": "Merge", "ref": "fusion::comp::tool:Merge1"}]
    code, out, _ = run(capsys, world["resolve"], "fusion", "tools", "-i", "2", "--selected", "--type", "TextPlus")
    assert code == 0 and out["tools"] == []
    assert world["comp"].called("GetToolList") == [(True, "TextPlus")]


def test_comp_by_name_uses_get_fusion_comp_by_name(capsys, world):
    code, out, _ = run(capsys, world["resolve"], "fusion", "tools", "-i", "2", "--comp", "Glow")
    assert code == 0 and out["comp"] == "item#item-2::comp@2"
    assert [t["name"] for t in out["tools"]] == ["Glow1"]
    assert world["title"].called("GetFusionCompByName") == [("Glow",)]


def test_comp_by_number(capsys, world):
    code, out, _ = run(capsys, world["resolve"], "fusion", "tools", "-i", "2", "-c", "2")
    assert code == 0 and out["comp"] == "item#item-2::comp@2"
    assert world["title"].called("GetFusionCompByIndex") == [(2,)]


@pytest.mark.parametrize("which", ["Missing", "3", "0"])
def test_unknown_comp_lists_the_compositions(capsys, world, which):
    code, _, err = run(capsys, world["resolve"], "fusion", "tools", "-i", "2", "-c", which)
    assert code == 1 and f"has no Fusion composition {which!r}. Its compositions: Composition 1, Glow." in err


def test_clip_without_composition(capsys, world):
    code, _, err = run(capsys, world["resolve"], "fusion", "tools", "-i", "1")
    assert code == 1 and "'shot_010' has no Fusion composition. Add one with 'dava fusion add'." in err


def test_clip_index_out_of_range(capsys, world):
    code, _, err = run(capsys, world["resolve"], "fusion", "tools", "-i", "9")
    assert code == 1 and "Clip index 9 is out of range" in err


def test_track_option_selects_the_track(capsys, world):
    code, _, err = run(capsys, world["resolve"], "fusion", "tools", "-T", "2", "-i", "1")
    assert code == 1 and "Video track 2" in err
    assert ("video", 2) in world["timeline"].called("GetItemListInTrack")


def test_comp_ref_forms(capsys, world):
    resolve = world["resolve"]
    code, out, _ = run(capsys, resolve, "fusion", "tools", "--ref", "item#item-2", "--comp", "Glow")
    assert code == 0 and out["comp"] == "item#item-2::comp@2"
    code, out, _ = run(capsys, resolve, "fusion", "tools", "--ref", "item:video:1:2::comp@2")
    assert code == 0 and out["comp"] == "item:video:1:2::comp@2" and out["tools"][0]["name"] == "Glow1"
    world["parts"]["fusion"].set(GetCurrentComp=world["comp"])
    code, out, _ = run(capsys, resolve, "fusion", "tools", "--ref", "fusion::comp")
    assert code == 0 and out["comp"] == "fusion::comp" and len(out["tools"]) == 3


def test_comp_ref_conflicts(capsys, world):
    resolve = world["resolve"]
    code, _, err = run(capsys, resolve, "fusion", "tools", "--ref", "item#item-2", "-T", "2")
    assert code == 1 and "do not combine them with --ref" in err
    code, _, err = run(capsys, resolve, "fusion", "tools", "--ref", "item#item-2::comp@1", "-c", "2")
    assert code == 1 and "this --ref already names one" in err
    code, _, err = run(capsys, resolve, "fusion", "tools", "--ref", "timeline")
    assert code == 1 and "must name a timeline item or a Fusion composition" in err and "Timeline" in err


def test_target_usage_errors(world):
    resolve = world["resolve"]
    assert usage_error(resolve, "fusion", "tools") == 2
    assert usage_error(resolve, "fusion", "tools", "-i", "1", "--ref", "item") == 2
    assert usage_error(resolve, "fusion", "comps") == 2


# ---------------------------------------------------------------------------
# inputs / get


def test_inputs_lists_and_searches(capsys, world):
    inputs = {1.0: Rec("i1", GetAttrs={"INPS_ID": "StyledText", "INPS_Name": "Styled Text", "INPS_DataType": "Text"}),
              2.0: Rec("i2", GetAttrs={"INPS_ID": "Center", "INPS_Name": "Center", "INPS_DataType": "Point"}),
              3.0: Rec("i3", GetAttrs={"INPS_ID": "Size", "INPS_Name": "Size", "INPS_DataType": "Number"})}
    world["text"].set(GetInputList=inputs)
    code, out, _ = run(capsys, world["resolve"], "fusion", "inputs", "Text1", "-i", "2")
    assert code == 0 and out == [{"id": "StyledText", "name": "Styled Text", "type": "Text"},
                                 {"id": "Center", "name": "Center", "type": "Point"},
                                 {"id": "Size", "name": "Size", "type": "Number"}]
    code, out, _ = run(capsys, world["resolve"], "fusion", "inputs", "Text1", "-i", "2", "-s", "styled")
    assert code == 0 and [row["id"] for row in out] == ["StyledText"]
    code, _, err = run(capsys, world["resolve"], "fusion", "inputs", "Text1", "-i", "2", "-s", "zzz")
    assert code == 1 and "No input of Text1 matches 'zzz'" in err


def test_get_converts_points_to_lists(capsys, world):
    world["text"].set(GetInput={1.0: 0.5, 2.0: 0.25, 3.0: 0.0})
    code, out, _ = run(capsys, world["resolve"], "fusion", "get", "Text1", "Center", "-i", "2")
    assert code == 0 and out == {"tool": "Text1", "input": "Center", "time": None, "value": [0.5, 0.25, 0.0]}
    assert world["text"].called("GetInput") == [("Center",)]


def test_get_at_a_time(capsys, world):
    world["text"].set(GetInput="Hello")
    code, out, _ = run(capsys, world["resolve"], "fusion", "get", "Text1", "StyledText", "-i", "2", "--time", "12")
    assert code == 0 and out["value"] == "Hello" and out["time"] == 12.0
    assert world["text"].called("GetInput") == [("StyledText", 12.0)]


def test_get_errors(capsys, world):
    code, _, err = run(capsys, world["resolve"], "fusion", "get", "Text9", "Size", "-i", "2")
    assert code == 1 and "no tool named 'Text9'. Its tools: Text1, Merge1, MediaOut1" in err
    world["text"].set(GetInput=None)
    code, _, err = run(capsys, world["resolve"], "fusion", "get", "Text1", "Nope", "-i", "2")
    assert code == 1 and "returned no value for input 'Nope'" in err


# ---------------------------------------------------------------------------
# set / animate


@pytest.mark.parametrize("text, sent, shown", [
    ("Hello world", "Hello world", "Hello world"),
    ("0.08", 0.08, 0.08),
    ("-45", -45, -45),
    ("[0.5, 0.8]", {1: 0.5, 2: 0.8}, [0.5, 0.8]),
    ("true", 1, 1),
    ('"42"', "42", "42"),
    ('{"1": 0.5, "2": 0.1}', {1: 0.5, 2: 0.1}, [0.5, 0.1]),
    # A superscript two passes str.isdigit but not int(): it must stay a text key, not crash.
    ('{"\\u00b2": 1}', {"\u00b2": 1}, {"\u00b2": 1}),
])
def test_set_converts_values(capsys, world, text, sent, shown):
    code, out, _ = run(capsys, world["resolve"], "fusion", "set", "Text1", "Center", text, "-i", "2")
    assert code == 0 and out == {"tool": "Text1", "input": "Center", "time": None, "value": shown}
    assert world["log"] == ["Lock", ("SetInput", ("Center", sent)), "Unlock"]


def test_set_string_flag_and_time(capsys, world):
    code, out, _ = run(capsys, world["resolve"], "fusion", "set", "Text1", "StyledText", "42", "--string",
                       "--time", "10", "-i", "2")
    assert code == 0 and out["value"] == "42"
    assert world["log"] == ["Lock", ("SetInput", ("StyledText", "42", 10.0)), "Unlock"]


def test_set_rejected_by_fusion_still_unlocks(capsys, world):
    world["text"].set(SetInput=lambda *a: (world["log"].append("SetInput"), False)[1])
    code, out, err = run(capsys, world["resolve"], "fusion", "set", "Text1", "Size", "big", "-i", "2")
    assert code == 1 and out is None and "Fusion rejected Size='big' on Text1" in err
    assert world["log"] == ["Lock", "SetInput", "Unlock"]


@pytest.mark.parametrize("text", ["null", "[]", '["a", 1]', "[true]", "1" * 5000])
def test_set_bad_values_change_nothing(capsys, world, text):
    # "1" * 5000 is valid JSON that json.loads refuses with a plain ValueError (Python's int digit limit).
    code, _, err = run(capsys, world["resolve"], "fusion", "set", "Text1", "Center", text, "-i", "2")
    assert code == 1 and "VALUE" in err
    assert world["log"] == []


@pytest.mark.parametrize("text", ["NaN", "Infinity", "-Infinity", "[0.5, NaN]", '{"1": Infinity}'])
def test_set_refuses_non_finite_numbers(capsys, world, text):
    # '--' lets a value start with '-' even when it is not a number (-Infinity).
    code, out, err = run(capsys, world["resolve"], "fusion", "set", "Text1", "Center", "-i", "2", "--", text)
    assert code == 1 and out is None and "is not a finite number" in err
    assert world["log"] == []


@pytest.mark.parametrize("argv, message", [
    (["fusion", "get", "Text1", "Size", "--time", "nan"], "argument --time: expected a finite number, got nan"),
    (["fusion", "get", "Text1", "Size", "--time", "x"], "argument --time: invalid float value: 'x'"),
    (["fusion", "set", "Text1", "Size", "1", "--time", "inf"], "argument --time: expected a finite number, got inf"),
    (["fusion", "set", "Text1", "Size", "1", "--time=-inf"], "argument --time: expected a finite number, got -inf"),
    (["fusion", "add-tool", "Blur", "--pos", "nan", "0"], "argument --pos: expected a finite number, got nan"),
    (["fusion", "add-tool", "Blur", "--pos", "0", "inf"], "argument --pos: expected a finite number, got inf"),
])
def test_time_and_position_must_be_finite_numbers(capsys, world, argv, message):
    # nan/inf would reach Fusion and print as NaN/Infinity, which is not JSON.
    assert usage_error(world["resolve"], *argv, "-i", "2") == 2
    assert message in capsys.readouterr().err
    assert world["log"] == [] and world["comp"].calls == [] and world["title"].calls == []


def test_time_and_position_stay_numbers_for_mcp():
    """The nan/inf check must not turn --time/--pos into strings in the generated MCP tool schemas."""
    from dava import mcp

    schemas = {tool["name"]: tool["inputSchema"]["properties"]
               for tool, _, _ in mcp.cli_tools(cli.build_parser(), False, cli.READ_ONLY_COMMANDS)}
    assert schemas["dava_fusion_get"]["time"]["type"] == "number"
    assert schemas["dava_fusion_set"]["time"]["type"] == "number"
    assert schemas["dava_fusion_add_tool"]["pos"]["type"] == "array"
    assert schemas["dava_fusion_add_tool"]["pos"]["items"] == {"type": "number"}


def test_set_on_a_tool_the_comp_lacks(capsys, world):
    code, _, err = run(capsys, world["resolve"], "fusion", "set", "Blur1", "XBlurSize", "2", "-i", "2")
    assert code == 1 and "no tool named 'Blur1'" in err and world["log"] == []


def test_animate_adds_a_spline_then_sorted_keys(capsys, world):
    code, out, _ = run(capsys, world["resolve"], "fusion", "animate", "Text1", "Size",
                       "24=0.1", "0=0.05", "12.5=[0.5,0.5]", "-i", "2")
    assert code == 0
    assert world["log"] == ["Lock", ("AddModifier", ("Size", "BezierSpline")),
                            ("SetInput", ("Size", 0.05, 0)), ("SetInput", ("Size", {1: 0.5, 2: 0.5}, 12.5)),
                            ("SetInput", ("Size", 0.1, 24)), "Unlock"]
    assert out == {"tool": "Text1", "input": "Size", "modifier": "BezierSpline",
                   "keys": [{"frame": 0, "value": 0.05}, {"frame": 12.5, "value": [0.5, 0.5]},
                            {"frame": 24, "value": 0.1}]}


def test_animate_modifier_choice_and_keys_only(capsys, world):
    code, _, _ = run(capsys, world["resolve"], "fusion", "animate", "Text1", "Center", "0=[0.5,0.1]",
                     "-m", "XYPath", "-i", "2")
    assert code == 0 and world["log"][1] == ("AddModifier", ("Center", "XYPath"))
    world["log"].clear()
    code, out, _ = run(capsys, world["resolve"], "fusion", "animate", "Text1", "Size", "5=1", "--keys-only", "-i", "2")
    assert code == 0 and out["modifier"] is None
    assert world["log"] == ["Lock", ("SetInput", ("Size", 1, 5)), "Unlock"]
    assert usage_error(world["resolve"], "fusion", "animate", "Text1", "Size", "0=1", "-m", "Perturb", "-i", "2") == 2


def test_animate_failed_modifier_sets_no_keys(capsys, world):
    world["text"].set(AddModifier=False)
    code, _, err = run(capsys, world["resolve"], "fusion", "animate", "Text1", "Nope", "0=1", "-i", "2")
    assert code == 1 and "could not animate Text1.Nope with a BezierSpline" in err
    assert world["log"] == ["Lock", "Unlock"]


def test_animate_rejected_keys_are_reported(capsys, world):
    world["text"].set(SetInput=lambda name, value, frame: frame != 10)
    code, _, err = run(capsys, world["resolve"], "fusion", "animate", "Text1", "Size", "0=1", "10=2", "-i", "2",
                       "--keys-only")
    assert code == 1 and "rejected the Size key(s) at frame(s) 10" in err


@pytest.mark.parametrize("pair, message", [
    ("5", "Expected FRAME=VALUE"),
    ("=5", "Expected FRAME=VALUE"),
    ("x=1", "frame must be a number"),
    ("inf=1", "frame must be a finite number"),
    ("1=null", "null is not a Fusion input value"),
    ("1=NaN", "frame 1: nan is not a finite number"),
])
def test_animate_bad_keys_change_nothing(capsys, world, pair, message):
    code, _, err = run(capsys, world["resolve"], "fusion", "animate", "Text1", "Size", pair, "-i", "2")
    assert code == 1 and message in err and world["log"] == []


def test_animate_duplicate_frames(capsys, world):
    code, _, err = run(capsys, world["resolve"], "fusion", "animate", "Text1", "Size", "1=1", "1.0=2", "-i", "2")
    assert code == 1 and "Frame 1 is given more than once" in err and world["log"] == []


# ---------------------------------------------------------------------------
# add-tool / connect / delete-tool / lua


def test_add_tool_default_and_explicit_position(capsys, world):
    comp = world["comp"]
    comp.set(AddTool=make_tool("Blur1", "Blur"))
    code, out, _ = run(capsys, world["resolve"], "fusion", "add-tool", "Blur", "-i", "2")
    assert code == 0 and out == {"name": "Blur1", "type": "Blur", "ref": "item#item-2::comp@1::tool:Blur1"}
    code, _, _ = run(capsys, world["resolve"], "fusion", "add-tool", "Blur", "--pos", "2", "-1.5", "-i", "2")
    assert code == 0
    assert comp.called("AddTool") == [("Blur", -32768, -32768), ("Blur", 2.0, -1.5)]
    assert world["log"] == ["Lock", "Unlock", "Lock", "Unlock"]


def test_add_tool_failure(capsys, world):
    world["comp"].set(AddTool=None)
    code, _, err = run(capsys, world["resolve"], "fusion", "add-tool", "Blurr", "-i", "2")
    assert code == 1 and "could not add a 'Blurr' tool" in err
    assert world["log"] == ["Lock", "Unlock"]
    assert usage_error(world["resolve"], "fusion", "add-tool", "Blur", "--pos", "1", "-i", "2") == 2


def test_connect_passes_the_source_tool(capsys, world):
    code, out, _ = run(capsys, world["resolve"], "fusion", "connect", "Merge1", "Foreground", "Text1", "-i", "2")
    assert code == 0 and out == {"tool": "Merge1", "input": "Foreground", "source": "Text1"}
    assert world["log"] == ["Lock", ("ConnectInput", ("Foreground", world["text"])), "Unlock"]
    # Both tools come from one listing of the composition (each listed tool costs a GetAttrs call).
    assert world["comp"].called("GetToolList") == [(False,)]
    assert world["text"].called("GetAttrs") == [()] and world["merge"].called("GetAttrs") == [()]


def test_connect_disconnect(capsys, world):
    code, out, _ = run(capsys, world["resolve"], "fusion", "connect", "Merge1", "Foreground", "--disconnect",
                       "-i", "2")
    assert code == 0 and out["source"] is None
    assert world["log"] == ["Lock", ("ConnectInput", ("Foreground", None)), "Unlock"]


def test_connect_errors(capsys, world):
    resolve = world["resolve"]
    code, _, err = run(capsys, resolve, "fusion", "connect", "Merge1", "Foreground", "Text7", "-i", "2")
    assert code == 1 and "no tool named 'Text7'" in err and world["log"] == []
    code, _, err = run(capsys, resolve, "fusion", "connect", "Merge9", "Foreground", "Text7", "-i", "2")
    assert code == 1 and "no tool named 'Merge9' or 'Text7'. Its tools: Text1, Merge1, MediaOut1" in err
    assert world["log"] == []
    world["merge"].set(ConnectInput=False)
    code, _, err = run(capsys, resolve, "fusion", "connect", "Merge1", "Nope", "Text1", "-i", "2")
    assert code == 1 and "could not connect Text1 to Merge1.Nope" in err
    assert world["log"] == ["Lock", "Unlock"]
    assert usage_error(resolve, "fusion", "connect", "Merge1", "Foreground", "-i", "2") == 2
    assert usage_error(resolve, "fusion", "connect", "Merge1", "Foreground", "Text1", "--disconnect", "-i", "2") == 2


def test_delete_tool_removes_and_verifies(capsys, world):
    tools = world["tools"]
    world["merge"].set(Delete=lambda: tools.remove(world["merge"]))
    code, out, _ = run(capsys, world["resolve"], "fusion", "delete-tool", "Merge1", "-i", "2")
    assert code == 0 and out == {"comp": "item#item-2::comp@1", "deleted": ["Merge1"], "tools": ["Text1", "MediaOut1"]}
    assert world["log"] == ["Lock", "Unlock"]


def test_delete_tool_given_twice_is_deleted_once(capsys, world):
    tools = world["tools"]
    world["merge"].set(Delete=lambda: tools.remove(world["merge"]))
    code, out, _ = run(capsys, world["resolve"], "fusion", "delete-tool", "Merge1", "Merge1", "-i", "2")
    assert code == 0 and out["deleted"] == ["Merge1"] and out["tools"] == ["Text1", "MediaOut1"]
    assert world["merge"].called("Delete") == [()]
    # One listing to find the tools, one to check they are gone.
    assert world["comp"].called("GetToolList") == [(False,), (False,)]


def test_delete_tool_that_stays_is_an_error(capsys, world):
    code, _, err = run(capsys, world["resolve"], "fusion", "delete-tool", "Merge1", "Text1", "-i", "2")
    assert code == 1 and "Fusion did not delete Merge1, Text1" in err
    assert world["merge"].called("Delete") == [()] and world["text"].called("Delete") == [()]


def test_delete_unknown_tool_deletes_nothing(capsys, world):
    code, _, err = run(capsys, world["resolve"], "fusion", "delete-tool", "Merge1", "Ghost1", "-i", "2")
    assert code == 1 and "no tool named 'Ghost1'" in err
    assert world["merge"].called("Delete") == []


def test_lua_runs_code_locked(capsys, world, tmp_path):
    comp = world["comp"]
    comp.set(Execute=lambda code: world["log"].append(("Execute", code)))
    code, out, _ = run(capsys, world["resolve"], "fusion", "lua", "print(1)", "-i", "2")
    assert code == 0 and out == {"comp": "item#item-2::comp@1", "result": None}
    script = tmp_path / "s.lua"
    script.write_text("comp:SetActiveTool(nil)\n", encoding="utf-8")
    code, _, _ = run(capsys, world["resolve"], "fusion", "lua", "-f", str(script), "-i", "2")
    assert code == 0
    assert world["log"] == ["Lock", ("Execute", "print(1)"), "Unlock",
                            "Lock", ("Execute", "comp:SetActiveTool(nil)\n"), "Unlock"]


def test_lua_errors(capsys, world, tmp_path):
    resolve = world["resolve"]
    code, _, err = run(capsys, resolve, "fusion", "lua", "-f", str(tmp_path / "missing.lua"), "-i", "2")
    assert code == 1 and "Cannot read Lua file" in err and world["comp"].calls == []
    latin1 = tmp_path / "latin1.lua"
    latin1.write_bytes(b"print('\xff')\n")
    code, _, err = run(capsys, resolve, "fusion", "lua", "-f", str(latin1), "-i", "2")
    assert code == 1 and "Cannot read Lua file" in err and "utf-8" in err and world["comp"].calls == []
    # An empty -f is a file that cannot be read, not "no file": nothing may run (Execute(None) did).
    code, _, err = run(capsys, resolve, "fusion", "lua", "-f", "", "-i", "2")
    assert code == 1 and "Cannot read Lua file ''" in err and world["comp"].calls == []
    world["comp"].set(Execute=False)
    code, _, err = run(capsys, resolve, "fusion", "lua", "x = 1", "-i", "2")
    assert code == 1 and "script failed" in err
    assert usage_error(resolve, "fusion", "lua", "-i", "2") == 2
    assert usage_error(resolve, "fusion", "lua", "x = 1", "-f", "a.lua", "-i", "2") == 2


def test_fusion_edits_are_refused_in_read_only_mode(capsys, world):
    for argv in (["fusion", "set", "Text1", "Size", "1"], ["fusion", "animate", "Text1", "Size", "0=1"],
                 ["fusion", "add-tool", "Blur"], ["fusion", "connect", "Merge1", "Input", "Text1"],
                 ["fusion", "delete-tool", "Text1"], ["fusion", "lua", "x=1"], ["fusion", "load", "Glow"]):
        code, _, err = run(capsys, world["resolve"], "--read-only", *argv, "-i", "2")
        assert code == 1 and "read-only" in err, argv
    assert world["log"] == [] and world["comp"].calls == [] and world["title"].calls == []


# ---------------------------------------------------------------------------
# Declarations


def fusion_ext_leaves():
    return {" ".join(words): leaf for words, leaf, _ in leaf_parsers(cli.build_parser())
            if getattr(leaf._defaults.get("func"), "__module__", "") == fusion_ext.__name__}


def test_every_command_declares_read_only():
    leaves = fusion_ext_leaves()
    expected_read_only = {"fusion comps", "fusion tools", "fusion inputs", "fusion get"}
    assert set(leaves) == expected_read_only | {
        "insert generator", "insert title", "insert fusion-comp", "fusion load", "fusion cache", "fusion set",
        "fusion animate", "fusion add-tool", "fusion connect", "fusion delete-tool", "fusion lua"}
    for name, leaf in leaves.items():
        declared = leaf._defaults["read_only"]
        if name == "fusion cache":
            assert callable(declared)
        else:
            assert declared is (name in expected_read_only), name


def test_read_only_commands_call_only_getters(capsys, world):
    """A command declared read_only may call nothing but Get*/Is*/Has* methods."""
    world["text"].set(GetInput=1.0, GetInputList={1: Rec("in", GetAttrs={"INPS_ID": "Size"})})
    world["title"].set(GetIsFusionOutputCacheEnabled="Auto")
    recs = [world["resolve"], world["parts"]["pm"], world["project"], world["timeline"], world["title"],
            world["plain"], world["comp"], world["glow"], *world["tools"]]
    for argv in (["fusion", "comps"], ["fusion", "tools"], ["fusion", "inputs", "Text1"],
                 ["fusion", "get", "Text1", "Size"], ["fusion", "cache"]):
        code, _, err = run(capsys, world["resolve"], "--read-only", *argv, "-i", "2")
        assert code == 0, (argv, err)
    called = set(all_calls(*recs))
    assert called and all(re.match(r"(Get|Is|Has)[A-Z]", name) for name in called), sorted(called)


OWNED_METHODS = [
    ("Timeline", "InsertGeneratorIntoTimeline"), ("Timeline", "InsertFusionGeneratorIntoTimeline"),
    ("Timeline", "InsertFusionCompositionIntoTimeline"), ("Timeline", "InsertOFXGeneratorIntoTimeline"),
    ("Timeline", "InsertTitleIntoTimeline"), ("Timeline", "InsertFusionTitleIntoTimeline"),
    ("TimelineItem", "GetFusionCompByName"), ("TimelineItem", "LoadFusionCompByName"),
    ("TimelineItem", "GetIsFusionOutputCacheEnabled"), ("TimelineItem", "SetFusionOutputCache"),
    ("TimelineItem", "GetFusionCompCount"),
]


def test_every_owned_method_is_called_by_a_command():
    with open(fusion_ext.__file__, encoding="utf-8") as handle:
        source = handle.read()
    missing = [name for _, name in OWNED_METHODS if f".{name}(" not in source]
    assert missing == []


@pytest.mark.skipif(not HAS_STUB, reason="Resolve scripting SDK not installed")
def test_owned_methods_exist_and_fusion_names_are_not_resolve_api():
    from dava.spec import load_spec

    spec = load_spec()
    for cls, name in OWNED_METHODS:
        assert name in spec.classes[cls].methods, f"{cls}.{name}"
    resolve_names = {name for c in spec.classes.values() for name in c.methods}
    # Declared Fusion names must really be outside the Resolve API, or the declaration hides a typo.
    assert sorted(fusion_ext.FUSION_API_METHODS & resolve_names) == []
