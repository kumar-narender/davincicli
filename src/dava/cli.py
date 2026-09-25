"""Command-line interface for DaVinci Resolve, built on its external scripting API."""

import argparse
import json
import os
import shlex
import subprocess
import sys
import time

from . import __version__, bridge
from .bridge import REF_HELP, Session, iter_clips
from .commands import load_command_modules
from .connect import ResolveError, get_resolve, load_script_module
from .registry import Registry

PAGES = ["media", "photo", "cut", "edit", "fusion", "color", "fairlight", "deliver"]

MARKER_COLORS = [
    "Blue", "Cyan", "Green", "Yellow", "Red", "Pink", "Purple", "Fuchsia",
    "Rose", "Lavender", "Sky", "Mint", "Lemon", "Sand", "Cocoa", "Cream",
]

# CLI format name -> (exportType constant, exportSubtype constant) on the resolve object.
# The subtype only matters for AAF and EDL; Resolve ignores it for every other type.
EXPORT_FORMATS = {
    "aaf": ("EXPORT_AAF", "EXPORT_AAF_NEW"),
    "aaf-existing": ("EXPORT_AAF", "EXPORT_AAF_EXISTING"),
    "edl": ("EXPORT_EDL", "EXPORT_NONE"),
    "edl-cdl": ("EXPORT_EDL", "EXPORT_CDL"),
    "edl-sdl": ("EXPORT_EDL", "EXPORT_SDL"),
    "edl-missing": ("EXPORT_EDL", "EXPORT_MISSING_CLIPS"),
    "fcp7xml": ("EXPORT_FCP_7_XML", "EXPORT_NONE"),
    "fcpxml": ("EXPORT_FCPXML_1_10", "EXPORT_NONE"),
    "fcpxml-1.9": ("EXPORT_FCPXML_1_9", "EXPORT_NONE"),
    "fcpxml-1.8": ("EXPORT_FCPXML_1_8", "EXPORT_NONE"),
    "otio": ("EXPORT_OTIO", "EXPORT_NONE"),
    "drt": ("EXPORT_DRT", "EXPORT_NONE"),
    "csv": ("EXPORT_TEXT_CSV", "EXPORT_NONE"),
    "tab": ("EXPORT_TEXT_TAB", "EXPORT_NONE"),
    "ale": ("EXPORT_ALE", "EXPORT_NONE"),
    "ale-cdl": ("EXPORT_ALE_CDL", "EXPORT_NONE"),
}

CLIP_COLORS = [
    "Orange", "Apricot", "Yellow", "Lime", "Olive", "Green", "Teal", "Navy",
    "Blue", "Purple", "Violet", "Pink", "Tan", "Beige", "Brown", "Chocolate",
]

STILL_EXTENSIONS = (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".heic", ".dng", ".exr", ".dpx", ".bmp", ".tga",
                    ".webp")
FAILED_JOB_STATES = {"Failed", "Cancelled", "Background Render Cancelled", "Remote Render Cancelled"}


from .helpers import (  # noqa: E402  re-exported for command modules and tests
    add_item_args, apply_render_settings, check_node, current_project, find_clip, find_timeline, parse_assignments,
    select_items, select_one_item, timelines,
)


# ---------------------------------------------------------------------------
# Command handlers: each takes (resolve, args) and returns data to print.


def cmd_status(resolve, args):
    project = resolve.GetProjectManager().GetCurrentProject()
    timeline = project.GetCurrentTimeline() if project else None
    return {
        "product": resolve.GetProductName(),
        "version": resolve.GetVersionString(),
        "page": resolve.GetCurrentPage(),
        "project": project.GetName() if project else None,
        "timeline": timeline.GetName() if timeline else None,
    }


def cmd_page(resolve, args):
    if args.name is None:
        return resolve.GetCurrentPage()
    if not resolve.OpenPage(args.name):
        raise ResolveError(f"Could not switch to the {args.name} page.")
    return args.name


def cmd_project_list(resolve, args):
    return resolve.GetProjectManager().GetProjectListInCurrentFolder()


def cmd_project_open(resolve, args):
    if not resolve.GetProjectManager().LoadProject(args.name):
        raise ResolveError(f"Could not open project {args.name!r} (not found in the current folder?).")
    return f"Opened project {args.name}"


def cmd_project_create(resolve, args):
    if not resolve.GetProjectManager().CreateProject(args.name):
        raise ResolveError(f"Could not create project {args.name!r} (a project with that name may exist).")
    return f"Created project {args.name}"


def cmd_project_save(resolve, args):
    project = current_project(resolve)
    if not resolve.GetProjectManager().SaveProject():
        raise ResolveError(f"Could not save project {project.GetName()!r}.")
    return f"Saved project {project.GetName()}"


def cmd_project_settings(resolve, args):
    settings = current_project(resolve).GetSettings()
    if not args.keys:
        return settings
    missing = [key for key in args.keys if key not in settings]
    if missing:
        raise ResolveError(f"Unknown project setting(s): {', '.join(missing)}")
    return {key: settings[key] for key in args.keys}


def cmd_timeline_list(resolve, args):
    project = current_project(resolve)
    current = project.GetCurrentTimeline()
    current_id = current.GetUniqueId() if current else None
    return [
        {
            "current": "*" if timeline.GetUniqueId() == current_id else "",
            "name": timeline.GetName(),
            "start": timeline.GetStartTimecode(),
            "video_tracks": timeline.GetTrackCount("video"),
            "audio_tracks": timeline.GetTrackCount("audio"),
        }
        for timeline in timelines(project)
    ]


def cmd_timeline_use(resolve, args):
    project = current_project(resolve)
    if not project.SetCurrentTimeline(find_timeline(project, args.name)):
        raise ResolveError(f"Could not make {args.name!r} the current timeline.")
    return f"Current timeline: {args.name}"


