"""dava audio / dava fairlight: levels, voice isolation, channel mapping, clip audio properties and speech.

Clips are picked on timeline *audio* tracks with -t TIMELINE -T TRACK -i INDEX (all 1-based).
Channel mappings follow the README section 'Audio Mapping' of the Resolve scripting API.
"""

import difflib
import json
import os
import sys

from ..bridge import describe_object
from ..connect import ResolveError
from ..helpers import current_project, find_clip, find_timeline, parse_assignments, typed_assignments

# NormalizeAudioOptions.setLevelMode: --set-level choice -> resolve.* constant.
SET_LEVEL_MODES = {
    "relative": "NORMALIZE_AUDIO_SET_LEVEL_RELATIVE",
    "independent": "NORMALIZE_AUDIO_SET_LEVEL_INDEPENDENT",
}

# TimelineItemProperties.AudioDialogueLevelerMode: --leveler choice -> resolve.* constant.
LEVELER_MODES = {
    "wider-dynamics": "DIALOGUE_LEVELER_MODE_ALLOW_WIDER_DYNAMICS",
    "moderate-levels": "DIALOGUE_LEVELER_MODE_OPTIMIZE_MODERATE_LEVELS",
    "more-lift": "DIALOGUE_LEVELER_MODE_MORE_LIFT_FOR_LOW_LEVELS",
    "soft-whispery": "DIALOGUE_LEVELER_MODE_LIFT_SOFT_WHISPERY_SOURCES",
}

# Channel count of every track 'type' an audio mapping accepts (README 'Audio Mapping').
TRACK_FORMATS = {
    "mono": 1, "stereo": 2, "lrc": 3, "lcr": 3, "lrcs": 4, "lcrs": 4, "quad": 4,
    "5.0": 5, "5.0_film": 5, "5.1": 6, "5.1_film": 6, "7.0": 7, "7.0_film": 7, "7.1": 8, "7.1_film": 8,
    **{f"adaptive_{n}": n for n in range(1, 37)},
}
TRACK_FORMAT_HELP = ("mono, stereo, lrc, lcr, lrcs, lcrs, quad, 5.0, 5.0_film, 5.1, 5.1_film, 7.0, 7.0_film, "
                     "7.1, 7.1_film, adaptive_1 ... adaptive_36")
MAPPING_TRACK_KEYS = ("channel_idx", "mute", "type")
MAPPING_EXAMPLE = '{"track_mapping": {"1": {"type": "stereo", "channel_idx": [1, 2], "mute": false}}}'

VOICE_ISOLATION_KEYS = ("isEnabled", "amount")
STUDIO_HINT = "voice isolation and the dialogue leveler may need DaVinci Resolve Studio"

SPEECH_TEXT_LIMIT = 350
CUSTOM_VOICE = "Custom Voice"


# --- lookups ------------------------------------------------------------------------


def constant(resolve, name):
    value = getattr(resolve, name, None)
    if value is None:
        raise ResolveError(f"This Resolve build has no constant {name}.")
    return value


def make_current(project, timeline, args):
    """Make the timeline named with -t current (without -t it already is).

    Keys marked [Active Timeline Only] in the API (voice isolation, dialogue leveler) apply only
    to clips of the current timeline.
    """
    if args.timeline is not None and not project.SetCurrentTimeline(timeline):
        raise ResolveError(f"Could not make {timeline.GetName()!r} the current timeline.")


def open_timeline(resolve, args, activate=False):
    """Return (project, timeline) for -t; activate=True makes a named timeline current first."""
    project = current_project(resolve)
    timeline = find_timeline(project, args.timeline)
    if activate:
        make_current(project, timeline, args)
    return project, timeline


def check_track(timeline, track):
    count = timeline.GetTrackCount("audio") or 0
    if not 1 <= track <= count:
        raise ResolveError(f"Audio track {track} does not exist; {timeline.GetName()!r} has {count} audio track(s).")


def audio_items(timeline, track, index=None, allow_empty=False):
    """Return [(clip index, TimelineItem)] for clip `index` of audio track `track`, or all of its clips."""
    check_track(timeline, track)
    items = timeline.GetItemListInTrack("audio", track) or []
    if not items and not allow_empty:
        raise ResolveError(f"Audio track {track} of {timeline.GetName()!r} has no clips.")
    if index is None:
        return list(enumerate(items, start=1))
    if not 1 <= index <= len(items):
        raise ResolveError(f"Clip index {index} is out of range; audio track {track} has {len(items)} clip(s).")
    return [(index, items[index - 1])]


def clip_row(index, item, **extra):
    return {"index": index, "name": item.GetName(), **extra}


