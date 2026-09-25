"""Tests for dava audio (tracks, normalize, voice isolation, channel mapping, clip properties, insert, speech)
and dava fairlight (presets)."""

import io
import json
import os

import pytest

from dava import connect
from dava.cli import build_parser, main
from dava.commands import audio
from dava.registry import leaf_parsers
from recorder import Rec, rec_resolve

HAS_STUB = os.path.isfile(connect.stub_path() or "")
needs_stub = pytest.mark.skipif(not HAS_STUB, reason="Resolve scripting API definition not installed")

# resolve.* constants are plain values on the real object; give them distinct numbers here.
CONSTANTS = {
    "NORMALIZE_AUDIO_SET_LEVEL_RELATIVE": 0, "NORMALIZE_AUDIO_SET_LEVEL_INDEPENDENT": 1,
    "DIALOGUE_LEVELER_MODE_ALLOW_WIDER_DYNAMICS": 10, "DIALOGUE_LEVELER_MODE_OPTIMIZE_MODERATE_LEVELS": 11,
    "DIALOGUE_LEVELER_MODE_MORE_LIFT_FOR_LOW_LEVELS": 12, "DIALOGUE_LEVELER_MODE_LIFT_SOFT_WHISPERY_SOURCES": 13,
}
MODES = ["Sample Peak Program", "True Peak Program", "EBU R128"]
STEREO = {"track_mapping": {"1": {"channel_idx": [1, 2], "mute": False, "type": "Stereo"}}}


def clip(number, name=None, **returns):
    """A recorded timeline item on an audio track."""
    defaults = dict(GetName=name or f"dialog_{number:02d}", GetUniqueId=f"audio-{number}",
                    GetVoiceIsolationState={"isEnabled": number % 2 == 0, "amount": 10 * number},
                    GetSourceAudioChannelMapping=json.dumps(STEREO),
                    GetProperties={"Opacity": 100.0, "AudioVolume": -3.0, "AudioPan": 0.0,
                                   "AudioDialogueLevelerMode": 12})
    defaults.update(returns)
    return Rec(f"audio{number}", **defaults)


def session(tracks=None, clips=None):
    """A recorded Resolve whose current timeline 'Edit v1' has audio tracks {number: [items]}.

    Default: audio track 1 (stereo) holds two clips, audio track 2 (5.1) is empty. The track getters
    answer differently for the video track, so a handler that asks about the wrong track type is caught.
    """
    resolve, parts = rec_resolve(clips=clips)
    for name, value in CONSTANTS.items():
        setattr(resolve, name, value)
    tracks = tracks if tracks is not None else {1: [clip(1), clip(2)], 2: []}
    formats = {number: "stereo" if number == 1 else "5.1" for number in tracks}
    video = parts["items"]
    parts["timeline"].set(
        GetTrackCount=lambda kind: len(tracks) if kind == "audio" else 1,
        GetItemListInTrack=lambda kind, index: tracks.get(index, []) if kind == "audio" else (
            video if index == 1 else []),
        GetTrackName=lambda kind, index: f"{kind.capitalize()} {index}",
        GetTrackSubType=lambda kind, index: formats.get(index) if kind == "audio" else "",
        GetIsTrackEnabled=lambda kind, index: kind == "audio" and index == 1,
        GetIsTrackLocked=lambda kind, index: kind == "audio" and index == 2,
        GetVoiceIsolationState=lambda index: {"isEnabled": index == 2, "amount": 20 + 10 * index},
        GetNormalizeAudioModes=MODES, GetCurrentTimecode="01:00:10:00")
    parts["tracks"], parts["formats"] = tracks, formats
    return resolve, parts


def grow_on_add_track(parts):
    """Make AddTrack('audio'[, format]) append an empty audio track of that format, as Resolve does."""
    tracks, formats = parts["tracks"], parts["formats"]

    def add_track(kind, *subtype):
        if kind == "audio":
            tracks[len(tracks) + 1] = []
            formats[len(tracks)] = subtype[0] if subtype else "mono"
        return True

    parts["timeline"].set(AddTrack=add_track)


def run(resolve, capsys, *argv):
    code = main(["--json", *argv], resolve=resolve)
    captured = capsys.readouterr()
    out = json.loads(captured.out) if captured.out.strip() else None
    return code, out, captured.err


def names(rec):
    return [name for name, _ in rec.calls]


def logging_to(log, label, value=True):
    """A return value for Rec that records the call order across objects in `log`."""
    def method(*args):
        log.append((label, args))
        return value
    return method


# --- registration --------------------------------------------------------------------


READ_ONLY = {
    ("audio", "tracks"): True, ("audio", "add-track"): False, ("audio", "modes"): True,
    ("audio", "normalize"): False, ("audio", "isolation"): True, ("audio", "set-isolation"): False,
    ("audio", "mapping"): True, ("audio", "set-mapping"): False, ("audio", "props"): True,
    ("audio", "set"): False, ("audio", "insert"): False, ("audio", "speech"): False,
    ("fairlight", "presets"): True, ("fairlight", "apply"): False,
}


def test_every_command_declares_handler_read_only_and_ascii_help():
    leaves = {words: (leaf, help_text) for words, leaf, help_text in leaf_parsers(build_parser())
              if words[0] in ("audio", "fairlight")}
    assert set(leaves) == set(READ_ONLY)
    for words, (leaf, help_text) in leaves.items():
        assert leaf._defaults["func"].__module__ == audio.__name__, words
        assert leaf._defaults["read_only"] is READ_ONLY[words], words
        texts = [help_text] + [action.help or "" for action in leaf._actions]
        assert all(text and text.isascii() for text in texts), words


