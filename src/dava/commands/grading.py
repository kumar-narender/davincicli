"""dava look (hue-selective look LUTs) and dava deliver (platform-ready renders and checks)."""

import json
import os
import shutil
import struct
import subprocess
import sys
import time

from .. import looklut
from ..connect import ResolveError
from ..helpers import add_item_args, apply_render_settings, check_node, current_project, find_timeline, select_items

LUT_SUBDIR = "dava"


def lut_root():
    """Resolve's master LUT folder (BMD_RESOLVE_LUT_DIR overrides it, per Resolve's configuration notes)."""
    if os.environ.get("BMD_RESOLVE_LUT_DIR"):
        return os.environ["BMD_RESOLVE_LUT_DIR"]
    if sys.platform.startswith("darwin"):
        return "/Library/Application Support/Blackmagic Design/DaVinci Resolve/LUT"
    if sys.platform.startswith("win"):
        return os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"), "Blackmagic Design", "DaVinci Resolve",
                            "Support", "LUT")
    return "/opt/resolve/LUT"


def look_params(args):
    params = dict(looklut.PRESETS[args.preset])
    for key in params:
        value = getattr(args, key, None)
        if value is not None:
            params[key] = value
    return params


def write_look_file(preset, name=None, overrides=None):
    """Write a look LUT into Resolve's LUT folder; returns (path, name as SetLUT expects it)."""
    if preset not in looklut.PRESETS:
        raise ResolveError(f"Unknown look preset {preset!r}; choose from {', '.join(looklut.PRESETS)}.")
    params = dict(looklut.PRESETS[preset])
    unknown = sorted(set(overrides or {}) - set(params) - {"green_shift", "green_sat", "green_deepen", "skin_protect",
                                                              "skin_shift", "skin_sat", "skin_bright"})
    if unknown:
        raise ResolveError(f"Unknown look parameter(s): {', '.join(unknown)}.")
    params.update(overrides or {})
    name = name or f"dava {preset}"
    folder = os.path.join(lut_root(), LUT_SUBDIR)
    try:
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"{name}.cube")
        looklut.write_cube(path, params, title=name)
    except OSError as exc:
        raise ResolveError(f"Cannot write the LUT into {folder}: {exc.strerror}. Set BMD_RESOLVE_LUT_DIR or fix "
                           "the folder permissions.") from exc
    return path, f"{LUT_SUBDIR}/{name}.cube"


def write_look(args):
    overrides = {k: v for k, v in look_params(args).items() if v != looklut.PRESETS[args.preset].get(k)}
    return write_look_file(args.preset, args.name, overrides)


def cmd_look_create(resolve, args):
    path, relative = write_look(args)
    return {"lut": path, "resolve_name": relative, "params": look_params(args)}


def cmd_look_apply(resolve, args):
    path, relative = write_look(args)
    project = current_project(resolve)
    items = select_items(resolve, args)
    project.RefreshLUTList()  # SetLUT only finds LUTs Resolve has scanned
    for index, item in items:
        graph = item.GetNodeGraph()
        check_node(graph, args.node)
        if not graph.SetLUT(args.node, relative):
            raise ResolveError(f"Resolve did not accept LUT {relative!r} on clip {index} node {args.node}.")
    return {"lut": path, "applied_to": [item.GetName() for _, item in items], "node": args.node,
            "params": look_params(args)}


def cmd_look_presets(resolve, args):
    return [{"preset": name, **params} for name, params in looklut.PRESETS.items()]


# -- delivery ------------------------------------------------------------------------

INSTAGRAM = {"width": 1080, "height": 1920, "codec": "H264", "format": "mp4", "bitrate": 12000}


