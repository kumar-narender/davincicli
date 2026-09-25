"""Run a Python snippet against the live Resolve session: the escape hatch for anything else."""

import contextlib
import io
import traceback

from .bridge import resolve_ref, serialize
from .connect import ResolveError

EXEC_HELP = """\
The snippet runs with these names bound:
  resolve, pm (ProjectManager), project, mediapool, timeline (current, or None), fusion,
  ref("REF") -> live object for a dava REF,  session (handles: session.handles)
Assign the value to return to `result`; Resolve objects in it come back as {"$ref", "$type"}.
Anything the snippet prints is returned as "stdout".
Example:  result = [t.GetName() for t in (timeline.GetItemListInTrack("video", 1) or [])]
"""


def namespace(session):
    resolve = session.resolve
    pm = resolve.GetProjectManager()
    project = pm.GetCurrentProject()
    return {
        "resolve": resolve,
        "pm": pm,
        "project": project,
        "mediapool": project.GetMediaPool() if project else None,
        "timeline": project.GetCurrentTimeline() if project else None,
        "fusion": resolve.Fusion(),
        "ref": lambda text: resolve_ref(session, text)[0],
        "session": session,
        "result": None,
    }


def run(session, code, filename="<exec>", read_only=False):
    if read_only:
        raise ResolveError("exec runs arbitrary code and read-only mode is on.")
    try:
        compiled = compile(code, filename, "exec")
    except SyntaxError as exc:
        raise ResolveError(f"{filename}: syntax error: {exc}") from exc
    scope = namespace(session)
    captured = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured):
            exec(compiled, scope)  # noqa: S102  running user code is the purpose of this command
    except SystemExit as exc:
        raise ResolveError(f"{filename} called exit({exc.code}).") from exc
    except Exception as exc:  # user code: report any failure with its traceback
        detail = "".join(traceback.format_exception(exc)[-6:]).rstrip()
        raise ResolveError(f"{filename} failed:\n{detail}\nstdout so far:\n{captured.getvalue()}") from exc
    return {"result": serialize(session, scope.get("result")), "stdout": captured.getvalue()}