def cmd_timeline_create(resolve, args):
    media_pool = current_project(resolve).GetMediaPool()
    if not media_pool.CreateEmptyTimeline(args.name):
        raise ResolveError(f"Could not create timeline {args.name!r} (the name may already be taken).")
    return f"Created timeline {args.name}"


def cmd_timeline_export(resolve, args):
    timeline = find_timeline(current_project(resolve), args.timeline)
    type_name, subtype_name = EXPORT_FORMATS[args.format]
    path = os.path.abspath(args.output)
    if not timeline.Export(path, getattr(resolve, type_name), getattr(resolve, subtype_name)):
        raise ResolveError(f"Export of {timeline.GetName()!r} as {args.format} to {path} failed.")
    return f"Exported {timeline.GetName()} to {path}"


def cmd_media_import(resolve, args):
    paths = [os.path.abspath(p) for p in args.paths]
    missing = [p for p in paths if not os.path.exists(p)]
    if missing:
        raise ResolveError(f"Path(s) not found: {', '.join(missing)}")
    media_pool = current_project(resolve).GetMediaPool()
    # Resolve merges numbered stills that arrive in one call (IMG_0001.jpg, IMG_0002.jpg) into a single image
    # sequence clip, so each still gets its own call; `media import-sequence` imports real sequences.
    is_still = lambda path: os.path.isfile(path) and path.lower().endswith(STILL_EXTENSIONS)
    batches = []
    for path in paths:
        if is_still(path) or not batches or is_still(batches[-1][0]):
            batches.append([path])
        else:
            batches[-1].append(path)
    clips = []
    for batch in batches:
        imported = media_pool.ImportMedia([{"FilePath": p} for p in batch])
        if not imported:
            # Resolve's Lua API (used by the free-edition bridge) only accepts the plain list of paths.
            imported = media_pool.ImportMedia(batch)
        clips += imported or []
    if not clips:
        raise ResolveError("Resolve did not import any media from the given paths.")
    if args.append:
        if not media_pool.AppendToTimeline([{"mediaPoolItem": clip} for clip in clips]):
            raise ResolveError("Imported media but could not append it to the current timeline.")
    return [clip.GetName() for clip in clips]


def cmd_media_list(resolve, args):
    media_pool = current_project(resolve).GetMediaPool()
    if args.all:
        return [{"folder": path, "clip": clip.GetName()} for path, clip in iter_clips(media_pool.GetRootFolder())]
    folder = media_pool.GetCurrentFolder()
    return [{"folder": folder.GetName(), "clip": clip.GetName()} for clip in folder.GetClipList()]


def cmd_marker_list(resolve, args):
    timeline = find_timeline(current_project(resolve), args.timeline)
    return [{"frame": frame, **info} for frame, info in sorted(timeline.GetMarkers().items())]


def cmd_marker_add(resolve, args):
    timeline = find_timeline(current_project(resolve), args.timeline)
    if not timeline.AddMarker(args.frame, args.color, args.name, args.note, args.duration):
        raise ResolveError(f"Could not add a marker at frame {args.frame} (one may already exist there).")
    return f"Added {args.color} marker at frame {args.frame} on {timeline.GetName()}"


def cmd_render_presets(resolve, args):
    return current_project(resolve).GetRenderPresetList()


def _job_rows(project, jobs):
    rows = []
    for job in jobs:
        status = project.GetRenderJobStatus(job["JobId"])
        rows.append({
            "id": job["JobId"],
            "timeline": job.get("TimelineName"),
            "status": status.get("JobStatus"),
            "percent": status.get("CompletionPercentage"),
            "output": os.path.join(job.get("TargetDir", ""), job.get("OutputFilename", "")),
            "error": status.get("Error", ""),
        })
    return rows


def cmd_render_jobs(resolve, args):
    project = current_project(resolve)
    return _job_rows(project, project.GetRenderJobList())


def _add_render_job(project, timeline, preset, target_dir, name):
    if timeline is not None and not project.SetCurrentTimeline(timeline):
        raise ResolveError(f"Could not make {timeline.GetName()!r} the current timeline.")
    if preset and not project.LoadRenderPreset(preset):
        raise ResolveError(f"Could not load render preset {preset!r}. See 'dava render presets'.")
    settings = {}
    if target_dir:
        settings["TargetDir"] = os.path.abspath(target_dir)
    if name:
        settings["CustomName"] = name
    rejected = apply_render_settings(project, settings)
    if rejected:
        raise ResolveError(f"Resolve rejected render setting(s): {', '.join(rejected)}.")
    job_id = project.AddRenderJob()
    if not job_id:
        raise ResolveError("Could not add a render job. Check the render settings on the Deliver page.")
    return job_id


def cmd_render_add(resolve, args):
    project = current_project(resolve)
    if not args.all_timelines:
        timeline = find_timeline(project, args.timeline) if args.timeline is not None else None
        return _add_render_job(project, timeline, args.preset, args.dir, args.name)
    # One job per timeline; each output is named after its timeline so files do not collide.
    rows = []
    for timeline in timelines(project):
        name = f"{args.name}_{timeline.GetName()}" if args.name else timeline.GetName()
        job_id = _add_render_job(project, timeline, args.preset, args.dir, name)
        rows.append({"timeline": timeline.GetName(), "job_id": job_id})
    return rows


def cmd_render_start(resolve, args):
    project = current_project(resolve)
    job_ids = args.job_ids or [job["JobId"] for job in project.GetRenderJobList()]
    if not job_ids:
        raise ResolveError("The render queue is empty. Add a job with 'dava render add'.")
    if not project.StartRendering(job_ids):
        raise ResolveError("Resolve refused to start rendering.")
    if not args.wait:
        return f"Started rendering {len(job_ids)} job(s)"

    while project.IsRenderingInProgress():
        parts = []
        for job_id in job_ids:
            status = project.GetRenderJobStatus(job_id)
            parts.append(f"{job_id[:8]} {status.get('JobStatus')} {status.get('CompletionPercentage', 0)}%")
        print("\r" + " | ".join(parts), end="", file=sys.stderr, flush=True)
        time.sleep(args.interval)
    print(file=sys.stderr)

    jobs = [job for job in project.GetRenderJobList() if job["JobId"] in job_ids]
    rows = _job_rows(project, jobs)
    failed = [row for row in rows if row["status"] in FAILED_JOB_STATES]
    if failed:
        details = "; ".join(f"{row['id']}: {row['status']} {row['error']}".strip() for row in failed)
        raise ResolveError(f"{len(failed)} render job(s) did not complete: {details}")
    return rows


