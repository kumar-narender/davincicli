"""dava item / dava take: inspect and edit timeline items (clips, titles, generators, transitions) and their takes.

Items are chosen with -t TIMELINE -T TRACK -i INDEX (helpers.add_item_args) plus -k TRACK_TYPE, or with
--ref REF in the dava REF grammar (item#ID, item:audio:1:2, 'item' for the clip under the playhead).
Commands that take several items apply the change item by item and stop at the first one Resolve rejects.
"""

import argparse

from .. import templates
from ..bridge import coerce, describe_object, resolve_ref, serialize
from ..connect import ResolveError
from ..helpers import add_item_args, current_project, find_clip, find_timeline, timelines, typed_assignments

TRACK_TYPES = ("video", "audio", "subtitle")
REF_HINT = "e.g. item#ID, item:audio:1:2, or item for the video clip under the playhead; see 'dava api refs'"

# CreateMagicMask modes, as documented in the API definition.
MAGIC_MASK_MODES = {"forward": "F", "backward": "B", "both": "BI"}
# FlattenMulticam grade options -> resolve.* constant names.
FLATTEN_GRADES = {"copy": "FLATTEN_MULTICAM_COPY_GRADE", "retain-angle": "FLATTEN_MULTICAM_RETAIN_GRADE_FROM_ANGLE"}
TRANSITION_CATEGORIES = ("simple", "fusion", "ofx", "audio")
STUDIO_HINT = "Some AI tools need DaVinci Resolve Studio or an Extras download; try it once from the UI to see Resolve's reason."


def positive_int(text):
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {text}")
    return value


# ---------------------------------------------------------------------------
# Selecting items


def add_target_args(p, many=False, explicit_all=False):
    """Item selection: -t/-T/-i from add_item_args, plus -k TRACK_TYPE and --ref.

    many: the command takes several items (--ref is repeatable; without -i it takes every item on the
    track, or, with explicit_all, only when --all is given).
    """
    add_item_args(p)
    # No default here: select() needs to tell an explicit -k from none (defaults: video, track 1).
    p.add_argument("-k", "--track-type", choices=TRACK_TYPES, help="type of track -T (default: video)")
    if many:
        p.add_argument("--ref", action="append", metavar="REF",
                       help=f"an item by REF instead of -t/-T/-i ({REF_HINT}); repeatable")
        if explicit_all:
            p.add_argument("--all", action="store_true", help="every item on the track")
            index_help = "item number on the track, from 1 (or --all for every item)"
        else:
            index_help = "item number on the track, from 1 (default: every item on the track)"
    else:
        p.add_argument("--ref", metavar="REF", help=f"the item by REF instead of -t/-T/-i ({REF_HINT})")
        index_help = "item number on the track, from 1 (required unless --ref is given)"
    for action in p._actions:
        if action.dest == "index":
            action.help = index_help
        elif action.dest == "track":
            action.default = None
            action.help = "track number, of the type -k gives (default: 1)"


def item_from_ref(session, ref):
    obj, cls = resolve_ref(session, ref)
    if cls != "TimelineItem":
        raise ResolveError(f"REF {ref!r} names a {cls}, not a timeline item ({REF_HINT}).")
    return obj


def select(resolve, args, many=False):
    """Return (timeline, items) chosen by --ref or by -t/-T/-k/-i; timeline is None for --ref selections."""
    refs = (args.ref or []) if many else ([args.ref] if args.ref else [])
    use_all = getattr(args, "all", False)
    if refs:
        given = [flag for flag, value in (("-t", args.timeline), ("-T", args.track), ("-k", args.track_type),
                                          ("-i", args.index), ("--all", use_all or None)) if value is not None]
        if given:
            raise ResolveError(f"Select items either with --ref or with -t/-T/-k/-i, not both (got --ref and "
                               f"{', '.join(given)}).")
        return None, [item_from_ref(args.session, ref) for ref in refs]
    if args.index is None and not many:
        raise ResolveError(f"Select one item with -i N (item N on track -T, default 1) or --ref REF ({REF_HINT}).")
    if args.index is not None and use_all:
        raise ResolveError("Give -i or --all, not both.")
    if args.index is None and hasattr(args, "all") and not use_all:
        raise ResolveError("Select items with -i N, --ref REF, or --all for every item on the track.")
    track_type = args.track_type or "video"
    track = args.track if args.track is not None else 1
    timeline = find_timeline(current_project(resolve), args.timeline)
    items = list(timeline.GetItemListInTrack(track_type, track) or [])
    if not items:
        raise ResolveError(f"{track_type.capitalize()} track {track} of {timeline.GetName()!r} has no items.")
    if args.index is None:
        return timeline, items
    if not 1 <= args.index <= len(items):
        raise ResolveError(f"Item index {args.index} is out of range; {track_type} track {track} "
                           f"has {len(items)} item(s).")
    return timeline, [items[args.index - 1]]