def cmd_instagram(resolve, args):
    project = current_project(resolve)
    timeline = find_timeline(project, args.timeline)
    if not project.SetCurrentTimeline(timeline):
        raise ResolveError(f"Could not open timeline {timeline.GetName()!r}.")
    if not project.SetCurrentRenderFormatAndCodec(INSTAGRAM["format"], INSTAGRAM["codec"]):
        raise ResolveError("Could not select MP4 / H.264.")
    if not project.SetCurrentRenderMode(1):
        raise ResolveError("Could not select single-clip render mode.")
    name = args.name or timeline.GetName()
    rejected = apply_render_settings(project, {
        "MarkIn": timeline.GetStartFrame(), "MarkOut": timeline.GetEndFrame() - 1,
        "TargetDir": os.path.abspath(os.path.expanduser(args.dir)), "CustomName": name,
        "ExportVideo": True, "ExportAudio": args.audio,
        "FormatWidth": INSTAGRAM["width"], "FormatHeight": INSTAGRAM["height"],
        "VideoQuality": args.bitrate, "EncodingProfile": "High", "MultiPassEncode": True, "NetworkOptimization": True,
    })
    critical = {"MarkIn", "MarkOut", "TargetDir", "FormatWidth", "FormatHeight"} & set(rejected)
    if critical:
        raise ResolveError(f"Resolve rejected essential render settings: {', '.join(sorted(critical))}.")
    job = project.AddRenderJob()
    if not job:
        raise ResolveError("Could not add the render job.")
    if not project.StartRendering([job], False):
        raise ResolveError("Resolve refused to start rendering.")
    output = os.path.join(os.path.abspath(os.path.expanduser(args.dir)), f"{name}.mp4")
    result = {"job": job, "output": output, "fps": timeline.GetSettings().get("timelineFrameRate"),
              "warnings": [f"setting rejected: {key}" for key in rejected]}
    if args.wait:
        while project.IsRenderingInProgress():
            status = project.GetRenderJobStatus(job)
            print(f"\rrendering {status.get('CompletionPercentage', 0)}%", end="", file=sys.stderr, flush=True)
            time.sleep(1)
        print(file=sys.stderr)
        status = project.GetRenderJobStatus(job)
        if status.get("JobStatus") != "Complete":
            raise ResolveError(f"Render ended as {status.get('JobStatus')}: {status.get('Error', '')}".strip())
        if shutil.which("ffprobe") and os.path.isfile(output):
            result["check"] = check_instagram(output)
    return result


def _faststart(path):
    order = []
    with open(path, "rb") as handle:
        while len(order) < 10:
            header = handle.read(8)
            if len(header) < 8:
                break
            size, kind = struct.unpack(">I4s", header)
            order.append(kind.decode("latin1"))
            if size == 1:
                size = struct.unpack(">Q", handle.read(8))[0]
                handle.seek(size - 16, 1)
            elif size < 8:
                break
            else:
                handle.seek(size - 8, 1)
    return "moov" in order and "mdat" in order and order.index("moov") < order.index("mdat")


def check_instagram(path):
    """Check a file against Instagram Reels delivery expectations; returns {"ok", "problems", "facts"}."""
    if not shutil.which("ffprobe"):
        raise ResolveError("ffprobe is not installed (brew install ffmpeg).")
    probe = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path],
                           capture_output=True, text=True, check=False)
    if probe.returncode != 0:
        raise ResolveError(f"ffprobe could not read {path}: {probe.stderr.strip()}")
    info = json.loads(probe.stdout)
    video = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    audio = [s for s in info["streams"] if s["codec_type"] == "audio"]
    if video is None:
        raise ResolveError(f"{path} has no video stream.")
    num, den = (int(x) for x in video.get("r_frame_rate", "0/1").split("/"))
    fps = num / den if den else 0
    bitrate = int(video.get("bit_rate") or info["format"].get("bit_rate") or 0)
    facts = {"codec": video.get("codec_name"), "profile": video.get("profile"), "size": f"{video['width']}x{video['height']}",
             "fps": round(fps, 3), "pix_fmt": video.get("pix_fmt"), "bitrate_mbps": round(bitrate / 1e6, 2),
             "primaries": video.get("color_primaries"), "transfer": video.get("color_transfer"),
             "matrix": video.get("color_space"), "audio": [a.get("codec_name") for a in audio],
             "duration_s": round(float(info["format"].get("duration", 0)), 2),
             "size_mb": round(int(info["format"].get("size", 0)) / 1e6, 1), "faststart": _faststart(path)}
    problems = []
    if facts["codec"] != "h264":
        problems.append("codec should be H.264")
    if video["width"] * 16 != video["height"] * 9 or video["width"] < 1080:
        problems.append("frame should be 9:16 and at least 1080x1920")
    if not 23 <= fps <= 60:
        problems.append("frame rate should be between 23.976 and 60")
    if facts["pix_fmt"] != "yuv420p":
        problems.append("pixel format should be yuv420p (8-bit 4:2:0)")
    for key in ("primaries", "transfer", "matrix"):
        if facts[key] != "bt709":
            problems.append(f"color {key} should be tagged bt709 (untagged or HDR files look washed out)")
    if bitrate and not 5e6 <= bitrate <= 25e6:
        problems.append("video bitrate should be 5-25 Mbps")
    if any(codec != "aac" for codec in facts["audio"]):
        problems.append("audio should be AAC")
    if facts["size_mb"] > 250:
        problems.append("file should be under 250 MB")
    if not facts["faststart"]:
        problems.append("moov atom should be at the start (faststart / network optimization)")
    return {"ok": not problems, "problems": problems, "facts": facts}


