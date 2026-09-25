"""dava node, version, colorgroup and gallery, plus more color and still actions: the grading API.

The 'node' commands work on any node graph: the graphs of timeline clips (any node stack layer),
the timeline's own graph and the pre-clip / post-clip graphs of color groups (--graph). The older
'color' commands only reach the first layer of clip graphs.
"""

import os
from argparse import ArgumentTypeError, Namespace

from ..bridge import describe_object
from ..connect import ResolveError
from ..helpers import add_item_args, current_project, find_timeline, select_items

# --graph choices: which node graph a node command works on.
GRAPHS = ("clip", "timeline", "pre", "post")

# 'node cache' words -> resolve.CACHE_* constant names (README section "Cache Mode information").
CACHE_MODES = {"auto": "CACHE_AUTO_ENABLED", "on": "CACHE_ENABLED", "off": "CACHE_DISABLED"}

# Graph.ApplyGradeFromDRX gradeMode values, with the same words as 'dava color drx --keyframes'.
DRX_GRADE_MODES = {"none": 0, "source-tc": 1, "start-frames": 2}

# versionType of the TimelineItem version methods.
VERSION_TYPES = {"local": 0, "remote": 1}
VERSION_NAMES = {code: name for name, code in VERSION_TYPES.items()}

LUT_HINT = (" Give an absolute path or one relative to Resolve's LUT folders; "
            "'dava node list' shows the LUT paths Resolve reports.")


def positive_int(text):
    """argparse type for 1-based numbers (nodes, layers, clips, stills)."""
    value = int(text)
    if value < 1:
        raise ArgumentTypeError(f"must be at least 1, got {text}")
    return value


# ---------------------------------------------------------------------------
# Targets: the clips, graphs, color groups and albums a command works on.
# A target is (where, object); `where` is a dict naming it in results and error messages.


def describe(where):
    kind = where.get("graph", "clip")
    if kind == "timeline":
        text = f"the timeline graph of {where['name']!r}"
    elif kind in ("pre", "post"):
        text = f"the {kind}-clip graph of color group {where['name']!r}"
    else:
        text = f"clip {where['clip']} {where['name']!r}" + (f" (layer {where['layer']})" if "layer" in where else "")
    return text[0].upper() + text[1:]


def failure(where, what, changed, hint=""):
    """A ResolveError for a refused change that also says how many earlier changes were applied."""
    earlier = f" {changed} earlier change(s) were already applied." if changed else ""
    return ResolveError(f"{describe(where)}: Resolve could not {what}.{earlier}{hint}")


def clip_targets(resolve, args):
    """[(where, TimelineItem)] for -t/-T/-i."""
    return [({"clip": index, "name": item.GetName()}, item) for index, item in select_items(resolve, args)]


def find_group(project, name):
    groups = project.GetColorGroupsList() or []
    matches = [group for group in groups if group.GetName() == name]
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise ResolveError(f"{len(matches)} color groups are named {name!r}; rename one with "
                           "'dava call colorgroup@N SetName NEW' (N from 'dava colorgroup list').")
    names = ", ".join(repr(group.GetName()) for group in groups) or "none"
    raise ResolveError(f"No color group named {name!r} (color groups: {names}). "
                       "Create one with 'dava colorgroup create NAME'.")


def graph_targets(resolve, args):
    """[(where, Graph)] for the options of add_graph_args."""
    if args.graph in ("pre", "post"):
        if args.group is None:
            raise ResolveError(f"--graph {args.graph} needs --group NAME (see 'dava colorgroup list').")
    elif args.group is not None:
        raise ResolveError("--group only applies with --graph pre or --graph post.")
    if args.graph != "clip":
        stray = [flag for flag, value in (("-i/--index", args.index), ("-L/--layer", args.layer)) if value is not None]
        if args.track != 1:  # -T defaults to 1, so only another track number shows it was given
            stray.append("-T/--track")
        if args.graph != "timeline" and args.timeline is not None:
            stray.append("-t/--timeline")
        if stray:
            raise ResolveError(f"{' and '.join(stray)} cannot be used with --graph {args.graph}.")

    if args.graph == "clip":
        targets = []
        for index, item in select_items(resolve, args):
            where = {"graph": "clip", "clip": index, "name": item.GetName(), "layer": args.layer or 1}
            targets.append((where, item.GetNodeGraph(args.layer) if args.layer else item.GetNodeGraph()))
    elif args.graph == "timeline":
        timeline = find_timeline(current_project(resolve), args.timeline)
        targets = [({"graph": "timeline", "name": timeline.GetName()}, timeline.GetNodeGraph())]
    else:
        group = find_group(current_project(resolve), args.group)
        graph = group.GetPreClipNodeGraph() if args.graph == "pre" else group.GetPostClipNodeGraph()
        targets = [({"graph": args.graph, "name": group.GetName()}, graph)]

    for where, graph in targets:
        if not graph:
            hint = (" The project setting nodeStackLayers sets how many layers exist "
                    "('dava project settings nodeStackLayers')." if args.layer else "")
            raise ResolveError(f"{describe(where)}: Resolve returned no node graph.{hint}")
    return targets


