"""dava render (formats, codecs, settings, job details, wait), dava preset and dava quick.

Render formats are named by Resolve (e.g. 'QuickTime') and selected by file extension ('mov'); codecs
have a description ('Apple ProRes 422 HQ') and a name ('ProRes422HQ'). Commands here accept either
form and pass the extension and codec name on to Resolve.
"""

import argparse
import os
import sys
import time
from argparse import ArgumentError

from ..connect import ResolveError
from ..helpers import apply_render_settings, current_project, find_timeline, typed_assignments

RENDER_MODES = {"individual": 0, "single": 1}
MODE_NAMES = {value: name for name, value in RENDER_MODES.items()}

# RenderJobStatus.JobStatus values after which a job does not change any more.
DONE_JOB_STATES = {"Complete", "Failed", "Cancelled", "Background Render Cancelled", "Remote Render Cancelled"}
FAILED_JOB_STATES = DONE_JOB_STATES - {"Complete"}

# QuickExportRenderStatus.JobStatus values that mean the export worked.
QUICK_OK_STATES = {"Render Complete", "Upload Completed"}


# ---------------------------------------------------------------------------
# Formats and codecs


def _formats(project, audio):
    return (project.GetAudioRenderFormats() if audio else project.GetRenderFormats()) or {}


def _codecs(project, extension, audio):
    return (project.GetAudioRenderCodecs(extension) if audio else project.GetRenderCodecs(extension)) or {}


def _format_extension(project, text, audio=False):
    """Map a format name or extension to the extension Resolve expects; unknown text is passed through."""
    formats = _formats(project, audio)
    for name, extension in formats.items():
        if text == extension or text.lower() in (name.lower(), str(extension).lower()):
            return extension
    return text


def _codec_name(project, extension, text, audio=False):
    """Map a codec description or name to the codec name Resolve expects; unknown text is passed through."""
    for description, name in _codecs(project, extension, audio).items():
        if text == name or text.lower() in (description.lower(), str(name).lower()):
            return name
    return text


def _choices_hint(project, extension, audio=False):
    """' Formats: ...' when the extension is unknown, else ' Codecs for EXT: ...'."""
    formats = _formats(project, audio)
    if extension not in formats.values():
        listed = ", ".join(f"{name} ({ext})" for name, ext in sorted(formats.items()))
        return f" Formats: {listed}." if listed else " See 'dava render formats'."
    codecs = _codecs(project, extension, audio)
    listed = ", ".join(f"{description} ({name})" for description, name in sorted(codecs.items()))
    return f" Codecs for {extension}: {listed}." if listed else f" Resolve lists no codecs for {extension}."


def _current_format(project):
    current = project.GetCurrentRenderFormatAndCodec()
    if not current:
        raise ResolveError("Resolve did not report the current render format and codec.")
    return current


def _codec_rows(codecs):
    return [{"codec": name, "description": description} for description, name in sorted(codecs.items())]


def cmd_render_formats(resolve, args):
    project = current_project(resolve)
    formats = _formats(project, args.audio)
    if not formats:
        raise ResolveError(f"Resolve returned no {'audio ' if args.audio else ''}render formats.")
    rows = [{"format": name, "extension": extension} for name, extension in sorted(formats.items())]
    if args.codecs:
        for row in rows:
            row["codecs"] = _codec_rows(_codecs(project, row["extension"], args.audio))
    return rows


def cmd_render_codecs(resolve, args):
    project = current_project(resolve)
    if args.format is None:
        if args.audio:
            raise ResolveError("Give an audio format, e.g. 'dava render codecs wav --audio'."
                               + _choices_hint(project, None, audio=True))
        args.format = _current_format(project)["format"]
    extension = _format_extension(project, args.format, args.audio)
    codecs = _codecs(project, extension, args.audio)
    if not codecs:
        raise ResolveError(f"Resolve lists no codecs for format {args.format!r}."
                           + _choices_hint(project, extension, args.audio))
    return _codec_rows(codecs)


