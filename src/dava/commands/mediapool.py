"""dava folder / clip / storage, and new media and meta actions: the media pool and Media Storage.

Folders are named by a path from the root (/Master/Shots, or Shots/Day1) or by REF (folder#ID, folder
for the current one). Clips are named by their unique name or by REF (clip#ID, clip:/Master/Shots/a.mov).
Commands that take several clips act clip by clip and stop at the first one Resolve rejects, naming the
clips already done. Commands that append clip ranges take CLIP@IN:OUT (source frames, both included).
"""

import argparse
import os
import re
import time

from ..bridge import describe_object, iter_clips, resolve_ref
from ..connect import ResolveError
from ..helpers import current_project, find_timeline

CLIP_HELP = "media pool clip: a unique clip name, or a REF such as clip#ID or clip:/Master/Shots/a.mov"
FOLDER_HELP = "media pool folder: a path from the root such as /Master/Shots or Shots/Day1, or a REF such as folder#ID"
RANGE_HELP = ("a clip as in CLIP, optionally with a source range CLIP@IN:OUT (frames IN to OUT, both included; "
              "ranges are ignored for stills, which keep the standard still duration)")
AI_HINT = ("AI tools need DaVinci Resolve Studio and sometimes an Extras download (application menu > Extras "
           "Download Manager); run the tool once from the UI to see Resolve's reason.")

# Marker colors a slate analysis can use (SlateMarkerColor without MARKER_NONE) and flag colors (FlagColor).
MARKER_COLORS = ("Blue", "Cyan", "Green", "Yellow", "Red", "Pink", "Purple", "Fuchsia",
                 "Rose", "Lavender", "Sky", "Mint", "Lemon", "Sand", "Cocoa", "Cream")
FLAG_COLORS = MARKER_COLORS
MARK_TYPES = ("video", "audio", "all")
STEREO_EYES = ("left", "right")

# CLI choice -> resolve.* constant name, per API constant alias.
SYNC_MODES = {  # AudioSyncMode
    "waveform": "AUDIO_SYNC_WAVEFORM", "timecode": "AUDIO_SYNC_TIMECODE", "in": "AUDIO_SYNC_IN",
    "out": "AUDIO_SYNC_OUT", "marker": "AUDIO_SYNC_MARKER",
}
SYNC_CHANNELS = {"auto": "AUDIO_SYNC_CHANNEL_AUTOMATIC", "mix": "AUDIO_SYNC_CHANNEL_MIX"}  # AudioSyncChannel
ANGLE_SYNC = {  # MulticamAngleSyncMode
    "in": "MULTICAM_ANGLE_SYNC_IN", "out": "MULTICAM_ANGLE_SYNC_OUT", "timecode": "MULTICAM_ANGLE_SYNC_TIMECODE",
    "audio": "MULTICAM_ANGLE_SYNC_AUDIO", "marker": "MULTICAM_ANGLE_SYNC_MARKER",
}
MULTICAM_AUDIO = {  # MulticamAudioMode
    "adaptive": "MULTICAM_AUDIO_ADAPTIVE", "source": "MULTICAM_AUDIO_SOURCE",
    "reference": "MULTICAM_AUDIO_REFERENCE", "all": "MULTICAM_AUDIO_ALL",
}
ANGLE_NAMES = {  # MulticamAngleNameMode
    "sequential": "MULTICAM_ANGLE_NAME_SEQUENTIAL", "angle": "MULTICAM_ANGLE_NAME_ANGLE",
    "camera": "MULTICAM_ANGLE_NAME_CAMERA", "clip": "MULTICAM_ANGLE_NAME_CLIP", "file": "MULTICAM_ANGLE_NAME_FILE",
}
DETECT_MODES = {  # MulticamDetectMode
    "camera-number": "MULTICAM_DETECT_BY_CAMERA_NUMBER", "angle": "MULTICAM_DETECT_BY_ANGLE",
    "reel-number": "MULTICAM_DETECT_BY_REEL_NUMBER", "reel-name": "MULTICAM_DETECT_BY_REEL_NAME",
    "roll-card": "MULTICAM_DETECT_BY_ROLL_CARD", "none": "MULTICAM_DETECT_NONE",
}
CHECKSUMS = {  # CloneChecksumType
    "none": "CLONE_CHECKSUM_TYPE_NONE", "filesize": "CLONE_CHECKSUM_TYPE_FILESIZE",
    "crc32": "CLONE_CHECKSUM_TYPE_CRC32", "md5": "CLONE_CHECKSUM_TYPE_MD5", "sha256": "CLONE_CHECKSUM_TYPE_SHA256",
    "sha512": "CLONE_CHECKSUM_TYPE_SHA512", "xxh64": "CLONE_CHECKSUM_TYPE_XXH_64",
}

# Clip properties 'clip info' shows (all of them: 'dava meta props CLIP').
INFO_PROPERTIES = ("Type", "File Path", "Duration", "Frames", "FPS", "Resolution", "Start TC", "End TC",
                   "Video Codec", "Audio Ch", "Online Status", "Proxy", "Proxy Media Path", "Usage")

RANGE = re.compile(r"^(?P<target>.+)@(?P<start>\d+):(?P<end>\d+)$")


def positive_int(text):
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {text}")
    return value


def non_negative_float(text):
    value = float(text)
    if not value >= 0:  # also rejects nan
        raise argparse.ArgumentTypeError(f"must be 0 or more, got {text}")
    return value


def sync_channel(text):
    """An audio channel number (1, 2, ...) or 'auto' / 'mix'."""
    if text in SYNC_CHANNELS:
        return text
    return positive_int(text)


# ---------------------------------------------------------------------------
# Lookups


def media_pool(resolve):
    return current_project(resolve).GetMediaPool()


def constant(resolve, name):
    value = getattr(resolve, name, None)
    if value is None:
        raise ResolveError(f"This Resolve build has no constant {name}; update DaVinci Resolve.")
    return value


def channel_value(resolve, value):
    return constant(resolve, SYNC_CHANNELS[value]) if value in SYNC_CHANNELS else value


def local_path(path):
    return os.path.abspath(os.path.expanduser(path))


def existing(path, kind):
    """Absolute path that must exist: kind 'file', 'dir' or 'path' (either)."""
    path = local_path(path)
    if not {"file": os.path.isfile, "dir": os.path.isdir, "path": os.path.exists}[kind](path):
        raise ResolveError(f"{'Folder' if kind == 'dir' else kind.capitalize()} not found: {path}")
    return path


def output_path(path):
    path = local_path(path)
    if not os.path.isdir(os.path.dirname(path)):
        raise ResolveError(f"Output folder does not exist: {os.path.dirname(path)}")
    return path


def clip_ref(clip):
    return f"clip#{clip.GetUniqueId()}"


def clip_row(clip, **extra):
    return {"ref": clip_ref(clip), "name": clip.GetName(), **extra}


def is_clip_ref(text):
    return text.startswith(("clip:", "clip#", "$")) or "::" in text


def get_clips(resolve, session, texts):
    """Media pool clips by unique name or REF, in the order given (one walk of the pool for all names)."""
    index = {}
    if any(not is_clip_ref(text) for text in texts):
        for path, clip in iter_clips(media_pool(resolve).GetRootFolder()):
            index.setdefault(clip.GetName(), []).append(("/" + path, clip))
    clips = []
    for text in texts:
        if is_clip_ref(text):
            obj, cls = resolve_ref(session, text)
            if cls != "MediaPoolItem":
                raise ResolveError(f"REF {text!r} names a {cls}, not a media pool clip.")
            clips.append(obj)
            continue
        matches = index.get(text, [])
        if not matches:
            raise ResolveError(f"No media pool clip named {text!r}. List clips with 'dava folder list FOLDER' "
                               "or 'dava media list --all'.")
        if len(matches) > 1:
            folders = ", ".join(path for path, _ in matches)
            raise ResolveError(f"Clip name {text!r} is ambiguous (in {folders}); use clip:/FOLDER/PATH/{text} "
                               "or clip#ID ('dava folder list FOLDER' shows the ids).")
        clips.append(matches[0][1])
    return clips


def get_clip(resolve, session, text):
    return get_clips(resolve, session, [text])[0]


def chosen_clips(resolve, args):
    """The CLIP arguments plus, with --selected, the clips selected in the media pool (each once)."""
    clips = get_clips(resolve, args.session, args.clips)
    if args.selected:
        selected = media_pool(resolve).GetSelectedClips() or []
        if not selected:
            raise ResolveError("No clips are selected in the media pool.")
        clips += selected
    if not clips:
        raise ResolveError("Name at least one CLIP, or use --selected for the clips selected in the media pool.")
    unique = {}
    for clip in clips:
        unique.setdefault(clip.GetUniqueId(), clip)
    return list(unique.values())