def node_count(graph, where):
    count = graph.GetNumNodes()
    if count is None or isinstance(count, bool):
        raise ResolveError(f"{describe(where)}: Resolve did not report a node count.")
    return int(count)


def check_nodes(targets, nodes):
    """Check every node number against every graph before anything is changed."""
    for where, graph in targets:
        count = node_count(graph, where)
        for node in nodes:
            if not 1 <= node <= count:
                raise ResolveError(f"Node {node} is out of range: {describe(where)} has {count} node(s).")


def cache_constant(resolve, mode):
    name = CACHE_MODES[mode]
    value = getattr(resolve, name, None)
    if value is None:
        raise ResolveError(f"This Resolve does not define resolve.{name}.")
    return value


def cache_word(resolve, value):
    """The 'node cache' word for a GetNodeCacheMode result; the raw value if it matches no constant."""
    if value is not None:
        for word, name in CACHE_MODES.items():
            if getattr(resolve, name, None) == value:
                return word
    return value


def project_gallery(resolve):
    gallery = current_project(resolve).GetGallery()
    if not gallery:
        raise ResolveError("Resolve returned no gallery for the current project.")
    return gallery


def album_kind(powergrade):
    return "PowerGrade album" if powergrade else "still album"


def albums_of(gallery, powergrade):
    return list((gallery.GetGalleryPowerGradeAlbums() if powergrade else gallery.GetGalleryStillAlbums()) or [])


def album_names(gallery, powergrade):
    return [gallery.GetAlbumName(album) for album in albums_of(gallery, powergrade)]


def find_album(gallery, name, powergrade=False):
    """The album with this exact name, else '@N': the Nth album as 'dava gallery list' numbers them."""
    kind = album_kind(powergrade)
    albums = albums_of(gallery, powergrade)
    names = [gallery.GetAlbumName(album) for album in albums]
    matches = [number for number, each in enumerate(names, start=1) if each == name]
    if len(matches) == 1:
        return albums[matches[0] - 1]
    if matches:
        numbers = ", ".join(f"@{number}" for number in matches)
        raise ResolveError(f"{len(matches)} {kind}s are named {name!r}; pick one by number: {numbers}.")
    digits = name[1:]
    if name.startswith("@") and digits.isascii() and digits.isdigit():
        if 1 <= int(digits) <= len(albums):
            return albums[int(digits) - 1]
        raise ResolveError(f"There are {len(albums)} {kind}(s); there is no {kind} {name}.")
    listed = ", ".join(repr(each) for each in names) or "none"
    raise ResolveError(f"No {kind} named {name!r} ({kind}s: {listed}). See 'dava gallery list'.")


def pick_album(resolve, args, how="-a NAME"):
    """(album, name) for the album option and --powergrade; the current still album by default.

    how: how this command names an album, for the error messages.
    """
    gallery = project_gallery(resolve)
    if args.album is not None:
        album = find_album(gallery, args.album, args.powergrade)
    elif args.powergrade:
        raise ResolveError(f"Name the PowerGrade album with {how} (see 'dava gallery list -k powergrade').")
    else:
        album = gallery.GetCurrentStillAlbum()
        if not album:
            raise ResolveError(f"There is no current still album; name one with {how} "
                               "or choose one with 'dava gallery use NAME'.")
    return album, gallery.GetAlbumName(album)


def pick_stills(album, name, numbers):
    """The stills with these 1-based numbers (duplicates dropped), as 'dava gallery show' numbers them."""
    stills = list(album.GetStills() or [])
    for number in numbers:
        if number > len(stills):
            raise ResolveError(f"Still {number} is out of range: album {name!r} has {len(stills)} still(s). "
                               "See 'dava gallery show'.")
    return [stills[number - 1] for number in dict.fromkeys(numbers)]


# ---------------------------------------------------------------------------
# node: any node graph


