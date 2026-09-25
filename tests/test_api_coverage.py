"""Every method in the installed Resolve API definition must be callable through the bridge.

For each method (410 in Resolve 21.1): build a sample value for every parameter from its declared type
(one sample per union member), bind it the way 'dava call' and the MCP tools do, and check the runtime
type of every bound argument. Bind the command-line string forms too. Then serialize a sample return
value and require a strict JSON round trip. Finally, resolve one REF per API class against the fakes.
"""

import json
import os

import pytest

from dava import bridge
from dava.bridge import Session
from dava.connect import stub_path
from dava.spec import Spec
from fakes import FakeResolve

STUB = stub_path()
pytestmark = pytest.mark.skipif(not STUB or not os.path.isfile(STUB), reason="Resolve scripting SDK not installed")


def load():
    with open(STUB, encoding="utf-8") as handle:
        return Spec(handle.read(), STUB)


SPEC = load() if STUB and os.path.isfile(STUB) else None


class Anything:
    """Stands in for any live Resolve object. Methods return a sample of their declared return type
    (so lists are lists); resolve.* constant names are distinct numbers, as on the real object."""

    def __init__(self, cls="Object"):
        self.cls = cls

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        if name in SPEC.constants:
            return float(sorted(SPEC.constants).index(name) + 1)
        if name in ("GetUniqueId", "GetName", "GetAlbumName"):
            return lambda *a: f"{self.cls}-{name}"
        declared = next((c.methods[name].returns for c in SPEC.classes.values() if name in c.methods), ("any",))
        return lambda *a: sample_result(declared)


def sample_values(t):
    """One or more JSON-style samples for type t: every union member gets its own."""
    kind = t[0]
    if kind == "union":
        return [v for m in t[1] if m[0] != "none" for v in sample_values(m)]
    if kind == "prim":
        return [{"str": "x", "int": 7, "float": 1.5, "bool": True}[t[1]]]
    if kind == "list":
        return [[v] for v in sample_values(t[1])[:1]]
    if kind == "dict":
        return [{"key": v} for v in sample_values(t[2])[:1]] if t[2] != ("any",) else [{"key": "value"}]
    if kind == "typeddict":
        return [{name: sample_values(ft)[0] for name, (ft, _, _) in SPEC.typeddicts[t[1]].fields.items()}]
    if kind == "literal":
        return [t[1][0]]
    if kind == "const":
        return [t[2][0]]
    if kind == "class":
        return ["@" + t[1]]
    return ["x"]


def sample_result(t):
    kind = t[0]
    if kind == "class":
        return Anything(t[1])
    if kind == "list":
        return [sample_result(t[1])]
    if kind == "typeddict":
        return {name: sample_result(ft) for name, (ft, _, _) in SPEC.typeddicts[t[1]].fields.items()}
    if kind == "dict":
        return {1: sample_result(t[2])}
    if kind == "union":
        return sample_result(next(m for m in t[1] if m[0] != "none"))
    if kind == "none":
        return None
    return sample_values(t)[0]


def check_bound(value, t, sample, where):
    """Assert a bound argument has the runtime form Resolve expects for type t."""
    kind = t[0]
    if kind == "prim":
        assert type(value) is {"str": str, "int": int, "float": float, "bool": bool}[t[1]], (where, value)
    elif kind == "const":
        assert value == getattr(Anything("resolve"), sample), (where, value)
    elif kind == "class":
        assert isinstance(value, Anything) and value.cls == t[1], (where, value)
    elif kind == "list":
        assert isinstance(value, list), (where, value)
        for i, (v, s) in enumerate(zip(value, sample)):
            check_bound(v, t[1], s, f"{where}[{i}]")
    elif kind == "typeddict":
        fields = SPEC.typeddicts[t[1]].fields
        for key, v in value.items():
            check_bound(v, fields[key][0], sample[key], f"{where}.{key}")
    elif kind == "union":
        # The sample came from one member; the bound value must fit that member.
        member = next(m for m in t[1] if m[0] != "none" and sample in sample_values(m))
        check_bound(value, member, sample, where)
    elif kind == "literal":
        assert value in t[1], (where, value)