def cmd_render_stop(resolve, args):
    current_project(resolve).StopRendering()
    return "Stopped rendering"


def cmd_color_nodes(resolve, args):
    rows = []
    for index, item in select_items(resolve, args):
        graph = item.GetNodeGraph()
        for node in range(1, graph.GetNumNodes() + 1):
            rows.append({
                "clip": index,
                "name": item.GetName(),
                "node": node,
                "label": graph.GetNodeLabel(node),
                "lut": graph.GetLUT(node),
                "tools": ", ".join(graph.GetToolsInNode(node) or []),
            })
    return rows


def cmd_color_lut(resolve, args):
    project = current_project(resolve)
    items = select_items(resolve, args)
    # SetLUT only accepts LUTs Resolve has already discovered.
    project.RefreshLUTList()
    for index, item in items:
        graph = item.GetNodeGraph()
        check_node(graph, args.node)
        if not graph.SetLUT(args.node, args.lut):
            raise ResolveError(f"Could not set LUT {args.lut!r} on clip {index} node {args.node}.")
    return f"Applied LUT {args.lut} to node {args.node} of {len(items)} clip(s)"


def cmd_color_cdl(resolve, args):
    cdl = {"NodeIndex": args.node}
    for key, value in (("Slope", args.slope), ("Offset", args.offset), ("Power", args.power)):
        if value is not None:
            parts = value.split()
            if len(parts) != 3:
                raise ResolveError(f"--{key.lower()} needs three values 'R G B', got {value!r}.")
            cdl[key] = " ".join(parts)
    if args.saturation is not None:
        cdl["Saturation"] = args.saturation
    if len(cdl) == 1:
        raise ResolveError("Give at least one of --slope, --offset, --power, --saturation.")
    items = select_items(resolve, args)
    for index, item in items:
        check_node(item.GetNodeGraph(), args.node)
        if not item.SetCDL(cdl):
            raise ResolveError(f"Resolve rejected CDL {cdl} on clip {index}.")
    return f"Applied CDL to node {args.node} of {len(items)} clip(s)"


DRX_MODES = {"none": 0, "source-tc": 1, "start-frames": 2}


def cmd_color_drx(resolve, args):
    path = os.path.abspath(args.path)
    if not os.path.isfile(path):
        raise ResolveError(f"DRX file not found: {path}")
    items = select_items(resolve, args)
    for index, item in items:
        if not item.GetNodeGraph().ApplyGradeFromDRX(path, DRX_MODES[args.keyframes]):
            raise ResolveError(f"Could not apply {path} to clip {index}.")
    return f"Applied {os.path.basename(path)} to {len(items)} clip(s)"


def cmd_color_reset(resolve, args):
    items = select_items(resolve, args)
    for index, item in items:
        if not item.GetNodeGraph().ResetAllGrades():
            raise ResolveError(f"Could not reset grades on clip {index}.")
    return f"Reset grades on {len(items)} clip(s)"


LUT_EXPORT_TYPES = {"17": "EXPORT_LUT_17PTCUBE", "33": "EXPORT_LUT_33PTCUBE",
                    "65": "EXPORT_LUT_65PTCUBE", "vlut": "EXPORT_LUT_PANASONICVLUT"}


def cmd_color_export_lut(resolve, args):
    item = select_one_item(resolve, args)
    path = os.path.abspath(args.output)
    if not item.ExportLUT(getattr(resolve, LUT_EXPORT_TYPES[args.size]), path):
        raise ResolveError(f"Could not export a LUT from clip {args.index} to {path}.")
    return f"Exported LUT to {path}"


def _find_album(gallery, name):
    if name is None:
        return gallery.GetCurrentStillAlbum()
    for album in gallery.GetGalleryStillAlbums():
        if gallery.GetAlbumName(album) == name:
            return album
    raise ResolveError(f"No still album named {name!r}.")


def cmd_still_list(resolve, args):
    gallery = current_project(resolve).GetGallery()
    albums = [_find_album(gallery, args.album)] if args.album else gallery.GetGalleryStillAlbums()
    return [
        {"album": gallery.GetAlbumName(album), "still": number, "label": album.GetLabel(still)}
        for album in albums
        for number, still in enumerate(album.GetStills(), start=1)
    ]


def cmd_still_grab(resolve, args):
    timeline = find_timeline(current_project(resolve), args.timeline)
    if args.all is None:
        if not timeline.GrabStill():
            raise ResolveError("Could not grab a still. Open the Color page ('dava page color') and retry.")
        return "Grabbed 1 still"
    stills = timeline.GrabAllStills({"first": 1, "middle": 2}[args.all])
    if not stills:
        raise ResolveError("Could not grab stills. Open the Color page ('dava page color') and retry.")
    return f"Grabbed {len(stills)} still(s)"


def cmd_still_export(resolve, args):
    gallery = current_project(resolve).GetGallery()
    album = _find_album(gallery, args.album)
    stills = album.GetStills()
    if not stills:
        raise ResolveError(f"Album {gallery.GetAlbumName(album)!r} has no stills.")
    target = os.path.abspath(args.dir)
    os.makedirs(target, exist_ok=True)
    if not album.ExportStills(stills, target, args.prefix, args.format):
        raise ResolveError(f"Could not export stills as {args.format!r} to {target}.")
    return f"Exported {len(stills)} still(s) to {target}"


