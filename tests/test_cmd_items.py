"""Tests for dava item (inspect and edit timeline items) and dava take (take selectors)."""

import json
import os

import pytest

from dava import connect
from dava.cli import build_parser, main
from dava.commands import items as items_module
from dava.registry import leaf_parsers
from recorder import Rec, rec_resolve

HAS_STUB = os.path.isfile(connect.stub_path() or "")
needs_stub = pytest.mark.skipif(not HAS_STUB, reason="Resolve scripting API definition not installed")


def make_item(uid, name, track=("video", 1), **returns):
    """A recorded timeline item with plausible getter results; keyword arguments override them."""
    base = dict(
        GetName=name, GetUniqueId=uid, GetType="video", GetTrackTypeAndIndex=list(track),
        GetStart=lambda *a: 86400.5 if a == (True,) else 86400, GetEnd=86448, GetDuration=48,
        GetLeftOffset=10, GetRightOffset=5, GetSourceStartFrame=10, GetSourceEndFrame=57,
        GetSourceStartTime=0.4, GetSourceEndTime=2.4, GetClipEnabled=True, GetClipColor="",
        GetFlagList=[], GetTakesCount=0, GetSelectedTakeIndex=0, GetMediaPoolItem=None,
    )
    base.update(returns)
    return Rec(uid, **base)


def world(items=None, audio=None):
    """rec_resolve with two video items on V1 (and optional items on audio track 1)."""
    items = items if items is not None else [make_item("item-1", "shot_010"), make_item("item-2", "shot_020")]
    resolve, parts = rec_resolve(items=items)
    audio = audio or []
    tracks = {("video", 1): items, ("audio", 1): audio}
    parts["timeline"].set(GetItemListInTrack=lambda kind, index: tracks.get((kind, index), []),
                          GetTrackCount=lambda kind: {"video": 1, "audio": 1 if audio else 0}.get(kind, 0))
    parts["audio"] = audio
    return resolve, parts


def run(resolve, capsys, *argv):
    code = main(["--json", *argv], resolve=resolve)
    captured = capsys.readouterr()
    out = json.loads(captured.out) if captured.out.strip() else None
    return code, out, captured.err


def usage_error(resolve, *argv):
    with pytest.raises(SystemExit) as exc:
        main(list(argv), resolve=resolve)
    return exc.value.code


def names(rec):
    return [name for name, _ in rec.calls]


# --- selection ----------------------------------------------------------------------------


def test_info_reports_positions_source_range_offsets_and_clip(capsys):
    clip = Rec("clip", GetName="a.mov", GetUniqueId="clip-1")
    resolve, parts = world([make_item("item-1", "shot_010", GetMediaPoolItem=clip, GetFlagList=["Blue"])])
    code, out, _ = run(resolve, capsys, "item", "info", "-i", "1")
    assert code == 0
    assert out["$ref"] == "item#item-1" and out["$type"] == "TimelineItem" and out["id"] == "item-1"
    assert out["track_type"] == "video" and out["track"] == 1
    assert (out["start"], out["end"], out["duration"]) == (86400, 86448, 48)
    assert (out["left_offset"], out["right_offset"]) == (10, 5)
    assert (out["source_start_frame"], out["source_end_frame"]) == (10, 57)
    assert (out["source_start_time"], out["source_end_time"]) == (0.4, 2.4)
    assert out["media_pool_item"] == {"$type": "MediaPoolItem", "$ref": "clip#clip-1", "name": "a.mov"}
    assert out["flags"] == ["Blue"] and out["takes"] == 0 and out["enabled"] is True
    assert parts["items"][0].called("GetStart") == [()]


def test_info_subframe_asks_for_fractional_frames(capsys):
    resolve, parts = world()
    code, out, _ = run(resolve, capsys, "item", "info", "-i", "2", "--subframe")
    assert code == 0 and out["$ref"] == "item#item-2" and out["start"] == 86400.5
    item = parts["items"][1]
    for method in ("GetStart", "GetEnd", "GetDuration", "GetLeftOffset", "GetRightOffset"):
        assert item.called(method) == [(True,)], method


def test_single_item_commands_need_an_index_or_ref(capsys):
    resolve, _ = world()
    code, _, err = run(resolve, capsys, "item", "info")
    assert code == 1 and "Select one item with -i N" in err and "--ref REF" in err


def test_index_out_of_range_names_the_track_size(capsys):
    resolve, _ = world()
    code, _, err = run(resolve, capsys, "item", "info", "-i", "5")
    assert code == 1 and "Item index 5 is out of range; video track 1 has 2 item(s)" in err


def test_empty_track_is_an_error(capsys):
    resolve, _ = world()
    code, _, err = run(resolve, capsys, "item", "info", "-k", "audio", "-i", "1")
    assert code == 1 and "Audio track 1 of 'Edit v1' has no items" in err


def test_track_type_selects_audio_items(capsys):
    audio = [make_item("aud-1", "shot_010.wav", track=("audio", 1), GetType="audio")]
    resolve, parts = world(audio=audio)
    code, out, _ = run(resolve, capsys, "item", "info", "-k", "audio", "-T", "1", "-i", "1")
    assert code == 0 and out["$ref"] == "item#aud-1" and out["track_type"] == "audio"
    assert ("audio", 1) in parts["timeline"].called("GetItemListInTrack")


def test_ref_selects_an_item_by_unique_id(capsys):
    resolve, parts = world()
    code, out, _ = run(resolve, capsys, "item", "info", "--ref", "item#item-2")
    assert code == 0 and out["$ref"] == "item#item-2" and out["name"] == "shot_020"
    assert parts["items"][0].called("GetStart") == []