# --- tracks and normalization ---------------------------------------------------------


def cmd_tracks(resolve, args):
    _, timeline = open_timeline(resolve, args)
    rows = []
    for track in range(1, (timeline.GetTrackCount("audio") or 0) + 1):
        state = timeline.GetVoiceIsolationState(track)
        state = state if isinstance(state, dict) else {}  # empty columns: Resolve reported no state
        rows.append({
            "track": track,
            "name": timeline.GetTrackName("audio", track),
            "format": timeline.GetTrackSubType("audio", track),
            "enabled": timeline.GetIsTrackEnabled("audio", track),
            "locked": timeline.GetIsTrackLocked("audio", track),
            "clips": len(timeline.GetItemListInTrack("audio", track) or []),
            "voice_isolation": state.get("isEnabled"),
            "isolation_amount": state.get("amount"),
        })
    return rows


def cmd_add_track(resolve, args):
    _, timeline = open_timeline(resolve, args)
    before = timeline.GetTrackCount("audio") or 0
    added = timeline.AddTrack("audio", args.format) if args.format else timeline.AddTrack("audio")
    if not added:
        formats = sorted({timeline.GetTrackSubType("audio", track) for track in range(1, before + 1)} - {None, ""})
        seen = f" Formats of its existing audio tracks: {', '.join(formats)}." if formats else ""
        hint = "" if args.format else " Give the track format with -f, e.g. -f mono or -f stereo."
        raise ResolveError(f"Resolve could not add an audio track{f' of format {args.format!r}' if args.format else ''} "
                           f"to {timeline.GetName()!r}.{seen}{hint}")
    track = timeline.GetTrackCount("audio") or 0
    if track != before + 1:
        raise ResolveError(f"Resolve reported a new audio track, but {timeline.GetName()!r} still has {track} "
                           f"audio track(s) (had {before}).")
    if args.name is not None and not timeline.SetTrackName("audio", track, args.name):
        raise ResolveError(f"Added audio track {track} but Resolve would not name it {args.name!r}.")
    return {"timeline": timeline.GetName(), "track": track, "name": timeline.GetTrackName("audio", track),
            "format": timeline.GetTrackSubType("audio", track)}


def cmd_modes(resolve, args):
    _, timeline = open_timeline(resolve, args)
    modes = timeline.GetNormalizeAudioModes()
    if not modes:
        raise ResolveError("Resolve returned no audio normalization modes.")
    return list(modes)


def normalize_options(resolve, timeline, args):
    """Build NormalizeAudioOptions from the options; return (options for Resolve, options to show)."""
    options, shown = {}, {}
    if args.mode is not None:
        modes = list(timeline.GetNormalizeAudioModes() or [])
        matches = [mode for mode in modes if mode.lower() == args.mode.lower()]
        if modes and not matches:
            raise ResolveError(f"Unknown normalization mode {args.mode!r}. Modes: {', '.join(modes)}.")
        options["normalizationMode"] = shown["normalizationMode"] = matches[0] if matches else args.mode
    if args.level is not None:
        options["targetLevel"] = shown["targetLevel"] = args.level
    if args.loudness is not None:
        options["targetLoudness"] = shown["targetLoudness"] = args.loudness
    if args.set_level is not None:
        name = SET_LEVEL_MODES[args.set_level]
        options["setLevelMode"] = constant(resolve, name)
        shown["setLevelMode"] = name
    return options, shown


def cmd_normalize(resolve, args):
    _, timeline = open_timeline(resolve, args)
    if args.all_tracks:
        if args.index is not None:
            raise ResolveError("-i/--index picks a clip of one track; it cannot be combined with --all-tracks.")
        picked = [(track, index, item)
                  for track in range(1, (timeline.GetTrackCount("audio") or 0) + 1)
                  for index, item in audio_items(timeline, track, allow_empty=True)]
        if not picked:
            raise ResolveError(f"{timeline.GetName()!r} has no clips on its audio tracks.")
    else:
        track = 1 if args.track is None else args.track
        picked = [(track, index, item) for index, item in audio_items(timeline, track, args.index)]
    options, shown = normalize_options(resolve, timeline, args)
    items = [item for _, _, item in picked]
    done = timeline.NormalizeAudioLevel(items, options) if options else timeline.NormalizeAudioLevel(items)
    if not done:
        raise ResolveError(
            f"Resolve could not normalize {len(items)} audio clip(s) on {timeline.GetName()!r}. Check --mode "
            "against 'dava audio modes'; --level is a dBFS target, --loudness an LKFS target.")
    return {"timeline": timeline.GetName(), "options": shown,
            "clips": [clip_row(index, item, track=track) for track, index, item in picked]}