def each(clips, change, failure):
    """Call change(clip) per clip; raise at the first falsy result, naming the clips already done."""
    rows = []
    for clip in clips:
        if not change(clip):
            done = f" Done before it: {', '.join(row['name'] for row in rows)}." if rows else ""
            raise ResolveError(failure(repr(clip.GetName())) + done)
        rows.append(clip_row(clip))
    return rows


def is_folder_ref(text):
    return text == "folder" or text.startswith(("folder:", "folder#", "$")) or "::" in text


def get_folder(resolve, session, text=None):
    """A media pool folder by path or REF; the current folder when text is None."""
    if text is None:
        folder = media_pool(resolve).GetCurrentFolder()
        if not folder:
            raise ResolveError("The media pool has no current folder.")
        return folder
    try:
        obj, cls = resolve_ref(session, text if is_folder_ref(text) else "folder:" + text)
    except ResolveError as exc:
        raise ResolveError(f"{exc} List folders with 'dava folder tree'.") from exc
    if cls != "Folder":
        raise ResolveError(f"REF {text!r} names a {cls}, not a media pool folder.")
    return obj


def folder_paths(root):
    """{folder unique id: path such as /Master/Shots} for every folder of the media pool."""
    paths = {}

    def walk(folder, path):
        paths[folder.GetUniqueId()] = path
        for sub in folder.GetSubFolderList() or []:
            walk(sub, f"{path}/{sub.GetName()}")

    walk(root, "/" + root.GetName())
    return paths


def folder_row(folder, paths):
    return {"ref": f"folder#{folder.GetUniqueId()}", "name": folder.GetName(), "path": paths.get(folder.GetUniqueId())}


def folder_info(resolve, session, folder):
    info = describe_object(session, folder, "Folder")
    info["path"] = folder_paths(media_pool(resolve).GetRootFolder()).get(folder.GetUniqueId())
    return info


def split_range(text):
    """'a.mov@10:57' -> ('a.mov', 10, 57); 'a.mov' -> ('a.mov', None, None)."""
    match = RANGE.match(text)
    if not match:
        return text, None, None
    start, end = int(match["start"]), int(match["end"])
    if end < start:
        raise ResolveError(f"{text!r}: the end frame {end} comes before the start frame {start}.")
    return match["target"], start, end


def clip_infos(resolve, session, specs):
    """[{"mediaPoolItem", "startFrame", "endFrame"}] for CLIP or CLIP@IN:OUT arguments."""
    parsed = [split_range(text) for text in specs]
    clips = get_clips(resolve, session, [target for target, _, _ in parsed])
    infos = []
    for clip, (_, start, end) in zip(clips, parsed):
        info = {"mediaPoolItem": clip}
        if start is not None:
            info.update(startFrame=start, endFrame=end)
        infos.append(info)
    return infos


def transcribe_args(args):
    """Arguments for TranscribeAudio(useSpeakerDetection=None, transcribeAsNestedClip=False)."""
    speakers = True if args.speakers else False if args.no_speakers else None
    if args.nested_clip:
        return (speakers, True)
    return () if speakers is None else (speakers,)


def deblur_options(args):
    """DeblurOptions from the --file-name ... --more-gpu-memory options (only the keys given)."""
    options = {key: getattr(args, dest) for key, dest in (
        ("FileName", "file_name"), ("Format", "format"), ("Codec", "codec"),
        ("EncodingProfile", "profile"), ("Encoder", "encoder")) if getattr(args, dest) is not None}
    if args.no_extreme:
        options["UseExtremeMode"] = False
    if args.whole_clip:
        options["UseMarkInMarkOut"] = False
    if args.source_res:
        options["RenderAtSourceRes"] = True
    if args.more_gpu_memory:
        options["UseMoreGpuMemory"] = True
    return options


def slate_color(resolve, color):
    return constant(resolve, "MARKER_" + color.upper())


# ---------------------------------------------------------------------------
# dava folder


def _tree(folder, path, depth, with_clips):
    clips = folder.GetClipList() or []
    subs = folder.GetSubFolderList() or []
    node = {"name": folder.GetName(), "ref": f"folder#{folder.GetUniqueId()}", "path": path,
            "clip_count": len(clips), "folder_count": len(subs)}
    if with_clips:
        node["clips"] = [clip_row(clip) for clip in clips]
    if depth is None or depth > 0:
        node["folders"] = [_tree(sub, f"{path}/{sub.GetName()}", None if depth is None else depth - 1, with_clips)
                           for sub in subs]
    return node


def cmd_folder_tree(resolve, args):
    if args.depth is not None and args.depth < 0:
        raise ResolveError(f"--depth must be 0 or more, got {args.depth}.")
    root = media_pool(resolve).GetRootFolder()
    folder = root if args.folder is None else get_folder(resolve, args.session, args.folder)
    path = folder_paths(root).get(folder.GetUniqueId())
    return _tree(folder, path, args.depth, args.clips)


def cmd_folder_list(resolve, args):
    folder = get_folder(resolve, args.session, args.folder)
    rows = [{"kind": "folder", "name": sub.GetName(), "ref": f"folder#{sub.GetUniqueId()}", "type": ""}
            for sub in folder.GetSubFolderList() or []]
    rows += [{"kind": "clip", "name": clip.GetName(), "ref": clip_ref(clip), "type": clip.GetClipProperty("Type")}
             for clip in folder.GetClipList() or []]
    return rows


def cmd_folder_current(resolve, args):
    return folder_info(resolve, args.session, get_folder(resolve, args.session))


def cmd_folder_use(resolve, args):
    folder = get_folder(resolve, args.session, args.folder)
    if not media_pool(resolve).SetCurrentFolder(folder):
        raise ResolveError(f"Resolve could not make {folder.GetName()!r} the current folder.")
    return folder_info(resolve, args.session, folder)


def cmd_folder_create(resolve, args):
    pool = media_pool(resolve)
    parent = get_folder(resolve, args.session, args.parent)
    folder = pool.AddSubFolder(parent, args.name)
    if not folder:
        raise ResolveError(f"Resolve could not create folder {args.name!r} under {parent.GetName()!r}.")
    if args.use and not pool.SetCurrentFolder(folder):
        raise ResolveError(f"Created folder {args.name!r} but could not make it the current folder.")
    return folder_info(resolve, args.session, folder)


def cmd_folder_delete(resolve, args):
    pool = media_pool(resolve)
    root = pool.GetRootFolder()
    folders = [get_folder(resolve, args.session, text) for text in args.folders]
    if any(folder.GetUniqueId() == root.GetUniqueId() for folder in folders):
        raise ResolveError("The root folder cannot be deleted.")
    paths = folder_paths(root)
    rows = [folder_row(folder, paths) for folder in folders]
    if not pool.DeleteFolders(folders):
        # A $handle can name a folder that is no longer in the tree; it has no path then.
        names = ", ".join(row["path"] or repr(row["name"]) for row in rows)
        raise ResolveError(f"Resolve could not delete folder(s) {names}.")
    return {"deleted": rows}


def cmd_folder_move(resolve, args):
    pool = media_pool(resolve)
    folders = [get_folder(resolve, args.session, text) for text in args.folders]
    target = get_folder(resolve, args.session, args.to)
    if not pool.MoveFolders(folders, target):
        names = ", ".join(repr(folder.GetName()) for folder in folders)
        raise ResolveError(f"Resolve could not move folder(s) {names} into {target.GetName()!r} "
                           "(a folder cannot move into itself or below itself).")
    paths = folder_paths(pool.GetRootFolder())
    return {"to": paths.get(target.GetUniqueId()), "moved": [folder_row(folder, paths) for folder in folders]}


def cmd_folder_export(resolve, args):
    folder = get_folder(resolve, args.session, args.folder)
    path = output_path(args.file)
    if not folder.Export(path):
        raise ResolveError(f"Resolve could not export folder {folder.GetName()!r} to {path}.")
    return {"folder": folder_info(resolve, args.session, folder)["path"], "file": path}


def cmd_folder_import(resolve, args):
    path = existing(args.file, "file")
    pool = media_pool(resolve)
    if args.source_clips_path:
        ok = pool.ImportFolderFromFile(path, existing(args.source_clips_path, "dir"))
    else:
        ok = pool.ImportFolderFromFile(path)
    if not ok:
        raise ResolveError(f"Resolve could not import a folder from {path} (is it a .drb file from 'dava folder export'?).")
    return {"imported": path}


