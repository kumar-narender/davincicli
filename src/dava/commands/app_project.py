"""dava project/db/cloud/app/layout/keyboard: projects and project folders, databases, cloud projects,
project settings and their presets, and the app-level presets (UI layouts, keyboard, user preferences).

Data burn-in presets live in 'dava preset ACTION --burn-in' (render_ext.py), Fairlight presets in 'dava fairlight'.
"""

import os

from ..bridge import describe_object
from ..connect import ResolveError
from ..helpers import current_project, parse_assignments, typed_assignments

# 'dava app keyframe-mode' name -> resolve.* KeyframeMode constant.
KEYFRAME_MODES = {"all": "KEYFRAME_MODE_ALL", "color": "KEYFRAME_MODE_COLOR", "sizing": "KEYFRAME_MODE_SIZING"}

# CloudSettings field -> the resolve.* CloudSettingKey constant Resolve expects as the dict key.
CLOUD_KEYS = {
    "projectName": "CLOUD_SETTING_PROJECT_NAME",
    "projectMediaPath": "CLOUD_SETTING_PROJECT_MEDIA_PATH",
    "isCollab": "CLOUD_SETTING_IS_COLLAB",
    "syncMode": "CLOUD_SETTING_SYNC_MODE",
    "isCameraAccess": "CLOUD_SETTING_IS_CAMERA_ACCESS",
}

# 'dava cloud --sync' name -> resolve.* CloudSyncMode constant.
CLOUD_SYNC_MODES = {"none": "CLOUD_SYNC_NONE", "proxy": "CLOUD_SYNC_PROXY_ONLY",
                    "proxy-and-original": "CLOUD_SYNC_PROXY_AND_ORIG"}

DB_TYPES = ["Disk", "PostgreSQL"]


# ---------------------------------------------------------------------------
# Shared checks


def _require(ok, message):
    """Raise ResolveError(message) when Resolve reported failure (False or None)."""
    if not ok:
        raise ResolveError(message)


def _returned(value, what):
    """A value Resolve returned; None means the call failed."""
    if value is None:
        raise ResolveError(f"Resolve did not return {what}.")
    return value


def _hint(names, name, what):
    """' No WHAT is named NAME; available: ...' when name is not among names, else ''."""
    if names is None or name in names:
        return ""
    return f" No {what} is named {name!r}; available: {', '.join(map(str, names)) or 'none'}."


def _optional(*args):
    """Drop trailing None arguments so optional API parameters keep Resolve's defaults."""
    args = list(args)
    while args and args[-1] is None:
        args.pop()
    return args


_KINDS = {"file": (os.path.isfile, " or is not a file."), "folder": (os.path.isdir, " or is not a folder."),
          "any": (os.path.exists, ".")}


def _existing(path, what, kind="file"):
    """Absolute path of an input 'file', 'folder' or either ('any'); Resolve resolves relative paths from its cwd."""
    path = os.path.abspath(os.path.expanduser(path))
    check, problem = _KINDS[kind]
    if not check(path):
        raise ResolveError(f"{what} {path!r} does not exist{problem}")
    return path


def _output(path):
    """Absolute path of a file (or .dra folder) Resolve will write; its parent folder is created when missing."""
    path = os.path.abspath(os.path.expanduser(path))
    folder = os.path.dirname(path)
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as exc:
        raise ResolveError(f"Cannot create output folder {folder!r}: {exc.strerror or exc}.") from exc
    return path


def _constant(resolve, name):
    value = getattr(resolve, name, None)
    if value is None:
        raise ResolveError(f"This Resolve build has no constant resolve.{name}.")
    return value


# ---------------------------------------------------------------------------
# project: settings, name, info


def project_settings_from_pairs(session, pairs, unchecked=False):
    """KEY=VALUE pairs -> ProjectSettings dict in the given order; unchecked passes unknown keys as text."""
    values = parse_assignments(pairs)
    if not unchecked:
        return typed_assignments(session, pairs, "ProjectSettings")
    fields = session.spec.typeddicts["ProjectSettings"].fields
    typed = typed_assignments(session, [p for p in pairs if p.partition("=")[0] in fields], "ProjectSettings")
    return {key: typed.get(key, value) for key, value in values.items()}


def cmd_project_set(resolve, args):
    project = current_project(resolve)
    settings = project_settings_from_pairs(args.session, args.pairs, args.unchecked)
    # One key per call: a multi-key call fails as a whole and does not say which key Resolve refused.
    rejected = [key for key, value in settings.items() if not project.SetSettings({key: value})]
    if rejected:
        applied = [key for key in settings if key not in rejected]
        raise ResolveError(
            f"Resolve rejected project setting(s) {', '.join(f'{k}={settings[k]!r}' for k in rejected)}"
            + (f" (applied: {', '.join(applied)})" if applied else "")
            + ". See valid values with 'dava api show ProjectSettings' and current ones with 'dava project settings'.")
    return settings


