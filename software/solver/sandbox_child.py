#!/usr/bin/env python3
"""
Runs ONE request from the problem solver inside the sandbox unit
(os/systemd/prepper-solver-sandbox@.service: one instance per connection, a throw-away dynamic
user, no network namespace, AF_UNIX only, /etc/prepper /var/lib/prepper /srv … inaccessible,
read-only system, 256 MB, 16 tasks, 20 s). stdin/stdout are the connection.

Request (one JSON line, ≤ 32 KB):
    {"op": "run", "code": "print(2+2)", "timeout": 10}
    {"op": "probe"}         → what the OS isolation actually allows (the portal refuses to use the
                              runner unless files and network are proven blocked)
Reply: one JSON object.

Inside, the snippet runs in a fresh process with resource limits (CPU, address space, file size,
open files, processes, no core dumps), in its own session, in an empty temporary folder, with an
empty environment, and under an audit hook that refuses file access outside that folder and the
Python standard library, sockets, subprocesses, fork/exec, ctypes and friends. The audit hook is
a second layer only: the boundary is the OS isolation above, which the portal verifies.

Standard library only. Also importable (tests call run_request / probe directly).
"""
from __future__ import annotations

import errno
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

MAX_REQUEST = 32_768
MAX_CODE = 8_000
OUT_CAP = 16_000
MEM_BYTES = 256 * 1024 * 1024
FILE_BYTES = 512 * 1024

GUARD = r'''
import sys
def _install():
    import os, sysconfig
    work = os.path.realpath(os.getcwd())
    ro = set()
    for k in ("stdlib", "platstdlib"):
        p = sysconfig.get_paths().get(k)
        if p:
            ro.add(os.path.realpath(p))
    ro.add(os.path.realpath(os.path.dirname(os.__file__)))
    ro = tuple(sorted(ro))
    devs = ("/dev/null", "/dev/urandom", "/dev/random")
    blocked_mods = {"ctypes", "_ctypes", "socket", "_socket", "ssl", "_ssl", "subprocess", "_posixsubprocess",
                    "multiprocessing", "_multiprocessing", "mmap", "pty", "urllib", "http", "ftplib", "smtplib",
                    "poplib", "imaplib", "nntplib", "telnetlib", "webbrowser", "sqlite3", "_sqlite3", "_testcapi",
                    "_xxsubinterpreters", "_xxinterpchannels", "asyncio", "selectors", "select", "resource",
                    "_posixshmem", "xmlrpc", "socketserver", "concurrent", "_tkinter", "tkinter", "readline", "faulthandler"}
    prefixes = ("socket.", "subprocess.", "os.exec", "os.spawn", "os.posix_spawn", "os.fork", "os.system", "os.kill",
                "os.killpg", "os.setuid", "os.setgid", "os.setre", "os.setres", "os.chroot", "os.chown", "os.lchown",
                "os.chmod", "os.lchmod", "os.chflags", "os.link", "os.symlink", "os.mkfifo", "os.mknod",
                "os.startfile", "os.chdir", "os.fchdir", "os.setxattr", "os.removexattr", "os.listxattr",
                "os.getxattr", "pty.", "ctypes.", "urllib.", "http.", "ftplib.", "smtplib.", "poplib.", "imaplib.",
                "nntplib.", "telnetlib.", "webbrowser.", "sqlite3.", "mmap.", "resource.", "signal.pthread_kill",
                "sys._current_frames", "gc.get_", "code.__new__", "marshal.load", "cpython.",
                "shutil.chown", "msvcrt.", "winreg.", "_winapi.", "sys.addaudithook", "sys.setprofile", "sys.settrace",
                "object.__delattr__", "function.__new__", "os.add_dll_directory", "fcntl.", "syslog.")
    W = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

    def inside(p):
        return p == work or p.startswith(work + os.sep)

    def real(path):
        if isinstance(path, int):
            return None
        try:
            return os.path.realpath(os.fsdecode(path))
        except Exception:
            raise PermissionError("bad path")

    def hook(event, args):
        if event == "import":
            name = str(args[0])
            if name in blocked_mods or name.split(".")[0] in blocked_mods:
                raise PermissionError("import of " + name + " is not allowed in the sandbox")
            return
        if event == "open":
            path, mode, flags = args
            if path is None or isinstance(path, int):
                return
            p = real(path)
            write = any(c in str(mode or "") for c in "wax+") or bool((flags or 0) & W)
            if inside(p):
                return
            if not write and (p in devs or any(p == r or p.startswith(r + os.sep) for r in ro)):
                return
            raise PermissionError("access to " + str(path)[:80] + " is not allowed in the sandbox")
        if event in ("os.listdir", "os.scandir"):
            p = real(args[0] if args and args[0] is not None else ".")
            if p is None or inside(p) or any(p == r or p.startswith(r + os.sep) for r in ro):
                return
            raise PermissionError("listing " + str(args[0])[:80] + " is not allowed in the sandbox")
        if event in ("os.remove", "os.rmdir", "os.truncate", "os.mkdir", "os.rename", "os.utime", "shutil.rmtree",
                     "shutil.copyfile", "shutil.copytree", "shutil.move"):
            for a in args[:2]:
                if isinstance(a, (str, bytes)) and not inside(real(a)):
                    raise PermissionError(event + " outside the work folder is not allowed in the sandbox")
            return
        if event.startswith(prefixes):
            raise PermissionError(event + " is not allowed in the sandbox")

    sys.addaudithook(hook)
    try:
        import _imp
        orig = _imp.create_dynamic

        def create_dynamic(spec, *a):
            if str(getattr(spec, "name", "")).split(".")[0] in blocked_mods:
                raise PermissionError("loading that module is not allowed in the sandbox")
            return orig(spec, *a)
        _imp.create_dynamic = create_dynamic
    except Exception:
        pass
_install()
del _install
with open("snippet.py", encoding="utf-8") as _f:
    _src = _f.read()
del _f
_code = compile(_src, "snippet.py", "exec")
del _src
exec(_code, {"__name__": "__main__", "__builtins__": __builtins__})
'''


