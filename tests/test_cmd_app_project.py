"""Tests for dava project (settings, folders, import/export, presets), db, cloud, app, layout and keyboard."""

import json
import os

import pytest

from dava import connect
from dava.cli import build_parser, main
from dava.commands import app_project
from dava.connect import ResolveError
from dava.registry import leaf_parsers
from recorder import Rec, rec_resolve

HAS_STUB = os.path.isfile(connect.stub_path() or "")
needs_stub = pytest.mark.skipif(not HAS_STUB, reason="Resolve scripting API definition not installed")

CLOUD_KEY_CONSTANTS = ["CLOUD_SETTING_PROJECT_NAME", "CLOUD_SETTING_PROJECT_MEDIA_PATH", "CLOUD_SETTING_IS_COLLAB",
                       "CLOUD_SETTING_SYNC_MODE", "CLOUD_SETTING_IS_CAMERA_ACCESS"]
NAME, MEDIA, COLLAB, SYNC, CAMERA = 101.0, 102.0, 103.0, 104.0, 105.0
SYNC_NONE, SYNC_PROXY, SYNC_BOTH = 201.0, 202.0, 203.0

LOCAL_DB = {"DbType": "Disk", "DbName": "Local Database"}
STUDIO_DB = {"DbType": "PostgreSQL", "DbName": "Studio", "IpAddress": "10.0.0.2"}
ATTRIBUTES = {
    "Promo": {"lastModifiedDate": "2026-09-01T10:00:00+02:00", "creationDate": "2026-08-01T09:00:00+02:00",
              "notes": "client cut", "liveCollaborationMode": "single_user"},
    "Teaser": {"lastModifiedDate": "2026-09-02T10:00:00+02:00", "creationDate": "2026-08-02T09:00:00+02:00",
               "notes": "", "liveCollaborationMode": "multi_user"},
}
PROJECT_PRESETS = [{"Name": "HD", "Width": 1920, "Height": 1080}, {"Name": "UHD", "Width": 3840, "Height": 2160}]


def session():
    """A recorded Resolve with realistic preset, folder and database listings and numeric resolve.* constants."""
    resolve, parts = rec_resolve()
    # KEYFRAME_MODE_ALL is 0 on purpose: a falsy constant must still be usable.
    resolve.KEYFRAME_MODE_ALL, resolve.KEYFRAME_MODE_COLOR, resolve.KEYFRAME_MODE_SIZING = 0, 1, 2
    for value, name in zip((NAME, MEDIA, COLLAB, SYNC, CAMERA), CLOUD_KEY_CONSTANTS):
        setattr(resolve, name, value)
    resolve.CLOUD_SYNC_NONE, resolve.CLOUD_SYNC_PROXY_ONLY, resolve.CLOUD_SYNC_PROXY_AND_ORIG = (
        SYNC_NONE, SYNC_PROXY, SYNC_BOTH)
    resolve.set(GetLayoutPresetList=["Edit", "Color"], GetKeyboardPresetList=["DaVinci Resolve", "Mine"],
                GetCurrentKeyboardPreset="Mine", GetUserPreferencesPresetList=["Default"],
                GetVersion=[21, 1, 0, 5, ""], IsStudio=False, GetKeyframeMode=1,
                DisableBackgroundTasksForCurrentResolveSession=None, ValidateDCTL=None)
    parts["pm"].set(GetCurrentFolder="Clients", GetFolderListInCurrentFolder=["2025", "2026"],
                    GetProjectListInCurrentFolder=["Promo", "Teaser"],
                    GetProjectAttributesInCurrentFolder=ATTRIBUTES, GetProjectLastModifiedTime=1756713600,
                    GetDatabaseList=[LOCAL_DB, STUDIO_DB], GetCurrentDatabase=LOCAL_DB)
    parts["project"].set(GetProjectSettingsPresetList=PROJECT_PRESETS)
    parts["resolve"] = resolve
    return resolve, parts


def run(resolve, capsys, *argv):
    code = main(["--json", *argv], resolve=resolve)
    captured = capsys.readouterr()
    out = json.loads(captured.out) if captured.out.strip() else None
    return code, out, captured.err


def cmd_id(value):
    """Test id for a parametrized argv list; other parameters get pytest's default id."""
    if isinstance(value, list) and all(isinstance(word, str) for word in value):
        return " ".join(value)
    return value if isinstance(value, str) else None


def names(rec):
    return [name for name, _ in rec.calls]


def all_calls(parts):
    return [name for key in ("resolve", "pm", "project") for name in names(parts[key])]


# --- registration and read-only declarations ---------------------------------------------

READ_ONLY_COMMANDS = {
    "project info", "project attributes", "project folders", "project preset-list",
    "db list", "db current", "app info", "app prefs-list", "layout list", "keyboard list", "keyboard current",
}