def cmd_project_rename(resolve, args):
    project = current_project(resolve)
    old = project.GetName()
    _require(project.SetName(args.name),
             f"Could not rename project {old!r} to {args.name!r} (the name must be unique in its folder).")
    return f"Renamed project {old} to {args.name}"


def cmd_project_info(resolve, args):
    pm = resolve.GetProjectManager()
    project = pm.GetCurrentProject()
    name = args.name
    if name is None:
        if not project:
            raise ResolveError("No project is open. Name a project of the current folder: 'dava project info NAME'.")
        name = project.GetName()
    info = {"name": name, "folder": pm.GetCurrentFolder(), "open": bool(project) and project.GetName() == name}
    if info["open"]:
        info["uniqueId"] = project.GetUniqueId()
    attributes = _returned(pm.GetProjectAttributesInCurrentFolder(), "the project attributes of the current folder")
    if name not in attributes:
        if args.name is not None:
            raise ResolveError(f"No project named {name!r} in project folder {info['folder']!r}. "
                               "See 'dava project folders' and 'dava project cd'.")
        return info  # the open project lives in another project folder
    info["lastModifiedTime"] = pm.GetProjectLastModifiedTime(name)
    info.update(attributes[name])
    return info


def cmd_project_attributes(resolve, args):
    attributes = _returned(resolve.GetProjectManager().GetProjectAttributesInCurrentFolder(),
                         "the project attributes of the current folder")
    return [{"name": name, **values} for name, values in attributes.items()]


def cmd_project_close(resolve, args):
    pm = resolve.GetProjectManager()
    project = current_project(resolve)
    name = project.GetName()
    if args.save:
        _require(pm.SaveProject(), f"Could not save project {name!r}; it is still open.")
    _require(pm.CloseProject(project), f"Could not close project {name!r}.")
    return f"Closed project {name}" + ("" if args.save else " without saving")


def cmd_project_delete(resolve, args):
    pm = resolve.GetProjectManager()
    if not pm.DeleteProject(args.name):
        project = pm.GetCurrentProject()
        is_open = bool(project) and project.GetName() == args.name
        raise ResolveError(
            f"Could not delete project {args.name!r} from project folder {pm.GetCurrentFolder()!r}; it must be "
            "in that folder and must not be open."
            + (" It is open now: 'dava project close' first." if is_open else "")
            + _hint(pm.GetProjectListInCurrentFolder(), args.name, "project in this folder"))
    return f"Deleted project {args.name}"


# ---------------------------------------------------------------------------
# project: project manager folders


def _folder_listing(pm):
    return {
        "folder": pm.GetCurrentFolder(),
        "folders": _returned(pm.GetFolderListInCurrentFolder(), "the folder list"),
        "projects": _returned(pm.GetProjectListInCurrentFolder(), "the project list"),
    }


def cmd_project_folders(resolve, args):
    return _folder_listing(resolve.GetProjectManager())


def cmd_project_cd(resolve, args):
    pm = resolve.GetProjectManager()
    if args.path.startswith("/"):
        _require(pm.GotoRootFolder(), "Could not go to the root project folder.")
    for part in args.path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            _require(pm.GotoParentFolder(), f"Project folder {pm.GetCurrentFolder()!r} has no parent folder.")
        elif not pm.OpenFolder(part):
            here = pm.GetCurrentFolder()
            raise ResolveError(f"Could not open project folder {part!r} in {here!r} (stopped there)."
                               + _hint(pm.GetFolderListInCurrentFolder(), part, "folder in it"))
    return _folder_listing(pm)


def cmd_project_mkdir(resolve, args):
    pm = resolve.GetProjectManager()
    _require(pm.CreateFolder(args.name),
             f"Could not create project folder {args.name!r} in {pm.GetCurrentFolder()!r} (does it exist already?).")
    return f"Created project folder {args.name}"


def cmd_project_rmdir(resolve, args):
    pm = resolve.GetProjectManager()
    _require(pm.DeleteFolder(args.name), f"Could not delete project folder {args.name!r} from "
             f"{pm.GetCurrentFolder()!r}." + _hint(pm.GetFolderListInCurrentFolder(), args.name, "folder in it"))
    return f"Deleted project folder {args.name}"


# ---------------------------------------------------------------------------
# project: import, export, archive, restore


def _project_name(resolve, name):
    return name if name is not None else current_project(resolve).GetName()


def _project_hint(pm, name):
    return _hint(pm.GetProjectListInCurrentFolder(), name, f"project in folder {pm.GetCurrentFolder()!r}")


def cmd_project_import(resolve, args):
    path = _existing(args.file, "Project file")
    _require(resolve.GetProjectManager().ImportProject(*_optional(path, args.name)),
             f"Resolve could not import project file {path!r} (a project with that name may exist in the "
             "current folder; pass --name).")
    return f"Imported project from {path}" + (f" as {args.name}" if args.name else "")