def cmd_node_list(resolve, args):
    rows = []
    for where, graph in graph_targets(resolve, args):
        for node in range(1, node_count(graph, where) + 1):
            rows.append({**where, "node": node, "label": graph.GetNodeLabel(node), "lut": graph.GetLUT(node),
                         "tools": ", ".join(graph.GetToolsInNode(node) or []),
                         "cache": cache_word(resolve, graph.GetNodeCacheMode(node))})
    return rows


def cmd_node_lut(resolve, args):
    targets = graph_targets(resolve, args)
    check_nodes(targets, [args.node])
    # SetLUT only accepts LUTs Resolve has already discovered.
    if not current_project(resolve).RefreshLUTList():
        raise ResolveError("Resolve could not refresh its LUT list.")
    rows = []
    for where, graph in targets:
        if not graph.SetLUT(args.node, args.lut):
            raise failure(where, f"set LUT {args.lut!r} on node {args.node}", len(rows), LUT_HINT)
        rows.append({**where, "node": args.node, "lut": args.lut})
    return rows


def set_nodes_enabled(resolve, args, enabled):
    targets = graph_targets(resolve, args)
    nodes = list(dict.fromkeys(args.nodes))
    check_nodes(targets, nodes)
    rows = []
    for where, graph in targets:
        for node in nodes:
            if not graph.SetNodeEnabled(node, enabled):
                raise failure(where, f"{'enable' if enabled else 'disable'} node {node}", len(rows))
            rows.append({**where, "node": node, "enabled": enabled})
    return rows


def cmd_node_enable(resolve, args):
    return set_nodes_enabled(resolve, args, True)


def cmd_node_disable(resolve, args):
    return set_nodes_enabled(resolve, args, False)


def cmd_node_cache(resolve, args):
    targets = graph_targets(resolve, args)
    check_nodes(targets, [args.node])
    if args.mode is None:
        return [{**where, "node": args.node, "cache": cache_word(resolve, graph.GetNodeCacheMode(args.node))}
                for where, graph in targets]
    value = cache_constant(resolve, args.mode)
    rows = []
    for where, graph in targets:
        if not graph.SetNodeCacheMode(args.node, value):
            raise failure(where, f"set the cache mode of node {args.node} to {args.mode}", len(rows))
        rows.append({**where, "node": args.node, "cache": args.mode})
    return rows


def cmd_node_apply_drx(resolve, args):
    path = os.path.abspath(args.path)
    if not os.path.isfile(path):
        raise ResolveError(f"DRX file not found: {path}")
    rows = []
    for where, graph in graph_targets(resolve, args):
        if not graph.ApplyGradeFromDRX(path, DRX_GRADE_MODES[args.keyframes]):
            raise failure(where, f"apply the grade of {path}", len(rows))
        rows.append({**where, "drx": path, "keyframes": args.keyframes})
    return rows


def cmd_node_reset_grades(resolve, args):
    rows = []
    for where, graph in graph_targets(resolve, args):
        if not graph.ResetAllGrades():
            raise failure(where, "reset all grades", len(rows))
        rows.append({**where, "reset": "all grades"})
    return rows


def cmd_node_arri_cdl_lut(resolve, args):
    rows = []
    for where, graph in graph_targets(resolve, args):
        if not graph.ApplyArriCdlLut():
            raise failure(where, "apply the ARRI CDL and LUT", len(rows))
        rows.append({**where, "applied": "ARRI CDL and LUT"})
    return rows


def cmd_node_reset_colors(resolve, args):
    rows = []
    for where, item in clip_targets(resolve, args):
        if not item.ResetAllNodeColors():
            raise failure(where, "reset the node colors", len(rows))
        rows.append({**where, "reset": "node colors"})
    return rows


# ---------------------------------------------------------------------------
# color: grade copies and the LUT list