def test_read_only_mode_refuses_changes_but_allows_reads(capsys):
    resolve, parts = session()
    code, _, err = run(resolve, capsys, "--read-only", "audio", "set-isolation", "--on")
    assert code == 1 and "read-only mode is on" in err
    assert parts["timeline"].called("SetVoiceIsolationState") == []
    code, out, _ = run(resolve, capsys, "--read-only", "audio", "modes")
    assert code == 0 and out == MODES


# --- tracks and add-track ---------------------------------------------------------------


def test_tracks_lists_every_audio_track(capsys):
    resolve, parts = session()
    code, out, _ = run(resolve, capsys, "audio", "tracks")
    assert code == 0
    assert out == [
        {"track": 1, "name": "Audio 1", "format": "stereo", "enabled": True, "locked": False, "clips": 2,
         "voice_isolation": False, "isolation_amount": 30},
        {"track": 2, "name": "Audio 2", "format": "5.1", "enabled": False, "locked": True, "clips": 0,
         "voice_isolation": True, "isolation_amount": 40},
    ]
    assert parts["timeline"].called("GetVoiceIsolationState") == [(1,), (2,)]
    assert ("audio", 2) in parts["timeline"].called("GetTrackSubType")


def test_tracks_leave_isolation_empty_when_resolve_reports_none(capsys):
    resolve, parts = session()
    parts["timeline"].set(GetVoiceIsolationState=None)
    code, out, _ = run(resolve, capsys, "audio", "tracks")
    assert code == 0
    assert [(row["voice_isolation"], row["isolation_amount"]) for row in out] == [(None, None), (None, None)]


def test_add_track_passes_format_and_names_the_new_track(capsys):
    resolve, parts = session()
    grow_on_add_track(parts)
    code, out, _ = run(resolve, capsys, "audio", "add-track", "--format", "7.1", "--name", "VO")
    assert code == 0
    assert parts["timeline"].called("AddTrack") == [("audio", "7.1")]
    assert parts["timeline"].called("SetTrackName") == [("audio", 3, "VO")]
    assert out == {"timeline": "Edit v1", "track": 3, "name": "Audio 3", "format": "7.1"}


def test_add_track_without_format_passes_only_the_track_type(capsys):
    resolve, parts = session()
    grow_on_add_track(parts)
    code, out, _ = run(resolve, capsys, "audio", "add-track")
    assert code == 0
    assert out == {"timeline": "Edit v1", "track": 3, "name": "Audio 3", "format": "mono"}
    assert parts["timeline"].called("AddTrack") == [("audio",)]
    assert parts["timeline"].called("SetTrackName") == []


def test_add_track_fails_when_resolve_refuses_and_lists_known_formats(capsys):
    resolve, parts = session()
    parts["timeline"].set(AddTrack=False)
    code, _, err = run(resolve, capsys, "audio", "add-track", "-f", "9.1", "-n", "VO")
    assert code == 1
    assert "could not add an audio track of format '9.1'" in err and "5.1, stereo" in err
    assert "Give the track format with -f" not in err
    assert parts["timeline"].called("SetTrackName") == []
    code, _, err = run(resolve, capsys, "audio", "add-track")
    assert code == 1
    assert "could not add an audio track to 'Edit v1'" in err and "Give the track format with -f" in err


def test_add_track_fails_when_the_track_count_does_not_grow(capsys):
    resolve, parts = session()  # AddTrack returns True but adds nothing
    code, _, err = run(resolve, capsys, "audio", "add-track")
    assert code == 1 and "still has 2 audio track(s)" in err


def test_add_track_fails_when_naming_is_refused(capsys):
    resolve, parts = session()
    grow_on_add_track(parts)
    parts["timeline"].set(SetTrackName=False)
    code, _, err = run(resolve, capsys, "audio", "add-track", "-n", "VO")
    assert code == 1 and "Added audio track 3" in err and "'VO'" in err


# --- normalize ----------------------------------------------------------------------------


def test_modes_list_and_empty_list_is_an_error(capsys):
    resolve, parts = session()
    code, out, _ = run(resolve, capsys, "audio", "modes")
    assert code == 0 and out == MODES
    parts["timeline"].set(GetNormalizeAudioModes=[])
    code, _, err = run(resolve, capsys, "audio", "modes")
    assert code == 1 and "no audio normalization modes" in err


def test_normalize_without_options_passes_only_the_clips(capsys):
    resolve, parts = session()
    code, out, _ = run(resolve, capsys, "audio", "normalize")
    assert code == 0
    assert parts["timeline"].called("NormalizeAudioLevel") == [(parts["tracks"][1],)]
    assert out == {"timeline": "Edit v1", "options": {}, "clips": [
        {"index": 1, "name": "dialog_01", "track": 1}, {"index": 2, "name": "dialog_02", "track": 1}]}


