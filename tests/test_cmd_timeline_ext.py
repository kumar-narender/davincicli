"""Tests for the timeline_ext commands: timeline (new actions), track, playhead and marker (new actions)."""

import base64
import json
import os

import pytest

from dava import connect
from dava.cli import build_parser, command_is_read_only, main
from dava.commands import timeline_ext
from dava.registry import leaf_parsers
from recorder import Rec, rec_resolve

HAS_STUB = os.path.isfile(connect.stub_path() or "")
needs_stub = pytest.mark.skipif(not HAS_STUB, reason="Resolve scripting API definition not installed")


def run(resolve, capsys, *argv):
    code = main(["--json", *argv], resolve=resolve)
    captured = capsys.readouterr()
    out = json.loads(captured.out) if captured.out.strip() else None
    return code, out, captured.err


def names(rec):
    return [name for name, _ in rec.calls]


def items_on_v1():
    return [Rec("item1", GetName="shot_010", GetUniqueId="item-1", GetType="video",
                GetTrackTypeAndIndex=["video", 1], GetStart=86400, GetEnd=86448, GetDuration=48),
            Rec("item2", GetName="shot_020", GetUniqueId="item-2", GetType="video",
                GetTrackTypeAndIndex=["video", 1], GetStart=86448, GetEnd=86496, GetDuration=48)]


def session():
    resolve, parts = rec_resolve(items=items_on_v1())
    return resolve, parts


def constants(resolve, **values):
    """Give a recorded resolve object real constant values (Rec would hand out methods)."""
    resolve.__dict__.update(values)
    return resolve


def marker_host(rec, markers):
    """Make rec keep markers {frame: info} that its marker methods read and change."""
    def delete_at(frame):
        return markers.pop(frame, None) is not None

    def delete_color(color):
        doomed = [f for f, info in markers.items() if color == "All" or info["color"] == color]
        for frame in doomed:
            del markers[frame]
        return bool(doomed)

    def delete_data(data):
        for frame in sorted(markers):
            if markers[frame].get("customData") == data:
                del markers[frame]
                return True
        return False

    def by_data(data):
        return next((dict(markers[f]) for f in sorted(markers) if markers[f].get("customData") == data), {})

    def update(frame, data):
        if frame not in markers:
            return False
        markers[frame]["customData"] = data
        return True

    rec.set(GetMarkers=lambda: dict(markers), DeleteMarkerAtFrame=delete_at, DeleteMarkersByColor=delete_color,
            DeleteMarkerByCustomData=delete_data, GetMarkerByCustomData=by_data, UpdateMarkerCustomData=update,
            GetMarkerCustomData=lambda frame: markers[frame].get("customData", ""))
    return markers


def two_markers():
    return {10: {"color": "Red", "duration": 1, "note": "", "name": "a", "customData": "id-1"},
            40: {"color": "Blue", "duration": 5, "note": "n", "name": "b", "customData": "id-2"}}


# --- registration ------------------------------------------------------------------------


OWN_GROUPS = {"track", "playhead"}


def own_leaves():
    for words, parser, _ in leaf_parsers(build_parser()):
        func = parser.get_default("func")
        if func is not None and func.__module__ == timeline_ext.__name__:
            yield words, parser


def test_commands_are_registered_with_read_only_declared():
    leaves = {" ".join(words): parser for words, parser in own_leaves()}
    expected = {
        "timeline info", "timeline rename", "timeline duplicate", "timeline delete", "timeline import",
        "timeline import-aaf", "timeline settings", "timeline set", "timeline start-timecode", "timeline selected",
        "timeline in-out", "timeline set-in-out", "timeline clear-in-out", "timeline blanking", "timeline thumbnail",
        "timeline scene-cuts", "timeline subtitles", "timeline convert-stereo", "timeline link", "timeline unlink",
        "timeline align", "timeline compound", "timeline fusion-clip", "timeline delete-clips",
        "timeline dolby-vision", "track list", "track add", "track delete", "track name", "track enable",
        "track disable", "track lock", "track unlock", "playhead get", "playhead set", "playhead item",
        "marker show", "marker create", "marker delete", "marker find", "marker data",
    }
    assert set(leaves) == expected
    for name, parser in leaves.items():
        assert parser.get_default("read_only") is not None, name
    # Every command of the groups this module owns comes from it.
    for words, parser, _ in leaf_parsers(build_parser()):
        if words[0] in OWN_GROUPS:
            assert parser.get_default("func").__module__ == timeline_ext.__name__


def test_help_texts_are_ascii():
    for words, parser in own_leaves():
        parser.format_help().encode("ascii")


# One sample command line per action (both forms for the commands whose read_only depends on the arguments)
# -> whether it may run in --read-only mode. Only Get*/Is*/Has* calls and no file writes count as read-only.
READ_ONLY_CASES = [
    ("timeline info", True), ("timeline rename X", False), ("timeline duplicate X", False),
    ("timeline delete X", False), ("timeline import cut.edl", False), ("timeline import-aaf a.aaf", False),
    ("timeline settings", True), ("timeline set useCustomSettings=1", False),
    ("timeline start-timecode", True), ("timeline start-timecode 01:00:00:00", False),
    ("timeline selected", True), ("timeline in-out", True), ("timeline set-in-out 1 2", False),
    ("timeline clear-in-out", False), ("timeline blanking", True), ("timeline blanking --left 2", False),
    ("timeline thumbnail", True), ("timeline thumbnail --base64", True), ("timeline thumbnail -o t.ppm", False),
    ("timeline scene-cuts", False), ("timeline subtitles", False), ("timeline convert-stereo", False),
    ("timeline link --selected", False), ("timeline unlink --selected", False), ("timeline align --selected", False),
    ("timeline compound --selected", False), ("timeline fusion-clip --selected", False),
    ("timeline delete-clips --selected", False), ("timeline dolby-vision --all", False),
    ("track list", True), ("track add video", False), ("track delete video 1", False),
    ("track name video 1", True), ("track name video 1 X", False), ("track enable video 1", False),
    ("track disable video 1", False), ("track lock video 1", False), ("track unlock video 1", False),
    ("playhead get", True), ("playhead set 01:00:00:00", False), ("playhead item", True),
    ("marker show", True), ("marker create 1", False), ("marker delete --all", False),
    ("marker find x", True), ("marker data 1", True), ("marker data 1 x", False),
]


