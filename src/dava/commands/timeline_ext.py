"""dava timeline / track / playhead / marker: timelines beyond list/use/create/export, their tracks, the
playhead, in/out marks, and markers on timelines, timeline items and media pool clips.

Commands that work on several timeline items (link, align, compound, ...) take the items as REFs:
item:video:1:2 (2nd item on video track 1), item#ID, or item (the clip under the playhead); these short
refs are looked up on the timeline -t names. --selected takes the items selected in Resolve instead.
Marker commands pick whose markers with --on REF: timeline (default), timeline:NAME, an item REF, or
clip:NAME / clip#ID for a media pool clip.
"""

import argparse
import base64
import binascii
import json
import os

from ..bridge import coerce, describe_object, resolve_ref
from ..connect import ResolveError
from ..helpers import current_project, find_timeline, parse_assignments, timelines, typed_assignments

TRACK_TYPES = ("video", "audio", "subtitle")
MARKER_COLORS = ("Blue", "Cyan", "Green", "Yellow", "Red", "Pink", "Purple", "Fuchsia",
                 "Rose", "Lavender", "Sky", "Mint", "Lemon", "Sand", "Cocoa", "Cream")
MARK_TYPES = ("video", "audio", "all")
BLANKING_SIDES = ("Top", "Bottom", "Left", "Right")

# CLI names -> resolve.* constant names (checked against the API definition when used).
CAPTION_PRESETS = {"default": "AUTO_CAPTION_SUBTITLE_DEFAULT", "teletext": "AUTO_CAPTION_TELETEXT",
                   "netflix": "AUTO_CAPTION_NETFLIX"}
CAPTION_LINE_BREAKS = {"single": "AUTO_CAPTION_LINE_SINGLE", "double": "AUTO_CAPTION_LINE_DOUBLE"}
ALIGN_USING = {"timecode": "AUTO_ALIGN_CLIPS_USING_TIMECODE", "waveform": "AUTO_ALIGN_CLIPS_USING_WAVEFORM"}
ALIGN_TRACKS = {"mix": "AUTO_ALIGN_CLIPS_WAVEFORM_TRACK_MIX", "auto": "AUTO_ALIGN_CLIPS_WAVEFORM_TRACK_AUTOMATIC"}
DOLBY_ANALYSES = {"blend-shots": "DLB_BLEND_SHOTS"}

# Classes that carry markers -> the word used in messages.
MARKER_HOSTS = {"Timeline": "timeline", "TimelineItem": "item", "MediaPoolItem": "clip"}

ITEM_HINT = "item:video:1:2 (2nd item on V1), item:audio:1:1, item#ID, or item for the clip under the playhead"
ON_HINT = ("timeline (current, default), timeline:NAME, an item (item:video:1:2, item#ID, item = under the "
           "playhead) or a media pool clip (clip:NAME, clip#ID)")
AI_HINT = ("Some analysis features need DaVinci Resolve Studio or an Extras download; run it once from the UI "
           "to see Resolve's reason.")


def positive_int(text):
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {text}")
    return value


def non_negative_int(text):
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be 0 or more, got {text}")
    return value


def marker_color(text):
    for color in MARKER_COLORS:
        if color.lower() == text.strip().lower():
            return color
    raise argparse.ArgumentTypeError(f"{text!r} is not a marker color ({', '.join(MARKER_COLORS)})")


def align_track(text):
    if text.lower() in ALIGN_TRACKS:
        return text.lower()
    try:
        return positive_int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a track number, mix or auto, got {text!r}") from None


# ---------------------------------------------------------------------------
# Shared lookups


def _timeline(resolve, args):
    return find_timeline(current_project(resolve), args.timeline)


def _check_free_name(project, name):
    if any(timeline.GetName() == name for timeline in timelines(project)):
        raise ResolveError(f"The project already has a timeline named {name!r}; choose another name.")


def _path(text):
    """Absolute path; '~' is expanded here too, as batch files and MCP calls do not go through a shell."""
    return os.path.abspath(os.path.expanduser(text))


def _require_file(path):
    path = _path(path)
    if not os.path.isfile(path):
        raise ResolveError(f"File not found: {path}")
    return path


def _api_value(session, cls, method, index, value):
    """Check value against parameter `index` of cls.method in the API definition (constant names -> values)."""
    declared = session.spec.method(cls, method)
    if declared is None:
        raise ResolveError(f"The installed API definition has no {cls}.{method}; update DaVinci Resolve.")
    param = declared.params[index]
    return coerce(session, value, param.type, f"{cls}.{method} {param.name}")


def _field_value(session, typeddict, key, value):
    """Check value against field `key` of an API TypedDict (constant names -> values)."""
    field_type = session.spec.typeddicts[typeddict].fields[key][0]
    return coerce(session, value, field_type, f"{typeddict}.{key}")


def _frame(value):
    """Frames can arrive as floats through the bridges; whole numbers are shown as ints."""
    return int(value) if isinstance(value, float) and value.is_integer() else value


def _item_ref(item):
    return f"item#{item.GetUniqueId()}"


def _item_row(item):
    return {"ref": _item_ref(item), "name": item.GetName()}


def _track_of(item):
    """(track type, track number) of a timeline item."""
    pair = item.GetTrackTypeAndIndex() or []
    values = [pair[key] for key in sorted(pair)] if isinstance(pair, dict) else list(pair)
    if len(values) != 2:
        raise ResolveError(f"Resolve did not report the track of {item.GetName()!r} ({_item_ref(item)}).")
    return values[0], int(values[1])


def _video_items(timeline):
    return [item for track in range(1, (timeline.GetTrackCount("video") or 0) + 1)
            for item in timeline.GetItemListInTrack("video", track) or []]


# ---------------------------------------------------------------------------
# Choosing timeline items (REFs, --selected, --all)


def _items_from_refs(session, timeline, refs):
    items = []
    for ref in refs:
        text = ref
        # Short item refs name items of the chosen timeline, not only of the current one.
        if ref == "item" or ref.startswith(("item:", "item#")):
            text = f"timeline#{timeline.GetUniqueId()}::{ref}"
        obj, cls = resolve_ref(session, text)
        if cls != "TimelineItem":
            raise ResolveError(f"REF {ref!r} names a {cls}, not a timeline item ({ITEM_HINT}).")
        items.append(obj)
    return items