def test_normalize_builds_options_with_mode_level_and_set_level_constant(capsys):
    resolve, parts = session()
    code, out, _ = run(resolve, capsys, "audio", "normalize", "-i", "2", "-m", "sample peak program",
                       "--level", "-9", "--set-level", "independent")
    assert code == 0
    ((items, options),) = parts["timeline"].called("NormalizeAudioLevel")
    assert items == [parts["tracks"][1][1]]
    assert options == {"normalizationMode": "Sample Peak Program", "targetLevel": -9.0, "setLevelMode": 1}
    assert isinstance(options["targetLevel"], float)
    assert out["options"]["setLevelMode"] == "NORMALIZE_AUDIO_SET_LEVEL_INDEPENDENT"
    assert out["clips"] == [{"index": 2, "name": "dialog_02", "track": 1}]


def test_normalize_loudness_target(capsys):
    resolve, parts = session()
    code, _, _ = run(resolve, capsys, "audio", "normalize", "-m", "EBU R128", "--loudness", "-23")
    assert code == 0
    assert parts["timeline"].called("NormalizeAudioLevel")[0][1] == {
        "normalizationMode": "EBU R128", "targetLoudness": -23.0}


def test_normalize_all_tracks_skips_empty_tracks(capsys):
    a1, a3 = clip(1), clip(3)
    resolve, parts = session(tracks={1: [a1], 2: [], 3: [a3]})
    code, out, _ = run(resolve, capsys, "audio", "normalize", "--all-tracks", "--set-level", "relative")
    assert code == 0
    assert parts["timeline"].called("NormalizeAudioLevel") == [([a1, a3], {"setLevelMode": 0})]
    assert [(row["track"], row["index"]) for row in out["clips"]] == [(1, 1), (3, 1)]


@pytest.mark.parametrize("argv, message", [
    (["-m", "Loudest"], "Unknown normalization mode 'Loudest'. Modes: Sample Peak Program, True Peak Program"),
    (["--all-tracks", "-i", "1"], "cannot be combined with --all-tracks"),
    (["-T", "5"], "Audio track 5 does not exist; 'Edit v1' has 2 audio track(s)"),
    (["-T", "2"], "Audio track 2 of 'Edit v1' has no clips"),
    (["-i", "3"], "Clip index 3 is out of range; audio track 1 has 2 clip(s)"),
])
def test_normalize_refuses_bad_selection_before_calling_resolve(capsys, argv, message):
    resolve, parts = session()
    code, _, err = run(resolve, capsys, "audio", "normalize", *argv)
    assert code == 1 and message in err
    assert parts["timeline"].called("NormalizeAudioLevel") == []


def test_normalize_all_tracks_without_clips_is_an_error(capsys):
    resolve, parts = session(tracks={1: [], 2: []})
    code, _, err = run(resolve, capsys, "audio", "normalize", "--all-tracks")
    assert code == 1 and "has no clips on its audio tracks" in err


def test_normalize_track_and_all_tracks_is_a_usage_error():
    resolve, _ = session()
    with pytest.raises(SystemExit) as exc:
        main(["audio", "normalize", "-T", "1", "--all-tracks"], resolve=resolve)
    assert exc.value.code == 2


def test_normalize_fails_when_resolve_returns_false(capsys):
    resolve, parts = session()
    parts["timeline"].set(NormalizeAudioLevel=False)
    code, _, err = run(resolve, capsys, "audio", "normalize", "--level", "-9")
    assert code == 1 and "could not normalize 2 audio clip(s)" in err


# --- voice isolation -----------------------------------------------------------------------


def test_isolation_shows_track_and_clip_states(capsys):
    resolve, parts = session()
    code, out, _ = run(resolve, capsys, "audio", "isolation")
    assert code == 0
    assert out == {"timeline": "Edit v1", "track": 1, "state": {"isEnabled": False, "amount": 30}, "clips": [
        {"index": 1, "name": "dialog_01", "state": {"isEnabled": False, "amount": 10}},
        {"index": 2, "name": "dialog_02", "state": {"isEnabled": True, "amount": 20}}]}


def test_isolation_of_one_clip_skips_the_track_state(capsys):
    resolve, parts = session()
    code, out, _ = run(resolve, capsys, "audio", "isolation", "-i", "2")
    assert code == 0 and "state" not in out
    assert out["clips"] == [{"index": 2, "name": "dialog_02", "state": {"isEnabled": True, "amount": 20}}]
    assert parts["timeline"].called("GetVoiceIsolationState") == []


def test_isolation_without_state_is_an_error_with_a_studio_hint(capsys):
    resolve, parts = session()
    parts["timeline"].set(GetVoiceIsolationState=None)
    code, _, err = run(resolve, capsys, "audio", "isolation", "-T", "2")
    assert code == 1 and "no voice isolation state for audio track 2" in err and "Studio" in err


def test_set_isolation_on_a_track_keeps_the_current_amount(capsys):
    resolve, parts = session()
    code, out, _ = run(resolve, capsys, "audio", "set-isolation", "--on")
    assert code == 0
    assert parts["timeline"].called("SetVoiceIsolationState") == [(1, {"isEnabled": True, "amount": 30})]
    assert parts["project"].called("SetCurrentTimeline") == []
    assert out == {"timeline": "Edit v1", "track": 1, "state": {"isEnabled": True, "amount": 30}}


def test_set_isolation_on_a_clip_makes_the_timeline_current_first(capsys):
    resolve, parts = session()
    log = []
    item = parts["tracks"][1][1]
    parts["project"].set(SetCurrentTimeline=logging_to(log, "current"))
    item.set(SetVoiceIsolationState=logging_to(log, "set"))
    code, out, _ = run(resolve, capsys, "audio", "set-isolation", "-t", "Edit v1", "-i", "2", "--amount", "80")
    assert code == 0
    assert log == [("current", (parts["timeline"],)), ("set", ({"isEnabled": True, "amount": 80},))]
    assert out == {"timeline": "Edit v1", "track": 1, "index": 2, "name": "dialog_02",
                   "state": {"isEnabled": True, "amount": 80}}