# --- voice isolation ---------------------------------------------------------------------


def isolation_state(value, what):
    if not isinstance(value, dict) or not value:
        raise ResolveError(f"Resolve returned no voice isolation state for {what} ({STUDIO_HINT}).")
    return value


def cmd_isolation(resolve, args):
    _, timeline = open_timeline(resolve, args)
    check_track(timeline, args.track)
    result = {"timeline": timeline.GetName(), "track": args.track}
    if args.index is None:
        result["state"] = isolation_state(timeline.GetVoiceIsolationState(args.track), f"audio track {args.track}")
    result["clips"] = [
        clip_row(index, item, state=isolation_state(item.GetVoiceIsolationState(), f"clip {index} ({item.GetName()!r})"))
        for index, item in audio_items(timeline, args.track, args.index, allow_empty=True)]
    return result


def merged_state(current, given, what):
    """The current VoiceIsolationState with the given fields replaced, so one flag keeps the other value.

    Resolve takes the whole state {isEnabled, amount}; if it cannot report the current one, both must be given.
    """
    state = {key: current[key] for key in VOICE_ISOLATION_KEYS if isinstance(current, dict) and key in current}
    state.update(given)
    if any(key not in state for key in VOICE_ISOLATION_KEYS):
        raise ResolveError(f"Resolve returned no voice isolation state for {what} to keep the value you did not "
                           f"give; give both --on or --off and --amount N ({STUDIO_HINT}).")
    return state


def cmd_set_isolation(resolve, args):
    given = {}
    if args.on or args.off:
        given["isEnabled"] = bool(args.on)
    if args.amount is not None:
        if not 0 <= args.amount <= 100:
            raise ResolveError(f"--amount must be 0 to 100, got {args.amount}.")
        given["amount"] = args.amount
    if not given:
        raise ResolveError("Nothing to set. Give --on, --off and/or --amount N.")
    project, timeline = open_timeline(resolve, args)
    if args.index is None:
        check_track(timeline, args.track)
        state = merged_state(timeline.GetVoiceIsolationState(args.track), given, f"audio track {args.track}")
        if not timeline.SetVoiceIsolationState(args.track, state):
            raise ResolveError(f"Resolve rejected voice isolation {state} on audio track {args.track} ({STUDIO_HINT}).")
        return {"timeline": timeline.GetName(), "track": args.track, "state": state}
    index, item = audio_items(timeline, args.track, args.index)[0]
    make_current(project, timeline, args)  # after the clip is found, so a bad -i changes nothing
    state = merged_state(item.GetVoiceIsolationState(), given, f"clip {index} ({item.GetName()!r})")
    if not item.SetVoiceIsolationState(state):
        raise ResolveError(f"Resolve rejected voice isolation {state} on clip {index} ({item.GetName()!r}) "
                           f"of audio track {args.track} ({STUDIO_HINT}).")
    return {"timeline": timeline.GetName(), "track": args.track, **clip_row(index, item, state=state)}


# --- channel mapping ------------------------------------------------------------------------


def parse_mapping(value, what):
    if isinstance(value, dict):
        return value
    if not value:
        raise ResolveError(f"Resolve returned no audio mapping for {what}; does it have audio?")
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise ResolveError(f"Resolve returned an audio mapping for {what} that is not JSON: {value!r}") from exc


def load_mapping(source):
    """Parse MAPPING: inline JSON, '-' for standard input, or the path of a JSON file."""
    if source == "-":
        text, where = sys.stdin.read(), "standard input"
    elif source.lstrip().startswith("{"):
        text, where = source, "the inline mapping"
    else:
        path = os.path.abspath(os.path.expanduser(source))
        if not os.path.isfile(path):
            raise ResolveError(f"Audio mapping file not found: {path}. Give inline JSON, a JSON file or '-' for stdin.")
        with open(path, encoding="utf-8") as handle:
            text, where = handle.read(), path
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ResolveError(f"Invalid JSON in {where}: {exc}.") from exc