def test_read_only_flags_match_what_each_command_calls(capsys):
    covered = {" ".join(line.split()[:2]) for line, _ in READ_ONLY_CASES}
    assert covered == {" ".join(words) for words, _ in own_leaves()}
    parser = build_parser()
    for line, expected in READ_ONLY_CASES:
        assert command_is_read_only(parser.parse_args(line.split())) is expected, line
        if not expected:
            # Refused before Resolve is touched at all.
            resolve, _ = session()
            code, _, err = run(resolve, capsys, "--read-only", *line.split())
            assert code == 1 and "read-only mode is on" in err, line
            assert resolve.calls == [], line


# --- timeline: identity, copies, import -----------------------------------------------------


def test_info_describes_the_timeline(capsys):
    resolve, parts = session()
    clip = Rec("tlclip", GetName="Edit v1", GetUniqueId="clip-tl")
    parts["timeline"].set(GetMediaPoolItem=clip, GetTrackCount=lambda kind: {"video": 2, "audio": 1}.get(kind, 0))
    code, out, _ = run(resolve, capsys, "--read-only", "timeline", "info")
    assert code == 0
    assert out["$ref"] == "timeline#tl-1" and out["name"] == "Edit v1" and out["current"] is True
    assert out["start_frame"] == 86400 and out["end_frame"] == 86640 and out["duration_frames"] == 240
    assert out["tracks"] == {"video": 2, "audio": 1, "subtitle": 0}
    assert out["media_pool_item"]["$ref"] == "clip#clip-tl"


def test_rename_checks_the_name_and_calls_set_name(capsys):
    resolve, parts = session()
    code, out, _ = run(resolve, capsys, "timeline", "rename", "Final")
    assert code == 0 and out == {"timeline": "Final", "old_name": "Edit v1"}
    assert parts["timeline"].called("SetName") == [("Final",)]

    code, _, err = run(resolve, capsys, "timeline", "rename", "Edit v1")
    assert code == 1 and "already has a timeline named 'Edit v1'" in err
    assert len(parts["timeline"].called("SetName")) == 1

    parts["timeline"].set(SetName=False)
    code, _, err = run(resolve, capsys, "timeline", "rename", "Other")
    assert code == 1 and "could not rename" in err


def test_duplicate_returns_the_copy(capsys):
    resolve, parts = session()
    copy = Rec("copy", GetName="Edit v2", GetUniqueId="tl-2")
    parts["timeline"].set(DuplicateTimeline=copy)
    code, out, _ = run(resolve, capsys, "timeline", "duplicate", "Edit v2")
    assert code == 0
    assert parts["timeline"].called("DuplicateTimeline") == [("Edit v2",)]
    assert out == {"$type": "Timeline", "$ref": "timeline#tl-2", "name": "Edit v2", "source": "Edit v1"}

    parts["timeline"].set(DuplicateTimeline=None)
    code, _, err = run(resolve, capsys, "timeline", "duplicate", "Edit v3")
    assert code == 1 and "could not duplicate" in err


def test_delete_passes_the_timelines_to_the_media_pool(capsys):
    resolve, parts = session()
    code, out, _ = run(resolve, capsys, "timeline", "delete", "Edit v1")
    assert code == 0 and out == {"deleted": ["Edit v1"]}
    assert parts["mediapool"].called("DeleteTimelines") == [([parts["timeline"]],)]

    code, _, err = run(resolve, capsys, "timeline", "delete", "Nope")
    assert code == 1 and "No timeline named 'Nope'" in err
    assert len(parts["mediapool"].called("DeleteTimelines")) == 1

    parts["mediapool"].set(DeleteTimelines=False)
    code, _, err = run(resolve, capsys, "timeline", "delete", "Edit v1")
    assert code == 1 and "could not delete" in err


@needs_stub
def test_import_builds_import_options(tmp_path, capsys):
    resolve, parts = session()
    source = tmp_path / "cut.otio"
    source.write_text("{}")
    new = Rec("new", GetName="Cut", GetUniqueId="tl-9")
    parts["mediapool"].set(ImportTimelineFromFile=new)
    code, out, _ = run(resolve, capsys, "timeline", "import", str(source), "-n", "Cut", "--no-source-clips",
                       "--source-folder", "folder:/", "--interlace", "-o", "sourceClipsPath=/media")
    assert code == 0 and out["$ref"] == "timeline#tl-9" and out["file"] == str(source)
    ((path, options),) = parts["mediapool"].called("ImportTimelineFromFile")
    assert path == str(source)
    assert options == {"timelineName": "Cut", "importSourceClips": False, "sourceClipsFolders": [parts["root"]],
                       "interlaceProcessing": True, "sourceClipsPath": "/media"}

    run(resolve, capsys, "timeline", "import", str(source))
    assert parts["mediapool"].called("ImportTimelineFromFile")[-1] == (str(source),)


@needs_stub
def test_import_failures(tmp_path, capsys):
    resolve, parts = session()
    code, _, err = run(resolve, capsys, "timeline", "import", str(tmp_path / "missing.edl"))
    assert code == 1 and "File not found" in err
    source = tmp_path / "cut.edl"
    source.write_text("TITLE: x")
    code, _, err = run(resolve, capsys, "timeline", "import", str(source), "-o", "bogus=1")
    assert code == 1 and "Unknown ImportOptions key(s): bogus" in err
    code, _, err = run(resolve, capsys, "timeline", "import", str(source), "-n", "Edit v1")
    assert code == 1 and "already has a timeline named" in err
    assert parts["mediapool"].called("ImportTimelineFromFile") == []
    parts["mediapool"].set(ImportTimelineFromFile=None)
    code, _, err = run(resolve, capsys, "timeline", "import", str(source))
    assert code == 1 and "could not import a timeline" in err


@needs_stub
def test_import_aaf_types_the_options(tmp_path, capsys):
    resolve, parts = session()
    source = tmp_path / "a.aaf"
    source.write_bytes(b"aaf")
    code, out, _ = run(resolve, capsys, "timeline", "import-aaf", str(source), "-o", "insertAdditionalTracks=false",
                       "-o", "insertWithOffset=01:00:10:00")
    assert code == 0
    assert parts["timeline"].called("ImportIntoTimeline") == [
        (str(source), {"insertAdditionalTracks": False, "insertWithOffset": "01:00:10:00"})]
    assert out["options"] == {"insertAdditionalTracks": "false", "insertWithOffset": "01:00:10:00"}

    parts["timeline"].set(ImportIntoTimeline=False)
    code, _, err = run(resolve, capsys, "timeline", "import-aaf", str(source))
    assert code == 1 and "could not import the AAF" in err
    assert parts["timeline"].called("ImportIntoTimeline")[-1] == (str(source),)


