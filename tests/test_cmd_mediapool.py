"""Tests for dava folder / clip / storage and the new media and meta actions (dava/commands/mediapool.py)."""

import ast
import json
import os

import pytest

from dava import cli, connect
from dava.commands import mediapool
from dava.registry import leaf_parsers
from recorder import Rec, rec_resolve

HAS_STUB = os.path.isfile(connect.stub_path() or "")
needs_stub = pytest.mark.skipif(not HAS_STUB, reason="Resolve scripting API definition not installed")

OWN_GROUPS = {"folder", "clip", "storage"}
OWN_ACTIONS = {("media", "info"), ("media", "import-sequence"), ("media", "import-timeline"),
               ("media", "delete-timeline"), ("media", "timeline-mattes"), ("media", "timeline-matte-add"),
               ("meta", "export")}

PROPS = {"Type": "Video", "File Path": "/media/a.mov", "FPS": "24", "Frames": "240", "Clip Color": "Orange"}

# Constant values as a real Resolve exposes them (numbers), set on the fake resolve object.
CONSTANTS = {"MARKER_RED": 5.0, "MARKER_BLUE": 1.0, "AUDIO_SYNC_WAVEFORM": 11.0, "AUDIO_SYNC_CHANNEL_MIX": 21.0,
             "AUDIO_SYNC_CHANNEL_AUTOMATIC": 22.0, "MULTICAM_ANGLE_SYNC_AUDIO": 31.0, "MULTICAM_AUDIO_ALL": 41.0,
             "MULTICAM_ANGLE_NAME_CLIP": 51.0, "MULTICAM_DETECT_BY_REEL_NAME": 61.0,
             "CLONE_CHECKSUM_TYPE_XXH_64": 71.0, "CLONE_CHECKSUM_TYPE_MD5": 72.0}


def make_clip(label, name, **returns):
    base = dict(GetName=name, GetUniqueId=f"clip-{label}", GetMediaId=f"media-{label}", GetFlagList=[],
                GetMarkInOut={}, GetTimeline=None,
                GetClipProperty=lambda *key: PROPS.get(key[0], "") if key else dict(PROPS))
    base.update(returns)
    return Rec(label, **base)


def world():
    """Media pool /Master (a.mov) > Shots (b.mov, c.wav) > Day1 (empty); the current folder is Shots."""
    a, b, c = make_clip("a", "a.mov"), make_clip("b", "b.mov"), make_clip("c", "c.wav")
    day1 = Rec("day1", GetName="Day1", GetUniqueId="folder-day1", GetClipList=[], GetSubFolderList=[],
               GetIsFolderStale=False)
    shots = Rec("shots", GetName="Shots", GetUniqueId="folder-shots", GetClipList=[b, c], GetSubFolderList=[day1],
                GetIsFolderStale=True)
    resolve, parts = rec_resolve(clips=[a])
    parts["root"].set(GetSubFolderList=[shots], GetIsFolderStale=False)
    parts["mediapool"].set(GetCurrentFolder=shots, GetSelectedClips=[], GetUniqueId="pool-1")
    for name, value in CONSTANTS.items():
        setattr(resolve, name, value)
    parts.update(resolve=resolve, a=a, b=b, c=c, shots=shots, day1=day1)
    return resolve, parts


def run(capsys, resolve, *argv):
    """Run dava with --json; return (exit code, parsed stdout or None, stderr)."""
    code = cli.main(["--json", *argv], resolve=resolve)
    out, err = capsys.readouterr()
    return code, (json.loads(out) if out.strip() else None), err


def usage_error(resolve, *argv):
    with pytest.raises(SystemExit) as exc:
        cli.main(list(argv), resolve=resolve)
    return exc.value.code


def names(rec):
    return [name for name, _ in rec.calls]


# --- registration -------------------------------------------------------------------------


def own_leaves():
    for words, leaf, help_text in leaf_parsers(cli.build_parser()):
        if words[0] in OWN_GROUPS or tuple(words) in OWN_ACTIONS:
            yield words, leaf, help_text


def test_every_command_declares_handler_read_only_and_ascii_help():
    leaves = list(own_leaves())
    assert len(leaves) == 68
    for words, leaf, help_text in leaves:
        assert leaf._defaults["func"].__module__ == mediapool.__name__, words
        assert isinstance(leaf._defaults.get("read_only"), bool), words
        texts = [help_text, leaf.description or "", leaf.format_help()] + [a.help or "" for a in leaf._actions]
        assert all(text.isascii() for text in texts), words
        assert help_text and all(a.help for a in leaf._actions), words
    for group in OWN_GROUPS | {words[0] for words in OWN_ACTIONS}:
        with pytest.raises(SystemExit) as exc:  # group help %-formats every action's help text
            cli.main([group, "--help"])
        assert exc.value.code == 0, group


READ_ONLY_RUNS = [
    ("folder", "tree", "--clips"), ("folder", "list"), ("folder", "list", "Shots/Day1"), ("folder", "current"),
    ("folder", "stale"), ("clip", "info", "b.mov"), ("clip", "in-out", "a.mov"), ("clip", "flags", "a.mov"),
    ("clip", "mattes", "a.mov"), ("clip", "selected"), ("clip", "transcript", "a.mov"), ("storage", "volumes"),
    ("storage", "ls", os.sep), ("storage", "clone-status"), ("media", "info"), ("media", "timeline-mattes"),
]


def test_exactly_the_commands_run_in_read_only_mode_are_declared_read_only():
    declared = {tuple(words) for words, leaf, _ in own_leaves() if leaf._defaults["read_only"]}
    assert declared == {tuple(argv[:2]) for argv in READ_ONLY_RUNS}


@pytest.mark.parametrize("argv", READ_ONLY_RUNS)
def test_read_only_commands_call_only_getters_and_run_in_read_only_mode(capsys, argv):
    resolve, w = world()
    w["storage"].set(GetMountedVolumeList=["/Volumes/Card"], GetCloneStatus={"JobStatus": "Complete"},
                     GetSubFolderList=[], GetFileList=[])
    w["mediapool"].set(GetClipMatteList=[], GetTimelineMatteList=[])
    w["a"].set(GetTranscription={"language": "en", "segments": []})
    code, _, err = run(capsys, resolve, "--read-only", *argv)
    assert code == 0, err
    recs = [v for v in w.values() if isinstance(v, Rec)]
    called = {name for rec in recs for name in names(rec)}
    assert called and all(name.startswith(("Get", "Is", "Has")) for name in called), called


def test_read_only_mode_refuses_changes_before_touching_resolve(capsys):
    resolve, w = world()
    code, _, err = run(capsys, resolve, "--read-only", "folder", "create", "X")
    assert code == 1 and "read-only" in err
    assert w["mediapool"].calls == []