def check_mapping(mapping, single_track):
    """Validate an audio mapping; Resolve reads only its 'track_mapping' object."""
    tracks = mapping.get("track_mapping") if isinstance(mapping, dict) else None
    if not isinstance(tracks, dict) or not tracks:
        raise ResolveError(f'An audio mapping needs a non-empty "track_mapping" object, e.g. {MAPPING_EXAMPLE}')
    if single_track and len(tracks) != 1:
        raise ResolveError(f"A timeline clip's source channel mapping takes exactly 1 track in track_mapping, "
                           f"got {len(tracks)}.")
    for key, track in tracks.items():
        where = f"track_mapping[{key!r}]"
        if not (key.isdigit() and int(key) >= 1):
            raise ResolveError(f'{where}: track keys are track numbers from 1, e.g. "1".')
        if not isinstance(track, dict):
            raise ResolveError(f"{where}: expected an object with {', '.join(MAPPING_TRACK_KEYS)}.")
        unknown = sorted(set(track) - set(MAPPING_TRACK_KEYS))
        if unknown:
            raise ResolveError(f"{where}: unknown key(s) {', '.join(unknown)}; valid: {', '.join(MAPPING_TRACK_KEYS)}.")
        missing = [key for key in ("type", "channel_idx") if key not in track]
        if missing:
            raise ResolveError(f"{where}: missing {', '.join(missing)}.")
        kind = track["type"]
        channels = TRACK_FORMATS.get(kind.strip().lower().replace(" ", "_")) if isinstance(kind, str) else None
        if channels is None:
            raise ResolveError(f"{where}: unknown type {kind!r}. Valid types: {TRACK_FORMAT_HELP}.")
        indexes = track["channel_idx"]
        if not isinstance(indexes, list) or not all(
                isinstance(c, int) and not isinstance(c, bool) and c >= 0 for c in indexes):
            raise ResolveError(f"{where}: channel_idx must be a list of source channel numbers "
                               "(1-based; 0 = unconnected).")
        if len(indexes) != channels:
            raise ResolveError(f"{where}: type {kind!r} has {channels} channel(s) but channel_idx lists {len(indexes)}.")
        if "mute" in track and not isinstance(track["mute"], bool):
            raise ResolveError(f"{where}: mute must be true or false.")
    return tracks


def check_target(args):
    """--clip (media pool) and -t/-i (timeline clip) pick different objects; refuse a mix."""
    if args.clip is not None and (args.timeline is not None or args.index is not None):
        raise ResolveError("--clip picks a media pool clip; do not combine it with -t/-i (timeline clips).")


def cmd_mapping(resolve, args):
    check_target(args)
    if args.clip is not None:
        clip = find_clip(resolve, args.clip)
        return {"clip": clip.GetName(), "mapping": parse_mapping(clip.GetAudioMapping(), f"clip {args.clip!r}")}
    _, timeline = open_timeline(resolve, args)
    return {"timeline": timeline.GetName(), "track": args.track, "clips": [
        clip_row(index, item, mapping=parse_mapping(item.GetSourceAudioChannelMapping(),
                                                    f"clip {index} of audio track {args.track}"))
        for index, item in audio_items(timeline, args.track, args.index)]}


def cmd_set_mapping(resolve, args):
    check_target(args)
    if args.clip is None and args.index is None:
        raise ResolveError("Give --clip NAME (media pool clip) or -i INDEX (clip on audio track -T of the timeline).")
    mapping = load_mapping(args.mapping)
    tracks = check_mapping(mapping, single_track=args.clip is None)
    text = json.dumps(mapping)
    if args.clip is not None:
        clip = find_clip(resolve, args.clip)
        if not clip.SetAudioMapping(text):
            raise ResolveError(f"Resolve rejected the audio mapping for clip {args.clip!r}. Channel numbers must exist "
                               f"in its source media; see 'dava audio mapping --clip {args.clip!r}'.")
        return {"clip": clip.GetName(), "track_mapping": tracks}
    _, timeline = open_timeline(resolve, args)
    index, item = audio_items(timeline, args.track, args.index)[0]
    if not item.SetSourceAudioChannelMapping(text):
        raise ResolveError(f"Resolve rejected the source channel mapping for clip {index} ({item.GetName()!r}) of "
                           f"audio track {args.track}. See 'dava audio mapping -T {args.track} -i {index}'.")
    return {"timeline": timeline.GetName(), "track": args.track, **clip_row(index, item, track_mapping=tracks)}


# --- clip audio properties ------------------------------------------------------------------


def cmd_props(resolve, args):
    _, timeline = open_timeline(resolve, args)
    # Getters return constants as numbers; show the constant name instead.
    leveler_names = {getattr(resolve, name, None): name for name in LEVELER_MODES.values()}
    leveler_names.pop(None, None)
    clips = []
    for index, item in audio_items(timeline, args.track, args.index):
        props = item.GetProperties()
        if not isinstance(props, dict) or not props:
            raise ResolveError(f"Could not read the properties of clip {index} ({item.GetName()!r}).")
        audio = {key: value for key, value in props.items() if key.startswith("Audio")}
        mode = audio.get("AudioDialogueLevelerMode")
        if mode is not None and mode in leveler_names:
            audio["AudioDialogueLevelerMode"] = leveler_names[mode]
        clips.append(clip_row(index, item, properties=audio))
    return {"timeline": timeline.GetName(), "track": args.track, "clips": clips}


