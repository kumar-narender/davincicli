"""dava bridge: control the free edition of Resolve through a script running inside it."""

import os
import sys
import time

from .. import luabridge, remote
from ..connect import ResolveError

LAUNCHER_NAME = "dava_bridge.py"


def scripts_dir():
    """The per-user Fusion/Scripts/Utility folder (listed in Workspace > Scripts on every page)."""
    home = os.path.expanduser("~")
    if sys.platform.startswith("darwin"):
        base = os.path.join(home, "Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts")
    elif sys.platform.startswith("win"):
        base = os.path.join(os.environ.get("APPDATA", ""), "Blackmagic Design", "DaVinci Resolve", "Support", "Fusion", "Scripts")
    else:
        base = os.path.join(home, ".local/share/DaVinciResolve/Fusion/Scripts")
    return os.path.join(base, "Utility")


def launcher_source():
    source_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return (
        '"""dava bridge: lets the dava CLI control this Resolve. Stop it with \'dava bridge stop\'."""\n'
        "import sys\n"
        f"sys.path.insert(0, {source_root!r})\n"
        "from dava.inapp import serve_bridge\n"
        "try:\n"
        "    resolve  # noqa: B018 - defined by Resolve for menu and console scripts\n"
        "except NameError:\n"
        "    resolve = bmd.scriptapp('Resolve')  # noqa: F821 - started via fusion:RunScript\n"
        "serve_bridge(resolve)\n"
    )


def install_lua():
    """Copy the Lua bridge to ~/.dava/bridge.lua and create its private request folder."""
    source = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bridge.lua")
    os.makedirs(luabridge.LUA_DIR, mode=0o700, exist_ok=True)
    os.chmod(os.path.dirname(luabridge.LUA_DIR), 0o700)
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    with open(luabridge.BRIDGE_LUA, "w", encoding="utf-8") as handle:
        handle.write(text)
    return luabridge.BRIDGE_LUA


def cmd_install(resolve, args):
    folder = args.dir or scripts_dir()
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, LAUNCHER_NAME)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(launcher_source())
    return {
        "python_launcher": path,
        "lua_bridge": install_lua(),
        "start_free_edition": "Resolve > Workspace > Console, Lua mode, run: " + luabridge.CONSOLE_LINE,
        "start_with_python": "Resolve > Workspace > Scripts > dava_bridge (if Python scripts are available)",
    }


def cmd_uninstall(resolve, args):
    path = os.path.join(args.dir or scripts_dir(), LAUNCHER_NAME)
    if not os.path.isfile(path):
        raise ResolveError(f"No bridge launcher at {path}.")
    os.remove(path)
    return {"removed": path}


def cmd_status(resolve, args):
    state = remote.read_state()
    installed = os.path.join(args.dir or scripts_dir(), LAUNCHER_NAME)
    info = {"launcher": installed if os.path.isfile(installed) else None,
            "lua_bridge": luabridge.BRIDGE_LUA if os.path.isfile(luabridge.BRIDGE_LUA) else None,
            "running": None}
    if state is not None:
        root = remote.BridgeClient(state).proxy(0)
        info.update({"running": "python", "port": state["port"], "pid": state["pid"]})
    else:
        root = luabridge.connect()
        if root is None:
            return info
        info["running"] = "lua"
    info.update({"product": root.GetProductName(), "version": root.GetVersionString()})
    return info


def cmd_stop(resolve, args):
    if remote.read_state() is None:
        if not os.path.isdir(luabridge.LUA_DIR):
            raise ResolveError("No dava bridge is running.")
        luabridge.request_stop()
        return "Asked the Lua bridge to stop."
    remote.request_stop()
    for _ in range(int(args.timeout * 10)):
        if remote.read_state() is None:
            return "The dava bridge stopped."
        time.sleep(0.1)
    raise ResolveError("The bridge did not stop in time; it stops within a second of its next idle moment.")


def register(registry):
    help_text = "control the free edition of Resolve through a script running inside it"
    p = registry.action("bridge", "install", "put the 'dava bridge' script into Resolve's Workspace > Scripts menu",
                        group_help=help_text)
    p.add_argument("--dir", help="scripts folder (default: the per-user Fusion/Scripts/Utility folder)")
    p.set_defaults(func=cmd_install, needs_resolve=False, read_only=False)

    p = registry.action("bridge", "uninstall", "remove the bridge script from the Scripts menu")
    p.add_argument("--dir", help="scripts folder (default: the per-user Fusion/Scripts/Utility folder)")
    p.set_defaults(func=cmd_uninstall, needs_resolve=False, read_only=False)

    p = registry.action("bridge", "status", "show whether the bridge is installed and running")
    p.add_argument("--dir", help="scripts folder (default: the per-user Fusion/Scripts/Utility folder)")
    p.set_defaults(func=cmd_status, needs_resolve=False, read_only=True)

    p = registry.action("bridge", "stop", "stop a running bridge (Resolve keeps running)")
    p.add_argument("--timeout", type=float, default=5.0, help="seconds to wait (default: 5)")
    p.set_defaults(func=cmd_stop, needs_resolve=False, read_only=False)