def cmd_project_export(resolve, args):
    name = _project_name(resolve, args.name)
    path = _output(args.file)
    pm = resolve.GetProjectManager()
    if not pm.ExportProject(name, path, not args.no_stills_luts):
        raise ResolveError(f"Resolve could not export project {name!r} to {path!r}." + _project_hint(pm, name))
    return f"Exported project {name} to {path}"


def cmd_project_archive(resolve, args):
    name = _project_name(resolve, args.name)
    path = _output(args.file)
    pm = resolve.GetProjectManager()
    if not pm.ArchiveProject(name, path, not args.no_media, not args.no_render_cache, args.proxy_media):
        raise ResolveError(f"Resolve could not archive project {name!r} to {path!r}." + _project_hint(pm, name))
    return f"Archived project {name} to {path}"


def cmd_project_restore(resolve, args):
    # A .dra archive is a folder (Resolve manual, 'Managing Projects'), so any existing path is accepted.
    path = _existing(args.archive, "Project archive", kind="any")
    _require(resolve.GetProjectManager().RestoreProject(*_optional(path, args.name)),
             f"Resolve could not restore project archive {path!r} (is it a .dra archive folder? A project with "
             "that name may exist in the current folder; pass --name).")
    return f"Restored project from {path}" + (f" as {args.name}" if args.name else "")


# ---------------------------------------------------------------------------
# project: settings presets, IntelliSearch


def _preset_names(project):
    presets = project.GetProjectSettingsPresetList()
    return None if presets is None else [preset.get("Name") for preset in presets]


def cmd_project_preset_list(resolve, args):
    return _returned(current_project(resolve).GetProjectSettingsPresetList(), "the project settings preset list")


def cmd_project_preset_apply(resolve, args):
    project = current_project(resolve)
    _require(project.SetProjectSettingsPreset(args.name), f"Could not apply project settings preset {args.name!r}."
             + _hint(_preset_names(project), args.name, "project settings preset"))
    return f"Applied project settings preset {args.name} to project {project.GetName()}"


def cmd_project_preset_save(resolve, args):
    _require(current_project(resolve).SaveCurrentProjectSettingsAsNewPreset(args.name),
             f"Could not save project settings preset {args.name!r} (does it exist already? "
             "use 'dava project preset-update').")
    return f"Saved project settings preset {args.name}"


def cmd_project_preset_update(resolve, args):
    project = current_project(resolve)
    _require(project.UpdateProjectSettingsPreset(args.name), f"Could not update project settings preset "
             f"{args.name!r}." + _hint(_preset_names(project), args.name, "project settings preset"))
    return f"Updated project settings preset {args.name}"


def cmd_project_preset_delete(resolve, args):
    project = current_project(resolve)
    _require(project.DeleteProjectSettingsPreset(args.name), f"Could not delete project settings preset "
             f"{args.name!r}." + _hint(_preset_names(project), args.name, "project settings preset"))
    return f"Deleted project settings preset {args.name}"


def cmd_project_preset_export(resolve, args):
    project = current_project(resolve)
    path = _output(args.file)
    _require(project.ExportProjectSettingsPreset(args.name, path), f"Could not export project settings preset "
             f"{args.name!r} to {path!r}." + _hint(_preset_names(project), args.name, "project settings preset"))
    return f"Exported project settings preset {args.name} to {path}"


def cmd_project_preset_import(resolve, args):
    path = _existing(args.file, "Preset file")
    _require(current_project(resolve).ImportProjectSettingsPreset(*_optional(path, args.name)),
             f"Could not import project settings preset from {path!r}.")
    return f"Imported project settings preset from {path}" + (f" as {args.name}" if args.name else "")


def cmd_project_reset_intellisearch(resolve, args):
    project = current_project(resolve)
    _require(project.ResetIntellisearchAnalysis(),
             f"Could not reset the IntelliSearch analysis of project {project.GetName()!r}.")
    return f"Reset the IntelliSearch analysis of project {project.GetName()}"


# ---------------------------------------------------------------------------
# db


def _db_key(info):
    return info.get("DbType"), info.get("DbName"), info.get("IpAddress") or ""


def _db_text(info):
    return f"{info.get('DbName')} ({info.get('DbType')}" + (f" at {info['IpAddress']})" if info.get("IpAddress")
                                                              else ")")


def cmd_db_list(resolve, args):
    pm = resolve.GetProjectManager()
    databases = _returned(pm.GetDatabaseList(), "the database list")
    current = pm.GetCurrentDatabase()
    current_key = _db_key(current) if current else None
    return [{**info, "current": _db_key(info) == current_key} for info in databases]


def cmd_db_current(resolve, args):
    return _returned(resolve.GetProjectManager().GetCurrentDatabase(), "the current database")