def cmd_color_copy(resolve, args):
    sources = select_items(resolve, Namespace(timeline=args.timeline, track=args.track, index=None))
    if not 1 <= args.index <= len(sources):
        raise ResolveError(f"Clip index {args.index} is out of range; track {args.track} has {len(sources)} clip(s).")
    source = sources[args.index - 1][1]
    track = args.to_track or args.track
    same_track = track == args.track
    candidates = sources if same_track else select_items(
        resolve, Namespace(timeline=args.timeline, track=track, index=None))
    if args.to_all:
        numbers = [number for number, _ in candidates if not (same_track and number == args.index)]
    else:
        numbers = list(dict.fromkeys(args.to))
        bad = [number for number in numbers if not 1 <= number <= len(candidates)]
        if bad:
            raise ResolveError(f"Target clip {bad[0]} is out of range; track {track} has {len(candidates)} clip(s).")
        if same_track and args.index in numbers:
            raise ResolveError(f"Clip {args.index} is the source; leave it out of --to.")
    if not numbers:
        raise ResolveError(f"Track {track} has no other clip to copy the grade to.")
    name = source.GetName()
    if not source.CopyGrades([candidates[number - 1][1] for number in numbers]):
        raise ResolveError(f"Resolve could not copy the grade of clip {args.index} {name!r} to clip(s) "
                           f"{', '.join(map(str, numbers))} of track {track}.")
    return {"from": args.index, "name": name, "track": track, "to": numbers}


def cmd_color_refresh_luts(resolve, args):
    if not current_project(resolve).RefreshLUTList():
        raise ResolveError("Resolve could not refresh its LUT list.")
    return "Refreshed the LUT list"


# ---------------------------------------------------------------------------
# version: grade versions of timeline clips


def current_version(item, where):
    info = item.GetCurrentVersion()
    if not info:
        raise ResolveError(f"{describe(where)}: Resolve reported no current grade version.")
    kind = info.get("versionType")
    return {"version": info.get("versionName"), "type": VERSION_NAMES.get(kind, kind)}


def version_kind(args):
    return "remote" if args.remote else "local"


def cmd_version_list(resolve, args):
    rows = []
    for where, item in clip_targets(resolve, args):
        current = current_version(item, where)
        for kind, code in VERSION_TYPES.items():
            for name in item.GetVersionNameList(code) or []:
                loaded = (name, kind) == (current["version"], current["type"])
                rows.append({**where, "type": kind, "version": name, "current": "*" if loaded else ""})
    return rows


def cmd_version_current(resolve, args):
    return [{**where, **current_version(item, where)} for where, item in clip_targets(resolve, args)]


def cmd_version_add(resolve, args):
    kind = version_kind(args)
    rows = []
    for where, item in clip_targets(resolve, args):
        if not item.AddVersion(args.version, VERSION_TYPES[kind]):
            raise failure(where, f"add {kind} version {args.version!r}", len(rows),
                          " Is the name already used? See 'dava version list'.")
        rows.append({**where, "type": kind, "version": args.version})
    return rows


def cmd_version_load(resolve, args):
    kind = version_kind(args)
    rows = []
    for where, item in clip_targets(resolve, args):
        if not item.LoadVersionByName(args.version, VERSION_TYPES[kind]):
            raise failure(where, f"load {kind} version {args.version!r}", len(rows),
                          " See 'dava version list' for the names.")
        rows.append({**where, "type": kind, "version": args.version})
    return rows


def cmd_version_delete(resolve, args):
    kind = version_kind(args)
    rows = []
    for where, item in clip_targets(resolve, args):
        if not item.DeleteVersionByName(args.version, VERSION_TYPES[kind]):
            raise failure(where, f"delete {kind} version {args.version!r}", len(rows),
                          " Check the name with 'dava version list'; if it is the loaded version, "
                          "load another one first ('dava version load').")
        rows.append({**where, "type": kind, "deleted": args.version})
    return rows


def cmd_version_rename(resolve, args):
    kind = version_kind(args)
    rows = []
    for where, item in clip_targets(resolve, args):
        if not item.RenameVersionByName(args.old, args.new, VERSION_TYPES[kind]):
            raise failure(where, f"rename {kind} version {args.old!r} to {args.new!r}", len(rows),
                          " Check the names with 'dava version list'.")
        rows.append({**where, "type": kind, "old": args.old, "version": args.new})
    return rows


# ---------------------------------------------------------------------------
# colorgroup: color groups and their clips


def cmd_colorgroup_list(resolve, args):
    groups = current_project(resolve).GetColorGroupsList() or []
    return [{"number": number, "group": group.GetName()} for number, group in enumerate(groups, start=1)]


def cmd_colorgroup_create(resolve, args):
    project = current_project(resolve)
    if any(group.GetName() == args.name for group in project.GetColorGroupsList() or []):
        raise ResolveError(f"A color group named {args.name!r} already exists.")
    group = project.AddColorGroup(args.name)
    if not group:
        raise ResolveError(f"Resolve could not create color group {args.name!r}.")
    return describe_object(args.session, group, "ColorGroup")