@needs_stub
def test_api_names_and_constants_exist_in_the_api_definition():
    from dava.spec import load_spec

    spec = load_spec()
    tree = ast.parse(open(mediapool.__file__, encoding="utf-8").read())
    used = {node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute) and node.func.attr[:1].isupper()
            and not (isinstance(node.func.value, ast.Name) and node.func.value.id == "argparse")}
    known = {m for cls in spec.classes.values() for m in cls.methods}
    assert used - known == set()
    # Every owned method has a command, except the ones other command groups own.
    elsewhere = {"CreateEmptyTimeline", "SetClipProperty", "GetAudioMapping", "SetAudioMapping", "GetMetadata",
                 "SetMetadata", "GetThirdPartyMetadata", "SetThirdPartyMetadata", "GetClipColor", "SetClipColor",
                 "ClearClipColor", "AddMarker", "DeleteMarkersByColor", "DeleteMarkerAtFrame",
                 "DeleteMarkerByCustomData", "GetMarkers", "GetMarkerByCustomData", "UpdateMarkerCustomData",
                 "GetMarkerCustomData"}
    for cls in ("MediaPool", "Folder", "MediaPoolItem", "MediaStorage"):
        assert set(spec.classes[cls].methods) - used - elsewhere == set(), cls
    tables = {"AudioSyncMode": mediapool.SYNC_MODES, "AudioSyncChannel": mediapool.SYNC_CHANNELS,
              "MulticamAngleSyncMode": mediapool.ANGLE_SYNC, "MulticamAudioMode": mediapool.MULTICAM_AUDIO,
              "MulticamAngleNameMode": mediapool.ANGLE_NAMES, "MulticamDetectMode": mediapool.DETECT_MODES,
              "CloneChecksumType": mediapool.CHECKSUMS}
    for alias, table in tables.items():
        assert sorted(table.values()) == sorted(spec.aliases[alias].values), alias
    slate = {"MARKER_" + color.upper() for color in mediapool.MARKER_COLORS}
    assert slate == set(spec.aliases["SlateMarkerColor"].values) - {"MARKER_NONE"}
    assert set(mediapool.FLAG_COLORS) == set(spec.aliases["FlagColor"].values)
    assert set(mediapool.MARK_TYPES) == set(spec.aliases["MarkType"].values)
    assert set(mediapool.INFO_PROPERTIES) <= set(spec.typeddicts["ClipProperties"].fields)


# --- folder ------------------------------------------------------------------------------


def test_folder_tree_nests_folders_with_paths_counts_and_optional_clips(capsys):
    resolve, _ = world()
    code, out, _ = run(capsys, resolve, "folder", "tree")
    assert code == 0
    assert out["path"] == "/Master" and out["clip_count"] == 1 and out["folder_count"] == 1
    shots = out["folders"][0]
    assert shots == {"name": "Shots", "ref": "folder#folder-shots", "path": "/Master/Shots", "clip_count": 2,
                     "folder_count": 1, "folders": [{"name": "Day1", "ref": "folder#folder-day1",
                                                     "path": "/Master/Shots/Day1", "clip_count": 0,
                                                     "folder_count": 0, "folders": []}]}
    code, out, _ = run(capsys, resolve, "folder", "tree", "Shots", "--depth", "0", "--clips")
    assert out["path"] == "/Master/Shots" and "folders" not in out
    assert out["clips"] == [{"ref": "clip#clip-b", "name": "b.mov"}, {"ref": "clip#clip-c", "name": "c.wav"}]
    code, _, err = run(capsys, resolve, "folder", "tree", "--depth", "-1")
    assert code == 1 and "--depth" in err


def test_folder_list_shows_the_current_folder_or_a_path(capsys):
    resolve, _ = world()
    code, out, _ = run(capsys, resolve, "folder", "list")
    assert code == 0
    assert out == [{"kind": "folder", "name": "Day1", "ref": "folder#folder-day1", "type": ""},
                   {"kind": "clip", "name": "b.mov", "ref": "clip#clip-b", "type": "Video"},
                   {"kind": "clip", "name": "c.wav", "ref": "clip#clip-c", "type": "Video"}]
    code, out, _ = run(capsys, resolve, "folder", "list", "/Master")
    assert [row["name"] for row in out] == ["Shots", "a.mov"]
    code, out, _ = run(capsys, resolve, "folder", "list", "folder#folder-day1")
    assert code == 0 and out == []


def test_unknown_folder_path_fails_with_a_hint(capsys):
    resolve, _ = world()
    code, _, err = run(capsys, resolve, "folder", "list", "Shots/Nope")
    assert code == 1 and "'Nope'" in err and "dava folder tree" in err
    code, _, err = run(capsys, resolve, "folder", "list", "clip:a.mov")
    assert code == 1 and "No folder" in err
    code, _, err = run(capsys, resolve, "folder", "list", "$nothing")
    assert code == 1 and "Unknown handle" in err


def test_folder_current_and_use(capsys):
    resolve, w = world()
    code, out, _ = run(capsys, resolve, "folder", "current")
    assert out == {"$type": "Folder", "$ref": "folder#folder-shots", "name": "Shots", "path": "/Master/Shots"}
    code, out, _ = run(capsys, resolve, "folder", "use", "Shots/Day1")
    assert code == 0 and out["path"] == "/Master/Shots/Day1"
    assert w["mediapool"].called("SetCurrentFolder") == [(w["day1"],)]
    w["mediapool"].set(SetCurrentFolder=False)
    code, _, err = run(capsys, resolve, "folder", "use", "/Master")
    assert code == 1 and "could not make 'Master' the current folder" in err
    assert usage_error(resolve, "folder", "use") == 2


def test_folder_create_under_current_or_parent_and_optionally_use_it(capsys):
    resolve, w = world()
    new = Rec("new", GetName="Day2", GetUniqueId="folder-day2", GetSubFolderList=[])

    def add(parent, name):
        parent.returns["GetSubFolderList"].append(new)
        return new

    w["mediapool"].set(AddSubFolder=add)
    code, out, _ = run(capsys, resolve, "folder", "create", "Day2", "--use")
    assert code == 0
    assert w["mediapool"].called("AddSubFolder") == [(w["shots"], "Day2")]
    assert w["mediapool"].called("SetCurrentFolder") == [(new,)]
    assert out["$ref"] == "folder#folder-day2" and out["path"] == "/Master/Shots/Day2"
    code, out, _ = run(capsys, resolve, "folder", "create", "Audio", "-p", "/Master")
    assert w["mediapool"].called("AddSubFolder")[-1] == (w["root"], "Audio")
    assert len(w["mediapool"].called("SetCurrentFolder")) == 1
    w["mediapool"].set(AddSubFolder=None)
    code, _, err = run(capsys, resolve, "folder", "create", "X")
    assert code == 1 and "could not create folder 'X' under 'Shots'" in err


def test_folder_delete_refuses_the_root_and_reports_failures(capsys):
    resolve, w = world()
    code, out, _ = run(capsys, resolve, "folder", "delete", "Shots/Day1")
    assert code == 0 and w["mediapool"].called("DeleteFolders") == [([w["day1"]],)]
    assert out == {"deleted": [{"ref": "folder#folder-day1", "name": "Day1", "path": "/Master/Shots/Day1"}]}
    code, _, err = run(capsys, resolve, "folder", "delete", "/")
    assert code == 1 and "root folder" in err
    assert len(w["mediapool"].called("DeleteFolders")) == 1
    w["mediapool"].set(DeleteFolders=False)
    code, _, err = run(capsys, resolve, "folder", "delete", "Shots")
    assert code == 1 and "/Master/Shots" in err
    # A folder REF that is no longer in the tree (e.g. an old $handle) has no path; name it instead.
    gone = Rec("gone", GetName="Gone", GetUniqueId="folder-gone", GetSubFolderList=[], GetClipList=[])
    w["mediapool"].set(GetCurrentFolder=gone)
    code, _, err = run(capsys, resolve, "folder", "delete", "folder")
    assert code == 1 and "could not delete folder(s) 'Gone'" in err
    assert w["mediapool"].called("DeleteFolders")[-1] == ([gone],)


def test_folder_move(capsys):
    resolve, w = world()
    code, out, _ = run(capsys, resolve, "folder", "move", "Shots/Day1", "--to", "/Master")
    assert code == 0 and w["mediapool"].called("MoveFolders") == [([w["day1"]], w["root"])]
    assert out["to"] == "/Master" and out["moved"][0]["ref"] == "folder#folder-day1"
    w["mediapool"].set(MoveFolders=False)
    code, _, err = run(capsys, resolve, "folder", "move", "Shots", "--to", "Shots/Day1")
    assert code == 1 and "could not move" in err
    assert usage_error(resolve, "folder", "move", "Shots") == 2


