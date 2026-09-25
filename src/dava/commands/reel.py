"""dava reel build SPEC.json: a short-form edit (Instagram reel, story) from a JSON spec.

Resolve's API cannot trim stills (they keep the standard still duration) or change their speed, so each shot
goes on its own video track, starting where the previous shot should end; the higher track covers the one
below. Transitions go on each shot's start edge (right-aligned), which works over the shot underneath.

Spec (JSON):
{
  "project": "My Project",                 optional; the build refuses to run in any other open project
  "timeline": "My Reel",                   created if missing (as an emptied copy of a timeline that already has
                                           the resolution: resizing the current timeline can hang Resolve)
  "clear": true,                           required to empty an existing timeline that has clips
  "resolution": [1080, 1920],              optional custom timeline resolution
  "intro": {"clip": "hero.jpg", "frames": 72, "title": "HELLO\\nWORLD"},
  "shots": [
    {"clip": "a.jpg", "frames": 26, "transition": "Crash Zoom", "caption": "PRAYER WHEELS"},
    {"clip": "b.mov", "in": 62, "frames": 60, "transition": "Tunnel of Light"},
    {"clip": "graded.mp4", "in": 50, "frames": 75, "grade": false, "zoom": false}   already graded: no reel grade
  ],
  "outro": {"text": "NAMASTE", "frames": 64},    shown over the last shot
  "transition_frames": 10,
  "grade": {"Slope": "1.12 1.04 0.93", "Offset": "-0.03 -0.02 0.0", "Power": "1.0 1.0 1.0", "Saturation": 1.3},
                                            a shot's own "cdl" (same keys) replaces "grade" for that shot
  "look": {"preset": "travel", "name": "My Look"},   hue-selective LUT on every graded shot (see 'dava look presets')
  "zoom": true,                             dynamic zoom (push-in) on every shot
  "render": {"dir": "~/Desktop", "name": "My Reel", "format": "mp4", "codec": "H264"}
}
"""

import json
import os

from ..connect import ResolveError
from ..bridge import iter_clips
from ..helpers import apply_render_settings, current_project, timelines
from .grading import write_look_file
from .overlay import add_text_overlay


def load_spec(path):
    try:
        with open(path, encoding="utf-8") as handle:
            spec = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ResolveError(f"Cannot read reel spec {path}: {exc}") from exc
    if not spec.get("shots"):
        raise ResolveError("The spec needs a non-empty 'shots' list.")
    return spec


def _size(timeline):
    settings = timeline.GetSettings() or {}
    return [str(settings.get("timelineResolutionWidth")), str(settings.get("timelineResolutionHeight"))]


def _timeline(project, spec):
    """Open (or make) the reel timeline at the spec's resolution; returns (timeline, copied).

    Resizing the current timeline deadlocked Resolve 21.1 (its main thread waited for Fusion's render lock while
    a Fusion render waited for the main thread), so the size is only changed when it differs, and never on the
    current timeline: a new timeline starts as a copy of one that already has the size (copied=True, the build
    empties it), otherwise another timeline is made current while this one is resized.
    """
    name = spec.get("timeline", "Reel")
    size = [str(v) for v in spec["resolution"]] if spec.get("resolution") else None
    others = timelines(project)
    timeline = next((t for t in others if t.GetName() == name), None)
    copied = False
    if timeline is None:
        current = project.GetCurrentTimeline()
        template = next((t for t in [current, *others] if t and size and _size(t) == size), None)
        if template:
            timeline = template.DuplicateTimeline(name)
            copied = True
        else:
            timeline = project.GetMediaPool().CreateEmptyTimeline(name)
        if not timeline:
            raise ResolveError(f"Could not create timeline {name!r}.")
    else:
        others = [t for t in others if t.GetUniqueId() != timeline.GetUniqueId()]
    if size and _size(timeline) != size:
        current = project.GetCurrentTimeline()
        if current and current.GetUniqueId() == timeline.GetUniqueId() and others:
            if not project.SetCurrentTimeline(others[0]):
                raise ResolveError(f"Could not switch away from {name!r} to resize it.")
        width, height = size
        if not timeline.SetSettings({"useCustomSettings": "1", "timelineResolutionWidth": width,
                                     "timelineResolutionHeight": height}):
            raise ResolveError(f"Could not set the timeline resolution to {width}x{height}.")
    if not project.SetCurrentTimeline(timeline):
        raise ResolveError(f"Could not open timeline {name!r}.")
    return timeline, copied


