"""Generic access to every Resolve API method: object references, argument coercion, results.

A *reference* names a live Resolve object in a string, so that a caller with no Python
access (a shell, an AI agent, an MCP client) can point at it. References are resolved
fresh on every call, so the stable ones survive across separate dava invocations.
"""

import contextlib
import difflib
import json
import re

from .connect import ResolveError, get_resolve
from .spec import is_read_only_name, load_spec

REF_HELP = """\
A REF names a Resolve object. Stable refs work across separate dava runs; $handles last one session
(one batch file or one MCP server run).

Top-level refs (all within the current project):
  resolve | projectmanager (pm) | project | mediapool | mediastorage | gallery | fusion
  timeline                current timeline      timeline:NAME | timeline#ID | timeline@N (1-based)
  clip:NAME               media pool clip by name (must be unique); clip:/Master/Sub/NAME by path; clip#ID
  folder                  current media pool folder; folder:/ (root); folder:/Master/Sub; folder#ID
  item                    current video item of the current timeline
  item:TYPE:TRACK:N       Nth clip (1-based) on track TRACK of TYPE video|audio|subtitle, current timeline
  item#ID                 timeline item by unique id (searches the current timeline first, then all)
  colorgroup:NAME         color group (colorgroup@N by position)
  album | album:NAME      current / named gallery still album (album@N by position)
  powergrade:NAME         PowerGrade album (powergrade@N by position)
  $NAME                   a session handle (from a result, or saved with --as NAME)

Child segments, joined with '::':
  timeline...::item:TYPE:TRACK:N | ::item#ID | ::item (current video item) | ::graph
  item...::graph | ::graph:LAYER | ::comp:NAME | ::comp@N | ::clip (its media pool item)
          (::comp:2 means the composition named "2" if there is one, else the 2nd composition)
  colorgroup:NAME::pregraph | ::postgraph
  album...::still:N
  folder...::clip:NAME | ::folder:NAME
  fusion::comp            the composition open in the Fusion page
  item...::comp           first Fusion composition of the item (same as ::comp@1)
  ...comp...::tool:NAME   a tool in a composition, e.g. item:video:2:1::comp::tool:Template
  ...tool...::input:NAME  an input of a Fusion tool, e.g. ...::tool:Text1::input:StyledText

Names are matched exactly, including leading and trailing spaces. A name containing '::' can only be
reached by #ID or @N forms.

Examples:  timeline:Edit v1::item:video:1:3::graph    colorgroup:Warm::postgraph    album:Stills 1::still:2

Fusion objects (compositions, tools, inputs) are not described by the API definition: call their
methods with 'dava call' (checked only against what the live object has) and list them with
'dava inspect REF'. Useful Fusion methods: comp FindTool, GetToolList, AddTool, Lock, Unlock,
StartUndo, EndUndo, Execute; tool SetInput, GetInput, GetInputList, AddModifier, ConnectInput, GetAttrs.

Arguments are coerced to the parameter types in the API definition ('dava api show Class.Method'):
  object parameters take a REF string (or {"$ref": REF}); lists of objects take a JSON list of REFs;
  resolve.* constants take their name, e.g. "EXPORT_AAF"; typed dicts take a JSON object whose object
  fields are REFs, e.g. {"mediaPoolItem": "clip:a.mov", "startFrame": 0}.
Results that are Resolve objects come back as {"$ref": ..., "$type": ...}; pass the $ref back in.
"""

# Classes that are singletons of the session, addressable by a bare keyword.
SINGLETONS = {
    "resolve": "Resolve",
    "projectmanager": "ProjectManager",
    "pm": "ProjectManager",
    "project": "Project",
    "mediapool": "MediaPool",
    "mediastorage": "MediaStorage",
    "gallery": "Gallery",
    "fusion": "Fusion",
}

TRACK_TYPES = ("video", "audio", "subtitle")

# Fusion scripting API methods used here; they are not in the Resolve API definition.
FUSION_API_METHODS = {"GetCurrentComp", "FindTool", "GetAttrs"}


def iter_clips(folder, prefix=""):
    """Yield (folder path, MediaPoolItem) for every clip under folder, depth first."""
    path = f"{prefix}/{folder.GetName()}" if prefix else folder.GetName()
    for clip in folder.GetClipList() or []:
        yield path, clip
    for sub in folder.GetSubFolderList() or []:
        yield from iter_clips(sub, path)


def iter_folders(folder, prefix=""):
    """Yield (folder path, Folder) for folder and every folder below it."""
    path = f"{prefix}/{folder.GetName()}" if prefix else folder.GetName()
    yield path, folder
    for sub in folder.GetSubFolderList() or []:
        yield from iter_folders(sub, path)