# --- timeline: settings, start timecode, in/out, blanking, thumbnail -------------------------


def test_settings_show_all_or_some_keys(capsys):
    resolve, parts = session()
    parts["timeline"].set(GetSettings={"useCustomSettings": "1", "timelineFrameRate": 25.0})
    code, out, _ = run(resolve, capsys, "--read-only", "timeline", "settings")
    assert code == 0 and out == {"useCustomSettings": "1", "timelineFrameRate": 25.0}
    code, out, _ = run(resolve, capsys, "timeline", "settings", "timelineFrameRate")
    assert out == {"timelineFrameRate": 25.0}
    code, _, err = run(resolve, capsys, "timeline", "settings", "nope")
    assert code == 1 and "Unknown timeline setting(s): nope" in err


@needs_stub
def test_set_applies_use_custom_settings_first_one_key_at_a_time(capsys):
    resolve, parts = session()
    code, out, _ = run(resolve, capsys, "timeline", "set", "timelineFrameRate=25", "superScale=2",
                       "useCustomSettings=1")
    assert code == 0
    assert parts["timeline"].called("SetSettings") == [
        ({"useCustomSettings": "1"},), ({"timelineFrameRate": "25"},), ({"superScale": 2},)]
    assert out["settings"] == {"timelineFrameRate": "25", "superScale": 2, "useCustomSettings": "1"}


@needs_stub
def test_set_reports_rejected_keys_with_a_hint(capsys):
    resolve, parts = session()
    parts["timeline"].set(SetSettings=lambda values: "superScale" not in values)
    code, _, err = run(resolve, capsys, "timeline", "set", "timelineResolutionWidth=1080", "superScale=3")
    assert code == 1
    assert "rejected timeline setting(s) superScale=3" in err and "(applied: timelineResolutionWidth)" in err
    assert "add useCustomSettings=1" in err
    code, _, err = run(resolve, capsys, "timeline", "set", "bogus=1")
    assert code == 1 and "Unknown TimelineSettings key(s): bogus" in err


def test_start_timecode_get_and_set(capsys):
    resolve, parts = session()
    code, out, _ = run(resolve, capsys, "--read-only", "timeline", "start-timecode")
    assert code == 0 and out == {"timeline": "Edit v1", "start_timecode": "01:00:00:00", "start_frame": 86400}
    assert parts["timeline"].called("SetStartTimecode") == []

    code, _, err = run(resolve, capsys, "--read-only", "timeline", "start-timecode", "00:59:50:00")
    assert code == 1 and "read-only" in err
    code, _, _ = run(resolve, capsys, "timeline", "start-timecode", "00:59:50:00")
    assert code == 0 and parts["timeline"].called("SetStartTimecode") == [("00:59:50:00",)]

    parts["timeline"].set(SetStartTimecode=False)
    code, _, err = run(resolve, capsys, "timeline", "start-timecode", "bad")
    assert code == 1 and "rejected start timecode 'bad'" in err


def test_in_out_show_set_clear(capsys):
    resolve, parts = session()
    marks = {"video": {"in": 10, "out": 20}}
    parts["timeline"].set(GetMarkInOut=marks)
    code, out, _ = run(resolve, capsys, "--read-only", "timeline", "in-out")
    assert code == 0 and out == {"timeline": "Edit v1", "video": {"in": 10, "out": 20}, "audio": None}

    code, _, _ = run(resolve, capsys, "timeline", "set-in-out", "10", "20", "--type", "video")
    assert code == 0 and parts["timeline"].called("SetMarkInOut") == [(10, 20, "video")]
    run(resolve, capsys, "timeline", "set-in-out", "0", "5")
    assert parts["timeline"].called("SetMarkInOut")[-1] == (0, 5)

    code, _, err = run(resolve, capsys, "timeline", "set-in-out", "20", "10")
    assert code == 1 and "OUT (10) is before IN (20)" in err
    assert len(parts["timeline"].called("SetMarkInOut")) == 2

    with pytest.raises(SystemExit) as exc:
        main(["timeline", "set-in-out", "-5", "10"], resolve=resolve)
    assert exc.value.code == 2
    assert len(parts["timeline"].called("SetMarkInOut")) == 2

    parts["timeline"].set(SetMarkInOut=False)
    code, _, err = run(resolve, capsys, "timeline", "set-in-out", "1", "2")
    assert code == 1 and "could not set in 1 / out 2" in err

    run(resolve, capsys, "timeline", "clear-in-out")
    run(resolve, capsys, "timeline", "clear-in-out", "--type", "audio")
    assert parts["timeline"].called("ClearMarkInOut") == [(), ("audio",)]
    parts["timeline"].set(ClearMarkInOut=False)
    code, _, err = run(resolve, capsys, "timeline", "clear-in-out")
    assert code == 1 and "could not clear" in err


def test_blanking_merges_given_sides(capsys):
    resolve, parts = session()
    state = {"Top": 0, "Bottom": 0, "Left": 4, "Right": 4}
    parts["timeline"].set(GetOutputBlanking=lambda: dict(state), SetOutputBlanking=lambda v: state.update(v) or True)
    code, out, _ = run(resolve, capsys, "--read-only", "timeline", "blanking")
    assert code == 0 and out == {"timeline": "Edit v1", "Top": 0, "Bottom": 0, "Left": 4, "Right": 4}
    code, out, _ = run(resolve, capsys, "timeline", "blanking", "--top", "140", "--bottom", "140")
    assert code == 0
    assert parts["timeline"].called("SetOutputBlanking") == [({"Top": 140, "Bottom": 140, "Left": 4, "Right": 4},)]
    assert out["Top"] == 140

    parts["timeline"].set(SetOutputBlanking=False)
    code, _, err = run(resolve, capsys, "timeline", "blanking", "--left", "1")
    assert code == 1 and "rejected output blanking" in err
    with pytest.raises(SystemExit) as exc:
        main(["timeline", "blanking", "--top", "-1"], resolve=resolve)
    assert exc.value.code == 2