@pytest.mark.parametrize("argv, message", [
    ([], "Nothing to set"),
    (["--amount", "101"], "--amount must be 0 to 100, got 101"),
    (["--amount", "-1"], "--amount must be 0 to 100, got -1"),
])
def test_set_isolation_checks_options_before_calling_resolve(capsys, argv, message):
    resolve, parts = session()
    code, _, err = run(resolve, capsys, "audio", "set-isolation", *argv)
    assert code == 1 and message in err
    assert "SetVoiceIsolationState" not in names(parts["timeline"])


def test_set_isolation_on_and_off_is_a_usage_error():
    resolve, _ = session()
    with pytest.raises(SystemExit) as exc:
        main(["audio", "set-isolation", "--on", "--off"], resolve=resolve)
    assert exc.value.code == 2


def test_set_isolation_fails_when_resolve_rejects_it(capsys):
    resolve, parts = session()
    parts["timeline"].set(SetVoiceIsolationState=False)
    code, _, err = run(resolve, capsys, "audio", "set-isolation", "--off")
    assert code == 1 and "rejected voice isolation" in err and "audio track 1" in err
    parts["tracks"][1][0].set(SetVoiceIsolationState=False)
    code, _, err = run(resolve, capsys, "audio", "set-isolation", "--off", "-i", "1")
    assert code == 1 and "clip 1 ('dialog_01')" in err


def test_set_isolation_on_a_named_timeline_track_keeps_that_tracks_amount(capsys):
    resolve, parts = session()
    code, out, _ = run(resolve, capsys, "audio", "set-isolation", "-t", "Edit v1", "-T", "2", "--off")
    assert code == 0
    assert parts["timeline"].called("GetVoiceIsolationState") == [(2,)]
    assert parts["timeline"].called("SetVoiceIsolationState") == [(2, {"isEnabled": False, "amount": 40})]
    assert parts["project"].called("SetCurrentTimeline") == []  # a track setting needs no timeline switch
    assert out == {"timeline": "Edit v1", "track": 2, "state": {"isEnabled": False, "amount": 40}}


def test_set_isolation_without_a_readable_state_needs_both_values(capsys):
    resolve, parts = session()
    item = parts["tracks"][1][0]
    parts["timeline"].set(GetVoiceIsolationState=None)
    item.set(GetVoiceIsolationState={})
    code, _, err = run(resolve, capsys, "audio", "set-isolation", "--amount", "50")
    assert code == 1 and "no voice isolation state for audio track 1" in err and "give both --on or --off" in err
    code, _, err = run(resolve, capsys, "audio", "set-isolation", "-i", "1", "--off")
    assert code == 1 and "no voice isolation state for clip 1 ('dialog_01')" in err
    assert parts["timeline"].called("SetVoiceIsolationState") == [] and item.called("SetVoiceIsolationState") == []
    code, out, _ = run(resolve, capsys, "audio", "set-isolation", "--on", "--amount", "50")
    assert code == 0 and out["state"] == {"isEnabled": True, "amount": 50}
    assert parts["timeline"].called("SetVoiceIsolationState") == [(1, {"isEnabled": True, "amount": 50})]


def test_set_isolation_bad_clip_index_leaves_the_current_timeline_alone(capsys):
    resolve, parts = session()
    code, _, err = run(resolve, capsys, "audio", "set-isolation", "-t", "Edit v1", "-i", "9", "--on")
    assert code == 1 and "Clip index 9 is out of range; audio track 1 has 2 clip(s)" in err
    assert parts["project"].called("SetCurrentTimeline") == []


# --- channel mapping ------------------------------------------------------------------------


def media_clip(mapping=json.dumps(STEREO), **returns):
    return Rec("mpclip", GetName="interview.mov", GetUniqueId="clip-9", GetAudioMapping=mapping, **returns)


def test_mapping_of_a_media_pool_clip(capsys):
    resolve, parts = session(clips=[media_clip()])
    code, out, _ = run(resolve, capsys, "audio", "mapping", "--clip", "interview.mov")
    assert code == 0 and out == {"clip": "interview.mov", "mapping": STEREO}


def test_mapping_of_timeline_clips(capsys):
    resolve, parts = session()
    code, out, _ = run(resolve, capsys, "audio", "mapping")
    assert code == 0
    assert out == {"timeline": "Edit v1", "track": 1, "clips": [
        {"index": 1, "name": "dialog_01", "mapping": STEREO}, {"index": 2, "name": "dialog_02", "mapping": STEREO}]}


@pytest.mark.parametrize("value, message", [("", "no audio mapping"), ("not json", "that is not JSON")])
def test_mapping_fails_on_empty_or_broken_json_from_resolve(capsys, value, message):
    resolve, parts = session(clips=[media_clip(mapping=value)])
    code, _, err = run(resolve, capsys, "audio", "mapping", "-c", "interview.mov")
    assert code == 1 and message in err


def test_mapping_refuses_clip_and_timeline_selection_together(capsys):
    resolve, parts = session(clips=[media_clip()])
    code, _, err = run(resolve, capsys, "audio", "mapping", "-c", "interview.mov", "-i", "1")
    assert code == 1 and "do not combine it with -t/-i" in err