def cmd_folder_refresh(resolve, args):
    if not media_pool(resolve).RefreshFolders():
        raise ResolveError("Resolve could not refresh the folders (this works in collaboration mode only).")
    return "Refreshed media pool folders"


def cmd_folder_stale(resolve, args):
    pool = media_pool(resolve)
    root = pool.GetRootFolder()
    top = root if args.folder is None else get_folder(resolve, args.session, args.folder)
    rows = []

    def walk(folder, path):
        rows.append({"path": path, "ref": f"folder#{folder.GetUniqueId()}", "stale": bool(folder.GetIsFolderStale())})
        for sub in folder.GetSubFolderList() or []:
            walk(sub, f"{path}/{sub.GetName()}")

    walk(top, folder_paths(root).get(top.GetUniqueId()))
    return rows


def _folder_action(resolve, args, call, what, hint=""):
    """Run call(folder) on FOLDER (default: current) and raise with `what` when Resolve returns False."""
    folder = get_folder(resolve, args.session, args.folder)
    if not call(folder):
        raise ResolveError(f"Resolve could not {what} folder {folder.GetName()!r}." + (f" {hint}" if hint else ""))
    return folder_info(resolve, args.session, folder)


def cmd_folder_transcribe(resolve, args):
    return _folder_action(resolve, args, lambda f: f.TranscribeAudio(*transcribe_args(args)),
                          "transcribe the audio of the clips in", AI_HINT)


def cmd_folder_transcript_clear(resolve, args):
    return _folder_action(resolve, args, lambda f: f.ClearTranscription(), "clear the transcriptions in")


def cmd_folder_classify_audio(resolve, args):
    return _folder_action(resolve, args, lambda f: f.PerformAudioClassification(),
                          "classify the audio of the clips in", AI_HINT)


def cmd_folder_classify_audio_clear(resolve, args):
    return _folder_action(resolve, args, lambda f: f.ClearAudioClassification(), "clear the audio classification in")


def cmd_folder_intellisearch(resolve, args):
    return _folder_action(resolve, args, lambda f: f.AnalyzeForIntellisearch(args.faces, args.better),
                          "run IntelliSearch analysis on", AI_HINT)


def cmd_folder_slate_id(resolve, args):
    color = slate_color(resolve, args.color)
    return _folder_action(resolve, args, lambda f: f.AnalyzeForSlate(color), "run slate analysis on", AI_HINT)


def cmd_folder_deblur(resolve, args):
    folder = get_folder(resolve, args.session, args.folder)
    options = deblur_options(args)
    pairs = folder.RemoveMotionBlur(options) if options else folder.RemoveMotionBlur()
    if not pairs:
        raise ResolveError(f"Resolve did not deblur any clip in folder {folder.GetName()!r}. {AI_HINT}")
    rows = []
    for original, deblurred in pairs:
        rows.append({"original": clip_row(original), "deblurred": clip_row(deblurred)})
    return rows


# ---------------------------------------------------------------------------
# dava clip


def clip_folder(root, clip):
    unique_id = clip.GetUniqueId()
    for path, other in iter_clips(root):
        if other.GetUniqueId() == unique_id:
            return "/" + path
    return None


def cmd_clip_info(resolve, args):
    clip = get_clip(resolve, args.session, args.clip)
    info = describe_object(args.session, clip, "MediaPoolItem")
    properties = clip.GetClipProperty() or {}
    timeline = clip.GetTimeline()
    info.update({
        "media_id": clip.GetMediaId(),
        "folder": clip_folder(media_pool(resolve).GetRootFolder(), clip),
        "properties": {key: properties[key] for key in INFO_PROPERTIES if key in properties},
        "flags": list(clip.GetFlagList() or []),
        "in_out": clip.GetMarkInOut() or {},
        "timeline": describe_object(args.session, timeline, "Timeline") if timeline else None,
    })
    return info


def cmd_clip_rename(resolve, args):
    clip = get_clip(resolve, args.session, args.clip)
    old = clip.GetName()
    if not clip.SetName(args.name):
        raise ResolveError(f"Resolve could not rename clip {old!r} to {args.name!r}.")
    return clip_row(clip, old_name=old)


def cmd_clip_move(resolve, args):
    pool = media_pool(resolve)
    clips = chosen_clips(resolve, args)
    target = get_folder(resolve, args.session, args.to)
    if not pool.MoveClips(clips, target):
        raise ResolveError(f"Resolve could not move {len(clips)} clip(s) into folder {target.GetName()!r}.")
    return {"to": folder_info(resolve, args.session, target)["path"], "clips": [clip_row(clip) for clip in clips]}


def cmd_clip_delete(resolve, args):
    clips = chosen_clips(resolve, args)
    rows = [clip_row(clip) for clip in clips]
    if not media_pool(resolve).DeleteClips(clips):
        raise ResolveError(f"Resolve could not delete clip(s) {', '.join(repr(row['name']) for row in rows)}.")
    return {"deleted": rows}


def cmd_clip_relink(resolve, args):
    clips = chosen_clips(resolve, args)
    folder = existing(args.dir, "dir")
    if not media_pool(resolve).RelinkClips(clips, folder):
        raise ResolveError(f"Resolve could not relink {len(clips)} clip(s) to {folder} "
                           "(are the media files in that folder under their original names?).")
    return [clip_row(clip, folder=folder) for clip in clips]


def cmd_clip_unlink(resolve, args):
    clips = chosen_clips(resolve, args)
    if not media_pool(resolve).UnlinkClips(clips):
        raise ResolveError(f"Resolve could not unlink {len(clips)} clip(s).")
    return [clip_row(clip) for clip in clips]


def cmd_clip_replace(resolve, args):
    clip = get_clip(resolve, args.session, args.clip)
    path = existing(args.path, "file")
    ok = clip.ReplaceClipPreserveSubClip(path) if args.keep_subclip else clip.ReplaceClip(path)
    if not ok:
        raise ResolveError(f"Resolve could not replace the media of {clip.GetName()!r} with {path}.")
    return clip_row(clip, file=path)


def cmd_clip_proxy_link(resolve, args):
    clip = get_clip(resolve, args.session, args.clip)
    path = existing(args.path, "file")
    if not clip.LinkProxyMedia(path):
        raise ResolveError(f"Resolve could not link proxy {path} to {clip.GetName()!r} "
                           "(does the proxy match the clip's duration and frame rate?).")
    return clip_row(clip, proxy=path)


def cmd_clip_proxy_unlink(resolve, args):
    clip = get_clip(resolve, args.session, args.clip)
    if not clip.UnlinkProxyMedia():
        raise ResolveError(f"Resolve could not unlink the proxy of {clip.GetName()!r} (does it have one?).")
    return clip_row(clip)


def cmd_clip_full_res_link(resolve, args):
    clip = get_clip(resolve, args.session, args.clip)
    path = existing(args.path, "path")
    if not clip.LinkFullResolutionMedia(path):
        raise ResolveError(f"Resolve could not link full resolution media {path} to {clip.GetName()!r}.")
    return clip_row(clip, full_resolution=path)


def cmd_clip_in_out(resolve, args):
    clip = get_clip(resolve, args.session, args.clip)
    return clip_row(clip, marks=clip.GetMarkInOut() or {})


def cmd_clip_in_out_set(resolve, args):
    if args.mark_out < args.mark_in:
        raise ResolveError(f"The out frame {args.mark_out} comes before the in frame {args.mark_in}.")
    clip = get_clip(resolve, args.session, args.clip)
    extra = (args.type,) if args.type else ()
    if not clip.SetMarkInOut(args.mark_in, args.mark_out, *extra):
        raise ResolveError(f"Resolve could not set in {args.mark_in} / out {args.mark_out} on {clip.GetName()!r} "
                           "(are the frames inside the clip?).")
    return clip_row(clip, marks=clip.GetMarkInOut() or {})


def cmd_clip_in_out_clear(resolve, args):
    clip = get_clip(resolve, args.session, args.clip)
    extra = (args.type,) if args.type else ()
    if not clip.ClearMarkInOut(*extra):
        raise ResolveError(f"Resolve could not clear the in/out marks of {clip.GetName()!r}.")
    return clip_row(clip, marks=clip.GetMarkInOut() or {})