def cmd_db_use(resolve, args):
    pm = resolve.GetProjectManager()
    databases = _returned(pm.GetDatabaseList(), "the database list")
    matches = [info for info in databases if info.get("DbName") == args.name
               and args.type in (None, info.get("DbType")) and args.ip in (None, info.get("IpAddress"))]
    if len(matches) > 1:
        raise ResolveError(f"{len(matches)} databases match {args.name!r}: "
                           f"{', '.join(map(_db_text, matches))}. Add --type or --ip.")
    if matches:
        info = dict(matches[0])
    elif args.type:
        info = {"DbType": args.type, "DbName": args.name, **({"IpAddress": args.ip} if args.ip else {})}
    else:
        raise ResolveError(f"No database named {args.name!r}. Known: "
                           f"{', '.join(map(_db_text, databases)) or 'none'}. For a database that is not in "
                           "this list pass --type (and --ip for PostgreSQL).")
    _require(pm.SetCurrentDatabase(info), f"Resolve could not switch to database {_db_text(info)}.")
    return f"Switched to database {_db_text(info)}; no project is open now"


# ---------------------------------------------------------------------------
# cloud


def cloud_settings(resolve, args):
    """CloudSettings from the command options, keyed by the resolve.CLOUD_SETTING_* constants."""
    values = {"projectName": args.project_name,
              "projectMediaPath": os.path.abspath(os.path.expanduser(args.media_path))}
    if getattr(args, "collab", False):
        values["isCollab"] = True
    if args.sync is not None:
        values["syncMode"] = _constant(resolve, CLOUD_SYNC_MODES[args.sync])
    if getattr(args, "camera_access", False):
        values["isCameraAccess"] = True
    return {_constant(resolve, CLOUD_KEYS[field]): value for field, value in values.items() if value is not None}


def cmd_cloud_create(resolve, args):
    project = resolve.GetProjectManager().CreateCloudProject(cloud_settings(resolve, args))
    _require(project, f"Resolve could not create cloud project {args.project_name!r} (are you signed in to "
             "Blackmagic Cloud, and is the name free?).")
    return describe_object(args.session, project, "Project")


def cmd_cloud_open(resolve, args):
    project = resolve.GetProjectManager().LoadCloudProject(cloud_settings(resolve, args))
    _require(project, f"Resolve could not open cloud project {args.project_name!r} (not found, or not signed in "
             "to Blackmagic Cloud).")
    return describe_object(args.session, project, "Project")


def cmd_cloud_import(resolve, args):
    path = _existing(args.file, "Project file")
    _require(resolve.GetProjectManager().ImportCloudProject(path, cloud_settings(resolve, args)),
             f"Resolve could not import {path!r} as a cloud project.")
    return f"Imported {path} as a cloud project"


def cmd_cloud_restore(resolve, args):
    path = _existing(args.folder, "Folder", kind="folder")
    _require(resolve.GetProjectManager().RestoreCloudProject(path, cloud_settings(resolve, args)),
             f"Resolve could not restore a cloud project from {path!r}.")
    return f"Restored a cloud project from {path}"


# ---------------------------------------------------------------------------
# app


def cmd_app_info(resolve, args):
    return {"product": _returned(resolve.GetProductName(), "the product name"),
            "version": _returned(resolve.GetVersionString(), "the version string"),
            "versionFields": _returned(resolve.GetVersion(), "the version fields"),
            "studio": _returned(resolve.IsStudio(), "whether this is Resolve Studio")}


def cmd_app_keyframe_mode(resolve, args):
    if args.mode is not None:
        value = _constant(resolve, KEYFRAME_MODES[args.mode])
        _require(resolve.SetKeyframeMode(value), f"Could not set the keyframe mode to {args.mode}.")
        return {"mode": args.mode, "value": value}
    value = _returned(resolve.GetKeyframeMode(), "the keyframe mode")
    names = {getattr(resolve, constant, None): name for name, constant in KEYFRAME_MODES.items()}
    names.pop(None, None)
    return {"mode": names.get(value), "value": value}


def cmd_app_priority(resolve, args):
    _require(resolve.SetHighPriority(args.level == "high"), f"Could not set the script priority to {args.level}.")
    return f"Script priority is {args.level}"


def cmd_app_disable_background_tasks(resolve, args):
    # Documented to return None; only an explicit False is a reported failure.
    if resolve.DisableBackgroundTasksForCurrentResolveSession() is False:
        raise ResolveError("Resolve refused to disable background tasks.")
    return "Background tasks are disabled until Resolve restarts"


def cmd_app_prefs_list(resolve, args):
    return _returned(resolve.GetUserPreferencesPresetList(), "the user preferences preset list")


def cmd_app_prefs_load(resolve, args):
    _require(resolve.LoadUserPreferencesPreset(args.name), f"Could not load user preferences preset {args.name!r}."
             + _hint(resolve.GetUserPreferencesPresetList(), args.name, "user preferences preset"))
    return f"Loaded user preferences preset {args.name}"