def test_folder_export_and_import_drb(capsys, tmp_path):
    resolve, w = world()
    target = tmp_path / "shots.drb"
    code, out, _ = run(capsys, resolve, "folder", "export", "Shots", str(target))
    assert code == 0 and w["shots"].called("Export") == [(str(target),)]
    assert out == {"folder": "/Master/Shots", "file": str(target)}
    code, _, err = run(capsys, resolve, "folder", "export", "Shots", str(tmp_path / "missing" / "x.drb"))
    assert code == 1 and "Output folder does not exist" in err and len(w["shots"].called("Export")) == 1
    w["shots"].set(Export=False)
    code, _, err = run(capsys, resolve, "folder", "export", "Shots", str(target))
    assert code == 1 and "could not export folder 'Shots'" in err

    target.write_text("drb")
    code, out, _ = run(capsys, resolve, "folder", "import", str(target))
    assert code == 0 and w["mediapool"].called("ImportFolderFromFile") == [(str(target),)]
    code, _, _ = run(capsys, resolve, "folder", "import", str(target), "--source-clips-path", str(tmp_path))
    assert w["mediapool"].called("ImportFolderFromFile")[-1] == (str(target), str(tmp_path))
    code, _, err = run(capsys, resolve, "folder", "import", str(tmp_path / "none.drb"))
    assert code == 1 and "File not found" in err
    w["mediapool"].set(ImportFolderFromFile=False)
    code, _, err = run(capsys, resolve, "folder", "import", str(target))
    assert code == 1 and ".drb" in err


def test_folder_refresh_and_stale(capsys):
    resolve, w = world()
    code, out, _ = run(capsys, resolve, "folder", "stale")
    assert code == 0
    assert out == [{"path": "/Master", "ref": "folder#folder-root", "stale": False},
                   {"path": "/Master/Shots", "ref": "folder#folder-shots", "stale": True},
                   {"path": "/Master/Shots/Day1", "ref": "folder#folder-day1", "stale": False}]
    code, _, _ = run(capsys, resolve, "folder", "refresh")
    assert code == 0 and w["mediapool"].called("RefreshFolders") == [()]
    w["mediapool"].set(RefreshFolders=False)
    code, _, err = run(capsys, resolve, "folder", "refresh")
    assert code == 1 and "collaboration" in err


@pytest.mark.parametrize("flags, expected", [
    ((), ()), (("--speakers",), (True,)), (("--no-speakers",), (False,)),
    (("--nested-clip",), (None, True)), (("--no-speakers", "--nested-clip"), (False, True)),
])
def test_folder_transcribe_passes_only_the_given_options(capsys, flags, expected):
    resolve, w = world()
    code, out, _ = run(capsys, resolve, "folder", "transcribe", "Shots/Day1", *flags)
    assert code == 0 and w["day1"].called("TranscribeAudio") == [expected]
    assert out["path"] == "/Master/Shots/Day1"


def test_folder_transcribe_failure_and_conflicting_flags(capsys):
    resolve, w = world()
    w["shots"].set(TranscribeAudio=False)
    code, _, err = run(capsys, resolve, "folder", "transcribe")
    assert code == 1 and "folder 'Shots'" in err and "Studio" in err
    assert usage_error(resolve, "folder", "transcribe", "--speakers", "--no-speakers") == 2


@pytest.mark.parametrize("action, method", [
    ("transcript-clear", "ClearTranscription"), ("classify-audio", "PerformAudioClassification"),
    ("classify-audio-clear", "ClearAudioClassification"),
])
def test_folder_clear_and_classify(capsys, action, method):
    resolve, w = world()
    code, _, _ = run(capsys, resolve, "folder", action, "/Master")
    assert code == 0 and w["root"].called(method) == [()]
    w["root"].set(**{method: False})
    code, _, err = run(capsys, resolve, "folder", action, "/Master")
    assert code == 1 and "folder 'Master'" in err


def test_folder_intellisearch_and_slate_id(capsys):
    resolve, w = world()
    code, _, _ = run(capsys, resolve, "folder", "intellisearch", "--faces")
    assert code == 0 and w["shots"].called("AnalyzeForIntellisearch") == [(True, False)]
    code, _, _ = run(capsys, resolve, "folder", "intellisearch", "--better")
    assert w["shots"].called("AnalyzeForIntellisearch")[-1] == (False, True)
    code, _, _ = run(capsys, resolve, "folder", "slate-id", "-c", "Red")
    assert code == 0 and w["shots"].called("AnalyzeForSlate") == [(CONSTANTS["MARKER_RED"],)]
    assert usage_error(resolve, "folder", "slate-id") == 2
    assert usage_error(resolve, "folder", "slate-id", "-c", "None") == 2
    w["shots"].set(AnalyzeForSlate=False)
    code, _, err = run(capsys, resolve, "folder", "slate-id", "-c", "Blue")
    assert code == 1 and "slate analysis" in err and "Extras" in err


def test_folder_deblur_returns_original_and_new_clips(capsys):
    resolve, w = world()
    new_b = make_clip("nb", "b Deblur.mov")
    w["shots"].set(RemoveMotionBlur=lambda *a: [[w["b"], new_b]])
    code, out, _ = run(capsys, resolve, "folder", "deblur")
    assert code == 0 and w["shots"].called("RemoveMotionBlur") == [()]
    assert out == [{"original": {"ref": "clip#clip-b", "name": "b.mov"},
                    "deblurred": {"ref": "clip#clip-nb", "name": "b Deblur.mov"}}]
    code, _, _ = run(capsys, resolve, "folder", "deblur", "--format", "mov", "--codec", "ProRes422",
                     "--whole-clip", "--no-extreme", "--more-gpu-memory")
    assert w["shots"].called("RemoveMotionBlur")[-1] == ({"Format": "mov", "Codec": "ProRes422",
                                                           "UseExtremeMode": False, "UseMarkInMarkOut": False,
                                                           "UseMoreGpuMemory": True},)
    w["shots"].set(RemoveMotionBlur=[])
    code, _, err = run(capsys, resolve, "folder", "deblur")
    assert code == 1 and "did not deblur" in err


# --- clip lookup and info ------------------------------------------------------------------


def test_clip_info(capsys):
    resolve, w = world()
    w["b"].set(GetFlagList=["Red"], GetMarkInOut={"video": {"in": 2, "out": 9}})
    code, out, _ = run(capsys, resolve, "clip", "info", "b.mov")
    assert code == 0
    assert out == {"$type": "MediaPoolItem", "$ref": "clip#clip-b", "name": "b.mov", "media_id": "media-b",
                   "folder": "/Master/Shots",
                   "properties": {"Type": "Video", "File Path": "/media/a.mov", "Frames": "240", "FPS": "24"},
                   "flags": ["Red"], "in_out": {"video": {"in": 2, "out": 9}}, "timeline": None}
    timeline = Rec("tl2", GetName="Nested", GetUniqueId="tl-2")
    w["a"].set(GetTimeline=timeline)
    code, out, _ = run(capsys, resolve, "clip", "info", "clip#clip-a")
    assert out["timeline"] == {"$type": "Timeline", "$ref": "timeline#tl-2", "name": "Nested"}