def _items(timeline):
    return [item for kind in ("video", "audio", "subtitle")
            for track in range(1, (timeline.GetTrackCount(kind) or 0) + 1)
            for item in timeline.GetItemListInTrack(kind, track) or []]


def clip_index(project):
    """Walk the media pool once: name -> [clips]. Each lookup over a bridge is a round trip, so the build
    resolves every shot from this index instead of walking the pool per shot."""
    index = {}
    for _, clip in iter_clips(project.GetMediaPool().GetRootFolder()):
        index.setdefault(clip.GetName(), []).append(clip)
    return index


def lookup(index, name):
    clips = index.get(name, [])
    if not clips:
        raise ResolveError(f"No media pool clip named {name!r}.")
    if len(clips) > 1:
        raise ResolveError(f"Clip name {name!r} is ambiguous ({len(clips)} clips); rename one in the media pool.")
    return clips[0]


def _preflight(resolve, project, spec, index):
    """Check everything that can be checked before the first change to the project."""
    wanted = spec.get("project")
    if wanted is not None and project.GetName() != wanted:
        raise ResolveError(f"The spec is for project {wanted!r} but {project.GetName()!r} is open; "
                           f"open {wanted!r} first. Nothing was changed.")
    names = [s["clip"] for s in spec["shots"]] + ([spec["intro"]["clip"]] if spec.get("intro") else [])
    if spec.get("carrier"):
        names.append(spec["carrier"])
    missing = []
    for name in dict.fromkeys(names):
        try:
            lookup(index, name)
        except ResolveError as exc:
            missing.append(str(exc))
    if missing:
        raise ResolveError("Nothing was changed: " + " ".join(missing))
    for timeline in timelines(project):
        if timeline.GetName() == spec.get("timeline", "Reel"):
            count = len(_items(timeline))
            if count and not spec.get("clear"):
                raise ResolveError(f"Timeline {timeline.GetName()!r} already has {count} clip(s); set \"clear\": true "
                                   "in the spec to replace them, or use another timeline name. Nothing was changed.")


def _clear(timeline):
    for kind in ("video", "audio", "subtitle"):
        for track in range(1, (timeline.GetTrackCount(kind) or 0) + 1):
            items = timeline.GetItemListInTrack(kind, track) or []
            if items and not timeline.DeleteClips(items, False):
                raise ResolveError(f"Could not clear {kind} track {track}.")