def _limits(cpu: int) -> None:
    """In the snippet's process, before exec."""
    os.setsid()
    try:
        import resource
        for lim, val in ((resource.RLIMIT_CPU, (cpu, cpu + 1)), (resource.RLIMIT_FSIZE, (FILE_BYTES, FILE_BYTES)),
                         (resource.RLIMIT_NOFILE, (32, 32)), (resource.RLIMIT_CORE, (0, 0))):
            resource.setrlimit(lim, val)
        for name, val in (("RLIMIT_AS", MEM_BYTES), ("RLIMIT_NPROC", 16)):
            lim = getattr(resource, name, None)
            if lim is not None:
                try:
                    resource.setrlimit(lim, (val, val))
                except (ValueError, OSError):
                    pass                     # macOS ignores/refuses some limits; the unit enforces them on the Badger
    except ImportError:
        pass


def _read_cap(path: str) -> tuple[str, bool]:
    try:
        with open(path, "rb") as fh:
            data = fh.read(OUT_CAP + 1)
    except OSError:
        return "", False
    return data[:OUT_CAP].decode("utf-8", "replace"), len(data) > OUT_CAP


def run_code(code: str, timeout: float = 10.0) -> dict:
    if not isinstance(code, str) or not code.strip():
        return {"ok": False, "error": "no code"}
    if len(code) > MAX_CODE:
        return {"ok": False, "error": f"code too long (limit {MAX_CODE} characters)"}
    timeout = max(1.0, min(float(timeout), 10.0))
    work = tempfile.mkdtemp(prefix="sbx-")
    try:
        os.chmod(work, 0o700)
        with open(os.path.join(work, "snippet.py"), "w", encoding="utf-8") as fh:
            fh.write(code)
        out_p, err_p = os.path.join(work, ".out"), os.path.join(work, ".err")
        with open(out_p, "wb") as out, open(err_p, "wb") as err:
            start = time.monotonic()
            proc = subprocess.Popen([sys.executable, "-I", "-S", "-B", "-X", "pycache_prefix=" + os.path.join(work, ".nopyc"),
                                     "-c", GUARD], cwd=work, stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=err, close_fds=True,
                                    env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONIOENCODING": "utf-8"},
                                    preexec_fn=lambda: _limits(int(timeout) + 1))   # noqa: PLW1509 — single-threaded
            timed_out = False
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
            for _ in range(2):                    # the whole session: anything it forked dies with it
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            elapsed = time.monotonic() - start
        stdout, t1 = _read_cap(out_p)
        stderr, t2 = _read_cap(err_p)
        rc = proc.returncode
        sig = -rc if isinstance(rc, int) and rc < 0 else None
        if sig in (signal.SIGXCPU, signal.SIGKILL) and not timed_out and elapsed >= timeout * 0.8:
            timed_out = True
        return {"ok": True, "stdout": stdout, "stderr": stderr[-4000:], "exit": rc, "timed_out": timed_out,
                "truncated": t1 or t2, "seconds": round(elapsed, 2),
                "signal": sig}
    finally:
        shutil.rmtree(work, ignore_errors=True)