class Session:
    """State shared by the calls of one dava run: the connection, the API spec and handles."""

    def __init__(self, resolve=None, spec=None):
        self._resolve = resolve
        self._spec = spec
        self.handles = {}  # name -> (object, class name)
        self._counter = 0
        self._cache = None  # id/name indexes shared by the refs of one call

    @contextlib.contextmanager
    def lookup_cache(self):
        """Share one clip/item index across every REF resolved inside the block.

        Scoped to one call on purpose: batch and MCP sessions span mutating calls, and a
        longer-lived index would hand out deleted objects.
        """
        outer = self._cache
        if outer is None:
            self._cache = {}
        try:
            yield
        finally:
            if outer is None:
                self._cache = None

    def cached(self, key, build):
        if self._cache is None:
            return build()
        if key not in self._cache:
            self._cache[key] = build()
        return self._cache[key]

    @property
    def resolve(self):
        if self._resolve is None:
            self._resolve = get_resolve()
        return self._resolve

    @property
    def spec(self):
        if self._spec is None:
            self._spec = load_spec()
        return self._spec

    def new_handle(self, obj, cls):
        self._counter += 1
        while f"h{self._counter}" in self.handles:
            self._counter += 1
        name = f"h{self._counter}"
        self.handles[name] = (obj, cls)
        return name

    # -- lookups ---------------------------------------------------------------

    def project(self):
        project = self.resolve.GetProjectManager().GetCurrentProject()
        if not project:
            raise ResolveError("No project is open. Use 'dava project open NAME'.")
        return project

    def timelines(self):
        project = self.project()
        return [project.GetTimelineByIndex(i) for i in range(1, project.GetTimelineCount() + 1)]

    def current_timeline(self):
        timeline = self.project().GetCurrentTimeline()
        if not timeline:
            raise ResolveError("The project has no current timeline.")
        return timeline

    def root_folder(self):
        return self.project().GetMediaPool().GetRootFolder()


# ---------------------------------------------------------------------------
# Reference resolution


def _split_key(segment):
    """'timeline:Edit v1' -> ('timeline', ':', 'Edit v1'); 'clip#ab' -> ('clip', '#', 'ab').

    Only the keyword is trimmed; the name after the separator is kept exactly (names may end in spaces).
    """
    for index, char in enumerate(segment):
        if char in ":#@":
            return segment[:index].strip().lower(), char, segment[index + 1:]
    return segment.strip().lower(), "", ""


def _one(matches, what, hint="use its #ID ref instead"):
    if not matches:
        raise ResolveError(f"No {what}.")
    if len(matches) > 1:
        raise ResolveError(f"More than one {what}; {hint}.")
    return matches[0]


def _nth(items, text, what):
    items = list(items or [])
    number = _index(text, f"{what} index")
    if number > len(items):
        raise ResolveError(f"There are {len(items)} {what}(s); no {what} {number}.")
    return items[number - 1]


def _index(text, what):
    try:
        value = int(text)
    except ValueError as exc:
        raise ResolveError(f"{what} must be a number, got {text!r}.") from exc
    if value < 1:
        raise ResolveError(f"{what} is 1-based, got {value}.")
    return value


def _track_item(timeline, spec_text):
    parts = spec_text.split(":")
    if len(parts) != 3 or parts[0].lower() not in TRACK_TYPES:
        raise ResolveError(f"Expected item:TYPE:TRACK:N with TYPE video|audio|subtitle, got item:{spec_text!r}.")
    track_type, track, number = parts[0].lower(), _index(parts[1], "Track"), _index(parts[2], "Clip number")
    items = timeline.GetItemListInTrack(track_type, track) or []
    if number > len(items):
        raise ResolveError(
            f"{track_type} track {track} of {timeline.GetName()!r} has {len(items)} item(s); no item {number}."
        )
    return items[number - 1]


def _items_of(timeline):
    for track_type in TRACK_TYPES:
        for track in range(1, (timeline.GetTrackCount(track_type) or 0) + 1):
            yield from timeline.GetItemListInTrack(track_type, track) or []


def _item_index(session, timeline):
    return session.cached(("items", timeline.GetUniqueId()),
                          lambda: {item.GetUniqueId(): item for item in _items_of(timeline)})


def _item_by_id(session, unique_id, timeline=None):
    if timeline is not None:
        found = _item_index(session, timeline).get(unique_id)
        if found is None:
            raise ResolveError(f"No item with id {unique_id!r} on timeline {timeline.GetName()!r}.")
        return found
    current = session.project().GetCurrentTimeline()
    current_id = current.GetUniqueId() if current else None
    if current:
        found = _item_index(session, current).get(unique_id)
        if found is not None:
            return found
    # Only walk the other timelines when the current one does not have it.
    for other in session.timelines():
        if other.GetUniqueId() == current_id:
            continue
        found = _item_index(session, other).get(unique_id)
        if found is not None:
            return found
    raise ResolveError(f"No timeline item with id {unique_id!r}.")