def _chosen_items(resolve, args, minimum=1):
    """Return (timeline, items) from ITEM REFs, --selected or --all (every video item)."""
    use_all = getattr(args, "all", False)
    if sum([bool(args.items), args.selected, use_all]) != 1:
        other = ", --selected or --all" if hasattr(args, "all") else " or --selected"
        raise ResolveError(f"Choose the items one way: as REFs ({ITEM_HINT}){other}.")
    timeline = _timeline(resolve, args)
    if args.selected:
        items = list(timeline.GetSelectedClips() or [])
        if not items:
            raise ResolveError(f"Nothing is selected on {timeline.GetName()!r}; select clips in Resolve's timeline "
                               f"or name them as REFs ({ITEM_HINT}).")
    elif use_all:
        items = _video_items(timeline)
        if not items:
            raise ResolveError(f"Timeline {timeline.GetName()!r} has no video items.")
    else:
        items = _items_from_refs(args.session, timeline, args.items)
    unique = list({item.GetUniqueId(): item for item in items}.values())
    if len(unique) < minimum:
        raise ResolveError(f"This needs at least {minimum} different items; got {len(unique)}.")
    return timeline, unique


def _add_items_args(p, allow_all=False):
    p.add_argument("items", nargs="*", metavar="ITEM", help=f"timeline items as REFs: {ITEM_HINT}")
    p.add_argument("--selected", action="store_true", help="use the items selected in Resolve's timeline")
    if allow_all:
        p.add_argument("--all", action="store_true", help="every item on the timeline's video tracks")
    p.add_argument("-t", "--timeline", help="timeline name (default: current); short item REFs are looked up on it")


def _timeline_arg(p):
    p.add_argument("-t", "--timeline", help="timeline name (default: current)")


# ---------------------------------------------------------------------------
# timeline: identity, copies, import


def cmd_info(resolve, args):
    project = current_project(resolve)
    timeline = find_timeline(project, args.timeline)
    current = project.GetCurrentTimeline()
    clip = timeline.GetMediaPoolItem()
    start, end = timeline.GetStartFrame(), timeline.GetEndFrame()
    info = describe_object(args.session, timeline, "Timeline")
    info.update({
        "id": timeline.GetUniqueId(),
        "current": bool(current) and current.GetUniqueId() == timeline.GetUniqueId(),
        "start_timecode": timeline.GetStartTimecode(),
        "start_frame": start,
        "end_frame": end,
        "duration_frames": end - start,
        "tracks": {kind: timeline.GetTrackCount(kind) or 0 for kind in TRACK_TYPES},
        "media_pool_item": describe_object(args.session, clip, "MediaPoolItem") if clip else None,
    })
    return info


def cmd_rename(resolve, args):
    project = current_project(resolve)
    timeline = find_timeline(project, args.timeline)
    old = timeline.GetName()
    _check_free_name(project, args.name)
    if not timeline.SetName(args.name):
        raise ResolveError(f"Resolve could not rename timeline {old!r} to {args.name!r}.")
    return {"timeline": args.name, "old_name": old}


def cmd_duplicate(resolve, args):
    project = current_project(resolve)
    source = find_timeline(project, args.timeline)
    _check_free_name(project, args.name)
    copy = source.DuplicateTimeline(args.name)
    if not copy:
        raise ResolveError(f"Resolve could not duplicate timeline {source.GetName()!r} as {args.name!r}.")
    info = describe_object(args.session, copy, "Timeline")
    info["source"] = source.GetName()
    return info


def cmd_delete(resolve, args):
    project = current_project(resolve)
    names = list(dict.fromkeys(args.names))
    chosen = [find_timeline(project, name) for name in names]
    if not project.GetMediaPool().DeleteTimelines(chosen):
        raise ResolveError(f"Resolve could not delete timeline(s) {', '.join(map(repr, names))}.")
    return {"deleted": names}


def cmd_import(resolve, args):
    path = _require_file(args.file)
    pairs = []
    if args.name is not None:
        pairs.append(f"timelineName={args.name}")
    if args.no_source_clips:
        pairs.append("importSourceClips=false")
    if args.source_path:
        pairs.append(f"sourceClipsPath={_path(args.source_path)}")
    if args.source_folder:
        pairs.append("sourceClipsFolders=" + json.dumps(args.source_folder))
    if args.interlace:
        pairs.append("interlaceProcessing=true")
    options = typed_assignments(args.session, pairs + args.option, "ImportOptions")
    project = current_project(resolve)
    if "timelineName" in options:
        _check_free_name(project, options["timelineName"])
    media_pool = project.GetMediaPool()
    timeline = media_pool.ImportTimelineFromFile(path, options) if options else media_pool.ImportTimelineFromFile(path)
    if not timeline:
        raise ResolveError(f"Resolve could not import a timeline from {path}. Supported files: AAF, EDL, XML, FCPXML, "
                           "DRT, ADL, OTIO. If the media is missing, point --source-path at it (or use "
                           "--no-source-clips with --source-folder for clips already in the media pool).")
    info = describe_object(args.session, timeline, "Timeline")
    info["file"] = path
    return info


def cmd_import_aaf(resolve, args):
    path = _require_file(args.file)
    options = typed_assignments(args.session, args.option, "AAFImportOptions")
    timeline = _timeline(resolve, args)
    ok = timeline.ImportIntoTimeline(path, options) if options else timeline.ImportIntoTimeline(path)
    if not ok:
        raise ResolveError(f"Resolve could not import the AAF {path} into {timeline.GetName()!r}. Check the options "
                           "with 'dava api show AAFImportOptions'.")
    return {"timeline": timeline.GetName(), "file": path, "options": parse_assignments(args.option)}


# ---------------------------------------------------------------------------
# timeline: settings, start timecode, in/out, blanking, thumbnail


def cmd_settings(resolve, args):
    timeline = _timeline(resolve, args)
    settings = timeline.GetSettings()
    if not isinstance(settings, dict):
        raise ResolveError(f"Resolve returned no settings for {timeline.GetName()!r}.")
    if not args.keys:
        return dict(settings)
    unknown = [key for key in args.keys if key not in settings]
    if unknown:
        raise ResolveError(f"Unknown timeline setting(s): {', '.join(unknown)}. Keys: 'dava api show TimelineSettings'.")
    return {key: settings[key] for key in args.keys}


