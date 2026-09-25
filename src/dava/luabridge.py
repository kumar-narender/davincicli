"""Client for the Lua dava bridge (src/dava/bridge.lua), for the free edition of Resolve.

Requests are written to ~/.dava/lua/request.lua as Lua data; the bridge answers by raising
"DAVA-RESP <seq> <part>/<total> <json>" errors through fusion:Execute, which Resolve writes to its
log. This client tails that log. One request is in flight at a time; a lock file serialises
concurrent dava processes.
"""

import fcntl
import json
import math
import os
import re
import sys
import time

from .connect import ResolveError
from .inapp import decode

LUA_DIR = os.path.join(os.path.expanduser("~"), ".dava", "lua")
BRIDGE_LUA = os.path.join(os.path.expanduser("~"), ".dava", "bridge.lua")
CONSOLE_LINE = 'dofile(os.getenv("HOME") .. "/.dava/bridge.lua")'


def resolve_log_path():
    if sys.platform.startswith("darwin"):
        return os.path.join(os.path.expanduser("~"),
                            "Library/Application Support/Blackmagic Design/DaVinci Resolve/logs/davinci_resolve.log")
    if sys.platform.startswith("win"):
        return os.path.join(os.environ.get("APPDATA", ""), "Blackmagic Design", "DaVinci Resolve", "Support",
                            "logs", "davinci_resolve.log")
    return os.path.join(os.path.expanduser("~"), ".local/share/DaVinciResolve/logs/davinci_resolve.log")


def lua_literal(value):
    """Python value -> Lua literal. None is DAVA_NIL; dict keys keep their types."""
    if value is None:
        return "DAVA_NIL"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ResolveError(f"Cannot send {value!r} to Resolve.")
        return repr(value)
    if isinstance(value, str):
        out = []
        for byte in value.encode("utf-8"):
            char = chr(byte)
            if char in '"\\' or byte < 32 or byte > 126:
                out.append(f"\\{byte:03d}")  # Lua 5.1 decimal escape, byte exact
            else:
                out.append(char)
        return '"' + "".join(out) + '"'
    if isinstance(value, (list, tuple)):
        return "{" + ",".join(lua_literal(v) for v in value) + "}"
    if isinstance(value, dict):
        return "{" + ",".join(f"[{lua_literal(k)}]={lua_literal(v)}" for k, v in value.items()) + "}"
    raise ResolveError(f"Cannot send a {type(value).__name__} to Resolve.")


def normalize(value):
    """Turn Resolve's Lua tables into Python lists and dicts.

    The Lua API marks list tables with a hidden "__flags" key (4194304 in Resolve 21.1); dict tables
    have none. Flagged tables become lists ordered by their integer keys; the flag itself is dropped.
    """
    if isinstance(value, list):
        return [normalize(v) for v in value]
    if isinstance(value, dict):
        items = {k: normalize(v) for k, v in value.items() if k != "__flags"}
        if "__flags" in value and all(isinstance(k, int) and not isinstance(k, bool) for k in items):
            return [items[k] for k in sorted(items)]
        return items
    return value


# Calls that hang Resolve when made from the Lua console thread (seen on Resolve 21.1, free edition):
# the console, the bridge and Resolve's UI stop responding and Resolve must be force-quit.
UNSAFE_METHODS = {
    "GetInput": "Reading Fusion tool inputs through the Lua bridge hangs Resolve. Export the composition with "
                "'dava call REF ExportFusionComp PATH 1' and read the file instead.",
}

_RESPONSE = re.compile(rb"DAVA-RESP (\d+) (\d+)/(\d+) (.*)$")