def cmd_app_prefs_save(resolve, args):
    _require(resolve.SaveUserPreferencesPreset(args.name),
             f"Could not save user preferences preset {args.name!r} (does it exist already?).")
    return f"Saved the current user preferences as preset {args.name}"


def cmd_app_prefs_delete(resolve, args):
    _require(resolve.DeleteUserPreferencesPreset(args.name), f"Could not delete user preferences preset "
             f"{args.name!r}." + _hint(resolve.GetUserPreferencesPresetList(), args.name, "user preferences preset"))
    return f"Deleted user preferences preset {args.name}"


def cmd_app_prefs_import(resolve, args):
    path = _existing(args.file, "Preset file")
    _require(resolve.ImportUserPreferencesPreset(*_optional(path, args.name)),
             f"Could not import a user preferences preset from {path!r}.")
    return f"Imported user preferences preset from {path}" + (f" as {args.name}" if args.name else "")


def cmd_app_prefs_export(resolve, args):
    path = _output(args.file)
    _require(resolve.ExportUserPreferencesPreset(args.name, path), f"Could not export user preferences preset "
             f"{args.name!r} to {path!r}." + _hint(resolve.GetUserPreferencesPresetList(), args.name,
                                                   "user preferences preset"))
    return f"Exported user preferences preset {args.name} to {path}"


def cmd_app_dctl_validate(resolve, args):
    source, what = args.code, "the DCTL code"
    if args.file is not None:
        path = _existing(args.file, "DCTL file")
        try:
            with open(path, encoding="utf-8") as handle:
                source, what = handle.read(), path
        except (OSError, UnicodeDecodeError) as exc:
            raise ResolveError(f"Cannot read DCTL file {path!r} as UTF-8 text: {exc}.") from exc
    error = resolve.ValidateDCTL(source)
    if error is not None:
        raise ResolveError(f"{what} did not validate: {error or 'Resolve gave no details'}")
    return {"dctl": what, "valid": True}


def cmd_app_dctl_encrypt(resolve, args):
    path = _existing(args.file, "DCTL file")
    options = {"Name": args.name, "Expiry": args.expiry,
               "OutputFolder": os.path.abspath(os.path.expanduser(args.output_folder)) if args.output_folder else None}
    options = {key: value for key, value in options.items() if value is not None}
    _require(resolve.EncryptDCTL(*([path, options] if options else [path])),
             f"Resolve could not encrypt DCTL {path!r}.")
    folder = options.get("OutputFolder", "your home folder")
    return f"Encrypted {path} into {folder}"


# ---------------------------------------------------------------------------
# layout


def cmd_layout_list(resolve, args):
    return _returned(resolve.GetLayoutPresetList(), "the UI layout preset list")


def cmd_layout_load(resolve, args):
    _require(resolve.LoadLayoutPreset(args.name), f"Could not load UI layout preset {args.name!r}."
             + _hint(resolve.GetLayoutPresetList(), args.name, "UI layout preset"))
    return f"Loaded UI layout preset {args.name}"


def cmd_layout_save(resolve, args):
    _require(resolve.SaveLayoutPreset(args.name), f"Could not save UI layout preset {args.name!r} (does it exist "
             "already? use 'dava layout update').")
    return f"Saved the current UI layout as preset {args.name}"


def cmd_layout_update(resolve, args):
    _require(resolve.UpdateLayoutPreset(args.name), f"Could not update UI layout preset {args.name!r}."
             + _hint(resolve.GetLayoutPresetList(), args.name, "UI layout preset"))
    return f"Updated UI layout preset {args.name} with the current layout"


def cmd_layout_delete(resolve, args):
    _require(resolve.DeleteLayoutPreset(args.name), f"Could not delete UI layout preset {args.name!r}."
             + _hint(resolve.GetLayoutPresetList(), args.name, "UI layout preset"))
    return f"Deleted UI layout preset {args.name}"


def cmd_layout_export(resolve, args):
    path = _output(args.file)
    _require(resolve.ExportLayoutPreset(args.name, path), f"Could not export UI layout preset {args.name!r} to "
             f"{path!r}." + _hint(resolve.GetLayoutPresetList(), args.name, "UI layout preset"))
    return f"Exported UI layout preset {args.name} to {path}"


def cmd_layout_import(resolve, args):
    path = _existing(args.file, "Preset file")
    _require(resolve.ImportLayoutPreset(*_optional(path, args.name)),
             f"Could not import a UI layout preset from {path!r}.")
    return f"Imported UI layout preset from {path}" + (f" as {args.name}" if args.name else "")


# ---------------------------------------------------------------------------
# keyboard


def cmd_keyboard_list(resolve, args):
    return _returned(resolve.GetKeyboardPresetList(), "the keyboard preset list")


def cmd_keyboard_current(resolve, args):
    return _returned(resolve.GetCurrentKeyboardPreset(), "the current keyboard preset")


