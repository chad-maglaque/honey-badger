"""
Voice mode — ask a question out loud on a phone, hear the answer.

  phone records (MediaRecorder on HTTPS/localhost, or the phone's own recorder through
  <input type=file capture> on plain HTTP) → POST /api/voice/ask (raw upload, ≤ max_upload_mb)
  → a job in a one-at-a-time queue: ffmpeg → whisper.cpp (transcript shown at once)
  → the same library-grounded AI as the launcher's Ask page (ctx.ask_stream), or, with no AI model,
    the best library matches → piper reads the answer → the page plays it, text shown too.

The page polls GET /api/voice/job?id= (job ids are random, tied to the asking IP, kept 15 min).
Access is the same as Ask (public) unless [voice] access = "member"; per-IP rate limit; an admin
can switch it off at runtime (POST /api/voice/settings) or with [features] voice = false.
Uploads are checked by magic bytes and deleted as soon as they are converted.
"""
from __future__ import annotations

import collections
import json
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "speech"))
import speechlib  # noqa: E402

FEATURE = {"title": "Voice", "icon": "🎙️", "group": "tools", "route": "/voice", "script": "/features/voice.js",
           "access": "public", "description": "Ask out loud, hear the answer"}

JOB_TTL = 15 * 60
MAX_JOBS = 60
NO_AI = "The AI model isn't answering"          # what ask_stream says when llama-server is down


class _Abort(Exception):
    """Raised from ask_stream's write() to stop a too-long answer."""


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.cv = threading.Condition(self.lock)
        self.jobs: dict[str, dict] = {}
        self.queue: collections.deque[str] = collections.deque()
        self.running: str | None = None
        self.hits: dict[str, collections.deque] = {}
        self.worker: threading.Thread | None = None


S = State()


def _num(v, d, lo, hi):
    return d if isinstance(v, bool) or not isinstance(v, (int, float)) else max(lo, min(hi, v))


def conf(ctx) -> dict:
    c = ctx.cfg() or {}
    s = ctx.read_json(ctx.data_dir() / "settings.json", {}) or {}
    s = s if isinstance(s, dict) else {}
    return {
        "enabled": s["enabled"] if isinstance(s.get("enabled"), bool) else c.get("enabled", True) is not False,
        "access": "member" if c.get("access") == "member" else "public",
        "max_seconds": int(_num(c.get("max_seconds"), 30, 5, 60)),
        "max_bytes": int(_num(c.get("max_upload_mb"), 4, 0.1, 16) * 1_000_000),
        "per_ip": int(_num(c.get("per_ip_per_10min"), 6, 1, 100)),
        "queue": int(_num(c.get("queue"), 2, 0, 10)),
        "answer": "full" if c.get("answer") == "full" else "brief",
        "allow_long": c.get("allow_long", True) is not False,
        "speak": c.get("speak", True) is not False,
        "answer_timeout": float(_num(c.get("answer_timeout"), 120, 10, 600)),
        "stt_threads": int(_num(c.get("whisper_threads"), 4, 1, 8)),
    }


def speech(ctx) -> speechlib.Settings:
    c = ctx.cfg() or {}
    return speechlib.Settings.from_config(c, whisper_threads=conf(ctx)["stt_threads"],
                                          stt_timeout=int(_num(c.get("stt_timeout"), 90, 10, 600)))


def allowed(req, ctx) -> bool:
    if conf(ctx)["access"] == "member":
        return bool(req.member()) or req.is_admin()
    return True


def _jobs_dir(ctx) -> Path:
    d = ctx.data_dir() / "jobs"
    d.mkdir(mode=0o700, exist_ok=True)
    return d


def _cleanup(ctx, now: float) -> None:
    """Forget jobs older than JOB_TTL and delete their files. Call with S.lock held."""
    for jid in [j for j, job in S.jobs.items() if now - job["created"] > JOB_TTL and j != S.running]:
        job = S.jobs.pop(jid)
        for p in (job.get("input"), job.get("audio")):
            if p:
                try:
                    os.unlink(p)
                except OSError:
                    pass
    for ip in [ip for ip, dq in S.hits.items() if not dq or now - dq[-1] > 600]:
        del S.hits[ip]


def _llama_url(ctx) -> str:
    return str((ctx.raw_config().get("portal", {}) or {}).get("llama", "http://127.0.0.1:8090/ai")).rstrip("/")


def ai_up(ctx) -> bool:
    try:
        with urllib.request.urlopen(_llama_url(ctx) + "/health", timeout=1.5) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


# ─────────────────────────────── HTTP ────────────────────────────────────────

def info(req, ctx, args):
    c = conf(ctx)
    av = speechlib.available(speech(ctx))
    with S.lock:
        waiting = len(S.queue) + (1 if S.running else 0)
    return {"enabled": c["enabled"], "stt": av["stt"], "tts": av["tts"] and c["speak"],
            "install_hint": av["hint"] if not av["stt"] else "", "max_seconds": c["max_seconds"],
            "max_bytes": c["max_bytes"], "allow_long": c["allow_long"], "busy": waiting,
            "access": c["access"], "admin": bool(args["admin"])}