def test_thumbnail_reports_size_and_writes_ppm_or_raw(tmp_path, capsys):
    resolve, parts = session()
    raw = bytes(range(12))  # 2 x 2 RGB
    parts["timeline"].set(GetCurrentClipThumbnailImage={
        "width": 2, "height": 2, "format": "RGB 8 bit", "data": base64.b64encode(raw).decode()})
    code, out, _ = run(resolve, capsys, "--read-only", "timeline", "thumbnail")
    assert code == 0 and out == {"timeline": "Edit v1", "width": 2, "height": 2, "format": "RGB 8 bit", "bytes": 12}

    image = tmp_path / "thumb.ppm"
    code, out, _ = run(resolve, capsys, "timeline", "thumbnail", "-o", str(image), "--base64")
    assert code == 0 and out["path"] == str(image) and out["data"] == base64.b64encode(raw).decode()
    assert image.read_bytes() == b"P6\n2 2\n255\n" + raw
    rawfile = tmp_path / "thumb.bin"
    run(resolve, capsys, "timeline", "thumbnail", "-o", str(rawfile))
    assert rawfile.read_bytes() == raw

    code, _, err = run(resolve, capsys, "--read-only", "timeline", "thumbnail", "-o", str(rawfile))
    assert code == 1 and "read-only" in err


def test_thumbnail_failures(tmp_path, capsys):
    resolve, parts = session()
    parts["timeline"].set(GetCurrentClipThumbnailImage={})
    code, _, err = run(resolve, capsys, "timeline", "thumbnail")
    assert code == 1 and "dava page color" in err
    parts["timeline"].set(GetCurrentClipThumbnailImage={
        "width": 4, "height": 4, "format": "RGBA", "data": base64.b64encode(b"abc").decode()})
    code, _, err = run(resolve, capsys, "timeline", "thumbnail", "-o", str(tmp_path / "t.ppm"))
    assert code == 1 and "not 8-bit RGB" in err and not (tmp_path / "t.ppm").exists()
    parts["timeline"].set(GetCurrentClipThumbnailImage={"width": 1, "height": 1, "format": "RGB", "data": "!!"})
    code, _, err = run(resolve, capsys, "timeline", "thumbnail")
    assert code == 1 and "not valid base64" in err


def test_thumbnail_output_errors_and_home_paths(tmp_path, monkeypatch, capsys):
    resolve, parts = session()
    raw = bytes(range(3))
    parts["timeline"].set(GetCurrentClipThumbnailImage={
        "width": 1, "height": 1, "format": "RGB 8 bit", "data": base64.b64encode(raw).decode()})
    missing = tmp_path / "no" / "such" / "t.ppm"
    code, _, err = run(resolve, capsys, "timeline", "thumbnail", "-o", str(missing))
    assert code == 1 and f"Cannot write the thumbnail to {missing}" in err
    # '~' is expanded by dava itself: batch files and MCP calls do not pass through a shell.
    monkeypatch.setenv("HOME", str(tmp_path))
    code, out, _ = run(resolve, capsys, "timeline", "thumbnail", "-o", "~/t.ppm")
    assert code == 0 and out["path"] == str(tmp_path / "t.ppm")
    assert (tmp_path / "t.ppm").read_bytes() == b"P6\n1 1\n255\n" + raw


@needs_stub
def test_import_expands_home_in_paths(tmp_path, monkeypatch, capsys):
    resolve, parts = session()
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "cut.edl").write_text("TITLE: x")
    parts["mediapool"].set(ImportTimelineFromFile=Rec("new", GetName="Cut", GetUniqueId="tl-9"))
    code, out, _ = run(resolve, capsys, "timeline", "import", "~/cut.edl", "--source-path", "~/media")
    assert code == 0 and out["file"] == str(tmp_path / "cut.edl")
    assert parts["mediapool"].called("ImportTimelineFromFile") == [
        (str(tmp_path / "cut.edl"), {"sourceClipsPath": str(tmp_path / "media")})]


def test_getters_that_return_nothing_are_errors(capsys):
    resolve, parts = session()
    parts["timeline"].set(GetSettings=None, GetMarkInOut=None, GetOutputBlanking=None, GetCurrentTimecode="",
                          GetSelectedClips=[Rec("x", GetName="x", GetUniqueId="x-1", GetTrackTypeAndIndex=[])])
    for argv, message in ((["timeline", "settings"], "returned no settings for 'Edit v1'"),
                          (["timeline", "in-out"], "returned no in/out marks"),
                          (["timeline", "blanking"], "returned no output blanking"),
                          (["timeline", "blanking", "--top", "1"], "returned no output blanking"),
                          (["playhead", "get"], "returned no playhead position"),
                          (["timeline", "selected"], "did not report the track of 'x' (item#x-1)")):
        code, _, err = run(resolve, capsys, *argv)
        assert code == 1 and message in err, argv
    assert parts["timeline"].called("SetOutputBlanking") == []


def test_selected_accepts_a_track_pair_shaped_as_a_table(capsys):
    resolve, parts = session()
    item = Rec("a", GetName="dialog", GetUniqueId="a-1", GetType="audio", GetStart=0, GetEnd=5.0,
               GetTrackTypeAndIndex={1: "audio", 2: 3.0})
    parts["timeline"].set(GetSelectedClips=[item])
    code, out, _ = run(resolve, capsys, "timeline", "selected")
    assert code == 0 and out[0]["track_type"] == "audio" and out[0]["track"] == 3


# --- timeline: analysis and conversions ------------------------------------------------------


def test_scene_cuts_and_convert_stereo(capsys):
    resolve, parts = session()
    items = list(parts["items"])
    parts["timeline"].set(GetItemListInTrack=lambda kind, index: list(items) if (kind, index) == ("video", 1) else [],
                          DetectSceneCuts=lambda: items.append(Rec("cut", GetName="cut")) or True)
    code, out, _ = run(resolve, capsys, "timeline", "scene-cuts")
    assert code == 0 and parts["timeline"].called("DetectSceneCuts") == [()]
    assert out["video_items_before"] == 2 and out["video_items_after"] == 3
    parts["timeline"].set(DetectSceneCuts=False)
    code, _, err = run(resolve, capsys, "timeline", "scene-cuts")
    assert code == 1 and "could not detect scene cuts" in err

    code, _, _ = run(resolve, capsys, "timeline", "convert-stereo")
    assert code == 0 and parts["timeline"].called("ConvertTimelineToStereo") == [()]
    parts["timeline"].set(ConvertTimelineToStereo=False)
    code, _, err = run(resolve, capsys, "timeline", "convert-stereo")
    assert code == 1 and "could not convert" in err