def cmd_meta_get(resolve, args):
    clip = find_clip(resolve, args.clip)
    data = clip.GetThirdPartyMetadata() if args.third_party else clip.GetMetadata()
    data = data or {}
    if not args.keys:
        return data
    return {key: data.get(key, "") for key in args.keys}


def cmd_meta_set(resolve, args):
    clip = find_clip(resolve, args.clip)
    values = parse_assignments(args.pairs)
    ok = clip.SetThirdPartyMetadata(values) if args.third_party else clip.SetMetadata(values)
    if not ok:
        raise ResolveError(f"Resolve rejected metadata {values} for {args.clip!r}.")
    return f"Set {len(values)} metadata field(s) on {args.clip}"


def cmd_meta_props(resolve, args):
    props = find_clip(resolve, args.clip).GetClipProperty()
    if not args.keys:
        return props
    missing = [key for key in args.keys if key not in props]
    if missing:
        raise ResolveError(f"Unknown clip propert(ies): {', '.join(missing)}")
    return {key: props[key] for key in args.keys}


def cmd_meta_set_prop(resolve, args):
    clip = find_clip(resolve, args.clip)
    values = parse_assignments(args.pairs)
    failed = [key for key, value in values.items() if not clip.SetClipProperty(key, value)]
    if failed:
        raise ResolveError(f"Resolve rejected clip propert(ies) {', '.join(failed)} (read-only or invalid value?).")
    return f"Set {len(values)} clip propert(ies) on {args.clip}"


def cmd_meta_color(resolve, args):
    clip = find_clip(resolve, args.clip)
    if args.color is None:
        return clip.GetClipColor()
    ok = clip.ClearClipColor() if args.color == "none" else clip.SetClipColor(args.color)
    if not ok:
        raise ResolveError(f"Could not set clip color of {args.clip!r} to {args.color}.")
    return f"{args.clip}: {args.color}"


def cmd_fusion_list(resolve, args):
    return [
        {"clip": index, "name": item.GetName(), "comps": ", ".join(item.GetFusionCompNameList() or [])}
        for index, item in select_items(resolve, args)
    ]


def cmd_fusion_add(resolve, args):
    item = select_one_item(resolve, args)
    if not item.AddFusionComp():
        raise ResolveError(f"Could not add a Fusion composition to clip {args.index}.")
    return f"Added Fusion composition to {item.GetName()}"


def cmd_fusion_import(resolve, args):
    path = os.path.abspath(args.path)
    if not os.path.isfile(path):
        raise ResolveError(f"Composition file not found: {path}")
    item = select_one_item(resolve, args)
    if not item.ImportFusionComp(path):
        raise ResolveError(f"Could not import {path} into clip {args.index}.")
    return f"Imported {os.path.basename(path)} into {item.GetName()}"


def cmd_fusion_export(resolve, args):
    item = select_one_item(resolve, args)
    count = item.GetFusionCompCount()
    if not 1 <= args.comp <= count:
        raise ResolveError(f"Composition {args.comp} is out of range; the clip has {count}.")
    path = os.path.abspath(args.output)
    if not item.ExportFusionComp(path, args.comp):
        raise ResolveError(f"Could not export composition {args.comp} to {path}.")
    return f"Exported composition {args.comp} to {path}"


def cmd_fusion_delete(resolve, args):
    item = select_one_item(resolve, args)
    if not item.DeleteFusionCompByName(args.name):
        raise ResolveError(f"Could not delete composition {args.name!r} from clip {args.index}.")
    return f"Deleted composition {args.name}"


def cmd_fusion_rename(resolve, args):
    item = select_one_item(resolve, args)
    if not item.RenameFusionCompByName(args.old, args.new):
        raise ResolveError(f"Could not rename composition {args.old!r} on clip {args.index}.")
    return f"Renamed composition {args.old} to {args.new}"


# Resolve executables by platform. RESOLVE_APP overrides these.
RESOLVE_APPS = {
    "darwin": "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/MacOS/Resolve",
    "win32": r"C:\Program Files\Blackmagic Design\DaVinci Resolve\Resolve.exe",
    "linux": "/opt/resolve/bin/resolve",
}


def cmd_launch(resolve, args):
    module = load_script_module()
    if module.scriptapp("Resolve") is not None:
        return "Resolve is already running"
    app = os.environ.get("RESOLVE_APP") or RESOLVE_APPS.get(sys.platform)
    if not app or not os.path.isfile(app):
        raise ResolveError(f"Resolve executable not found at {app!r}. Set RESOLVE_APP to its path.")
    command = [app, "-nogui"] if args.nogui else [app]
    process = subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise ResolveError(f"Resolve exited during startup with code {process.returncode}.")
        if module.scriptapp("Resolve") is not None:
            mode = "headless" if args.nogui else "with UI"
            return f"Resolve is running {mode} (pid {process.pid})"
        time.sleep(args.interval)
    raise ResolveError(
        f"Resolve (pid {process.pid}) did not accept a scripting connection within {args.timeout:g}s. "
        "Check that External scripting is set to Local."
    )


def cmd_quit(resolve, args):
    if not resolve.Quit():
        raise ResolveError("Resolve refused to quit.")
    return "Resolve is quitting"


def strip_comment(line):
    """Drop a '#' comment that starts a line or follows whitespace, outside quotes.

    A '#' inside a word is kept, so refs like timeline#ID and item#ID survive.
    """
    quote = None
    for index, char in enumerate(line):
        if quote:
            if char == quote:
                quote = None
            elif char == "\\" and quote == '"':
                continue
        elif char in ("'", '"'):
            quote = char
        elif char == "#" and (index == 0 or line[index - 1].isspace()):
            return line[:index]
    return line