class LuaBridgeClient:
    def __init__(self, lua_dir=LUA_DIR, log_path=None, hello_timeout=2.0, call_timeout=300.0):
        self.dir = lua_dir
        self.log_path = log_path or resolve_log_path()
        self.call_timeout = call_timeout
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        self._lock = open(os.path.join(self.dir, "lock"), "w")
        # Sequence numbers only need to differ from the previous request the loop saw.
        self.seq = int(time.time() * 1000) % 10**12
        reply = self.request(op="hello", _timeout=hello_timeout)
        if not isinstance(reply, dict) or reply.get("protocol") != 1:
            raise ResolveError(f"Unexpected reply from the Lua bridge: {reply!r}")

    def _log_size(self):
        try:
            return os.path.getsize(self.log_path)
        except OSError:
            return 0

    def _open_log(self, offset):
        """Open the log at `offset`; returns (handle, inode) or (None, None) if it does not exist yet."""
        try:
            handle = open(self.log_path, "rb")
        except OSError:
            return None, None
        handle.seek(min(offset, os.fstat(handle.fileno()).st_size))
        return handle, os.fstat(handle.fileno()).st_ino

    def _await(self, seq, offset, timeout):
        """Collect the reply chunks for `seq` from Resolve's log, following log rotation.

        Resolve rotates davinci_resolve.log (renames it into LogArchive and starts a new file). The open
        handle keeps reading the old file, so a reply written just before the rotation is not lost; once
        the old file is drained, reading continues from the start of the new file.
        """
        parts, total = {}, None
        deadline = time.monotonic() + timeout
        buffer = b""
        handle, inode = self._open_log(offset)
        try:
            while time.monotonic() < deadline:
                data = handle.read() if handle else b""
                if data:
                    buffer += data
                    *lines, buffer = buffer.split(b"\n")
                    for line in lines:
                        match = _RESPONSE.search(line.rstrip(b"\r"))
                        if match and int(match.group(1)) == seq:
                            parts[int(match.group(2))] = match.group(4)
                            total = int(match.group(3))
                    if total is not None and len(parts) == total:
                        body = b"".join(parts[i] for i in range(1, total + 1))
                        return json.loads(body.decode("utf-8"))
                    continue
                try:
                    current = os.stat(self.log_path).st_ino
                except OSError:
                    current = None
                if handle is None or (current is not None and current != inode):
                    # Rotated (or first created): the old file is drained, follow the new one from its start.
                    if handle:
                        handle.close()
                    handle, inode = self._open_log(0)
                    buffer = b""
                    continue
                time.sleep(0.02)
        finally:
            if handle:
                handle.close()
        return None

    def request(self, _timeout=None, **payload):
        if payload.get("op") == "call" and payload.get("method") in UNSAFE_METHODS:
            raise ResolveError(UNSAFE_METHODS[payload["method"]])
        fcntl.flock(self._lock, fcntl.LOCK_EX)
        try:
            self.seq += 1
            seq = self.seq
            args = payload.pop("args", None)
            fields = {"seq": seq, **payload}
            if args is not None:
                fields["args"] = list(args)
                fields["n"] = len(args)
            text = "return {" + ",".join(f"[{lua_literal(k)}]={lua_literal(v)}" for k, v in fields.items()) + "}\n"
            offset = self._log_size()
            request_path = os.path.join(self.dir, "request.lua")
            temp_path = request_path + ".tmp"
            with open(temp_path, "w", encoding="ascii") as handle:
                handle.write(text)
            os.replace(temp_path, request_path)
            reply = self._await(seq, offset, _timeout or self.call_timeout)
            os.remove(request_path)
        finally:
            fcntl.flock(self._lock, fcntl.LOCK_UN)
        if reply is None:
            from .remote import ConnectionLost

            raise ConnectionLost(
                "The Lua dava bridge did not answer. In Resolve open Workspace > Console (Lua) and run: " + CONSOLE_LINE
            )
        if "error" in reply:
            from .remote import BridgeError

            raise BridgeError(reply["error"])
        return normalize(decode(reply.get("result"), self.proxy))

    def proxy(self, oid):
        from .remote import RemoteObject

        return RemoteObject(self, oid)

    def encode_args(self, args):
        return [_to_wire(a) for a in args]


def _to_wire(value):
    """Arguments for Lua: remote objects become {"$o": id}; lists and dicts stay native Lua tables."""
    from .remote import RemoteObject

    if isinstance(value, RemoteObject):
        return {"$o": value._oid}
    if isinstance(value, (list, tuple)):
        return [_to_wire(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_wire(v) for k, v in value.items()}
    return value


def connect():
    """Return the Resolve root object through a running Lua bridge, or None if none answers."""
    if not os.path.isdir(LUA_DIR):
        return None
    try:
        return LuaBridgeClient().proxy(0)
    except ResolveError:
        return None


def request_stop(lua_dir=LUA_DIR, wait=2.0):
    """Ask the Lua loop to stop, then clear the stop flag so the next start is not stopped at once."""
    os.makedirs(lua_dir, mode=0o700, exist_ok=True)
    stop_path = os.path.join(lua_dir, "stop")
    with open(stop_path, "w", encoding="ascii") as handle:
        handle.write("stop\n")
    time.sleep(wait)
    os.remove(stop_path)