@needs_stub
def test_subtitles_pass_auto_caption_settings(capsys):
    resolve, parts = session()
    constants(resolve, AUTO_CAPTION_GERMAN=7.0, AUTO_CAPTION_NETFLIX=31.0, AUTO_CAPTION_LINE_DOUBLE=41.0)
    parts["timeline"].set(GetTrackCount=lambda kind: 1)
    code, out, _ = run(resolve, capsys, "timeline", "subtitles", "-l", "German", "-p", "netflix",
                       "--chars-per-line", "30", "--line-break", "double", "--gap", "2")
    assert code == 0
    assert parts["timeline"].called("CreateSubtitlesFromAudio") == [
        ({"language": 7.0, "captionPreset": 31.0, "lineBreak": 41.0, "charsPerLine": 30, "gap": 2},)]
    assert out["settings"]["language"] == "AUTO_CAPTION_GERMAN" and out["subtitle_tracks"] == 1

    run(resolve, capsys, "timeline", "subtitles")
    assert parts["timeline"].called("CreateSubtitlesFromAudio")[-1] == ()


@needs_stub
def test_subtitles_failures(capsys):
    resolve, parts = session()
    code, _, err = run(resolve, capsys, "timeline", "subtitles", "-l", "klingon")
    assert code == 1 and "Unknown subtitle language 'klingon'" in err and "mandarin-simplified" in err
    code, _, err = run(resolve, capsys, "timeline", "subtitles", "--chars-per-line", "61")
    assert code == 1 and "1 to 60" in err
    code, _, err = run(resolve, capsys, "timeline", "subtitles", "--gap", "11")
    assert code == 1 and "0 to 10" in err
    assert parts["timeline"].called("CreateSubtitlesFromAudio") == []
    parts["timeline"].set(CreateSubtitlesFromAudio=False)
    code, _, err = run(resolve, capsys, "timeline", "subtitles")
    assert code == 1 and "could not create subtitles" in err


# --- timeline: several items ---------------------------------------------------------------


def test_link_resolves_item_refs_on_the_timeline(capsys):
    resolve, parts = session()
    item1, item2 = parts["items"]
    code, out, _ = run(resolve, capsys, "timeline", "link", "item:video:1:1", "item#item-2")
    assert code == 0
    assert parts["timeline"].called("SetClipsLinked") == [([item1, item2], True)]
    assert out["items"] == [{"ref": "item#item-1", "name": "shot_010"}, {"ref": "item#item-2", "name": "shot_020"}]

    parts["timeline"].set(GetSelectedClips=[item2])
    code, out, _ = run(resolve, capsys, "timeline", "unlink", "--selected")
    assert code == 0 and out["linked"] is False
    assert parts["timeline"].called("SetClipsLinked")[-1] == ([item2], False)


def test_short_item_refs_are_looked_up_on_the_timeline_option(capsys):
    resolve, parts = session()
    other_items = [Rec("o1", GetName="b_010", GetUniqueId="other-1"), Rec("o2", GetName="b_020", GetUniqueId="other-2")]
    other = Rec("other", GetName="B cam", GetUniqueId="tl-2", GetTrackCount=lambda kind: 1 if kind == "video" else 0,
                GetItemListInTrack=lambda kind, index: other_items if (kind, index) == ("video", 1) else [])
    parts["project"].set(GetTimelineCount=2, GetTimelineByIndex=lambda i: [parts["timeline"], other][i - 1])
    code, out, _ = run(resolve, capsys, "timeline", "link", "-t", "B cam", "item:video:1:1", "item#other-2")
    assert code == 0 and out["timeline"] == "B cam"
    assert other.called("SetClipsLinked") == [(other_items, True)]
    assert parts["timeline"].called("SetClipsLinked") == []
    # A full REF names its own timeline.
    code, _, err = run(resolve, capsys, "timeline", "link", "-t", "B cam", "item:video:1:1",
                       "timeline:Edit v1::item:video:1:2")
    assert code == 0
    assert other.called("SetClipsLinked")[-1] == ([other_items[0], parts["items"][1]], True)


def test_link_argument_errors(capsys):
    resolve, parts = session()
    code, _, err = run(resolve, capsys, "timeline", "link", "item:video:1:1")
    assert code == 1 and "at least 2 different items" in err
    code, _, err = run(resolve, capsys, "timeline", "link", "item:video:1:1", "item:video:1:1")
    assert code == 1 and "at least 2 different items" in err
    code, _, err = run(resolve, capsys, "timeline", "link", "item:video:1:1", "--selected")
    assert code == 1 and "Choose the items one way" in err
    code, _, err = run(resolve, capsys, "timeline", "link")
    assert code == 1 and "Choose the items one way" in err
    code, _, err = run(resolve, capsys, "timeline", "link", "item:video:1:1", "clip:a.mov")
    assert code == 1 and "names a MediaPoolItem, not a timeline item" in err
    parts["timeline"].set(GetSelectedClips=[])
    code, _, err = run(resolve, capsys, "timeline", "unlink", "--selected")
    assert code == 1 and "Nothing is selected" in err
    assert parts["timeline"].called("SetClipsLinked") == []

    parts["timeline"].set(SetClipsLinked=False)
    code, _, err = run(resolve, capsys, "timeline", "link", "item:video:1:1", "item:video:1:2")
    assert code == 1 and "could not link item#item-1, item#item-2" in err


@needs_stub
def test_align_builds_auto_align_options(capsys):
    resolve, parts = session()
    constants(resolve, AUTO_ALIGN_CLIPS_USING_WAVEFORM=1.0, AUTO_ALIGN_CLIPS_USING_TIMECODE=2.0,
              AUTO_ALIGN_CLIPS_WAVEFORM_TRACK_MIX=3.0)
    item1, item2 = parts["items"]
    code, _, _ = run(resolve, capsys, "timeline", "align", "item:video:1:1", "item:video:1:2",
                     "--using", "waveform", "--track", "2")
    assert code == 0
    assert parts["timeline"].called("AutoAlignClips") == [([item1, item2], {"SyncUsing": 1.0, "UseTrack": 2})]
    run(resolve, capsys, "timeline", "align", "item:video:1:1", "item:video:1:2", "--using", "waveform",
        "--track", "mix")
    assert parts["timeline"].called("AutoAlignClips")[-1] == ([item1, item2], {"SyncUsing": 1.0, "UseTrack": 3.0})
    run(resolve, capsys, "timeline", "align", "item:video:1:1", "item:video:1:2")
    assert parts["timeline"].called("AutoAlignClips")[-1] == ([item1, item2],)

    code, _, err = run(resolve, capsys, "timeline", "align", "item:video:1:1", "item:video:1:2", "--track", "2")
    assert code == 1 and "--track only applies with --using waveform" in err
    parts["timeline"].set(AutoAlignClips=False)
    code, _, err = run(resolve, capsys, "timeline", "align", "item:video:1:1", "item:video:1:2")
    assert code == 1 and "could not align 2 item(s)" in err
    with pytest.raises(SystemExit) as exc:
        main(["timeline", "align", "item", "--track", "zero"], resolve=resolve)
    assert exc.value.code == 2


