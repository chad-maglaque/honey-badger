"""
Portal side of the sandboxed Python runner.

The runner is a socket-activated systemd unit (os/systemd/prepper-solver-sandbox.socket and
prepper-solver-sandbox@.service): each connection starts a fresh, throw-away instance with a dynamic
unprivileged user, no network (PrivateNetwork, IPAddressDeny, AF_UNIX only), the family's data
(/etc/prepper, /var/lib/prepper, /srv, /media, /run/prepper) made inaccessible, a read-only system,
256 MB of memory, 16 tasks and a 20-second life. Only the portal's group may connect.

The rule (the Badger must never be turned against its owners): code is run ONLY when a probe run
inside the sandbox proves that /etc/prepper and /var/lib/prepper are unreadable, that internet
sockets can't even be created, and that it runs as a different, unprivileged user. Any doubt (no
socket, old systemd, a probe that can read a file) → the Python tool is disabled. There is no
setting to run code without the sandbox.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time

DEFAULT_SOCKET = "/run/prepper-solver/sandbox.sock"
MAX_REPLY = 256 * 1024


PATHS = {"etc_prepper": "/etc/prepper", "var_lib_prepper": "/var/lib/prepper", "prepper_toml": "/etc/prepper/prepper.toml",
         "members": "the family members file", "data_root": "/srv/prepper"}


class SandboxUnavailable(RuntimeError):
    pass


def verify_probe(p, portal_uid: int | None = None) -> tuple[bool, str]:
    """Is this probe result proof of isolation?"""
    if not isinstance(p, dict):
        return False, "no probe result"
    portal_uid = os.getuid() if portal_uid is None else portal_uid
    for key in ("etc_prepper", "var_lib_prepper"):
        if p.get(key) != "blocked":
            return False, f"{PATHS[key]} isn't proven blocked inside the sandbox ({str(p.get(key))[:20]})"
    for key in ("prepper_toml", "members", "data_root"):
        if p.get(key) == "readable":
            return False, f"the sandbox can read {PATHS[key]}"
    if p.get("inet") != "blocked" or p.get("inet6") != "blocked":
        return False, "the sandbox can open network sockets"
    if p.get("portal_reachable"):
        return False, "the sandbox can reach the portal"
    uid = p.get("uid")
    if not isinstance(uid, int) or isinstance(uid, bool) or uid == 0 or uid == portal_uid or p.get("euid") == 0:
        return False, "the sandbox doesn't run as a separate unprivileged user"
    if p.get("no_new_privs") is not True:
        return False, "the sandbox can gain privileges (NoNewPrivileges is off)"
    return True, "isolated"


class SandboxClient:
    def __init__(self, socket_path: str = DEFAULT_SOCKET, verify: bool = True, timeout: float = 20.0,
                 probe_ttl: float = 600.0, clock=time.monotonic):
        # verify=False exists only so the tests can exercise the runner's inner layers on a laptop;
        # the solver feature always constructs it with verify=True.
        self.path = socket_path
        self.verify = verify
        self.timeout = timeout
        self.probe_ttl = probe_ttl
        self.clock = clock
        self._state: tuple[float, bool, str] | None = None
        self._lock = threading.Lock()

    def _request(self, obj: dict) -> dict:
        data = (json.dumps(obj) + "\n").encode()
        if len(data) > 32_768:
            raise SandboxUnavailable("code too long")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        try:
            s.connect(self.path)
            s.sendall(data)
            try:
                s.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            buf = b""
            while len(buf) <= MAX_REPLY:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        except (OSError, socket.timeout) as e:
            raise SandboxUnavailable(f"the sandbox didn't answer ({type(e).__name__})") from None
        finally:
            s.close()
        try:
            res = json.loads(buf.decode("utf-8", "replace").strip().splitlines()[-1]) if buf.strip() else None
        except (ValueError, IndexError, RecursionError):
            res = None
        if not isinstance(res, dict):
            raise SandboxUnavailable("the sandbox gave a bad reply")
        return res

    def status(self, refresh: bool = False) -> tuple[bool, str]:
        with self._lock:
            now = self.clock()
            if self._state and not refresh and now - self._state[0] < self.probe_ttl:
                return self._state[1], self._state[2]
            if not os.path.exists(self.path):
                st = (False, "the sandbox service isn't installed")
            else:
                try:
                    res = self._request({"op": "probe"})
                    st = verify_probe(res.get("probe")) if self.verify else (True, "not verified (test mode)")
                except SandboxUnavailable as e:
                    st = (False, str(e))
            self._state = (now, st[0], st[1])
            return st

    def available(self) -> bool:
        return self.status()[0]

    def run(self, code: str, timeout: float = 10.0) -> dict:
        ok, why = self.status()
        if not ok:
            raise SandboxUnavailable(f"the Python runner is off: {why}")
        res = self._request({"op": "run", "code": code, "timeout": timeout})
        if not res.get("ok"):
            return {"stdout": "", "stderr": str(res.get("error", "failed"))[:500], "exit": None, "timed_out": False}
        return res
