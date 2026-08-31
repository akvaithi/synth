"""Client for synthd, the launchd-managed Apple layer.

synthd holds the TCC grants because launchd makes it its own responsible process.
Anything spawned from a shell cannot hold them, so all Apple access goes through here.
"""
from __future__ import annotations

import json
import os
import socket

SOCKET_PATH = os.path.expanduser("~/Developer/synth/.state/synthd.sock")


class SynthdError(RuntimeError):
    pass


def call(cmd: str, timeout: float = 90.0, **params):
    req = {"cmd": cmd, **params}
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(SOCKET_PATH)
    except OSError as e:
        raise SynthdError(f"cannot reach synthd at {SOCKET_PATH}: {e}") from e
    with s:
        s.sendall(json.dumps(req).encode())
        s.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            b = s.recv(65536)
            if not b:
                break
            chunks.append(b)
    raw = b"".join(chunks).decode().strip()
    if not raw:
        raise SynthdError(f"empty response from synthd for {cmd!r}")
    resp = json.loads(raw)
    if not resp.get("ok"):
        raise SynthdError(resp.get("error", "unknown synthd error"))
    return resp["result"]