def test_clip_lookup_errors_are_actionable(capsys):
    resolve, w = world()
    dup1, dup2 = make_clip("d1", "dup.mov"), make_clip("d2", "dup.mov")
    w["root"].set(GetClipList=[w["a"], dup1])
    w["day1"].set(GetClipList=[dup2])
    code, _, err = run(capsys, resolve, "clip", "info", "dup.mov")
    assert code == 1 and "ambiguous" in err and "/Master, /Master/Shots/Day1" in err and "clip#ID" in err
    code, out, _ = run(capsys, resolve, "clip", "info", "clip:/Master/Shots/Day1/dup.mov")
    assert code == 0 and out["$ref"] == "clip#clip-d2"
    code, _, err = run(capsys, resolve, "clip", "info", "nope.mov")
    assert code == 1 and "No media pool clip named 'nope.mov'" in err
    code, _, err = run(capsys, resolve, "clip", "info", "timeline")
    assert code == 1 and "No media pool clip named 'timeline'" in err
    code, _, err = run(capsys, resolve, "clip", "info", "folder:/Master::folder:Shots")
    assert code == 1 and "names a Folder, not a media pool clip" in err


def test_clip_rename(capsys):
    resolve, w = world()
    code, out, _ = run(capsys, resolve, "clip", "rename", "a.mov", "hero.mov")
    assert code == 0 and w["a"].called("SetName") == [("hero.mov",)]
    assert out == {"ref": "clip#clip-a", "name": "a.mov", "old_name": "a.mov"}
    w["a"].set(SetName=False)
    code, _, err = run(capsys, resolve, "clip", "rename", "a.mov", "x")
    assert code == 1 and "could not rename clip 'a.mov' to 'x'" in err


def test_clip_move_with_selected_clips_counted_once(capsys):
    resolve, w = world()
    w["mediapool"].set(GetSelectedClips=[w["c"], w["b"]])
    code, out, _ = run(capsys, resolve, "clip", "move", "b.mov", "--selected", "--to", "/Master")
    assert code == 0
    assert w["mediapool"].called("MoveClips") == [([w["b"], w["c"]], w["root"])]
    assert out == {"to": "/Master", "clips": [{"ref": "clip#clip-b", "name": "b.mov"},
                                              {"ref": "clip#clip-c", "name": "c.wav"}]}
    code, _, err = run(capsys, resolve, "clip", "move", "--to", "/Master")
    assert code == 1 and "--selected" in err
    w["mediapool"].set(GetSelectedClips=[])
    code, _, err = run(capsys, resolve, "clip", "move", "--selected", "--to", "/Master")
    assert code == 1 and "No clips are selected" in err
    w["mediapool"].set(MoveClips=False)
    code, _, err = run(capsys, resolve, "clip", "move", "a.mov", "--to", "Shots")
    assert code == 1 and "could not move 1 clip(s) into folder 'Shots'" in err
    assert len(w["mediapool"].called("MoveClips")) == 2


def test_clip_delete_unlink_relink(capsys, tmp_path):
    resolve, w = world()
    code, out, _ = run(capsys, resolve, "clip", "delete", "a.mov", "c.wav")
    assert code == 0 and w["mediapool"].called("DeleteClips") == [([w["a"], w["c"]],)]
    assert out["deleted"][1] == {"ref": "clip#clip-c", "name": "c.wav"}
    code, _, _ = run(capsys, resolve, "clip", "unlink", "b.mov")
    assert code == 0 and w["mediapool"].called("UnlinkClips") == [([w["b"]],)]
    code, out, _ = run(capsys, resolve, "clip", "relink", "b.mov", "--dir", str(tmp_path))
    assert code == 0 and w["mediapool"].called("RelinkClips") == [([w["b"]], str(tmp_path))]
    code, _, err = run(capsys, resolve, "clip", "relink", "b.mov", "--dir", str(tmp_path / "gone"))
    assert code == 1 and "Folder not found" in err and len(w["mediapool"].called("RelinkClips")) == 1
    w["mediapool"].set(DeleteClips=False, UnlinkClips=False, RelinkClips=False)
    for argv in (("delete", "a.mov"), ("unlink", "a.mov"), ("relink", "a.mov", "-d", str(tmp_path))):
        code, _, err = run(capsys, resolve, "clip", *argv)
        assert code == 1 and "could not" in err, argv


def test_clip_replace_and_proxies(capsys, tmp_path):
    resolve, w = world()
    media = tmp_path / "new.mov"
    media.write_text("x")
    code, _, _ = run(capsys, resolve, "clip", "replace", "a.mov", str(media))
    assert code == 0 and w["a"].called("ReplaceClip") == [(str(media),)]
    code, _, _ = run(capsys, resolve, "clip", "replace", "a.mov", str(media), "--keep-subclip")
    assert w["a"].called("ReplaceClipPreserveSubClip") == [(str(media),)] and len(w["a"].called("ReplaceClip")) == 1
    code, out, _ = run(capsys, resolve, "clip", "proxy-link", "a.mov", str(media))
    assert code == 0 and w["a"].called("LinkProxyMedia") == [(str(media),)] and out["proxy"] == str(media)
    code, _, _ = run(capsys, resolve, "clip", "proxy-unlink", "a.mov")
    assert w["a"].called("UnlinkProxyMedia") == [()]
    code, _, _ = run(capsys, resolve, "clip", "full-res-link", "a.mov", str(tmp_path))
    assert code == 0 and w["a"].called("LinkFullResolutionMedia") == [(str(tmp_path),)]
    code, _, err = run(capsys, resolve, "clip", "replace", "a.mov", str(tmp_path / "none.mov"))
    assert code == 1 and "File not found" in err
    w["a"].set(ReplaceClip=False, LinkProxyMedia=False, UnlinkProxyMedia=False, LinkFullResolutionMedia=False)
    for argv in (("replace", "a.mov", str(media)), ("proxy-link", "a.mov", str(media)),
                 ("proxy-unlink", "a.mov"), ("full-res-link", "a.mov", str(media))):
        code, _, err = run(capsys, resolve, "clip", *argv)
        assert code == 1 and "'a.mov'" in err, argv


def test_clip_in_out_get_set_clear(capsys):
    resolve, w = world()
    w["a"].set(GetMarkInOut={"video": {"in": 10, "out": 20}})
    code, out, _ = run(capsys, resolve, "clip", "in-out", "a.mov")
    assert out == {"ref": "clip#clip-a", "name": "a.mov", "marks": {"video": {"in": 10, "out": 20}}}
    code, _, _ = run(capsys, resolve, "clip", "in-out-set", "a.mov", "10", "20")
    assert code == 0 and w["a"].called("SetMarkInOut") == [(10, 20)]
    code, _, _ = run(capsys, resolve, "clip", "in-out-set", "a.mov", "3", "4", "--type", "audio")
    assert w["a"].called("SetMarkInOut")[-1] == (3, 4, "audio")
    code, _, err = run(capsys, resolve, "clip", "in-out-set", "a.mov", "20", "10")
    assert code == 1 and "comes before" in err and len(w["a"].called("SetMarkInOut")) == 2
    code, _, _ = run(capsys, resolve, "clip", "in-out-clear", "a.mov")
    code, _, _ = run(capsys, resolve, "clip", "in-out-clear", "a.mov", "--type", "video")
    assert w["a"].called("ClearMarkInOut") == [(), ("video",)]
    assert usage_error(resolve, "clip", "in-out-clear", "a.mov", "--type", "both") == 2
    w["a"].set(SetMarkInOut=False, ClearMarkInOut=False)
    code, _, err = run(capsys, resolve, "clip", "in-out-set", "a.mov", "1", "2")
    assert code == 1 and "inside the clip" in err
    code, _, err = run(capsys, resolve, "clip", "in-out-clear", "a.mov")
    assert code == 1 and "could not clear the in/out marks" in err