# --volume/--pan/--semitones/--cents: (option value attribute, property key, Inspector section switch).
SHORTCUTS = (("volume", "AudioVolume", "AudioVolumeEnabled"), ("pan", "AudioPan", "AudioPanEnabled"),
             ("semitones", "AudioPitchSemiTones", "AudioPitchEnabled"),
             ("cents", "AudioPitchCents", "AudioPitchEnabled"))


def shortcut_pairs(args, explicit_keys):
    """KEY=VALUE pairs for the shortcut options.

    A value option also turns its Inspector section on (e.g. --semitones sets AudioPitchEnabled=true),
    unless that switch is among explicit_keys, the keys the user gave as KEY=VALUE.
    """
    pairs, switches = [], []
    for attr, key, switch in SHORTCUTS:
        value = getattr(args, attr)
        if value is not None:
            pairs.append(f"{key}={value}")
            if switch not in explicit_keys and switch not in switches:
                switches.append(switch)
    pairs += [f"{switch}=true" for switch in switches]
    if args.leveler == "off":
        pairs.append("AudioDialogueLevelerEnabled=false")
    elif args.leveler is not None:
        pairs += ["AudioDialogueLevelerEnabled=true", f"AudioDialogueLevelerMode={LEVELER_MODES[args.leveler]}"]
    return pairs


def shown_values(resolve, fields, values):
    """The values to report: constants by name (e.g. DIALOGUE_LEVELER_MODE_...) instead of their numbers."""
    shown = {}
    for key, value in values.items():
        kind = fields[key][0]
        if kind[0] == "const":
            names = [name for name in kind[2] if getattr(resolve, name, None) == value]
            value = names[0] if names else value
        shown[key] = value
    return shown


def cmd_set(resolve, args):
    explicit = list(parse_assignments(args.pairs))
    pairs = shortcut_pairs(args, explicit) + list(args.pairs)
    if not pairs:
        raise ResolveError("Nothing to set. Give --volume, --pan, --semitones, --cents, --leveler or "
                           "KEY=VALUE pairs (keys: see 'dava audio props').")
    keys = [next(iter(parse_assignments([pair]))) for pair in pairs]
    twice = sorted({key for key in keys if keys.count(key) > 1})
    if twice:
        raise ResolveError(f"Property given twice (by an option and a KEY=VALUE pair?): {', '.join(twice)}.")
    fields = args.session.spec.typeddicts["TimelineItemProperties"].fields
    audio_keys = [key for key in fields if key.startswith("Audio")]
    other = [key for key in keys if key not in audio_keys]
    if other:
        raise ResolveError(f"Not an audio property: {', '.join(other)}. Audio properties: {', '.join(audio_keys)}.")
    values = typed_assignments(args.session, pairs, "TimelineItemProperties")
    shown = shown_values(resolve, fields, values)
    project, timeline = open_timeline(resolve, args)
    picked = audio_items(timeline, args.track, args.index)
    make_current(project, timeline, args)  # after the clips are found, so a bad -T/-i changes nothing
    done, failed = [], []
    for index, item in picked:
        (done if item.SetProperties(values) else failed).append(clip_row(index, item))
    if failed:
        names = ", ".join(f"{row['index']} ({row['name']!r})" for row in failed)
        applied = f" It was applied to clip(s) {', '.join(str(row['index']) for row in done)}." if done else ""
        raise ResolveError(
            f"Resolve rejected {shown} on clip(s) {names} of audio track {args.track}.{applied} Out-of-range values "
            f"are clipped, so a key was refused: [Active Timeline Only] keys need the clip's timeline to be current "
            f"(-t makes it current), and {STUDIO_HINT}.")
    return {"timeline": timeline.GetName(), "track": args.track, "properties": shown, "clips": done}


# --- insert and speech --------------------------------------------------------------------


def cmd_insert(resolve, args):
    path = os.path.abspath(os.path.expanduser(args.path))
    if not os.path.isfile(path):
        raise ResolveError(f"Audio file not found: {path}")
    if args.offset < 0:
        raise ResolveError(f"--offset must be 0 or more samples, got {args.offset}.")
    if args.duration <= 0:
        raise ResolveError(f"--duration must be more than 0 samples, got {args.duration}.")
    project, timeline = open_timeline(resolve, args, activate=True)
    if args.at and not timeline.SetCurrentTimecode(args.at):
        raise ResolveError(f"Could not move the playhead to {args.at!r}.")
    at = timeline.GetCurrentTimecode()
    if not project.InsertAudioToCurrentTrackAtPlayhead(path, args.offset, args.duration):
        raise ResolveError(
            f"Resolve could not insert {path}. It inserts into the current (selected) audio track, so select one "
            "(e.g. on the Fairlight page); also check that Resolve can read the file and that --offset plus "
            "--duration fit inside it.")
    return {"path": path, "timeline": timeline.GetName(), "at": at,
            "offset_samples": args.offset, "duration_samples": args.duration}