def cmd_render_format(resolve, args):
    project = current_project(resolve)
    if args.format is None:
        return _current_format(project)
    extension = _format_extension(project, args.format)
    if args.codec is None:
        raise ResolveError(f"Give the codec too: 'dava render format {extension} CODEC'."
                           + _choices_hint(project, extension))
    codec = _codec_name(project, extension, args.codec)
    if not project.SetCurrentRenderFormatAndCodec(extension, codec):
        raise ResolveError(f"Resolve rejected format {extension!r} with codec {codec!r}."
                           + _choices_hint(project, extension))
    return _current_format(project)


def cmd_render_mode(resolve, args):
    project = current_project(resolve)
    if args.mode is None:
        mode = project.GetCurrentRenderMode()
        # False is a failure, not mode 0 (False == 0 would read as 'individual').
        if isinstance(mode, bool) or not isinstance(mode, (int, float)):
            raise ResolveError(f"Resolve did not report the render mode (got {mode!r}).")
        return MODE_NAMES.get(mode, mode)
    if not project.SetCurrentRenderMode(RENDER_MODES[args.mode]):
        raise ResolveError(f"Resolve refused render mode {args.mode!r}.")
    return f"Render mode: {args.mode}"


def cmd_render_resolutions(resolve, args):
    project = current_project(resolve)
    call_args = []
    if args.format is not None:
        call_args.append(_format_extension(project, args.format))
        if args.codec is not None:
            call_args.append(_codec_name(project, call_args[0], args.codec))
    resolutions = project.GetRenderResolutions(*call_args)
    if not resolutions:
        hint = _choices_hint(project, call_args[0]) if call_args else ""
        raise ResolveError(f"Resolve lists no render resolutions for {' '.join(call_args) or 'the current format'}."
                           + hint)
    return list(resolutions)


# ---------------------------------------------------------------------------
# Render settings


def _int_or_text(field_type):
    return field_type[0] == "union" and ("prim", "int") in field_type[1] and ("prim", "str") in field_type[1]


def cmd_render_settings(resolve, args):
    values = typed_assignments(args.session, args.pairs, "RenderSettings")
    fields = args.session.spec.typeddicts["RenderSettings"].fields
    for key, value in values.items():
        # 'int | str' keys (VideoQuality) keep command-line text as str; digits mean the int form (a bit rate).
        if isinstance(value, str) and _int_or_text(fields[key][0]) and value.strip().lstrip("-").isdigit():
            values[key] = int(value)
    if "TargetDir" in values:
        values["TargetDir"] = os.path.abspath(os.path.expanduser(values["TargetDir"]))
    rejected = apply_render_settings(current_project(resolve), values)
    if rejected:
        applied = [key for key in values if key not in rejected]
        message = (f"Resolve rejected render setting(s): {', '.join(f'{k}={values[k]!r}' for k in rejected)}."
                   f" Applied: {', '.join(applied) or 'none'}.")
        if rejected == ["SelectAllFrames"] and values["SelectAllFrames"] is False:
            message += " Resolve rejects SelectAllFrames=False; set MarkIn and MarkOut to render a range."
        raise ResolveError(message)
    return values


# ---------------------------------------------------------------------------
# Render jobs


def _jobs(project):
    jobs = project.GetRenderJobList()
    if jobs is None or isinstance(jobs, bool):
        raise ResolveError("Resolve did not return the render job list.")
    return list(jobs)


def _pick_jobs(project, job_ids):
    """Return the jobs with the given ids (exact, or a unique prefix), or every job when none are given."""
    jobs = _jobs(project)
    if not job_ids:
        return jobs
    by_id = {job["JobId"]: job for job in jobs}
    picked = []
    for text in job_ids:
        matches = [by_id[text]] if text in by_id else [job for key, job in by_id.items() if key.startswith(text)]
        if len(matches) != 1:
            problem = "matches several render jobs" if matches else "matches no render job"
            queued = f"Queued job ids: {', '.join(by_id)}." if by_id else "The render queue is empty."
            raise ResolveError(f"{text!r} {problem}. {queued}")
        picked.append(matches[0])
    return picked