@pytest.mark.parametrize("extra, flag", [(["-i", "1"], "-i"), (["-T", "2"], "-T"), (["-k", "audio"], "-k"),
                                         (["-t", "Edit v1"], "-t")])
def test_ref_and_track_options_do_not_mix(capsys, extra, flag):
    resolve, parts = world()
    code, _, err = run(resolve, capsys, "item", "rename", "X", "--ref", "item#item-1", *extra)
    assert code == 1 and "either with --ref or with -t/-T/-k/-i" in err and flag in err
    assert all(not item.called("SetName") for item in parts["items"])


def test_ref_to_another_class_is_rejected(capsys):
    resolve, _ = world()
    code, _, err = run(resolve, capsys, "item", "info", "--ref", "timeline")
    assert code == 1 and "names a Timeline, not a timeline item" in err


def test_unknown_item_id_is_an_error(capsys):
    resolve, _ = world()
    code, _, err = run(resolve, capsys, "item", "info", "--ref", "item#nope")
    assert code == 1 and "No timeline item with id 'nope'" in err


# --- list -----------------------------------------------------------------------------------


def test_list_walks_every_track_of_every_type(capsys):
    audio = [make_item("aud-1", "a.wav", track=("audio", 1), GetType="audio", GetClipEnabled=False)]
    resolve, parts = world(audio=audio)
    code, out, _ = run(resolve, capsys, "item", "list")
    assert code == 0
    assert [(r["ref"], r["track_type"], r["track"], r["index"]) for r in out] == [
        ("item#item-1", "video", 1, 1), ("item#item-2", "video", 1, 2), ("item#aud-1", "audio", 1, 1)]
    assert out[2]["enabled"] is False and out[0]["name"] == "shot_010" and out[0]["duration"] == 48
    assert parts["timeline"].called("GetTrackCount") == [("video",), ("audio",), ("subtitle",)]


def test_list_filters_by_type_and_track(capsys):
    resolve, parts = world()
    code, out, _ = run(resolve, capsys, "item", "list", "-k", "video", "-T", "1")
    assert code == 0 and len(out) == 2
    assert parts["timeline"].called("GetItemListInTrack") == [("video", 1)]


def test_list_of_missing_track_is_an_error(capsys):
    resolve, _ = world()
    code, _, err = run(resolve, capsys, "item", "list", "-T", "3")
    assert code == 1 and "has no track 3" in err and "1 video" in err


def test_list_rejects_track_zero():
    resolve, _ = world()
    assert usage_error(resolve, "item", "list", "-T", "0") == 2


# --- properties --------------------------------------------------------------------------------


def test_get_returns_all_or_selected_properties(capsys):
    props = {"ZoomX": 1.0, "Opacity": 100.0, "CompositeMode": 0}
    resolve, _ = world([make_item("item-1", "shot_010", GetProperties=props)])
    code, out, _ = run(resolve, capsys, "item", "get", "-i", "1")
    assert code == 0 and out == props
    code, out, _ = run(resolve, capsys, "item", "get", "ZoomX", "Opacity", "-i", "1")
    assert code == 0 and out == {"ZoomX": 1.0, "Opacity": 100.0}


def test_get_unknown_key_lists_the_available_ones(capsys):
    resolve, _ = world([make_item("item-1", "shot_010", GetProperties={"ZoomX": 1.0})])
    code, _, err = run(resolve, capsys, "item", "get", "Zoom", "-i", "1")
    assert code == 1 and "no propert(ies) Zoom" in err and "Available: ZoomX" in err


def test_get_key_from_an_empty_property_dict(capsys):
    resolve, _ = world([make_item("item-1", "shot_010", GetProperties={})])
    code, _, err = run(resolve, capsys, "item", "get", "ZoomX", "-i", "1")
    assert code == 1 and "Available: none (Resolve reported no properties)." in err


def test_get_fails_when_resolve_returns_nothing(capsys):
    resolve, _ = world([make_item("item-1", "shot_010", GetProperties=None)])
    code, _, err = run(resolve, capsys, "item", "get", "-i", "1")
    assert code == 1 and "returned no properties" in err


@needs_stub
def test_set_converts_values_to_the_declared_types(capsys):
    resolve, parts = world()
    resolve.COMPOSITE_SCREEN = 5.0
    code, out, _ = run(resolve, capsys, "item", "set", "ZoomX=1.5", "FlipX=true",
                       "CompositeMode=COMPOSITE_SCREEN", "AudioVoiceIsolationAmount=40", "-i", "2")
    assert code == 0
    expected = {"ZoomX": 1.5, "FlipX": True, "CompositeMode": 5.0, "AudioVoiceIsolationAmount": 40}
    assert parts["items"][1].called("SetProperties") == [(expected,)]
    assert type(parts["items"][1].called("SetProperties")[0][0]["AudioVoiceIsolationAmount"]) is int
    assert parts["items"][0].called("SetProperties") == []
    assert out == {"properties": expected, "items": [{"ref": "item#item-2", "name": "shot_020"}]}


@needs_stub
def test_set_unknown_key_fails_before_touching_resolve(capsys):
    resolve, parts = world()
    code, _, err = run(resolve, capsys, "item", "set", "Zoom=2", "-i", "1")
    assert code == 1 and "Unknown TimelineItemProperties key(s): Zoom" in err and "ZoomX" in err
    assert parts["timeline"].calls == []