def cmd_keyboard_load(resolve, args):
    _require(resolve.LoadKeyboardPreset(args.name), f"Could not load keyboard preset {args.name!r}."
             + _hint(resolve.GetKeyboardPresetList(), args.name, "keyboard preset"))
    return f"Loaded keyboard preset {args.name}"


def cmd_keyboard_delete(resolve, args):
    _require(resolve.DeleteKeyboardPreset(args.name), f"Could not delete keyboard preset {args.name!r}."
             + _hint(resolve.GetKeyboardPresetList(), args.name, "keyboard preset"))
    return f"Deleted keyboard preset {args.name}"


def cmd_keyboard_import(resolve, args):
    path = _existing(args.file, "Preset file")
    _require(resolve.ImportKeyboardPreset(*_optional(path, args.name)),
             f"Could not import a keyboard preset from {path!r}.")
    return f"Imported keyboard preset from {path}" + (f" as {args.name}" if args.name else "")


def cmd_keyboard_export(resolve, args):
    path = _output(args.file)
    _require(resolve.ExportKeyboardPreset(args.name, path), f"Could not export keyboard preset {args.name!r} to "
             f"{path!r}." + _hint(resolve.GetKeyboardPresetList(), args.name, "keyboard preset"))
    return f"Exported keyboard preset {args.name} to {path}"


# ---------------------------------------------------------------------------
# Registration


def _name(p, what):
    p.add_argument("name", help=f"{what} name")


def _import_args(p, what):
    p.add_argument("file", help="preset file to import")
    p.add_argument("-n", "--name", help=f"{what} name (default: the file's base name)")


def _export_args(p, what):
    p.add_argument("name", help=f"{what} name")
    p.add_argument("file", help="file to write (missing folders are created)")


def _register_project(registry):
    p = registry.action("project", "set", "change project settings, e.g. timelineFrameRate=25 "
                        "timelineResolutionWidth=3840 (keys and values: 'dava api show ProjectSettings'; "
                        "current values: 'dava project settings')")
    p.add_argument("pairs", nargs="+", metavar="KEY=VALUE")
    p.add_argument("--unchecked", action="store_true",
                   help="also send keys missing from the API definition, as text")
    p.set_defaults(func=cmd_project_set, read_only=False)

    p = registry.action("project", "rename", "rename the current project (the name must be unique in its folder)")
    p.add_argument("name", help="new project name")
    p.set_defaults(func=cmd_project_rename, read_only=False)

    p = registry.action("project", "info", "name, unique id, dates and notes of the current project or of a project "
                        "in the current project folder")
    p.add_argument("name", nargs="?", help="project name (default: the open project)")
    p.set_defaults(func=cmd_project_info, read_only=True)

    p = registry.action("project", "attributes",
                        "every project of the current project folder with its dates, notes and collaboration mode")
    p.set_defaults(func=cmd_project_attributes, read_only=True)

    p = registry.action("project", "close", "close the current project WITHOUT saving (add --save to save first)")
    p.add_argument("--save", action="store_true", help="save the project before closing it")
    p.set_defaults(func=cmd_project_close, read_only=False)

    p = registry.action("project", "delete", "delete a project of the current project folder (it must not be open)")
    _name(p, "project")
    p.set_defaults(func=cmd_project_delete, read_only=False)

    p = registry.action("project", "folders", "the current project manager folder, its subfolders and its projects")
    p.set_defaults(func=cmd_project_folders, read_only=True, always_json=True)

    p = registry.action("project", "cd", "change the project manager folder: NAME, A/B, '..' (parent), '/' (root) "
                        "or /A/B (from the root)")
    p.add_argument("path", help="folder path")
    p.set_defaults(func=cmd_project_cd, read_only=False, always_json=True)

    p = registry.action("project", "mkdir", "create a project folder in the current project manager folder")
    _name(p, "folder")
    p.set_defaults(func=cmd_project_mkdir, read_only=False)

    p = registry.action("project", "rmdir", "delete a project folder of the current project manager folder")
    _name(p, "folder")
    p.set_defaults(func=cmd_project_rmdir, read_only=False)

    p = registry.action("project", "import", "import a project file (.drp) into the current project folder")
    p.add_argument("file", help=".drp file")
    p.add_argument("-n", "--name", help="project name (default: the name stored in the file)")
    p.set_defaults(func=cmd_project_import, read_only=False)

    p = registry.action("project", "export", "export a project to a .drp file")
    p.add_argument("file", help=".drp file to write")
    p.add_argument("-n", "--name", help="project of the current folder (default: the open project)")
    p.add_argument("--no-stills-luts", action="store_true", help="leave out gallery stills and LUTs")
    p.set_defaults(func=cmd_project_export, read_only=False)

    p = registry.action("project", "archive", "archive a project with its media to a .dra archive (Resolve writes "
                        "a folder named like FILE, holding the project and its media)")
    p.add_argument("file", help=".dra archive to write, e.g. promo.dra (missing parent folders are created)")
    p.add_argument("-n", "--name", help="project of the current folder (default: the open project)")
    p.add_argument("--no-media", action="store_true", help="leave out the source media")
    p.add_argument("--no-render-cache", action="store_true", help="leave out the render cache")
    p.add_argument("--proxy-media", action="store_true", help="include proxy media")
    p.set_defaults(func=cmd_project_archive, read_only=False)

    p = registry.action("project", "restore", "restore a project from a .dra archive into the current project folder "
                        "(the restored project keeps using the media inside the archive folder)")
    p.add_argument("archive", help=".dra archive folder")
    p.add_argument("-n", "--name", help="project name (default: the archived name)")
    p.set_defaults(func=cmd_project_restore, read_only=False)

    p = registry.action("project", "preset-list", "project settings presets (name and resolution)")
    p.set_defaults(func=cmd_project_preset_list, read_only=True)

    p = registry.action("project", "preset-apply", "apply a project settings preset to the current project")
    _name(p, "preset")
    p.set_defaults(func=cmd_project_preset_apply, read_only=False)

    p = registry.action("project", "preset-save", "save the current project settings as a new preset")
    _name(p, "preset")
    p.set_defaults(func=cmd_project_preset_save, read_only=False)

    p = registry.action("project", "preset-update", "overwrite a project settings preset with the current settings")
    _name(p, "preset")
    p.set_defaults(func=cmd_project_preset_update, read_only=False)

    p = registry.action("project", "preset-delete", "delete a project settings preset")
    _name(p, "preset")
    p.set_defaults(func=cmd_project_preset_delete, read_only=False)

    p = registry.action("project", "preset-export", "export a project settings preset to a file")
    _export_args(p, "preset")
    p.set_defaults(func=cmd_project_preset_export, read_only=False)

    p = registry.action("project", "preset-import", "import a project settings preset from a file")
    _import_args(p, "preset")
    p.set_defaults(func=cmd_project_preset_import, read_only=False)

    p = registry.action("project", "reset-intellisearch", "reset the IntelliSearch analysis of the current project")
    p.set_defaults(func=cmd_project_reset_intellisearch, read_only=False)


