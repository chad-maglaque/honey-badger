"""
Deep-solve job queue: one job runs at a time (the Pi has one AI model and four cores), the rest
wait in line. Jobs have random ids, belong to the device that asked (its IP, and its family member
if it was a member), survive page reloads (and portal restarts: finished jobs are kept on disk for
`ttl`; a job interrupted by a restart is marked so), report live progress, and can be cancelled.
"""
from __future__ import annotations

import collections
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

MAX_EVENTS = 150
ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,40}$")
FINAL = ("done", "error", "cancelled", "interrupted")


class QueueFull(Exception):
    pass


class JobQueue:
    def __init__(self, store: Path | None, runner, max_queue: int = 6, ttl: float = 12 * 3600, max_jobs: int = 200,
                 clock=time.time, atomic_write=None):
        self.store = Path(store) if store else None
        self.runner = runner              # runner(job_dict, progress(kind, text, **kw), cancel_event, stats(**kw)) -> dict
        self.max_queue = max_queue
        self.ttl = ttl
        self.max_jobs = max_jobs
        self.clock = clock
        self.atomic_write = atomic_write
        self.lock = threading.Lock()
        self.cv = threading.Condition(self.lock)
        self.jobs: dict[str, dict] = {}
        self.queue: collections.deque[str] = collections.deque()
        self.running: str | None = None
        self.cancels: dict[str, threading.Event] = {}
        self.worker: threading.Thread | None = None
        self._last_save: dict[str, float] = {}
        if self.store:
            self.store.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self.store, 0o700)
            except OSError:
                pass
            self._load()

    # — persistence —
    def _path(self, jid: str) -> Path:
        return self.store / f"{jid}.json"

    def _save(self, job: dict, force: bool = False) -> None:
        if not self.store:
            return
        now = time.monotonic()
        if not force and now - self._last_save.get(job["id"], 0) < 5:
            return
        self._last_save[job["id"]] = now
        text = json.dumps(job, separators=(",", ":"), default=str)
        p = self._path(job["id"])
        try:
            if self.atomic_write:
                self.atomic_write(p, text)
            else:
                tmp = p.with_suffix(".tmp")
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as fh:
                    fh.write(text)
                os.replace(tmp, p)
            os.chmod(p, 0o600)
        except OSError:
            pass

    def _load(self) -> None:
        now = self.clock()
        for p in sorted(self.store.glob("*.json"))[: self.max_jobs * 2]:
            try:
                job = json.loads(p.read_text())
            except (OSError, ValueError):
                job = None
            if not isinstance(job, dict) or not ID_RE.match(str(job.get("id", ""))) or p.stem != job["id"]:
                p.unlink(missing_ok=True)
                continue
            if now - float(job.get("created", 0)) > self.ttl:
                p.unlink(missing_ok=True)
                continue
            if job.get("state") not in FINAL:
                job["state"] = "interrupted"
                job["error"] = "The Badger restarted while this was waiting or being worked on. Please submit it again."
                job["finished"] = now
            self.jobs[job["id"]] = job
            self._save(job, force=True)

    # — public —
    def submit(self, kind: str, payload: dict, owner: dict, front: bool = False) -> tuple[dict, int]:
        now = self.clock()
        jid = secrets.token_urlsafe(18)
        job = {"id": jid, "kind": kind, "payload": payload, "owner": {"ip": owner.get("ip", ""),
                                                                     "member": owner.get("member")},
               "state": "queued", "created": now, "started": None, "finished": None, "events": [],
               "result": None, "error": None, "tokens": 0, "gen_seconds": 0.0, "model": None}
        with self.cv:
            self._cleanup(now)
            if len(self.queue) >= self.max_queue or len(self.jobs) >= self.max_jobs:
                raise QueueFull
            self.jobs[jid] = job
            self.cancels[jid] = threading.Event()
            if front:
                self.queue.appendleft(jid)
            else:
                self.queue.append(jid)
            pos = self._position(jid)
            self._save(job, force=True)
            self._ensure_worker()
            self.cv.notify_all()
        return dict(job), pos

    def _position(self, jid: str) -> int:
        if jid in self.queue:
            return self.queue.index(jid) + (1 if self.running else 0)
        return 0

    def can_see(self, job: dict, owner: dict) -> bool:
        o = job.get("owner") or {}
        if o.get("member"):
            return owner.get("member") == o["member"]          # a member's job (may hold family data): that member only
        if owner.get("admin") and job.get("kind") == "bench":
            return True
        return bool(owner.get("ip")) and owner.get("ip") == o.get("ip")

    def get(self, jid: str, owner: dict) -> dict | None:
        if not isinstance(jid, str) or not ID_RE.match(jid):
            return None
        with self.lock:
            job = self.jobs.get(jid)
            if not job or not self.can_see(job, owner):
                return None
            out = json.loads(json.dumps(job, default=str))
            out["position"] = self._position(jid)
            return out

    def cancel(self, jid: str, owner: dict) -> bool:
        with self.cv:
            job = self.jobs.get(jid) if isinstance(jid, str) else None
            if not job or not self.can_see(job, owner) or job["state"] in FINAL:
                return False
            ev = self.cancels.get(jid)
            if ev:
                ev.set()
            if jid in self.queue:
                self.queue.remove(jid)
                job.update(state="cancelled", finished=self.clock())
                self._save(job, force=True)
            return True

    def active_for(self, owner: dict) -> int:
        with self.lock:
            return sum(1 for j in self.jobs.values() if j["state"] in ("queued", "running")
                       and ((owner.get("member") and j["owner"].get("member") == owner.get("member"))
                            or j["owner"].get("ip") == owner.get("ip")))

    def stats(self) -> dict:
        with self.lock:
            return {"queued": len(self.queue), "running": bool(self.running), "max_queue": self.max_queue}

    def recent(self, owner: dict, limit: int = 10) -> list[dict]:
        with self.lock:
            mine = [j for j in self.jobs.values() if self.can_see(j, owner) and j["kind"] == "solve"]
        mine.sort(key=lambda j: j["created"], reverse=True)
        return [{"id": j["id"], "state": j["state"], "created": j["created"],
                 "problem": str((j.get("payload") or {}).get("problem", ""))[:120]} for j in mine[:limit]]

    # — worker —
    def _cleanup(self, now: float) -> None:
        for jid in [j for j, job in self.jobs.items() if job["state"] in FINAL and now - job["created"] > self.ttl]:
            self.jobs.pop(jid, None)
            self.cancels.pop(jid, None)
            self._last_save.pop(jid, None)
            if self.store:
                self._path(jid).unlink(missing_ok=True)

    def _ensure_worker(self) -> None:
        if self.worker is None or not self.worker.is_alive():
            self.worker = threading.Thread(target=self._loop, daemon=True, name="solver-worker")
            self.worker.start()

    def _loop(self) -> None:
        while True:
            with self.cv:
                while not self.queue:
                    self.cv.wait(60)
                    self._cleanup(self.clock())
                jid = self.queue.popleft()
                job = self.jobs.get(jid)
                if job is None or job["state"] != "queued":
                    continue
                self.running = jid
                job.update(state="running", started=self.clock())
                cancel = self.cancels.setdefault(jid, threading.Event())
                snapshot = json.loads(json.dumps(job, default=str))
            try:
                res = self.runner(snapshot, lambda kind, text, _j=jid, **kw: self._event(_j, kind, text, **kw), cancel,
                                  lambda _j=jid, **kw: self._stats(_j, **kw))
                with self.lock:
                    if cancel.is_set():
                        job.update(state="cancelled")
                    else:
                        job.update(state="done", result=res)
            except Exception as e:  # noqa: BLE001 — one bad job must not stop the queue
                with self.lock:
                    if cancel.is_set():
                        job.update(state="cancelled")
                    else:
                        job.update(state="error", error=str(e)[:300] or type(e).__name__)
            finally:
                with self.lock:
                    job["finished"] = self.clock()
                    self.running = None
                    self._save(job, force=True)

    def _event(self, jid: str, kind: str, text: str, **kw) -> None:
        with self.lock:
            job = self.jobs.get(jid)
            if not job:
                return
            ev = {"t": round(self.clock() - (job["started"] or job["created"]), 1), "kind": str(kind)[:12],
                  "text": str(text)[:600]}
            for k, v in kw.items():
                if isinstance(v, (str, int, float, bool)) or v is None:
                    ev[k] = v if not isinstance(v, str) else v[:300]
            job["events"].append(ev)
            if len(job["events"]) > MAX_EVENTS:
                job["events"] = job["events"][:10] + job["events"][-(MAX_EVENTS - 10):]
            self._save(job)

    def _stats(self, jid: str, **kw) -> None:
        with self.lock:
            job = self.jobs.get(jid)
            if job:
                for k in ("tokens", "gen_seconds", "model", "stage"):
                    if k in kw:
                        job[k] = kw[k]