def test_set_mapping_inline_json_on_a_timeline_clip(capsys):
    resolve, parts = session()
    item = parts["tracks"][1][0]
    mapping = {"track_mapping": {"1": {"type": "mono", "channel_idx": [2]}}}
    code, out, _ = run(resolve, capsys, "audio", "set-mapping", json.dumps(mapping), "-i", "1")
    assert code == 0
    ((text,),) = item.called("SetSourceAudioChannelMapping")
    assert isinstance(text, str) and json.loads(text) == mapping
    assert out == {"timeline": "Edit v1", "track": 1, "index": 1, "name": "dialog_01",
                   "track_mapping": mapping["track_mapping"]}


def test_set_mapping_from_a_file_reuses_getter_output_for_a_media_pool_clip(capsys, tmp_path):
    clip_rec = media_clip()
    resolve, parts = session(clips=[clip_rec])
    mapping = {"embedded_audio_channels": 2, "linked_audio": {"1": {"channels": 6, "offset": 0, "path": "/x.wav"}},
               "track_mapping": {"1": {"channel_idx": [1, 3], "mute": True, "type": "Stereo"},
                                 "2": {"channel_idx": [3, 4, 5, 6, 7, 8], "mute": False, "type": "5.1"},
                                 "3": {"channel_idx": [0], "type": "adaptive_1"}}}
    path = tmp_path / "map.json"
    path.write_text(json.dumps(mapping), encoding="utf-8")
    code, out, _ = run(resolve, capsys, "audio", "set-mapping", str(path), "--clip", "interview.mov")
    assert code == 0
    ((text,),) = clip_rec.called("SetAudioMapping")
    assert json.loads(text) == mapping
    assert out == {"clip": "interview.mov", "track_mapping": mapping["track_mapping"]}


def test_set_mapping_reads_standard_input(capsys, monkeypatch):
    resolve, parts = session()
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(STEREO)))
    code, _, _ = run(resolve, capsys, "audio", "set-mapping", "-", "-i", "2")
    assert code == 0
    assert json.loads(parts["tracks"][1][1].called("SetSourceAudioChannelMapping")[0][0]) == STEREO


@pytest.mark.parametrize("mapping, message", [
    ({"track_mapping": {"1": {"type": "stereo", "channel_idx": [1]}}},
     "type 'stereo' has 2 channel(s) but channel_idx lists 1"),
    ({"track_mapping": {"1": {"type": "5.1", "channel_idx": [1, 2, 3, 4, 5, 6, 7, 8]}}},
     "type '5.1' has 6 channel(s) but channel_idx lists 8"),
    ({"track_mapping": {"1": {"type": "surround", "channel_idx": [1]}}}, "unknown type 'surround'"),
    ({"track_mapping": {"1": {"type": "adaptive_37", "channel_idx": [1]}}}, "unknown type 'adaptive_37'"),
    ({"tracks": {}}, 'needs a non-empty "track_mapping" object'),
    ({"track_mapping": {}}, 'needs a non-empty "track_mapping" object'),
    ({"track_mapping": {"0": {"type": "mono", "channel_idx": [1]}}}, "track keys are track numbers from 1"),
    ({"track_mapping": {"1": {"type": "mono", "channel_idx": [-1]}}}, "channel_idx must be a list"),
    ({"track_mapping": {"1": {"type": "mono", "channel_idx": [True]}}}, "channel_idx must be a list"),
    ({"track_mapping": {"1": {"type": "mono", "channel_idx": [1], "mute": "yes"}}}, "mute must be true or false"),
    ({"track_mapping": {"1": {"type": "mono", "channel_idx": [1], "gain": 2}}}, "unknown key(s) gain"),
    ({"track_mapping": {"1": {"type": "mono"}}}, "missing channel_idx"),
    ({"track_mapping": {"1": {"type": "mono", "channel_idx": [1]}, "2": {"type": "mono", "channel_idx": [2]}}},
     "takes exactly 1 track in track_mapping, got 2"),
])
def test_set_mapping_validates_before_calling_resolve(capsys, mapping, message):
    resolve, parts = session()
    code, _, err = run(resolve, capsys, "audio", "set-mapping", json.dumps(mapping), "-i", "1")
    assert code == 1 and message in err
    assert "SetSourceAudioChannelMapping" not in names(parts["tracks"][1][0])


@pytest.mark.parametrize("source, message", [
    ('{"track_mapping": ', "Invalid JSON in the inline mapping"),
    ("/no/such/map.json", "Audio mapping file not found: /no/such/map.json"),
])
def test_set_mapping_reports_unreadable_input(capsys, source, message):
    resolve, parts = session()
    code, _, err = run(resolve, capsys, "audio", "set-mapping", source, "-i", "1")
    assert code == 1 and message in err


def test_set_mapping_needs_a_target(capsys):
    resolve, parts = session()
    code, _, err = run(resolve, capsys, "audio", "set-mapping", json.dumps(STEREO))
    assert code == 1 and "Give --clip NAME" in err


