"""Find the Fusion templates (titles, transitions, generators, effects) Resolve can insert by name.

Templates are .setting files, either loose in a Fusion/Templates folder or packed in .drfx bundles
(zip files). The names are what Timeline.InsertFusionTitleIntoTimeline,
InsertFusionGeneratorIntoTimeline and TimelineItem.AddTransition(category='fusion') expect.
"""

import os
import re
import sys
import zipfile

from .connect import ResolveError

KINDS = {"titles": "Titles", "transitions": "Transitions", "generators": "Generators", "effects": "Effects"}

# Templates bundled with the application (verified location on macOS only).
_BUNDLED = {
    "darwin": ["/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Resources/Fusion/Templates/Templates.drfx"],
}


def _template_dirs():
    """Fusion/Templates folders for all users and the current user, next to the documented Fusion/Scripts folders."""
    home = os.path.expanduser("~")
    if sys.platform.startswith("darwin"):
        roots = ["/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion",
                 os.path.join(home, "Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion")]
    elif sys.platform.startswith("win"):
        roots = [os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"), "Blackmagic Design", "DaVinci Resolve", "Fusion"),
                 os.path.join(os.environ.get("APPDATA", ""), "Blackmagic Design", "DaVinci Resolve", "Support", "Fusion")]
    else:
        roots = ["/opt/resolve/Fusion", os.path.join(home, ".local/share/DaVinciResolve/Fusion")]
    return [os.path.join(root, "Templates") for root in roots]


def sources():
    """Every template folder and .drfx bundle that exists, plus DAVA_TEMPLATE_PATHS entries."""
    extra = [p for p in os.environ.get("DAVA_TEMPLATE_PATHS", "").split(os.pathsep) if p]
    candidates = _BUNDLED.get(sys.platform, []) + _template_dirs() + extra
    return [p for p in candidates if os.path.exists(p)]


def _kind_of(path_in_source):
    parts = path_in_source.replace("\\", "/").split("/")
    for part in parts:
        for kind, folder in KINDS.items():
            if part == folder:
                return kind
    return "other"


def _entries(source):
    """Yield (relative path, reader) for every .setting in a folder, a .drfx, or folders of .drfx files."""
    if os.path.isfile(source) and source.lower().endswith(".drfx"):
        with zipfile.ZipFile(source) as bundle:
            for name in bundle.namelist():
                if name.lower().endswith(".setting"):
                    yield name, (lambda n=name: zipfile.ZipFile(source).read(n).decode("utf-8", "replace"))
        return
    for folder, _, files in os.walk(source):
        for file_name in files:
            full = os.path.join(folder, file_name)
            if file_name.lower().endswith(".setting"):
                yield os.path.relpath(full, source), (lambda f=full: open(f, encoding="utf-8", errors="replace").read())
            elif file_name.lower().endswith(".drfx"):
                for inner, reader in _entries(full):
                    yield os.path.join(os.path.relpath(full, source), inner), reader


def list_templates(kind=None, search=None):
    rows = []
    seen = set()
    for source in sources():
        for rel, _ in _entries(source):
            name = os.path.splitext(os.path.basename(rel))[0]
            template_kind = _kind_of(rel)
            if kind and template_kind != kind:
                continue
            if search and search.lower() not in name.lower():
                continue
            key = (template_kind, name)
            if key in seen:
                continue
            seen.add(key)
            rows.append({"name": name, "kind": template_kind, "source": source})
    rows.sort(key=lambda r: (r["kind"], r["name"].lower()))
    return rows


_INSTANCE_INPUT = re.compile(
    r'(\w+)\s*=\s*InstanceInput\s*\{\s*SourceOp\s*=\s*"([^"]+)",\s*Source\s*=\s*"([^"]+)"(?:,\s*Name\s*=\s*"([^"]*)")?'
)
_TOOL = re.compile(r'^\t\t\t\t(\w+)\s*=\s*(\w+)\s*\{', re.MULTILINE)


def show(name, kind=None):
    """Describe a template: its tools and the inputs it publishes (e.g. which tool holds StyledText)."""
    for source in sources():
        for rel, reader in _entries(source):
            if os.path.splitext(os.path.basename(rel))[0] != name:
                continue
            if kind and _kind_of(rel) != kind:
                continue
            text = reader()
            inputs = [{"published": m.group(1), "tool": m.group(2), "input": m.group(3),
                       **({"label": m.group(4)} if m.group(4) else {})}
                      for m in _INSTANCE_INPUT.finditer(text)]
            tools = sorted({(m.group(1), m.group(2)) for m in _TOOL.finditer(text)
                            if m.group(2) not in ("InstanceInput", "InstanceOutput", "Input", "BezierSpline", "Polyline")})
            text_tools = sorted({i["tool"] for i in inputs if i["input"] == "StyledText"})
            return {"name": name, "kind": _kind_of(rel), "source": source, "path": rel,
                    "text_tools": text_tools,
                    "tools": [{"name": t, "type": typ} for t, typ in tools],
                    "published_inputs": inputs}
    raise ResolveError(f"No template named {name!r}. List them with 'dava templates list'.")