def test_clip_flags(capsys):
    resolve, w = world()
    w["a"].set(GetFlagList=["Red", "Blue"])
    code, out, _ = run(capsys, resolve, "clip", "flag-add", "a.mov", "Red", "Blue")
    assert code == 0 and w["a"].called("AddFlag") == [("Red",), ("Blue",)]
    assert out["flags"] == ["Red", "Blue"]
    code, _, _ = run(capsys, resolve, "clip", "flag-clear", "a.mov")
    code, _, _ = run(capsys, resolve, "clip", "flag-clear", "a.mov", "Red")
    assert w["a"].called("ClearFlags") == [("All",), ("Red",)]
    assert usage_error(resolve, "clip", "flag-add", "a.mov", "Orange") == 2
    w["a"].set(AddFlag=lambda color: color != "Blue")
    code, _, err = run(capsys, resolve, "clip", "flag-add", "a.mov", "Red", "Blue", "Mint")
    assert code == 1 and "could not add a Blue flag to 'a.mov'. Added before it: Red." in err
    assert w["a"].called("AddFlag")[-2:] == [("Red",), ("Blue",)]
    w["a"].set(AddFlag=False, ClearFlags=False)
    code, _, err = run(capsys, resolve, "clip", "flag-add", "a.mov", "Mint")
    assert code == 1 and "Mint flag" in err and "Added before it" not in err
    code, _, err = run(capsys, resolve, "clip", "flag-clear", "a.mov")
    assert code == 1 and "All flag(s)" in err


# --- clip transcription and AI tools ----------------------------------------------------------


def test_clip_transcribe_stops_at_the_first_failure_and_names_the_done_clips(capsys):
    resolve, w = world()
    code, out, _ = run(capsys, resolve, "clip", "transcribe", "a.mov", "b.mov", "--speakers")
    assert code == 0 and w["a"].called("TranscribeAudio") == [(True,)] and w["b"].called("TranscribeAudio") == [(True,)]
    assert [row["name"] for row in out] == ["a.mov", "b.mov"]
    w["b"].set(TranscribeAudio=False)
    code, _, err = run(capsys, resolve, "clip", "transcribe", "a.mov", "b.mov", "c.wav")
    assert code == 1 and "'b.mov'" in err and "Done before it: a.mov" in err
    assert w["c"].called("TranscribeAudio") == []


def test_clip_transcript_as_json_or_text(capsys):
    resolve, w = world()
    transcription = {"language": "en", "segments": [
        {"start": "01:00:00:00", "end": "01:00:01:00", "text": "Hello", "speaker": "Ann", "words": []},
        {"start": "01:00:01:00", "end": "01:00:02:00", "text": "(...)", "speaker": None, "words": []}]}
    w["a"].set(GetTranscription=transcription)
    code, out, _ = run(capsys, resolve, "clip", "transcript", "a.mov")
    assert code == 0 and w["a"].called("GetTranscription") == [()]
    assert out["ref"] == "clip#clip-a" and out["language"] == "en" and out["segments"] == transcription["segments"]
    code, out, _ = run(capsys, resolve, "clip", "transcript", "a.mov", "--text", "--nested-clip")
    assert out == "Ann: Hello\n(...)" and w["a"].called("GetTranscription")[-1] == (True,)
    w["a"].set(GetTranscription=None)
    code, _, err = run(capsys, resolve, "clip", "transcript", "a.mov")
    assert code == 1 and "no transcription" in err and "dava clip transcribe" in err


def test_clip_transcript_clear_classify_intellisearch_slate(capsys):
    resolve, w = world()
    run(capsys, resolve, "clip", "transcript-clear", "a.mov")
    run(capsys, resolve, "clip", "transcript-clear", "a.mov", "--nested-clip")
    assert w["a"].called("ClearTranscription") == [(), (True,)]
    run(capsys, resolve, "clip", "classify-audio", "a.mov")
    run(capsys, resolve, "clip", "classify-audio-clear", "a.mov")
    assert names(w["a"]).count("PerformAudioClassification") == 1
    assert names(w["a"]).count("ClearAudioClassification") == 1
    run(capsys, resolve, "clip", "intellisearch", "a.mov", "--faces")
    run(capsys, resolve, "clip", "intellisearch", "a.mov", "--better")
    run(capsys, resolve, "clip", "intellisearch", "a.mov", "--faces", "--better")
    # (identifyFaces, isBetterMode)
    assert w["a"].called("AnalyzeForIntellisearch") == [(True, False), (False, True), (True, True)]
    code, _, _ = run(capsys, resolve, "clip", "slate-id", "a.mov", "-c", "Blue")
    assert code == 0 and w["a"].called("AnalyzeForSlate") == [(CONSTANTS["MARKER_BLUE"],)]
    for method, argv in (("ClearTranscription", ("transcript-clear",)), ("PerformAudioClassification", ("classify-audio",)),
                         ("ClearAudioClassification", ("classify-audio-clear",)),
                         ("AnalyzeForIntellisearch", ("intellisearch",)), ("AnalyzeForSlate", ("slate-id", "-c", "Red"))):
        w["a"].set(**{method: False})
        code, _, err = run(capsys, resolve, "clip", *argv, "a.mov")
        assert code == 1 and "'a.mov'" in err, method


def test_clip_deblur_returns_the_new_clip(capsys):
    resolve, w = world()
    new = make_clip("n", "a Deblur.mov")
    w["a"].set(RemoveMotionBlur=new)
    code, out, _ = run(capsys, resolve, "clip", "deblur", "a.mov")
    assert code == 0 and w["a"].called("RemoveMotionBlur") == [({},)]
    assert out["$ref"] == "clip#clip-n" and out["original"] == {"ref": "clip#clip-a", "name": "a.mov"}
    run(capsys, resolve, "clip", "deblur", "a.mov", "--file-name", "x", "--encoder", "Native", "--source-res",
        "--profile", "Main10")
    assert w["a"].called("RemoveMotionBlur")[-1] == ({"FileName": "x", "EncodingProfile": "Main10",
                                                       "Encoder": "Native", "RenderAtSourceRes": True},)
    w["a"].set(RemoveMotionBlur=None)
    code, _, err = run(capsys, resolve, "clip", "deblur", "a.mov")
    assert code == 1 and "motion blur" in err


def test_clip_monitor_growing_and_select(capsys):
    resolve, w = world()
    code, _, _ = run(capsys, resolve, "clip", "monitor-growing", "a.mov")
    assert code == 0 and w["a"].called("MonitorGrowingFile") == [()]
    code, _, _ = run(capsys, resolve, "clip", "select", "b.mov")
    assert code == 0 and w["mediapool"].called("SetSelectedClip") == [(w["b"],)]
    w["mediapool"].set(GetSelectedClips=[w["b"]])
    code, out, _ = run(capsys, resolve, "clip", "selected")
    assert out == [{"ref": "clip#clip-b", "name": "b.mov"}]
    w["a"].set(MonitorGrowingFile=False)
    w["mediapool"].set(SetSelectedClip=False)
    code, _, err = run(capsys, resolve, "clip", "monitor-growing", "a.mov")
    assert code == 1 and "growing file" in err
    code, _, err = run(capsys, resolve, "clip", "select", "a.mov")
    assert code == 1 and "could not select 'a.mov'" in err


# --- stereo, multicam, sync, timelines, mattes ---------------------------------------------