@needs_stub
def test_set_without_index_changes_every_item_and_stops_at_a_rejection(capsys):
    items = [make_item("item-1", "shot_010"), make_item("item-2", "shot_020", SetProperties=False),
             make_item("item-3", "shot_030")]
    resolve, _ = world(items)
    code, _, err = run(resolve, capsys, "item", "set", "Opacity=50")
    assert code == 1
    assert "Resolve rejected properties {'Opacity': 50.0} for 'shot_020' (item#item-2)" in err
    assert "Items changed before this one: item#item-1." in err
    # Whether SetProperties is all-or-nothing is not documented, so the error must not claim it.
    assert "may still have applied some of them" in err and "none of them were applied" not in err
    assert items[0].called("SetProperties") == [({"Opacity": 50.0},)]
    assert items[2].called("SetProperties") == []


@needs_stub
def test_set_several_refs(capsys):
    resolve, parts = world()
    code, out, _ = run(resolve, capsys, "item", "set", "Pan=10", "--ref", "item#item-2", "--ref", "item#item-1")
    assert code == 0 and [r["ref"] for r in out["items"]] == ["item#item-2", "item#item-1"]
    assert parts["items"][0].called("SetProperties") == [({"Pan": 10.0},)]


def test_set_is_refused_in_read_only_mode(capsys):
    resolve, parts = world()
    code, _, err = run(resolve, capsys, "--read-only", "item", "set", "Pan=10", "-i", "1")
    assert code == 1 and "read-only mode is on" in err
    assert parts["items"][0].calls == []


# --- fades and speed ----------------------------------------------------------------------------


def test_fades_without_options_only_reads(capsys):
    resolve, parts = world([make_item("item-1", "shot_010", GetFades={"FadeIn": 0, "FadeOut": 12})])
    code, out, _ = run(resolve, capsys, "--read-only", "item", "fades", "-i", "1")
    assert code == 0 and out == [{"ref": "item#item-1", "name": "shot_010", "fade_in": 0, "fade_out": 12}]
    assert "SetFades" not in names(parts["items"][0])


def test_fades_set_passes_only_the_given_edges(capsys):
    resolve, parts = world([make_item("item-1", "shot_010", GetFades={"FadeIn": 12, "FadeOut": 0})])
    code, out, _ = run(resolve, capsys, "item", "fades", "--in", "12", "-i", "1")
    assert code == 0 and out[0]["fade_in"] == 12
    assert parts["items"][0].called("SetFades") == [({"FadeIn": 12},)]
    code, _, err = run(resolve, capsys, "--read-only", "item", "fades", "--out", "3", "-i", "1")
    assert code == 1 and "read-only" in err


def test_fades_reject_negative_lengths_and_resolve_failures(capsys):
    resolve, parts = world([make_item("item-1", "shot_010", SetFades=False)])
    code, _, err = run(resolve, capsys, "item", "fades", "--in", "-2", "-i", "1")
    assert code == 1 and "must be 0 or more: FadeIn" in err and parts["items"][0].calls == []
    code, _, err = run(resolve, capsys, "item", "fades", "--in", "5", "--out", "6", "-i", "1")
    assert code == 1 and "Resolve rejected fades {'FadeIn': 5, 'FadeOut': 6}" in err


def test_speed_reads_without_percent(capsys):
    speed = {"Percentage": 100.0, "PitchCorrection": False}
    resolve, parts = world([make_item("item-1", "shot_010", GetSpeed=speed)])
    code, out, _ = run(resolve, capsys, "--read-only", "item", "speed", "-i", "1")
    assert code == 0 and out["Percentage"] == 100.0 and out["$ref"] == "item#item-1"
    assert "SetSpeed" not in names(parts["items"][0])


def test_speed_set_builds_speed_options(capsys):
    resolve, parts = world([make_item("item-1", "shot_010", GetSpeed={"Percentage": 50.0})])
    code, out, _ = run(resolve, capsys, "item", "speed", "50", "--ripple", "--stretch-keyframes",
                       "--pitch-correction", "off", "-i", "1")
    assert code == 0 and out["Percentage"] == 50.0
    assert parts["items"][0].called("SetSpeed") == [({"Percentage": 50.0, "PitchCorrection": False,
                                                       "StretchKeyframesToFit": True, "RippleTimeline": True},)]


def test_speed_rejected_by_resolve_explains_stills(capsys):
    resolve, _ = world([make_item("item-1", "still.png", SetSpeed=False)])
    code, _, err = run(resolve, capsys, "item", "speed", "200", "-i", "1")
    assert code == 1 and "Resolve rejected speed {'Percentage': 200.0}" in err and "Stills cannot be retimed" in err


def test_fades_and_speed_fail_when_resolve_returns_nothing(capsys):
    resolve, _ = world([make_item("item-1", "shot_010", GetFades=None, GetSpeed=None)])
    code, _, err = run(resolve, capsys, "item", "fades", "-i", "1")
    assert code == 1 and "returned no fades for 'shot_010' (item#item-1)" in err
    code, _, err = run(resolve, capsys, "item", "speed", "-i", "1")
    assert code == 1 and "returned no speed for 'shot_010' (item#item-1)" in err


def test_speed_ripple_needs_a_percent(capsys):
    resolve, parts = world()
    code, _, err = run(resolve, capsys, "item", "speed", "--ripple", "--pitch-correction", "on", "-i", "1")
    assert code == 1 and "only apply together with a new PERCENT" in err
    assert parts["items"][0].calls == []


# --- transitions -------------------------------------------------------------------------------


def test_transition_passes_transition_options_and_returns_the_new_item(capsys):
    transition = make_item("tr-1", "Cross Dissolve", GetStart=86436, GetDuration=24)
    resolve, parts = world([make_item("item-1", "shot_010", AddTransition=transition)])
    code, out, _ = run(resolve, capsys, "item", "transition", "Cross Dissolve", "-p", "start", "-a", "center",
                       "-d", "24", "-i", "1")
    assert code == 0
    assert parts["items"][0].called("AddTransition") == [({"type": "Cross Dissolve", "category": "simple",
                                                           "position": "start", "alignment": "center",
                                                           "duration": 24},)]
    assert out == [{"ref": "item#item-1", "name": "shot_010", "transition": "item#tr-1", "start": 86436,
                    "duration": 24}]