def cmd_set(resolve, args):
    settings = typed_assignments(args.session, args.pairs, "TimelineSettings")
    timeline = _timeline(resolve, args)
    # One key per call so a rejection names its key; useCustomSettings first, as the other keys depend on it.
    order = sorted(settings, key=lambda key: key != "useCustomSettings")
    rejected = [key for key in order if not timeline.SetSettings({key: settings[key]})]
    if rejected:
        applied = [key for key in order if key not in rejected]
        hint = ""
        if settings.get("useCustomSettings") != "1" and any(key != "useCustomSettings" for key in rejected):
            hint = (" Timeline settings other than useCustomSettings only change while useCustomSettings is '1'; "
                    "add useCustomSettings=1.")
        raise ResolveError(
            f"Resolve rejected timeline setting(s) {', '.join(f'{k}={settings[k]!r}' for k in rejected)} on "
            f"{timeline.GetName()!r}" + (f" (applied: {', '.join(applied)})" if applied else "") + "." + hint
            + " Valid values: 'dava api show TimelineSettings'.")
    return {"timeline": timeline.GetName(), "settings": settings}


def cmd_start_timecode(resolve, args):
    timeline = _timeline(resolve, args)
    if args.timecode is not None and not timeline.SetStartTimecode(args.timecode):
        raise ResolveError(f"Resolve rejected start timecode {args.timecode!r} for {timeline.GetName()!r}; "
                           "use HH:MM:SS:FF, e.g. 01:00:00:00.")
    return {"timeline": timeline.GetName(), "start_timecode": timeline.GetStartTimecode(),
            "start_frame": timeline.GetStartFrame()}


def _in_out(timeline):
    marks = timeline.GetMarkInOut()
    if not isinstance(marks, dict):
        raise ResolveError(f"Resolve returned no in/out marks for {timeline.GetName()!r}.")
    return {"timeline": timeline.GetName(), "video": marks.get("video"), "audio": marks.get("audio")}


def cmd_in_out(resolve, args):
    return _in_out(_timeline(resolve, args))


def cmd_set_in_out(resolve, args):
    if args.mark_out < args.mark_in:
        raise ResolveError(f"OUT ({args.mark_out}) is before IN ({args.mark_in}).")
    timeline = _timeline(resolve, args)
    extra = (args.type,) if args.type else ()
    if not timeline.SetMarkInOut(args.mark_in, args.mark_out, *extra):
        raise ResolveError(f"Resolve could not set in {args.mark_in} / out {args.mark_out} ({args.type or 'all'}) on "
                           f"{timeline.GetName()!r}; frames count from the timeline start (0 = first frame).")
    return _in_out(timeline)


def cmd_clear_in_out(resolve, args):
    timeline = _timeline(resolve, args)
    extra = (args.type,) if args.type else ()
    if not timeline.ClearMarkInOut(*extra):
        raise ResolveError(f"Resolve could not clear the {args.type or 'video and audio'} in/out marks of "
                           f"{timeline.GetName()!r}.")
    return _in_out(timeline)


def cmd_blanking(resolve, args):
    given = {side: getattr(args, side.lower()) for side in BLANKING_SIDES if getattr(args, side.lower()) is not None}
    timeline = _timeline(resolve, args)
    if given:
        current = timeline.GetOutputBlanking()
        if not isinstance(current, dict):
            raise ResolveError(f"Resolve returned no output blanking for {timeline.GetName()!r}.")
        values = {**current, **given}
        if not timeline.SetOutputBlanking(values):
            raise ResolveError(f"Resolve rejected output blanking {values} for {timeline.GetName()!r}.")
    blanking = timeline.GetOutputBlanking()
    if not isinstance(blanking, dict):
        raise ResolveError(f"Resolve returned no output blanking for {timeline.GetName()!r}.")
    return {"timeline": timeline.GetName(), **blanking}


def cmd_thumbnail(resolve, args):
    timeline = _timeline(resolve, args)
    thumb = timeline.GetCurrentClipThumbnailImage()
    if not isinstance(thumb, dict) or not thumb.get("data"):
        raise ResolveError("Resolve returned no thumbnail. It shows the clip under the playhead on the Color page: "
                           "run 'dava page color' first (move the playhead with 'dava playhead set TIMECODE').")
    try:
        raw = base64.b64decode(thumb["data"], validate=True)
    except binascii.Error as exc:
        raise ResolveError(f"Resolve returned thumbnail data that is not valid base64: {exc}") from exc
    width, height = int(thumb.get("width") or 0), int(thumb.get("height") or 0)
    result = {"timeline": timeline.GetName(), "width": width, "height": height,
              "format": thumb.get("format"), "bytes": len(raw)}
    if args.output:
        path = _path(args.output)
        data = raw
        if path.lower().endswith(".ppm"):
            if len(raw) != width * height * 3:
                raise ResolveError(f"The thumbnail ({thumb.get('format')}, {len(raw)} bytes for {width}x{height}) is "
                                   "not 8-bit RGB, so it cannot be saved as .ppm; use another extension for the raw bytes.")
            data = f"P6\n{width} {height}\n255\n".encode("ascii") + raw
        try:
            with open(path, "wb") as handle:
                handle.write(data)
        except OSError as exc:
            raise ResolveError(f"Cannot write the thumbnail to {path}: {exc.strerror or exc}. Check that the folder "
                               "exists and is writable.") from exc
        result["path"] = path
    if args.base64:
        result["data"] = thumb["data"]
    return result


# ---------------------------------------------------------------------------
# timeline: analysis and conversions


def cmd_scene_cuts(resolve, args):
    timeline = _timeline(resolve, args)
    before = len(_video_items(timeline))
    if not timeline.DetectSceneCuts():
        raise ResolveError(f"Resolve could not detect scene cuts on {timeline.GetName()!r}. {AI_HINT}")
    return {"timeline": timeline.GetName(), "video_items_before": before, "video_items_after": len(_video_items(timeline))}


def _caption_language(session, text):
    name = text.strip().upper().replace("-", "_").replace(" ", "_")
    if not name.startswith("AUTO_CAPTION_"):
        name = "AUTO_CAPTION_" + name
    field_type = session.spec.typeddicts["AutoCaptionSettings"].fields["language"][0]
    if field_type[0] == "const" and name not in field_type[2]:
        known = ", ".join(v.removeprefix("AUTO_CAPTION_").lower().replace("_", "-") for v in field_type[2])
        raise ResolveError(f"Unknown subtitle language {text!r}. Languages: {known}.")
    return name