def _clip_entries(session):
    """[(folder name parts, clip)] for the whole media pool, built once per call."""
    def build():
        entries = []

        def walk(folder, parts):
            parts = parts + (folder.GetName(),)
            for clip in folder.GetClipList() or []:
                entries.append((parts, clip))
            for sub in folder.GetSubFolderList() or []:
                walk(sub, parts)

        walk(session.root_folder(), ())
        return entries

    return session.cached("clips", build)


def _folder_by_path(session, path):
    root = session.root_folder()
    parts = [p for p in path.split("/") if p]
    if parts and parts[0] == root.GetName():
        parts = parts[1:]
    folder = root
    for part in parts:
        folder = _one([f for f in folder.GetSubFolderList() or [] if f.GetName() == part],
                      f"folder {part!r} under {folder.GetName()!r}")
    return folder


def _albums(gallery, power_grade=False):
    return (gallery.GetGalleryPowerGradeAlbums() if power_grade else gallery.GetGalleryStillAlbums()) or []


def _album(gallery, name, power_grade=False):
    kind = "PowerGrade album" if power_grade else "still album"
    prefix = "powergrade" if power_grade else "album"
    return _one([a for a in _albums(gallery, power_grade) if gallery.GetAlbumName(a) == name],
                f"{kind} named {name!r}", hint=f"address it by position with {prefix}@N")


def _root(session, segment):
    if segment.startswith("$"):
        name = segment[1:]
        if name not in session.handles:
            known = ", ".join("$" + h for h in session.handles) or "none"
            raise ResolveError(f"Unknown handle ${name}. Handles in this session: {known}.")
        return session.handles[name]

    key, sep, rest = _split_key(segment)
    if key in SINGLETONS and not sep:
        cls = SINGLETONS[key]
        resolve = session.resolve
        getter = {
            "Resolve": lambda: resolve,
            "ProjectManager": resolve.GetProjectManager,
            "Project": session.project,
            "MediaPool": lambda: session.project().GetMediaPool(),
            "MediaStorage": resolve.GetMediaStorage,
            "Gallery": lambda: session.project().GetGallery(),
            "Fusion": resolve.Fusion,
        }[cls]
        return getter(), cls

    if key == "timeline":
        if not sep:
            return session.current_timeline(), "Timeline"
        if sep == ":":
            return _one([t for t in session.timelines() if t.GetName() == rest], f"timeline named {rest!r}"), "Timeline"
        if sep == "#":
            return _one([t for t in session.timelines() if t.GetUniqueId() == rest], f"timeline with id {rest!r}"), "Timeline"
        number = _index(rest, "Timeline index")
        timelines = session.timelines()
        if number > len(timelines):
            raise ResolveError(f"The project has {len(timelines)} timeline(s); no timeline@{number}.")
        return timelines[number - 1], "Timeline"

    if key == "clip" and sep == "#":
        index = session.cached("clip-ids", lambda: {c.GetUniqueId(): c for _, c in _clip_entries(session)})
        if rest not in index:
            raise ResolveError(f"No clip with id {rest!r}.")
        return index[rest], "MediaPoolItem"
    if key == "clip" and sep == ":" and rest.startswith("/"):
        folder_path, _, name = rest.rpartition("/")
        folder = _folder_by_path(session, folder_path)
        return _one([c for c in folder.GetClipList() or [] if c.GetName() == name],
                    f"clip {name!r} in {folder_path or '/'}"), "MediaPoolItem"
    if key == "clip" and sep == ":":
        matches = [(parts, c) for parts, c in _clip_entries(session) if c.GetName() == rest]
        if len(matches) > 1:
            if any("/" in part for parts, _ in matches for part in parts):
                where = "folders whose names contain '/'; use clip#ID"
            else:
                where = ", ".join("/".join(parts) for parts, _ in matches) + f"; use clip:/FOLDER/PATH/{rest} or clip#ID"
            raise ResolveError(f"Clip name {rest!r} is ambiguous (in {where}).")
        return _one([c for _, c in matches], f"media pool clip named {rest!r}"), "MediaPoolItem"

    if key == "folder":
        if not sep:
            return session.project().GetMediaPool().GetCurrentFolder(), "Folder"
        if sep == "#":
            folders = iter_folders(session.root_folder())
            return _one([f for _, f in folders if f.GetUniqueId() == rest], f"folder with id {rest!r}"), "Folder"
        if sep == ":":
            return _folder_by_path(session, rest), "Folder"

    if key == "item":
        if not sep:
            return _current_video_item(session.current_timeline()), "TimelineItem"
        if sep == "#":
            return _item_by_id(session, rest), "TimelineItem"
        if sep == ":":
            return _track_item(session.current_timeline(), rest), "TimelineItem"

    if key == "colorgroup" and sep in (":", "@"):
        groups = session.project().GetColorGroupsList() or []
        if sep == "@":
            return _nth(groups, rest, "color group"), "ColorGroup"
        return _one([g for g in groups if g.GetName() == rest], f"color group named {rest!r}",
                    hint="address it by position with colorgroup@N"), "ColorGroup"

    if key == "album":
        gallery = session.project().GetGallery()
        if not sep:
            return gallery.GetCurrentStillAlbum(), "GalleryStillAlbum"
        if sep == ":":
            return _album(gallery, rest), "GalleryStillAlbum"
        if sep == "@":
            return _nth(_albums(gallery), rest, "still album"), "GalleryStillAlbum"

    if key == "powergrade" and sep in (":", "@"):
        gallery = session.project().GetGallery()
        if sep == "@":
            return _nth(_albums(gallery, True), rest, "PowerGrade album"), "GalleryStillAlbum"
        return _album(gallery, rest, power_grade=True), "GalleryStillAlbum"

    raise ResolveError(f"Unrecognised reference {segment!r}. See 'dava api refs'.")