def cmd_clip_flags(resolve, args):
    clip = get_clip(resolve, args.session, args.clip)
    return clip_row(clip, flags=list(clip.GetFlagList() or []))


def cmd_clip_flag_add(resolve, args):
    clip = get_clip(resolve, args.session, args.clip)
    for number, color in enumerate(args.colors):
        if not clip.AddFlag(color):
            done = f" Added before it: {', '.join(args.colors[:number])}." if number else ""
            raise ResolveError(f"Resolve could not add a {color} flag to {clip.GetName()!r}.{done}")
    return clip_row(clip, flags=list(clip.GetFlagList() or []))


def cmd_clip_flag_clear(resolve, args):
    clip = get_clip(resolve, args.session, args.clip)
    if not clip.ClearFlags(args.color):
        raise ResolveError(f"Resolve could not clear {args.color} flag(s) of {clip.GetName()!r}.")
    return clip_row(clip, flags=list(clip.GetFlagList() or []))


def cmd_clip_transcribe(resolve, args):
    extra = transcribe_args(args)
    return each(chosen_clips(resolve, args), lambda clip: clip.TranscribeAudio(*extra),
                lambda what: f"Resolve could not transcribe the audio of {what}. {AI_HINT}")


def cmd_clip_transcript(resolve, args):
    clip = get_clip(resolve, args.session, args.clip)
    transcription = clip.GetTranscription(True) if args.nested_clip else clip.GetTranscription()
    if not transcription:
        raise ResolveError(f"{clip.GetName()!r} has no transcription; run 'dava clip transcribe' on it first.")
    if args.text:
        lines = []
        for segment in transcription.get("segments") or []:
            speaker = segment.get("speaker")
            lines.append(f"{speaker}: {segment.get('text', '')}" if speaker else segment.get("text", ""))
        return "\n".join(lines)
    return clip_row(clip, **transcription)


def cmd_clip_transcript_clear(resolve, args):
    extra = (True,) if args.nested_clip else ()
    return each(chosen_clips(resolve, args), lambda clip: clip.ClearTranscription(*extra),
                lambda what: f"Resolve could not clear the transcription of {what}.")


def cmd_clip_classify_audio(resolve, args):
    return each(chosen_clips(resolve, args), lambda clip: clip.PerformAudioClassification(),
                lambda what: f"Resolve could not classify the audio of {what}. {AI_HINT}")


def cmd_clip_classify_audio_clear(resolve, args):
    return each(chosen_clips(resolve, args), lambda clip: clip.ClearAudioClassification(),
                lambda what: f"Resolve could not clear the audio classification of {what}.")


def cmd_clip_intellisearch(resolve, args):
    return each(chosen_clips(resolve, args), lambda clip: clip.AnalyzeForIntellisearch(args.faces, args.better),
                lambda what: f"Resolve could not run IntelliSearch analysis on {what}. {AI_HINT}")


def cmd_clip_slate_id(resolve, args):
    color = slate_color(resolve, args.color)
    return each(chosen_clips(resolve, args), lambda clip: clip.AnalyzeForSlate(color),
                lambda what: f"Resolve could not run slate analysis on {what}. {AI_HINT}")


def cmd_clip_deblur(resolve, args):
    clip = get_clip(resolve, args.session, args.clip)
    created = clip.RemoveMotionBlur(deblur_options(args))
    if not created:
        raise ResolveError(f"Resolve could not remove motion blur from {clip.GetName()!r}. {AI_HINT}")
    info = describe_object(args.session, created, "MediaPoolItem")
    info["original"] = clip_row(clip)
    return info


def cmd_clip_monitor_growing(resolve, args):
    clip = get_clip(resolve, args.session, args.clip)
    if not clip.MonitorGrowingFile():
        raise ResolveError(f"Resolve could not monitor {clip.GetName()!r} as a growing file.")
    return clip_row(clip, monitoring=True)


def cmd_clip_stereo(resolve, args):
    left, right = get_clips(resolve, args.session, [args.left, args.right])
    if left.GetUniqueId() == right.GetUniqueId():
        raise ResolveError("The left and right eye must be two different clips.")
    stereo = media_pool(resolve).CreateStereoClip(left, right)
    if not stereo:
        raise ResolveError(f"Resolve could not make a stereo clip from {left.GetName()!r} and {right.GetName()!r} "
                           "(do both eyes have the same duration and frame rate?).")
    info = describe_object(args.session, stereo, "MediaPoolItem")
    info.update({"left": clip_row(left), "right": clip_row(right)})
    return info


def cmd_clip_multicam(resolve, args):
    clips = chosen_clips(resolve, args)
    options = {key: value for key, value in (
        ("name", args.name), ("startTimecode", args.start_timecode), ("frameRate", args.frame_rate)) if value is not None}
    for key, value, names in (("angleSyncMode", args.sync, ANGLE_SYNC), ("multicamAudioMode", args.audio, MULTICAM_AUDIO),
                              ("angleNameMode", args.angle_names, ANGLE_NAMES),
                              ("detectSameCameraClipsMode", args.detect, DETECT_MODES)):
        if value is not None:
            options[key] = constant(resolve, names[value])
    if args.channel is not None:
        options["channelConfig"] = channel_value(resolve, args.channel)
    if args.split_at_gaps:
        options["splitAtGaps"] = True
    if args.full_extents:
        options["useFullClipExtents"] = True
    if args.no_source_bin:
        options["createBinForSourceClips"] = False
    created = media_pool(resolve).CreateMulticamClip(clips, options)
    if not created:
        raise ResolveError(f"Resolve could not make a multicam clip from {len(clips)} clip(s) "
                           "(timecode sync needs overlapping timecode; try --sync audio or --sync in).")
    return [clip_row(clip) for clip in created]


def cmd_clip_sync_audio(resolve, args):
    clips = chosen_clips(resolve, args)
    if len(clips) < 2:
        raise ResolveError("Audio sync needs at least one video clip and one audio clip.")
    settings = {}
    if args.mode is not None:
        settings["syncMode"] = constant(resolve, SYNC_MODES[args.mode])
    if args.channel is not None:
        settings["channelNumber"] = channel_value(resolve, args.channel)
    if args.keep_embedded_audio:
        settings["retainEmbeddedAudio"] = True
    if args.keep_video_metadata:
        settings["retainVideoMetadata"] = True
    if not media_pool(resolve).AutoSyncAudio(clips, settings):
        raise ResolveError(f"Resolve could not sync audio for {len(clips)} clip(s) (the list needs at least one "
                           "video and one audio clip; without matching timecode try --mode waveform).")
    return [clip_row(clip) for clip in clips]


def cmd_clip_append(resolve, args):
    if args.at is not None and args.at < 0:
        raise ResolveError(f"--at is a frame offset from the timeline start, 0 or more; got {args.at}.")
    if args.at is not None and len(args.clips) != 1:
        raise ResolveError("--at places one clip; give one CLIP per call (without --at, clips are appended one "
                           "after another at the end of the track).")
    project = current_project(resolve)
    timeline = find_timeline(project, args.timeline)
    infos = clip_infos(resolve, args.session, args.clips)  # before switching timelines, so a bad CLIP changes nothing
    if args.timeline is not None and not project.SetCurrentTimeline(timeline):
        raise ResolveError(f"Could not make {timeline.GetName()!r} the current timeline.")
    for info in infos:
        if args.track is not None:
            info["trackIndex"] = args.track
        if args.video_only or args.audio_only:
            info["mediaType"] = 1 if args.video_only else 2
        if args.at is not None:
            info["recordFrame"] = timeline.GetStartFrame() + args.at
    items = project.GetMediaPool().AppendToTimeline(infos)
    if not items:
        raise ResolveError(f"Resolve could not append the clip(s) to {timeline.GetName()!r} (does track "
                           f"{args.track or 1} exist? is each IN:OUT range inside its clip?).")
    return [{"ref": f"item#{item.GetUniqueId()}", "name": item.GetName(), "start": item.GetStart(),
             "end": item.GetEnd()} for item in items]


def cmd_clip_new_timeline(resolve, args):
    infos = clip_infos(resolve, args.session, args.clips)
    timeline = media_pool(resolve).CreateTimelineFromClips(args.name, infos)
    if not timeline:
        raise ResolveError(f"Resolve could not create timeline {args.name!r} (the name may be taken; see "
                           "'dava timeline list').")
    info = describe_object(args.session, timeline, "Timeline")
    info["clips"] = len(infos)
    return info


def cmd_clip_mattes(resolve, args):
    clip = get_clip(resolve, args.session, args.clip)
    return list(media_pool(resolve).GetClipMatteList(clip) or [])