def cmd_subtitles(resolve, args):
    session = args.session
    names = {}
    if args.language is not None:
        names["language"] = _caption_language(session, args.language)
    if args.preset is not None:
        names["captionPreset"] = CAPTION_PRESETS[args.preset]
    if args.line_break is not None:
        names["lineBreak"] = CAPTION_LINE_BREAKS[args.line_break]
    settings = {key: _field_value(session, "AutoCaptionSettings", key, name) for key, name in names.items()}
    if args.chars_per_line is not None:
        if not 1 <= args.chars_per_line <= 60:
            raise ResolveError(f"--chars-per-line must be 1 to 60, got {args.chars_per_line}.")
        settings["charsPerLine"] = names["charsPerLine"] = args.chars_per_line
    if args.gap is not None:
        if not 0 <= args.gap <= 10:
            raise ResolveError(f"--gap must be 0 to 10 frames, got {args.gap}.")
        settings["gap"] = names["gap"] = args.gap
    timeline = _timeline(resolve, args)
    ok = timeline.CreateSubtitlesFromAudio(settings) if settings else timeline.CreateSubtitlesFromAudio()
    if not ok:
        raise ResolveError(f"Resolve could not create subtitles from the audio of {timeline.GetName()!r}. The timeline "
                           f"needs audio clips; make it current with 'dava timeline use'. {AI_HINT}")
    return {"timeline": timeline.GetName(), "subtitle_tracks": timeline.GetTrackCount("subtitle") or 0,
            "settings": names}


def cmd_convert_stereo(resolve, args):
    timeline = _timeline(resolve, args)
    if not timeline.ConvertTimelineToStereo():
        raise ResolveError(f"Resolve could not convert {timeline.GetName()!r} to stereo.")
    return {"timeline": timeline.GetName(), "converted": True}


def cmd_dolby_vision(resolve, args):
    analysis = _api_value(args.session, "Timeline", "AnalyzeDolbyVision", 1, DOLBY_ANALYSES[args.analysis])
    timeline, items = _chosen_items(resolve, args)
    if not timeline.AnalyzeDolbyVision(items, analysis):
        raise ResolveError(f"Resolve could not run Dolby Vision analysis ({args.analysis}) on {len(items)} item(s) of "
                           f"{timeline.GetName()!r}; are they all on that timeline (-t)? {AI_HINT}")
    return {"timeline": timeline.GetName(), "analysis": args.analysis, "items": [_item_row(i) for i in items]}


# ---------------------------------------------------------------------------
# timeline: operations on several items


def _link(resolve, args, linked):
    timeline, items = _chosen_items(resolve, args, minimum=2 if linked else 1)
    if not timeline.SetClipsLinked(items, linked):
        word = "link" if linked else "unlink"
        raise ResolveError(f"Resolve could not {word} {', '.join(_item_ref(i) for i in items)} on "
                           f"{timeline.GetName()!r}; are they all on that timeline (-t)?")
    return {"timeline": timeline.GetName(), "linked": linked, "items": [_item_row(i) for i in items]}


def cmd_link(resolve, args):
    return _link(resolve, args, True)


def cmd_unlink(resolve, args):
    return _link(resolve, args, False)


def cmd_align(resolve, args):
    if args.track is not None and args.using != "waveform":
        raise ResolveError("--track only applies with --using waveform.")
    session = args.session
    options = {}
    if args.using is not None:
        options["SyncUsing"] = _field_value(session, "AutoAlignOptions", "SyncUsing", ALIGN_USING[args.using])
    if args.track is not None:
        value = ALIGN_TRACKS.get(args.track, args.track)
        options["UseTrack"] = _field_value(session, "AutoAlignOptions", "UseTrack", value)
    timeline, items = _chosen_items(resolve, args, minimum=2)
    ok = timeline.AutoAlignClips(items, options) if options else timeline.AutoAlignClips(items)
    if not ok:
        raise ResolveError(f"Resolve could not align {len(items)} item(s) on {timeline.GetName()!r} by "
                           f"{args.using or 'timecode'}; the clips need overlapping timecode (or, with --using "
                           "waveform, matching audio).")
    return {"timeline": timeline.GetName(), "using": args.using or "timecode",
            "items": [dict(_item_row(i), start=i.GetStart()) for i in items]}


def cmd_compound(resolve, args):
    options = {}
    if args.name is not None:
        options["name"] = args.name
    if args.start_timecode is not None:
        options["startTimecode"] = args.start_timecode
    timeline, items = _chosen_items(resolve, args)
    rows = [_item_row(i) for i in items]
    clip = timeline.CreateCompoundClip(items, options) if options else timeline.CreateCompoundClip(items)
    if not clip:
        raise ResolveError(f"Resolve could not make a compound clip of {len(items)} item(s) on "
                           f"{timeline.GetName()!r}; are they all on that timeline (-t)?")
    info = describe_object(args.session, clip, "TimelineItem")
    info["items"] = rows
    return info


def cmd_fusion_clip(resolve, args):
    timeline, items = _chosen_items(resolve, args)
    rows = [_item_row(i) for i in items]
    clip = timeline.CreateFusionClip(items)
    if not clip:
        raise ResolveError(f"Resolve could not make a Fusion clip of {len(items)} item(s) on "
                           f"{timeline.GetName()!r}; are they all video items on that timeline (-t)?")
    info = describe_object(args.session, clip, "TimelineItem")
    info["items"] = rows
    return info


def cmd_delete_clips(resolve, args):
    timeline, items = _chosen_items(resolve, args)
    rows = [_item_row(i) for i in items]
    if not timeline.DeleteClips(items, args.ripple):
        raise ResolveError(f"Resolve could not delete {', '.join(r['ref'] for r in rows)} from "
                           f"{timeline.GetName()!r}; is one of them on a locked track?")
    return {"timeline": timeline.GetName(), "ripple": args.ripple, "deleted": rows}


def cmd_selected(resolve, args):
    timeline = _timeline(resolve, args)
    rows = []
    for item in timeline.GetSelectedClips() or []:
        track_type, track = _track_of(item)
        rows.append({**_item_row(item), "type": item.GetType(), "track_type": track_type, "track": track,
                     "start": item.GetStart(), "end": item.GetEnd()})
    return rows