def test_clip_stereo(capsys):
    resolve, w = world()
    stereo = make_clip("s", "b.mov (3D)")
    w["mediapool"].set(CreateStereoClip=stereo)
    code, out, _ = run(capsys, resolve, "clip", "stereo", "b.mov", "c.wav")
    assert code == 0 and w["mediapool"].called("CreateStereoClip") == [(w["b"], w["c"])]
    assert out["$ref"] == "clip#clip-s" and out["left"]["name"] == "b.mov" and out["right"]["name"] == "c.wav"
    code, _, err = run(capsys, resolve, "clip", "stereo", "b.mov", "clip#clip-b")
    assert code == 1 and "two different clips" in err
    w["mediapool"].set(CreateStereoClip=None)
    code, _, err = run(capsys, resolve, "clip", "stereo", "b.mov", "c.wav")
    assert code == 1 and "stereo clip" in err


def test_clip_multicam_maps_options_to_constants(capsys):
    resolve, w = world()
    multicam = make_clip("m", "Multicam 1")
    w["mediapool"].set(CreateMulticamClip=[multicam])
    code, out, _ = run(capsys, resolve, "clip", "multicam", "a.mov", "b.mov", "-n", "MC", "--sync", "audio",
                       "--channel", "2", "--audio", "all", "--angle-names", "clip", "--detect", "reel-name",
                       "--frame-rate", "25", "--start-timecode", "10:00:00:00", "--split-at-gaps",
                       "--full-extents", "--no-source-bin")
    assert code == 0 and out == [{"ref": "clip#clip-m", "name": "Multicam 1"}]
    (clips, options), = w["mediapool"].called("CreateMulticamClip")
    assert clips == [w["a"], w["b"]]
    assert options == {"name": "MC", "startTimecode": "10:00:00:00", "frameRate": 25.0,
                       "angleSyncMode": CONSTANTS["MULTICAM_ANGLE_SYNC_AUDIO"],
                       "multicamAudioMode": CONSTANTS["MULTICAM_AUDIO_ALL"],
                       "angleNameMode": CONSTANTS["MULTICAM_ANGLE_NAME_CLIP"],
                       "detectSameCameraClipsMode": CONSTANTS["MULTICAM_DETECT_BY_REEL_NAME"],
                       "channelConfig": 2, "splitAtGaps": True, "useFullClipExtents": True,
                       "createBinForSourceClips": False}
    run(capsys, resolve, "clip", "multicam", "a.mov", "b.mov", "--channel", "auto")
    assert w["mediapool"].called("CreateMulticamClip")[-1][1] == {
        "channelConfig": CONSTANTS["AUDIO_SYNC_CHANNEL_AUTOMATIC"]}
    assert usage_error(resolve, "clip", "multicam", "a.mov", "--channel", "0") == 2
    assert usage_error(resolve, "clip", "multicam", "a.mov", "--sync", "gps") == 2
    w["mediapool"].set(CreateMulticamClip=[])
    code, _, err = run(capsys, resolve, "clip", "multicam", "a.mov", "b.mov")
    assert code == 1 and "multicam clip from 2 clip(s)" in err


def test_clip_sync_audio(capsys):
    resolve, w = world()
    code, out, _ = run(capsys, resolve, "clip", "sync-audio", "b.mov", "c.wav", "--mode", "waveform", "--channel",
                       "mix", "--keep-embedded-audio", "--keep-video-metadata")
    assert code == 0 and len(out) == 2
    assert w["mediapool"].called("AutoSyncAudio") == [([w["b"], w["c"]], {
        "syncMode": CONSTANTS["AUDIO_SYNC_WAVEFORM"], "channelNumber": CONSTANTS["AUDIO_SYNC_CHANNEL_MIX"],
        "retainEmbeddedAudio": True, "retainVideoMetadata": True})]
    run(capsys, resolve, "clip", "sync-audio", "b.mov", "c.wav")
    assert w["mediapool"].called("AutoSyncAudio")[-1] == ([w["b"], w["c"]], {})
    code, _, err = run(capsys, resolve, "clip", "sync-audio", "b.mov")
    assert code == 1 and "at least one video clip and one audio clip" in err
    w["mediapool"].set(AutoSyncAudio=False)
    code, _, err = run(capsys, resolve, "clip", "sync-audio", "b.mov", "c.wav")
    assert code == 1 and "could not sync audio" in err


def appended(*labels):
    return [Rec(label, GetUniqueId=f"item-{label}", GetName=label, GetStart=86400, GetEnd=86448) for label in labels]


def test_clip_append_builds_append_clip_infos(capsys):
    resolve, w = world()
    w["mediapool"].set(AppendToTimeline=appended("i1", "i2"))
    code, out, _ = run(capsys, resolve, "clip", "append", "a.mov@10:57", "clip#clip-b", "-T", "2", "--video-only")
    assert code == 0
    assert w["mediapool"].called("AppendToTimeline") == [([
        {"mediaPoolItem": w["a"], "startFrame": 10, "endFrame": 57, "trackIndex": 2, "mediaType": 1},
        {"mediaPoolItem": w["b"], "trackIndex": 2, "mediaType": 1}],)]
    assert out[0] == {"ref": "item#item-i1", "name": "i1", "start": 86400, "end": 86448}
    assert w["project"].called("SetCurrentTimeline") == []
    code, _, _ = run(capsys, resolve, "clip", "append", "clip:/Master/Shots/c.wav", "--at", "48", "--audio-only",
                     "-t", "Edit v1")
    assert code == 0 and w["project"].called("SetCurrentTimeline") == [(w["timeline"],)]
    assert w["mediapool"].called("AppendToTimeline")[-1] == ([
        {"mediaPoolItem": w["c"], "mediaType": 2, "recordFrame": 86400 + 48}],)


def test_clip_append_errors(capsys):
    resolve, w = world()
    code, _, err = run(capsys, resolve, "clip", "append", "a.mov", "b.mov", "--at", "0")
    assert code == 1 and "--at places one clip" in err
    code, _, err = run(capsys, resolve, "clip", "append", "a.mov", "--at", "-5")
    assert code == 1 and "0 or more" in err
    code, _, err = run(capsys, resolve, "clip", "append", "a.mov@50:10")
    assert code == 1 and "comes before the start frame" in err
    code, _, err = run(capsys, resolve, "clip", "append", "a.mov", "-t", "Other")
    assert code == 1 and "No timeline named 'Other'" in err
    # An unknown clip is reported before the timeline is switched.
    code, _, err = run(capsys, resolve, "clip", "append", "nope.mov", "-t", "Edit v1")
    assert code == 1 and "No media pool clip named 'nope.mov'" in err
    assert w["project"].called("SetCurrentTimeline") == []
    assert w["mediapool"].called("AppendToTimeline") == []
    assert usage_error(resolve, "clip", "append", "a.mov", "--video-only", "--audio-only") == 2
    assert usage_error(resolve, "clip", "append", "a.mov", "-T", "0") == 2
    w["mediapool"].set(AppendToTimeline=[])
    code, _, err = run(capsys, resolve, "clip", "append", "a.mov")
    assert code == 1 and "could not append" in err and "track 1" in err


def test_clip_new_timeline_from_ranges(capsys):
    resolve, w = world()
    timeline = Rec("tl", GetName="Selects", GetUniqueId="tl-9")
    w["mediapool"].set(CreateTimelineFromClips=timeline)
    code, out, _ = run(capsys, resolve, "clip", "new-timeline", "Selects", "a.mov@0:23", "b.mov")
    assert code == 0
    assert w["mediapool"].called("CreateTimelineFromClips") == [("Selects", [
        {"mediaPoolItem": w["a"], "startFrame": 0, "endFrame": 23}, {"mediaPoolItem": w["b"]}])]
    assert out == {"$type": "Timeline", "$ref": "timeline#tl-9", "name": "Selects", "clips": 2}
    w["mediapool"].set(CreateTimelineFromClips=None)
    code, _, err = run(capsys, resolve, "clip", "new-timeline", "Selects", "a.mov")
    assert code == 1 and "could not create timeline 'Selects'" in err