def cmd_clip_matte_add(resolve, args):
    clip = get_clip(resolve, args.session, args.clip)
    paths = [existing(path, "file") for path in args.paths]
    extra = (args.eye,) if args.eye else ()
    if not resolve.GetMediaStorage().AddClipMattesToMediaPool(clip, paths, *extra):
        hint = "" if args.eye else " (--eye left|right is needed for stereo clips)"
        raise ResolveError(f"Resolve could not add {len(paths)} matte(s) to {clip.GetName()!r}{hint}.")
    return list(media_pool(resolve).GetClipMatteList(clip) or [])


def cmd_clip_matte_delete(resolve, args):
    clip = get_clip(resolve, args.session, args.clip)
    pool = media_pool(resolve)
    mattes = list(pool.GetClipMatteList(clip) or [])
    paths = []
    for path in args.paths:
        path = path if path in mattes else local_path(path)
        if path not in mattes:
            raise ResolveError(f"{path} is not a matte of {clip.GetName()!r}. Its mattes: {', '.join(mattes) or 'none'}.")
        paths.append(path)
    if not pool.DeleteClipMattes(clip, paths):
        raise ResolveError(f"Resolve could not delete {len(paths)} matte(s) of {clip.GetName()!r}.")
    return list(pool.GetClipMatteList(clip) or [])


def cmd_clip_selected(resolve, args):
    return [clip_row(clip) for clip in media_pool(resolve).GetSelectedClips() or []]


def cmd_clip_select(resolve, args):
    clip = get_clip(resolve, args.session, args.clip)
    if not media_pool(resolve).SetSelectedClip(clip):
        raise ResolveError(f"Resolve could not select {clip.GetName()!r} in the media pool.")
    return clip_row(clip, selected=True)


# ---------------------------------------------------------------------------
# dava storage


def cmd_storage_volumes(resolve, args):
    volumes = resolve.GetMediaStorage().GetMountedVolumeList()
    if not volumes:
        raise ResolveError("Resolve reports no mounted volumes in Media Storage.")
    return list(volumes)


def cmd_storage_ls(resolve, args):
    path = existing(args.path, "dir")
    storage = resolve.GetMediaStorage()
    return {"path": path, "folders": list(storage.GetSubFolderList(path) or []),
            "files": list(storage.GetFileList(path) or [])}


def cmd_storage_reveal(resolve, args):
    path = existing(args.path, "path")
    if not resolve.GetMediaStorage().RevealInStorage(path):
        raise ResolveError(f"Resolve could not reveal {path} in Media Storage (is its volume listed in "
                           "'dava storage volumes'?).")
    return f"Revealed {path} in Media Storage"


def cmd_storage_add(resolve, args):
    items = []
    for text in args.paths:
        # A path that exists as written is never split, even if it ends in @N:M.
        target, start, end = (text, None, None) if os.path.exists(local_path(text)) else split_range(text)
        item = {"media": local_path(target)}
        if start is not None:
            item.update(startFrame=start, endFrame=end)
        items.append(item)
    clips = resolve.GetMediaStorage().AddItemListToMediaPool(items)
    if not clips:
        raise ResolveError("Resolve added nothing to the media pool; check the paths with 'dava storage ls FOLDER' "
                           "(image sequences are listed there as single entries).")
    return [clip_row(clip) for clip in clips]


def clone_settings(resolve, args):
    settings = {}
    if args.preserve_folder_name or args.no_preserve_folder_name:
        settings["PreserveFolderName"] = bool(args.preserve_folder_name)
    if args.checksum is not None:
        settings["ChecksumType"] = constant(resolve, CHECKSUMS[args.checksum])
    return settings


def clone_status(storage):
    status = storage.GetCloneStatus()
    if not status:
        raise ResolveError("Resolve did not report a clone status.")
    return dict(status)


def cmd_storage_clone(resolve, args):
    storage = resolve.GetMediaStorage()
    source = existing(args.source, "dir")
    targets = [local_path(target) for target in args.targets]
    settings = clone_settings(resolve, args)
    if settings and not storage.SetCloneToolSettings(settings):
        raise ResolveError(f"Resolve rejected the clone settings {settings}.")
    if not storage.StartCloneMedia(source, targets):
        raise ResolveError(f"Resolve did not start cloning {source} (is a clone job running? "
                           "see 'dava storage clone-status').")
    status = clone_status(storage)
    started = time.monotonic()
    while args.wait and status.get("JobStatus") == "Cloning":
        if args.timeout is not None and time.monotonic() - started > args.timeout:
            raise ResolveError(f"Cloning is still running after {args.timeout:g} s "
                               f"({status.get('CompletionPercentage', 0):g}%); follow it with 'dava storage "
                               "clone-status' or stop it with 'dava storage clone-stop'.")
        time.sleep(args.interval)
        status = clone_status(storage)
    if args.wait and status.get("JobStatus") in ("Failed", "Cancelled"):
        raise ResolveError(f"Cloning {source} ended as {status['JobStatus']}: {status.get('Error') or 'no reason given'}.")
    return {"source": source, "targets": targets, **status}


def cmd_storage_clone_status(resolve, args):
    return clone_status(resolve.GetMediaStorage())


def cmd_storage_clone_stop(resolve, args):
    if not resolve.GetMediaStorage().StopCloneMedia():
        raise ResolveError("No clone job is in progress.")
    return "Stopped the clone job"


def cmd_storage_clone_settings(resolve, args):
    settings = clone_settings(resolve, args)
    if not settings:
        raise ResolveError("Give --preserve-folder-name, --no-preserve-folder-name and/or --checksum TYPE.")
    if not resolve.GetMediaStorage().SetCloneToolSettings(settings):
        raise ResolveError(f"Resolve rejected the clone settings {settings}.")
    return {"PreserveFolderName": settings.get("PreserveFolderName"), "checksum": args.checksum}


# ---------------------------------------------------------------------------
# dava media / dava meta (new actions)


def cmd_media_info(resolve, args):
    pool = media_pool(resolve)
    root = pool.GetRootFolder()
    current = pool.GetCurrentFolder()
    paths = folder_paths(root)
    return {"id": pool.GetUniqueId(), "root": folder_row(root, paths),
            "current_folder": folder_row(current, paths) if current else None,
            "selected": [clip_row(clip) for clip in pool.GetSelectedClips() or []]}


def cmd_media_import_sequence(resolve, args):
    if args.start is not None and args.end is not None and args.end < args.start:
        raise ResolveError(f"--end {args.end} comes before --start {args.start}.")
    infos = []
    for pattern in args.patterns:
        path = local_path(pattern)
        if not os.path.isdir(os.path.dirname(path)):
            raise ResolveError(f"Folder not found: {os.path.dirname(path)}")
        info = {"FilePath": path}
        if args.start is not None:
            info["StartIndex"] = args.start
        if args.end is not None:
            info["EndIndex"] = args.end
        infos.append(info)
    clips = media_pool(resolve).ImportMedia(infos)
    if not clips:
        raise ResolveError("Resolve imported nothing; check the frame-number pattern of the file name and that "
                           "frames --start to --end exist.")
    return [clip_row(clip) for clip in clips]


def cmd_media_import_timeline(resolve, args):
    path = existing(args.file, "file")
    options = {}
    if args.name is not None:
        options["timelineName"] = args.name
    if args.no_source_clips:
        options["importSourceClips"] = False
    if args.source_clips_path is not None:
        options["sourceClipsPath"] = existing(args.source_clips_path, "dir")
    if args.source_folder:
        options["sourceClipsFolders"] = [get_folder(resolve, args.session, text) for text in args.source_folder]
    if args.interlace:
        options["interlaceProcessing"] = True
    pool = media_pool(resolve)
    timeline = pool.ImportTimelineFromFile(path, options) if options else pool.ImportTimelineFromFile(path)
    if not timeline:
        raise ResolveError(f"Resolve could not import a timeline from {path} (formats: AAF, EDL, XML, FCPXML, DRT, "
                           "ADL, OTIO; the timeline name may be taken).")
    return describe_object(args.session, timeline, "Timeline")


def cmd_media_delete_timeline(resolve, args):
    project = current_project(resolve)
    chosen = [find_timeline(project, name) for name in args.names]
    if not project.GetMediaPool().DeleteTimelines(chosen):
        raise ResolveError(f"Resolve could not delete timeline(s) {', '.join(map(repr, args.names))}.")
    return {"deleted": list(args.names)}