# ---------------------------------------------------------------------------
# track


def _check_track(timeline, kind, index):
    count = timeline.GetTrackCount(kind) or 0
    if not 1 <= index <= count:
        raise ResolveError(f"{timeline.GetName()!r} has {count} {kind} track(s); there is no {kind} track {index}.")


def _track_row(timeline, kind, index):
    row = {"type": kind, "track": index, "name": timeline.GetTrackName(kind, index)}
    if kind == "audio":
        row["format"] = timeline.GetTrackSubType(kind, index)
    row.update(enabled=timeline.GetIsTrackEnabled(kind, index), locked=timeline.GetIsTrackLocked(kind, index),
               items=len(timeline.GetItemListInTrack(kind, index) or []))
    return row


def cmd_track_list(resolve, args):
    timeline = _timeline(resolve, args)
    kinds = [args.type] if args.type else TRACK_TYPES
    return [_track_row(timeline, kind, index)
            for kind in kinds for index in range(1, (timeline.GetTrackCount(kind) or 0) + 1)]


def cmd_track_add(resolve, args):
    if args.format and args.type != "audio":
        raise ResolveError("--format is the audio track format; it only applies to audio tracks.")
    timeline = _timeline(resolve, args)
    before = timeline.GetTrackCount(args.type) or 0
    extra = (args.format,) if args.format else ()
    if not timeline.AddTrack(args.type, *extra):
        what = f"{args.format} {args.type}" if args.format else args.type
        raise ResolveError(f"Resolve could not add a {what} track to {timeline.GetName()!r}"
                           + ("; formats: mono, stereo, 5.1, 7.1, adaptive_1 ... (see the Audio Mapping section of "
                              "the scripting README)." if args.format else "."))
    index = timeline.GetTrackCount(args.type) or 0
    if index != before + 1:
        raise ResolveError(f"Resolve reported success but {timeline.GetName()!r} has {index} {args.type} track(s), "
                           f"expected {before + 1}.")
    if args.name is not None and not timeline.SetTrackName(args.type, index, args.name):
        raise ResolveError(f"Added {args.type} track {index} but Resolve could not name it {args.name!r}.")
    return _track_row(timeline, args.type, index)


def cmd_track_delete(resolve, args):
    timeline = _timeline(resolve, args)
    _check_track(timeline, args.type, args.index)
    name = timeline.GetTrackName(args.type, args.index)
    if not timeline.DeleteTrack(args.type, args.index):
        raise ResolveError(f"Resolve could not delete {args.type} track {args.index} ({name!r}) of "
                           f"{timeline.GetName()!r}; is it locked or the only track?")
    return {"timeline": timeline.GetName(), "type": args.type, "deleted": args.index, "name": name,
            "tracks_left": timeline.GetTrackCount(args.type) or 0}


def cmd_track_name(resolve, args):
    timeline = _timeline(resolve, args)
    _check_track(timeline, args.type, args.index)
    if args.name is not None and not timeline.SetTrackName(args.type, args.index, args.name):
        raise ResolveError(f"Resolve could not name {args.type} track {args.index} {args.name!r}.")
    return {"type": args.type, "track": args.index, "name": timeline.GetTrackName(args.type, args.index)}


def _track_switch(resolve, args, change, word):
    """Apply change(timeline, index) to every track in args.indexes; stop at the first one Resolve rejects."""
    timeline = _timeline(resolve, args)
    for index in args.indexes:
        _check_track(timeline, args.type, index)
    done = []
    for index in dict.fromkeys(args.indexes):
        if not change(timeline, index):
            before = f" Tracks changed before it: {', '.join(map(str, done))}." if done else ""
            raise ResolveError(f"Resolve could not {word} {args.type} track {index} of {timeline.GetName()!r}.{before}")
        done.append(index)
    return [_track_row(timeline, args.type, index) for index in done]


def cmd_track_enable(resolve, args):
    return _track_switch(resolve, args, lambda tl, index: tl.SetTrackEnable(args.type, index, True), "enable")


def cmd_track_disable(resolve, args):
    return _track_switch(resolve, args, lambda tl, index: tl.SetTrackEnable(args.type, index, False), "disable")


def cmd_track_lock(resolve, args):
    return _track_switch(resolve, args, lambda tl, index: tl.SetTrackLock(args.type, index, True), "lock")


def cmd_track_unlock(resolve, args):
    return _track_switch(resolve, args, lambda tl, index: tl.SetTrackLock(args.type, index, False), "unlock")


# ---------------------------------------------------------------------------
# playhead


def cmd_playhead_get(resolve, args):
    timeline = _timeline(resolve, args)
    timecode = timeline.GetCurrentTimecode()
    if not timecode:
        raise ResolveError(f"Resolve returned no playhead position for {timeline.GetName()!r}.")
    item = timeline.GetCurrentVideoItem()
    return {"timeline": timeline.GetName(), "timecode": timecode, "start_timecode": timeline.GetStartTimecode(),
            "video_item": _item_ref(item) if item else None, "video_item_name": item.GetName() if item else None}


def cmd_playhead_set(resolve, args):
    project = current_project(resolve)
    timeline = find_timeline(project, args.timeline)
    if args.timeline is not None and not project.SetCurrentTimeline(timeline):
        raise ResolveError(f"Could not make {timeline.GetName()!r} the current timeline.")
    if not timeline.SetCurrentTimecode(args.timecode):
        raise ResolveError(f"Resolve rejected timecode {args.timecode!r}; give HH:MM:SS:FF within the timeline, which "
                           f"starts at {timeline.GetStartTimecode()}.")
    return {"timeline": timeline.GetName(), "timecode": timeline.GetCurrentTimecode()}


def cmd_playhead_item(resolve, args):
    timeline = _timeline(resolve, args)
    item = timeline.GetCurrentVideoItem()
    if not item:
        raise ResolveError(f"No video clip under the playhead ({timeline.GetCurrentTimecode()}) on "
                           f"{timeline.GetName()!r}; move it with 'dava playhead set TIMECODE'.")
    info = describe_object(args.session, item, "TimelineItem")
    info.update({"timecode": timeline.GetCurrentTimecode(), "type": item.GetType(), "start": item.GetStart(),
                 "end": item.GetEnd(), "duration": item.GetDuration()})
    return info


