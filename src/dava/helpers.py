"""Lookups shared by the command modules: the current project, timelines, clips and items."""

from .bridge import iter_clips
from .connect import ResolveError


def current_project(resolve):
    project = resolve.GetProjectManager().GetCurrentProject()
    if not project:
        raise ResolveError("No project is open. Use 'dava project open NAME'.")
    return project


def timelines(project):
    return [project.GetTimelineByIndex(i) for i in range(1, project.GetTimelineCount() + 1)]


def find_timeline(project, name=None):
    """Return the named timeline, or the current one when name is None."""
    if name is None:
        timeline = project.GetCurrentTimeline()
        if not timeline:
            raise ResolveError("The project has no current timeline.")
        return timeline
    for timeline in timelines(project):
        if timeline.GetName() == name:
            return timeline
    raise ResolveError(f"No timeline named {name!r} in project {project.GetName()!r}.")


def select_items(resolve, args):
    """Return [(clip index, TimelineItem)] for --index on --track, or every item on the track."""
    timeline = find_timeline(current_project(resolve), args.timeline)
    items = timeline.GetItemListInTrack("video", args.track) or []
    if not items:
        raise ResolveError(f"Video track {args.track} of {timeline.GetName()!r} has no clips.")
    if args.index is None:
        return list(enumerate(items, start=1))
    if not 1 <= args.index <= len(items):
        raise ResolveError(f"Clip index {args.index} is out of range; track {args.track} has {len(items)} clip(s).")
    return [(args.index, items[args.index - 1])]


def select_one_item(resolve, args):
    return select_items(resolve, args)[0][1]


def check_node(graph, node):
    count = graph.GetNumNodes()
    if not 1 <= node <= count:
        raise ResolveError(f"Node {node} is out of range; the clip has {count} node(s).")


def find_clip(resolve, name):
    """Return the unique media pool clip with the given name, searching every folder."""
    root = current_project(resolve).GetMediaPool().GetRootFolder()
    matches = [(path, clip) for path, clip in iter_clips(root) if clip.GetName() == name]
    if not matches:
        raise ResolveError(f"No media pool clip named {name!r}.")
    if len(matches) > 1:
        folders = ", ".join(path for path, _ in matches)
        raise ResolveError(f"Clip name {name!r} is ambiguous; it appears in: {folders}")
    return matches[0][1]


def parse_assignments(pairs):
    result = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise ResolveError(f"Expected KEY=VALUE, got {pair!r}.")
        result[key] = value
    return result


def add_item_args(p, single=False):
    """Options that select clips on a timeline video track: -t TIMELINE, -T TRACK, -i INDEX."""
    p.add_argument("-t", "--timeline", help="timeline name (default: current)")
    p.add_argument("-T", "--track", type=int, default=1, help="video track number (default: 1)")
    if single:
        p.add_argument("-i", "--index", type=int, required=True, help="clip number on the track, from 1")
    else:
        p.add_argument("-i", "--index", type=int, help="clip number on the track, from 1 (default: all clips)")


def typed_assignments(session, pairs, typeddict_name):
    """Parse KEY=VALUE pairs into a dict typed by an API TypedDict (e.g. TimelineItemProperties).

    Values are converted to the declared field types (numbers, bools, resolve.* constants by name,
    REFs for object fields). Unknown keys are an error that lists the valid ones.
    """
    from .bridge import coerce, parse_cli_value

    values = parse_assignments(pairs)
    fields = session.spec.typeddicts[typeddict_name].fields
    unknown = sorted(set(values) - set(fields))
    if unknown:
        raise ResolveError(f"Unknown {typeddict_name} key(s): {', '.join(unknown)}. "
                           f"Valid keys: {', '.join(fields)}.")
    return {key: coerce(session, parse_cli_value(text, fields[key][0]), fields[key][0], key)
            for key, text in values.items()}


def apply_render_settings(project, settings):
    """Apply render settings one key at a time; return the keys Resolve rejected.

    SetRenderSettings with several keys returns False if any one fails (and keeps the others), which
    hides which key was the problem. Some keys are also rejected harmlessly (e.g. SelectAllFrames=False).
    """
    return [key for key, value in settings.items() if not project.SetRenderSettings({key: value})]
