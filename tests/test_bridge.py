import os

import pytest

from dava import bridge
from dava.bridge import Session, parse_cli_value, resolve_ref
from dava.connect import ResolveError
from dava.spec import Spec
from fakes import FakeClip, FakeFolder, FakeResolve, FakeTimeline

MINI = os.path.join(os.path.dirname(__file__), "data", "mini.pyi")


@pytest.fixture
def spec():
    with open(MINI) as handle:
        return Spec(handle.read(), MINI)


@pytest.fixture
def resolve():
    return FakeResolve()


@pytest.fixture
def session(resolve, spec):
    return Session(resolve=resolve, spec=spec)


def project(resolve):
    return resolve.pm.current


def ref(session, text):
    return resolve_ref(session, text)


# --- spec parsing -----------------------------------------------------------


def test_spec_parses_all_shapes(spec):
    assert spec.method("Timeline", "AddMarker").params[1].type == ("literal", ["Blue", "Cyan", "Green", "Red"], "MarkerColor")
    assert spec.method("Timeline", "Export").params[1].type[0:2] == ("const", "TimelineExportType")
    assert spec.method("Timeline", "DeleteClips").params[1].type == ("prim", "bool")  # typed from its default
    assert spec.typeddicts["ClipProperties"].fields["Clip Name"][0] == ("prim", "str")
    assert spec.typeddicts["AppendClipInfo"].fields["mediaPoolItem"][0] == ("class", "MediaPoolItem")
    assert spec.constants["EXPORT_CDL"] == "TimelineExportSubtype"
    assert spec.classes["Fusion"].methods == {}


def test_spec_describe_includes_referenced_types(spec):
    info = spec.describe("MediaPool.AppendToTimeline")
    assert info["signature"] == "MediaPool.AppendToTimeline(clipInfos: list[AppendClipInfo]) -> list[TimelineItem]"
    assert "AppendClipInfo" in info["types"]
    with pytest.raises(ResolveError, match="dava api search"):
        spec.describe("Timeline.Nope")


def test_spec_search_ranks_name_matches_first(spec):
    results = spec.search("marker")
    assert results[0]["name"] == "Timeline.AddMarker"
    assert any(r["name"] == "MarkerInfo" for r in results)


# --- references -------------------------------------------------------------


def test_timeline_refs(session, resolve):
    timelines = project(resolve).timelines
    assert ref(session, "timeline") == (timelines[0], "Timeline")
    assert ref(session, "timeline:Edit v2")[0] is timelines[1]
    assert ref(session, "timeline#id-Edit v2")[0] is timelines[1]
    assert ref(session, "timeline@2")[0] is timelines[1]
    with pytest.raises(ResolveError, match="no timeline@3"):
        ref(session, "timeline@3")
    with pytest.raises(ResolveError, match="1-based"):
        ref(session, "timeline@0")
    with pytest.raises(ResolveError, match="No timeline named 'Nope'"):
        ref(session, "timeline:Nope")


def test_clip_and_folder_refs(session, resolve):
    root = project(resolve).media_pool.root
    audio = root.subfolders[0]
    assert ref(session, "clip:a.mov") == (root.clips[0], "MediaPoolItem")
    assert ref(session, "clip:/Master/Audio/vo.wav")[0] is audio.clips[0]
    assert ref(session, "clip:/Audio/vo.wav")[0] is audio.clips[0]
    assert ref(session, "clip#clip-vo.wav")[0] is audio.clips[0]
    assert ref(session, "folder:/")[0] is root
    assert ref(session, "folder:/Master/Audio")[0] is audio
    assert ref(session, "folder#folder-Audio")[0] is audio
    assert ref(session, "folder:/Master::clip:a.mov")[0] is root.clips[0]
    assert ref(session, "folder:/::folder:Audio")[0] is audio


def test_ambiguous_clip_name_points_to_path_ref(session, resolve):
    project(resolve).media_pool.root.subfolders.append(FakeFolder("B-roll", ["a.mov"]))
    with pytest.raises(ResolveError, match="ambiguous .*clip:/FOLDER/PATH/a.mov"):
        ref(session, "clip:a.mov")
    assert ref(session, "clip:/Master/B-roll/a.mov")[0].GetName() == "a.mov"