def own_leaves():
    return {" ".join(words): leaf for words, leaf, _ in leaf_parsers(build_parser())
            if getattr(leaf._defaults.get("func"), "__module__", "") == app_project.__name__}


def test_every_command_declares_read_only_and_only_reads_are_marked_read_only():
    leaves = own_leaves()
    assert {"db", "cloud", "app", "layout", "keyboard"} <= {words.split()[0] for words in leaves}
    for words, leaf in leaves.items():
        assert "read_only" in leaf._defaults, words
    declared = {words for words, leaf in leaves.items() if leaf._defaults["read_only"] is True}
    assert declared == READ_ONLY_COMMANDS
    assert callable(leaves["app keyframe-mode"]._defaults["read_only"])


READ_ONLY_RUNS = [
    ["project", "info"], ["project", "info", "Teaser"], ["project", "attributes"], ["project", "folders"],
    ["project", "preset-list"], ["db", "list"], ["db", "current"], ["app", "info"], ["app", "keyframe-mode"],
    ["app", "prefs-list"], ["layout", "list"], ["keyboard", "list"], ["keyboard", "current"],
]


@pytest.mark.parametrize("argv", READ_ONLY_RUNS, ids=" ".join)
def test_read_only_commands_run_in_read_only_mode_and_call_only_getters(capsys, argv):
    resolve, parts = session()
    code, _, err = run(resolve, capsys, "--read-only", *argv)
    assert code == 0, err
    calls = all_calls(parts)
    assert calls
    assert [name for name in calls if not name.startswith(("Get", "Is", "Has"))] == []


def test_read_only_mode_refuses_changes_before_calling_resolve(capsys):
    resolve, parts = session()
    code, _, err = run(resolve, capsys, "--read-only", "project", "delete", "Teaser")
    assert code == 1 and "read-only mode is on" in err
    code, _, err = run(resolve, capsys, "--read-only", "app", "keyframe-mode", "sizing")
    assert code == 1 and "read-only mode is on" in err
    assert "DeleteProject" not in names(parts["pm"]) and "SetKeyframeMode" not in names(resolve)


# --- one-name commands: presets, folders, rename, delete ---------------------------------

NAME_COMMANDS = [
    (["project", "preset-apply"], "project", "SetProjectSettingsPreset"),
    (["project", "preset-save"], "project", "SaveCurrentProjectSettingsAsNewPreset"),
    (["project", "preset-update"], "project", "UpdateProjectSettingsPreset"),
    (["project", "preset-delete"], "project", "DeleteProjectSettingsPreset"),
    (["project", "rename"], "project", "SetName"),
    (["project", "delete"], "pm", "DeleteProject"),
    (["project", "mkdir"], "pm", "CreateFolder"),
    (["project", "rmdir"], "pm", "DeleteFolder"),
    (["app", "prefs-load"], "resolve", "LoadUserPreferencesPreset"),
    (["app", "prefs-save"], "resolve", "SaveUserPreferencesPreset"),
    (["app", "prefs-delete"], "resolve", "DeleteUserPreferencesPreset"),
    (["layout", "load"], "resolve", "LoadLayoutPreset"),
    (["layout", "save"], "resolve", "SaveLayoutPreset"),
    (["layout", "update"], "resolve", "UpdateLayoutPreset"),
    (["layout", "delete"], "resolve", "DeleteLayoutPreset"),
    (["keyboard", "load"], "resolve", "LoadKeyboardPreset"),
    (["keyboard", "delete"], "resolve", "DeleteKeyboardPreset"),
]


@pytest.mark.parametrize("argv, target, method", NAME_COMMANDS, ids=cmd_id)
def test_name_commands_pass_the_name_and_report_it(capsys, argv, target, method):
    resolve, parts = session()
    code, out, err = run(resolve, capsys, *argv, "Night Grade")
    assert code == 0, err
    assert parts[target].called(method) == [("Night Grade",)]
    assert "Night Grade" in out


@pytest.mark.parametrize("argv, target, method", NAME_COMMANDS, ids=cmd_id)
def test_name_commands_fail_when_resolve_returns_false(capsys, argv, target, method):
    resolve, parts = session()
    parts[target].set(**{method: False})
    code, out, err = run(resolve, capsys, *argv, "Night Grade")
    assert code == 1 and out is None
    assert "'Night Grade'" in err