def test_clip_mattes_add_and_delete(capsys, tmp_path):
    resolve, w = world()
    matte = tmp_path / "m.exr"
    matte.write_text("x")
    w["mediapool"].set(GetClipMatteList=[str(matte)])
    code, out, _ = run(capsys, resolve, "clip", "mattes", "a.mov")
    assert out == [str(matte)] and w["mediapool"].called("GetClipMatteList") == [(w["a"],)]
    code, _, _ = run(capsys, resolve, "clip", "matte-add", "a.mov", str(matte), "--eye", "left")
    assert code == 0 and w["storage"].called("AddClipMattesToMediaPool") == [(w["a"], [str(matte)], "left")]
    run(capsys, resolve, "clip", "matte-add", "a.mov", str(matte))
    assert w["storage"].called("AddClipMattesToMediaPool")[-1] == (w["a"], [str(matte)])
    code, _, _ = run(capsys, resolve, "clip", "matte-delete", "a.mov", str(matte))
    assert code == 0 and w["mediapool"].called("DeleteClipMattes") == [(w["a"], [str(matte)])]
    code, _, err = run(capsys, resolve, "clip", "matte-delete", "a.mov", str(tmp_path / "other.exr"))
    assert code == 1 and "is not a matte of 'a.mov'" in err and str(matte) in err
    assert len(w["mediapool"].called("DeleteClipMattes")) == 1
    w["storage"].set(AddClipMattesToMediaPool=False)
    w["mediapool"].set(DeleteClipMattes=False)
    code, _, err = run(capsys, resolve, "clip", "matte-add", "a.mov", str(matte))
    assert code == 1 and "--eye" in err
    code, _, err = run(capsys, resolve, "clip", "matte-add", "a.mov", str(matte), "--eye", "right")
    assert code == 1 and "could not add 1 matte(s) to 'a.mov'" in err and "--eye" not in err
    code, _, err = run(capsys, resolve, "clip", "matte-delete", "a.mov", str(matte))
    assert code == 1 and "could not delete 1 matte(s)" in err


# --- storage -------------------------------------------------------------------------------


def test_storage_volumes_ls_reveal(capsys, tmp_path):
    resolve, w = world()
    w["storage"].set(GetMountedVolumeList=["/Volumes/Card"], GetSubFolderList=[str(tmp_path / "A")],
                     GetFileList=[str(tmp_path / "f_[001-010].exr")])
    code, out, _ = run(capsys, resolve, "storage", "volumes")
    assert out == ["/Volumes/Card"]
    code, out, _ = run(capsys, resolve, "storage", "ls", str(tmp_path))
    assert out == {"path": str(tmp_path), "folders": [str(tmp_path / "A")], "files": [str(tmp_path / "f_[001-010].exr")]}
    assert w["storage"].called("GetSubFolderList") == [(str(tmp_path),)]
    assert w["storage"].called("GetFileList") == [(str(tmp_path),)]
    code, _, _ = run(capsys, resolve, "storage", "reveal", str(tmp_path))
    assert code == 0 and w["storage"].called("RevealInStorage") == [(str(tmp_path),)]
    code, _, err = run(capsys, resolve, "storage", "ls", str(tmp_path / "gone"))
    assert code == 1 and "Folder not found" in err
    w["storage"].set(GetMountedVolumeList=[], RevealInStorage=False)
    code, _, err = run(capsys, resolve, "storage", "volumes")
    assert code == 1 and "no mounted volumes" in err
    code, _, err = run(capsys, resolve, "storage", "reveal", str(tmp_path))
    assert code == 1 and "could not reveal" in err


def test_storage_add_with_frame_ranges(capsys, tmp_path):
    resolve, w = world()
    odd = tmp_path / "take@1:2.mov"
    odd.write_text("x")
    w["storage"].set(AddItemListToMediaPool=[w["a"]])
    code, out, _ = run(capsys, resolve, "storage", "add", str(tmp_path / "seq_[001-100].exr") + "@5:20", str(odd))
    assert code == 0 and out == [{"ref": "clip#clip-a", "name": "a.mov"}]
    assert w["storage"].called("AddItemListToMediaPool") == [([
        {"media": str(tmp_path / "seq_[001-100].exr"), "startFrame": 5, "endFrame": 20}, {"media": str(odd)}],)]
    w["storage"].set(AddItemListToMediaPool=[])
    code, _, err = run(capsys, resolve, "storage", "add", str(tmp_path))
    assert code == 1 and "added nothing" in err


def test_storage_clone_applies_settings_then_starts_and_waits(capsys, tmp_path):
    resolve, w = world()
    statuses = iter([{"JobStatus": "Cloning", "CompletionPercentage": 10.0},
                     {"JobStatus": "Cloning", "CompletionPercentage": 60.0},
                     {"JobStatus": "Complete", "CompletionPercentage": 100.0}])
    w["storage"].set(GetCloneStatus=lambda: next(statuses))
    code, out, _ = run(capsys, resolve, "storage", "clone", str(tmp_path), "/Volumes/A", "/Volumes/B",
                       "--checksum", "xxh64", "--preserve-folder-name", "--wait", "--interval", "0")
    assert code == 0
    assert names(w["storage"]) == ["SetCloneToolSettings", "StartCloneMedia", "GetCloneStatus", "GetCloneStatus",
                                   "GetCloneStatus"]
    assert w["storage"].called("SetCloneToolSettings") == [({"PreserveFolderName": True,
                                                             "ChecksumType": CONSTANTS["CLONE_CHECKSUM_TYPE_XXH_64"]},)]
    assert w["storage"].called("StartCloneMedia") == [(str(tmp_path), ["/Volumes/A", "/Volumes/B"])]
    assert out["JobStatus"] == "Complete" and out["targets"] == ["/Volumes/A", "/Volumes/B"]


def test_storage_clone_failures(capsys, tmp_path):
    resolve, w = world()
    w["storage"].set(GetCloneStatus={"JobStatus": "Failed", "Error": "disk full"})
    code, _, err = run(capsys, resolve, "storage", "clone", str(tmp_path), "/Volumes/A", "--wait", "--interval", "0")
    assert code == 1 and "Failed: disk full" in err
    assert w["storage"].called("SetCloneToolSettings") == []
    w["storage"].set(GetCloneStatus={"JobStatus": "Cloning", "CompletionPercentage": 5.0})
    code, _, err = run(capsys, resolve, "storage", "clone", str(tmp_path), "/Volumes/A", "--wait", "--interval", "0",
                       "--timeout", "0")
    assert code == 1 and "still running" in err and "clone-stop" in err
    code, _, err = run(capsys, resolve, "storage", "clone", str(tmp_path / "gone"), "/Volumes/A")
    assert code == 1 and "Folder not found" in err
    w["storage"].set(StartCloneMedia=False)
    code, _, err = run(capsys, resolve, "storage", "clone", str(tmp_path), "/Volumes/A")
    assert code == 1 and "did not start cloning" in err
    w["storage"].set(SetCloneToolSettings=False)
    before = len(w["storage"].called("StartCloneMedia"))
    code, _, err = run(capsys, resolve, "storage", "clone", str(tmp_path), "/Volumes/A", "--checksum", "md5")
    assert code == 1 and "rejected the clone settings" in err
    assert len(w["storage"].called("StartCloneMedia")) == before
    assert usage_error(resolve, "storage", "clone", str(tmp_path), "/x", "--preserve-folder-name",
                       "--no-preserve-folder-name") == 2