def test_item_refs(session, resolve):
    first, second = project(resolve).timelines[0].tracks[1]
    assert ref(session, "item") == (first, "TimelineItem")
    assert ref(session, "item:video:1:2")[0] is second
    assert ref(session, "item:VIDEO:1:2")[0] is second
    assert ref(session, "item#item-shot_020")[0] is second
    other = project(resolve).timelines[1].tracks[1][0]
    assert ref(session, "timeline:Edit v2::item:video:1:1")[0] is other
    with pytest.raises(ResolveError, match="has 2 item"):
        ref(session, "item:video:1:3")
    with pytest.raises(ResolveError, match="TYPE video"):
        ref(session, "item:video:1")


def test_item_by_id_searches_other_timelines(session, resolve):
    timeline = project(resolve).timelines[1]
    target = timeline.tracks[1][1]
    target.uid = "only-in-v2"
    assert ref(session, "item#only-in-v2")[0] is target
    with pytest.raises(ResolveError, match="No timeline item with id"):
        ref(session, "item#missing")


def test_child_segments(session, resolve):
    item = project(resolve).timelines[0].tracks[1][1]
    assert ref(session, "item:video:1:2::graph") == (item.graph, "Graph")
    assert ref(session, "item:video:1:2::graph:1")[0] is item.graph
    with pytest.raises(ResolveError, match="no node graph at layer 2"):
        ref(session, "item:video:1:2::graph:2")
    assert ref(session, "item:video:1:2::clip") == (item.clip, "MediaPoolItem")
    assert ref(session, "timeline::graph")[0] is project(resolve).timelines[0].graph
    group = project(resolve).color_groups[0]
    assert ref(session, "colorgroup:Warm::postgraph")[0] is group.post
    assert ref(session, "colorgroup:Warm::pregraph")[0] is group.pre
    album = project(resolve).gallery.albums[1]
    assert ref(session, "album:Looks::still:1")[0] is album.stills[0]
    with pytest.raises(ResolveError, match="no still 9"):
        ref(session, "album::still:9")
    item.comps = ["Title"]
    assert ref(session, "item:video:1:2::comp:1")[1] == "FusionComp"
    assert ref(session, "item:video:1:2::comp:Title")[0].name == "Title"
    with pytest.raises(ResolveError, match="not a valid child of a Timeline"):
        ref(session, "timeline::still:1")


def test_bad_refs(session):
    for text, message in [("nonsense", "Unrecognised"), ("", "Expected a reference"),
                          ("$nope", "Unknown handle"), ("clip", "Unrecognised")]:
        with pytest.raises(ResolveError, match=message):
            ref(session, text)


def test_singletons(session, resolve):
    assert ref(session, "resolve") == (resolve, "Resolve")
    assert ref(session, "pm")[1] == "ProjectManager"
    assert ref(session, "project")[0] is project(resolve)
    assert ref(session, "mediastorage")[1] == "MediaStorage"


def test_no_project(session, resolve):
    resolve.pm.current = None
    with pytest.raises(ResolveError, match="No project is open"):
        ref(session, "timeline")


# --- calls: arguments -------------------------------------------------------


def test_call_simple_and_serializes(session, resolve):
    out = bridge.call(session, "timeline", "GetName")
    assert out == {"ref": "timeline", "method": "Timeline.GetName", "result": "Edit v1"}


def test_cli_strings_are_typed(session, resolve):
    bridge.call(session, "timeline", "AddMarker", ["10", "red", "n", "", "2"], raw=True)
    assert project(resolve).timelines[0].markers[10] == {
        "color": "Red", "duration": 2, "note": "", "name": "n", "customData": ""
    }


def test_named_args_and_errors(session, resolve):
    bridge.call(session, "timeline", "AddMarker", [], {"frameId": "5", "color": "Blue", "name": "a",
                                                       "note": "b", "duration": "1"}, raw=True)
    assert 5 in project(resolve).timelines[0].markers
    with pytest.raises(ResolveError, match="needs argument 'duration'"):
        bridge.call(session, "timeline", "AddMarker", ["6", "Blue", "a", "b"], raw=True)
    with pytest.raises(ResolveError, match="Did you mean frameId"):
        bridge.call(session, "timeline", "AddMarker", [], {"frameid": "1"}, raw=True)
    with pytest.raises(ResolveError, match="takes 6 argument"):
        bridge.call(session, "timeline", "AddMarker", list("1234567"), raw=True)
    with pytest.raises(ResolveError, match="given twice"):
        bridge.call(session, "timeline", "AddMarker", ["1"], {"frameId": "1"}, raw=True)
    with pytest.raises(ResolveError, match="not one of Blue"):
        bridge.call(session, "timeline", "AddMarker", ["1", "Mauve", "a", "b", "1"], raw=True)
    with pytest.raises(ResolveError, match="expected int"):
        bridge.call(session, "timeline", "AddMarker", ["one", "Blue", "a", "b", "1"], raw=True)