def one(resolve, args):
    return select(resolve, args)[1][0]


def ref_of(item):
    return f"item#{item.GetUniqueId()}"


def label(item):
    return f"{item.GetName()!r} ({ref_of(item)})"


def row(item, **extra):
    return {"ref": ref_of(item), "name": item.GetName(), **extra}


def track_of(item):
    """(track type, track number) of an item."""
    pair = item.GetTrackTypeAndIndex() or []
    values = [pair[key] for key in sorted(pair)] if isinstance(pair, dict) else list(pair)
    if len(values) != 2:
        raise ResolveError(f"Resolve did not report the track of {label(item)}.")
    return values[0], int(values[1])


def apply(items, change, failure, extra=None):
    """Call change(item) for each item; raise at the first falsy result, naming the items already changed.

    failure(label) builds the error message; extra(result) adds fields to the item's result row.
    """
    rows = []
    for item in items:
        result = change(item)
        if not result:
            done = f" Items changed before this one: {', '.join(r['ref'] for r in rows)}." if rows else ""
            raise ResolveError(failure(label(item)) + done)
        rows.append(row(item, **(extra(result) if extra else {})))
    return rows


def api_value(session, method, value, index=0):
    """Check a value against parameter `index` of TimelineItem.<method> in the API definition.

    Literal names are matched case-insensitively and resolve.* constant names become their values.
    """
    declared = session.spec.method("TimelineItem", method)
    if declared is None:
        raise ResolveError(f"The installed API definition has no TimelineItem.{method}; update DaVinci Resolve.")
    param = declared.params[index]
    return coerce(session, value, param.type, param.name)


def timeline_of(project, items):
    """The timeline that holds all the items (the current timeline is checked first)."""
    current = project.GetCurrentTimeline()
    current_id = current.GetUniqueId() if current else None
    candidates = ([current] if current else []) + [t for t in timelines(project) if t.GetUniqueId() != current_id]
    places = [(track_of(item), item.GetUniqueId()) for item in items]
    for timeline in candidates:
        if all(any(other.GetUniqueId() == uid for other in timeline.GetItemListInTrack(kind, track) or [])
               for (kind, track), uid in places):
            return timeline
    raise ResolveError("The items are not all on one timeline of the current project; handle each timeline separately.")


# ---------------------------------------------------------------------------
# item: reading


def cmd_list(resolve, args):
    timeline = find_timeline(current_project(resolve), args.timeline)
    kinds = [args.track_type] if args.track_type else list(TRACK_TYPES)
    counts = {kind: timeline.GetTrackCount(kind) or 0 for kind in kinds}
    if args.track is not None and all(args.track > count for count in counts.values()):
        have = ", ".join(f"{count} {kind}" for kind, count in counts.items())
        raise ResolveError(f"Timeline {timeline.GetName()!r} has no track {args.track} (tracks: {have}).")
    rows = []
    for kind, count in counts.items():
        tracks = [args.track] if args.track is not None else range(1, count + 1)
        for track in tracks:
            if track > count:
                continue
            for index, item in enumerate(timeline.GetItemListInTrack(kind, track) or [], start=1):
                rows.append({
                    "ref": ref_of(item), "track_type": kind, "track": track, "index": index,
                    "name": item.GetName(), "type": item.GetType(),
                    "start": item.GetStart(), "end": item.GetEnd(), "duration": item.GetDuration(),
                    "enabled": item.GetClipEnabled(), "color": item.GetClipColor(),
                })
    return rows


def cmd_info(resolve, args):
    item = one(resolve, args)
    precision = (True,) if args.subframe else ()
    track_type, track = track_of(item)
    clip = item.GetMediaPoolItem()
    info = describe_object(args.session, item, "TimelineItem")
    info.update({
        "id": item.GetUniqueId(),
        "type": item.GetType(),
        "track_type": track_type,
        "track": track,
        "start": item.GetStart(*precision),
        "end": item.GetEnd(*precision),
        "duration": item.GetDuration(*precision),
        "left_offset": item.GetLeftOffset(*precision),
        "right_offset": item.GetRightOffset(*precision),
        "source_start_frame": item.GetSourceStartFrame(),
        "source_end_frame": item.GetSourceEndFrame(),
        "source_start_time": item.GetSourceStartTime(),
        "source_end_time": item.GetSourceEndTime(),
        "media_pool_item": describe_object(args.session, clip, "MediaPoolItem") if clip else None,
        "enabled": item.GetClipEnabled(),
        "clip_color": item.GetClipColor(),
        "flags": list(item.GetFlagList() or []),
        "takes": item.GetTakesCount(),
        "selected_take": item.GetSelectedTakeIndex(),
    })
    return info


