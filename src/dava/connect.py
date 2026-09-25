"""Locate the DaVinciResolveScript module and connect to a running Resolve instance."""

import importlib
import os
import sys

# Default locations of the Developer/Scripting folder, per the Resolve scripting README.
_DEFAULT_API_DIRS = {
    "darwin": "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting",
    "linux": "/opt/resolve/Developer/Scripting",
}


class ResolveError(Exception):
    """Raised when Resolve cannot be reached or an API call reports failure."""


def api_dir():
    """Return the Resolve Developer/Scripting folder: RESOLVE_SCRIPT_API, else the platform default."""
    if os.environ.get("RESOLVE_SCRIPT_API"):
        return os.environ["RESOLVE_SCRIPT_API"]
    if sys.platform.startswith("win"):
        programdata = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
        return os.path.join(programdata, "Blackmagic Design", "DaVinci Resolve", "Support", "Developer", "Scripting")
    for prefix, path in _DEFAULT_API_DIRS.items():
        if sys.platform.startswith(prefix):
            return path
    return None


def stub_path():
    """Return the path of the SDK's DaVinciResolveScript.pyi API definition (it may not exist)."""
    base = api_dir()
    return os.path.join(base, "DaVinciResolveScript.pyi") if base else None


def load_script_module():
    """Import DaVinciResolveScript, adding the SDK Modules folder to sys.path if needed.

    The module itself resolves fusionscript from RESOLVE_SCRIPT_LIB or the default
    install location, so only the Modules folder has to be discoverable here.
    """
    try:
        return importlib.import_module("DaVinciResolveScript")
    except ImportError:
        pass

    base = api_dir()
    module_dir = os.path.join(base, "Modules") if base else None
    if not module_dir or not os.path.isdir(module_dir):
        raise ResolveError(
            "Cannot find the DaVinciResolveScript module. Install DaVinci Resolve or set "
            "RESOLVE_SCRIPT_API to its Developer/Scripting folder."
        )
    if module_dir not in sys.path:
        sys.path.append(module_dir)
    try:
        return importlib.import_module("DaVinciResolveScript")
    except ImportError as exc:
        raise ResolveError(
            f"Found {module_dir} but could not load fusionscript: {exc}. "
            "Set RESOLVE_SCRIPT_LIB to the fusionscript library path."
        ) from exc


def get_resolve():
    """Return the Resolve application object, or raise ResolveError if it is not reachable.

    Tries Resolve's external scripting first (Studio), then a dava bridge running inside Resolve
    (works with the free edition; see 'dava bridge install').
    """
    module = load_script_module()
    resolve = module.scriptapp("Resolve")
    if resolve is not None:
        return resolve
    # Imported here: both bridges depend on this module.
    from .luabridge import connect as connect_lua
    from .remote import connect as connect_python

    resolve = connect_python() or connect_lua()
    if resolve is None:
        raise ResolveError(
            "Could not connect to DaVinci Resolve. With Resolve Studio, set Preferences > System > General > "
            "External scripting using to Local. With the free edition, run 'dava bridge install' once, then in "
            "Resolve open Workspace > Console (Lua) and run: dofile(os.getenv(\"HOME\") .. \"/.dava/bridge.lua\")"
        )
    return resolve