PROBE_PATHS = (("etc_prepper", "/etc/prepper"), ("prepper_toml", "/etc/prepper/prepper.toml"),
               ("var_lib_prepper", "/var/lib/prepper"), ("members", "/var/lib/prepper/portal/members.json"),
               ("data_root", "/srv/prepper"))


def probe() -> dict:
    """What this process can actually reach (no audit hook: this tests the OS layer)."""
    import socket
    r: dict = {"uid": os.getuid(), "euid": os.geteuid(), "pid": os.getpid()}
    for key, path in PROBE_PATHS:
        try:
            if os.path.isdir(path):
                os.listdir(path)
            else:
                with open(path, "rb") as fh:
                    fh.read(1)
            r[key] = "readable"
        except PermissionError:
            r[key] = "blocked"
        except FileNotFoundError:
            r[key] = "absent"
        except OSError as e:
            r[key] = "blocked" if e.errno in (errno.EACCES, errno.EPERM) else f"error {e.errno}"
    for key, fam in (("inet", socket.AF_INET), ("inet6", getattr(socket, "AF_INET6", None))):
        if fam is None:
            r[key] = "blocked"
            continue
        try:
            s = socket.socket(fam, socket.SOCK_STREAM)
            s.close()
            r[key] = "allowed"
        except OSError:
            r[key] = "blocked"
    try:
        c = socket.create_connection(("127.0.0.1", 8000), timeout=1)
        c.close()
        r["portal_reachable"] = True
    except OSError:
        r["portal_reachable"] = False
    nnp = None
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("NoNewPrivs:"):
                    nnp = line.split()[1] == "1"
    except OSError:
        pass
    r["no_new_privs"] = nnp
    return r


def run_request(req) -> dict:
    if not isinstance(req, dict):
        return {"ok": False, "error": "bad request"}
    op = req.get("op")
    if op == "probe":
        return {"ok": True, "probe": probe()}
    if op == "run":
        to = req.get("timeout", 10)
        if isinstance(to, bool) or not isinstance(to, (int, float)):
            to = 10
        return run_code(req.get("code"), to)
    return {"ok": False, "error": "unknown op"}


def main() -> int:
    raw = sys.stdin.buffer.readline(MAX_REQUEST + 1)
    if len(raw) > MAX_REQUEST:
        res = {"ok": False, "error": "request too large"}
    else:
        try:
            res = run_request(json.loads(raw.decode("utf-8", "replace")))
        except (ValueError, RecursionError):
            res = {"ok": False, "error": "bad request"}
    sys.stdout.write(json.dumps(res) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