def cmd_batch(resolve, args):
    try:
        if args.file == "-":
            lines = sys.stdin.read().splitlines()
        else:
            with open(args.file, encoding="utf-8") as handle:
                lines = handle.read().splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ResolveError(f"Cannot read batch file {args.file}: {exc}") from exc

    # Parse every line before running any, so a typo on line 20 does not leave a half-applied batch.
    # (Refs and argument values are still checked when each line runs.)
    parser = build_parser()
    commands = []
    for number, line in enumerate(lines, start=1):
        try:
            tokens = shlex.split(strip_comment(line))
        except ValueError as exc:
            raise ResolveError(f"{args.file}:{number}: {exc}") from exc
        if not tokens:
            continue
        try:
            sub = parser.parse_args(tokens)
        except SystemExit as exc:
            raise ResolveError(f"{args.file}:{number}: invalid command: {line.strip()}") from exc
        if sub.func in (cmd_batch, cmd_launch, cmd_mcp):
            raise ResolveError(f"{args.file}:{number}: '{tokens[0]}' cannot be used inside a batch.")
        # Lines share one session, so a $handle saved with 'call --as' is visible to later lines.
        sub.session = args.session
        sub.read_only_mode = args.read_only_mode or sub.read_only_flag
        try:
            ensure_allowed(sub)
        except ResolveError as exc:
            raise ResolveError(f"{args.file}:{number}: {exc}") from exc
        commands.append((number, line.strip(), sub))

    failures = 0
    for number, text, sub in commands:
        print(f"$ dava {text}", file=sys.stderr)
        try:
            # With --json, a batch prints JSON Lines: one compact document per command.
            emit(sub.func(resolve, sub), wants_json(args) or wants_json(sub), compact=True)
        except ResolveError as exc:
            if not args.keep_going:
                raise ResolveError(f"{args.file}:{number}: {exc}") from exc
            print(f"dava: error: {args.file}:{number}: {exc}", file=sys.stderr)
            if wants_json(args) or wants_json(sub):
                # Keep JSON Lines aligned with the commands: one line per command, failures included.
                emit({"error": str(exc), "line": number}, True, compact=True)
            failures += 1
    if failures:
        raise ResolveError(f"{failures} of {len(commands)} batch command(s) failed.")
    return None


def cmd_api_classes(resolve, args):
    return [{"class": c.name, "methods": len(c.methods), "doc": c.doc} for c in args.session.spec.classes.values()]


def _api_class(spec, name):
    if name in spec.classes:
        return spec.classes[name]
    folded = [c for c in spec.classes if c.lower() == name.lower()]
    if folded:
        return spec.classes[folded[0]]
    raise ResolveError(f"No API class {name!r}. Classes: {', '.join(spec.classes)}.")


def cmd_api_methods(resolve, args):
    cls = _api_class(args.session.spec, args.cls)
    if not cls.methods:
        return f"{cls.name} is opaque in the API definition; call its methods with 'dava call REF METHOD --unchecked'."
    return [{"method": m.name, "read_only": m.read_only, "signature": m.signature(), "doc": m.doc}
            for m in cls.methods.values()]


def cmd_api_show(resolve, args):
    return args.session.spec.describe(args.name)


def cmd_api_search(resolve, args):
    results = args.session.spec.search(" ".join(args.query), limit=args.limit)
    if not results:
        raise ResolveError(f"Nothing in the API matches {' '.join(args.query)!r}.")
    return results


def cmd_api_constants(resolve, args):
    prefix = (args.prefix or "").upper()
    return [{"constant": name, "alias": alias}
            for name, alias in args.session.spec.constants.items() if name.startswith(prefix)]


def cmd_api_refs(resolve, args):
    return REF_HELP.rstrip()


def cmd_api_dump(resolve, args):
    return args.session.spec.as_dict()


def split_call_args(args):
    """Split 'dava call' values into positional and named arguments.

    VALUE is positional; NAME=VALUE is named (typed from the string); NAME:=JSON is named and
    parsed as JSON. --args/--kwargs supply JSON directly. Pre-parsed values are tagged
    ("json", value) so the bridge does not parse them again.
    """
    positional, named = [], {}
    if args.args_json is not None:
        values = _load_json(args.args_json, "--args")
        if not isinstance(values, list):
            raise ResolveError("--args must be a JSON list.")
        positional.extend(("json", v) for v in values)
    inline_positional = False
    for token in args.values:
        name, sep, value = token.partition("=")
        if sep and name.endswith(":") and name[:-1].isidentifier():
            named[name[:-1]] = ("json", _load_json(value, f"{name[:-1]}:="))
        elif sep and name.isidentifier():
            named[name] = value
        else:
            positional.append(token)
            inline_positional = True
    if inline_positional and args.args_json is not None:
        raise ResolveError("Give positional arguments either inline or with --args, not both.")
    if args.kwargs_json is not None:
        values = _load_json(args.kwargs_json, "--kwargs")
        if not isinstance(values, dict):
            raise ResolveError("--kwargs must be a JSON object.")
        clash = sorted(set(values) & set(named))
        if clash:
            raise ResolveError(f"Parameter(s) given both inline and in --kwargs: {', '.join(clash)}.")
        named.update({k: ("json", v) for k, v in values.items()})
    return positional, named


def _load_json(text, what):
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ResolveError(f"{what}: invalid JSON ({exc}).") from exc


def cmd_call(resolve, args):
    positional, named = split_call_args(args)
    return bridge.call(
        args.session, args.ref, args.method, positional, named, raw=True, unchecked=args.unchecked,
        read_only=args.read_only_mode, dry_run=args.dry_run, save_as=args.save_as,
    )


def cmd_inspect(resolve, args):
    return bridge.inspect(args.session, args.ref)


def cmd_mcp(resolve, args):
    from .mcp import serve

    serve(read_only=args.read_only_mode or args.mcp_read_only, toolset=args.toolset)
    return None


# Commands that never change Resolve state. 'call' and 'batch' are checked per method / per line.
READ_ONLY_COMMANDS = {
    cmd_status, cmd_project_list, cmd_project_settings, cmd_timeline_list, cmd_media_list,
    cmd_marker_list, cmd_render_presets, cmd_render_jobs, cmd_color_nodes, cmd_still_list,
    cmd_meta_get, cmd_meta_props, cmd_fusion_list, cmd_api_classes, cmd_api_methods, cmd_api_show,
    cmd_api_search, cmd_api_constants, cmd_api_refs, cmd_api_dump, cmd_inspect, cmd_call, cmd_batch, cmd_mcp,
}