def speech_settings(args):
    """Build SpeechSettings from the options, checking the documented limits first."""
    if not args.text.strip():
        raise ResolveError("The text to speak is empty.")
    if len(args.text) > SPEECH_TEXT_LIMIT:
        raise ResolveError(f"Resolve speaks at most {SPEECH_TEXT_LIMIT} characters per call; the text has "
                           f"{len(args.text)}. Split it over several 'dava audio speech' calls.")
    settings = {"TextInput": args.text}
    voice = args.voice
    if args.voice_file is not None:
        path = os.path.abspath(os.path.expanduser(args.voice_file))
        if not os.path.isfile(path):
            raise ResolveError(f"Custom voice file not found: {path}")
        if voice is None:
            voice = CUSTOM_VOICE
        elif voice != CUSTOM_VOICE:
            raise ResolveError(f"--voice-file needs --voice {CUSTOM_VOICE!r} (or no --voice), got {voice!r}.")
        settings["CustomVoiceFile"] = path
    elif voice == CUSTOM_VOICE:
        raise ResolveError(f"--voice {CUSTOM_VOICE!r} needs --voice-file PATH.")
    if voice is not None:
        settings["VoiceModel"] = voice
    for option, key, value, low, high in (("--speed", "Speed", args.speed, -10.0, 10.0),
                                          ("--variation", "Variation", args.variation, 0.0, 1.0),
                                          ("--pitch", "Pitch", args.pitch, -2.0, 2.0)):
        if value is not None:
            if not low <= value <= high:
                raise ResolveError(f"{option} must be {low} to {high}, got {value}.")
            settings[key] = value
    if args.seed is not None:
        if args.seed < 1:
            raise ResolveError(f"--seed must be 1 or more, got {args.seed}.")
        settings["GenerationID"] = args.seed
    if args.filename is not None:
        settings["Filename"] = args.filename
    adding = args.add_to_timeline or args.track is not None
    if args.timeline is not None and not adding:
        raise ResolveError("-t/--timeline only matters with --add-to-timeline or --track.")
    if adding:
        settings["AddToTimeline"] = True
    if args.track is not None:
        if args.track < 0:
            raise ResolveError(f"--track must be 0 (a new track) or an audio track number, got {args.track}.")
        settings["AudioTrack"] = args.track
    return settings


def cmd_speech(resolve, args):
    settings = speech_settings(args)
    if settings.get("AddToTimeline"):
        project, _ = open_timeline(resolve, args, activate=True)
    else:
        project = current_project(resolve)
    clip = project.GenerateSpeech(settings)
    if not clip:
        raise ResolveError(
            "Resolve could not generate speech. GenerateSpeech needs DaVinci Resolve Studio with the AI Speech "
            "Generator installed (Extras Download Manager); also check the --voice name.")
    info = describe_object(args.session, clip, "MediaPoolItem")
    info["settings"] = settings
    return info


# --- Fairlight presets -----------------------------------------------------------------------


def cmd_fairlight_presets(resolve, args):
    presets = resolve.GetFairlightPresets()
    if not isinstance(presets, (list, tuple)):
        raise ResolveError("Resolve did not return the Fairlight preset list.")
    return list(presets)


def cmd_fairlight_apply(resolve, args):
    presets = resolve.GetFairlightPresets()
    if isinstance(presets, (list, tuple)) and args.name not in presets:
        if not presets:
            raise ResolveError("Resolve lists no Fairlight presets; save one on the Fairlight page first.")
        close = difflib.get_close_matches(args.name, list(presets), n=3)
        hint = f" Did you mean {', '.join(close)}?" if close else f" Presets: {', '.join(presets)}."
        raise ResolveError(f"No Fairlight preset named {args.name!r}.{hint}")
    project, timeline = open_timeline(resolve, args, activate=True)
    if not project.ApplyFairlightPresetToCurrentTimeline(args.name):
        raise ResolveError(f"Resolve could not apply Fairlight preset {args.name!r} to {timeline.GetName()!r}. "
                           "See 'dava fairlight presets'.")
    return {"preset": args.name, "timeline": timeline.GetName()}