def cmd_check(resolve, args):
    return {os.path.basename(path): check_instagram(path) for path in args.files}


def _look_options(p):
    p.add_argument("-p", "--preset", choices=sorted(looklut.PRESETS), default="vibrant")
    p.add_argument("-n", "--name", help="LUT name (default: 'dava PRESET')")
    for key, help_text in (("sky_sat", "sky saturation multiplier"), ("sky_shift", "0..1 pull of sky hue to clean blue"),
                           ("sky_deepen", "0..0.2 sky darkening"), ("gold_sat", "gold saturation multiplier"),
                           ("gold_shift", "0..1 pull of yellow/orange to gold"), ("gold_bright", "gold brightening"),
                           ("vibrance", "boost for less saturated colors")):
        p.add_argument("--" + key.replace("_", "-"), dest=key, type=float, help=help_text)


def register(registry):
    p = registry.action("look", "apply", "generate a hue-selective look LUT (bluer sky, richer gold, neutrals kept "
                        "clean) and put it on a node of timeline clips", group_help="hue-selective look LUTs")
    _look_options(p)
    p.add_argument("-N", "--node", type=int, default=1, help="node number, from 1 (default: 1)")
    add_item_args(p)
    p.set_defaults(func=cmd_look_apply, read_only=False, always_json=True)

    p = registry.action("look", "create", "only write the look LUT into Resolve's LUT folder")
    _look_options(p)
    p.set_defaults(func=cmd_look_create, needs_resolve=False, read_only=False, always_json=True)

    p = registry.action("look", "presets", "list look presets and their parameters")
    p.set_defaults(func=cmd_look_presets, needs_resolve=False, read_only=True)

    p = registry.action("deliver", "instagram", "render a timeline for Instagram Reels: 1080x1920 H.264 High, "
                        "~12 Mbps multi-pass, faststart, native frame rate", group_help="platform-ready renders")
    p.add_argument("-t", "--timeline", help="timeline name (default: current)")
    p.add_argument("-o", "--dir", default="~/Desktop", help="output folder (default: ~/Desktop)")
    p.add_argument("-n", "--name", help="file name without extension (default: timeline name)")
    p.add_argument("--audio", action="store_true", help="include audio (default: none, add music in Instagram)")
    p.add_argument("--bitrate", type=int, default=INSTAGRAM["bitrate"], help="kbps (default: 12000)")
    p.add_argument("-w", "--wait", action="store_true", help="wait for the render and check the file")
    p.set_defaults(func=cmd_instagram, read_only=False, always_json=True)

    register_photos(registry)

    p = registry.action("deliver", "check", "check video files against Instagram Reels specs (needs ffprobe)")
    p.add_argument("files", nargs="+", metavar="FILE")
    p.set_defaults(func=cmd_check, needs_resolve=False, read_only=True, always_json=True)


def cmd_photos_grade(resolve, args):
    from .. import photos

    return photos.grade_photos(photos.collect(args.paths), os.path.abspath(os.path.expanduser(args.out)),
                               look=None if args.look == "none" else args.look, saturation=args.saturation,
                               suffix=args.suffix, jobs=args.jobs, quality=args.quality, profile=args.profile)


def register_photos(registry):
    p = registry.action("photos", "grade", "grade photos (files or folders) outside Resolve: measured dehaze and "
                        "exposure, color-managed to sRGB, plus a look (needs ImageMagick 7)",
                        group_help="grade still photos with the same looks")
    p.add_argument("paths", nargs="+", metavar="PATH")
    p.add_argument("-o", "--out", required=True, help="output folder")
    p.add_argument("-l", "--look", default="travel", choices=sorted(looklut.PRESETS) + ["none"])
    p.add_argument("--saturation", type=float, default=1.05, help="saturation before the look (default: 1.05)")
    p.add_argument("--suffix", default=" graded", help="added to each file name (default: ' graded')")
    p.add_argument("-j", "--jobs", type=int, default=8, help="photos graded in parallel (default: 8)")
    p.add_argument("--quality", type=int, default=95, help="JPEG quality (default: 95)")
    p.add_argument("--profile", help="sRGB ICC profile (default: the system one)")
    p.set_defaults(func=cmd_photos_grade, needs_resolve=False, read_only=False, always_json=True)