def cmd_get(resolve, args):
    item = one(resolve, args)
    props = item.GetProperties()
    if props is None:
        raise ResolveError(f"Resolve returned no properties for {label(item)}.")
    if not args.keys:
        return dict(props)
    unknown = [key for key in args.keys if key not in props]
    if unknown:
        raise ResolveError(f"{label(item)} has no propert(ies) {', '.join(unknown)}. "
                           f"Available: {', '.join(props) or 'none (Resolve reported no properties)'}.")
    return {key: props[key] for key in args.keys}


def cmd_linked(resolve, args):
    item = one(resolve, args)
    rows = []
    for other in item.GetLinkedItems() or []:
        track_type, track = track_of(other)
        rows.append(row(other, type=other.GetType(), track_type=track_type, track=track,
                        start=other.GetStart(), end=other.GetEnd()))
    return rows


def cmd_flags(resolve, args):
    _, items = select(resolve, args, many=True)
    return [row(item, flags=list(item.GetFlagList() or [])) for item in items]


def cmd_stereo(resolve, args):
    item = one(resolve, args)
    values = {
        "convergence": item.GetStereoConvergenceValues(),
        "left_floating_window": item.GetStereoLeftFloatingWindowParams(),
        "right_floating_window": item.GetStereoRightFloatingWindowParams(),
    }
    missing = [key for key, value in values.items() if value is None]
    if missing:
        raise ResolveError(f"Resolve returned no stereo 3D {', '.join(missing)} for {label(item)}; is it a stereo clip?")
    info = describe_object(args.session, item, "TimelineItem")
    info.update({key: serialize(args.session, value) for key, value in values.items()})
    return info


# ---------------------------------------------------------------------------
# item: changing


def cmd_set(resolve, args):
    values = typed_assignments(args.session, args.assignments, "TimelineItemProperties")
    _, items = select(resolve, args, many=True)
    rows = apply(items, lambda item: item.SetProperties(values), lambda what: (
        f"Resolve rejected properties {values} for {what}; it may still have applied some of them (compare "
        "with 'dava item get'). Check the value ranges with 'dava api show TimelineItemProperties'; keys marked "
        "[Active Timeline Only] need the item's timeline to be the current one."))
    return {"properties": values, "items": rows}


def cmd_fades(resolve, args):
    fades = {key: value for key, value in (("FadeIn", args.fade_in), ("FadeOut", args.fade_out)) if value is not None}
    negative = [key for key, value in fades.items() if value < 0]
    if negative:
        raise ResolveError(f"Fade lengths are frames and must be 0 or more: {', '.join(negative)}.")
    _, items = select(resolve, args, many=True)
    if fades:
        apply(items, lambda item: item.SetFades(fades),
              lambda what: f"Resolve rejected fades {fades} for {what} (longer than the item?).")
    rows = []
    for item in items:
        current = item.GetFades()
        if current is None:
            raise ResolveError(f"Resolve returned no fades for {label(item)}.")
        rows.append(row(item, fade_in=current.get("FadeIn"), fade_out=current.get("FadeOut")))
    return rows


def cmd_speed(resolve, args):
    options = {}
    if args.percentage is not None:
        options["Percentage"] = args.percentage
    if args.pitch_correction is not None:
        options["PitchCorrection"] = args.pitch_correction == "on"
    if (args.stretch_keyframes or args.ripple) and args.percentage is None:
        raise ResolveError("--stretch-keyframes and --ripple only apply together with a new PERCENT.")
    if args.stretch_keyframes:
        options["StretchKeyframesToFit"] = True
    if args.ripple:
        options["RippleTimeline"] = True
    item = one(resolve, args)
    if options and not item.SetSpeed(options):
        raise ResolveError(f"Resolve rejected speed {options} for {label(item)}. Stills cannot be retimed (they "
                           "keep the standard still duration); for other items check PERCENT (0 = freeze frame).")
    speed = item.GetSpeed()
    if speed is None:
        raise ResolveError(f"Resolve returned no speed for {label(item)}.")
    info = describe_object(args.session, item, "TimelineItem")
    info.update(speed)
    return info