def test_transition_defaults_leave_alignment_and_duration_to_resolve(capsys):
    resolve, parts = world([make_item("item-1", "shot_010", AddTransition=make_item("tr-1", "Wipe"))])
    code, _, _ = run(resolve, capsys, "item", "transition", "Box Wipe", "-c", "fusion", "-i", "1")
    assert code == 0
    assert parts["items"][0].called("AddTransition") == [({"type": "Box Wipe", "category": "fusion",
                                                           "position": "end"},)]


def test_transition_failure_suggests_similar_fusion_transitions(capsys, monkeypatch):
    searched = []

    def fake_list(kind=None, search=None):
        searched.append((kind, search))
        return [{"name": "Box Wipe"}, {"name": "Box Twist"}]

    monkeypatch.setattr(items_module.templates, "list_templates", fake_list)
    resolve, _ = world([make_item("item-1", "shot_010", AddTransition=None)])
    code, _, err = run(resolve, capsys, "item", "transition", "Box Wip", "-c", "fusion", "-i", "1")
    assert code == 1 and "could not add transition 'Box Wip' (category fusion)" in err
    assert "similar names: Box Wipe, Box Twist" in err and searched == [("transitions", "Box")]


def test_transition_rejects_unknown_category():
    resolve, _ = world()
    assert usage_error(resolve, "item", "transition", "Cross Dissolve", "-c", "video", "-i", "1") == 2


# --- enable, colors, flags, rename ---------------------------------------------------------------


def test_disable_without_index_disables_every_item(capsys):
    resolve, parts = world()
    code, out, _ = run(resolve, capsys, "item", "disable")
    assert code == 0 and [r["enabled"] for r in out] == [False, False]
    assert [item.called("SetClipEnabled") for item in parts["items"]] == [[(False,)], [(False,)]]


def test_enable_failure_is_an_error(capsys):
    resolve, _ = world([make_item("item-1", "shot_010", SetClipEnabled=False)])
    code, _, err = run(resolve, capsys, "item", "enable", "-i", "1")
    assert code == 1 and "could not enable 'shot_010' (item#item-1)" in err


@needs_stub
def test_color_matches_names_case_insensitively(capsys):
    resolve, parts = world()
    code, out, _ = run(resolve, capsys, "item", "color", "teal", "-i", "1")
    assert code == 0 and out == [{"ref": "item#item-1", "name": "shot_010", "color": "Teal"}]
    assert parts["items"][0].called("SetClipColor") == [("Teal",)]


@needs_stub
def test_color_unknown_name_fails_before_touching_resolve(capsys):
    resolve, parts = world()
    code, _, err = run(resolve, capsys, "item", "color", "Magenta", "-i", "1")
    assert code == 1 and "'Magenta' is not one of" in err and "Chocolate" in err
    assert parts["timeline"].calls == []


def test_color_clear(capsys):
    resolve, parts = world([make_item("item-1", "shot_010", ClearClipColor=False)])
    code, _, err = run(resolve, capsys, "item", "color-clear", "-i", "1")
    assert code == 1 and "could not clear the clip color" in err
    assert parts["items"][0].called("ClearClipColor") == [()]


@needs_stub
def test_flag_add_adds_each_color_and_reports_the_flags(capsys):
    resolve, parts = world([make_item("item-1", "shot_010", GetFlagList=["Blue", "Red"])])
    code, out, _ = run(resolve, capsys, "item", "flag-add", "Blue", "red", "-i", "1")
    assert code == 0 and out == [{"ref": "item#item-1", "name": "shot_010", "flags": ["Blue", "Red"]}]
    assert parts["items"][0].called("AddFlag") == [("Blue",), ("Red",)]


@needs_stub
def test_flag_add_tries_every_color_then_reports_failure(capsys):
    item = make_item("item-1", "shot_010", AddFlag=lambda color: color != "Blue")
    resolve, _ = world([item])
    code, _, err = run(resolve, capsys, "item", "flag-add", "Blue", "Red", "-i", "1")
    # Only the refused color is named as failed; the one Resolve accepted is reported as added.
    assert code == 1 and "could not add flag(s) Blue to 'shot_010' (item#item-1) (added: Red)." in err
    assert item.called("AddFlag") == [("Blue",), ("Red",)]


@needs_stub
def test_flag_add_names_every_refused_color_when_none_was_added(capsys):
    item = make_item("item-1", "shot_010", AddFlag=False)
    resolve, _ = world([item])
    code, _, err = run(resolve, capsys, "item", "flag-add", "Blue", "Red", "-i", "1")
    assert code == 1 and "could not add flag(s) Blue, Red to 'shot_010' (item#item-1)." in err
    assert "added:" not in err


@needs_stub
def test_flag_clear_failure_is_an_error(capsys):
    item = make_item("item-1", "shot_010", ClearFlags=False)
    resolve, _ = world([item])
    code, _, err = run(resolve, capsys, "item", "flag-clear", "Red", "-i", "1")
    assert code == 1 and "could not clear Red flags of 'shot_010' (item#item-1)" in err
    assert item.called("ClearFlags") == [("Red",)] and item.called("GetFlagList") == []