def cmd_colorgroup_delete(resolve, args):
    project = current_project(resolve)
    group = find_group(project, args.name)
    if not project.DeleteColorGroup(group):
        raise ResolveError(f"Resolve could not delete color group {args.name!r}.")
    return f"Deleted color group {args.name}; its clips are now ungrouped"


def cmd_colorgroup_rename(resolve, args):
    project = current_project(resolve)
    group = find_group(project, args.old)
    if any(each.GetName() == args.new for each in project.GetColorGroupsList() or []):
        raise ResolveError(f"A color group named {args.new!r} already exists.")
    if not group.SetName(args.new):
        raise ResolveError(f"Resolve could not rename color group {args.old!r} to {args.new!r}.")
    return f"Renamed color group {args.old} to {args.new}"


def cmd_colorgroup_assign(resolve, args):
    group = find_group(current_project(resolve), args.group)
    rows = []
    for where, item in clip_targets(resolve, args):
        if not item.AssignToColorGroup(group):
            raise failure(where, f"assign the clip to color group {args.group!r}", len(rows))
        rows.append({**where, "group": args.group})
    return rows


def cmd_colorgroup_unassign(resolve, args):
    rows, changed = [], 0
    for where, item in clip_targets(resolve, args):
        group = item.GetColorGroup()
        if not group:
            rows.append({**where, "group": "", "removed": False})
            continue
        name = group.GetName()
        if not item.RemoveFromColorGroup():
            raise failure(where, f"remove the clip from color group {name!r}", changed)
        changed += 1
        rows.append({**where, "group": name, "removed": True})
    return rows


def cmd_colorgroup_show(resolve, args):
    rows = []
    for where, item in clip_targets(resolve, args):
        group = item.GetColorGroup()
        rows.append({**where, "group": group.GetName() if group else ""})
    return rows


def cmd_colorgroup_clips(resolve, args):
    project = current_project(resolve)
    group = find_group(project, args.name)
    timeline = find_timeline(project, args.timeline)
    rows = []
    for item in group.GetClipsInTimeline(timeline) or []:
        info = describe_object(args.session, item, "TimelineItem")
        track_type, track = (list(item.GetTrackTypeAndIndex() or []) + [None, None])[:2]
        rows.append({"name": info["name"], "track_type": track_type, "track": track,
                     "start": item.GetStart(), "$ref": info["$ref"]})
    return rows


# ---------------------------------------------------------------------------
# gallery: still and PowerGrade albums


def cmd_gallery_list(resolve, args):
    gallery = project_gallery(resolve)
    current = gallery.GetCurrentStillAlbum()
    current_name = gallery.GetAlbumName(current) if current else None
    rows = []
    for kind in ("still", "powergrade"):
        if args.kind not in (None, kind):
            continue
        for number, album in enumerate(albums_of(gallery, kind == "powergrade"), start=1):
            name = gallery.GetAlbumName(album)
            rows.append({"kind": kind, "number": number, "album": name, "stills": len(album.GetStills() or []),
                         "current": "*" if kind == "still" and name == current_name else ""})
    return rows


def cmd_gallery_show(resolve, args):
    album, name = pick_album(resolve, args, how="the ALBUM argument")
    return [{"album": name, "still": number, "label": album.GetLabel(still)}
            for number, still in enumerate(album.GetStills() or [], start=1)]


def cmd_gallery_create(resolve, args):
    gallery = project_gallery(resolve)
    kind = album_kind(args.powergrade)
    if args.name is not None and args.name in album_names(gallery, args.powergrade):
        raise ResolveError(f"A {kind} named {args.name!r} already exists.")
    album = gallery.CreateGalleryPowerGradeAlbum() if args.powergrade else gallery.CreateGalleryStillAlbum()
    if not album:
        raise ResolveError(f"Resolve could not create a {kind}.")
    if args.name is not None and not gallery.SetAlbumName(album, args.name):
        raise ResolveError(f"Created {kind} {gallery.GetAlbumName(album)!r}, but Resolve refused to "
                           f"rename it to {args.name!r}.")
    name = gallery.GetAlbumName(album)
    result = {"kind": "powergrade" if args.powergrade else "still", "album": name}
    # A name ref only when it names this album alone (see 'dava api refs').
    if isinstance(name, str) and "::" not in name and album_names(gallery, args.powergrade).count(name) == 1:
        result["$ref"] = f"{'powergrade' if args.powergrade else 'album'}:{name}"
    return result