def _transition_failure(options):
    def message(what):
        text = (f"Resolve could not add transition {options['type']!r} (category {options['category']}) to {what}. "
                "Use category simple for built-in transitions such as 'Cross Dissolve' and category fusion for "
                "the names 'dava templates list transitions' prints; a centered transition needs media beyond "
                "the cut (see left_offset/right_offset in 'dava item info').")
        words = options["type"].split()
        similar = templates.list_templates(kind="transitions", search=words[0]) if words else []
        if similar:
            text += " Fusion transitions with similar names: " + ", ".join(r["name"] for r in similar[:8]) + "."
        return text
    return message


def cmd_transition(resolve, args):
    options = {"type": args.type, "category": args.category, "position": args.position}
    if args.alignment:
        options["alignment"] = args.alignment
    if args.duration is not None:
        options["duration"] = args.duration
    _, items = select(resolve, args, many=True)
    return apply(items, lambda item: item.AddTransition(options), _transition_failure(options),
                 extra=lambda transition: {"transition": ref_of(transition), "start": transition.GetStart(),
                                           "duration": transition.GetDuration()})


def _enable(resolve, args, enabled):
    _, items = select(resolve, args, many=True)
    word = "enable" if enabled else "disable"
    return apply(items, lambda item: item.SetClipEnabled(enabled), lambda what: f"Resolve could not {word} {what}.",
                 extra=lambda _: {"enabled": enabled})


def cmd_enable(resolve, args):
    return _enable(resolve, args, True)


def cmd_disable(resolve, args):
    return _enable(resolve, args, False)


def cmd_color(resolve, args):
    color = api_value(args.session, "SetClipColor", args.color)
    _, items = select(resolve, args, many=True)
    return apply(items, lambda item: item.SetClipColor(color),
                 lambda what: f"Resolve could not set clip color {color} on {what}.", extra=lambda _: {"color": color})


def cmd_color_clear(resolve, args):
    _, items = select(resolve, args, many=True)
    return apply(items, lambda item: item.ClearClipColor(), lambda what: f"Resolve could not clear the clip color of {what}.")


def cmd_flag_add(resolve, args):
    colors = [api_value(args.session, "AddFlag", color) for color in args.colors]
    _, items = select(resolve, args, many=True)
    rejected = []

    def add(item):
        # Try every color (no short-circuit), then report exactly the ones Resolve refused.
        rejected[:] = [color for color in colors if not item.AddFlag(color)]
        return not rejected

    def failure(what):
        added = [color for color in colors if color not in rejected]
        return (f"Resolve could not add flag(s) {', '.join(rejected)} to {what}"
                + (f" (added: {', '.join(added)})." if added else "."))

    apply(items, add, failure)
    return [row(item, flags=list(item.GetFlagList() or [])) for item in items]


def cmd_flag_clear(resolve, args):
    color = api_value(args.session, "ClearFlags", args.color)
    _, items = select(resolve, args, many=True)
    apply(items, lambda item: item.ClearFlags(color), lambda what: f"Resolve could not clear {color} flags of {what}.")
    return [row(item, flags=list(item.GetFlagList() or [])) for item in items]


def cmd_rename(resolve, args):
    item = one(resolve, args)
    old = item.GetName()
    if not item.SetName(args.name):
        raise ResolveError(f"Resolve could not rename {label(item)} to {args.name!r}.")
    info = describe_object(args.session, item, "TimelineItem")
    info["old_name"] = old
    return info


def cmd_delete(resolve, args):
    timeline, items = select(resolve, args, many=True)
    if timeline is None:
        timeline = timeline_of(current_project(resolve), items)
    rows = [row(item) for item in items]
    if not timeline.DeleteClips(items, args.ripple):
        raise ResolveError(f"Resolve could not delete {', '.join(r['ref'] for r in rows)} from {timeline.GetName()!r}.")
    return {"timeline": timeline.GetName(), "ripple": args.ripple, "deleted": rows}


def cmd_stabilize(resolve, args):
    _, items = select(resolve, args, many=True)
    return apply(items, lambda item: item.Stabilize(), lambda what: f"Resolve could not stabilize {what}. {STUDIO_HINT}")


def cmd_smart_reframe(resolve, args):
    _, items = select(resolve, args, many=True)
    return apply(items, lambda item: item.SmartReframe(),
                 lambda what: f"Resolve could not smart-reframe {what}. {STUDIO_HINT}")


def cmd_magic_mask(resolve, args):
    if args.regenerate == (args.direction is not None):
        raise ResolveError("Give a DIRECTION (forward, backward, both) to create a magic mask, or --regenerate.")
    item = one(resolve, args)
    if args.regenerate:
        ok, what = item.RegenerateMagicMask(), "regenerate the magic mask of"
    else:
        ok, what = item.CreateMagicMask(MAGIC_MASK_MODES[args.direction]), f"create a {args.direction} magic mask on"
    if not ok:
        raise ResolveError(f"Resolve could not {what} {label(item)}; is a Magic Mask set up on its Color page "
                           f"node? {STUDIO_HINT}")
    return describe_object(args.session, item, "TimelineItem")