@pytest.mark.parametrize("argv, available", [
    (["project", "preset-apply"], "HD, UHD"), (["project", "preset-delete"], "HD, UHD"),
    (["app", "prefs-load"], "Default"), (["layout", "load"], "Edit, Color"), (["layout", "delete"], "Edit, Color"),
    (["keyboard", "load"], "DaVinci Resolve, Mine"), (["project", "rmdir"], "2025, 2026"),
    (["project", "delete"], "Promo, Teaser"),
], ids=cmd_id)
def test_unknown_names_list_the_available_ones(capsys, argv, available):
    resolve, parts = session()
    for rec in (resolve, parts["pm"], parts["project"]):
        rec.set(**{method: False for method in ("SetProjectSettingsPreset", "DeleteProjectSettingsPreset",
                                                "LoadUserPreferencesPreset", "LoadLayoutPreset",
                                                "DeleteLayoutPreset", "LoadKeyboardPreset", "DeleteFolder",
                                                "DeleteProject")})
    code, _, err = run(resolve, capsys, *argv, "Missing")
    assert code == 1
    assert f"available: {available}." in err


def test_deleting_the_open_project_says_to_close_it_first(capsys):
    resolve, parts = session()
    parts["pm"].set(DeleteProject=False)
    code, _, err = run(resolve, capsys, "project", "delete", "Promo")
    assert code == 1 and "'dava project close' first" in err
    assert "available:" not in err  # the name exists; only the open state is the problem


# --- file commands: export (NAME FILE) and import (FILE [-n NAME]) -----------------------

EXPORT_COMMANDS = [
    (["project", "preset-export"], "project", "ExportProjectSettingsPreset"),
    (["app", "prefs-export"], "resolve", "ExportUserPreferencesPreset"),
    (["layout", "export"], "resolve", "ExportLayoutPreset"),
    (["keyboard", "export"], "resolve", "ExportKeyboardPreset"),
]


@pytest.mark.parametrize("argv, target, method", EXPORT_COMMANDS, ids=cmd_id)
def test_exports_pass_name_and_absolute_path_and_create_the_folder(capsys, tmp_path, monkeypatch, argv, target,
                                                                   method):
    monkeypatch.chdir(tmp_path)
    resolve, parts = session()
    code, _, err = run(resolve, capsys, *argv, "Mine", os.path.join("out", "mine.preset"))
    assert code == 0, err
    assert parts[target].called(method) == [("Mine", str(tmp_path / "out" / "mine.preset"))]
    assert (tmp_path / "out").is_dir()
    parts[target].set(**{method: False})
    code, _, err = run(resolve, capsys, *argv, "Mine", str(tmp_path / "x.preset"))
    assert code == 1 and "'Mine'" in err


IMPORT_COMMANDS = [
    (["project", "preset-import"], "project", "ImportProjectSettingsPreset"),
    (["app", "prefs-import"], "resolve", "ImportUserPreferencesPreset"),
    (["layout", "import"], "resolve", "ImportLayoutPreset"),
    (["keyboard", "import"], "resolve", "ImportKeyboardPreset"),
    (["project", "import"], "pm", "ImportProject"),
]


@pytest.mark.parametrize("argv, target, method", IMPORT_COMMANDS, ids=cmd_id)
def test_imports_pass_the_name_only_when_given(capsys, tmp_path, argv, target, method):
    path = tmp_path / "in.file"
    path.write_text("x")
    resolve, parts = session()
    code, _, err = run(resolve, capsys, *argv, str(path))
    assert code == 0, err
    code, _, err = run(resolve, capsys, *argv, str(path), "-n", "Renamed")
    assert code == 0, err
    assert parts[target].called(method) == [(str(path),), (str(path), "Renamed")]


@pytest.mark.parametrize("argv, target, method", IMPORT_COMMANDS, ids=cmd_id)
def test_imports_check_the_file_and_the_result(capsys, tmp_path, argv, target, method):
    resolve, parts = session()
    for missing in (tmp_path / "nope.file", tmp_path):  # a missing file, and a folder instead of a file
        code, _, err = run(resolve, capsys, *argv, str(missing))
        assert code == 1 and "is not a file" in err
    assert parts[target].called(method) == []
    path = tmp_path / "in.file"
    path.write_text("x")
    parts[target].set(**{method: False})
    code, _, err = run(resolve, capsys, *argv, str(path))
    assert code == 1 and str(path) in err


def test_project_restore_takes_the_dra_archive_folder(capsys, tmp_path):
    # Resolve writes a .dra archive as a folder; restore must accept it and pass its absolute path.
    archive = tmp_path / "promo.dra"
    archive.mkdir()
    resolve, parts = session()
    code, out, err = run(resolve, capsys, "project", "restore", str(archive))
    assert code == 0, err
    code, out, err = run(resolve, capsys, "project", "restore", str(archive), "-n", "Promo 2")
    assert code == 0 and out == f"Restored project from {archive} as Promo 2"
    assert parts["pm"].called("RestoreProject") == [(str(archive),), (str(archive), "Promo 2")]
    code, _, err = run(resolve, capsys, "project", "restore", str(tmp_path / "missing.dra"))
    assert code == 1 and "does not exist" in err
    assert len(parts["pm"].called("RestoreProject")) == 2
    parts["pm"].set(RestoreProject=False)
    code, _, err = run(resolve, capsys, "project", "restore", str(archive))
    assert code == 1 and f"could not restore project archive {str(archive)!r}" in err