def _current_video_item(timeline):
    item = timeline.GetCurrentVideoItem()
    if not item:
        raise ResolveError(f"Timeline {timeline.GetName()!r} has no clip under the playhead.")
    return item


def _child(session, obj, cls, segment):
    key, sep, rest = _split_key(segment)
    if cls == "Timeline":
        if key == "item" and not sep:
            return _current_video_item(obj), "TimelineItem"
        if key == "item" and sep == ":":
            return _track_item(obj, rest), "TimelineItem"
        if key == "item" and sep == "#":
            return _item_by_id(session, rest, timeline=obj), "TimelineItem"
        if key == "graph" and not sep:
            return obj.GetNodeGraph(), "Graph"
    elif cls == "TimelineItem":
        if key == "graph" and sep in ("", ":"):
            graph = obj.GetNodeGraph(_index(rest, "Layer")) if sep == ":" else obj.GetNodeGraph()
            if not graph:
                raise ResolveError(f"Item {obj.GetName()!r} has no node graph{' at layer ' + rest if sep else ''}.")
            return graph, "Graph"
        if key == "comp" and sep == "@":
            comp = obj.GetFusionCompByIndex(_index(rest, "Composition index"))
            if not comp:
                raise ResolveError(f"Item {obj.GetName()!r} has no Fusion composition @{rest}.")
            return comp, "FusionComp"
        if key == "comp" and sep == ":":
            # A name wins; digits fall back to a 1-based index only when no composition has that name.
            if rest in (obj.GetFusionCompNameList() or []):
                comp = obj.GetFusionCompByName(rest)
            elif rest.isascii() and rest.isdigit():
                comp = obj.GetFusionCompByIndex(_index(rest, "Composition index"))
            else:
                comp = None
            if not comp:
                raise ResolveError(f"Item {obj.GetName()!r} has no Fusion composition {rest!r}.")
            return comp, "FusionComp"
        if key == "comp" and not sep:
            comp = obj.GetFusionCompByIndex(1)
            if not comp:
                raise ResolveError(f"Item {obj.GetName()!r} has no Fusion composition.")
            return comp, "FusionComp"
        if key == "clip" and not sep:
            clip = obj.GetMediaPoolItem()
            if not clip:
                raise ResolveError(f"Item {obj.GetName()!r} has no media pool item.")
            return clip, "MediaPoolItem"
    elif cls == "ColorGroup":
        if key == "pregraph" and not sep:
            return obj.GetPreClipNodeGraph(), "Graph"
        if key == "postgraph" and not sep:
            return obj.GetPostClipNodeGraph(), "Graph"
    elif cls == "GalleryStillAlbum":
        if key == "still" and sep == ":":
            stills = obj.GetStills() or []
            number = _index(rest, "Still number")
            if number > len(stills):
                raise ResolveError(f"The album has {len(stills)} still(s); no still {number}.")
            return stills[number - 1], "GalleryStill"
    elif cls == "Fusion":
        if key == "comp" and not sep:
            comp = obj.GetCurrentComp()
            if not comp:
                raise ResolveError("No Fusion composition is open in the Fusion page.")
            return comp, "FusionComp"
    elif cls == "FusionComp":
        if key == "tool" and sep == ":":
            tool = obj.FindTool(rest)
            if not tool:
                raise ResolveError(f"The composition has no tool named {rest!r}. "
                                   "List them with 'dava call REF GetToolList --unchecked'.")
            return tool, "FusionTool"
    elif cls == "FusionTool":
        if key == "input" and sep == ":":
            fusion_input = getattr(obj, rest, None)
            if fusion_input is None:
                raise ResolveError(f"The tool has no input {rest!r}.")
            return fusion_input, "FusionInput"
    elif cls == "Folder":
        if key == "clip" and sep == ":":
            return _one([c for c in obj.GetClipList() or [] if c.GetName() == rest],
                        f"clip {rest!r} in folder {obj.GetName()!r}"), "MediaPoolItem"
        if key == "folder" and sep == ":":
            return _one([f for f in obj.GetSubFolderList() or [] if f.GetName() == rest],
                        f"folder {rest!r} under {obj.GetName()!r}"), "Folder"
    raise ResolveError(f"'::{segment}' is not a valid child of a {cls}. See 'dava api refs'.")