def command_is_read_only(args):
    # Commands from dava/commands/ declare read_only themselves (a bool, or a callable of args).
    declared = getattr(args, "read_only", None)
    if declared is not None:
        return bool(declared(args)) if callable(declared) else bool(declared)
    return args.func in READ_ONLY_COMMANDS


def ensure_allowed(args):
    if args.read_only_mode and not command_is_read_only(args):
        name = " ".join(filter(None, [args.command, getattr(args, "action", None)]))
        raise ResolveError(f"'{name}' can change Resolve state and read-only mode is on.")


# ---------------------------------------------------------------------------
# Output


def format_human(result):
    if result is None:
        return ""
    if isinstance(result, dict):
        if any(isinstance(value, (dict, list)) for value in result.values()):
            return json.dumps(result, indent=2, default=str)
        return "\n".join(f"{key}: {value}" for key, value in result.items())
    if isinstance(result, list):
        if result and all(isinstance(row, dict) for row in result):
            return format_table(result)
        return "\n".join(str(item) for item in result)
    return str(result)


def format_table(rows):
    columns = []
    for row in rows:
        columns.extend(key for key in row if key not in columns)
    cells = [[str(row.get(col, "")) for col in columns] for row in rows]
    widths = [max(len(col), *(len(line[i]) for line in cells)) for i, col in enumerate(columns)]
    lines = ["  ".join(col.ljust(w) for col, w in zip(columns, widths)).rstrip()]
    lines += ["  ".join(cell.ljust(w) for cell, w in zip(line, widths)).rstrip() for line in cells]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Parser