# ---------------------------------------------------------------------------
# marker (timelines, timeline items, media pool clips)


def _marker_host(session, ref):
    """(object, its $ref, description) for --on REF."""
    obj, cls = resolve_ref(session, ref)
    if cls not in MARKER_HOSTS:
        raise ResolveError(f"--on {ref!r} names a {cls}; markers belong to a {ON_HINT}.")
    info = describe_object(session, obj, cls)
    return obj, info["$ref"], f"{MARKER_HOSTS[cls]} {obj.GetName()!r}"


def _markers(obj, what):
    markers = obj.GetMarkers()
    if not isinstance(markers, dict):
        raise ResolveError(f"Resolve returned no marker list for {what}.")
    return {_frame(frame): dict(info) for frame, info in markers.items()}


def _frames_text(markers):
    return ", ".join(str(frame) for frame in sorted(markers)) or "none"


def cmd_marker_show(resolve, args):
    obj, _, what = _marker_host(args.session, args.on)
    markers = _markers(obj, what)
    return [{"frame": frame, **info} for frame, info in sorted(markers.items())
            if args.color is None or info.get("color") == args.color]


def cmd_marker_create(resolve, args):
    obj, ref, what = _marker_host(args.session, args.on)
    extra = (args.data,) if args.data is not None else ()
    if not obj.AddMarker(args.frame, args.color, args.name, args.note, args.duration, *extra):
        raise ResolveError(f"Resolve could not add a marker at frame {args.frame} to {what}; there may already be one "
                           "there (see 'dava marker show'), or the frame is outside it.")
    return {"on": ref, "frame": args.frame, "color": args.color, "name": args.name, "note": args.note,
            "duration": args.duration, "customData": args.data or ""}


def cmd_marker_delete(resolve, args):
    obj, ref, what = _marker_host(args.session, args.on)
    before = _markers(obj, what)
    if args.frame:
        missing = [frame for frame in args.frame if frame not in before]
        if missing:
            raise ResolveError(f"No marker at frame(s) {', '.join(map(str, missing))} on {what}; markers are at: "
                               f"{_frames_text(before)}.")
        for frame in dict.fromkeys(args.frame):
            if not obj.DeleteMarkerAtFrame(frame):
                raise ResolveError(f"Resolve could not delete the marker at frame {frame} of {what}.")
    elif args.custom_data is not None:
        if not any(info.get("customData") == args.custom_data for info in before.values()):
            raise ResolveError(f"No marker on {what} has custom data {args.custom_data!r}.")
        if not obj.DeleteMarkerByCustomData(args.custom_data):
            raise ResolveError(f"Resolve could not delete the marker with custom data {args.custom_data!r} of {what}.")
    else:
        color = "All" if args.all else args.color
        if not any(color == "All" or info.get("color") == color for info in before.values()):
            return {"on": ref, "deleted": []}
        if not obj.DeleteMarkersByColor(color):
            which = "markers" if color == "All" else f"{color} markers"
            raise ResolveError(f"Resolve could not delete the {which} of {what}.")
    after = _markers(obj, what)
    return {"on": ref, "deleted": [frame for frame in sorted(before) if frame not in after]}


def cmd_marker_find(resolve, args):
    obj, ref, what = _marker_host(args.session, args.on)
    info = obj.GetMarkerByCustomData(args.data)
    if not info:
        raise ResolveError(f"No marker on {what} has custom data {args.data!r}.")
    frames = [frame for frame, marker in sorted(_markers(obj, what).items()) if marker.get("customData") == args.data]
    return {"on": ref, "frames": frames, **info}


def cmd_marker_data(resolve, args):
    obj, ref, what = _marker_host(args.session, args.on)
    markers = _markers(obj, what)
    if args.frame not in markers:
        raise ResolveError(f"No marker at frame {args.frame} on {what}; markers are at: {_frames_text(markers)}.")
    if args.text is not None and not obj.UpdateMarkerCustomData(args.frame, args.text):
        raise ResolveError(f"Resolve could not set the custom data of the marker at frame {args.frame} of {what}.")
    return {"on": ref, "frame": args.frame, "customData": obj.GetMarkerCustomData(args.frame)}


# ---------------------------------------------------------------------------
# Registration