def test_storage_clone_status_stop_settings(capsys):
    resolve, w = world()
    w["storage"].set(GetCloneStatus={"JobStatus": "Complete", "CompletionPercentage": 100.0})
    code, out, _ = run(capsys, resolve, "storage", "clone-status")
    assert out == {"JobStatus": "Complete", "CompletionPercentage": 100.0}
    code, _, _ = run(capsys, resolve, "storage", "clone-stop")
    assert code == 0 and w["storage"].called("StopCloneMedia") == [()]
    code, out, _ = run(capsys, resolve, "storage", "clone-settings", "--no-preserve-folder-name", "--checksum", "md5")
    assert code == 0 and w["storage"].called("SetCloneToolSettings") == [(
        {"PreserveFolderName": False, "ChecksumType": CONSTANTS["CLONE_CHECKSUM_TYPE_MD5"]},)]
    code, _, err = run(capsys, resolve, "storage", "clone-settings")
    assert code == 1 and "--checksum" in err and len(w["storage"].called("SetCloneToolSettings")) == 1
    w["storage"].set(GetCloneStatus=None, StopCloneMedia=False, SetCloneToolSettings=False)
    code, _, err = run(capsys, resolve, "storage", "clone-status")
    assert code == 1 and "clone status" in err
    code, _, err = run(capsys, resolve, "storage", "clone-stop")
    assert code == 1 and "No clone job" in err
    code, _, err = run(capsys, resolve, "storage", "clone-settings", "--checksum", "md5")
    assert code == 1 and "rejected" in err


# --- media and meta ------------------------------------------------------------------------


def test_media_info(capsys):
    resolve, w = world()
    w["mediapool"].set(GetSelectedClips=[w["b"]])
    code, out, _ = run(capsys, resolve, "media", "info")
    assert out == {"id": "pool-1", "root": {"ref": "folder#folder-root", "name": "Master", "path": "/Master"},
                   "current_folder": {"ref": "folder#folder-shots", "name": "Shots", "path": "/Master/Shots"},
                   "selected": [{"ref": "clip#clip-b", "name": "b.mov"}]}


def test_media_import_sequence(capsys, tmp_path):
    resolve, w = world()
    w["mediapool"].set(ImportMedia=[w["a"]])
    pattern = str(tmp_path / "shot_%04d.exr")
    code, out, _ = run(capsys, resolve, "media", "import-sequence", pattern, "--start", "1", "--end", "48")
    assert code == 0 and out == [{"ref": "clip#clip-a", "name": "a.mov"}]
    assert w["mediapool"].called("ImportMedia") == [([{"FilePath": pattern, "StartIndex": 1, "EndIndex": 48}],)]
    run(capsys, resolve, "media", "import-sequence", pattern)
    assert w["mediapool"].called("ImportMedia")[-1] == ([{"FilePath": pattern}],)
    code, _, err = run(capsys, resolve, "media", "import-sequence", pattern, "--start", "9", "--end", "2")
    assert code == 1 and "comes before" in err
    code, _, err = run(capsys, resolve, "media", "import-sequence", str(tmp_path / "gone" / "f_%04d.exr"))
    assert code == 1 and "Folder not found" in err
    assert len(w["mediapool"].called("ImportMedia")) == 2
    w["mediapool"].set(ImportMedia=[])
    code, _, err = run(capsys, resolve, "media", "import-sequence", pattern)
    assert code == 1 and "imported nothing" in err


def test_media_import_timeline(capsys, tmp_path):
    resolve, w = world()
    edl = tmp_path / "cut.edl"
    edl.write_text("TITLE: cut")
    timeline = Rec("tl", GetName="cut", GetUniqueId="tl-5")
    w["mediapool"].set(ImportTimelineFromFile=timeline)
    code, out, _ = run(capsys, resolve, "media", "import-timeline", str(edl))
    assert code == 0 and out == {"$type": "Timeline", "$ref": "timeline#tl-5", "name": "cut"}
    assert w["mediapool"].called("ImportTimelineFromFile") == [(str(edl),)]
    code, _, _ = run(capsys, resolve, "media", "import-timeline", str(edl), "-n", "Cut 2", "--no-source-clips",
                     "--source-folder", "Shots", "--source-folder", "/Master", "--source-clips-path", str(tmp_path),
                     "--interlace")
    assert w["mediapool"].called("ImportTimelineFromFile")[-1] == (str(edl), {
        "timelineName": "Cut 2", "importSourceClips": False, "sourceClipsPath": str(tmp_path),
        "sourceClipsFolders": [w["shots"], w["root"]], "interlaceProcessing": True})
    w["mediapool"].set(ImportTimelineFromFile=None)
    code, _, err = run(capsys, resolve, "media", "import-timeline", str(edl))
    assert code == 1 and "could not import a timeline" in err
    code, _, err = run(capsys, resolve, "media", "import-timeline", str(tmp_path / "none.xml"))
    assert code == 1 and "File not found" in err


def test_media_delete_timeline(capsys):
    resolve, w = world()
    code, out, _ = run(capsys, resolve, "media", "delete-timeline", "Edit v1")
    assert code == 0 and out == {"deleted": ["Edit v1"]}
    assert w["mediapool"].called("DeleteTimelines") == [([w["timeline"]],)]
    code, _, err = run(capsys, resolve, "media", "delete-timeline", "Nope")
    assert code == 1 and "No timeline named 'Nope'" in err and len(w["mediapool"].called("DeleteTimelines")) == 1
    w["mediapool"].set(DeleteTimelines=False)
    code, _, err = run(capsys, resolve, "media", "delete-timeline", "Edit v1")
    assert code == 1 and "could not delete timeline(s) 'Edit v1'" in err


def test_media_timeline_mattes(capsys, tmp_path):
    resolve, w = world()
    matte = make_clip("tm", "matte.png")
    w["mediapool"].set(GetTimelineMatteList=[matte])
    code, out, _ = run(capsys, resolve, "media", "timeline-mattes", "/Master")
    assert out == [{"ref": "clip#clip-tm", "name": "matte.png"}]
    assert w["mediapool"].called("GetTimelineMatteList") == [(w["root"],)]
    png = tmp_path / "matte.png"
    png.write_text("x")
    w["storage"].set(AddTimelineMattesToMediaPool=[matte])
    code, out, _ = run(capsys, resolve, "media", "timeline-matte-add", str(png))
    assert code == 0 and w["storage"].called("AddTimelineMattesToMediaPool") == [([str(png)],)]
    w["storage"].set(AddTimelineMattesToMediaPool=None)
    code, _, err = run(capsys, resolve, "media", "timeline-matte-add", str(png))
    assert code == 1 and "timeline mattes" in err


def test_meta_export(capsys, tmp_path):
    resolve, w = world()
    csv = tmp_path / "meta.csv"
    code, out, _ = run(capsys, resolve, "meta", "export", str(csv))
    assert code == 0 and out == {"file": str(csv), "clips": "all"}
    assert w["mediapool"].called("ExportMetadata") == [(str(csv),)]
    code, out, _ = run(capsys, resolve, "meta", "export", str(csv), "a.mov", "b.mov")
    assert out["clips"] == 2 and w["mediapool"].called("ExportMetadata")[-1] == (str(csv), [w["a"], w["b"]])
    w["mediapool"].set(ExportMetadata=False)
    code, _, err = run(capsys, resolve, "meta", "export", str(csv))
    assert code == 1 and "could not export clip metadata" in err
    code, _, err = run(capsys, resolve, "meta", "export", str(tmp_path / "no" / "x.csv"))
    assert code == 1 and "Output folder does not exist" in err


def test_folder_list_human_output_is_a_table(capsys):
    resolve, _ = world()
    assert cli.main(["folder", "list"], resolve=resolve) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["kind", "name", "ref", "type"]
    assert lines[2].split() == ["clip", "b.mov", "clip#clip-b", "Video"]