def cmd_gallery_rename(resolve, args):
    gallery = project_gallery(resolve)
    kind = album_kind(args.powergrade)
    album = find_album(gallery, args.album, args.powergrade)
    old = gallery.GetAlbumName(album)
    if args.new in album_names(gallery, args.powergrade):
        raise ResolveError(f"A {kind} named {args.new!r} already exists.")
    if not gallery.SetAlbumName(album, args.new):
        raise ResolveError(f"Resolve could not rename {kind} {old!r} to {args.new!r}.")
    return f"Renamed {kind} {old} to {args.new}"


def cmd_gallery_use(resolve, args):
    gallery = project_gallery(resolve)
    album = find_album(gallery, args.album, args.powergrade)
    name = gallery.GetAlbumName(album)
    if not gallery.SetCurrentStillAlbum(album):
        raise ResolveError(f"Resolve could not make {album_kind(args.powergrade)} {name!r} the current album.")
    return f"Current album: {name}"


# ---------------------------------------------------------------------------
# still: labels, deleting and importing stills


def cmd_still_label(resolve, args):
    album, name = pick_album(resolve, args)
    (still,) = pick_stills(album, name, [args.number])
    if not album.SetLabel(still, args.label):
        raise ResolveError(f"Resolve could not set the label of still {args.number} in album {name!r}.")
    return {"album": name, "still": args.number, "label": args.label}


def cmd_still_delete(resolve, args):
    if bool(args.numbers) == args.all:
        raise ResolveError("Give the numbers of the stills to delete, or --all (not both). See 'dava gallery show'.")
    album, name = pick_album(resolve, args)
    if args.all:
        stills = list(album.GetStills() or [])
        if not stills:
            raise ResolveError(f"Album {name!r} has no stills.")
    else:
        stills = pick_stills(album, name, args.numbers)
    if not album.DeleteStills(stills):
        raise ResolveError(f"Resolve could not delete {len(stills)} still(s) from album {name!r}.")
    return {"album": name, "deleted": len(stills)}


def cmd_still_import(resolve, args):
    paths = [os.path.abspath(path) for path in args.paths]
    missing = [path for path in paths if not os.path.isfile(path)]
    if missing:
        raise ResolveError(f"File(s) not found: {', '.join(missing)}")
    album, name = pick_album(resolve, args)
    before = len(album.GetStills() or [])
    if not album.ImportStills(paths):
        raise ResolveError(f"Resolve could not import {len(paths)} file(s) into album {name!r}; "
                           "check that Resolve can read them as gallery stills.")
    after = len(album.GetStills() or [])
    return {"album": name, "files": len(paths), "added": after - before, "stills": after}


# ---------------------------------------------------------------------------
# Parsers


def add_graph_args(p):
    """Options that pick node graphs: clip graphs (-t/-T/-i, -L), the timeline graph or a color group graph."""
    p.add_argument("-G", "--graph", choices=GRAPHS, default="clip",
                   help="which node graph: clip = the graphs of the clips picked by -T/-i (default); "
                        "timeline = the timeline's own graph (of -t); pre / post = the pre-clip / "
                        "post-clip graph of the color group --group")
    p.add_argument("--group", metavar="NAME", help="color group for --graph pre/post (see 'dava colorgroup list')")
    p.add_argument("-L", "--layer", type=positive_int,
                   help="node stack layer of clip graphs, from 1 (default: the first layer)")
    add_item_args(p)


def _album_args(p, positional=False):
    if positional:
        p.add_argument("album", nargs="?", metavar="ALBUM",
                       help="album name, or @N for the Nth album of 'dava gallery list' "
                            "(default: the current still album)")
    else:
        p.add_argument("-a", "--album", help="album name, or @N for the Nth album of 'dava gallery list' "
                                             "(default: the current still album)")
    p.add_argument("--powergrade", action="store_true", help="the album is a PowerGrade album")