def test_set_mapping_fails_when_resolve_rejects_it(capsys):
    clip_rec = media_clip(SetAudioMapping=False)
    resolve, parts = session(clips=[clip_rec])
    code, _, err = run(resolve, capsys, "audio", "set-mapping", json.dumps(STEREO), "-c", "interview.mov")
    assert code == 1 and "rejected the audio mapping for clip 'interview.mov'" in err
    parts["tracks"][1][0].set(SetSourceAudioChannelMapping=False)
    code, _, err = run(resolve, capsys, "audio", "set-mapping", json.dumps(STEREO), "-i", "1")
    assert code == 1 and "rejected the source channel mapping for clip 1 ('dialog_01')" in err


# --- clip audio properties ------------------------------------------------------------------


def test_props_show_only_audio_keys_with_constants_by_name(capsys):
    resolve, parts = session()
    code, out, _ = run(resolve, capsys, "audio", "props", "-i", "1")
    assert code == 0
    assert out == {"timeline": "Edit v1", "track": 1, "clips": [{"index": 1, "name": "dialog_01", "properties": {
        "AudioVolume": -3.0, "AudioPan": 0.0,
        "AudioDialogueLevelerMode": "DIALOGUE_LEVELER_MODE_MORE_LIFT_FOR_LOW_LEVELS"}}]}


def test_props_fail_when_resolve_returns_nothing(capsys):
    resolve, parts = session()
    parts["tracks"][1][1].set(GetProperties=None)
    code, _, err = run(resolve, capsys, "audio", "props")
    assert code == 1 and "Could not read the properties of clip 2 ('dialog_02')" in err


@needs_stub
def test_set_shortcuts_become_typed_properties_and_enable_their_sections(capsys):
    resolve, parts = session()
    first, second = parts["tracks"][1]
    code, out, _ = run(resolve, capsys, "audio", "set", "-i", "1", "--volume", "-6", "--pan", "20",
                       "--leveler", "more-lift")
    assert code == 0
    ((props,),) = first.called("SetProperties")
    assert props == {"AudioVolume": -6.0, "AudioPan": 20.0, "AudioVolumeEnabled": True, "AudioPanEnabled": True,
                     "AudioDialogueLevelerEnabled": True, "AudioDialogueLevelerMode": 12}
    assert type(props["AudioVolume"]) is float and props["AudioVolumeEnabled"] is True
    assert second.called("SetProperties") == []
    assert out["properties"]["AudioDialogueLevelerMode"] == "DIALOGUE_LEVELER_MODE_MORE_LIFT_FOR_LOW_LEVELS"
    assert out["clips"] == [{"index": 1, "name": "dialog_01"}]


@needs_stub
def test_set_explicit_section_switch_wins_over_the_shortcut(capsys):
    resolve, parts = session()
    code, _, _ = run(resolve, capsys, "audio", "set", "-i", "2", "--semitones", "2", "--cents", "10",
                     "AudioPitchEnabled=false")
    assert code == 0
    assert parts["tracks"][1][1].called("SetProperties") == [
        ({"AudioPitchSemiTones": 2.0, "AudioPitchCents": 10.0, "AudioPitchEnabled": False},)]


@needs_stub
def test_set_pairs_apply_to_every_clip_after_making_the_timeline_current(capsys):
    resolve, parts = session()
    log = []
    parts["project"].set(SetCurrentTimeline=logging_to(log, "current"))
    for item in parts["tracks"][1]:
        item.set(SetProperties=logging_to(log, item.label))
    code, out, _ = run(resolve, capsys, "audio", "set", "-t", "Edit v1", "--leveler", "off",
                       "AudioDialogueLevelerOutputGain=2")
    assert code == 0
    expected = {"AudioDialogueLevelerEnabled": False, "AudioDialogueLevelerOutputGain": 2.0}
    assert log == [("current", (parts["timeline"],)), ("audio1", (expected,)), ("audio2", (expected,))]
    assert out["properties"] == expected and len(out["clips"]) == 2


@needs_stub
def test_set_accepts_constants_by_name(capsys):
    resolve, parts = session()
    code, out, _ = run(resolve, capsys, "audio", "set", "-i", "1",
                       "AudioDialogueLevelerMode=resolve.DIALOGUE_LEVELER_MODE_ALLOW_WIDER_DYNAMICS")
    assert code == 0
    assert parts["tracks"][1][0].called("SetProperties") == [({"AudioDialogueLevelerMode": 10},)]
    assert out["properties"] == {"AudioDialogueLevelerMode": "DIALOGUE_LEVELER_MODE_ALLOW_WIDER_DYNAMICS"}


@needs_stub
@pytest.mark.parametrize("argv, message", [
    ([], "Nothing to set"),
    (["Opacity=50"], "Not an audio property: Opacity"),
    (["Loudness=3"], "Not an audio property: Loudness"),
    (["--volume", "-6", "AudioVolume=-3"], "Property given twice"),
    (["AudioPan"], "Expected KEY=VALUE"),
    (["AudioVolumeEnabled=maybe"], "AudioVolumeEnabled"),
    (["AudioDialogueLevelerMode=LOUD"], "is not a DialogueLevelerMode constant"),
])
def test_set_refuses_bad_input_before_touching_resolve(capsys, argv, message):
    resolve, parts = session()
    code, _, err = run(resolve, capsys, "audio", "set", "-t", "Edit v1", *argv)
    assert code == 1 and message in err
    assert parts["project"].called("SetCurrentTimeline") == []
    assert all(item.called("SetProperties") == [] for item in parts["tracks"][1])