def _status(project, job_id):
    status = project.GetRenderJobStatus(job_id)
    if not status:
        raise ResolveError(f"Resolve returned no status for render job {job_id}.")
    return {"JobId": job_id, **status}


def _busy_hint(project):
    return " Rendering is in progress; stop it with 'dava render stop' first." if project.IsRenderingInProgress() else ""


def cmd_render_info(resolve, args):
    project = current_project(resolve)
    return [{**job, **_status(project, job["JobId"])} for job in _pick_jobs(project, args.job_ids)]


def cmd_render_status(resolve, args):
    project = current_project(resolve)
    jobs = _pick_jobs(project, args.job_ids)
    return {"rendering": bool(project.IsRenderingInProgress()),
            "jobs": [_status(project, job["JobId"]) for job in jobs]}


def cmd_render_delete(resolve, args):
    project = current_project(resolve)
    if args.all:
        count = len(_jobs(project))
        if not project.DeleteAllRenderJobs():
            raise ResolveError("Resolve refused to clear the render queue." + _busy_hint(project))
        return f"Deleted {count} render job(s)"
    deleted = []
    for job in _pick_jobs(project, args.job_ids):
        if not project.DeleteRenderJob(job["JobId"]):
            done = f" Already deleted: {', '.join(deleted)}." if deleted else ""
            raise ResolveError(f"Resolve refused to delete render job {job['JobId']}.{done}" + _busy_hint(project))
        deleted.append(job["JobId"])
    return f"Deleted {len(deleted)} render job(s): {', '.join(deleted)}"


def _progress_line(statuses):
    parts = []
    for status in statuses:
        text = f"{status['JobId'][:8]} {status.get('JobStatus')} {status.get('CompletionPercentage', 0)}%"
        if status.get("EstimatedTimeRemainingInMs"):
            text += f" ~{status['EstimatedTimeRemainingInMs'] / 1000:.0f}s left"
        parts.append(text)
    return " | ".join(parts)


def _pending(statuses):
    return [status for status in statuses if status.get("JobStatus") not in DONE_JOB_STATES]


def wait_for_jobs(project, job_ids, timeout=0, interval=1.0, progress=True):
    """Poll until every job is Complete, Failed or Cancelled; return their final statuses.

    Raises ResolveError when the timeout (seconds, 0 = none) runs out, or when rendering stops while a
    job has not finished (e.g. it was never started).
    """
    deadline = time.monotonic() + timeout if timeout else None
    shown = 0  # length of the progress line on stderr, so a shorter line can blank out the old one
    try:
        while True:
            statuses = [_status(project, job_id) for job_id in job_ids]
            if progress:
                line = _progress_line(statuses)
                print("\r" + line.ljust(shown), end="", file=sys.stderr, flush=True)
                shown = max(shown, len(line))
            if not _pending(statuses):
                return statuses
            if not project.IsRenderingInProgress():
                # A job can finish between the two calls: look once more before giving up.
                statuses = [_status(project, job_id) for job_id in job_ids]
                pending = _pending(statuses)
                if not pending:
                    return statuses
                listed = ", ".join(f"{s['JobId']} ({s.get('JobStatus')})" for s in pending)
                raise ResolveError(f"Rendering is not in progress but job(s) {listed} have not finished. "
                                   f"Start them with 'dava render start {' '.join(s['JobId'] for s in pending)}'.")
            if deadline is not None and time.monotonic() >= deadline:
                listed = ", ".join(f"{s['JobId']} ({s.get('JobStatus')} {s.get('CompletionPercentage', 0)}%)"
                                   for s in _pending(statuses))
                raise ResolveError(f"Timed out after {timeout:g}s waiting for render job(s) {listed}. "
                                   "Rendering continues; wait again or stop it with 'dava render stop'.")
            # Never sleep past the deadline, so --timeout is kept even with a long --interval. The clock
            # moves on after the check above, so the rest can be below 0, which time.sleep rejects.
            time.sleep(interval if deadline is None else max(0.0, min(interval, deadline - time.monotonic())))
    finally:
        if shown:
            print(file=sys.stderr)