def _register_node(registry):
    group_help = ("node graphs of clips (any layer), the timeline and color groups: nodes, LUTs, "
                  "enable/disable, cache, DRX grades, reset")
    p = registry.action("node", "list", "list the nodes of node graphs: label, LUT, tools and cache mode",
                        group_help=group_help)
    add_graph_args(p)
    p.set_defaults(func=cmd_node_list, read_only=True)

    p = registry.action("node", "lut", "set a LUT on a node (refreshes Resolve's LUT list first)")
    p.add_argument("node", type=positive_int, help="node number, from 1")
    p.add_argument("lut", help="LUT path, absolute or relative to Resolve's LUT folders")
    add_graph_args(p)
    p.set_defaults(func=cmd_node_lut, read_only=False)

    p = registry.action("node", "enable", "enable nodes")
    p.add_argument("nodes", nargs="+", type=positive_int, metavar="NODE", help="node numbers, from 1")
    add_graph_args(p)
    p.set_defaults(func=cmd_node_enable, read_only=False)

    p = registry.action("node", "disable", "disable (bypass) nodes")
    p.add_argument("nodes", nargs="+", type=positive_int, metavar="NODE", help="node numbers, from 1")
    add_graph_args(p)
    p.set_defaults(func=cmd_node_disable, read_only=False)

    p = registry.action("node", "cache", "show or set the cache mode of a node")
    p.add_argument("node", type=positive_int, help="node number, from 1")
    p.add_argument("mode", nargs="?", choices=list(CACHE_MODES),
                   help="auto, on or off (resolve.CACHE_AUTO_ENABLED / CACHE_ENABLED / CACHE_DISABLED); "
                        "omit to show the current mode")
    add_graph_args(p)
    p.set_defaults(func=cmd_node_cache, read_only=lambda a: a.mode is None)

    p = registry.action("node", "apply-drx", "apply the grade of a .drx still file to node graphs "
                                             "(also color group and timeline graphs)")
    p.add_argument("path", help=".drx file")
    p.add_argument("-k", "--keyframes", choices=list(DRX_GRADE_MODES), default="none",
                   help="keyframe alignment: none, source-tc (source timecode) or start-frames (default: none)")
    add_graph_args(p)
    p.set_defaults(func=cmd_node_apply_drx, read_only=False)

    p = registry.action("node", "reset-grades", "reset all grades of node graphs (clip, timeline or color group)")
    add_graph_args(p)
    p.set_defaults(func=cmd_node_reset_grades, read_only=False)

    p = registry.action("node", "arri-cdl-lut", "apply the ARRI CDL and LUT to node graphs (Graph.ApplyArriCdlLut)")
    add_graph_args(p)
    p.set_defaults(func=cmd_node_arri_cdl_lut, read_only=False)

    p = registry.action("node", "reset-colors", "reset the node colors of every node in the clips' active version")
    add_item_args(p)
    p.set_defaults(func=cmd_node_reset_colors, read_only=False)


def _register_color(registry):
    p = registry.action("color", "copy", "copy a clip's grade (current node stack layer) to other clips")
    add_item_args(p, single=True)
    targets = p.add_mutually_exclusive_group(required=True)
    targets.add_argument("--to", nargs="+", type=positive_int, metavar="INDEX",
                         help="clip numbers to copy to, on --to-track (default: the same track)")
    targets.add_argument("--to-all", action="store_true", help="copy to every other clip of --to-track")
    p.add_argument("--to-track", type=positive_int, metavar="TRACK",
                   help="video track of the target clips (default: the source clip's track)")
    p.set_defaults(func=cmd_color_copy, read_only=False)

    p = registry.action("color", "refresh-luts", "rescan the LUT folders so newly added LUT files can be used")
    p.set_defaults(func=cmd_color_refresh_luts, read_only=False)


def _register_version(registry):
    group_help = "grade versions of timeline clips (local or remote)"
    p = registry.action("version", "list", "list the local and remote grade versions of clips (* = loaded)",
                        group_help=group_help)
    add_item_args(p)
    p.set_defaults(func=cmd_version_list, read_only=True)

    p = registry.action("version", "current", "show the loaded grade version of clips")
    add_item_args(p)
    p.set_defaults(func=cmd_version_current, read_only=True)

    for name, func, help_text in (
        ("add", cmd_version_add, "add a grade version to clips"),
        ("load", cmd_version_load, "load a grade version by name (makes it the active grade)"),
        ("delete", cmd_version_delete, "delete a grade version by name"),
    ):
        p = registry.action("version", name, help_text)
        p.add_argument("version", help="version name")
        p.add_argument("-r", "--remote", action="store_true", help="a remote version (default: local)")
        add_item_args(p)
        p.set_defaults(func=func, read_only=False)

    p = registry.action("version", "rename", "rename a grade version")
    p.add_argument("old", help="current version name (see 'dava version list')")
    p.add_argument("new", help="new version name")
    p.add_argument("-r", "--remote", action="store_true", help="a remote version (default: local)")
    add_item_args(p)
    p.set_defaults(func=cmd_version_rename, read_only=False)