@needs_stub
def test_set_reports_clips_resolve_rejected_and_those_it_changed(capsys):
    resolve, parts = session()
    parts["tracks"][1][1].set(SetProperties=False)
    code, _, err = run(resolve, capsys, "audio", "set", "--volume", "3")
    assert code == 1
    assert "on clip(s) 2 ('dialog_02') of audio track 1" in err and "applied to clip(s) 1" in err


@needs_stub
@pytest.mark.parametrize("argv, message", [
    (["-i", "9"], "Clip index 9 is out of range; audio track 1 has 2 clip(s)"),
    (["-T", "5"], "Audio track 5 does not exist"),
    (["-T", "2"], "Audio track 2 of 'Edit v1' has no clips"),
])
def test_set_bad_clip_selection_leaves_the_current_timeline_alone(capsys, argv, message):
    resolve, parts = session()
    code, _, err = run(resolve, capsys, "audio", "set", "-t", "Edit v1", "--volume", "1", *argv)
    assert code == 1 and message in err
    assert parts["project"].called("SetCurrentTimeline") == []
    assert all(item.called("SetProperties") == [] for item in parts["tracks"][1])


# --- insert ------------------------------------------------------------------------------


@pytest.fixture
def wav(tmp_path):
    path = tmp_path / "vo.wav"
    path.write_bytes(b"RIFF")
    return str(path)


def test_insert_moves_the_playhead_then_inserts_with_samples(capsys, wav):
    resolve, parts = session()
    log = []
    parts["project"].set(SetCurrentTimeline=logging_to(log, "current"),
                         InsertAudioToCurrentTrackAtPlayhead=logging_to(log, "insert"))
    parts["timeline"].set(SetCurrentTimecode=logging_to(log, "playhead"))
    code, out, _ = run(resolve, capsys, "audio", "insert", wav, "--offset", "48000", "--duration", "96000",
                       "--at", "01:00:05:00", "-t", "Edit v1")
    assert code == 0
    assert log == [("current", (parts["timeline"],)), ("playhead", ("01:00:05:00",)),
                   ("insert", (wav, 48000, 96000))]
    assert out == {"path": wav, "timeline": "Edit v1", "at": "01:00:10:00",
                   "offset_samples": 48000, "duration_samples": 96000}


@pytest.mark.parametrize("argv, message", [
    (["/no/such/vo.wav", "--duration", "10"], "Audio file not found: /no/such/vo.wav"),
    (["WAV", "--duration", "10", "--offset", "-1"], "--offset must be 0 or more samples"),
    (["WAV", "--duration", "0"], "--duration must be more than 0 samples"),
])
def test_insert_checks_arguments_before_calling_resolve(capsys, wav, argv, message):
    resolve, parts = session()
    code, _, err = run(resolve, capsys, "audio", "insert", *[wav if a == "WAV" else a for a in argv])
    assert code == 1 and message in err
    assert parts["project"].called("InsertAudioToCurrentTrackAtPlayhead") == []


def test_insert_passes_resolve_an_absolute_path(capsys, wav, monkeypatch):
    monkeypatch.setenv("HOME", os.path.dirname(wav))
    resolve, parts = session()
    code, out, _ = run(resolve, capsys, "audio", "insert", "~/vo.wav", "--duration", "10")
    assert code == 0
    assert parts["project"].called("InsertAudioToCurrentTrackAtPlayhead") == [(wav, 0, 10)]
    assert out["path"] == wav


def test_insert_needs_a_duration(wav):
    resolve, _ = session()
    with pytest.raises(SystemExit) as exc:
        main(["audio", "insert", wav], resolve=resolve)
    assert exc.value.code == 2


def test_insert_fails_when_the_playhead_or_the_insert_is_refused(capsys, wav):
    resolve, parts = session()
    parts["timeline"].set(SetCurrentTimecode=False)
    code, _, err = run(resolve, capsys, "audio", "insert", wav, "--duration", "10", "--at", "99:00:00:00")
    assert code == 1 and "Could not move the playhead to '99:00:00:00'" in err
    assert parts["project"].called("InsertAudioToCurrentTrackAtPlayhead") == []
    parts["project"].set(InsertAudioToCurrentTrackAtPlayhead=False)
    code, _, err = run(resolve, capsys, "audio", "insert", wav, "--duration", "10")
    assert code == 1 and "could not insert" in err and "current (selected) audio track" in err


# --- speech -------------------------------------------------------------------------------


def speech_session():
    resolve, parts = session()
    generated = Rec("speech", GetName="vo1.wav", GetUniqueId="gen-1")
    parts["project"].set(GenerateSpeech=generated)
    return resolve, parts


def test_speech_builds_settings_and_returns_the_media_pool_clip(capsys):
    resolve, parts = speech_session()
    code, out, _ = run(resolve, capsys, "audio", "speech", "Hello there", "--voice", "Female 1", "--speed", "1.5",
                       "--variation", "0.3", "--pitch", "-0.5", "--seed", "7", "--filename", "vo1")
    assert code == 0
    settings = {"TextInput": "Hello there", "VoiceModel": "Female 1", "Speed": 1.5, "Variation": 0.3,
                "Pitch": -0.5, "GenerationID": 7, "Filename": "vo1"}
    assert parts["project"].called("GenerateSpeech") == [(settings,)]
    assert parts["project"].called("SetCurrentTimeline") == []
    assert out == {"$type": "MediaPoolItem", "$ref": "clip#gen-1", "name": "vo1.wav", "settings": settings}