def ask(req, ctx, args):
    c = conf(ctx)
    n = int(req.headers.get("Content-Length") or 0)          # the framework checked it is a number ≤ max_bytes
    if not c["enabled"]:
        req.close_connection = True
        return 403, {"error": "Voice questions are switched off on this Badger."}
    if n > c["max_bytes"]:
        req.close_connection = True
        return 413, {"error": f"That recording is too big (limit {c['max_bytes'] // 1_000_000} MB). "
                              f"Keep questions under {c['max_seconds']} seconds."}
    if n < 100:
        req.close_connection = True
        return 400, {"error": "That recording is empty. Hold the button a little longer."}
    av = speechlib.available(speech(ctx))
    if not av["stt"]:
        req.close_connection = True
        return 503, {"error": "Speech recognition isn't installed on this Badger yet.", "install_hint": av["hint"]}
    ip, now = args["ip"], time.time()
    with S.lock:
        _cleanup(ctx, now)
        dq = S.hits.setdefault(ip, collections.deque())
        while dq and now - dq[0] > 600:
            dq.popleft()
        if len(dq) >= c["per_ip"]:
            req.close_connection = True
            return 429, {"error": "That's a lot of questions in a row — wait a few minutes and try again."}, \
                {"Retry-After": "120"}
        if len(S.queue) >= c["queue"] + (0 if S.running else 1) or len(S.jobs) >= MAX_JOBS:
            req.close_connection = True
            return 503, {"error": "The Badger is busy answering someone else. Try again in a minute.", "busy": True}, \
                {"Retry-After": "30"}
        dq.append(now)
    data = req.rfile.read(n)
    if len(data) < n:
        req.close_connection = True
        return 400, {"error": "The upload was cut off. Try again."}
    kind = speechlib.sniff_audio(data[:64])
    if not kind:
        return 415, {"error": "That doesn't look like a sound recording. Use the record button, or pick an "
                              "audio file (m4a, mp3, ogg, webm, wav)."}
    jid = secrets.token_urlsafe(18)
    path = _jobs_dir(ctx) / f"{jid}.{kind}"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    long_q = (args["qs"].get("long") or [""])[0] == "1" and c["allow_long"]
    job = {"id": jid, "ip": ip, "created": now, "stage": "queued", "input": str(path), "audio": None,
           "transcript": "", "answer": "", "sources": [], "error": None, "note": None, "ai": None,
           "brief": not long_q if c["answer"] == "brief" else False}
    with S.cv:
        if len(S.queue) >= c["queue"] + (0 if S.running else 1):     # re-check: another upload got in first
            os.unlink(path)
            return 503, {"error": "The Badger is busy answering someone else. Try again in a minute.", "busy": True}, \
                {"Retry-After": "30"}
        S.jobs[jid] = job
        S.queue.append(jid)
        pos = len(S.queue) - 1 + (1 if S.running else 0)
        _ensure_worker(ctx)
        S.cv.notify_all()
    return {"id": jid, "position": pos}


def _job_for(args):
    jid = (args["qs"].get("id") or [""])[0]
    if not (16 <= len(jid) <= 40):
        return None
    with S.lock:
        job = S.jobs.get(jid)
        if not job or job["ip"] != args["ip"]:
            return None
        return dict(job)


def job_status(req, ctx, args):
    job = _job_for(args)
    if not job:
        return 404, {"error": "That question has expired. Ask again."}
    with S.lock:
        pos = S.queue.index(job["id"]) + (1 if S.running else 0) if job["id"] in S.queue else 0
    return {"id": job["id"], "stage": job["stage"], "position": pos, "transcript": job["transcript"],
            "answer": job["answer"], "sources": job["sources"][:6], "error": job["error"], "note": job["note"],
            "ai": job["ai"], "audio": bool(job["audio"])}


def job_audio(req, ctx, args):
    job = _job_for(args)
    if not job or not job["audio"] or not os.path.isfile(job["audio"]):
        return 404, {"error": "no spoken answer"}
    ctx.send_file(req, Path(job["audio"]), "audio/wav")
    return None


def save_settings(req, ctx, args):
    data = args["data"] or {}
    if not isinstance(data.get("enabled"), bool):
        return 400, {"error": "enabled must be true or false"}
    path = ctx.data_dir() / "settings.json"
    with S.lock:
        s = ctx.read_json(path, {}) or {}
        s = s if isinstance(s, dict) else {}
        s["enabled"] = data["enabled"]
        ctx.atomic_write(path, json.dumps(s))
    return {"ok": True, "enabled": data["enabled"]}


# ─────────────────────────────── worker ──────────────────────────────────────

def _set(jid, **kw):
    with S.lock:
        if jid in S.jobs:
            S.jobs[jid].update(kw)