def cmd_render_wait(resolve, args):
    project = current_project(resolve)
    jobs = _pick_jobs(project, args.job_ids)
    statuses = wait_for_jobs(project, [job["JobId"] for job in jobs], args.timeout, args.interval,
                             progress=not args.quiet)
    for job, status in zip(jobs, statuses):
        status["OutputPath"] = os.path.join(job.get("TargetDir", ""), job.get("OutputFilename", ""))
    failed = [s for s in statuses if s.get("JobStatus") in FAILED_JOB_STATES]
    if failed:
        details = "; ".join(f"{s['JobId']}: {s.get('JobStatus')} {s.get('Error', '')}".strip() for s in failed)
        raise ResolveError(f"{len(failed)} render job(s) did not complete: {details}")
    return statuses


# ---------------------------------------------------------------------------
# Presets


def _names(names, what):
    if names is None or isinstance(names, bool):
        raise ResolveError(f"Resolve did not return the {what} preset list.")
    return list(names)


def _render_presets(project):
    return _names(project.GetRenderPresetList(), "render")


def _burn_in_presets(resolve):
    return _names(resolve.GetBurnInPresetList(), "data burn-in")


def _quick_presets(project):
    return _names(project.GetQuickExportRenderPresets(), "Quick Export")


def _listing(names, what):
    return f" {what} presets: {', '.join(names)}." if names else f" There are no {what} presets."


def _preset_hint(resolve, burn_in):
    """List the known presets for an error message (render presets need an open project)."""
    if burn_in:
        return _listing(resolve.GetBurnInPresetList() or [], "Data burn-in")
    project = resolve.GetProjectManager().GetCurrentProject()
    if not project:
        return " Open a project to list the render presets."
    return _listing(project.GetRenderPresetList() or [], "Render")


def _kind(args):
    return "data burn-in" if args.burn_in else "render"


def cmd_preset_list(resolve, args):
    if args.burn_in:
        return _burn_in_presets(resolve)
    project = current_project(resolve)
    quick = set(_quick_presets(project))
    return [{"name": name, "quick_export": name in quick} for name in _render_presets(project)]


def cmd_preset_load(resolve, args):
    project = current_project(resolve)
    ok = project.LoadBurnInPreset(args.name) if args.burn_in else project.LoadRenderPreset(args.name)
    if not ok:
        raise ResolveError(f"Could not load {_kind(args)} preset {args.name!r}." + _preset_hint(resolve, args.burn_in))
    return f"Loaded {_kind(args)} preset {args.name}"


def cmd_preset_save(resolve, args):
    if not current_project(resolve).SaveAsNewRenderPreset(args.name):
        raise ResolveError(f"Could not save render preset {args.name!r}; if the name is taken, "
                           f"use 'dava preset update {args.name}'.")
    return f"Saved the current render settings as preset {args.name}"


def cmd_preset_update(resolve, args):
    if not current_project(resolve).UpdateRenderPreset(args.name):
        raise ResolveError(f"Could not update render preset {args.name!r}." + _preset_hint(resolve, False))
    return f"Updated render preset {args.name} with the current render settings"


def cmd_preset_delete(resolve, args):
    if args.burn_in:
        ok = resolve.DeleteBurnInPreset(args.name)
    else:
        ok = current_project(resolve).DeleteRenderPreset(args.name)
    if not ok:
        raise ResolveError(f"Could not delete {_kind(args)} preset {args.name!r}." + _preset_hint(resolve, args.burn_in))
    return f"Deleted {_kind(args)} preset {args.name}"