def resolve_ref(session, ref):
    """Return (object, class name) for a reference string."""
    if isinstance(ref, dict) and "$ref" in ref:
        ref = ref["$ref"]
    if not isinstance(ref, str) or not ref.strip():
        raise ResolveError(f"Expected a reference string, got {ref!r}.")
    segments = ref.split("::")
    with session.lookup_cache():
        obj, cls = _root(session, segments[0])
        done = segments[0]
        for segment in segments[1:]:
            if obj is None:
                raise ResolveError(f"{done!r} is null; cannot take ::{segment}.")
            obj, cls = _child(session, obj, cls, segment)
            done += "::" + segment
    if obj is None:
        raise ResolveError(f"Reference {ref!r} resolved to nothing.")
    return obj, cls


# ---------------------------------------------------------------------------
# Arguments


def _type_text(t):
    kind = t[0]
    if kind == "prim":
        return t[1]
    if kind in ("class", "typeddict"):
        return t[1]
    if kind == "list":
        return f"list[{_type_text(t[1])}]"
    if kind == "dict":
        return f"dict[{_type_text(t[1])}, {_type_text(t[2])}]"
    if kind == "union":
        return " | ".join(_type_text(m) for m in t[1])
    if kind == "literal":
        return t[2] or "one of " + ", ".join(map(repr, t[1]))
    if kind == "const":
        return t[1]
    return kind


def accepts_string(t):
    """True when a plain command-line string is a valid value for type t (or one of its members)."""
    kind = t[0]
    if kind in ("prim", "literal", "const", "class"):
        return True
    if kind == "union":
        return any(accepts_string(m) for m in t[1])
    return False


def holds_structured(t):
    """True when t (or a member of it) takes JSON structures: lists, dicts, typed dicts or {"$ref"} objects."""
    kind = t[0]
    if kind in ("list", "dict", "typeddict", "class", "any"):
        return True
    if kind == "union":
        return any(holds_structured(m) for m in t[1])
    return False


def parse_cli_value(text, t):
    """Turn one command-line string into a value for type t.

    Text starting with '[' or '{' is parsed as JSON; so is any text for a structured type
    (list, dict, typed dict). Everything else stays a string for coerce() to convert.
    'null' becomes None for optional parameters that cannot hold a string.
    """
    stripped = text.strip()
    if t[0] == "union" and ("none",) in t[1] and stripped == "null" and ("prim", "str") not in t[1]:
        return None
    if (stripped[:1] in ("[", "{") and holds_structured(t)) or not accepts_string(t):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            if not accepts_string(t):
                raise ResolveError(f"Expected JSON for a {_type_text(t)}, got {text!r}.") from None
    return text


