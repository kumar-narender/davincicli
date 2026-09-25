"""Client side of the dava bridge: proxies that forward API calls to a bridge running inside Resolve."""

import json
import os
import socket

from .connect import ResolveError
from .inapp import STATE_PATH, STOP_PATH, decode, encode


class BridgeError(ResolveError):
    """An API call through the bridge failed inside Resolve."""


class ConnectionLost(ResolveError):
    """The bridge went away (script stopped or Resolve quit)."""


def read_state():
    """The running bridge's {port, token, pid, ...}, or None when no bridge is running."""
    if not os.path.isfile(STATE_PATH):
        return None
    with open(STATE_PATH, encoding="utf-8") as handle:
        return json.load(handle)


class BridgeClient:
    def __init__(self, state, timeout=3600):
        try:
            self.sock = socket.create_connection(("127.0.0.1", state["port"]), timeout=5)
        except OSError as exc:
            raise ResolveError(
                f"The dava bridge is not answering on port {state['port']} ({exc.strerror or exc}). "
                "Run Workspace > Scripts > dava bridge in Resolve again."
            ) from exc
        self.sock.settimeout(timeout)
        self.reader = self.sock.makefile("rb")
        self.writer = self.sock.makefile("wb")
        self._send({"token": state["token"]})
        reply = self._receive()
        if not reply.get("ok"):
            raise ResolveError(f"The dava bridge refused the connection: {reply.get('error')}")

    def _send(self, payload):
        try:
            self.writer.write(json.dumps(payload).encode("utf-8") + b"\n")
            self.writer.flush()
        except OSError as exc:
            raise ConnectionLost(f"Lost the dava bridge connection ({exc}).") from exc

    def _receive(self):
        try:
            line = self.reader.readline()
        except OSError as exc:
            raise ConnectionLost(f"Lost the dava bridge connection ({exc}).") from exc
        if not line:
            raise ConnectionLost("The dava bridge closed the connection (was the script stopped in Resolve?).")
        return json.loads(line)

    def close(self):
        for stream in (self.reader, self.writer, self.sock):
            stream.close()

    def request(self, **payload):
        self._send(payload)
        reply = self._receive()
        if "error" in reply:
            raise BridgeError(reply["error"])
        return decode(reply.get("result"), self.proxy)

    def proxy(self, oid):
        return RemoteObject(self, oid)

    def encode_args(self, args):
        return [encode(a, _register_remote) for a in args]

    def shutdown(self):
        self._send({"op": "shutdown"})
        self._receive()


def _register_remote(obj):
    if isinstance(obj, RemoteObject):
        return obj._oid
    raise TypeError(f"Cannot send a local {type(obj).__name__} object to Resolve.")


class RemoteObject:
    """A Resolve API object living in the bridge; method calls are forwarded."""

    __slots__ = ("_client", "_oid")

    def __init__(self, client, oid):
        self._client = client
        self._oid = oid

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if name.isupper():
            # resolve.* constants (SCALE_FILL, EXPORT_EDL, ...) are values, not methods; cache them.
            cache = self._client.__dict__.setdefault("_constants", {})
            key = (self._oid, name)
            if key not in cache:
                cache[key] = self._client.request(op="getattr", target=self._oid, name=name)
            return cache[key]

        def method(*args):
            return self._client.request(op="call", target=self._oid, method=name, args=self._client.encode_args(args))

        method.__name__ = name
        return method

    def __dir__(self):
        return self._client.request(op="dir", target=self._oid)

    def _dava_attr(self, name):
        """Read a plain attribute (e.g. a Fusion tool input) instead of calling a method."""
        return self._client.request(op="getattr", target=self._oid, name=name)

    def __repr__(self):
        return f"<Resolve object #{self._oid} via dava bridge>"


def connect():
    """Return the Resolve root object through a running bridge, or None when there is none."""
    state = read_state()
    if state is None:
        return None
    return BridgeClient(state).proxy(0)


def request_stop():
    """Ask a running bridge to stop (it checks for the stop file every second)."""
    os.makedirs(os.path.dirname(STOP_PATH), exist_ok=True)
    with open(STOP_PATH, "w", encoding="utf-8") as handle:
        handle.write("stop\n")