def cmd_preset_import(resolve, args):
    path = os.path.abspath(os.path.expanduser(args.path))
    if not os.path.isfile(path):
        raise ResolveError(f"Preset file not found: {path}")
    if args.burn_in:
        before = _burn_in_presets(resolve)
        ok = resolve.ImportBurnInPreset(path)
        after = _burn_in_presets(resolve) if ok else []
    else:
        project = current_project(resolve)
        before = _render_presets(project)
        ok = resolve.ImportRenderPreset(path)
        after = _render_presets(project) if ok else []
    if not ok:
        raise ResolveError(f"Resolve could not import {_kind(args)} preset file {path}.")
    return {"imported": path, "kind": _kind(args), "new_presets": [name for name in after if name not in before]}


def cmd_preset_export(resolve, args):
    path = os.path.abspath(os.path.expanduser(args.path))
    if not os.path.isdir(os.path.dirname(path)):
        raise ResolveError(f"Folder not found: {os.path.dirname(path)}")
    if args.burn_in:
        ok = resolve.ExportBurnInPreset(args.name, path)
    else:
        ok = resolve.ExportRenderPreset(args.name, path)
    if not ok:
        raise ResolveError(f"Could not export {_kind(args)} preset {args.name!r} to {path}."
                           + _preset_hint(resolve, args.burn_in))
    return f"Exported {_kind(args)} preset {args.name} to {path}"


# ---------------------------------------------------------------------------
# Quick Export


def cmd_quick_list(resolve, args):
    return _quick_presets(current_project(resolve))


def cmd_quick_export(resolve, args):
    project = current_project(resolve)
    timeline = find_timeline(project, args.timeline)
    if args.timeline is not None and not project.SetCurrentTimeline(timeline):
        raise ResolveError(f"Could not make {timeline.GetName()!r} the current timeline.")
    settings = {}
    if args.dir:
        settings["TargetDir"] = os.path.abspath(os.path.expanduser(args.dir))
    if args.name:
        settings["CustomName"] = args.name
    if args.quality is not None:
        settings["VideoQuality"] = args.quality
    if args.upload:
        settings["EnableUpload"] = True
    status = project.RenderWithQuickExport(args.preset, settings)
    if not status:
        raise ResolveError(f"Quick Export with preset {args.preset!r} did not run."
                           + _listing(_quick_presets(project), "Quick Export"))
    if status.get("JobStatus") not in QUICK_OK_STATES:
        raise ResolveError(f"Quick Export with preset {args.preset!r} ended with {status.get('JobStatus')!r}: "
                           f"{status.get('Error') or 'Resolve gave no details'}.")
    return {"preset": args.preset, "timeline": timeline.GetName(), "settings": settings, **status}


def cmd_quick_enable(resolve, args):
    project = current_project(resolve)
    verb = "enable" if args.enabled else "disable"
    if not project.SetQuickExportEnabledForRenderPreset(args.preset, args.enabled):
        raise ResolveError(f"Could not {verb} Quick Export for render preset {args.preset!r}."
                           + _preset_hint(resolve, False))
    return f"Quick Export {verb}d for render preset {args.preset}"


# ---------------------------------------------------------------------------
# Parser


def non_negative_float(text):
    value = float(text)
    if not value >= 0:  # also rejects nan
        raise argparse.ArgumentTypeError(f"must be 0 or more, got {text}")
    return value


class _NestedOutput(argparse._StoreTrueAction):
    """A store-true flag whose result is nested, so it also switches the output to JSON.

    It stays a _StoreTrueAction so the MCP tool schema offers it as a boolean.
    """

    def __call__(self, parser, namespace, values, option_string=None):
        super().__call__(parser, namespace, values, option_string)
        namespace.always_json = True