def cmd_multicam_flatten(resolve, args):
    grade = api_value(args.session, "FlattenMulticam", FLATTEN_GRADES[args.grade])
    item = one(resolve, args)
    if not item.FlattenMulticam(grade):
        raise ResolveError(f"Resolve could not flatten {label(item)}; is it a multicam clip?")
    return describe_object(args.session, item, "TimelineItem")


def cmd_multicam_switch(resolve, args):
    settings = typed_assignments(args.session, args.settings, "SmartSwitchSettings")
    item = one(resolve, args)
    if not item.PerformMulticamSmartSwitch(settings):
        raise ResolveError(f"Resolve could not run SmartSwitch on {label(item)} with {settings}; is it a multicam "
                           f"clip? {STUDIO_HINT}")
    info = describe_object(args.session, item, "TimelineItem")
    info["settings"] = settings
    return info


BLANKING_SIDES = ("Top", "Bottom", "Left", "Right")


def cmd_blanking(resolve, args):
    given = {side: getattr(args, side.lower()) for side in BLANKING_SIDES if getattr(args, side.lower()) is not None}
    negative = [side for side, value in given.items() if value < 0]
    if negative:
        raise ResolveError(f"Blanking is in pixels and must be 0 or more: {', '.join(negative)}.")
    if given and args.use_timeline == "on":
        flags = ", ".join(f"--{side.lower()}" for side in given)
        raise ResolveError(f"--use-timeline on makes the item use the timeline's output blanking, so {flags} "
                           "would not take effect. Give the sides alone or with --use-timeline off.")
    item = one(resolve, args)
    if args.use_timeline is not None:
        if not item.SetUseTimelineForOutputBlanking(args.use_timeline == "on"):
            raise ResolveError(f"Resolve could not turn timeline output blanking {args.use_timeline} for {label(item)}.")
    if given:
        current = item.GetOutputBlanking()
        if current is None:
            raise ResolveError(f"Resolve returned no output blanking for {label(item)}.")
        values = {**current, **given}
        if not item.SetOutputBlanking(values):
            raise ResolveError(f"Resolve rejected output blanking {values} for {label(item)}.")
    blanking = item.GetOutputBlanking()
    if blanking is None:
        raise ResolveError(f"Resolve returned no output blanking for {label(item)}.")
    info = describe_object(args.session, item, "TimelineItem")
    info.update({"use_timeline": item.GetUseTimelineForOutputBlanking(), "blanking": dict(blanking)})
    return info


def cmd_sidecar(resolve, args):
    _, items = select(resolve, args, many=True)
    return apply(items, lambda item: item.UpdateSidecar(),
                 lambda what: f"Resolve could not update the sidecar of {what}; only BRAW (sidecar) and R3D (RMD) "
                              "clips have one.")


def cmd_burn_in(resolve, args):
    _, items = select(resolve, args, many=True)

    def failure(what):
        presets = resolve.GetBurnInPresetList() or []
        known = f" Presets: {', '.join(presets)}." if presets else " There are no data burn-in presets."
        return f"Resolve could not load data burn-in preset {args.preset!r} for {what}.{known}"

    return apply(items, lambda item: item.LoadBurnInPreset(args.preset), failure)


# ---------------------------------------------------------------------------
# take


def _take_count(item, number):
    count = item.GetTakesCount() or 0
    if count == 0:
        raise ResolveError(f"{label(item)} is not a take selector; add takes with 'dava take add'.")
    if not 1 <= number <= count:
        raise ResolveError(f"Take {number} is out of range; {label(item)} has {count} take(s).")


def cmd_take_list(resolve, args):
    item = one(resolve, args)
    count = item.GetTakesCount() or 0
    takes = []
    for number in range(1, count + 1):
        take = item.GetTakeByIndex(number)
        if not take:
            raise ResolveError(f"Resolve returned no info for take {number} of {label(item)}.")
        clip = take.get("mediaPoolItem")
        takes.append({"index": number, "clip": clip.GetName() if clip else None,
                      "clip_ref": describe_object(args.session, clip, "MediaPoolItem")["$ref"] if clip else None,
                      "start_frame": take.get("startFrame"), "end_frame": take.get("endFrame")})
    info = describe_object(args.session, item, "TimelineItem")
    info.update({"selected": item.GetSelectedTakeIndex(), "takes": takes})
    return info