def build_parser():
    parser = argparse.ArgumentParser(prog="dava", description="Control DaVinci Resolve from the command line.")
    parser.add_argument("--version", action="version", version=f"dava {__version__}")
    parser.add_argument("--json", action="store_true", help="print results as JSON")
    parser.add_argument("--read-only", dest="read_only_flag", action="store_true",
                        help="refuse anything that can change Resolve state (also: DAVA_READ_ONLY=1)")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    registry = Registry(commands)

    p = commands.add_parser("status", help="show Resolve version, page, project and timeline")
    p.set_defaults(func=cmd_status)

    p = commands.add_parser("page", help="show or switch the current page")
    p.add_argument("name", nargs="?", choices=PAGES)
    p.set_defaults(func=cmd_page, read_only=lambda a: a.name is None)

    project = registry.group("project", "manage projects")
    p = project.add_parser("list", help="list projects in the current project manager folder")
    p.set_defaults(func=cmd_project_list)
    p = project.add_parser("open", help="open a project by name")
    p.add_argument("name")
    p.set_defaults(func=cmd_project_open)
    p = project.add_parser("create", help="create and open a new project")
    p.add_argument("name")
    p.set_defaults(func=cmd_project_create)
    p = project.add_parser("save", help="save the current project")
    p.set_defaults(func=cmd_project_save)
    p = project.add_parser("settings", help="show project settings (all, or the given keys)")
    p.add_argument("keys", nargs="*", metavar="KEY")
    p.set_defaults(func=cmd_project_settings)

    timeline = registry.group("timeline", "manage timelines")
    p = timeline.add_parser("list", help="list timelines in the current project")
    p.set_defaults(func=cmd_timeline_list)
    p = timeline.add_parser("use", help="make a timeline current")
    p.add_argument("name")
    p.set_defaults(func=cmd_timeline_use)
    p = timeline.add_parser("create", help="create an empty timeline")
    p.add_argument("name")
    p.set_defaults(func=cmd_timeline_create)
    p = timeline.add_parser("export", help="export a timeline (EDL, AAF, FCPXML, OTIO, ...)")
    p.add_argument("output", help="output file path")
    p.add_argument("-f", "--format", required=True, choices=sorted(EXPORT_FORMATS))
    p.add_argument("-t", "--timeline", help="timeline name (default: current)")
    p.set_defaults(func=cmd_timeline_export)

    media = registry.group("media", "manage the media pool")
    p = media.add_parser("import", help="import files or folders into the current media pool folder")
    p.add_argument("paths", nargs="+", metavar="PATH")
    p.add_argument("--append", action="store_true", help="also append the imported clips to the current timeline")
    p.set_defaults(func=cmd_media_import)
    p = media.add_parser("list", help="list clips in the current media pool folder")
    p.add_argument("-a", "--all", action="store_true", help="walk every folder from the root")
    p.set_defaults(func=cmd_media_list)

    marker = registry.group("marker", "manage timeline markers")
    p = marker.add_parser("list", help="list markers on a timeline")
    p.add_argument("-t", "--timeline", help="timeline name (default: current)")
    p.set_defaults(func=cmd_marker_list)
    p = marker.add_parser("add", help="add a marker at a frame offset from the timeline start")
    p.add_argument("frame", type=int)
    p.add_argument("-c", "--color", default="Blue", choices=MARKER_COLORS)
    p.add_argument("-n", "--name", default="")
    p.add_argument("--note", default="")
    p.add_argument("-d", "--duration", type=int, default=1, help="duration in frames (default: 1)")
    p.add_argument("-t", "--timeline", help="timeline name (default: current)")
    p.set_defaults(func=cmd_marker_add)

    render = registry.group("render", "manage the render queue")
    p = render.add_parser("presets", help="list render presets")
    p.set_defaults(func=cmd_render_presets)
    p = render.add_parser("jobs", help="list render jobs and their status")
    p.set_defaults(func=cmd_render_jobs)
    p = render.add_parser("add", help="add a render job for a timeline and print its job id")
    p.add_argument("-p", "--preset", help="render preset to load first")
    p.add_argument("-o", "--dir", help="output directory")
    p.add_argument("-n", "--name", help="output file name (without extension); a prefix with --all-timelines")
    which = p.add_mutually_exclusive_group()
    which.add_argument("-t", "--timeline", help="timeline name (default: current)")
    which.add_argument("--all-timelines", action="store_true", help="add one job per timeline, named after it")
    p.set_defaults(func=cmd_render_add)
    p = render.add_parser("start", help="start rendering the given jobs (default: whole queue)")
    p.add_argument("job_ids", nargs="*", metavar="JOB_ID")
    p.add_argument("-w", "--wait", action="store_true", help="block until rendering finishes")
    p.add_argument("--interval", type=non_negative_float, default=1.0, help="poll interval in seconds with --wait")
    p.set_defaults(func=cmd_render_start)
    p = render.add_parser("stop", help="stop any render in progress")
    p.set_defaults(func=cmd_render_stop)

    color = registry.group("color", "grade timeline clips: LUTs, CDL, DRX, nodes")
    p = color.add_parser("nodes", help="list nodes with their labels, LUTs and tools")
    add_item_args(p)
    p.set_defaults(func=cmd_color_nodes)
    p = color.add_parser("lut", help="set a LUT on a node")
    p.add_argument("lut", help="LUT path, absolute or relative to Resolve's LUT folders")
    p.add_argument("-N", "--node", type=int, default=1, help="node number, from 1 (default: 1)")
    add_item_args(p)
    p.set_defaults(func=cmd_color_lut)
    p = color.add_parser("cdl", help="set ASC CDL values on a node")
    p.add_argument("--slope", metavar="'R G B'")
    p.add_argument("--offset", metavar="'R G B'")
    p.add_argument("--power", metavar="'R G B'")
    p.add_argument("--saturation", type=float)
    p.add_argument("-N", "--node", type=int, default=1, help="node number, from 1 (default: 1)")
    add_item_args(p)
    p.set_defaults(func=cmd_color_cdl)
    p = color.add_parser("drx", help="apply a grade from a .drx still file")
    p.add_argument("path")
    p.add_argument("-k", "--keyframes", choices=sorted(DRX_MODES), default="none",
                   help="keyframe alignment (default: none)")
    add_item_args(p)
    p.set_defaults(func=cmd_color_drx)
    p = color.add_parser("reset", help="reset all grades")
    add_item_args(p)
    p.set_defaults(func=cmd_color_reset)
    p = color.add_parser("export-lut", help="export a clip's grade as a LUT")
    p.add_argument("output")
    p.add_argument("-s", "--size", choices=list(LUT_EXPORT_TYPES), default="33",
                   help="17/33/65 point cube, or Panasonic VLUT (default: 33)")
    add_item_args(p, single=True)
    p.set_defaults(func=cmd_color_export_lut)

    still = registry.group("still", "gallery stills")
    p = still.add_parser("list", help="list stills in every album, or one album")
    p.add_argument("-a", "--album")
    p.set_defaults(func=cmd_still_list)
    p = still.add_parser("grab", help="grab a still from the current clip (Color page)")
    p.add_argument("--all", choices=["first", "middle"], help="grab from every clip at this frame instead")
    p.add_argument("-t", "--timeline", help="timeline name (default: current)")
    p.set_defaults(func=cmd_still_grab)
    p = still.add_parser("export", help="export every still of an album")
    p.add_argument("dir")
    p.add_argument("-a", "--album", help="album name (default: current album)")
    p.add_argument("--prefix", default="still", help="file name prefix (default: still)")
    p.add_argument("-f", "--format", default="jpg", help="image format, e.g. jpg, png, tif, dpx, drx (default: jpg)")
    p.set_defaults(func=cmd_still_export)

    meta = registry.group("meta", "media pool clip metadata and properties")
    p = meta.add_parser("get", help="show metadata of a clip (all, or the given keys)")
    p.add_argument("clip")
    p.add_argument("keys", nargs="*", metavar="KEY")
    p.add_argument("--third-party", action="store_true", help="use third-party metadata")
    p.set_defaults(func=cmd_meta_get)
    p = meta.add_parser("set", help="set metadata fields")
    p.add_argument("clip")
    p.add_argument("pairs", nargs="+", metavar="KEY=VALUE")
    p.add_argument("--third-party", action="store_true", help="use third-party metadata")
    p.set_defaults(func=cmd_meta_set)
    p = meta.add_parser("props", help="show clip properties (all, or the given keys)")
    p.add_argument("clip")
    p.add_argument("keys", nargs="*", metavar="KEY")
    p.set_defaults(func=cmd_meta_props)
    p = meta.add_parser("set-prop", help="set clip properties")
    p.add_argument("clip")
    p.add_argument("pairs", nargs="+", metavar="KEY=VALUE")
    p.set_defaults(func=cmd_meta_set_prop)
    p = meta.add_parser("color", help="show or set a clip's color ('none' clears it)")
    p.add_argument("clip")
    p.add_argument("color", nargs="?", choices=CLIP_COLORS + ["none"])
    p.set_defaults(func=cmd_meta_color, read_only=lambda a: a.color is None)

    fusion = registry.group("fusion", "Fusion compositions on timeline clips")
    p = fusion.add_parser("list", help="list compositions per clip")
    add_item_args(p)
    p.set_defaults(func=cmd_fusion_list)
    p = fusion.add_parser("add", help="add an empty composition to a clip")
    add_item_args(p, single=True)
    p.set_defaults(func=cmd_fusion_add)
    p = fusion.add_parser("import", help="import a .comp file into a clip")
    p.add_argument("path")
    add_item_args(p, single=True)
    p.set_defaults(func=cmd_fusion_import)
    p = fusion.add_parser("export", help="export a clip's composition to a .comp file")
    p.add_argument("output")
    p.add_argument("-c", "--comp", type=int, default=1, help="composition number, from 1 (default: 1)")
    add_item_args(p, single=True)
    p.set_defaults(func=cmd_fusion_export)
    p = fusion.add_parser("delete", help="delete a composition by name")
    p.add_argument("name")
    add_item_args(p, single=True)
    p.set_defaults(func=cmd_fusion_delete)
    p = fusion.add_parser("rename", help="rename a composition")
    p.add_argument("old")
    p.add_argument("new")
    add_item_args(p, single=True)
    p.set_defaults(func=cmd_fusion_rename)

    p = commands.add_parser("launch", help="start Resolve and wait until scripting is available")
    p.add_argument("--nogui", action="store_true", help="run headless, without the user interface")
    p.add_argument("--timeout", type=float, default=120.0, help="seconds to wait (default: 120)")
    p.add_argument("--interval", type=non_negative_float, default=2.0, help="poll interval in seconds (default: 2)")
    p.set_defaults(func=cmd_launch, needs_resolve=False)
    p = commands.add_parser("quit", help="quit Resolve")
    p.set_defaults(func=cmd_quit)
    p = commands.add_parser("batch", help="run dava commands from a file ('-' for stdin) over one connection")
    p.add_argument("file")
    p.add_argument("-k", "--keep-going", action="store_true", help="continue after a failed command")
    p.set_defaults(func=cmd_batch)

    api = registry.group("api", "browse the full Resolve scripting API (no Resolve needed)")
    p = api.add_parser("classes", help="list API classes")
    p.set_defaults(func=cmd_api_classes, needs_resolve=False)
    p = api.add_parser("methods", help="list the methods of a class")
    p.add_argument("cls", metavar="CLASS")
    p.set_defaults(func=cmd_api_methods, needs_resolve=False)
    p = api.add_parser("show", help="describe a class, Class.Method, typed dict, alias or constant")
    p.add_argument("name")
    p.set_defaults(func=cmd_api_show, needs_resolve=False, always_json=True)
    p = api.add_parser("search", help="search method names and docs, types and constants")
    p.add_argument("query", nargs="+")
    p.add_argument("-l", "--limit", type=positive_int, default=50)
    p.set_defaults(func=cmd_api_search, needs_resolve=False)
    p = api.add_parser("constants", help="list resolve.* constants, optionally by prefix")
    p.add_argument("prefix", nargs="?")
    p.set_defaults(func=cmd_api_constants, needs_resolve=False)
    p = api.add_parser("refs", help="explain the REF syntax used by 'call' and 'inspect'")
    p.set_defaults(func=cmd_api_refs, needs_resolve=False)
    p = api.add_parser("dump", help="the whole API definition as JSON")
    p.set_defaults(func=cmd_api_dump, needs_resolve=False, always_json=True)

    p = commands.add_parser(
        "call", help="call any API method on any object",
        description="Call METHOD on the object REF names. VALUE is positional, NAME=VALUE is named, "
                    "NAME:=JSON is named and parsed as JSON. See 'dava api refs' and 'dava api show Class.Method'.",
    )
    p.add_argument("ref", metavar="REF")
    p.add_argument("method", metavar="METHOD")
    p.add_argument("values", nargs="*", metavar="VALUE | NAME=VALUE | NAME:=JSON")
    p.add_argument("--args", dest="args_json", metavar="JSON_LIST", help="positional arguments as a JSON list")
    p.add_argument("--kwargs", dest="kwargs_json", metavar="JSON_OBJECT", help="named arguments as a JSON object")
    p.add_argument("--as", dest="save_as", metavar="NAME", help="keep the result as $NAME for later lines of a batch")
    p.add_argument("--dry-run", action="store_true", help="resolve and check arguments without calling")
    p.add_argument("--unchecked", action="store_true", help="allow methods missing from the API definition")
    p.set_defaults(func=cmd_call, always_json=True)

    p = commands.add_parser("inspect", help="show what a REF points at and the methods it has")
    p.add_argument("ref", metavar="REF")
    p.set_defaults(func=cmd_inspect, always_json=True)

    p = commands.add_parser("mcp", help="serve dava as MCP tools over stdio (for AI clients)")
    p.add_argument("--read-only", dest="mcp_read_only", action="store_true",
                   help="expose only tools that cannot change Resolve state")
    p.add_argument("--toolset", choices=["full", "core"], default="full",
                   help="full: core tools plus one tool per dava command (default); core: 7 generic tools")
    p.set_defaults(func=cmd_mcp, needs_resolve=False)

    # Domain modules in dava/commands/ add their own commands and group actions.
    load_command_modules(registry)
    return parser


