"""The dava bridge: runs INSIDE DaVinci Resolve and relays API calls from the dava CLI.

The free edition of Resolve does not accept external scripting connections, but it runs scripts from
Workspace > Scripts with a ready `resolve` object. 'dava bridge install' puts a launcher there; running
it calls serve_bridge(resolve), which listens on 127.0.0.1 and serves one dava process at a time.

Security: the port is bound to 127.0.0.1 only, every connection must present a random token that is
written to a file only the current user can read, and only public method names (no '_' prefix) can
be called. The bridge relays API calls; it never evaluates code sent by the client.

This module must run on Resolve's bundled Python with the standard library only.
"""

import hmac
import json
import os
import secrets
import socket
import time

STATE_PATH = os.path.join(os.path.expanduser("~"), ".dava", "bridge.json")
STOP_PATH = os.path.join(os.path.expanduser("~"), ".dava", "bridge.stop")
PROTOCOL = 1


def encode(value, register):
    """JSON-able form of a value; API objects become {"$o": id} via register(obj) -> id."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [encode(v, register) for v in value]
    if isinstance(value, dict):
        # Keys keep their types (Resolve uses int keys, e.g. markers by frame and Fusion tool lists).
        return {"$d": [[encode(k, register), encode(v, register)] for k, v in value.items()]}
    return {"$o": register(value)}


def decode(value, lookup):
    """Inverse of encode; lookup(id) -> object for {"$o": id}."""
    if isinstance(value, list):
        return [decode(v, lookup) for v in value]
    if isinstance(value, dict):
        if "$o" in value:
            return lookup(value["$o"])
        if "$d" in value:
            return {decode(k, lookup): decode(v, lookup) for k, v in value["$d"]}
        return {k: decode(v, lookup) for k, v in value.items()}
    return value


def _write_state(port, token):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    fd = os.open(STATE_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump({"port": port, "token": token, "pid": os.getpid(), "protocol": PROTOCOL,
                   "started": time.strftime("%Y-%m-%d %H:%M:%S")}, handle)
    os.chmod(STATE_PATH, 0o600)


class _Connection:
    def __init__(self, resolve):
        self.objects = {0: resolve}
        self.next_id = 1

    def register(self, obj):
        oid = self.next_id
        self.next_id += 1
        self.objects[oid] = obj
        return oid

    def lookup(self, oid):
        if oid not in self.objects:
            raise KeyError(f"unknown object id {oid}")
        return self.objects[oid]

    def handle(self, request):
        op = request.get("op")
        if op == "call":
            name = request["method"]
            if not isinstance(name, str) or name.startswith("_"):
                raise PermissionError(f"method {name!r} is not callable through the bridge")
            target = self.lookup(request["target"])
            args = decode(request.get("args", []), self.lookup)
            return encode(getattr(target, name)(*args), self.register)
        if op == "getattr":
            name = request["name"]
            if not isinstance(name, str) or name.startswith("_"):
                raise PermissionError(f"attribute {name!r} is not readable through the bridge")
            return encode(getattr(self.lookup(request["target"]), name), self.register)
        if op == "dir":
            return [n for n in dir(self.lookup(request["target"])) if not n.startswith("_")]
        if op == "release":
            for oid in request.get("ids", []):
                if oid != 0:
                    self.objects.pop(oid, None)
            return None
        raise ValueError(f"unknown op {op!r}")


class _Client:
    """One connected dava process: its read buffer and its object table."""

    def __init__(self, conn, resolve):
        self.conn = conn
        self.buffer = b""
        self.authenticated = False
        self.state = _Connection(resolve)

    def send(self, payload):
        self.conn.sendall(json.dumps(payload).encode("utf-8") + b"\n")


def _process_line(client, line, token):
    """Handle one request line. Returns 'close', 'stop' or None."""
    try:
        request = json.loads(line)
    except ValueError as exc:
        client.send({"error": f"ValueError: {exc}"})
        return None
    if not client.authenticated:
        if not isinstance(request, dict) or not hmac.compare_digest(str(request.get("token", "")), token):
            client.send({"error": "PermissionError: bad token"})
            return "close"
        client.authenticated = True
        client.send({"ok": True, "protocol": PROTOCOL})
        return None
    if request.get("op") == "shutdown":
        client.send({"result": None})
        return "stop"
    try:
        client.send({"result": client.state.handle(request)})
    except Exception as exc:  # report every API failure back to the client instead of dying
        client.send({"error": f"{type(exc).__name__}: {exc}"})
    return None


def serve_bridge(resolve, port=0):
    """Serve dava clients on 127.0.0.1 until one sends 'shutdown' or ~/.dava/bridge.stop appears.

    Requests are handled one at a time on the calling (script) thread, so Resolve is only ever called
    from the thread it started the script on; several clients may be connected at once.
    """
    import selectors

    if os.path.exists(STOP_PATH):
        os.remove(STOP_PATH)
    token = secrets.token_hex(32)
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen(8)
    server.setblocking(False)
    selector = selectors.DefaultSelector()
    selector.register(server, selectors.EVENT_READ, None)
    _write_state(server.getsockname()[1], token)
    print(f"dava bridge: listening on 127.0.0.1:{server.getsockname()[1]} (stop with 'dava bridge stop')")

    def drop(client):
        selector.unregister(client.conn)
        client.conn.close()

    stopping = False
    try:
        while not stopping and not os.path.exists(STOP_PATH):
            for key, _ in selector.select(timeout=1.0):
                if key.data is None:
                    conn, _ = server.accept()
                    conn.setblocking(True)
                    selector.register(conn, selectors.EVENT_READ, _Client(conn, resolve))
                    continue
                client = key.data
                try:
                    chunk = client.conn.recv(65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    drop(client)
                    continue
                client.buffer += chunk
                while b"\n" in client.buffer:
                    line, client.buffer = client.buffer.split(b"\n", 1)
                    outcome = _process_line(client, line, token)
                    if outcome == "close":
                        drop(client)
                        break
                    if outcome == "stop":
                        stopping = True
                        break
    finally:
        for key in list(selector.get_map().values()):
            key.fileobj.close()
        selector.close()
        for path in (STATE_PATH, STOP_PATH):
            if os.path.exists(path):
                os.remove(path)
        print("dava bridge: stopped")