@pytest.fixture
def session(monkeypatch):
    # Object parameters get refs like '@Timeline'; resolve them to a stand-in of that class.
    monkeypatch.setattr(bridge, "resolve_ref", lambda s, ref: (Anything(ref[1:]), ref[1:]))
    return Session(resolve=Anything("Resolve"), spec=SPEC)


def all_methods():
    return [m.qualname for c in SPEC.classes.values() for m in c.methods.values()] if SPEC else []


def test_stub_has_the_expected_size():
    total = sum(len(c.methods) for c in SPEC.classes.values())
    assert total >= 400, total
    untyped = [(m.qualname, p.name) for c in SPEC.classes.values() for m in c.methods.values()
               for p in m.params if p.type == ("any",)]
    assert untyped == []


@pytest.mark.parametrize("qualname", all_methods())
def test_every_method_binds_and_serializes(session, qualname):
    method = SPEC.method(*qualname.split("."))
    # Every parameter by name with every union member's sample, as the MCP tools pass them.
    for p in method.params:
        for sample in sample_values(p.type):
            named = {q.name: ("json", sample if q is p else sample_values(q.type)[0]) for q in method.params}
            args = bridge.bind(session, method, [], named)
            assert len(args) == len(method.params)
            check_bound(args[method.params.index(p)], p.type, sample, f"{qualname}({p.name})")
    # Only the required ones, positionally, as a CLI user would, from command-line strings.
    required = [p for p in method.params if not p.has_default]
    cli = []
    for p in required:
        sample = sample_values(p.type)[0]
        cli.append(sample if isinstance(sample, str) else json.dumps(sample))
    args = bridge.bind(session, method, cli, {}, raw=True)
    for p, value, sample in zip(required, args, [sample_values(p.type)[0] for p in required]):
        check_bound(value, p.type, sample, f"{qualname}({p.name}) from CLI")
    # The return value, whatever its declared type, must serialize to strict JSON.
    result = bridge.serialize(session, sample_result(method.returns), method.returns)
    assert json.loads(json.dumps(result)) == result


def test_json_looking_strings_stay_strings_for_str_parameters(session):
    method = SPEC.method("MediaPoolItem", "SetAudioMapping")
    mapping = '{"track_mapping": {"1": {"channel_idx": [1, 2], "mute": false, "type": "stereo"}}}'
    assert bridge.bind(session, method, [mapping], {}, raw=True) == [mapping]


def test_frame_rate_strings_reach_resolve_as_strings(session):
    for cls in ("Project", "Timeline"):
        method = SPEC.method(cls, "SetSettings")
        for text in ("24", "23.976", "29.97 DF"):
            (settings,) = bridge.bind(session, method, [("json", {"timelineFrameRate": text})], {})
            assert settings["timelineFrameRate"] == text


REFS_BY_CLASS = {
    "Resolve": "resolve", "ProjectManager": "pm", "Project": "project", "MediaPool": "mediapool",
    "MediaStorage": "mediastorage", "Gallery": "gallery", "Fusion": "fusion", "Timeline": "timeline",
    "MediaPoolItem": "clip:a.mov", "Folder": "folder:/", "TimelineItem": "item:video:1:1",
    "ColorGroup": "colorgroup:Warm", "GalleryStillAlbum": "album:Looks", "GalleryStill": "album:Looks::still:1",
    "Graph": "item:video:1:1::graph", "FusionComp": "item:video:1:1::comp@1",
}


def test_every_api_class_is_reachable_by_a_ref():
    resolve = FakeResolve()
    resolve.pm.current.timelines[0].tracks[1][0].comps = ["Composition 1"]
    fresh = Session(resolve=resolve, spec=SPEC)
    missing = sorted(set(SPEC.classes) - set(REFS_BY_CLASS))
    assert missing == [], f"no REF form known for {missing}"
    for cls, ref in REFS_BY_CLASS.items():
        assert bridge.resolve_ref(fresh, ref)[1] == cls, ref