def test_compound_and_fusion_clip_return_the_new_item(capsys):
    resolve, parts = session()
    item1, item2 = parts["items"]
    made = Rec("made", GetName="Compound Clip 1", GetUniqueId="item-9")
    parts["timeline"].set(CreateCompoundClip=made, CreateFusionClip=made)
    code, out, _ = run(resolve, capsys, "timeline", "compound", "item:video:1:1", "item:video:1:2", "-n", "Intro",
                       "--start-timecode", "00:00:00:00")
    assert code == 0 and out["$ref"] == "item#item-9" and len(out["items"]) == 2
    assert parts["timeline"].called("CreateCompoundClip") == [
        ([item1, item2], {"name": "Intro", "startTimecode": "00:00:00:00"})]
    run(resolve, capsys, "timeline", "compound", "item:video:1:1")
    assert parts["timeline"].called("CreateCompoundClip")[-1] == ([item1],)

    code, out, _ = run(resolve, capsys, "timeline", "fusion-clip", "item:video:1:2")
    assert code == 0 and out["$ref"] == "item#item-9"
    assert parts["timeline"].called("CreateFusionClip") == [([item2],)]

    parts["timeline"].set(CreateCompoundClip=None, CreateFusionClip=None)
    code, _, err = run(resolve, capsys, "timeline", "compound", "item:video:1:1")
    assert code == 1 and "could not make a compound clip" in err
    code, _, err = run(resolve, capsys, "timeline", "fusion-clip", "item:video:1:1")
    assert code == 1 and "could not make a Fusion clip" in err


def test_delete_clips_passes_ripple(capsys):
    resolve, parts = session()
    item1, item2 = parts["items"]
    parts["timeline"].set(GetSelectedClips=[item1, item2])
    code, out, _ = run(resolve, capsys, "timeline", "delete-clips", "--selected", "--ripple")
    assert code == 0 and out["ripple"] is True and [r["ref"] for r in out["deleted"]] == ["item#item-1", "item#item-2"]
    run(resolve, capsys, "timeline", "delete-clips", "item:video:1:2")
    assert parts["timeline"].called("DeleteClips") == [([item1, item2], True), ([item2], False)]
    parts["timeline"].set(DeleteClips=False)
    code, _, err = run(resolve, capsys, "timeline", "delete-clips", "item:video:1:1")
    assert code == 1 and "could not delete item#item-1" in err


@needs_stub
def test_dolby_vision_on_all_video_items(capsys):
    resolve, parts = session()
    constants(resolve, DLB_BLEND_SHOTS=5.0)
    item1, item2 = parts["items"]
    code, out, _ = run(resolve, capsys, "timeline", "dolby-vision", "--all")
    assert code == 0 and out["analysis"] == "blend-shots"
    assert parts["timeline"].called("AnalyzeDolbyVision") == [([item1, item2], 5.0)]
    parts["timeline"].set(AnalyzeDolbyVision=False)
    code, _, err = run(resolve, capsys, "timeline", "dolby-vision", "item:video:1:1")
    assert code == 1 and "could not run Dolby Vision analysis" in err


def test_selected_lists_items_with_their_tracks(capsys):
    resolve, parts = session()
    parts["timeline"].set(GetSelectedClips=parts["items"][:1])
    code, out, _ = run(resolve, capsys, "--read-only", "timeline", "selected")
    assert code == 0
    assert out == [{"ref": "item#item-1", "name": "shot_010", "type": "video", "track_type": "video", "track": 1,
                    "start": 86400, "end": 86448}]


# --- track ---------------------------------------------------------------------------------


def track_timeline(parts, counts):
    """Tracks with names and states kept in dicts, so add/delete/rename can be observed."""
    timeline = parts["timeline"]
    tracks = {kind: [{"name": f"{kind[0].upper()}{n}", "enabled": True, "locked": False}
                     for n in range(1, count + 1)] for kind, count in counts.items()}

    def add(kind, *sub):
        tracks.setdefault(kind, []).append({"name": "new", "enabled": True, "locked": False, "format": sub[0] if sub
                                            else "stereo"})
        return True

    timeline.set(
        GetTrackCount=lambda kind: len(tracks.get(kind, [])),
        GetTrackName=lambda kind, i: tracks[kind][i - 1]["name"],
        GetTrackSubType=lambda kind, i: tracks[kind][i - 1].get("format", "stereo"),
        GetIsTrackEnabled=lambda kind, i: tracks[kind][i - 1]["enabled"],
        GetIsTrackLocked=lambda kind, i: tracks[kind][i - 1]["locked"],
        SetTrackName=lambda kind, i, name: tracks[kind][i - 1].update(name=name) or True,
        SetTrackEnable=lambda kind, i, on: tracks[kind][i - 1].update(enabled=on) or True,
        SetTrackLock=lambda kind, i, on: tracks[kind][i - 1].update(locked=on) or True,
        AddTrack=add,
        DeleteTrack=lambda kind, i: tracks[kind].pop(i - 1) and True,
        GetItemListInTrack=lambda kind, i: parts["items"] if (kind, i) == ("video", 1) else [],
    )
    return tracks


def test_track_list_shows_every_type(capsys):
    resolve, parts = session()
    track_timeline(parts, {"video": 2, "audio": 1})
    code, out, _ = run(resolve, capsys, "--read-only", "track", "list")
    assert code == 0
    assert out == [
        {"type": "video", "track": 1, "name": "V1", "enabled": True, "locked": False, "items": 2},
        {"type": "video", "track": 2, "name": "V2", "enabled": True, "locked": False, "items": 0},
        {"type": "audio", "track": 1, "name": "A1", "format": "stereo", "enabled": True, "locked": False, "items": 0},
    ]
    code, out, _ = run(resolve, capsys, "track", "list", "-k", "audio")
    assert [row["type"] for row in out] == ["audio"]


def test_track_add_with_format_and_name(capsys):
    resolve, parts = session()
    tracks = track_timeline(parts, {"audio": 1})
    code, out, _ = run(resolve, capsys, "track", "add", "audio", "-f", "5.1", "-n", "Music")
    assert code == 0
    assert parts["timeline"].called("AddTrack") == [("audio", "5.1")]
    assert parts["timeline"].called("SetTrackName") == [("audio", 2, "Music")]
    assert out["track"] == 2 and out["name"] == "Music" and out["format"] == "5.1"
    run(resolve, capsys, "track", "add", "video")
    assert parts["timeline"].called("AddTrack")[-1] == ("video",)
    assert len(tracks["video"]) == 1

    code, _, err = run(resolve, capsys, "track", "add", "video", "-f", "stereo")
    assert code == 1 and "only applies to audio tracks" in err
    assert len(parts["timeline"].called("AddTrack")) == 2