def test_unknown_method_suggests(session):
    with pytest.raises(ResolveError, match="Did you mean GetName"):
        bridge.call(session, "timeline", "GetNam")


def test_constants_by_name(session, resolve, tmp_path):
    out = str(tmp_path / "x.edl")
    bridge.call(session, "timeline", "Export", [out, "EXPORT_EDL", "resolve.export_none"], raw=True)
    assert project(resolve).timelines[0].exports == [(out, FakeResolve.EXPORT_EDL, FakeResolve.EXPORT_NONE)]
    with pytest.raises(ResolveError, match="Did you mean EXPORT_EDL"):
        bridge.call(session, "timeline", "Export", [out, "EXPORT_EDLL", "EXPORT_NONE"], raw=True)
    with pytest.raises(ResolveError, match="no constant EXPORT_CDL"):
        bridge.call(session, "timeline", "Export", [out, "EXPORT_EDL", "EXPORT_CDL"], raw=True)


def test_typeddict_with_object_refs(session, resolve):
    out = bridge.call(session, "mediapool", "AppendToTimeline",
                      [("json", [{"mediaPoolItem": "clip:a.mov", "startFrame": "0"}])])
    appended = project(resolve).media_pool.appended
    assert appended[0]["mediaPoolItem"] is project(resolve).media_pool.root.clips[0]
    assert appended[0]["startFrame"] == 0.0
    assert out["result"] == [{"$type": "TimelineItem", "$ref": "item#item-a.mov", "name": "a.mov"}]


def test_list_of_refs_and_trailing_defaults(session, resolve):
    timeline = project(resolve).timelines[0]
    bridge.call(session, "timeline", "DeleteClips", ['["item:video:1:1", {"$ref": "item#item-shot_020"}]'], raw=True)
    items, ripple = timeline.deleted
    assert items == timeline.tracks[1]
    assert ripple is False  # omitted trailing default is not passed; the fake's own default applies
    with pytest.raises(ResolveError, match="expected a TimelineItem reference, but 'clip:a.mov' is a MediaPoolItem"):
        bridge.call(session, "timeline", "DeleteClips", [("json", ["clip:a.mov"])])


def test_middle_default_is_filled(session, resolve):
    graph_item = project(resolve).timelines[0].tracks[1][0]
    out = bridge.call(session, "item:video:1:1", "GetNodeGraph", [], {"layerIdx": ("json", 1)})
    assert out["result"]["$type"] == "Graph"
    assert graph_item.graph is session.handles[out["result"]["$ref"][1:]][0]


def test_union_str_or_list(session, resolve):
    storage = resolve.GetMediaStorage()
    resolve.GetMediaStorage = lambda: storage
    bridge.call(session, "mediastorage", "StartCloneMedia", ["/src", "/dst"], raw=True)
    bridge.call(session, "mediastorage", "StartCloneMedia", ["/src", '["/a", "/b"]'], raw=True)
    assert storage.clones == [("/src", "/dst"), ("/src", ["/a", "/b"])]


def test_optional_none(session, resolve):
    clip = project(resolve).media_pool.root.clips[0]
    clip.GetMetadata = lambda key=None: {"Scene": "1"} if key is None else "1"
    assert bridge.call(session, "clip:a.mov", "GetMetadata", [("json", None)])["result"] == {"Scene": "1"}
    assert bridge.call(session, "clip:a.mov", "GetMetadata", ["Scene"], raw=True)["result"] == "1"


def test_dict_keys_and_typeddict_results(session, resolve):
    project(resolve).timelines[0].markers[48] = {"color": "Red", "name": "x"}
    out = bridge.call(session, "timeline", "GetMarkers")
    assert out["result"] == {"48": {"color": "Red", "name": "x"}}