def cmd_media_timeline_mattes(resolve, args):
    folder = get_folder(resolve, args.session, args.folder)
    return [clip_row(matte) for matte in media_pool(resolve).GetTimelineMatteList(folder) or []]


def cmd_media_timeline_matte_add(resolve, args):
    paths = [existing(path, "file") for path in args.paths]
    created = resolve.GetMediaStorage().AddTimelineMattesToMediaPool(paths)
    if not created:
        raise ResolveError(f"Resolve could not add {len(paths)} file(s) as timeline mattes.")
    return [clip_row(matte) for matte in created]


def cmd_meta_export(resolve, args):
    path = output_path(args.file)
    pool = media_pool(resolve)
    if args.clips or args.selected:
        clips = chosen_clips(resolve, args)
        ok = pool.ExportMetadata(path, clips)
    else:
        clips = None
        ok = pool.ExportMetadata(path)
    if not ok:
        raise ResolveError(f"Resolve could not export clip metadata to {path}.")
    return {"file": path, "clips": "all" if clips is None else len(clips)}


# ---------------------------------------------------------------------------
# Parsers


def _folder_arg(p, default="the current folder"):
    p.add_argument("folder", nargs="?", metavar="FOLDER", help=f"{FOLDER_HELP} (default: {default})")


def _clips_arg(p):
    p.add_argument("clips", nargs="*", metavar="CLIP", help=CLIP_HELP)
    p.add_argument("--selected", action="store_true", help="also the clips selected in the media pool")


def _transcribe_options(p):
    group = p.add_mutually_exclusive_group()
    group.add_argument("--speakers", action="store_true", help="detect speakers")
    group.add_argument("--no-speakers", action="store_true", help="do not detect speakers")
    p.add_argument("--nested-clip", action="store_true", help="transcribe as a nested clip (transcribeAsNestedClip)")


def _intellisearch_options(p):
    p.add_argument("--faces", action="store_true", help="also identify faces")
    p.add_argument("--better", action="store_true",
                   help="use the Better model (Extras: AI IntelliSearch - Better) instead of Faster")


def _slate_options(p):
    p.add_argument("-c", "--color", required=True, choices=MARKER_COLORS,
                   help="color of the markers placed on detected slates")


def _deblur_options(p):
    p.add_argument("--file-name", metavar="PATTERN", help="output file name pattern (default: source name + ' Deblur')")
    p.add_argument("--format", help="output container, e.g. mov or mp4")
    p.add_argument("--codec", help="output codec, e.g. H264, H265, ProRes422")
    p.add_argument("--profile", help="encoding profile for H.264/H.265, e.g. Main10")
    p.add_argument("--encoder", choices=["Native", "MainConcept"], help="H.265 encoder")
    p.add_argument("--no-extreme", action="store_true", help="do not use extreme deblur mode (default: on)")
    p.add_argument("--whole-clip", action="store_true", help="process the whole clip, not only its in/out range")
    p.add_argument("--source-res", action="store_true", help="render at source resolution")
    p.add_argument("--more-gpu-memory", action="store_true", help="let the deblur use more GPU memory")


def _clone_options(p):
    # Resolve keeps Clone Tool settings between jobs: an option not given keeps its last value.
    group = p.add_mutually_exclusive_group()
    group.add_argument("--preserve-folder-name", action="store_true",
                       help="keep the source folder name in each target (default: the last setting)")
    group.add_argument("--no-preserve-folder-name", action="store_true",
                       help="copy the contents only (Resolve's initial setting)")
    p.add_argument("--checksum", choices=list(CHECKSUMS),
                   help="checksum used to verify copies (default: the last setting; Resolve's initial one is md5)")


def _register_folder(registry):
    help_text = "media pool folders: tree, create, move, delete, .drb export/import, transcription and AI analysis"
    p = registry.action("folder", "tree", "the folder tree with clip counts (as JSON)", group_help=help_text)
    _folder_arg(p, default="the root folder")
    p.add_argument("--clips", action="store_true", help="also list the clips of every folder")
    p.add_argument("-d", "--depth", type=int, help="levels of subfolders to expand (default: all)")
    p.set_defaults(func=cmd_folder_tree, read_only=True, always_json=True)

    p = registry.action("folder", "list", "subfolders and clips of one folder, with refs")
    _folder_arg(p)
    p.set_defaults(func=cmd_folder_list, read_only=True)

    p = registry.action("folder", "current", "the current media pool folder (where imports go)")
    p.set_defaults(func=cmd_folder_current, read_only=True)

    p = registry.action("folder", "use", "make a folder the current one (imports and new timelines go there)")
    p.add_argument("folder", metavar="FOLDER", help=FOLDER_HELP)
    p.set_defaults(func=cmd_folder_use, read_only=False)

    p = registry.action("folder", "create", "add a subfolder")
    p.add_argument("name", help="name of the new folder")
    p.add_argument("-p", "--parent", metavar="FOLDER", help=f"parent folder (default: the current folder); {FOLDER_HELP}")
    p.add_argument("--use", action="store_true", help="make the new folder the current one")
    p.set_defaults(func=cmd_folder_create, read_only=False)

    p = registry.action("folder", "delete", "delete folders with their clips")
    p.add_argument("folders", nargs="+", metavar="FOLDER", help=FOLDER_HELP)
    p.set_defaults(func=cmd_folder_delete, read_only=False)

    p = registry.action("folder", "move", "move folders into another folder")
    p.add_argument("folders", nargs="+", metavar="FOLDER", help=FOLDER_HELP)
    p.add_argument("--to", required=True, metavar="FOLDER", help="target folder")
    p.set_defaults(func=cmd_folder_move, read_only=False)

    p = registry.action("folder", "export", "export a folder (clips, bins, timelines) as a .drb file")
    p.add_argument("folder", metavar="FOLDER", help=FOLDER_HELP)
    p.add_argument("file", help="output .drb file")
    p.set_defaults(func=cmd_folder_export, read_only=False)

    p = registry.action("folder", "import", "import a folder from a .drb file (see 'dava folder export')")
    p.add_argument("file", help=".drb file")
    p.add_argument("--source-clips-path", metavar="DIR", help="folder to search for the media of the clips")
    p.set_defaults(func=cmd_folder_import, read_only=False)

    p = registry.action("folder", "refresh", "update the folders from the server (collaboration mode)")
    p.set_defaults(func=cmd_folder_refresh, read_only=False)

    p = registry.action("folder", "stale", "which folders are stale (collaboration mode); refresh with "
                        "'dava folder refresh'")
    _folder_arg(p, default="the root folder")
    p.set_defaults(func=cmd_folder_stale, read_only=True)

    p = registry.action("folder", "transcribe", "transcribe the audio of every clip in a folder and its subfolders "
                        "(Studio); read it with 'dava clip transcript'")
    _folder_arg(p)
    _transcribe_options(p)
    p.set_defaults(func=cmd_folder_transcribe, read_only=False)

    p = registry.action("folder", "transcript-clear", "clear the transcriptions of the clips in a folder and below")
    _folder_arg(p)
    p.set_defaults(func=cmd_folder_transcript_clear, read_only=False)

    p = registry.action("folder", "classify-audio", "classify the audio of the clips in a folder and below into "
                        "categories (Studio)")
    _folder_arg(p)
    p.set_defaults(func=cmd_folder_classify_audio, read_only=False)

    p = registry.action("folder", "classify-audio-clear", "clear the audio classification of the clips in a folder "
                        "and below")
    _folder_arg(p)
    p.set_defaults(func=cmd_folder_classify_audio_clear, read_only=False)

    p = registry.action("folder", "intellisearch", "analyze the clips of a folder for IntelliSearch (Studio, "
                        "Extras: AI IntelliSearch)")
    _folder_arg(p)
    _intellisearch_options(p)
    p.set_defaults(func=cmd_folder_intellisearch, read_only=False)

    p = registry.action("folder", "slate-id", "analyze the clips of a folder for slates and mark them (Studio, "
                        "Extras: AI Slate ID)")
    _folder_arg(p)
    _slate_options(p)
    p.set_defaults(func=cmd_folder_slate_id, read_only=False)

    p = registry.action("folder", "deblur", "remove motion blur from the clips of a folder into new clips (Studio)")
    _folder_arg(p)
    _deblur_options(p)
    p.set_defaults(func=cmd_folder_deblur, read_only=False, always_json=True)