def test_track_add_failures(capsys):
    resolve, parts = session()
    track_timeline(parts, {"audio": 1})
    parts["timeline"].set(AddTrack=False)
    code, _, err = run(resolve, capsys, "track", "add", "audio", "-f", "9.9")
    assert code == 1 and "could not add a 9.9 audio track" in err and "formats: mono, stereo" in err
    parts["timeline"].set(AddTrack=True)  # claims success but the count does not change
    code, _, err = run(resolve, capsys, "track", "add", "audio")
    assert code == 1 and "expected 2" in err
    track_timeline(parts, {"audio": 1})
    parts["timeline"].set(SetTrackName=False)
    code, _, err = run(resolve, capsys, "track", "add", "audio", "-n", "Music")
    assert code == 1 and "could not name it 'Music'" in err


def test_track_delete_and_name(capsys):
    resolve, parts = session()
    tracks = track_timeline(parts, {"video": 2})
    code, out, _ = run(resolve, capsys, "track", "delete", "video", "2")
    assert code == 0 and out["deleted"] == 2 and out["name"] == "V2" and out["tracks_left"] == 1
    assert parts["timeline"].called("DeleteTrack") == [("video", 2)]
    code, _, err = run(resolve, capsys, "track", "delete", "video", "2")
    assert code == 1 and "has 1 video track(s); there is no video track 2" in err
    assert len(parts["timeline"].called("DeleteTrack")) == 1
    parts["timeline"].set(DeleteTrack=False)
    code, _, err = run(resolve, capsys, "track", "delete", "video", "1")
    assert code == 1 and "could not delete video track 1" in err

    code, out, _ = run(resolve, capsys, "--read-only", "track", "name", "video", "1")
    assert code == 0 and out == {"type": "video", "track": 1, "name": "V1"}
    code, out, _ = run(resolve, capsys, "track", "name", "video", "1", "Graphics")
    assert out["name"] == "Graphics" and tracks["video"][0]["name"] == "Graphics"
    code, _, err = run(resolve, capsys, "--read-only", "track", "name", "video", "1", "X")
    assert code == 1 and "read-only" in err
    parts["timeline"].set(SetTrackName=False)
    code, _, err = run(resolve, capsys, "track", "name", "video", "1", "X")
    assert code == 1 and "could not name video track 1" in err
    with pytest.raises(SystemExit) as exc:
        main(["track", "name", "video", "0"], resolve=resolve)
    assert exc.value.code == 2


def test_track_enable_disable_lock_unlock(capsys):
    resolve, parts = session()
    tracks = track_timeline(parts, {"audio": 3})
    code, out, _ = run(resolve, capsys, "track", "disable", "audio", "1", "3")
    assert code == 0 and [row["enabled"] for row in out] == [False, False]
    assert parts["timeline"].called("SetTrackEnable") == [("audio", 1, False), ("audio", 3, False)]
    run(resolve, capsys, "track", "enable", "audio", "1")
    assert parts["timeline"].called("SetTrackEnable")[-1] == ("audio", 1, True)
    run(resolve, capsys, "track", "lock", "audio", "2")
    run(resolve, capsys, "track", "unlock", "audio", "2")
    assert parts["timeline"].called("SetTrackLock") == [("audio", 2, True), ("audio", 2, False)]
    assert [t["enabled"] for t in tracks["audio"]] == [True, True, False]

    code, _, err = run(resolve, capsys, "track", "lock", "audio", "1", "4")
    assert code == 1 and "no audio track 4" in err
    assert len(parts["timeline"].called("SetTrackLock")) == 2  # checked before changing anything
    parts["timeline"].set(SetTrackLock=lambda kind, i, on: i != 2)
    code, _, err = run(resolve, capsys, "track", "lock", "audio", "1", "2")
    assert code == 1 and "could not lock audio track 2" in err and "Tracks changed before it: 1" in err


# --- playhead -------------------------------------------------------------------------------


def test_playhead_get_and_item(capsys):
    resolve, parts = session()
    item1 = parts["items"][0]
    parts["timeline"].set(GetCurrentTimecode="01:00:01:00", GetCurrentVideoItem=item1)
    code, out, _ = run(resolve, capsys, "--read-only", "playhead", "get")
    assert code == 0 and out == {"timeline": "Edit v1", "timecode": "01:00:01:00", "start_timecode": "01:00:00:00",
                                 "video_item": "item#item-1", "video_item_name": "shot_010"}
    code, out, _ = run(resolve, capsys, "--read-only", "playhead", "item")
    assert code == 0 and out["$ref"] == "item#item-1" and out["start"] == 86400 and out["duration"] == 48

    parts["timeline"].set(GetCurrentVideoItem=None)
    code, out, _ = run(resolve, capsys, "playhead", "get")
    assert code == 0 and out["video_item"] is None
    code, _, err = run(resolve, capsys, "playhead", "item")
    assert code == 1 and "No video clip under the playhead (01:00:01:00)" in err


def test_playhead_set(capsys):
    resolve, parts = session()
    parts["timeline"].set(GetCurrentTimecode="01:00:05:00")
    code, out, _ = run(resolve, capsys, "playhead", "set", "01:00:05:00")
    assert code == 0 and out == {"timeline": "Edit v1", "timecode": "01:00:05:00"}
    assert parts["timeline"].called("SetCurrentTimecode") == [("01:00:05:00",)]
    assert parts["project"].called("SetCurrentTimeline") == []
    run(resolve, capsys, "playhead", "set", "01:00:06:00", "-t", "Edit v1")
    assert parts["project"].called("SetCurrentTimeline") == [(parts["timeline"],)]

    parts["timeline"].set(SetCurrentTimecode=False)
    code, _, err = run(resolve, capsys, "playhead", "set", "99")
    assert code == 1 and "rejected timecode '99'" in err and "starts at 01:00:00:00" in err
    code, _, err = run(resolve, capsys, "--read-only", "playhead", "set", "01:00:00:00")
    assert code == 1 and "read-only" in err


# --- marker ---------------------------------------------------------------------------------


