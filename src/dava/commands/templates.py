"""dava templates: the Fusion titles, transitions, generators and effects Resolve can insert by name."""

from .. import templates
from ..connect import ResolveError


def cmd_list(resolve, args):
    rows = templates.list_templates(kind=args.kind, search=args.search)
    if not rows:
        raise ResolveError("No templates match." if args.kind or args.search else
                           "No template folders or bundles found; set DAVA_TEMPLATE_PATHS.")
    return rows


def cmd_show(resolve, args):
    return templates.show(args.name, kind=args.kind)


def cmd_sources(resolve, args):
    return templates.sources()


def register(registry):
    help_text = "Fusion titles, transitions, generators and effects (no Resolve needed)"
    p = registry.action("templates", "list", "list templates by kind", group_help=help_text)
    p.add_argument("kind", nargs="?", choices=sorted(templates.KINDS) + ["other"])
    p.add_argument("-s", "--search", help="only names containing this text")
    p.set_defaults(func=cmd_list, needs_resolve=False, read_only=True)

    p = registry.action("templates", "show", "tools and published inputs of a template (which tool holds the text)")
    p.add_argument("name")
    p.add_argument("-k", "--kind", choices=sorted(templates.KINDS) + ["other"])
    p.set_defaults(func=cmd_show, needs_resolve=False, read_only=True, always_json=True)

    p = registry.action("templates", "sources", "the folders and .drfx bundles searched")
    p.set_defaults(func=cmd_sources, needs_resolve=False, read_only=True)