def media_pool_item(resolve, session, text):
    """A media pool clip by name, or by REF when text starts with clip: / clip# / $."""
    if text.startswith(("clip:", "clip#", "$")):
        obj, cls = resolve_ref(session, text)
        if cls != "MediaPoolItem":
            raise ResolveError(f"REF {text!r} names a {cls}, not a media pool clip.")
        return obj
    return find_clip(resolve, text)


def cmd_take_add(resolve, args):
    if (args.start is None) != (args.end is None):
        raise ResolveError("Give both --start and --end (source frames), or neither for the whole clip.")
    item = one(resolve, args)
    clip = media_pool_item(resolve, args.session, args.clip)
    extents = (args.start, args.end) if args.start is not None else ()
    if not item.AddTake(clip, *extents):
        span = f" frames {args.start}-{args.end}" if extents else ""
        raise ResolveError(f"Resolve could not add {clip.GetName()!r}{span} as a take of {label(item)}.")
    info = describe_object(args.session, item, "TimelineItem")
    info.update({"takes": item.GetTakesCount(), "selected": item.GetSelectedTakeIndex()})
    return info


def cmd_take_select(resolve, args):
    item = one(resolve, args)
    _take_count(item, args.take)
    if not item.SelectTakeByIndex(args.take):
        raise ResolveError(f"Resolve could not select take {args.take} of {label(item)}.")
    info = describe_object(args.session, item, "TimelineItem")
    info["selected"] = item.GetSelectedTakeIndex()
    return info


def cmd_take_delete(resolve, args):
    item = one(resolve, args)
    _take_count(item, args.take)
    if not item.DeleteTakeByIndex(args.take):
        raise ResolveError(f"Resolve could not delete take {args.take} of {label(item)}.")
    info = describe_object(args.session, item, "TimelineItem")
    info.update({"takes": item.GetTakesCount(), "selected": item.GetSelectedTakeIndex()})
    return info


def cmd_take_finalize(resolve, args):
    item = one(resolve, args)
    if not item.GetTakesCount():
        raise ResolveError(f"{label(item)} is not a take selector; there is nothing to finalize.")
    if not item.FinalizeTake():
        raise ResolveError(f"Resolve could not finalize the take selection of {label(item)}.")
    return describe_object(args.session, item, "TimelineItem")


# ---------------------------------------------------------------------------
# Registration