def _register_clip(registry):
    help_text = "media pool clips: info, move, relink, proxies, in/out marks, flags, transcripts, multicam, append"
    p = registry.action("clip", "info", "name, id, folder, key properties, flags and in/out marks of a clip",
                        group_help=help_text)
    p.add_argument("clip", metavar="CLIP", help=CLIP_HELP)
    p.set_defaults(func=cmd_clip_info, read_only=True, always_json=True)

    p = registry.action("clip", "rename", "rename a clip")
    p.add_argument("clip", metavar="CLIP", help=CLIP_HELP)
    p.add_argument("name", help="new name")
    p.set_defaults(func=cmd_clip_rename, read_only=False)

    p = registry.action("clip", "move", "move clips into a folder")
    _clips_arg(p)
    p.add_argument("--to", required=True, metavar="FOLDER", help=f"target folder; {FOLDER_HELP}")
    p.set_defaults(func=cmd_clip_move, read_only=False, always_json=True)

    p = registry.action("clip", "delete", "delete clips (or timeline mattes) from the media pool")
    _clips_arg(p)
    p.set_defaults(func=cmd_clip_delete, read_only=False, always_json=True)

    p = registry.action("clip", "relink", "relink clips to the media files in a folder")
    _clips_arg(p)
    p.add_argument("-d", "--dir", required=True, help="folder that holds the media files")
    p.set_defaults(func=cmd_clip_relink, read_only=False)

    p = registry.action("clip", "unlink", "unlink clips from their media files (they go offline)")
    _clips_arg(p)
    p.set_defaults(func=cmd_clip_unlink, read_only=False)

    p = registry.action("clip", "replace", "replace the media file (and metadata) of a clip")
    p.add_argument("clip", metavar="CLIP", help=CLIP_HELP)
    p.add_argument("path", help="new media file")
    p.add_argument("--keep-subclip", action="store_true", help="keep the original subclip extents")
    p.set_defaults(func=cmd_clip_replace, read_only=False)

    p = registry.action("clip", "proxy-link", "link a proxy media file to a clip")
    p.add_argument("clip", metavar="CLIP", help=CLIP_HELP)
    p.add_argument("path", help="proxy media file")
    p.set_defaults(func=cmd_clip_proxy_link, read_only=False)

    p = registry.action("clip", "proxy-unlink", "unlink the proxy media of a clip")
    p.add_argument("clip", metavar="CLIP", help=CLIP_HELP)
    p.set_defaults(func=cmd_clip_proxy_unlink, read_only=False)

    p = registry.action("clip", "full-res-link", "link a proxy-only clip to its full resolution media")
    p.add_argument("clip", metavar="CLIP", help=CLIP_HELP)
    p.add_argument("path", help="full resolution media file or folder")
    p.set_defaults(func=cmd_clip_full_res_link, read_only=False)

    p = registry.action("clip", "in-out", "the in/out marks of a clip (video and audio)")
    p.add_argument("clip", metavar="CLIP", help=CLIP_HELP)
    p.set_defaults(func=cmd_clip_in_out, read_only=True, always_json=True)

    p = registry.action("clip", "in-out-set", "set the in/out marks of a clip")
    p.add_argument("clip", metavar="CLIP", help=CLIP_HELP)
    p.add_argument("mark_in", type=int, metavar="IN", help="in frame")
    p.add_argument("mark_out", type=int, metavar="OUT", help="out frame")
    p.add_argument("--type", choices=MARK_TYPES, help="which marks (Resolve default: all)")
    p.set_defaults(func=cmd_clip_in_out_set, read_only=False, always_json=True)

    p = registry.action("clip", "in-out-clear", "clear the in/out marks of a clip")
    p.add_argument("clip", metavar="CLIP", help=CLIP_HELP)
    p.add_argument("--type", choices=MARK_TYPES, help="which marks (Resolve default: all)")
    p.set_defaults(func=cmd_clip_in_out_clear, read_only=False, always_json=True)

    p = registry.action("clip", "flags", "the flag colors of a clip")
    p.add_argument("clip", metavar="CLIP", help=CLIP_HELP)
    p.set_defaults(func=cmd_clip_flags, read_only=True)

    p = registry.action("clip", "flag-add", "add flags to a clip")
    p.add_argument("clip", metavar="CLIP", help=CLIP_HELP)
    p.add_argument("colors", nargs="+", metavar="COLOR", choices=FLAG_COLORS, help="flag color")
    p.set_defaults(func=cmd_clip_flag_add, read_only=False)

    p = registry.action("clip", "flag-clear", "clear flags of one color, or all flags, of a clip")
    p.add_argument("clip", metavar="CLIP", help=CLIP_HELP)
    p.add_argument("color", nargs="?", default="All", choices=FLAG_COLORS + ("All",), help="flag color (default: All)")
    p.set_defaults(func=cmd_clip_flag_clear, read_only=False)

    p = registry.action("clip", "transcribe", "transcribe the audio of clips (Studio); read it with "
                        "'dava clip transcript'")
    _clips_arg(p)
    _transcribe_options(p)
    p.set_defaults(func=cmd_clip_transcribe, read_only=False)

    p = registry.action("clip", "transcript", "the transcription of a clip: language and segments with timecodes, "
                        "speakers and words")
    p.add_argument("clip", metavar="CLIP", help=CLIP_HELP)
    p.add_argument("--nested-clip", action="store_true", help="the nested clip transcription (useNestedClipTranscription)")
    p.add_argument("--text", action="store_true", help="plain text, one segment per line, 'SPEAKER: ' when known")
    p.set_defaults(func=cmd_clip_transcript, read_only=True)

    p = registry.action("clip", "transcript-clear", "clear the transcription of clips")
    _clips_arg(p)
    p.add_argument("--nested-clip", action="store_true", help="also clear the nested clip transcription")
    p.set_defaults(func=cmd_clip_transcript_clear, read_only=False)

    p = registry.action("clip", "classify-audio", "classify the audio of clips into categories (Studio)")
    _clips_arg(p)
    p.set_defaults(func=cmd_clip_classify_audio, read_only=False)

    p = registry.action("clip", "classify-audio-clear", "clear the audio classification of clips")
    _clips_arg(p)
    p.set_defaults(func=cmd_clip_classify_audio_clear, read_only=False)

    p = registry.action("clip", "intellisearch", "analyze clips for IntelliSearch (Studio, Extras: AI IntelliSearch)")
    _clips_arg(p)
    _intellisearch_options(p)
    p.set_defaults(func=cmd_clip_intellisearch, read_only=False)

    p = registry.action("clip", "slate-id", "analyze clips for slates and mark them (Studio, Extras: AI Slate ID)")
    _clips_arg(p)
    _slate_options(p)
    p.set_defaults(func=cmd_clip_slate_id, read_only=False)

    p = registry.action("clip", "deblur", "remove motion blur from a clip into a new clip (Studio)")
    p.add_argument("clip", metavar="CLIP", help=CLIP_HELP)
    _deblur_options(p)
    p.set_defaults(func=cmd_clip_deblur, read_only=False, always_json=True)

    p = registry.action("clip", "monitor-growing", "keep updating a clip while its file is still being written")
    p.add_argument("clip", metavar="CLIP", help=CLIP_HELP)
    p.set_defaults(func=cmd_clip_monitor_growing, read_only=False)

    p = registry.action("clip", "stereo", "make a stereoscopic 3D clip from a left and a right eye clip (it replaces "
                        "both in the media pool)")
    p.add_argument("left", metavar="LEFT", help=f"left eye; {CLIP_HELP}")
    p.add_argument("right", metavar="RIGHT", help="right eye clip")
    p.set_defaults(func=cmd_clip_stereo, read_only=False, always_json=True)

    p = registry.action("clip", "multicam", "make a multicam clip from angle clips")
    _clips_arg(p)
    p.add_argument("-n", "--name", help="multicam clip name (default: from the first clip)")
    p.add_argument("--start-timecode", metavar="TC", help="start timecode (default: 01:00:00:00)")
    p.add_argument("--frame-rate", type=float, help="frame rate, e.g. 23.976 (default: the timeline frame rate)")
    p.add_argument("--sync", choices=list(ANGLE_SYNC), help="how angles are synced (Resolve default: timecode)")
    p.add_argument("--channel", type=sync_channel, metavar="N|auto|mix", help="audio channel to sync on (--sync audio)")
    p.add_argument("--audio", choices=list(MULTICAM_AUDIO), help="which audio the multicam clip uses (default: source)")
    p.add_argument("--angle-names", choices=list(ANGLE_NAMES), help="how angles are named (default: sequential)")
    p.add_argument("--detect", choices=list(DETECT_MODES), help="group clips of the same camera by (default: none)")
    p.add_argument("--split-at-gaps", action="store_true", help="split at gaps (--sync audio)")
    p.add_argument("--full-extents", action="store_true", help="use the full clip extents")
    p.add_argument("--no-source-bin", action="store_true", help="do not put the source clips in a new bin")
    p.set_defaults(func=cmd_clip_multicam, read_only=False)

    p = registry.action("clip", "sync-audio", "sync separately recorded audio clips to video clips")
    _clips_arg(p)
    p.add_argument("--mode", choices=list(SYNC_MODES), help="sync by (Resolve default: timecode)")
    p.add_argument("--channel", type=sync_channel, metavar="N|auto|mix", help="audio channel for --mode waveform")
    p.add_argument("--keep-embedded-audio", action="store_true", help="keep the camera audio as well")
    p.add_argument("--keep-video-metadata", action="store_true", help="keep the video clip metadata")
    p.set_defaults(func=cmd_clip_sync_audio, read_only=False)

    p = registry.action("clip", "append", "append clips or source ranges to the end of a timeline track, or at a frame")
    p.add_argument("clips", nargs="+", metavar="CLIP[@IN:OUT]", help=RANGE_HELP)
    p.add_argument("-t", "--timeline", help="make this timeline current first (default: the current timeline)")
    p.add_argument("-T", "--track", type=positive_int, help="track number, which must exist (default: 1)")
    p.add_argument("--at", type=int, metavar="FRAME",
                   help="put the one CLIP at this frame offset from the timeline start instead of at the end")
    kind = p.add_mutually_exclusive_group()
    kind.add_argument("--video-only", action="store_true", help="only the video of the clips")
    kind.add_argument("--audio-only", action="store_true", help="only the audio of the clips")
    p.set_defaults(func=cmd_clip_append, read_only=False)

    p = registry.action("clip", "new-timeline", "create a timeline from clips or source ranges, in order")
    p.add_argument("name", help="new timeline name")
    p.add_argument("clips", nargs="+", metavar="CLIP[@IN:OUT]", help=RANGE_HELP)
    p.set_defaults(func=cmd_clip_new_timeline, read_only=False)

    p = registry.action("clip", "mattes", "the matte files attached to a clip")
    p.add_argument("clip", metavar="CLIP", help=CLIP_HELP)
    p.set_defaults(func=cmd_clip_mattes, read_only=True)

    p = registry.action("clip", "matte-add", "attach media files to a clip as mattes")
    p.add_argument("clip", metavar="CLIP", help=CLIP_HELP)
    p.add_argument("paths", nargs="+", metavar="PATH", help="matte file")
    p.add_argument("--eye", choices=STEREO_EYES, help="eye of a stereo clip the mattes belong to")
    p.set_defaults(func=cmd_clip_matte_add, read_only=False)

    p = registry.action("clip", "matte-delete", "remove mattes from a clip by file path (see 'dava clip mattes')")
    p.add_argument("clip", metavar="CLIP", help=CLIP_HELP)
    p.add_argument("paths", nargs="+", metavar="PATH", help="matte file path")
    p.set_defaults(func=cmd_clip_matte_delete, read_only=False)

    p = registry.action("clip", "selected", "the clips selected in the media pool")
    p.set_defaults(func=cmd_clip_selected, read_only=True)

    p = registry.action("clip", "select", "select a clip in the media pool")
    p.add_argument("clip", metavar="CLIP", help=CLIP_HELP)
    p.set_defaults(func=cmd_clip_select, read_only=False)