def _register_db(registry):
    help_text = "project databases (disk and PostgreSQL)"
    p = registry.action("db", "list", "databases added to Resolve; 'current' marks the connected one",
                        group_help=help_text)
    p.set_defaults(func=cmd_db_list, read_only=True)

    p = registry.action("db", "current", "the connected database")
    p.set_defaults(func=cmd_db_current, read_only=True)

    p = registry.action("db", "use", "connect to another database (Resolve closes the open project: "
                        "'dava project save' first)")
    p.add_argument("name", help="database name, see 'dava db list'")
    p.add_argument("--type", choices=DB_TYPES, help="database type, to pick among equal names or to use a "
                   "database missing from 'dava db list'")
    p.add_argument("--ip", help="PostgreSQL server address, e.g. 127.0.0.1")
    p.set_defaults(func=cmd_db_use, read_only=False)


def _cloud_args(p, name_required=True, full=True):
    p.add_argument("-m", "--media-path", required=True, help="local folder for the project's media")
    p.add_argument("-n", "--name", dest="project_name", metavar="NAME", required=name_required,
                   help="cloud project name" + ("" if name_required else " (optional)"))
    p.add_argument("--sync", choices=sorted(CLOUD_SYNC_MODES), help="media sync mode (Resolve default: proxy)")
    if full:
        p.add_argument("--collab", action="store_true", help="turn on live collaboration")
        p.add_argument("--camera-access", action="store_true", help="turn on camera access")


def _register_cloud(registry):
    help_text = "Blackmagic Cloud projects (needs a signed-in Blackmagic Cloud account)"
    p = registry.action("cloud", "create", "create a cloud project and open it", group_help=help_text)
    _cloud_args(p)
    p.set_defaults(func=cmd_cloud_create, read_only=False)

    p = registry.action("cloud", "open", "open a cloud project (media path and sync mode count on the first "
                        "open on this computer only)")
    _cloud_args(p, full=False)
    p.set_defaults(func=cmd_cloud_open, read_only=False)

    p = registry.action("cloud", "import", "import a project file (.drp) as a cloud project")
    p.add_argument("file", help=".drp file")
    _cloud_args(p, name_required=False)
    p.set_defaults(func=cmd_cloud_import, read_only=False)

    p = registry.action("cloud", "restore", "restore a cloud project from a folder")
    p.add_argument("folder", help="folder to restore from")
    _cloud_args(p, name_required=False)
    p.set_defaults(func=cmd_cloud_restore, read_only=False)