class _PositiveSeconds(argparse.Action):
    """Store a float option (type=float) that must be more than 0 and finite; usage error otherwise.

    Checked here rather than in a custom type= function so the MCP tool schema still sees a number.
    """

    def __call__(self, parser, namespace, values, option_string=None):
        if not 0 < values < float("inf"):  # also rejects nan
            raise ArgumentError(self, f"must be a number of seconds more than 0, got {values:g}")
        setattr(namespace, self.dest, values)


def _job_ids(p, help_text):
    p.add_argument("job_ids", nargs="*", metavar="JOB_ID", help=help_text)


def _format_args(p, what):
    p.add_argument("format", nargs="?", help=f"render format name or extension, e.g. QuickTime or mov ({what})")
    p.add_argument("codec", nargs="?", help=f"codec name or description, e.g. H264 or 'H.264' ({what})")


def _register_render(registry):
    p = registry.action("render", "formats", "list render formats and their file extensions")
    p.add_argument("-a", "--audio", action="store_true", help="audio-only render formats")
    p.add_argument("-c", "--codecs", action=_NestedOutput, help="also list the codecs of every format (JSON output)")
    p.set_defaults(func=cmd_render_formats, read_only=True)

    p = registry.action("render", "codecs", "list the codecs of a render format (name and description)")
    p.add_argument("format", nargs="?",
                   help="format name or extension, e.g. QuickTime or mov (default: the current render format)")
    p.add_argument("-a", "--audio", action="store_true", help="audio codecs of an audio-only format")
    p.set_defaults(func=cmd_render_codecs, read_only=True)

    p = registry.action("render", "format", "show the current render format and codec, or set both "
                        "(e.g. 'dava render format mp4 H264')")
    _format_args(p, "omit both to show the current ones")
    p.set_defaults(func=cmd_render_format, read_only=lambda a: a.codec is None)

    p = registry.action("render", "mode", "show or set the render mode: individual clips or a single clip")
    p.add_argument("mode", nargs="?", choices=sorted(RENDER_MODES))
    p.set_defaults(func=cmd_render_mode, read_only=lambda a: a.mode is None)

    p = registry.action("render", "resolutions", "list the resolutions of a render format and codec "
                        "(default: the current ones)")
    _format_args(p, "optional")
    p.set_defaults(func=cmd_render_resolutions, read_only=True)

    p = registry.action("render", "settings", "set render settings, one key at a time; keys and types: "
                        "'dava api show RenderSettings'")
    p.add_argument("pairs", nargs="+", metavar="KEY=VALUE",
                   help="e.g. MarkIn=86400 MarkOut=86639 TargetDir=~/Renders ExportAudio=false VideoQuality=High")
    p.set_defaults(func=cmd_render_settings, read_only=False)

    p = registry.action("render", "info", "full details and status of render jobs (every setting Resolve reports)")
    _job_ids(p, "job id or unique prefix (default: every job)")
    p.set_defaults(func=cmd_render_info, read_only=True, always_json=True)

    p = registry.action("render", "status", "whether Resolve is rendering, and the status of render jobs")
    _job_ids(p, "job id or unique prefix (default: every job)")
    p.set_defaults(func=cmd_render_status, read_only=True, always_json=True)

    p = registry.action("render", "delete", "delete render jobs from the queue")
    which = p.add_mutually_exclusive_group(required=True)
    which.add_argument("job_ids", nargs="*", metavar="JOB_ID", help="job id or unique prefix")
    which.add_argument("--all", action="store_true", help="delete every job in the render queue")
    p.set_defaults(func=cmd_render_delete, read_only=False)

    p = registry.action("render", "wait", "wait until render jobs finish, with progress on stderr; fails if a job "
                        "fails, is cancelled, is not rendering, or the timeout runs out")
    p.add_argument("job_ids", nargs="+", metavar="JOB_ID", help="job id or unique prefix")
    p.add_argument("--timeout", type=non_negative_float, default=0.0,
                   help="give up after this many seconds (default: 0, no limit); rendering continues")
    p.add_argument("--interval", type=float, action=_PositiveSeconds, default=1.0,
                   help="poll interval in seconds, more than 0 (default: 1)")
    p.add_argument("-q", "--quiet", action="store_true", help="no progress on stderr")
    p.set_defaults(func=cmd_render_wait, read_only=True)