def build(resolve, session, spec):
    project = current_project(resolve)
    media_pool = project.GetMediaPool()
    index = clip_index(project)
    _preflight(resolve, project, spec, index)
    timeline, copied = _timeline(project, spec)
    if spec.get("clear") or copied:
        _clear(timeline)
    if copied and not timeline.DeleteMarkersByColor("All"):
        raise ResolveError(f"Could not remove the markers copied into {timeline.GetName()!r}.")
    start = timeline.GetStartFrame()
    grade = spec.get("grade")
    zoom = spec.get("zoom", True)
    look = None
    if spec.get("look"):
        look_spec = dict(spec["look"])
        _, look = write_look_file(look_spec.pop("preset", "travel"), look_spec.pop("name", None), look_spec)
        project.RefreshLUTList()
    transition_frames = spec.get("transition_frames", 10)
    warnings, placed = [], []

    def place(name, track, record, source_in=None, frames=None, shot_grade=True, shot_zoom=True, shot_cdl=None):
        while timeline.GetTrackCount("video") < track:
            timeline.AddTrack("video")
        info = {"mediaPoolItem": lookup(index, name), "trackIndex": track, "recordFrame": record, "mediaType": 1}
        if source_in is not None:
            info.update({"startFrame": source_in, "endFrame": source_in + frames - 1})
        items = media_pool.AppendToTimeline([info])
        if not items:
            raise ResolveError(f"Could not place {name!r} on video track {track}.")
        item = items[0]
        properties = {"Scaling": resolve.SCALE_FILL}
        if zoom and shot_zoom:
            properties.update({"DynamicZoomEnabled": True, "DynamicZoomEase": resolve.DYNAMIC_ZOOM_EASE_IN_AND_OUT})
        if not item.SetProperties(properties):
            warnings.append(f"{name}: properties not applied")
        cdl = shot_cdl or grade
        if shot_grade and cdl and not item.SetCDL({"NodeIndex": 1, **cdl}):
            warnings.append(f"{name}: grade not applied")
        if shot_grade and look and not item.GetNodeGraph().SetLUT(1, look):
            warnings.append(f"{name}: look LUT not applied")
        return item

    intro = spec.get("intro")
    track, record = 1, start
    if intro:
        place(intro["clip"], 1, start, shot_grade=intro.get("grade", True), shot_cdl=intro.get("cdl"))
        placed.append({"clip": intro["clip"], "track": 1, "at": 0})
        record = start + intro.get("frames", 72)
        track = 2
    captions = []
    last_record = record
    for shot in spec["shots"]:
        frames = shot.get("frames", 26)
        item = place(shot["clip"], track, record, shot.get("in"), frames if "in" in shot else None,
                     shot.get("grade", True), shot.get("zoom", True), shot.get("cdl"))
        if shot.get("transition"):
            if not item.AddTransition({"type": shot["transition"], "category": shot.get("transition_category", "fusion"),
                                       "position": "start", "alignment": "right",
                                       "duration": shot.get("transition_frames", transition_frames)}):
                warnings.append(f"{shot['clip']}: transition {shot['transition']!r} not added")
        if shot.get("caption"):
            captions.append((shot["caption"], record - start))
        placed.append({"clip": shot["clip"], "track": track, "at": record - start})
        last_record = record
        record += item.GetDuration() if "in" in shot else frames
        track += 1

    overlays = []
    if intro and intro.get("title"):
        overlays.append((intro["title"], "morph", 0))
    overlays.extend((text, "caption", at) for text, at in captions)
    outro = spec.get("outro") or {}
    if outro.get("text"):
        overlays.append((outro["text"], "outro", last_record - start))
    carrier = spec.get("carrier", intro["clip"] if intro else None)
    for text, preset, at in overlays:
        add_text_overlay(resolve, timeline, text, preset, at, carrier=carrier)

    end = last_record + outro.get("frames", 64) - 1
    result = {"timeline": timeline.GetName(), "shots": placed, "overlays": len(overlays),
              "tracks": timeline.GetTrackCount("video"), "frames": end - start + 1,
              "seconds": round((end - start + 1) / float(str(timeline.GetSettings().get("timelineFrameRate")).split()[0]), 2),
              "warnings": warnings}
    render = spec.get("render")
    if render:
        if not project.SetCurrentRenderFormatAndCodec(render.get("format", "mp4"), render.get("codec", "H264")):
            raise ResolveError("Could not select the render format and codec.")
        # Same delivery settings as 'dava deliver instagram' (checked with 'dava deliver check').
        rejected = apply_render_settings(project, {
            "MarkIn": start, "MarkOut": end, "TargetDir": os.path.expanduser(render.get("dir", "~/Desktop")),
            "CustomName": render.get("name", timeline.GetName()), "ExportVideo": True,
            "ExportAudio": render.get("audio", False), "VideoQuality": render.get("bitrate", 12000),
            "EncodingProfile": "High", "MultiPassEncode": True, "NetworkOptimization": True})
        warnings.extend(f"render setting rejected: {key}" for key in rejected)
        job = project.AddRenderJob()
        if not job:
            raise ResolveError("Could not add the render job.")
        if not project.StartRendering([job], False):
            raise ResolveError("Resolve refused to start rendering.")
        result["render_job"] = job
    return result


def cmd_build(resolve, args):
    return build(resolve, args.session, load_spec(args.spec))


def register(registry):
    p = registry.action("reel", "build", "build a short-form edit (stacked shots, Fusion transitions, grade, "
                        "text overlays, render) from a JSON spec; see 'dava reel build --help'",
                        group_help="short-form edits from a JSON spec", description=__doc__)
    p.add_argument("spec", help="path of the JSON spec")
    p.set_defaults(func=cmd_build, read_only=False, always_json=True)