@needs_stub
def test_flag_clear_defaults_to_all(capsys):
    resolve, parts = world()
    code, _, _ = run(resolve, capsys, "item", "flag-clear", "-i", "1")
    assert code == 0 and parts["items"][0].called("ClearFlags") == [("All",)]
    code, _, _ = run(resolve, capsys, "item", "flag-clear", "cyan", "-i", "2")
    assert code == 0 and parts["items"][1].called("ClearFlags") == [("Cyan",)]


def test_flags_lists_every_item(capsys):
    items = [make_item("item-1", "shot_010", GetFlagList=["Blue"]), make_item("item-2", "shot_020")]
    resolve, _ = world(items)
    code, out, _ = run(resolve, capsys, "--read-only", "item", "flags")
    assert code == 0 and [r["flags"] for r in out] == [["Blue"], []]


def test_rename_reports_the_old_name(capsys):
    resolve, parts = world()
    code, out, _ = run(resolve, capsys, "item", "rename", "Hero", "--ref", "item#item-2")
    assert code == 0 and out["old_name"] == "shot_020"
    assert parts["items"][1].called("SetName") == [("Hero",)]


def test_rename_failure(capsys):
    resolve, _ = world([make_item("item-1", "shot_010", SetName=False)])
    code, _, err = run(resolve, capsys, "item", "rename", "Hero", "-i", "1")
    assert code == 1 and "could not rename 'shot_010' (item#item-1) to 'Hero'" in err


# --- delete and linked ---------------------------------------------------------------------------


def test_delete_one_item_with_ripple(capsys):
    resolve, parts = world()
    code, out, _ = run(resolve, capsys, "item", "delete", "-i", "2", "--ripple")
    assert code == 0 and out["ripple"] is True and out["deleted"] == [{"ref": "item#item-2", "name": "shot_020"}]
    assert parts["timeline"].called("DeleteClips") == [([parts["items"][1]], True)]


def test_delete_needs_an_explicit_selection(capsys):
    resolve, parts = world()
    code, _, err = run(resolve, capsys, "item", "delete")
    assert code == 1 and "--all for every item" in err
    assert parts["timeline"].called("DeleteClips") == []
    code, _, err = run(resolve, capsys, "item", "delete", "--all", "-i", "1")
    assert code == 1 and "not both" in err
    code, out, _ = run(resolve, capsys, "item", "delete", "--all")
    assert code == 0 and parts["timeline"].called("DeleteClips") == [(parts["items"], False)]


def test_delete_by_ref_finds_the_timeline(capsys):
    resolve, parts = world()
    parts["timeline"].set(DeleteClips=False)
    code, _, err = run(resolve, capsys, "item", "delete", "--ref", "item#item-1")
    assert code == 1 and "could not delete item#item-1 from 'Edit v1'" in err
    assert parts["timeline"].called("DeleteClips") == [([parts["items"][0]], False)]


def test_linked_items_with_their_tracks(capsys):
    audio = make_item("aud-1", "shot_010", track=("audio", 2), GetType="audio")
    resolve, _ = world([make_item("item-1", "shot_010", GetLinkedItems=[audio])])
    code, out, _ = run(resolve, capsys, "item", "linked", "-i", "1")
    assert code == 0
    assert out == [{"ref": "item#aud-1", "name": "shot_010", "type": "audio", "track_type": "audio", "track": 2,
                    "start": 86400, "end": 86448}]


def test_track_from_a_lua_style_table(capsys):
    """Through the Lua bridge GetTrackTypeAndIndex arrives as a 1-based table."""
    audio = make_item("aud-1", "shot_010", GetTrackTypeAndIndex={1: "audio", 2: 3})
    resolve, _ = world([make_item("item-1", "shot_010", GetLinkedItems=[audio])])
    code, out, _ = run(resolve, capsys, "item", "linked", "-i", "1")
    assert code == 0 and (out[0]["track_type"], out[0]["track"]) == ("audio", 3)


# --- AI tools, multicam, blanking, stereo, sidecar, burn-in ---------------------------------------


def test_stabilize_and_smart_reframe_run_on_each_item(capsys):
    resolve, parts = world()
    assert run(resolve, capsys, "item", "stabilize")[0] == 0
    assert run(resolve, capsys, "item", "smart-reframe", "-i", "2")[0] == 0
    assert [item.called("Stabilize") for item in parts["items"]] == [[()], [()]]
    assert parts["items"][1].called("SmartReframe") == [()] and parts["items"][0].called("SmartReframe") == []


def test_ai_tool_failure_mentions_studio(capsys):
    resolve, _ = world([make_item("item-1", "shot_010", SmartReframe=False)])
    code, _, err = run(resolve, capsys, "item", "smart-reframe", "-i", "1")
    assert code == 1 and "could not smart-reframe" in err and "Studio" in err


def test_magic_mask_modes(capsys):
    resolve, parts = world()
    assert run(resolve, capsys, "item", "magic-mask", "both", "-i", "1")[0] == 0
    assert run(resolve, capsys, "item", "magic-mask", "backward", "-i", "1")[0] == 0
    assert run(resolve, capsys, "item", "magic-mask", "--regenerate", "-i", "1")[0] == 0
    assert parts["items"][0].called("CreateMagicMask") == [("BI",), ("B",)]
    assert parts["items"][0].called("RegenerateMagicMask") == [()]


def test_magic_mask_needs_exactly_one_mode(capsys):
    resolve, parts = world([make_item("item-1", "shot_010", CreateMagicMask=False)])
    code, _, err = run(resolve, capsys, "item", "magic-mask", "-i", "1")
    assert code == 1 and "Give a DIRECTION" in err
    code, _, err = run(resolve, capsys, "item", "magic-mask", "forward", "--regenerate", "-i", "1")
    assert code == 1 and "Give a DIRECTION" in err
    assert parts["items"][0].calls == []
    code, _, err = run(resolve, capsys, "item", "magic-mask", "forward", "-i", "1")
    assert code == 1 and "could not create a forward magic mask" in err
    assert parts["items"][0].called("CreateMagicMask") == [("F",)]