def _register_timeline(registry):
    p = registry.action("timeline", "info", "name, id, start timecode, frames, track counts and media pool item "
                        "of a timeline")
    _timeline_arg(p)
    p.set_defaults(func=cmd_info, read_only=True, always_json=True)

    p = registry.action("timeline", "rename", "rename a timeline (the name must be unique in the project)")
    p.add_argument("name", help="new timeline name")
    _timeline_arg(p)
    p.set_defaults(func=cmd_rename, read_only=False)

    p = registry.action("timeline", "duplicate", "copy a timeline under a new name")
    p.add_argument("name", help="name of the copy")
    p.add_argument("-t", "--timeline", help="timeline to copy (default: current)")
    p.set_defaults(func=cmd_duplicate, read_only=False)

    p = registry.action("timeline", "delete", "delete timelines from the project (MediaPool.DeleteTimelines)")
    p.add_argument("names", nargs="+", metavar="NAME", help="timeline names")
    p.set_defaults(func=cmd_delete, read_only=False)

    p = registry.action("timeline", "import", "create a timeline from an AAF, EDL, XML, FCPXML, DRT, ADL or OTIO file")
    p.add_argument("file", help="timeline file to import")
    p.add_argument("-n", "--name", help="name of the new timeline (not for DRT)")
    p.add_argument("--no-source-clips", action="store_true",
                   help="do not import the source clips into the media pool (not for DRT)")
    p.add_argument("--source-path", help="folder to search for source media that is not where the file says")
    p.add_argument("--source-folder", action="append", metavar="FOLDER",
                   help="media pool folder REF to search for source clips with --no-source-clips, e.g. "
                        "folder:/Master/Footage (repeatable)")
    p.add_argument("--interlace", action="store_true", help="enable interlace processing (AAF only)")
    p.add_argument("-o", "--option", action="append", default=[], metavar="KEY=VALUE",
                   help="any other ImportOptions key ('dava api show ImportOptions')")
    p.set_defaults(func=cmd_import, read_only=False)

    p = registry.action("timeline", "import-aaf", "import the items of an AAF file into an existing timeline")
    p.add_argument("file", help="AAF file")
    _timeline_arg(p)
    p.add_argument("-o", "--option", action="append", default=[], metavar="KEY=VALUE",
                   help="AAFImportOptions, e.g. insertAdditionalTracks=false insertWithOffset=01:00:10:00 "
                        "('dava api show AAFImportOptions')")
    p.set_defaults(func=cmd_import_aaf, read_only=False)

    p = registry.action("timeline", "settings", "show timeline settings (all, or the given keys); a timeline "
                        "without custom settings reports the project settings")
    p.add_argument("keys", nargs="*", metavar="KEY")
    _timeline_arg(p)
    p.set_defaults(func=cmd_settings, read_only=True)

    p = registry.action("timeline", "set", "change timeline settings, e.g. useCustomSettings=1 "
                        "timelineResolutionWidth=1080 timelineResolutionHeight=1920 (keys and values: "
                        "'dava api show TimelineSettings')")
    p.add_argument("pairs", nargs="+", metavar="KEY=VALUE")
    _timeline_arg(p)
    p.set_defaults(func=cmd_set, read_only=False, always_json=True)

    p = registry.action("timeline", "start-timecode", "show the start timecode of a timeline, or set it")
    p.add_argument("timecode", nargs="?", help="new start timecode, e.g. 01:00:00:00")
    _timeline_arg(p)
    p.set_defaults(func=cmd_start_timecode, read_only=lambda a: a.timecode is None)

    p = registry.action("timeline", "selected", "list the items selected in Resolve's timeline, on every track")
    _timeline_arg(p)
    p.set_defaults(func=cmd_selected, read_only=True)

    p = registry.action("timeline", "in-out", "show the video and audio in/out marks (frames from the timeline start)")
    _timeline_arg(p)
    p.set_defaults(func=cmd_in_out, read_only=True, always_json=True)

    p = registry.action("timeline", "set-in-out", "set the in and out marks (frames from the timeline start)")
    p.add_argument("mark_in", metavar="IN", type=non_negative_int, help="in frame")
    p.add_argument("mark_out", metavar="OUT", type=non_negative_int, help="out frame")
    p.add_argument("--type", choices=MARK_TYPES, help="which marks (default: all)")
    _timeline_arg(p)
    p.set_defaults(func=cmd_set_in_out, read_only=False, always_json=True)

    p = registry.action("timeline", "clear-in-out", "clear the in and out marks")
    p.add_argument("--type", choices=MARK_TYPES, help="which marks (default: all)")
    _timeline_arg(p)
    p.set_defaults(func=cmd_clear_in_out, read_only=False, always_json=True)

    p = registry.action("timeline", "blanking", "show the timeline's output blanking (pixels), or set sides; "
                        "sides not given keep their value")
    for side in BLANKING_SIDES:
        p.add_argument(f"--{side.lower()}", type=non_negative_int, metavar="PX", help=f"{side.lower()} blanking in pixels")
    _timeline_arg(p)
    p.set_defaults(func=cmd_blanking,
                   read_only=lambda a: all(getattr(a, side.lower()) is None for side in BLANKING_SIDES))

    p = registry.action("timeline", "thumbnail", "thumbnail of the clip under the playhead on the Color page "
                        "(size and format; --output saves it, as a viewable image when the name ends in .ppm)")
    p.add_argument("-o", "--output", help="file to write: NAME.ppm for an image, any other name for the raw bytes")
    p.add_argument("--base64", action="store_true", help="include the base64 image data in the result")
    _timeline_arg(p)
    p.set_defaults(func=cmd_thumbnail, read_only=lambda a: a.output is None)

    p = registry.action("timeline", "scene-cuts", "detect scene cuts and cut the timeline's clips at them "
                        "(Timeline.DetectSceneCuts)")
    _timeline_arg(p)
    p.set_defaults(func=cmd_scene_cuts, read_only=False)

    p = registry.action("timeline", "subtitles", "transcribe the timeline's audio into a subtitle track "
                        "(Timeline.CreateSubtitlesFromAudio)")
    p.add_argument("-l", "--language", help="spoken language, e.g. english, german, auto (default: auto); all "
                   "names: 'dava api show AutoCaptionLanguage'")
    p.add_argument("-p", "--preset", choices=sorted(CAPTION_PRESETS), help="caption preset (default: default)")
    p.add_argument("--chars-per-line", type=int, metavar="N", help="maximum characters per line, 1 to 60")
    p.add_argument("--line-break", choices=sorted(CAPTION_LINE_BREAKS), help="single or double lines")
    p.add_argument("--gap", type=int, metavar="FRAMES", help="gap between subtitles in frames, 0 to 10")
    _timeline_arg(p)
    p.set_defaults(func=cmd_subtitles, read_only=False)

    p = registry.action("timeline", "convert-stereo", "convert the timeline to stereo "
                        "(Timeline.ConvertTimelineToStereo)")
    _timeline_arg(p)
    p.set_defaults(func=cmd_convert_stereo, read_only=False)

    p = registry.action("timeline", "link", "link timeline items (e.g. a video clip and its audio) so they move "
                        "together")
    _add_items_args(p)
    p.set_defaults(func=cmd_link, read_only=False, always_json=True)

    p = registry.action("timeline", "unlink", "unlink timeline items")
    _add_items_args(p)
    p.set_defaults(func=cmd_unlink, read_only=False, always_json=True)

    p = registry.action("timeline", "align", "auto-align clips on different tracks by timecode or audio waveform")
    _add_items_args(p)
    p.add_argument("--using", choices=sorted(ALIGN_USING), help="sync by timecode (default) or waveform")
    p.add_argument("--track", type=align_track, metavar="N|mix|auto",
                   help="with --using waveform: audio track number to compare, mix, or auto")
    p.set_defaults(func=cmd_align, read_only=False, always_json=True)

    p = registry.action("timeline", "compound", "turn timeline items into one compound clip")
    _add_items_args(p)
    p.add_argument("-n", "--name", help="compound clip name")
    p.add_argument("--start-timecode", metavar="TIMECODE", help="start timecode of the compound clip, e.g. 00:00:00:00")
    p.set_defaults(func=cmd_compound, read_only=False, always_json=True)

    p = registry.action("timeline", "fusion-clip", "turn timeline items into one Fusion clip")
    _add_items_args(p)
    p.set_defaults(func=cmd_fusion_clip, read_only=False, always_json=True)

    p = registry.action("timeline", "delete-clips", "remove items from a timeline (also the current selection); "
                        "--ripple closes the gaps")
    _add_items_args(p)
    p.add_argument("--ripple", action="store_true", help="ripple delete: move later clips back")
    p.set_defaults(func=cmd_delete_clips, read_only=False, always_json=True)

    p = registry.action("timeline", "dolby-vision", "run Dolby Vision analysis on timeline items")
    _add_items_args(p, allow_all=True)
    p.add_argument("--analysis", choices=sorted(DOLBY_ANALYSES), default="blend-shots",
                   help="analysis type (default: blend-shots)")
    p.set_defaults(func=cmd_dolby_vision, read_only=False, always_json=True)


