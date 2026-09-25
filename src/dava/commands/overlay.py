"""dava overlay: animated text on top of everything, as a text-only Fusion composition.

See dava/fusiontext.py for why overlays are built this way instead of with InsertFusionTitleIntoTimeline.
"""

import os
import tempfile

from .. import fusiontext
from ..bridge import describe_object
from ..connect import ResolveError
from ..helpers import current_project, find_clip, find_timeline

TMP_DIR = os.path.join(os.path.expanduser("~"), ".dava", "tmp")


def frames_per_second(timeline):
    rate = timeline.GetSettings().get("timelineFrameRate")
    return float(str(rate).split()[0])


def to_offset(timeline, at):
    """'48' -> 48 frames; '00:00:02:00' -> frames from the timeline start; None -> 0."""
    if at is None:
        return 0
    if ":" not in at:
        return int(at)
    fps = round(frames_per_second(timeline))
    hours, minutes, seconds, frames = (int(part) for part in at.split(":"))
    start_hours, start_minutes, start_seconds, start_frames = (int(p) for p in timeline.GetStartTimecode().split(":"))
    absolute = ((hours * 60 + minutes) * 60 + seconds) * fps + frames
    base = ((start_hours * 60 + start_minutes) * 60 + start_seconds) * fps + start_frames
    return absolute - base if absolute >= base else absolute


def first_still(project):
    root = project.GetMediaPool().GetRootFolder()
    for clip in root.GetClipList() or []:
        if (clip.GetClipProperty() or {}).get("Type") == "Still":
            return clip
    raise ResolveError("No still image in the media pool root to carry the overlay; pass --carrier CLIP.")


def add_text_overlay(resolve, timeline, text, preset="morph", offset=0, track=None, carrier=None,
                     font="Avenir Next", scale=1.0, center=None):
    """Place a carrier still at `offset` frames on `track` (default: a new top track) with a text comp."""
    project = current_project(resolve)
    media_pool = project.GetMediaPool()
    clip = find_clip(resolve, carrier) if carrier else first_still(project)
    if track is None:
        timeline.AddTrack("video")
        track = timeline.GetTrackCount("video")
    while timeline.GetTrackCount("video") < track:
        timeline.AddTrack("video")
    items = media_pool.AppendToTimeline([{"mediaPoolItem": clip, "trackIndex": track,
                                          "recordFrame": timeline.GetStartFrame() + offset, "mediaType": 1}])
    if not items:
        raise ResolveError(f"Could not place the overlay carrier on video track {track}.")
    item = items[0]
    item.SetProperties({"DynamicZoomEnabled": False})
    if not item.AddFusionComp():
        raise ResolveError("Could not add a Fusion composition to the overlay clip.")
    os.makedirs(TMP_DIR, mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=TMP_DIR) as tmp:
        default_path = os.path.join(tmp, "default.comp")
        if not item.ExportFusionComp(default_path, 1):
            raise ResolveError("Could not export the overlay clip's composition.")
        with open(default_path, encoding="utf-8") as handle:
            comp_text = fusiontext.overlay_comp(handle.read(), text, preset, font, center, scale)
        overlay_path = os.path.join(tmp, "overlay.comp")
        with open(overlay_path, "w", encoding="utf-8") as handle:
            handle.write(comp_text)
        if not item.ImportFusionComp(overlay_path):
            raise ResolveError("Resolve refused the generated overlay composition.")
    return item, track


def _center(text):
    if text is None:
        return None
    x, _, y = text.partition(",")
    return float(x), float(y)


def cmd_text(resolve, args):
    project = current_project(resolve)
    timeline = find_timeline(project, args.timeline)
    offset = to_offset(timeline, args.at)
    item, track = add_text_overlay(resolve, timeline, args.text.replace("\\n", "\n"), args.preset, offset,
                                   args.track, args.carrier, args.font, args.scale, _center(args.center))
    info = describe_object(args.session, item, "TimelineItem")
    info.update({"track": track, "offset": offset, "preset": args.preset})
    return info


def cmd_presets(resolve, args):
    return [{"preset": name, "delay_per_letter": p["delay"], "center": list(p["center"]),
             "last_key_frame": max(t for t, _ in p["opacity"] + p["blur"])} for name, p in fusiontext.PRESETS.items()]


def register(registry):
    help_text = "animated text overlays above all shots (Fusion, per-letter animation)"
    p = registry.action("overlay", "text", "add animated text on a new top track (use \\n for a line break)",
                        group_help=help_text)
    p.add_argument("text")
    p.add_argument("-p", "--preset", choices=sorted(fusiontext.PRESETS), default="morph",
                   help="morph: big title in and out (~3 s); caption: short lower text (~1 s); outro: stays")
    p.add_argument("--at", help="frames from the timeline start, or a timecode (default: 0)")
    p.add_argument("-T", "--track", type=int, help="video track (default: a new top track)")
    p.add_argument("--carrier", help="still clip that carries the composition (default: first still in the pool)")
    p.add_argument("--font", default="Avenir Next")
    p.add_argument("--scale", type=float, default=1.0, help="text size multiplier (default: 1)")
    p.add_argument("--center", metavar="X,Y", help="text center in 0..1 frame coordinates, e.g. 0.5,0.8")
    p.add_argument("-t", "--timeline", help="timeline name (default: current)")
    p.set_defaults(func=cmd_text, read_only=False, always_json=True)

    p = registry.action("overlay", "presets", "list the text animation presets")
    p.set_defaults(func=cmd_presets, needs_resolve=False, read_only=True)