def parse_untyped(text):
    """Parse a command-line value for an unchecked call: JSON if it parses, else the string."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off"}


def coerce(session, value, t, where, lenient_list=True):
    kind = t[0]
    if kind == "any":
        return _resolve_embedded_refs(session, value)
    if kind == "none":
        if value is None:
            return None
        raise ResolveError(f"{where}: expected null, got {value!r}.")
    if kind == "prim":
        return _coerce_prim(value, t[1], where)
    if kind == "list":
        if not isinstance(value, list):
            if not lenient_list:
                raise ResolveError(f"{where}: expected a list, got {value!r}.")
            value = [value]
        return [coerce(session, v, t[1], f"{where}[{i}]") for i, v in enumerate(value)]
    if kind == "dict":
        if not isinstance(value, dict):
            raise ResolveError(f"{where}: expected a JSON object, got {value!r}.")
        return {coerce(session, k, t[1], f"{where} key"): coerce(session, v, t[2], f"{where}[{k!r}]")
                for k, v in value.items()}
    if kind == "typeddict":
        if not isinstance(value, dict):
            raise ResolveError(f"{where}: expected a JSON object ({t[1]}), got {value!r}.")
        fields = session.spec.typeddicts[t[1]].fields
        out = {}
        for key, item in value.items():
            if key in fields:
                out[key] = coerce(session, item, fields[key][0], f"{where}.{key}")
            else:
                # Unknown keys may be valid in newer Resolve builds; pass them through.
                out[key] = _resolve_embedded_refs(session, item)
        return out
    if kind == "literal":
        values = t[1]
        if value in values:
            return value
        if isinstance(value, str):
            folded = [v for v in values if isinstance(v, str) and v.lower() == value.lower()]
            if folded:
                return folded[0]
        raise ResolveError(f"{where}: {value!r} is not one of {', '.join(map(str, values))}.")
    if kind == "const":
        if isinstance(value, bool):
            raise ResolveError(f"{where}: expected a {t[1]} constant name, got {value!r}.")
        if isinstance(value, str):
            try:
                value = float(value.strip())
            except ValueError:
                pass
        if isinstance(value, (int, float)):
            # Getters return constants as numbers; accept a number only if it is one of this type's constants.
            known = {getattr(session.resolve, n): n for n in t[2] if getattr(session.resolve, n, None) is not None}
            if value in known:
                return value
            raise ResolveError(f"{where}: {value!r} is not the value of any {t[1]} constant "
                               f"({', '.join(f'{n}={v}' for v, n in known.items())}).")
        if isinstance(value, str):
            name = value.removeprefix("resolve.").upper()
            if name not in t[2]:
                close = difflib.get_close_matches(name, t[2], n=3)
                hint = f" Did you mean {', '.join(close)}?" if close else f" Choices: {', '.join(t[2])}."
                raise ResolveError(f"{where}: {value!r} is not a {t[1]} constant.{hint}")
            constant = getattr(session.resolve, name, None)
            if constant is None:
                raise ResolveError(f"{where}: this Resolve build has no constant {name}.")
            return constant
        raise ResolveError(f"{where}: expected a {t[1]} constant name, got {value!r}.")
    if kind == "class":
        obj, cls = resolve_ref(session, value)
        if cls != t[1]:
            raise ResolveError(f"{where}: expected a {t[1]} reference, but {value!r} is a {cls}.")
        return obj
    if kind == "union":
        members = t[1]
        if value is None and ("none",) in members:
            return None
        # A value that already has one of the member types exactly is kept as is: "24" for float | str
        # stays the string Resolve documents, instead of being converted by an earlier member.
        exact = {bool: "bool", int: "int", float: "float", str: "str"}.get(type(value))
        if exact and ("prim", exact) in members:
            return value
        # Try list members first for lists, the others first for everything else.
        ordered = sorted((m for m in members if m[0] != "none"), key=lambda m: (m[0] == "list") != isinstance(value, list))
        errors = []
        for member in ordered:
            try:
                return coerce(session, value, member, where, lenient_list=False)
            except ResolveError as exc:
                errors.append(str(exc))
        raise ResolveError(f"{where}: {value!r} does not fit {_type_text(t)} ({'; '.join(errors)}).")
    raise ResolveError(f"{where}: unsupported parameter type {t!r}.")


def _coerce_prim(value, name, where):
    if name == "str":
        if isinstance(value, str):
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
    elif name == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in _TRUE | _FALSE:
            return value.lower() in _TRUE
        if value in (0, 1):
            return bool(value)
    elif name == "int":
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            try:
                return int(value.strip())
            except ValueError:
                try:
                    number = float(value.strip())
                except ValueError:
                    number = None
                if number is not None and number.is_integer():
                    return int(number)
    elif name == "float":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                pass
    raise ResolveError(f"{where}: expected {name}, got {value!r}.")


def _resolve_embedded_refs(session, value):
    """Replace {"$ref": ...} objects nested anywhere in an untyped value with live objects."""
    if isinstance(value, dict):
        if set(value) == {"$ref"} or ("$ref" in value and "$type" in value):
            return resolve_ref(session, value["$ref"])[0]
        return {k: _resolve_embedded_refs(session, v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_embedded_refs(session, v) for v in value]
    return value


def bind(session, method, positional, named, raw=False):
    """Match positional and named values to a method's parameters and coerce them.

    raw=True means the values are command-line strings that still need parsing.
    Values in `named` whose key ends with ':=' in the CLI are passed pre-parsed via raw=False
    by the caller splitting them out; here every named value follows the `raw` flag unless it
    is wrapped as ('json', value).
    """
    params = method.params
    by_name = {p.name: i for i, p in enumerate(params)}
    slots = {}
    if len(positional) > len(params):
        raise ResolveError(f"{method.qualname} takes {len(params)} argument(s), got {len(positional)}. "
                           f"Signature: {method.signature()}")
    for i, value in enumerate(positional):
        slots[i] = value
    for name, value in (named or {}).items():
        if name not in by_name:
            close = difflib.get_close_matches(name, list(by_name), n=3)
            hint = f" Did you mean {', '.join(close)}?" if close else ""
            raise ResolveError(f"{method.qualname} has no parameter {name!r}.{hint} Signature: {method.signature()}")
        if by_name[name] in slots:
            raise ResolveError(f"Parameter {name!r} of {method.qualname} was given twice.")
        slots[by_name[name]] = value

    last = max(slots) if slots else -1
    args = []
    for i, param in enumerate(params):
        if i in slots:
            value = slots[i]
            if isinstance(value, tuple) and len(value) == 2 and value[0] == "json":
                value = value[1]
            elif raw and isinstance(value, str):
                value = parse_cli_value(value, param.type)
            args.append(coerce(session, value, param.type, f"argument {param.name!r}"))
        elif i < last and param.has_default:
            args.append(param.default)
        elif not param.has_default:
            raise ResolveError(f"{method.qualname} needs argument {param.name!r}. Signature: {method.signature()}")
    return args


# ---------------------------------------------------------------------------
# Results


STABLE_ID_PREFIX = {"Timeline": "timeline", "MediaPoolItem": "clip", "Folder": "folder", "TimelineItem": "item"}
SINGLETON_REFS = {"Resolve": "resolve", "ProjectManager": "projectmanager", "MediaPool": "mediapool",
                  "MediaStorage": "mediastorage", "Gallery": "gallery", "Fusion": "fusion"}


def describe_object(session, obj, cls):
    """Summarise a live object as {"$ref", "$type", ...} with a stable ref when one exists."""
    info = {"$type": cls}
    if cls in SINGLETON_REFS:
        info["$ref"] = SINGLETON_REFS[cls]
    elif cls in STABLE_ID_PREFIX:
        unique_id = obj.GetUniqueId()
        info["$ref"] = f"{STABLE_ID_PREFIX[cls]}#{unique_id}"
        info["name"] = obj.GetName()
    elif cls == "Project":
        info["name"] = obj.GetName()
        current = session.resolve.GetProjectManager().GetCurrentProject()
        if current and current.GetUniqueId() == obj.GetUniqueId():
            info["$ref"] = "project"
    elif cls == "ColorGroup":
        name = info["name"] = obj.GetName()
        # Emit a name ref only when it resolves back to this group (unique, no '::').
        names = [g.GetName() for g in session.project().GetColorGroupsList() or []]
        if "::" not in name and names.count(name) == 1:
            info["$ref"] = f"colorgroup:{name}"
    elif cls == "GalleryStillAlbum":
        gallery = session.project().GetGallery()
        name = info["name"] = gallery.GetAlbumName(obj)
        for prefix, power_grade in (("album", False), ("powergrade", True)):
            names = [gallery.GetAlbumName(a) for a in _albums(gallery, power_grade)]
            if "::" not in name and names.count(name) == 1:
                info["$ref"] = f"{prefix}:{name}"
                break
    if "$ref" not in info:
        info["$ref"] = "$" + session.new_handle(obj, cls)
    return info


def _is_plain(value):
    return value is None or isinstance(value, (bool, int, float, str))


def serialize(session, value, t=("any",)):
    """Convert a Resolve return value to JSON-able data, turning objects into references."""
    if _is_plain(value):
        return value
    if isinstance(value, (list, tuple)):
        inner = _member(t, "list")
        inner = inner[1] if inner else ("any",)
        return [serialize(session, v, inner) for v in value]
    if isinstance(value, dict):
        typed = _member(t, "typeddict")
        mapping = _member(t, "dict")
        fields = session.spec.typeddicts[typed[1]].fields if typed and session._spec else {}
        value_type = mapping[2] if mapping else ("any",)
        return {str(k): serialize(session, v, fields[k][0] if k in fields else value_type) for k, v in value.items()}
    cls_type = _member(t, "class")
    cls = cls_type[1] if cls_type else identify_class(session, value)
    return describe_object(session, value, cls)


def _member(t, kind):
    if t[0] == kind:
        return t
    if t[0] == "union":
        matches = [m for m in t[1] if m[0] == kind]
        return matches[0] if len(matches) == 1 else None
    return None


def identify_class(session, obj):
    """Best guess at an undeclared object's class, from the methods it exposes."""
    try:
        spec = session.spec
    except ResolveError:
        return "Object"
    exposed = set(dir(obj))
    best, best_score = "Object", 0
    for name, cls in spec.classes.items():
        if not cls.methods:
            continue
        score = len(exposed & set(cls.methods)) / len(cls.methods)
        if score > best_score:
            best, best_score = name, score
    return best if best_score >= 0.5 else "Object"


