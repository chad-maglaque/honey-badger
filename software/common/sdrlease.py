"""
sdrlease — share one RTL-SDR dongle between services without fighting over it.

The owner (alertd, on the "weather" dongle) holds an exclusive flock on
/run/prepper/sdr/<device>.lock while it listens. A borrower (a weather-satellite
pass) asks for the dongle:

    from sdrlease import borrow, Busy
    try:
        with borrow("weather", owner="satpass", seconds=900, wait=60):
            run_capture()               # the dongle is yours until the block ends
    except Busy as e:
        print("not now:", e)            # the owner refused (an alert is live) or did not yield in time

The borrower writes <device>.borrow ({owner, until, pid}); the owner sees it
(owner_should_yield), stops its receiver and releases the lock; the borrower
takes the lock. When the borrow ends (block exits, borrower dies or `until`
passes) the request disappears or goes stale and the owner takes the dongle back.
The owner can refuse with refuse(device, reason) — the borrower gets Busy at once.

Stdlib only. Everything runs as the prepper user; the directory is created 0775.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from pathlib import Path

SDR_DIR = Path(os.environ.get("PREPPER_SDR_DIR", "/run/prepper/sdr"))
MAX_BORROW = 3600


class Busy(Exception):
    """The dongle could not be borrowed now."""


def _dir() -> Path:
    SDR_DIR.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(SDR_DIR, 0o775)
    return SDR_DIR


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read(p: Path) -> dict | None:
    try:
        d = json.loads(p.read_text())
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def _write(p: Path, obj: dict) -> None:
    tmp = p.with_name(f".{p.name}.{os.getpid()}")
    tmp.write_text(json.dumps(obj))
    os.replace(tmp, p)


def borrow_request(device: str) -> dict | None:
    """The live borrow request for a dongle, or None (missing, expired, or its process is gone)."""
    d = _read(_dir() / f"{device}.borrow")
    if not d:
        return None
    try:
        if float(d.get("until", 0)) < time.time() or not _pid_alive(int(d.get("pid", 0))):
            return None
    except (TypeError, ValueError):
        return None
    return d


def owner_should_yield(device: str) -> bool:
    return borrow_request(device) is not None


def refuse(device: str, reason: str) -> None:
    """Owner side: turn the current request down (the borrower raises Busy(reason))."""
    req = borrow_request(device)
    if req:
        _write(_dir() / f"{device}.refused", {"pid": req.get("pid"), "reason": str(reason)[:200], "at": time.time()})


@contextlib.contextmanager
def hold(device: str, poll: float = 1.0):
    """Owner side: wait until no borrow is live, then hold the dongle's lock for the block.
    The owner must watch owner_should_yield() inside the block and leave it promptly."""
    lock = os.open(_dir() / f"{device}.lock", os.O_CREAT | os.O_RDWR, 0o664)
    try:
        while True:
            if not owner_should_yield(device):
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    pass
            time.sleep(poll)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    finally:
        os.close(lock)


@contextlib.contextmanager
def borrow(device: str = "weather", owner: str = "?", seconds: int = 900, wait: float = 60, poll: float = 0.5):
    """Borrower side. Raises Busy if refused or not yielded within `wait` seconds."""
    d = _dir()
    req, ref = d / f"{device}.borrow", d / f"{device}.refused"
    if borrow_request(device):
        raise Busy("someone else is borrowing it")
    ref.unlink(missing_ok=True)
    me = os.getpid()
    _write(req, {"owner": str(owner)[:40], "pid": me, "until": time.time() + min(int(seconds), MAX_BORROW) + wait})
    lock = os.open(d / f"{device}.lock", os.O_CREAT | os.O_RDWR, 0o664)
    got = False
    try:
        deadline = time.monotonic() + wait
        while True:
            r = _read(ref)
            if r and r.get("pid") == me:
                raise Busy(r.get("reason") or "refused")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise Busy("the dongle's owner did not let go in time") from None
                time.sleep(poll)
        yield
    finally:
        if got:
            fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)
        cur = _read(req)
        if cur and cur.get("pid") == me:
            req.unlink(missing_ok=True)
        r = _read(ref)
        if r and r.get("pid") == me:
            ref.unlink(missing_ok=True)


def holder_free(device: str) -> bool:
    """True if nobody holds the lock right now (for status pages)."""
    lock = os.open(_dir() / f"{device}.lock", os.O_CREAT | os.O_RDWR, 0o664)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(lock, fcntl.LOCK_UN)
        return True
    except BlockingIOError:
        return False
    finally:
        os.close(lock)