def test_parse_cli_value():
    assert parse_cli_value("10", ("prim", "int")) == "10"  # coerced later
    assert parse_cli_value("[1, 2]", ("list", ("prim", "int"))) == [1, 2]
    assert parse_cli_value("/a", ("union", [("prim", "str"), ("list", ("prim", "str"))])) == "/a"
    assert parse_cli_value('["/a"]', ("union", [("prim", "str"), ("list", ("prim", "str"))])) == ["/a"]
    assert parse_cli_value("{not json", ("prim", "str")) == "{not json"
    with pytest.raises(ResolveError, match="Expected JSON"):
        parse_cli_value("nope", ("typeddict", "AppendClipInfo"))


# --- calls: modes -----------------------------------------------------------


def test_read_only_blocks_mutations(session, resolve):
    with pytest.raises(ResolveError, match="read-only mode is on"):
        bridge.call(session, "timeline", "SetName", ["x"], raw=True, read_only=True)
    assert bridge.call(session, "timeline", "GetName", read_only=True)["result"] == "Edit v1"
    assert project(resolve).timelines[0].name == "Edit v1"


def test_dry_run_does_not_call(session, resolve):
    out = bridge.call(session, "timeline", "SetName", ["New"], raw=True, dry_run=True)
    assert out["dry_run"] is True
    assert out["args"] == ["New"]
    assert project(resolve).timelines[0].name == "Edit v1"


def test_false_from_mutating_call_is_an_error(session, resolve):
    with pytest.raises(ResolveError, match="returned False"):
        bridge.call(session, "timeline", "SetName", [""], raw=True)


def test_remote_exception_is_reported(session, resolve):
    def boom():
        raise RuntimeError("bridge down")
    project(resolve).timelines[0].GetName = boom
    with pytest.raises(ResolveError, match="Timeline.GetName raised RuntimeError: bridge down"):
        bridge.call(session, "timeline", "GetName")


def test_unchecked_calls_on_opaque_and_unknown(session, resolve):
    item = project(resolve).timelines[0].tracks[1][0]
    item.comps = ["Title"]
    out = bridge.call(session, "item::comp:1", "GetAttrs", ["COMPS_Name"], raw=True)
    assert out["result"] == "Title"  # FusionComp is opaque: no definition, so no check
    with pytest.raises(ResolveError, match="--unchecked"):
        bridge.call(session, "timeline", "GetStartTimecode")
    out = bridge.call(session, "timeline", "GetStartTimecode", unchecked=True)
    assert out["result"] == "01:00:00:00"
    with pytest.raises(ResolveError, match="no method 'Nope'"):
        bridge.call(session, "timeline", "Nope", unchecked=True)


def test_save_as_handles(session, resolve):
    out = bridge.call(session, "item:video:1:2", "GetNodeGraph", save_as="g")
    assert out["saved_as"] == "$g"
    assert bridge.call(session, "$g", "GetNumNodes")["result"] == 2
    bridge.call(session, "$g", "SetLUT", ["1", "a.cube"], raw=True)
    assert project(resolve).timelines[0].tracks[1][1].graph.luts[1] == "a.cube"


def test_inspect(session):
    info = bridge.inspect(session, "timeline:Edit v2")
    assert info["$ref"] == "timeline#id-Edit v2"
    assert info["$type"] == "Timeline"
    assert "Timeline.GetName() -> str" in info["methods"]


def test_stable_refs_round_trip(session, resolve):
    """A $ref returned by one call resolves back to the same object in a new session."""
    out = bridge.call(session, "mediapool", "GetRootFolder")
    folder_ref = out["result"]["$ref"]
    assert folder_ref == "folder#folder-Master"
    fresh = Session(resolve=resolve, spec=session.spec)
    assert resolve_ref(fresh, folder_ref)[0] is project(resolve).media_pool.root


def test_project_ref_only_for_current(session, resolve):
    out = bridge.call(session, "project", "GetName")
    assert out["result"] == "Promo"
    info = bridge.describe_object(session, resolve.pm.projects["Doc"], "Project")
    assert info["$ref"].startswith("$h")
    assert bridge.describe_object(session, project(resolve), "Project")["$ref"] == "project"


def test_new_timeline_objects(session, resolve):
    """Timelines added after the session started are still found by name."""
    project(resolve).timelines.append(FakeTimeline("Late"))
    assert ref(session, "timeline:Late")[0].GetName() == "Late"
    assert isinstance(FakeClip("x").GetUniqueId(), str)