def test_magic_mask_regenerate_failure(capsys):
    item = make_item("item-1", "shot_010", RegenerateMagicMask=False)
    resolve, _ = world([item])
    code, _, err = run(resolve, capsys, "item", "magic-mask", "--regenerate", "-i", "1")
    assert code == 1 and "could not regenerate the magic mask of 'shot_010' (item#item-1)" in err
    assert item.called("RegenerateMagicMask") == [()] and item.called("CreateMagicMask") == []


@needs_stub
def test_multicam_flatten_passes_the_grade_constant(capsys):
    resolve, parts = world()
    resolve.FLATTEN_MULTICAM_COPY_GRADE = 0.0
    resolve.FLATTEN_MULTICAM_RETAIN_GRADE_FROM_ANGLE = 1.0
    assert run(resolve, capsys, "item", "multicam-flatten", "--grade", "retain-angle", "-i", "1")[0] == 0
    assert parts["items"][0].called("FlattenMulticam") == [(1.0,)]
    parts["items"][0].set(FlattenMulticam=False)
    code, _, err = run(resolve, capsys, "item", "multicam-flatten", "--grade", "copy", "-i", "1")
    assert code == 1 and "is it a multicam clip?" in err
    assert parts["items"][0].called("FlattenMulticam")[-1] == (0.0,)


def test_multicam_flatten_requires_a_grade_option():
    resolve, _ = world()
    assert usage_error(resolve, "item", "multicam-flatten", "-i", "1") == 2


@needs_stub
def test_multicam_switch_types_the_settings(capsys):
    resolve, parts = world()
    resolve.SMART_SWITCH_QUALITY_BETTER = 1.0
    code, out, _ = run(resolve, capsys, "item", "multicam-switch", "minEditDuration=2",
                       "quality=SMART_SWITCH_QUALITY_BETTER", "switchOnVideoOnly=false", "-i", "1")
    assert code == 0
    settings = {"minEditDuration": 2.0, "quality": 1.0, "switchOnVideoOnly": False}
    assert parts["items"][0].called("PerformMulticamSmartSwitch") == [(settings,)]
    assert out["settings"] == settings
    code, _, err = run(resolve, capsys, "item", "multicam-switch", "speed=2", "-i", "1")
    assert code == 1 and "Unknown SmartSwitchSettings key(s): speed" in err


@needs_stub
def test_multicam_switch_failure(capsys):
    item = make_item("item-1", "shot_010", PerformMulticamSmartSwitch=False)
    resolve, _ = world([item])
    code, _, err = run(resolve, capsys, "item", "multicam-switch", "-i", "1")
    assert code == 1 and "could not run SmartSwitch on 'shot_010' (item#item-1)" in err
    assert item.called("PerformMulticamSmartSwitch") == [({},)]


def test_blanking_without_options_only_reads(capsys):
    blanking = {"Top": 0, "Bottom": 0, "Left": 4, "Right": 4}
    resolve, parts = world([make_item("item-1", "shot_010", GetOutputBlanking=blanking,
                                      GetUseTimelineForOutputBlanking=False)])
    code, out, _ = run(resolve, capsys, "--read-only", "item", "blanking", "-i", "1")
    assert code == 0 and out["blanking"] == blanking and out["use_timeline"] is False
    assert not [n for n in names(parts["items"][0]) if n.startswith("Set")]


def test_blanking_set_merges_sides_after_switching_off_timeline_blanking(capsys):
    blanking = {"Top": 0, "Bottom": 0, "Left": 4, "Right": 4}
    item = make_item("item-1", "shot_010", GetOutputBlanking=blanking)
    resolve, _ = world([item])
    code, _, _ = run(resolve, capsys, "item", "blanking", "--top", "20", "--use-timeline", "off", "-i", "1")
    assert code == 0
    sets = [(n, a) for n, a in item.calls if n.startswith("Set")]
    assert sets == [("SetUseTimelineForOutputBlanking", (False,)),
                    ("SetOutputBlanking", ({"Top": 20, "Bottom": 0, "Left": 4, "Right": 4},))]


def test_blanking_rejects_sides_together_with_timeline_blanking(capsys):
    item = make_item("item-1", "shot_010", GetOutputBlanking={})
    resolve, _ = world([item])
    code, _, err = run(resolve, capsys, "item", "blanking", "--use-timeline", "on", "--top", "4", "--left", "2",
                       "-i", "1")
    assert code == 1 and "--use-timeline on" in err and "--top, --left would not take effect" in err
    assert item.calls == []


def test_blanking_use_timeline_failure_stops_before_setting_sides(capsys):
    item = make_item("item-1", "shot_010", GetOutputBlanking={"Top": 0}, SetUseTimelineForOutputBlanking=False)
    resolve, _ = world([item])
    code, _, err = run(resolve, capsys, "item", "blanking", "--use-timeline", "off", "--top", "4", "-i", "1")
    assert code == 1 and "could not turn timeline output blanking off for 'shot_010' (item#item-1)" in err
    assert item.called("SetUseTimelineForOutputBlanking") == [(False,)] and item.called("SetOutputBlanking") == []


def test_blanking_failures(capsys):
    item = make_item("item-1", "shot_010", GetOutputBlanking={}, SetOutputBlanking=False)
    resolve, _ = world([item])
    code, _, err = run(resolve, capsys, "item", "blanking", "--left", "-3", "-i", "1")
    assert code == 1 and "must be 0 or more: Left" in err and item.calls == []
    code, _, err = run(resolve, capsys, "item", "blanking", "--left", "8", "-i", "1")
    assert code == 1 and "Resolve rejected output blanking {'Left': 8}" in err