def test_speech_voice_file_implies_custom_voice(capsys, tmp_path):
    resolve, parts = speech_session()
    voice = tmp_path / "me.wav"
    voice.write_bytes(b"RIFF")
    code, _, _ = run(resolve, capsys, "audio", "speech", "Hi", "--voice-file", str(voice))
    assert code == 0
    assert parts["project"].called("GenerateSpeech") == [
        ({"TextInput": "Hi", "CustomVoiceFile": str(voice), "VoiceModel": "Custom Voice"},)]


def test_speech_track_adds_to_the_timeline_after_making_it_current(capsys):
    resolve, parts = speech_session()
    log = []
    parts["project"].set(SetCurrentTimeline=logging_to(log, "current"),
                         GenerateSpeech=logging_to(log, "speech", Rec("speech", GetName="x", GetUniqueId="g")))
    code, _, _ = run(resolve, capsys, "audio", "speech", "Hi", "-T", "0", "-t", "Edit v1")
    assert code == 0
    assert log == [("current", (parts["timeline"],)),
                   ("speech", ({"TextInput": "Hi", "AddToTimeline": True, "AudioTrack": 0},))]


@pytest.mark.parametrize("argv, message", [
    (["x" * 351], "at most 350 characters per call; the text has 351"),
    (["   "], "The text to speak is empty"),
    (["Hi", "--voice", "Custom Voice"], "needs --voice-file PATH"),
    (["Hi", "--voice-file", "/no/such/voice.wav"], "Custom voice file not found"),
    (["Hi", "--speed", "11"], "--speed must be -10.0 to 10.0, got 11.0"),
    (["Hi", "--variation", "1.5"], "--variation must be 0.0 to 1.0"),
    (["Hi", "--pitch", "-3"], "--pitch must be -2.0 to 2.0"),
    (["Hi", "--seed", "0"], "--seed must be 1 or more"),
    (["Hi", "-t", "Edit v1"], "-t/--timeline only matters with --add-to-timeline or --track"),
    (["Hi", "-T", "-1"], "--track must be 0 (a new track) or an audio track number"),
])
def test_speech_checks_settings_before_calling_resolve(capsys, argv, message):
    resolve, parts = speech_session()
    code, _, err = run(resolve, capsys, "audio", "speech", *argv)
    assert code == 1 and message in err
    assert parts["project"].called("GenerateSpeech") == []


def test_speech_voice_file_with_another_voice_is_refused(capsys, tmp_path):
    resolve, parts = speech_session()
    voice = tmp_path / "me.wav"
    voice.write_bytes(b"RIFF")
    code, _, err = run(resolve, capsys, "audio", "speech", "Hi", "--voice", "Male 1", "--voice-file", str(voice))
    assert code == 1 and "--voice-file needs --voice 'Custom Voice'" in err
    assert parts["project"].called("GenerateSpeech") == []


def test_speech_fails_when_resolve_generates_nothing(capsys):
    resolve, parts = speech_session()
    parts["project"].set(GenerateSpeech=None)
    code, _, err = run(resolve, capsys, "audio", "speech", "Hi")
    assert code == 1 and "could not generate speech" in err and "Studio" in err


# --- fairlight -----------------------------------------------------------------------------


PRESETS = ["Dialog Mix", "Music Bed"]


def test_fairlight_presets(capsys):
    resolve, parts = session()
    resolve.set(GetFairlightPresets=PRESETS)
    code, out, _ = run(resolve, capsys, "fairlight", "presets")
    assert code == 0 and out == PRESETS
    resolve.set(GetFairlightPresets=None)
    code, _, err = run(resolve, capsys, "fairlight", "presets")
    assert code == 1 and "did not return the Fairlight preset list" in err


def test_fairlight_apply_makes_the_timeline_current_first(capsys):
    resolve, parts = session()
    resolve.set(GetFairlightPresets=PRESETS)
    log = []
    parts["project"].set(SetCurrentTimeline=logging_to(log, "current"),
                         ApplyFairlightPresetToCurrentTimeline=logging_to(log, "apply"))
    code, out, _ = run(resolve, capsys, "fairlight", "apply", "Dialog Mix", "-t", "Edit v1")
    assert code == 0
    assert log == [("current", (parts["timeline"],)), ("apply", ("Dialog Mix",))]
    assert out == {"preset": "Dialog Mix", "timeline": "Edit v1"}


@pytest.mark.parametrize("presets, name, message", [
    (PRESETS, "Dialog Mixx", "No Fairlight preset named 'Dialog Mixx'. Did you mean Dialog Mix?"),
    (PRESETS, "Zzz", "Presets: Dialog Mix, Music Bed."),
    ([], "Dialog Mix", "lists no Fairlight presets"),
])
def test_fairlight_apply_refuses_unknown_presets(capsys, presets, name, message):
    resolve, parts = session()
    resolve.set(GetFairlightPresets=presets)
    code, _, err = run(resolve, capsys, "fairlight", "apply", name)
    assert code == 1 and message in err
    assert parts["project"].called("ApplyFairlightPresetToCurrentTimeline") == []


def test_fairlight_apply_fails_when_resolve_returns_false(capsys):
    resolve, parts = session()
    resolve.set(GetFairlightPresets=PRESETS)
    parts["project"].set(ApplyFairlightPresetToCurrentTimeline=False)
    code, _, err = run(resolve, capsys, "fairlight", "apply", "Music Bed")
    assert code == 1 and "could not apply Fairlight preset 'Music Bed' to 'Edit v1'" in err