def _register_storage(registry):
    help_text = "Media Storage: volumes, browsing, adding files to the media pool, cloning media"
    p = registry.action("storage", "volumes", "mounted volumes shown in Media Storage", group_help=help_text)
    p.set_defaults(func=cmd_storage_volumes, read_only=True)

    p = registry.action("storage", "ls", "subfolders and media files of a folder as Media Storage lists them "
                        "(image sequences are one entry)")
    p.add_argument("path", help="folder")
    p.set_defaults(func=cmd_storage_ls, read_only=True)

    p = registry.action("storage", "reveal", "show a file or folder in the Media Storage panel")
    p.add_argument("path", help="file or folder")
    p.set_defaults(func=cmd_storage_reveal, read_only=False)

    p = registry.action("storage", "add", "add files or folders to the current media pool folder")
    p.add_argument("paths", nargs="+", metavar="PATH[@START:END]",
                   help="file or folder, or an entry from 'dava storage ls'; @START:END adds only those frames")
    p.set_defaults(func=cmd_storage_add, read_only=False)

    p = registry.action("storage", "clone", "copy a camera card or folder to one or more targets with checksums "
                        "(the Clone Tool)")
    p.add_argument("source", help="source folder")
    p.add_argument("targets", nargs="+", metavar="TARGET", help="target folder")
    _clone_options(p)
    p.add_argument("-w", "--wait", action="store_true", help="block until the clone job ends")
    p.add_argument("--interval", type=non_negative_float, default=1.0, help="poll interval in seconds with --wait")
    p.add_argument("--timeout", type=non_negative_float, help="give up waiting after this many seconds")
    p.set_defaults(func=cmd_storage_clone, read_only=False)

    p = registry.action("storage", "clone-status", "status of the current or last clone job")
    p.set_defaults(func=cmd_storage_clone_status, read_only=True)

    p = registry.action("storage", "clone-stop", "stop the running clone job")
    p.set_defaults(func=cmd_storage_clone_stop, read_only=False)

    p = registry.action("storage", "clone-settings", "set the Clone Tool options used by later clone jobs")
    _clone_options(p)
    p.set_defaults(func=cmd_storage_clone_settings, read_only=False)


def _register_media_and_meta(registry):
    p = registry.action("media", "info", "media pool id, root and current folder, selected clips")
    p.set_defaults(func=cmd_media_info, read_only=True, always_json=True)

    p = registry.action(
        "media", "import-sequence", "import image sequences, or frames START to END of them, into the current folder",
        description="Import image sequences into the current media pool folder. PATTERN is a file name with a "
                    "printf frame number, e.g. /renders/shot_%04d.exr for shot_0001.exr, shot_0002.exr, ...")
    p.add_argument("patterns", nargs="+", metavar="PATTERN", help="sequence file name with a frame-number pattern")
    p.add_argument("--start", type=int, metavar="N", help="first frame number to import")
    p.add_argument("--end", type=int, metavar="N", help="last frame number to import")
    p.set_defaults(func=cmd_media_import_sequence, read_only=False)

    p = registry.action("media", "import-timeline", "create a timeline from an AAF, EDL, XML, FCPXML, DRT, ADL or "
                        "OTIO file")
    p.add_argument("file", help="timeline file (.aaf, .edl, .xml, .fcpxml, .drt, .adl or .otio)")
    p.add_argument("-n", "--name", help="timeline name (not for DRT)")
    p.add_argument("--no-source-clips", action="store_true",
                   help="do not import the source clips; link to clips already in the media pool (not for DRT)")
    p.add_argument("--source-clips-path", metavar="DIR", help="folder to search for media that is not accessible")
    p.add_argument("--source-folder", action="append", metavar="FOLDER",
                   help="media pool folder to search for source clips with --no-source-clips (repeatable)")
    p.add_argument("--interlace", action="store_true", help="enable interlace processing (AAF only)")
    p.set_defaults(func=cmd_media_import_timeline, read_only=False)

    p = registry.action("media", "delete-timeline", "delete timelines from the media pool")
    p.add_argument("names", nargs="+", metavar="NAME", help="timeline name")
    p.set_defaults(func=cmd_media_delete_timeline, read_only=False)

    p = registry.action("media", "timeline-mattes", "the timeline mattes in a folder")
    _folder_arg(p)
    p.set_defaults(func=cmd_media_timeline_mattes, read_only=True)

    p = registry.action("media", "timeline-matte-add", "add media files as timeline mattes to the current folder")
    p.add_argument("paths", nargs="+", metavar="PATH", help="matte file")
    p.set_defaults(func=cmd_media_timeline_matte_add, read_only=False)

    p = registry.action("meta", "export", "export the metadata of clips (default: every clip) to a CSV file")
    p.add_argument("file", help="output .csv file")
    _clips_arg(p)
    p.set_defaults(func=cmd_meta_export, read_only=False)


def register(registry):
    _register_folder(registry)
    _register_clip(registry)
    _register_storage(registry)
    _register_media_and_meta(registry)