def _register_track(registry):
    help_text = "timeline tracks: list, add, delete, name, enable, lock"
    p = registry.action("track", "list", "tracks of a timeline: name, audio format, enabled, locked, item count",
                        group_help=help_text)
    p.add_argument("-k", "--type", choices=TRACK_TYPES, help="only tracks of this type")
    _timeline_arg(p)
    p.set_defaults(func=cmd_track_list, read_only=True)

    p = registry.action("track", "add", "add a track; it becomes the highest-numbered track of its type")
    p.add_argument("type", choices=TRACK_TYPES)
    p.add_argument("-f", "--format", help="audio track format, e.g. mono, stereo, 5.1, 7.1, adaptive_1")
    p.add_argument("-n", "--name", help="name the new track")
    _timeline_arg(p)
    p.set_defaults(func=cmd_track_add, read_only=False)

    p = registry.action("track", "delete", "delete a track and the items on it")
    p.add_argument("type", choices=TRACK_TYPES)
    p.add_argument("index", type=positive_int, help="track number, from 1")
    _timeline_arg(p)
    p.set_defaults(func=cmd_track_delete, read_only=False)

    p = registry.action("track", "name", "show a track's name, or rename it")
    p.add_argument("type", choices=TRACK_TYPES)
    p.add_argument("index", type=positive_int, help="track number, from 1")
    p.add_argument("name", nargs="?", help="new name")
    _timeline_arg(p)
    p.set_defaults(func=cmd_track_name, read_only=lambda a: a.name is None)

    for action, func, text in (
        ("enable", cmd_track_enable, "enable tracks (their items play and render)"),
        ("disable", cmd_track_disable, "disable tracks (their items stay but do not play or render)"),
        ("lock", cmd_track_lock, "lock tracks against edits"),
        ("unlock", cmd_track_unlock, "unlock tracks"),
    ):
        p = registry.action("track", action, text)
        p.add_argument("type", choices=TRACK_TYPES)
        p.add_argument("indexes", nargs="+", type=positive_int, metavar="INDEX", help="track numbers, from 1")
        _timeline_arg(p)
        p.set_defaults(func=func, read_only=False)


def _register_playhead(registry):
    help_text = "the playhead: position and the clip under it"
    p = registry.action("playhead", "get", "the playhead timecode and the video clip under it", group_help=help_text)
    _timeline_arg(p)
    p.set_defaults(func=cmd_playhead_get, read_only=True)

    p = registry.action("playhead", "set", "move the playhead to a timecode (with -t, that timeline becomes current)")
    p.add_argument("timecode", help="HH:MM:SS:FF, e.g. 01:00:05:00")
    _timeline_arg(p)
    p.set_defaults(func=cmd_playhead_set, read_only=False)

    p = registry.action("playhead", "item", "the video clip under the playhead: REF, type, start, end, duration")
    _timeline_arg(p)
    p.set_defaults(func=cmd_playhead_item, read_only=True)


def _on_arg(p):
    p.add_argument("--on", default="timeline", metavar="REF", help=f"whose markers: {ON_HINT}")


def _register_marker(registry):
    frame_help = "marker frame as 'dava marker show' lists it"
    p = registry.action("marker", "show", "markers of a timeline, timeline item or media pool clip, with custom data "
                        "('marker list' is the timeline-only form)")
    _on_arg(p)
    p.add_argument("-c", "--color", type=marker_color, help="only markers of this color")
    p.set_defaults(func=cmd_marker_show, read_only=True)

    p = registry.action("marker", "create", "add a marker with optional custom data to a timeline, timeline item "
                        "or media pool clip ('marker add' is the timeline-only form)")
    p.add_argument("frame", type=int, help="frame (timelines: offset from the timeline start)")
    _on_arg(p)
    p.add_argument("-c", "--color", type=marker_color, default="Blue", help=f"one of {', '.join(MARKER_COLORS)}")
    p.add_argument("-n", "--name", default="")
    p.add_argument("--note", default="")
    p.add_argument("-d", "--duration", type=positive_int, default=1, help="duration in frames (default: 1)")
    p.add_argument("--data", help="custom data text (not shown in Resolve's UI; find it with 'dava marker find')")
    p.set_defaults(func=cmd_marker_create, read_only=False)

    p = registry.action("marker", "delete", "delete markers by frame, color, custom data, or all of them")
    _on_arg(p)
    which = p.add_mutually_exclusive_group(required=True)
    which.add_argument("-f", "--frame", type=int, action="append", help=f"{frame_help} (repeatable)")
    which.add_argument("-c", "--color", type=marker_color, help="every marker of this color")
    which.add_argument("--custom-data", metavar="TEXT", help="the first marker with this custom data")
    which.add_argument("--all", action="store_true", help="every marker")
    p.set_defaults(func=cmd_marker_delete, read_only=False)

    p = registry.action("marker", "find", "the first marker whose custom data is TEXT")
    p.add_argument("data", metavar="TEXT", help="custom data to look for")
    _on_arg(p)
    p.set_defaults(func=cmd_marker_find, read_only=True)

    p = registry.action("marker", "data", "show the custom data of the marker at FRAME, or replace it with TEXT")
    p.add_argument("frame", type=int, help=frame_help)
    p.add_argument("text", nargs="?", help="new custom data")
    _on_arg(p)
    p.set_defaults(func=cmd_marker_data, read_only=lambda a: a.text is None)


def register(registry):
    _register_timeline(registry)
    _register_track(registry)
    _register_playhead(registry)
    _register_marker(registry)
