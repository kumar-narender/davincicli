"""dava exec: run Python against the live Resolve session."""

import argparse
import sys

from .. import scripting
from ..connect import ResolveError


def cmd_exec(resolve, args):
    if args.code is not None:
        code, name = args.code, "<-c>"
    elif args.file == "-":
        code, name = sys.stdin.read(), "<stdin>"
    elif args.file:
        try:
            with open(args.file, encoding="utf-8") as handle:
                code, name = handle.read(), args.file
        except OSError as exc:
            raise ResolveError(f"Cannot read {args.file}: {exc.strerror}") from exc
    else:
        raise ResolveError("Give -c CODE, a FILE, or '-' to read the snippet from stdin.")
    return scripting.run(args.session, code, name, read_only=args.read_only_mode)


def register(registry):
    p = registry.command(
        "exec", help="run a Python snippet against the live Resolve session (escape hatch)",
        description=scripting.EXEC_HELP, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("file", nargs="?", metavar="FILE", help="Python file to run, or '-' for stdin")
    p.add_argument("-c", dest="code", metavar="CODE", help="the snippet as a string")
    p.set_defaults(func=cmd_exec, read_only=False, always_json=True)