def _register_app(registry):
    help_text = ("Resolve app: version, keyframe mode, script priority, user preferences presets, DCTL "
                 "(burn-in presets: 'dava preset ACTION --burn-in')")
    p = registry.action("app", "info", "product name, version and whether this is Resolve Studio",
                        group_help=help_text)
    p.set_defaults(func=cmd_app_info, read_only=True, always_json=True)

    p = registry.action("app", "keyframe-mode", "show or set the Color page keyframe mode")
    p.add_argument("mode", nargs="?", choices=sorted(KEYFRAME_MODES), help="new mode (default: show the current one)")
    p.set_defaults(func=cmd_app_keyframe_mode, read_only=lambda a: a.mode is None)

    p = registry.action("app", "priority", "run scripts at high or normal priority")
    p.add_argument("level", choices=["high", "normal"])
    p.set_defaults(func=cmd_app_priority, read_only=False)

    p = registry.action("app", "disable-background-tasks", "turn off background tasks until Resolve restarts")
    p.set_defaults(func=cmd_app_disable_background_tasks, read_only=False)

    p = registry.action("app", "prefs-list", "list user preferences presets")
    p.set_defaults(func=cmd_app_prefs_list, read_only=True)
    for action, func, text in (
            ("prefs-load", cmd_app_prefs_load, "load a user preferences preset"),
            ("prefs-save", cmd_app_prefs_save, "save the current user preferences as a new preset"),
            ("prefs-delete", cmd_app_prefs_delete, "delete a user preferences preset")):
        p = registry.action("app", action, text)
        _name(p, "preset")
        p.set_defaults(func=func, read_only=False)

    p = registry.action("app", "prefs-import", "import a user preferences preset from a file")
    _import_args(p, "preset")
    p.set_defaults(func=cmd_app_prefs_import, read_only=False)
    p = registry.action("app", "prefs-export", "export a user preferences preset to a file")
    _export_args(p, "preset")
    p.set_defaults(func=cmd_app_prefs_export, read_only=False)

    p = registry.action("app", "dctl-validate", "check DCTL source code; exit 1 with Resolve's message when invalid")
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("file", nargs="?", help=".dctl file")
    source.add_argument("--code", help="DCTL source text instead of a file")
    p.set_defaults(func=cmd_app_dctl_validate, read_only=False)

    p = registry.action("app", "dctl-encrypt", "encrypt a .dctl file for distribution")
    p.add_argument("file", help=".dctl file")
    p.add_argument("-n", "--name", help="output file name (default: the input file name)")
    p.add_argument("--expiry", metavar="ISO_DATE", help="expiry date in ISO 8601, e.g. 2027-12-31 (default: none)")
    p.add_argument("-o", "--output-folder", help="output folder (default: your home folder)")
    p.set_defaults(func=cmd_app_dctl_encrypt, read_only=False)


def _register_layout(registry):
    help_text = "UI layout presets"
    p = registry.action("layout", "list", "list UI layout presets", group_help=help_text)
    p.set_defaults(func=cmd_layout_list, read_only=True)
    for action, func, text in (
            ("load", cmd_layout_load, "switch the UI to a saved layout preset"),
            ("save", cmd_layout_save, "save the current UI layout as a new preset"),
            ("update", cmd_layout_update, "overwrite a layout preset with the current UI layout"),
            ("delete", cmd_layout_delete, "delete a layout preset")):
        p = registry.action("layout", action, text)
        _name(p, "preset")
        p.set_defaults(func=func, read_only=False)
    p = registry.action("layout", "export", "export a layout preset to a file")
    _export_args(p, "preset")
    p.set_defaults(func=cmd_layout_export, read_only=False)
    p = registry.action("layout", "import", "import a layout preset from a file")
    _import_args(p, "preset")
    p.set_defaults(func=cmd_layout_import, read_only=False)


def _register_keyboard(registry):
    help_text = "keyboard shortcut presets"
    p = registry.action("keyboard", "list", "list keyboard presets", group_help=help_text)
    p.set_defaults(func=cmd_keyboard_list, read_only=True)
    p = registry.action("keyboard", "current", "the active keyboard preset")
    p.set_defaults(func=cmd_keyboard_current, read_only=True)
    for action, func, text in (
            ("load", cmd_keyboard_load, "activate a keyboard preset"),
            ("delete", cmd_keyboard_delete, "delete a keyboard preset")):
        p = registry.action("keyboard", action, text)
        _name(p, "preset")
        p.set_defaults(func=func, read_only=False)
    p = registry.action("keyboard", "export", "export a keyboard preset to a file")
    _export_args(p, "preset")
    p.set_defaults(func=cmd_keyboard_export, read_only=False)
    p = registry.action("keyboard", "import", "import a keyboard preset from a file")
    _import_args(p, "preset")
    p.set_defaults(func=cmd_keyboard_import, read_only=False)


def register(registry):
    _register_project(registry)
    _register_db(registry)
    _register_cloud(registry)
    _register_app(registry)
    _register_layout(registry)
    _register_keyboard(registry)