# --- parsers ----------------------------------------------------------------------------


def _timeline_arg(p, help_text="timeline name (default: current)"):
    p.add_argument("-t", "--timeline", help=help_text)


def _audio_item_args(p, index_help="clip number on the audio track, from 1 (default: every clip)",
                     timeline_help="timeline name (default: current)"):
    _timeline_arg(p, timeline_help)
    p.add_argument("-T", "--track", type=int, default=1, help="audio track number (default: 1)")
    p.add_argument("-i", "--index", type=int, help=index_help)


def register(registry):
    help_text = "audio tracks, levels, voice isolation, channel mapping, clip audio properties and AI speech"
    p = registry.action("audio", "tracks", "audio tracks of a timeline: name, format (mono, stereo, 5.1...), "
                        "enabled, locked, clip count and voice isolation", group_help=help_text)
    _timeline_arg(p)
    p.set_defaults(func=cmd_tracks, read_only=True)

    p = registry.action("audio", "add-track", "add an audio track at the bottom of a timeline, optionally with a "
                        "channel format (the only way to choose a track's format through the API)")
    p.add_argument("-f", "--format", help="track format passed to Resolve as is, e.g. mono, stereo or 5.1 "
                   "(default: Resolve's default); see the formats in 'dava audio tracks'")
    p.add_argument("-n", "--name", help="name for the new track")
    _timeline_arg(p)
    p.set_defaults(func=cmd_add_track, read_only=False)

    p = registry.action("audio", "modes", "normalization modes for 'dava audio normalize --mode'")
    _timeline_arg(p)
    p.set_defaults(func=cmd_modes, read_only=True)

    p = registry.action("audio", "normalize", "normalize the audio level of clips on audio tracks to a peak "
                        "level (dBFS) or loudness (LKFS) target")
    _timeline_arg(p)
    where = p.add_mutually_exclusive_group()
    where.add_argument("-T", "--track", type=int, help="audio track number (default: 1)")
    where.add_argument("--all-tracks", action="store_true", help="every clip on every audio track")
    p.add_argument("-i", "--index", type=int, help="clip number on the audio track, from 1 (default: every clip)")
    p.add_argument("-m", "--mode", help="normalization mode, e.g. 'Sample Peak Program' (Resolve's default); "
                   "see 'dava audio modes'")
    p.add_argument("--level", type=float, metavar="DBFS", help="target level in dBFS for peak modes, e.g. -9")
    p.add_argument("--loudness", type=float, metavar="LKFS", help="target loudness in LKFS for loudness modes, "
                   "e.g. -23")
    p.add_argument("--set-level", choices=sorted(SET_LEVEL_MODES),
                   help="relative: one gain for all clips (Resolve's default); independent: each clip on its own")
    p.set_defaults(func=cmd_normalize, read_only=False, always_json=True)

    p = registry.action("audio", "isolation", "voice isolation state of an audio track and of its clips")
    _audio_item_args(p, index_help="only this clip of the track (default: the track and every clip)")
    p.set_defaults(func=cmd_isolation, read_only=True, always_json=True)

    p = registry.action("audio", "set-isolation", "turn voice isolation on or off and set its amount, for an "
                        "audio track or (with -i) one clip")
    _audio_item_args(p, index_help="set this clip instead of the whole track",
                     timeline_help="timeline name (default: current); with -i it is made current first, because "
                                   "clip voice isolation is marked [Active Timeline Only] in the API")
    switch = p.add_mutually_exclusive_group()
    switch.add_argument("--on", action="store_true", help="enable voice isolation")
    switch.add_argument("--off", action="store_true", help="disable voice isolation")
    p.add_argument("--amount", type=int, help="isolation strength 0 to 100")
    p.set_defaults(func=cmd_set_isolation, read_only=False, always_json=True)

    p = registry.action("audio", "mapping", "audio channel mapping (JSON) of a media pool clip (--clip) or of "
                        "clips on an audio track")
    p.add_argument("-c", "--clip", help="media pool clip name (instead of timeline clips)")
    _audio_item_args(p)
    p.set_defaults(func=cmd_mapping, read_only=True, always_json=True)

    p = registry.action("audio", "set-mapping", "route source channels to audio tracks: a media pool clip's "
                        "mapping (--clip) or a timeline clip's source channel mapping (-i, exactly one track)")
    p.add_argument("mapping", help="mapping JSON inline, a JSON file, or '-' for stdin; only 'track_mapping' is "
                   f"used, e.g. {MAPPING_EXAMPLE}; each channel_idx lists one source channel (1-based, 0 = none) "
                   f"per channel of its type ({TRACK_FORMAT_HELP})")
    p.add_argument("-c", "--clip", help="media pool clip name")
    _audio_item_args(p, index_help="clip number on the audio track, from 1 (required without --clip)")
    p.set_defaults(func=cmd_set_mapping, read_only=False, always_json=True)

    p = registry.action("audio", "props", "audio properties of clips on an audio track (volume, pan, pitch, "
                        "voice isolation, dialogue leveler)")
    _audio_item_args(p)
    p.set_defaults(func=cmd_props, read_only=True, always_json=True)

    p = registry.action("audio", "set", "set audio properties of clips on an audio track (per clip, Resolve "
                        "applies all given properties or none); out-of-range values are clipped by Resolve")
    p.add_argument("pairs", nargs="*", metavar="KEY=VALUE",
                   help="any audio property, e.g. AudioVolumeEnabled=true AudioDialogueLevelerOutputGain=2 "
                        "(keys: 'dava api show TimelineItemProperties')")
    _audio_item_args(p, timeline_help="timeline name (default: current); it is made current first, because "
                                      "some audio properties only apply on the current timeline")
    p.add_argument("--volume", type=float, metavar="DB",
                   help="clip volume in dB, -100 to 30 (AudioVolume; also sets AudioVolumeEnabled=true)")
    p.add_argument("--pan", type=float, help="pan, -100 (left) to 100 (right) (AudioPan; also sets "
                   "AudioPanEnabled=true)")
    p.add_argument("--semitones", type=float, help="pitch shift in semitones, -24 to 24 (AudioPitchSemiTones; "
                   "also sets AudioPitchEnabled=true)")
    p.add_argument("--cents", type=float, help="fine pitch shift in cents, -100 to 100 (AudioPitchCents; also "
                   "sets AudioPitchEnabled=true)")
    p.add_argument("--leveler", choices=["off", *LEVELER_MODES],
                   help="dialogue leveler: off, or enable it with this mode (AudioDialogueLevelerEnabled/Mode)")
    p.set_defaults(func=cmd_set, read_only=False, always_json=True)

    p = registry.action("audio", "insert", "insert part of an audio file into the current (selected) audio "
                        "track at the playhead, as an insert edit (later clips on that track may move); offset "
                        "and duration are in samples")
    p.add_argument("path", help="audio file to insert")
    p.add_argument("--duration", type=int, required=True, metavar="SAMPLES", help="length to insert, in samples")
    p.add_argument("--offset", type=int, default=0, metavar="SAMPLES",
                   help="start this many samples into the file (default: 0)")
    p.add_argument("--at", metavar="TIMECODE", help="move the playhead here first, e.g. 01:00:05:00")
    _timeline_arg(p, "timeline name, made current first (default: current)")
    p.set_defaults(func=cmd_insert, read_only=False)

    p = registry.action("audio", "speech", "generate a voice-over clip from text with Resolve's AI Speech "
                        "Generator (Studio; the extra must be installed); the clip goes to the media pool")
    p.add_argument("text", help=f"text to speak, at most {SPEECH_TEXT_LIMIT} characters")
    p.add_argument("--voice", help=f"voice model, e.g. 'Female 1', 'Male 1' or {CUSTOM_VOICE!r}")
    p.add_argument("--voice-file", help=f"custom voice file (implies --voice {CUSTOM_VOICE!r})")
    p.add_argument("--speed", type=float, help="speed adjustment, -10 to 10")
    p.add_argument("--variation", type=float, help="variation amount, 0 to 1")
    p.add_argument("--pitch", type=float, help="pitch adjustment, -2 to 2")
    p.add_argument("--seed", type=int, help="generation id (1 or more) to reproduce a result")
    p.add_argument("--filename", help="name of the generated file")
    p.add_argument("--add-to-timeline", action="store_true", help="also put the clip on the timeline")
    p.add_argument("-T", "--track", type=int,
                   help="audio track for the clip, 0 = a new track (implies --add-to-timeline)")
    _timeline_arg(p, "timeline to add the clip to, made current first (default: current)")
    p.set_defaults(func=cmd_speech, read_only=False, always_json=True)

    help_text = "Fairlight audio presets"
    p = registry.action("fairlight", "presets", "names of the saved Fairlight presets", group_help=help_text)
    p.set_defaults(func=cmd_fairlight_presets, read_only=True)

    p = registry.action("fairlight", "apply", "apply a Fairlight preset to the current timeline")
    p.add_argument("name", help="preset name; see 'dava fairlight presets'")
    _timeline_arg(p, "apply to this timeline instead, made current first (default: current)")
    p.set_defaults(func=cmd_fairlight_apply, read_only=False)