def _register_colorgroup(registry):
    group_help = ("color groups: create, rename, delete, assign clips (grade a group with "
                  "'dava node ... --graph pre|post --group NAME')")
    p = registry.action("colorgroup", "list", "list the color groups of the project", group_help=group_help)
    p.set_defaults(func=cmd_colorgroup_list, read_only=True)

    p = registry.action("colorgroup", "create", "create a color group")
    p.add_argument("name", help="name of the new group (must not be taken)")
    p.set_defaults(func=cmd_colorgroup_create, read_only=False)

    p = registry.action("colorgroup", "delete", "delete a color group (its clips become ungrouped)")
    p.add_argument("name", help="color group name (see 'dava colorgroup list')")
    p.set_defaults(func=cmd_colorgroup_delete, read_only=False)

    p = registry.action("colorgroup", "rename", "rename a color group")
    p.add_argument("old", help="current color group name (see 'dava colorgroup list')")
    p.add_argument("new", help="new name (must not be taken)")
    p.set_defaults(func=cmd_colorgroup_rename, read_only=False)

    p = registry.action("colorgroup", "assign", "put clips into a color group")
    p.add_argument("group", help="color group name")
    add_item_args(p)
    p.set_defaults(func=cmd_colorgroup_assign, read_only=False)

    p = registry.action("colorgroup", "unassign", "take clips out of their color group (ungrouped clips are skipped)")
    add_item_args(p)
    p.set_defaults(func=cmd_colorgroup_unassign, read_only=False)

    p = registry.action("colorgroup", "show", "show the color group of clips")
    add_item_args(p)
    p.set_defaults(func=cmd_colorgroup_show, read_only=True)

    p = registry.action("colorgroup", "clips", "list the clips of a color group on a timeline")
    p.add_argument("name", help="color group name")
    p.add_argument("-t", "--timeline", help="timeline name (default: current)")
    p.set_defaults(func=cmd_colorgroup_clips, read_only=True)


def _register_gallery(registry):
    group_help = "gallery still and PowerGrade albums"
    p = registry.action("gallery", "list", "list still and PowerGrade albums (* = current still album)",
                        group_help=group_help)
    p.add_argument("-k", "--kind", choices=["still", "powergrade"], help="only albums of this kind")
    p.set_defaults(func=cmd_gallery_list, read_only=True)

    p = registry.action("gallery", "show", "list the stills of an album with their numbers and labels")
    _album_args(p, positional=True)
    p.set_defaults(func=cmd_gallery_show, read_only=True)

    p = registry.action("gallery", "create", "create a still album (or --powergrade album)")
    p.add_argument("name", nargs="?", help="album name (default: the name Resolve gives it)")
    p.add_argument("--powergrade", action="store_true", help="create a PowerGrade album")
    p.set_defaults(func=cmd_gallery_create, read_only=False)

    p = registry.action("gallery", "rename", "rename an album")
    p.add_argument("album", help="album name, or @N for the Nth album of 'dava gallery list'")
    p.add_argument("new", help="new name")
    p.add_argument("--powergrade", action="store_true", help="the album is a PowerGrade album")
    p.set_defaults(func=cmd_gallery_rename, read_only=False)

    p = registry.action("gallery", "use", "make an album the current still album")
    p.add_argument("album", help="album name, or @N for the Nth album of 'dava gallery list'")
    p.add_argument("--powergrade", action="store_true", help="the album is a PowerGrade album")
    p.set_defaults(func=cmd_gallery_use, read_only=False)


def _register_still(registry):
    p = registry.action("still", "label", "set the label of a still")
    p.add_argument("number", type=positive_int, help="still number, from 1 (see 'dava gallery show')")
    p.add_argument("label", help="new label text")
    _album_args(p)
    p.set_defaults(func=cmd_still_label, read_only=False)

    p = registry.action("still", "delete", "delete stills from an album")
    p.add_argument("numbers", nargs="*", type=positive_int, metavar="NUMBER",
                   help="still numbers, from 1 (see 'dava gallery show')")
    p.add_argument("--all", action="store_true", help="delete every still of the album")
    _album_args(p)
    p.set_defaults(func=cmd_still_delete, read_only=False)

    p = registry.action("still", "import", "import still files into an album")
    p.add_argument("paths", nargs="+", metavar="PATH", help="still files, e.g. ones written by 'dava still export'")
    _album_args(p)
    p.set_defaults(func=cmd_still_import, read_only=False)


def register(registry):
    _register_node(registry)
    _register_color(registry)
    _register_version(registry)
    _register_colorgroup(registry)
    _register_gallery(registry)
    _register_still(registry)
