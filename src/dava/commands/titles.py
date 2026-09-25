"""dava title: insert animated Fusion titles and set their text."""

from .. import templates
from ..bridge import describe_object
from ..connect import ResolveError
from ..helpers import current_project, find_timeline, select_one_item, add_item_args

# Fusion tool types whose StyledText input holds a title's text.
TEXT_TOOL_TYPES = ("TextPlus", "Text3D")

# Fusion scripting API methods used here; they are not in the Resolve API definition.
FUSION_API_METHODS = {"GetToolList", "GetAttrs", "SetInput", "Lock", "Unlock"}


def text_tools(comp):
    """Return the text tools of a composition (Text+ and Text 3D)."""
    found = []
    for tool_type in TEXT_TOOL_TYPES:
        tools = comp.GetToolList(False, tool_type) or {}
        found.extend(tools.values() if isinstance(tools, dict) else tools)
    return found


def set_title_text(item, lines, font=None, size=None):
    """Put lines[i] into the i-th text tool of the item's first composition (sorted by tool name)."""
    comp = item.GetFusionCompByIndex(1)
    if not comp:
        raise ResolveError(f"{item.GetName()!r} has no Fusion composition; is it a Fusion title?")
    tools = sorted(text_tools(comp), key=lambda tool: tool.GetAttrs()["TOOLS_Name"])
    if not tools:
        raise ResolveError(f"The composition of {item.GetName()!r} has no Text+ or Text 3D tool.")
    if len(lines) > len(tools):
        raise ResolveError(f"{len(lines)} line(s) given but the title has {len(tools)} text tool(s).")
    comp.Lock()
    try:
        for tool, line in zip(tools, lines):
            tool.SetInput("StyledText", line)
            if font:
                tool.SetInput("Font", font)
            if size is not None:
                tool.SetInput("Size", size)
    finally:
        comp.Unlock()
    return [tool.GetAttrs()["TOOLS_Name"] for tool in tools[:len(lines)]]


def cmd_title_add(resolve, args):
    project = current_project(resolve)
    timeline = find_timeline(project, args.timeline)
    if not project.SetCurrentTimeline(timeline):
        raise ResolveError(f"Could not make {timeline.GetName()!r} the current timeline.")
    if args.at and not timeline.SetCurrentTimecode(args.at):
        raise ResolveError(f"Could not move the playhead to {args.at!r}.")
    item = timeline.InsertFusionTitleIntoTimeline(args.template)
    if not item:
        hint = templates.list_templates(kind="titles", search=args.template.split()[0])
        names = ", ".join(r["name"] for r in hint[:8])
        raise ResolveError(f"Resolve could not insert title {args.template!r}."
                           + (f" Similar titles: {names}." if names else " See 'dava templates list titles'."))
    tools = set_title_text(item, [args.text] + args.line, font=args.font, size=args.size)
    info = describe_object(args.session, item, "TimelineItem")
    info.update({"template": args.template, "text_tools": tools,
                 "start": item.GetStart(), "duration": item.GetDuration()})
    return info


def cmd_title_text(resolve, args):
    item = select_one_item(resolve, args)
    tools = set_title_text(item, [args.text] + args.line, font=args.font, size=args.size)
    return {"item": item.GetName(), "text_tools": tools}


def _text_options(p):
    p.add_argument("--line", action="append", default=[], metavar="TEXT",
                   help="text for the next text tool of multi-line titles (repeatable)")
    p.add_argument("--font", help="font family, e.g. 'Open Sans'")
    p.add_argument("--size", type=float, help="text size (Fusion units, e.g. 0.08)")


def register(registry):
    help_text = "animated Fusion titles"
    p = registry.action(
        "title", "add", "insert a Fusion title at the playhead and set its text (Resolve inserts it on V1 and "
        "shifts later clips; for text over existing shots use 'dava overlay text')", group_help=help_text)
    p.add_argument("text")
    p.add_argument("-T", "--template", default="Text+",
                   help="title template name (default: Text+); see 'dava templates list titles'")
    p.add_argument("--at", metavar="TIMECODE", help="move the playhead here first, e.g. 01:00:05:00")
    p.add_argument("-t", "--timeline", help="timeline name (default: current)")
    _text_options(p)
    p.set_defaults(func=cmd_title_add, read_only=False, always_json=True)

    p = registry.action("title", "text", "change the text of a title already on the timeline")
    p.add_argument("text")
    add_item_args(p, single=True)
    _text_options(p)
    p.set_defaults(func=cmd_title_text, read_only=False)