# ---------------------------------------------------------------------------
# Calls


HANDLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
AUTO_HANDLE = re.compile(r"h[0-9]+")


def check_handle_name(save_as):
    """Validate a --as / save_as name and return it without a leading '$'."""
    if not isinstance(save_as, str):
        raise ResolveError(f"save_as must be a string, got {save_as!r}.")
    name = save_as[1:] if save_as.startswith("$") else save_as
    if not HANDLE_NAME.fullmatch(name):
        raise ResolveError(f"Handle name {save_as!r} must be letters, digits and '_' (not starting with a digit).")
    if AUTO_HANDLE.fullmatch(name):
        raise ResolveError(f"Handle names like {name!r} (h + digits) are reserved for automatic handles.")
    return name


def call(session, ref, method_name, positional=(), named=None, raw=False, unchecked=False,
         read_only=False, dry_run=False, save_as=None):
    """Call method_name on the object ref names, and return {"ref", "method", "result"}."""
    handle_name = check_handle_name(save_as) if save_as is not None else None
    with session.lookup_cache():
        return _call(session, ref, method_name, positional, named, raw, unchecked, read_only, dry_run, handle_name)


def _call(session, ref, method_name, positional, named, raw, unchecked, read_only, dry_run, save_as):
    obj, cls = resolve_ref(session, ref)
    method = None
    api_class = None
    try:
        api_class = session.spec.classes.get(cls)
    except ResolveError:
        if not unchecked:
            raise
    if api_class is not None:
        method = api_class.methods.get(method_name)
    if method is None and not unchecked and api_class is not None and api_class.methods:
        close = difflib.get_close_matches(method_name, list(api_class.methods), n=5)
        hint = f" Did you mean {', '.join(close)}?" if close else ""
        raise ResolveError(f"{cls} has no method {method_name!r} in the API definition.{hint} "
                           "Use --unchecked to call it anyway.")
    if read_only and not is_read_only_name(method_name):
        raise ResolveError(f"{cls}.{method_name} may change Resolve state and read-only mode is on.")

    if save_as and method is not None and _member(method.returns, "class") is None:
        raise ResolveError(f"--as needs a method that returns one object; {cls}.{method_name} returns "
                           f"{method.returns_text or 'None'}. Use the $ref values in its result instead.")
    if method is not None:
        args = bind(session, method, list(positional), named, raw=raw)
    else:
        if named:
            raise ResolveError(f"Named arguments need an API definition; {cls}.{method_name} has none. "
                               "Pass arguments positionally.")
        args = [parse_untyped(v) if raw and isinstance(v, str) else _unwrap_json(v) for v in positional]
        args = [_resolve_embedded_refs(session, a) for a in args]

    qualname = f"{cls}.{method_name}"
    if dry_run:
        return {"ref": ref, "method": qualname, "dry_run": True,
                "signature": method.signature() if method else None,
                "args": serialize(session, args)}

    function = getattr(obj, method_name, None)
    if function is None:
        raise ResolveError(f"The live {cls} object has no method {method_name!r} (older Resolve build?).")
    try:
        result = function(*args)
    except Exception as exc:  # the remote bridge raises arbitrary types; report them with context
        raise ResolveError(f"{qualname} raised {type(exc).__name__}: {exc}") from exc

    returns = method.returns if method else ("any",)
    out = {"ref": ref, "method": qualname, "result": serialize(session, result, returns)}
    if save_as:
        if _is_plain(result) or isinstance(result, (list, tuple, dict)):
            out["saved_as"] = None
            out["note"] = "--as keeps single objects only; this result was not saved."
        else:
            declared = _member(returns, "class")
            session.handles[save_as] = (result, declared[1] if declared else identify_class(session, result))
            out["saved_as"] = "$" + save_as
    # A mutating call that returns False is how the API reports failure.
    if result is False and method is not None and returns == ("prim", "bool") and not method.read_only:
        raise ResolveError(f"{qualname} returned False (Resolve rejected the call).")
    return out


def _unwrap_json(value):
    if isinstance(value, tuple) and len(value) == 2 and value[0] == "json":
        return value[1]
    return value


FUSION_CLASSES = {"Fusion", "FusionComp", "FusionTool", "FusionInput"}


def live_members(obj):
    """Public names the live object exposes (Resolve's remote objects support dir())."""
    return sorted(name for name in dir(obj) if not name.startswith("_"))


def inspect(session, ref):
    obj, cls = resolve_ref(session, ref)
    info = describe_object(session, obj, cls)
    try:
        api_class = session.spec.classes.get(cls)
    except ResolveError:
        api_class = None
    if api_class is not None and api_class.methods:
        info["doc"] = api_class.doc
        info["methods"] = [m.signature() for m in api_class.methods.values()]
    else:
        # Not described by the API definition (Fusion objects, stills): report what the object has.
        info["members"] = live_members(obj)
    if cls in FUSION_CLASSES - {"Fusion"}:
        info["attrs"] = serialize(session, obj.GetAttrs())
    return info