def library_answer(ctx, q: str) -> tuple[str, list[dict]]:
    """No AI model: read out the best library matches."""
    hits = ctx.kiwix_search(q, count=4)[:3]
    if not hits:
        return ("The AI assistant isn't available, and I couldn't find anything in the library about that. "
                "Try asking with different words."), []
    words = ["First", "Second", "Third"]
    parts = ["The AI assistant isn't available, so here is what the library has."]
    for i, h in enumerate(hits):
        snip = (h.get("snippet") or "").strip()
        parts.append(f"{words[i]}: {h.get('title', '')}." + (f" {snip[:260]}" if snip else ""))
    srcs = [{"n": i + 1, "title": h.get("title", ""), "url": h.get("url", ""), "source": h.get("source", "")}
            for i, h in enumerate(hits)]
    return " ".join(parts), srcs


def process(ctx, jid: str) -> None:
    with S.lock:
        if jid not in S.jobs:
            return
        job = dict(S.jobs[jid])
    c, sp = conf(ctx), speech(ctx)
    wav = Path(job["input"]).with_suffix(".16k.wav")
    try:
        _set(jid, stage="listening")
        try:
            speechlib.to_wav16k(job["input"], wav, sp, max_seconds=c["max_seconds"])
        finally:
            try:
                os.unlink(job["input"])                    # the recording itself is not kept
            except OSError:
                pass
            _set(jid, input=None)
        inf = speechlib.wav_info(wav)
        if inf["seconds"] < 0.4 or inf["rms"] < 40:
            _set(jid, stage="error", error="I couldn't hear anything. Hold the phone closer and speak up.")
            return
        text = speechlib.transcribe(wav, sp, threads=c["stt_threads"])
        if len(text.strip()) < 2:
            _set(jid, stage="error", error="I couldn't make out any words. Try again in a quieter spot.")
            return
        text = text[:500]
        _set(jid, stage="thinking", transcript=text)
        answer, sources, used_ai = "", [], False
        if ai_up(ctx):
            buf, deadline = [], time.monotonic() + c["answer_timeout"]

            def write(event, data):
                nonlocal sources
                if event == "sources" and isinstance(data, list):
                    sources = data
                    _set(jid, sources=data)
                elif event == "token" and isinstance(data, str):
                    buf.append(data)
                    _set(jid, answer="".join(buf)[:6000])
                if time.monotonic() > deadline:
                    raise _Abort
            try:
                ctx.ask_stream(text, write, job["brief"])
            except _Abort:
                buf.append(" …")
            answer = "".join(buf).strip()[:6000]
            used_ai = bool(answer) and NO_AI not in answer
        if not used_ai:
            answer, sources = library_answer(ctx, text)
        _set(jid, answer=answer, sources=sources, ai=used_ai)
        if c["speak"] and speechlib.available(sp)["tts"]:
            _set(jid, stage="speaking")
            out = Path(job["input"]).with_suffix(".answer.wav")
            try:
                speechlib.synthesize(answer, out, sp)
                _set(jid, audio=str(out))
            except speechlib.SpeechError as e:
                _set(jid, note=f"No spoken answer this time ({e}).")
        else:
            _set(jid, note="Spoken answers aren't installed on this Badger; here is the text.")
        _set(jid, stage="done")
    except speechlib.SpeechError as e:
        _set(jid, stage="error", error=f"Sorry — {e}. Try again.")
    except Exception as e:  # noqa: BLE001 — one bad job must not stop the queue
        ctx.log(f"job failed: {type(e).__name__}: {e}")
        _set(jid, stage="error", error="Something went wrong on the Badger. Try again.")
    finally:
        try:
            wav.unlink()
        except OSError:
            pass


def _loop(ctx):
    while True:
        with S.cv:
            while not S.queue:
                S.cv.wait(60)
                _cleanup(ctx, time.time())
            jid = S.queue.popleft()
            S.running = jid
        try:
            process(ctx, jid)
        finally:
            with S.lock:
                S.running = None


def _ensure_worker(ctx):
    """Start the single worker (call with S.lock held)."""
    if S.worker is None or not S.worker.is_alive():
        S.worker = threading.Thread(target=_loop, args=(ctx,), daemon=True, name="voice-worker")
        S.worker.start()


def setup(ctx):
    c = conf(ctx)
    for r in ROUTES:
        if r["path"] == "/api/voice/ask":
            r["max_bytes"] = c["max_bytes"]
    try:                                               # leftovers from before a restart
        for p in _jobs_dir(ctx).iterdir():
            p.unlink()
    except OSError:
        pass


ROUTES = [
    {"method": "GET", "path": "/api/voice/info", "handler": info, "access": allowed},
    {"method": "POST", "path": "/api/voice/ask", "handler": ask, "access": allowed, "raw": True,
     "max_bytes": 4_000_000},
    {"method": "GET", "path": "/api/voice/job", "handler": job_status, "access": allowed},
    {"method": "GET", "path": "/api/voice/audio", "handler": job_audio, "access": allowed},
    {"method": "POST", "path": "/api/voice/settings", "handler": save_settings, "access": "admin"},
]