def test_marker_show_on_timeline_item_and_clip(capsys):
    resolve, parts = session()
    marker_host(parts["timeline"], two_markers())
    marker_host(parts["items"][1], {12.0: {"color": "Green", "duration": 1, "note": "", "name": "i",
                                           "customData": ""}})
    marker_host(parts["clips"][0], {3: {"color": "Red", "duration": 1, "note": "", "name": "c", "customData": "x"}})
    code, out, _ = run(resolve, capsys, "--read-only", "marker", "show")
    assert code == 0 and [row["frame"] for row in out] == [10, 40]
    code, out, _ = run(resolve, capsys, "marker", "show", "--color", "blue")
    assert [row["name"] for row in out] == ["b"]
    code, out, _ = run(resolve, capsys, "marker", "show", "--on", "item:video:1:2")
    assert out == [{"frame": 12, "color": "Green", "duration": 1, "note": "", "name": "i", "customData": ""}]
    code, out, _ = run(resolve, capsys, "marker", "show", "--on", "clip:a.mov")
    assert out[0]["customData"] == "x"

    code, _, err = run(resolve, capsys, "marker", "show", "--on", "project")
    assert code == 1 and "names a Project" in err
    parts["timeline"].set(GetMarkers=None)
    code, _, err = run(resolve, capsys, "marker", "show")
    assert code == 1 and "no marker list" in err


def test_marker_create_with_custom_data(capsys):
    resolve, parts = session()
    clip = parts["clips"][0]
    code, out, _ = run(resolve, capsys, "marker", "create", "24", "--on", "clip:a.mov", "-c", "red", "-n", "Take",
                       "--data", "id-7")
    assert code == 0 and out["on"] == "clip#clip-1" and out["customData"] == "id-7" and out["color"] == "Red"
    assert clip.called("AddMarker") == [(24, "Red", "Take", "", 1, "id-7")]
    run(resolve, capsys, "marker", "create", "5")
    assert parts["timeline"].called("AddMarker") == [(5, "Blue", "", "", 1)]

    parts["timeline"].set(AddMarker=False)
    code, _, err = run(resolve, capsys, "marker", "create", "5")
    assert code == 1 and "could not add a marker at frame 5" in err
    with pytest.raises(SystemExit) as exc:
        main(["marker", "create", "5", "-c", "Magenta"], resolve=resolve)
    assert exc.value.code == 2


def test_marker_delete_by_frame(capsys):
    resolve, parts = session()
    markers = marker_host(parts["timeline"], two_markers())
    code, out, _ = run(resolve, capsys, "marker", "delete", "-f", "10")
    assert code == 0 and out == {"on": "timeline#tl-1", "deleted": [10]}
    assert parts["timeline"].called("DeleteMarkerAtFrame") == [(10,)] and list(markers) == [40]

    code, _, err = run(resolve, capsys, "marker", "delete", "-f", "11")
    assert code == 1 and "No marker at frame(s) 11" in err and "markers are at: 40" in err
    assert len(parts["timeline"].called("DeleteMarkerAtFrame")) == 1
    parts["timeline"].set(DeleteMarkerAtFrame=False)
    code, _, err = run(resolve, capsys, "marker", "delete", "-f", "40")
    assert code == 1 and "could not delete the marker at frame 40" in err


def test_marker_delete_by_color_all_and_custom_data(capsys):
    resolve, parts = session()
    item = parts["items"][0]
    markers = marker_host(item, two_markers())
    code, out, _ = run(resolve, capsys, "marker", "delete", "--on", "item#item-1", "-c", "Cream")
    assert code == 0 and out["deleted"] == [] and item.called("DeleteMarkersByColor") == []
    code, out, _ = run(resolve, capsys, "marker", "delete", "--on", "item#item-1", "--custom-data", "id-2")
    assert code == 0 and out["deleted"] == [40] and item.called("DeleteMarkerByCustomData") == [("id-2",)]
    code, _, err = run(resolve, capsys, "marker", "delete", "--on", "item#item-1", "--custom-data", "zzz")
    assert code == 1 and "has custom data 'zzz'" in err
    code, out, _ = run(resolve, capsys, "marker", "delete", "--on", "item#item-1", "--all")
    assert code == 0 and out["deleted"] == [10] and item.called("DeleteMarkersByColor") == [("All",)]
    assert markers == {}

    marker_host(item, two_markers())
    run(resolve, capsys, "marker", "delete", "--on", "item#item-1", "-c", "red")
    assert item.called("DeleteMarkersByColor")[-1] == ("Red",)
    marker_host(item, two_markers())
    item.set(DeleteMarkersByColor=False, DeleteMarkerByCustomData=False)
    code, _, err = run(resolve, capsys, "marker", "delete", "--on", "item#item-1", "-c", "Blue")
    assert code == 1 and "could not delete the Blue markers" in err
    code, _, err = run(resolve, capsys, "marker", "delete", "--on", "item#item-1", "--custom-data", "id-1")
    assert code == 1 and "could not delete the marker with custom data 'id-1'" in err
    with pytest.raises(SystemExit) as exc:
        main(["marker", "delete"], resolve=resolve)
    assert exc.value.code == 2


def test_marker_find_and_data(capsys):
    resolve, parts = session()
    clip = parts["clips"][0]
    markers = marker_host(clip, two_markers())
    code, out, _ = run(resolve, capsys, "--read-only", "marker", "find", "id-2", "--on", "clip:a.mov")
    assert code == 0 and out["frames"] == [40] and out["name"] == "b"
    assert clip.called("GetMarkerByCustomData") == [("id-2",)]
    code, _, err = run(resolve, capsys, "marker", "find", "none", "--on", "clip:a.mov")
    assert code == 1 and "No marker on clip 'a.mov' has custom data 'none'" in err

    code, out, _ = run(resolve, capsys, "--read-only", "marker", "data", "10", "--on", "clip:a.mov")
    assert code == 0 and out == {"on": "clip#clip-1", "frame": 10, "customData": "id-1"}
    code, _, err = run(resolve, capsys, "--read-only", "marker", "data", "10", "new", "--on", "clip:a.mov")
    assert code == 1 and "read-only" in err
    code, out, _ = run(resolve, capsys, "marker", "data", "10", "new", "--on", "clip:a.mov")
    assert code == 0 and out["customData"] == "new" and markers[10]["customData"] == "new"
    assert clip.called("UpdateMarkerCustomData") == [(10, "new")]
    code, _, err = run(resolve, capsys, "marker", "data", "11", "--on", "clip:a.mov")
    assert code == 1 and "No marker at frame 11" in err
    clip.set(UpdateMarkerCustomData=False)
    code, _, err = run(resolve, capsys, "marker", "data", "40", "x", "--on", "clip:a.mov")
    assert code == 1 and "could not set the custom data" in err