def test_stereo_serializes_keyframe_dicts(capsys):
    window = {0: {"left": 1.0, "right": 0.0, "top": 0.0, "bottom": 0.0}}
    resolve, _ = world([make_item("item-1", "shot_010", GetStereoConvergenceValues={0: 0.5, 24: 1.0},
                                  GetStereoLeftFloatingWindowParams=window,
                                  GetStereoRightFloatingWindowParams={})])
    code, out, _ = run(resolve, capsys, "item", "stereo", "-i", "1")
    assert code == 0 and out["convergence"] == {"0": 0.5, "24": 1.0}
    assert out["left_floating_window"] == {"0": window[0]} and out["right_floating_window"] == {}


def test_stereo_fails_when_resolve_returns_nothing(capsys):
    resolve, _ = world([make_item("item-1", "shot_010", GetStereoConvergenceValues=None)])
    code, _, err = run(resolve, capsys, "item", "stereo", "-i", "1")
    assert code == 1 and "no stereo 3D convergence" in err


def test_sidecar_failure_names_the_clip_formats(capsys):
    resolve, parts = world([make_item("item-1", "a.braw"), make_item("item-2", "b.mov", UpdateSidecar=False)])
    code, _, err = run(resolve, capsys, "item", "sidecar")
    assert code == 1 and "sidecar of 'b.mov'" in err and "BRAW" in err and "item#item-1" in err
    assert parts["items"][0].called("UpdateSidecar") == [()]


def test_burn_in_loads_the_preset_and_lists_presets_on_failure(capsys):
    resolve, parts = world()
    assert run(resolve, capsys, "item", "burn-in", "Slate", "-i", "1")[0] == 0
    assert parts["items"][0].called("LoadBurnInPreset") == [("Slate",)]
    parts["items"][0].set(LoadBurnInPreset=False)
    resolve.set(GetBurnInPresetList=["Default", "Dailies"])
    code, _, err = run(resolve, capsys, "item", "burn-in", "Slat", "-i", "1")
    assert code == 1 and "preset 'Slat'" in err and "Presets: Default, Dailies" in err


# --- takes ------------------------------------------------------------------------------------------


def take_world(count=2, selected=1, **returns):
    clips = [Rec("clip1", GetName="a.mov", GetUniqueId="clip-1"), Rec("clip2", GetName="b.mov", GetUniqueId="clip-2")]
    takes = {i + 1: {"mediaPoolItem": clip, "startFrame": 0, "endFrame": 47 + i} for i, clip in enumerate(clips)}
    item = make_item("item-1", "shot_010", GetTakesCount=count, GetSelectedTakeIndex=selected,
                     GetTakeByIndex=lambda i: takes.get(i), **returns)
    resolve, parts = rec_resolve(items=[item], clips=clips)
    return resolve, parts, item


def test_take_list(capsys):
    resolve, _, item = take_world()
    code, out, _ = run(resolve, capsys, "--read-only", "take", "list", "-i", "1")
    assert code == 0 and out["selected"] == 1
    assert out["takes"] == [
        {"index": 1, "clip": "a.mov", "clip_ref": "clip#clip-1", "start_frame": 0, "end_frame": 47},
        {"index": 2, "clip": "b.mov", "clip_ref": "clip#clip-2", "start_frame": 0, "end_frame": 48}]
    assert item.called("GetTakeByIndex") == [(1,), (2,)]


def test_take_list_fails_on_missing_take_info(capsys):
    resolve, _, _ = take_world(count=3)
    code, _, err = run(resolve, capsys, "take", "list", "-i", "1")
    assert code == 1 and "no info for take 3" in err


def test_take_add_by_name_with_and_without_extents(capsys):
    resolve, parts, item = take_world(count=0, selected=0)
    assert run(resolve, capsys, "take", "add", "b.mov", "--start", "10", "--end", "40", "-i", "1")[0] == 0
    assert run(resolve, capsys, "take", "add", "clip#clip-1", "-i", "1")[0] == 0
    assert item.called("AddTake") == [(parts["clips"][1], 10, 40), (parts["clips"][0],)]


def test_take_add_errors(capsys):
    resolve, _, item = take_world(count=0, selected=0, AddTake=False)
    code, _, err = run(resolve, capsys, "take", "add", "a.mov", "--start", "10", "-i", "1")
    assert code == 1 and "Give both --start and --end" in err
    code, _, err = run(resolve, capsys, "take", "add", "missing.mov", "-i", "1")
    assert code == 1 and "No media pool clip named 'missing.mov'" in err
    assert item.called("AddTake") == []
    code, _, err = run(resolve, capsys, "take", "add", "a.mov", "-i", "1")
    assert code == 1 and "could not add 'a.mov' as a take of 'shot_010' (item#item-1)" in err


def test_take_select_and_delete_check_the_range(capsys):
    resolve, _, item = take_world()
    assert run(resolve, capsys, "take", "select", "2", "-i", "1")[0] == 0
    assert run(resolve, capsys, "take", "delete", "1", "-i", "1")[0] == 0
    assert item.called("SelectTakeByIndex") == [(2,)] and item.called("DeleteTakeByIndex") == [(1,)]
    code, _, err = run(resolve, capsys, "take", "select", "3", "-i", "1")
    assert code == 1 and "Take 3 is out of range; 'shot_010' (item#item-1) has 2 take(s)" in err
    assert item.called("SelectTakeByIndex") == [(2,)]