def register(registry):
    group_help = "timeline items (clips, titles, generators, transitions) on any track: list, inspect, edit"

    p = registry.action("item", "list", "list the items on a timeline's tracks with their REF, type, position, "
                        "enabled state and clip color", group_help=group_help)
    p.add_argument("-t", "--timeline", help="timeline name (default: current)")
    p.add_argument("-k", "--track-type", choices=TRACK_TYPES, help="only tracks of this type (default: all)")
    p.add_argument("-T", "--track", type=positive_int, help="only this track number (default: all)")
    p.set_defaults(func=cmd_list, read_only=True)

    p = registry.action("item", "info", "one item in detail: type, track, start/end/duration, source range, "
                        "left/right offsets (handles), media pool clip, enabled, color, flags, takes")
    add_target_args(p)
    p.add_argument("--subframe", action="store_true", help="fractional frames for positions and offsets")
    p.set_defaults(func=cmd_info, read_only=True, always_json=True)

    p = registry.action("item", "get", "show item properties (transform, crop, composite, retime, audio); "
                        "constants come back as numbers")
    p.add_argument("keys", nargs="*", metavar="KEY", help="only these properties (default: all)")
    add_target_args(p)
    p.set_defaults(func=cmd_get, read_only=True)

    p = registry.action("item", "set", "set item properties, e.g. ZoomX=1.2 ZoomY=1.2 Opacity=50 "
                        "CompositeMode=COMPOSITE_SCREEN FlipX=true; items are changed one by one, stopping at the "
                        "first one Resolve rejects. Keys and ranges: 'dava api show TimelineItemProperties'")
    p.add_argument("assignments", nargs="+", metavar="KEY=VALUE")
    add_target_args(p, many=True)
    p.set_defaults(func=cmd_set, read_only=False, always_json=True)

    p = registry.action("item", "fades", "show the fade-in/fade-out lengths (frames) of the video or audio "
                        "fader, or set them with --in/--out")
    p.add_argument("--in", dest="fade_in", type=int, metavar="FRAMES", help="new fade-in length in frames")
    p.add_argument("--out", dest="fade_out", type=int, metavar="FRAMES", help="new fade-out length in frames")
    add_target_args(p, many=True)
    p.set_defaults(func=cmd_fades, read_only=lambda a: a.fade_in is None and a.fade_out is None)

    p = registry.action("item", "speed", "show the clip speed, or set it in percent (100 = normal, 0 = freeze "
                        "frame). Stills cannot be retimed: they keep the standard still duration")
    p.add_argument("percentage", nargs="?", type=float, metavar="PERCENT", help="new speed in percent")
    p.add_argument("--pitch-correction", choices=["on", "off"],
                   help="pitch correction of linked audio (default: unchanged)")
    p.add_argument("--stretch-keyframes", action="store_true", help="stretch keyframes to fit the new length")
    p.add_argument("--ripple", action="store_true", help="ripple the timeline (move later items)")
    add_target_args(p)
    p.set_defaults(func=cmd_speed, read_only=lambda a: a.percentage is None and a.pitch_correction is None
                   and not a.stretch_keyframes and not a.ripple)

    p = registry.action("item", "transition", "add a transition at the start or end of items (TimelineItem."
                        "AddTransition); prints the new transition's REF")
    p.add_argument("type", metavar="TYPE", help="transition name: built-in ones such as 'Cross Dissolve' with "
                   "--category simple; Fusion ones as listed by 'dava templates list transitions' with --category fusion")
    p.add_argument("-c", "--category", choices=TRANSITION_CATEGORIES, default="simple",
                   help="transition category (default: simple)")
    p.add_argument("-p", "--position", choices=["start", "end"], default="end",
                   help="edge of the item to put it on (default: end)")
    p.add_argument("-a", "--alignment", choices=["left", "center", "right"],
                   help="placement relative to the edge (default: Resolve's)")
    p.add_argument("-d", "--duration", type=positive_int, metavar="FRAMES",
                   help="length in frames (default: calculated by Resolve)")
    add_target_args(p, many=True)
    p.set_defaults(func=cmd_transition, read_only=False)

    p = registry.action("item", "enable", "enable items (they play and render again)")
    add_target_args(p, many=True)
    p.set_defaults(func=cmd_enable, read_only=False)

    p = registry.action("item", "disable", "disable items (kept on the timeline but not played or rendered)")
    add_target_args(p, many=True)
    p.set_defaults(func=cmd_disable, read_only=False)

    p = registry.action("item", "color", "set the clip color of items, e.g. Orange, Teal, Navy; all names: "
                        "'dava api show ClipColor'")
    p.add_argument("color", metavar="COLOR")
    add_target_args(p, many=True)
    p.set_defaults(func=cmd_color, read_only=False)

    p = registry.action("item", "color-clear", "remove the clip color of items")
    add_target_args(p, many=True)
    p.set_defaults(func=cmd_color_clear, read_only=False)

    p = registry.action("item", "flags", "list the flag colors of items")
    add_target_args(p, many=True)
    p.set_defaults(func=cmd_flags, read_only=True)

    p = registry.action("item", "flag-add", "add flags to items, e.g. Blue Red; all names: 'dava api show FlagColor'")
    p.add_argument("colors", nargs="+", metavar="COLOR")
    add_target_args(p, many=True)
    p.set_defaults(func=cmd_flag_add, read_only=False)

    p = registry.action("item", "flag-clear", "remove flags of one color, or all flags, from items")
    p.add_argument("color", nargs="?", default="All", metavar="COLOR", help="flag color to remove (default: All)")
    add_target_args(p, many=True)
    p.set_defaults(func=cmd_flag_clear, read_only=False)

    p = registry.action("item", "rename", "rename an item on the timeline (the media pool clip keeps its name)")
    p.add_argument("name", metavar="NAME")
    add_target_args(p)
    p.set_defaults(func=cmd_rename, read_only=False)

    p = registry.action("item", "delete", "remove items from their timeline; --ripple closes the gaps")
    p.add_argument("--ripple", action="store_true", help="ripple delete: move later items left")
    add_target_args(p, many=True, explicit_all=True)
    p.set_defaults(func=cmd_delete, read_only=False, always_json=True)

    p = registry.action("item", "linked", "the items linked to an item (e.g. the audio of a video clip)")
    add_target_args(p)
    p.set_defaults(func=cmd_linked, read_only=True)

    p = registry.action("item", "stabilize", "run stabilization on items (Resolve analyses the whole clip)")
    add_target_args(p, many=True)
    p.set_defaults(func=cmd_stabilize, read_only=False)

    p = registry.action("item", "smart-reframe", "run Smart Reframe on items (AI; Studio)")
    add_target_args(p, many=True)
    p.set_defaults(func=cmd_smart_reframe, read_only=False)

    p = registry.action("item", "magic-mask", "track the item's Magic Mask forward, backward or both ways, or "
                        "regenerate it (AI; the mask is drawn on the Color page)")
    p.add_argument("direction", nargs="?", choices=sorted(MAGIC_MASK_MODES), help="tracking direction")
    p.add_argument("--regenerate", action="store_true", help="regenerate the existing magic mask instead")
    add_target_args(p)
    p.set_defaults(func=cmd_magic_mask, read_only=False)

    p = registry.action("item", "multicam-flatten", "replace a multicam item with its active angles")
    p.add_argument("--grade", choices=sorted(FLATTEN_GRADES), required=True,
                   help="copy: FLATTEN_MULTICAM_COPY_GRADE (copy the multicam grade); retain-angle: "
                        "FLATTEN_MULTICAM_RETAIN_GRADE_FROM_ANGLE (keep each angle's own grade)")
    add_target_args(p)
    p.set_defaults(func=cmd_multicam_flatten, read_only=False)

    p = registry.action("item", "multicam-switch", "run Multicam SmartSwitch on a multicam item (AI angle "
                        "switching). Settings: minEditDuration, editChangeDelay, isAutoDetectWideAngle, analysisMode, "
                        "wideAngleID, wideAngleFrequency, isUseWideAngleForIntroOutro, isUseWideAngleForSilence, "
                        "switchOnVideoOnly, quality; see 'dava api show SmartSwitchSettings'")
    p.add_argument("settings", nargs="*", metavar="KEY=VALUE", help="settings (default: Resolve's defaults)")
    add_target_args(p)
    p.set_defaults(func=cmd_multicam_switch, read_only=False, always_json=True)

    p = registry.action("item", "blanking", "show the item's output blanking (pixels), or set sides and "
                        "whether the timeline's blanking is used")
    for side in BLANKING_SIDES:
        p.add_argument(f"--{side.lower()}", type=int, metavar="PIXELS", help=f"{side.lower()} blanking in pixels")
    p.add_argument("--use-timeline", choices=["on", "off"],
                   help="on: use the timeline's output blanking (not with side values); off: use the item's own "
                        "values")
    add_target_args(p)
    p.set_defaults(func=cmd_blanking, always_json=True,
                   read_only=lambda a: a.use_timeline is None and all(getattr(a, s.lower()) is None for s in BLANKING_SIDES))

    p = registry.action("item", "stereo", "stereo 3D keyframes: convergence values and left/right floating "
                        "window parameters, by keyframe offset")
    add_target_args(p)
    p.set_defaults(func=cmd_stereo, read_only=True, always_json=True)

    p = registry.action("item", "sidecar", "write the sidecar file of BRAW clips (or the RMD file of R3D clips)")
    add_target_args(p, many=True)
    p.set_defaults(func=cmd_sidecar, read_only=False)

    p = registry.action("item", "burn-in", "load a data burn-in preset for items (presets: 'dava call resolve "
                        "GetBurnInPresetList')")
    p.add_argument("preset", metavar="PRESET")
    add_target_args(p, many=True)
    p.set_defaults(func=cmd_burn_in, read_only=False)

    take_help = "take selectors: alternative media pool clips (takes) inside one timeline item"
    p = registry.action("take", "list", "the takes of an item and which one is selected", group_help=take_help)
    add_target_args(p)
    p.set_defaults(func=cmd_take_list, read_only=True, always_json=True)

    p = registry.action("take", "add", "add a media pool clip as a take (makes the item a take selector)")
    p.add_argument("clip", metavar="CLIP", help="media pool clip name, or a REF such as clip#ID or clip:/Master/Sub/NAME")
    p.add_argument("--start", type=int, metavar="FRAME", help="first source frame of the take (with --end)")
    p.add_argument("--end", type=int, metavar="FRAME", help="last source frame of the take (with --start)")
    add_target_args(p)
    p.set_defaults(func=cmd_take_add, read_only=False)

    p = registry.action("take", "select", "make take N the active one")
    p.add_argument("take", type=positive_int, metavar="N", help="take number, from 1 (see 'dava take list')")
    add_target_args(p)
    p.set_defaults(func=cmd_take_select, read_only=False)

    p = registry.action("take", "delete", "remove take N from the take selector")
    p.add_argument("take", type=positive_int, metavar="N", help="take number, from 1 (see 'dava take list')")
    add_target_args(p)
    p.set_defaults(func=cmd_take_delete, read_only=False)

    p = registry.action("take", "finalize", "keep the selected take and turn the take selector back into a clip")
    add_target_args(p)
    p.set_defaults(func=cmd_take_finalize, read_only=False)