def emit(result, as_json, compact=False):
    if result is None:
        return
    if as_json:
        print(json.dumps(result, default=str) if compact else json.dumps(result, indent=2, default=str))
        return
    text = format_human(result)
    if text:
        print(text)


def wants_json(args):
    """--json, or a command whose output is only meaningful as JSON (call, inspect, api show/dump)."""
    return args.json or getattr(args, "always_json", False)


def read_only_env():
    """DAVA_READ_ONLY: off for empty/0/false/no/off/n/f; any other value turns read-only on (fail safe)."""
    return os.environ.get("DAVA_READ_ONLY", "").strip().lower() not in ("", "0", "false", "no", "off", "n", "f")


def non_negative_float(text):
    value = float(text)
    if not value >= 0:  # also rejects nan
        raise argparse.ArgumentTypeError(f"must be 0 or more, got {text}")
    return value


def positive_int(text):
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {text}")
    return value


def main(argv=None, resolve=None):
    args = build_parser().parse_args(argv)
    args.read_only_mode = args.read_only_flag or read_only_env()
    try:
        ensure_allowed(args)
        if resolve is None and getattr(args, "needs_resolve", True):
            resolve = get_resolve()
        args.session = Session(resolve=resolve)
        result = args.func(resolve, args)
    except ResolveError as exc:
        print(f"dava: error: {exc}", file=sys.stderr)
        return 1
    try:
        emit(result, wants_json(args))
    except BrokenPipeError:
        # The reader went away (e.g. '| head'); silence the flush at interpreter exit too.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    return 0