def test_take_commands_on_a_plain_clip(capsys):
    resolve, _, item = take_world(count=0, selected=0)
    code, _, err = run(resolve, capsys, "take", "select", "1", "-i", "1")
    assert code == 1 and "is not a take selector" in err
    code, _, err = run(resolve, capsys, "take", "finalize", "-i", "1")
    assert code == 1 and "nothing to finalize" in err
    assert item.called("SelectTakeByIndex") == [] and item.called("FinalizeTake") == []


def test_take_resolve_failures(capsys):
    resolve, _, item = take_world(SelectTakeByIndex=False, DeleteTakeByIndex=False, FinalizeTake=False)
    code, _, err = run(resolve, capsys, "take", "select", "1", "-i", "1")
    assert code == 1 and "could not select take 1" in err
    code, _, err = run(resolve, capsys, "take", "delete", "2", "-i", "1")
    assert code == 1 and "could not delete take 2" in err
    code, _, err = run(resolve, capsys, "take", "finalize", "-i", "1")
    assert code == 1 and "could not finalize" in err
    item.set(FinalizeTake=True)
    code, out, _ = run(resolve, capsys, "take", "finalize", "-i", "1")
    assert code == 0 and out["$ref"] == "item#item-1"


def test_take_number_is_one_based():
    resolve, _, _ = take_world()
    assert usage_error(resolve, "take", "select", "0", "-i", "1") == 2


# --- registration and read-only declarations -----------------------------------------------------


def owned_leaves():
    return {words: leaf for words, leaf, _ in leaf_parsers(build_parser()) if words[0] in ("item", "take")}


def test_every_command_declares_its_handler_and_read_only_flag():
    leaves = owned_leaves()
    assert {" ".join(words) for words in leaves} >= {
        "item list", "item info", "item get", "item set", "item fades", "item speed", "item transition",
        "item enable", "item disable", "item color", "item color-clear", "item flags", "item flag-add",
        "item flag-clear", "item rename", "item delete", "item linked", "item stabilize", "item smart-reframe",
        "item magic-mask", "item multicam-flatten", "item multicam-switch", "item blanking", "item stereo",
        "item sidecar", "item burn-in", "take list", "take add", "take select", "take delete", "take finalize"}
    for words, leaf in leaves.items():
        assert leaf._defaults["func"].__module__ == items_module.__name__, words
        assert "read_only" in leaf._defaults, words


READ_COMMANDS = [
    ["item", "list"], ["item", "info", "-i", "1"], ["item", "get", "-i", "1"], ["item", "flags"],
    ["item", "linked", "-i", "1"], ["item", "stereo", "-i", "1"], ["item", "fades", "-i", "1"],
    ["item", "speed", "-i", "1"], ["item", "blanking", "-i", "1"], ["take", "list", "-i", "1"],
]


@pytest.mark.parametrize("argv", READ_COMMANDS, ids=" ".join)
def test_read_only_commands_call_only_getters(capsys, argv):
    clip = Rec("clip", GetName="a.mov", GetUniqueId="clip-1")
    linked = make_item("aud-1", "a", track=("audio", 1))
    item = make_item("item-1", "shot_010", GetProperties={"ZoomX": 1.0}, GetLinkedItems=[linked],
                     GetStereoConvergenceValues={}, GetStereoLeftFloatingWindowParams={},
                     GetStereoRightFloatingWindowParams={}, GetFades={"FadeIn": 0, "FadeOut": 0},
                     GetSpeed={"Percentage": 100.0}, GetOutputBlanking={}, GetMediaPoolItem=clip,
                     GetTakesCount=1, GetSelectedTakeIndex=1,
                     GetTakeByIndex={"mediaPoolItem": clip, "startFrame": 0, "endFrame": 1})
    resolve, parts = world([item])
    code, _, err = run(resolve, capsys, "--read-only", *argv)
    assert code == 0, err
    recs = [resolve, parts["pm"], parts["project"], parts["timeline"], item, linked, clip]
    called = {name for rec in recs for name in names(rec)}
    assert called and all(name.startswith(("Get", "Is", "Has")) for name in called), sorted(called)


@pytest.mark.parametrize("argv", [
    ["item", "set", "Pan=1", "-i", "1"], ["item", "fades", "--in", "2", "-i", "1"],
    ["item", "speed", "50", "-i", "1"], ["item", "transition", "Cross Dissolve", "-i", "1"],
    ["item", "enable", "-i", "1"], ["item", "disable", "-i", "1"], ["item", "color", "Teal", "-i", "1"],
    ["item", "color-clear", "-i", "1"], ["item", "flag-add", "Blue", "-i", "1"], ["item", "flag-clear", "-i", "1"],
    ["item", "rename", "X", "-i", "1"], ["item", "delete", "-i", "1"], ["item", "stabilize", "-i", "1"],
    ["item", "smart-reframe", "-i", "1"], ["item", "magic-mask", "both", "-i", "1"],
    ["item", "multicam-flatten", "--grade", "copy", "-i", "1"], ["item", "multicam-switch", "-i", "1"],
    ["item", "blanking", "--use-timeline", "on", "-i", "1"], ["item", "sidecar", "-i", "1"],
    ["item", "burn-in", "P", "-i", "1"], ["take", "add", "a.mov", "-i", "1"], ["take", "select", "1", "-i", "1"],
    ["take", "delete", "1", "-i", "1"], ["take", "finalize", "-i", "1"],
], ids=" ".join)
def test_changing_commands_are_refused_in_read_only_mode(capsys, argv):
    resolve, parts = world()
    code, _, err = run(resolve, capsys, "--read-only", *argv)
    assert code == 1 and "read-only mode is on" in err
    assert all(item.calls == [] for item in parts["items"])