def _register_presets(registry):
    burn_in_help = "a data burn-in preset instead of a render preset"
    p = registry.action("preset", "list", "list render presets (and which are enabled for Quick Export)",
                        group_help="render and data burn-in presets: list, load, save, update, delete, "
                                   "import, export")
    p.add_argument("-b", "--burn-in", action="store_true", help="list data burn-in presets instead")
    p.set_defaults(func=cmd_preset_list, read_only=True)

    p = registry.action("preset", "load", "load a render preset into the current render settings "
                        "(or a data burn-in preset into the project)")
    p.add_argument("name")
    p.add_argument("-b", "--burn-in", action="store_true", help=burn_in_help)
    p.set_defaults(func=cmd_preset_load, read_only=False)

    p = registry.action("preset", "save", "save the current render settings as a new render preset")
    p.add_argument("name")
    p.set_defaults(func=cmd_preset_save, read_only=False)

    p = registry.action("preset", "update", "overwrite a render preset with the current render settings")
    p.add_argument("name")
    p.set_defaults(func=cmd_preset_update, read_only=False)

    p = registry.action("preset", "delete", "delete a render preset (or a data burn-in preset)")
    p.add_argument("name")
    p.add_argument("-b", "--burn-in", action="store_true", help=burn_in_help)
    p.set_defaults(func=cmd_preset_delete, read_only=False)

    p = registry.action("preset", "import", "import a preset file; a render preset is also selected "
                        "(prints the names of new presets)")
    p.add_argument("path", help="preset file (.xml)")
    p.add_argument("-b", "--burn-in", action="store_true", help=burn_in_help)
    p.set_defaults(func=cmd_preset_import, read_only=False)

    p = registry.action("preset", "export", "export a render preset (or a data burn-in preset) to a file")
    p.add_argument("name")
    p.add_argument("path", help="output file (.xml); its folder must exist")
    p.add_argument("-b", "--burn-in", action="store_true", help=burn_in_help)
    p.set_defaults(func=cmd_preset_export, read_only=False)


def _register_quick(registry):
    p = registry.action("quick", "list", "list Quick Export presets",
                        group_help="Quick Export: render the current timeline with a Quick Export preset")
    p.set_defaults(func=cmd_quick_list, read_only=True)

    p = registry.action("quick", "export", "render a timeline with a Quick Export preset; blocks until the render "
                        "(and optional upload) finishes")
    p.add_argument("preset", help="Quick Export preset name; see 'dava quick list'")
    p.add_argument("-t", "--timeline", help="make this timeline current first (default: current)")
    p.add_argument("-o", "--dir", help="output directory")
    p.add_argument("-n", "--name", help="output file name (without extension)")
    p.add_argument("--quality", type=int, metavar="KBPS", help="bit rate limit (0 = automatic)")
    p.add_argument("--upload", action="store_true", help="upload for web presets that support it")
    p.set_defaults(func=cmd_quick_export, read_only=False, always_json=True)

    p = registry.action("quick", "enable", "offer a render preset in Quick Export")
    p.add_argument("preset", help="render preset name; see 'dava preset list'")
    p.set_defaults(func=cmd_quick_enable, enabled=True, read_only=False)

    p = registry.action("quick", "disable", "stop offering a render preset in Quick Export")
    p.add_argument("preset", help="render preset name; see 'dava preset list'")
    p.set_defaults(func=cmd_quick_enable, enabled=False, read_only=False)


def register(registry):
    _register_render(registry)
    _register_presets(registry)
    _register_quick(registry)