def test_outputs_fail_cleanly_when_the_folder_cannot_be_created(capsys, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the output folder should be")
    resolve, parts = session()
    code, out, err = run(resolve, capsys, "project", "export", str(blocker / "promo.drp"))
    assert code == 1 and out is None
    assert f"Cannot create output folder {str(blocker)!r}" in err
    code, _, err = run(resolve, capsys, "layout", "export", "Edit", str(blocker / "edit.preset"))
    assert code == 1 and "Cannot create output folder" in err
    assert parts["pm"].called("ExportProject") == [] and resolve.called("ExportLayoutPreset") == []


@pytest.mark.parametrize("argv, method", [
    (["project", "export", "OUT", "-n", "Nope"], "ExportProject"),
    (["project", "archive", "OUT", "-n", "Nope"], "ArchiveProject"),
], ids=cmd_id)
def test_export_and_archive_failures_list_the_projects_of_the_folder(capsys, tmp_path, argv, method):
    resolve, parts = session()
    parts["pm"].set(**{method: False})
    code, _, err = run(resolve, capsys, *[str(tmp_path / "out") if word == "OUT" else word for word in argv])
    assert code == 1
    assert "No project in folder 'Clients' is named 'Nope'; available: Promo, Teaser." in err


def test_project_export_defaults_to_the_open_project_with_stills_and_luts(capsys, tmp_path):
    resolve, parts = session()
    path = str(tmp_path / "promo.drp")
    assert run(resolve, capsys, "project", "export", path)[0] == 0
    assert run(resolve, capsys, "project", "export", path, "-n", "Teaser", "--no-stills-luts")[0] == 0
    assert parts["pm"].called("ExportProject") == [("Promo", path, True), ("Teaser", path, False)]
    parts["pm"].set(ExportProject=False)
    code, _, err = run(resolve, capsys, "project", "export", path)
    assert code == 1 and "'Promo'" in err


def test_project_archive_maps_flags_to_the_three_booleans(capsys, tmp_path):
    resolve, parts = session()
    path = str(tmp_path / "promo.dra")
    assert run(resolve, capsys, "project", "archive", path)[0] == 0
    assert run(resolve, capsys, "project", "archive", path, "-n", "Teaser", "--no-media", "--proxy-media")[0] == 0
    assert run(resolve, capsys, "project", "archive", path, "--no-render-cache")[0] == 0
    assert parts["pm"].called("ArchiveProject") == [("Promo", path, True, True, False),
                                                   ("Teaser", path, False, True, True),
                                                   ("Promo", path, True, False, False)]
    parts["pm"].set(ArchiveProject=False)
    code, _, err = run(resolve, capsys, "project", "archive", path)
    assert code == 1 and "could not archive" in err


# --- project settings ----------------------------------------------------------------------


@needs_stub
def test_project_set_sends_one_typed_key_per_call_in_order(capsys):
    resolve, parts = session()
    code, out, err = run(resolve, capsys, "project", "set", "timelineResolutionWidth=3840", "timelineFrameRate=25")
    assert code == 0, err
    assert parts["project"].called("SetSettings") == [({"timelineResolutionWidth": "3840"},),
                                                      ({"timelineFrameRate": "25"},)]
    assert out == {"timelineResolutionWidth": "3840", "timelineFrameRate": "25"}


@needs_stub
def test_project_set_names_the_rejected_and_the_applied_keys(capsys):
    resolve, parts = session()
    parts["project"].set(SetSettings=lambda settings: "timelineFrameRate" not in settings)
    code, out, err = run(resolve, capsys, "project", "set", "timelineResolutionWidth=3840", "timelineFrameRate=31")
    assert code == 1 and out is None
    assert "timelineFrameRate='31'" in err and "(applied: timelineResolutionWidth)" in err


@needs_stub
def test_project_set_refuses_unknown_keys_before_calling_resolve(capsys):
    resolve, parts = session()
    code, _, err = run(resolve, capsys, "project", "set", "timelineFrameRat=25")
    assert code == 1 and "Unknown ProjectSettings key(s): timelineFrameRat" in err
    code, _, err = run(resolve, capsys, "project", "set", "timelineFrameRate")
    assert code == 1 and "Expected KEY=VALUE" in err
    assert parts["project"].called("SetSettings") == []


@needs_stub
def test_project_set_unchecked_passes_unknown_keys_as_text(capsys):
    resolve, parts = session()
    code, _, err = run(resolve, capsys, "project", "set", "--unchecked", "newKey21=1", "timelineFrameRate=24")
    assert code == 0, err
    assert parts["project"].called("SetSettings") == [({"newKey21": "1"},), ({"timelineFrameRate": "24"},)]


# --- project info, attributes, close -------------------------------------------------------


def test_project_info_of_the_open_project(capsys):
    resolve, parts = session()
    code, out, _ = run(resolve, capsys, "project", "info")
    assert code == 0
    assert out == {"name": "Promo", "folder": "Clients", "open": True, "uniqueId": "project-1",
                   "lastModifiedTime": 1756713600, **ATTRIBUTES["Promo"]}
    assert parts["pm"].called("GetProjectLastModifiedTime") == [("Promo",)]


def test_project_info_of_another_project_and_of_a_missing_one(capsys):
    resolve, parts = session()
    code, out, _ = run(resolve, capsys, "project", "info", "Teaser")
    assert code == 0 and out["open"] is False and "uniqueId" not in out
    assert out["liveCollaborationMode"] == "multi_user"
    code, _, err = run(resolve, capsys, "project", "info", "Nope")
    assert code == 1 and "No project named 'Nope' in project folder 'Clients'" in err


def test_project_info_needs_a_name_when_no_project_is_open(capsys):
    resolve, parts = session()
    parts["pm"].set(GetCurrentProject=None)
    code, _, err = run(resolve, capsys, "project", "info")
    assert code == 1 and "No project is open" in err
    parts["pm"].set(GetProjectAttributesInCurrentFolder=None)
    code, _, err = run(resolve, capsys, "project", "info", "Promo")
    assert code == 1 and "did not return the project attributes" in err


def test_project_attributes_are_rows(capsys):
    resolve, _ = session()
    code, out, _ = run(resolve, capsys, "project", "attributes")
    assert code == 0
    assert out == [{"name": "Promo", **ATTRIBUTES["Promo"]}, {"name": "Teaser", **ATTRIBUTES["Teaser"]}]


def test_project_close_saves_only_when_asked(capsys):
    resolve, parts = session()
    code, out, _ = run(resolve, capsys, "project", "close")
    assert code == 0 and out == "Closed project Promo without saving"
    assert names(parts["pm"]) == ["GetCurrentProject", "CloseProject"]
    assert parts["pm"].called("CloseProject") == [(parts["project"],)]
    resolve, parts = session()
    code, out, _ = run(resolve, capsys, "project", "close", "--save")
    assert code == 0 and out == "Closed project Promo"
    assert names(parts["pm"]) == ["GetCurrentProject", "SaveProject", "CloseProject"]


def test_project_close_stops_when_saving_or_closing_fails(capsys):
    resolve, parts = session()
    parts["pm"].set(SaveProject=False)
    code, _, err = run(resolve, capsys, "project", "close", "--save")
    assert code == 1 and "Could not save project 'Promo'" in err
    assert "CloseProject" not in names(parts["pm"])
    parts["pm"].set(CloseProject=False)
    code, _, err = run(resolve, capsys, "project", "close")
    assert code == 1 and "Could not close project 'Promo'" in err


# --- project folders ----------------------------------------------------------------------


def test_project_folders_lists_the_current_folder(capsys):
    resolve, _ = session()
    code, out, _ = run(resolve, capsys, "project", "folders")
    assert code == 0
    assert out == {"folder": "Clients", "folders": ["2025", "2026"], "projects": ["Promo", "Teaser"]}


def test_project_cd_walks_absolute_and_relative_paths(capsys):
    resolve, parts = session()
    code, out, _ = run(resolve, capsys, "project", "cd", "/Clients/2026")
    assert code == 0 and out["folder"] == "Clients"
    nav = [(name, args) for name, args in parts["pm"].calls if name in ("GotoRootFolder", "OpenFolder",
                                                                        "GotoParentFolder")]
    assert nav == [("GotoRootFolder", ()), ("OpenFolder", ("Clients",)), ("OpenFolder", ("2026",))]
    parts["pm"].calls.clear()
    assert run(resolve, capsys, "project", "cd", "../2025/.")[0] == 0
    nav = [(name, args) for name, args in parts["pm"].calls if name in ("GotoRootFolder", "OpenFolder",
                                                                        "GotoParentFolder")]
    assert nav == [("GotoParentFolder", ()), ("OpenFolder", ("2025",))]


def test_project_cd_reports_where_it_stopped(capsys):
    resolve, parts = session()
    parts["pm"].set(OpenFolder=lambda name: name != "Nope")
    code, _, err = run(resolve, capsys, "project", "cd", "2026/Nope/Deeper")
    assert code == 1
    assert "Could not open project folder 'Nope' in 'Clients' (stopped there)" in err
    assert "available: 2025, 2026" in err
    assert parts["pm"].called("OpenFolder") == [("2026",), ("Nope",)]
    parts["pm"].set(GotoParentFolder=False)
    code, _, err = run(resolve, capsys, "project", "cd", "..")
    assert code == 1 and "has no parent folder" in err


# --- db -------------------------------------------------------------------------------------


def test_db_list_marks_the_current_database(capsys):
    resolve, _ = session()
    code, out, _ = run(resolve, capsys, "db", "list")
    assert code == 0
    assert out == [{**LOCAL_DB, "current": True}, {**STUDIO_DB, "current": False}]
    code, out, _ = run(resolve, capsys, "db", "current")
    assert code == 0 and out == LOCAL_DB


def test_db_use_passes_the_listed_database_info(capsys):
    resolve, parts = session()
    code, out, _ = run(resolve, capsys, "db", "use", "Studio")
    assert code == 0 and "Studio (PostgreSQL at 10.0.0.2)" in out
    assert parts["pm"].called("SetCurrentDatabase") == [(STUDIO_DB,)]


def test_db_use_needs_type_for_ambiguous_or_unlisted_databases(capsys):
    resolve, parts = session()
    disk_studio = {"DbType": "Disk", "DbName": "Studio"}
    parts["pm"].set(GetDatabaseList=[LOCAL_DB, STUDIO_DB, disk_studio])
    code, _, err = run(resolve, capsys, "db", "use", "Studio")
    assert code == 1 and "2 databases match 'Studio'" in err and "Add --type or --ip" in err
    assert run(resolve, capsys, "db", "use", "Studio", "--type", "Disk")[0] == 0
    code, _, err = run(resolve, capsys, "db", "use", "Archive")
    assert code == 1 and "No database named 'Archive'" in err and "pass --type" in err
    assert run(resolve, capsys, "db", "use", "Archive", "--type", "PostgreSQL", "--ip", "10.0.0.9")[0] == 0
    assert parts["pm"].called("SetCurrentDatabase") == [
        (disk_studio,), ({"DbType": "PostgreSQL", "DbName": "Archive", "IpAddress": "10.0.0.9"},)]


def test_db_use_fails_when_resolve_refuses(capsys):
    resolve, parts = session()
    parts["pm"].set(SetCurrentDatabase=False)
    code, _, err = run(resolve, capsys, "db", "use", "Local Database")
    assert code == 1 and "could not switch to database Local Database (Disk)" in err
    with pytest.raises(SystemExit) as exc:
        main(["db", "use", "X", "--type", "MySQL"], resolve=resolve)
    assert exc.value.code == 2


# --- cloud ------------------------------------------------------------------------------------


def test_cloud_create_keys_settings_by_the_resolve_constants(capsys, tmp_path):
    resolve, parts = session()
    parts["pm"].set(CreateCloudProject=parts["project"])
    code, out, err = run(resolve, capsys, "cloud", "create", "-n", "Show", "-m", str(tmp_path), "--sync", "proxy",
                         "--collab", "--camera-access")
    assert code == 0, err
    assert parts["pm"].called("CreateCloudProject") == [
        ({NAME: "Show", MEDIA: str(tmp_path), COLLAB: True, SYNC: SYNC_PROXY, CAMERA: True},)]
    assert out == {"$type": "Project", "name": "Promo", "$ref": "project"}


def test_cloud_create_leaves_out_unset_options_and_fails_on_none(capsys, tmp_path):
    resolve, parts = session()
    parts["pm"].set(CreateCloudProject=None)
    code, _, err = run(resolve, capsys, "cloud", "create", "-n", "Show", "-m", str(tmp_path))
    assert code == 1 and "could not create cloud project 'Show'" in err
    assert parts["pm"].called("CreateCloudProject") == [({NAME: "Show", MEDIA: str(tmp_path)},)]
    with pytest.raises(SystemExit) as exc:
        main(["cloud", "create", "-m", str(tmp_path)], resolve=resolve)
    assert exc.value.code == 2


def test_cloud_open_only_takes_the_keys_resolve_honours(capsys, tmp_path):
    resolve, parts = session()
    parts["pm"].set(LoadCloudProject=parts["project"])
    code, _, err = run(resolve, capsys, "cloud", "open", "-n", "Show", "-m", str(tmp_path), "--sync", "none")
    assert code == 0, err
    assert parts["pm"].called("LoadCloudProject") == [({NAME: "Show", MEDIA: str(tmp_path), SYNC: SYNC_NONE},)]
    with pytest.raises(SystemExit) as exc:
        main(["cloud", "open", "-n", "Show", "-m", str(tmp_path), "--collab"], resolve=resolve)
    assert exc.value.code == 2
    parts["pm"].set(LoadCloudProject=None)
    code, _, err = run(resolve, capsys, "cloud", "open", "-n", "Show", "-m", str(tmp_path))
    assert code == 1 and "could not open cloud project 'Show'" in err


def test_cloud_import_and_restore_pass_path_then_settings(capsys, tmp_path):
    resolve, parts = session()
    drp = tmp_path / "show.drp"
    drp.write_text("x")
    media = str(tmp_path / "media")
    assert run(resolve, capsys, "cloud", "import", str(drp), "-m", media, "--sync", "proxy-and-original")[0] == 0
    assert parts["pm"].called("ImportCloudProject") == [(str(drp), {MEDIA: media, SYNC: SYNC_BOTH})]
    assert run(resolve, capsys, "cloud", "restore", str(tmp_path), "-m", media, "-n", "Show")[0] == 0
    assert parts["pm"].called("RestoreCloudProject") == [(str(tmp_path), {NAME: "Show", MEDIA: media})]
    parts["pm"].set(ImportCloudProject=False, RestoreCloudProject=False)
    code, _, err = run(resolve, capsys, "cloud", "import", str(drp), "-m", media)
    assert code == 1 and "could not import" in err
    code, _, err = run(resolve, capsys, "cloud", "restore", str(tmp_path), "-m", media)
    assert code == 1 and "could not restore" in err
    code, _, err = run(resolve, capsys, "cloud", "restore", str(drp), "-m", media)
    assert code == 1 and "is not a folder" in err
    assert len(parts["pm"].called("RestoreCloudProject")) == 2


def test_missing_constants_are_an_actionable_error():
    with pytest.raises(ResolveError, match="no constant resolve.CLOUD_SYNC_NONE"):
        app_project._constant(object(), "CLOUD_SYNC_NONE")


# --- app ------------------------------------------------------------------------------------------


def test_app_info(capsys):
    resolve, _ = session()
    code, out, _ = run(resolve, capsys, "app", "info")
    assert code == 0
    assert out == {"product": "DaVinci Resolve Studio", "version": "21.1.0", "versionFields": [21, 1, 0, 5, ""],
                   "studio": False}


def test_app_keyframe_mode_shows_and_sets_by_name(capsys):
    resolve, _ = session()
    code, out, _ = run(resolve, capsys, "app", "keyframe-mode")
    assert code == 0 and out == {"mode": "color", "value": 1}
    code, out, _ = run(resolve, capsys, "app", "keyframe-mode", "all")
    assert code == 0 and out == {"mode": "all", "value": 0}
    assert run(resolve, capsys, "app", "keyframe-mode", "sizing")[0] == 0
    assert resolve.called("SetKeyframeMode") == [(0,), (2,)]
    resolve.set(SetKeyframeMode=False)
    code, _, err = run(resolve, capsys, "app", "keyframe-mode", "color")
    assert code == 1 and "Could not set the keyframe mode to color" in err
    with pytest.raises(SystemExit) as exc:
        main(["app", "keyframe-mode", "fast"], resolve=resolve)
    assert exc.value.code == 2


def test_app_priority_and_background_tasks(capsys):
    resolve, _ = session()
    assert run(resolve, capsys, "app", "priority", "high")[0] == 0
    assert run(resolve, capsys, "app", "priority", "normal")[0] == 0
    assert resolve.called("SetHighPriority") == [(True,), (False,)]
    resolve.set(SetHighPriority=False)
    code, _, err = run(resolve, capsys, "app", "priority", "high")
    assert code == 1 and "Could not set the script priority" in err
    code, out, _ = run(resolve, capsys, "app", "disable-background-tasks")
    assert code == 0 and "disabled" in out
    resolve.set(DisableBackgroundTasksForCurrentResolveSession=False)
    code, _, err = run(resolve, capsys, "app", "disable-background-tasks")
    assert code == 1 and "refused" in err


def test_app_dctl_validate_file_or_code(capsys, tmp_path):
    resolve, _ = session()
    dctl = tmp_path / "look.dctl"
    dctl.write_text("__DEVICE__ float3 transform(...) { return rgb; }")
    code, out, _ = run(resolve, capsys, "app", "dctl-validate", str(dctl))
    assert code == 0 and out == {"dctl": str(dctl), "valid": True}
    assert run(resolve, capsys, "app", "dctl-validate", "--code", "bad code")[0] == 0
    assert resolve.called("ValidateDCTL") == [(dctl.read_text(),), ("bad code",)]
    resolve.set(ValidateDCTL="line 1: syntax error")
    code, _, err = run(resolve, capsys, "app", "dctl-validate", "--code", "bad code")
    assert code == 1 and "did not validate: line 1: syntax error" in err


def test_app_dctl_validate_needs_exactly_one_source(capsys, tmp_path):
    resolve, _ = session()
    for argv in (["app", "dctl-validate"], ["app", "dctl-validate", "x.dctl", "--code", "y"]):
        with pytest.raises(SystemExit) as exc:
            main(argv, resolve=resolve)
        assert exc.value.code == 2
    code, _, err = run(resolve, capsys, "app", "dctl-validate", str(tmp_path / "missing.dctl"))
    assert code == 1 and "does not exist" in err
    binary = tmp_path / "binary.dctl"
    binary.write_bytes(b"\xff\xfe\x00bad")
    code, _, err = run(resolve, capsys, "app", "dctl-validate", str(binary))
    assert code == 1 and f"Cannot read DCTL file {str(binary)!r} as UTF-8 text" in err
    assert resolve.called("ValidateDCTL") == []


def test_app_dctl_encrypt_passes_only_given_options(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    resolve, _ = session()
    dctl = tmp_path / "look.dctl"
    dctl.write_text("x")
    assert run(resolve, capsys, "app", "dctl-encrypt", "look.dctl")[0] == 0
    assert run(resolve, capsys, "app", "dctl-encrypt", "look.dctl", "-n", "look_enc", "--expiry", "2027-12-31",
               "-o", "out")[0] == 0
    assert resolve.called("EncryptDCTL") == [
        (str(dctl),),
        (str(dctl), {"Name": "look_enc", "Expiry": "2027-12-31", "OutputFolder": str(tmp_path / "out")})]
    resolve.set(EncryptDCTL=False)
    code, _, err = run(resolve, capsys, "app", "dctl-encrypt", "look.dctl")
    assert code == 1 and "could not encrypt DCTL" in err


# --- plain listings -----------------------------------------------------------------------------

LISTINGS = [
    (["project", "preset-list"], "project", "GetProjectSettingsPresetList", PROJECT_PRESETS),
    (["app", "prefs-list"], "resolve", "GetUserPreferencesPresetList", ["Default"]),
    (["layout", "list"], "resolve", "GetLayoutPresetList", ["Edit", "Color"]),
    (["keyboard", "list"], "resolve", "GetKeyboardPresetList", ["DaVinci Resolve", "Mine"]),
    (["keyboard", "current"], "resolve", "GetCurrentKeyboardPreset", "Mine"),
]


@pytest.mark.parametrize("argv, target, method, expected", LISTINGS, ids=cmd_id)
def test_listings_return_what_resolve_returns_and_fail_on_none(capsys, argv, target, method, expected):
    resolve, parts = session()
    code, out, _ = run(resolve, capsys, *argv)
    assert code == 0 and out == expected
    assert parts[target].called(method) == [()]
    parts[target].set(**{method: None})
    code, _, err = run(resolve, capsys, *argv)
    assert code == 1 and "Resolve did not return" in err


GETTER_FAILURES = [
    (["project", "attributes"], "pm", "GetProjectAttributesInCurrentFolder", "the project attributes"),
    (["project", "folders"], "pm", "GetFolderListInCurrentFolder", "the folder list"),
    (["project", "folders"], "pm", "GetProjectListInCurrentFolder", "the project list"),
    (["db", "list"], "pm", "GetDatabaseList", "the database list"),
    (["db", "current"], "pm", "GetCurrentDatabase", "the current database"),
    (["app", "keyframe-mode"], "resolve", "GetKeyframeMode", "the keyframe mode"),
    (["app", "info"], "resolve", "GetProductName", "the product name"),
    (["app", "info"], "resolve", "GetVersionString", "the version string"),
    (["app", "info"], "resolve", "GetVersion", "the version fields"),
    (["app", "info"], "resolve", "IsStudio", "whether this is Resolve Studio"),
]


@pytest.mark.parametrize("argv, target, method, what", GETTER_FAILURES, ids=cmd_id)
def test_reads_fail_when_resolve_returns_none(capsys, argv, target, method, what):
    resolve, parts = session()
    parts[target].set(**{method: None})
    code, out, err = run(resolve, capsys, *argv)
    assert code == 1 and out is None
    assert f"Resolve did not return {what}" in err


def test_reset_intellisearch(capsys):
    resolve, parts = session()
    assert run(resolve, capsys, "project", "reset-intellisearch")[0] == 0
    assert parts["project"].called("ResetIntellisearchAnalysis") == [()]
    parts["project"].set(ResetIntellisearchAnalysis=False)
    code, _, err = run(resolve, capsys, "project", "reset-intellisearch")
    assert code == 1 and "Could not reset the IntelliSearch analysis of project 'Promo'" in err
