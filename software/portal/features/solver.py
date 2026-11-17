"""
Problem solver — "deep solve" for real problems: science, engineering, maths, game theory and
decisions, basic code, survival. Not a writing assistant.

  POST /api/solver/solve      {"problem"} → {"id", "position"} (a job in a one-at-a-time queue), or
                              {"redirect": "…"} for writing chores (no AI used)
  GET  /api/solver/job?id=&since=N   live progress (plan, tool calls, results), then the answer with
                              working, assumptions, checks, sources; elapsed time and tokens/s
  POST /api/solver/cancel     {"id"}
  GET  /api/solver/info       what's available (AI model, Python runner, queue)
  GET  /api/solver/calculators, POST /api/solver/calc {"tool", "args"}   the calculators without AI
  GET/POST /api/solver/models (admin)   AI models per role: installed versions, pick, roll back,
                              self-test benchmark (score + tokens/s), RAM check
  POST /api/solver/settings   (admin) {"enabled"}

The model calls only side-effect-free tools (software/solver/tools.py). The Python tool runs only in
the verified OS sandbox. A family member's job may read the household supplies summary (read-only);
a visitor's job never can, and a member's job is visible to that member only.
Access is public like Ask, unless [solver] access = "member"; per-IP limits; one deep job at a time.
"""
from __future__ import annotations

import collections
import importlib.util
import json
import sys
import threading
import time
from pathlib import Path

_SOFTWARE = Path(__file__).resolve().parents[2]
if str(_SOFTWARE) not in sys.path:
    sys.path.insert(0, str(_SOFTWARE))
from solver import bench, jobs, loop, models, sandbox, tools  # noqa: E402
from solver.llm import LLM  # noqa: E402

FEATURE = {"title": "Problem solver", "icon": "🧮", "group": "tools", "route": "/solver",
           "script": "/features/solver.js", "access": "public",
           "description": "Works out engineering, science, maths, game-theory, code and survival problems step by step"}

MAX_PROBLEM = 2000


def _num(v, d, lo, hi):
    return d if isinstance(v, bool) or not isinstance(v, (int, float)) else max(lo, min(hi, v))


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.queue: jobs.JobQueue | None = None
        self.models: models.ModelManager | None = None
        self.sandbox: sandbox.SandboxClient | None = None
        self.llm: LLM | None = None
        self.hits: dict[str, collections.deque] = {}
        self.calc_hits: dict[str, collections.deque] = {}
        self.ctx = None
        self.idle: threading.Thread | None = None


S = State()


def conf(ctx) -> dict:
    c = ctx.cfg() or {}
    s = ctx.read_json(ctx.data_dir() / "settings.json", {}) or {}
    s = s if isinstance(s, dict) else {}
    return {
        "enabled": s["enabled"] if isinstance(s.get("enabled"), bool) else c.get("enabled", True) is not False,
        "access": "member" if c.get("access") == "member" else "public",
        "per_ip_per_hour": int(_num(c.get("per_ip_per_hour"), 6, 1, 100)),
        "max_queue": int(_num(c.get("max_queue"), 6, 1, 30)),
        "max_steps": int(_num(c.get("max_steps"), 10, 2, 30)),
        "context_tokens": int(_num(c.get("context_tokens"), 0, 0, 131072)),
        "idle_minutes": float(_num(c.get("idle_minutes"), 10, 1, 1440)),
        "job_ttl_hours": float(_num(c.get("job_ttl_hours"), 12, 1, 168)),
        "calc_per_10min": int(_num(c.get("calc_per_10min"), 60, 5, 1000)),
        "token_timeout": float(_num(c.get("token_timeout"), 300, 30, 1800)),
        "job_timeout": float(_num(c.get("job_timeout_minutes"), 45, 5, 240)) * 60,
        "reserve_mb": int(_num(c.get("reserve_mb"), models.RESERVE_MB, 300, 4000)),
        "sandbox_socket": str(c.get("sandbox_socket", sandbox.DEFAULT_SOCKET)),
        "python": c.get("python", True) is not False,
    }


def allowed(req, ctx) -> bool:
    if conf(ctx)["access"] == "member":
        return bool(req.member()) or req.is_admin()
    return True


def _raw(ctx, section) -> dict:
    v = (ctx.raw_config() or {}).get(section, {})
    return v if isinstance(v, dict) else {}


def _owner(args) -> dict:
    m = args.get("member")
    return {"ip": args.get("ip", ""), "member": (m or {}).get("id") if m else None, "admin": bool(args.get("admin"))}


def _limit(table: dict, ip: str, n: int, window: float) -> bool:
    now = time.time()
    with S.lock:
        dq = table.setdefault(ip, collections.deque())
        while dq and now - dq[0] > window:
            dq.popleft()
        if len(dq) >= n:
            return False
        dq.append(now)
        for k in [k for k, d in table.items() if not d]:
            del table[k]
        return True


# ─────────────────────────────── supplies (family members only, read-only) ───────────────────────────────

def _supplies_reader(ctx):
    """A function returning the household supplies summary, read from the supplies feature's data file
    without changing it. Only ever handed to a family member's job."""
    path = ctx.data_dir().parent / "supplies" / "supplies.json"
    mod_path = None
    for cand in (Path(__file__).with_name("supplies.py"), _SOFTWARE / "portal" / "features" / "supplies.py"):
        if cand.exists():
            mod_path = cand
            break
    if mod_path is None:
        return None

    def read() -> dict:
        if not path.exists():
            return {"note": "The family hasn't entered any supplies yet (Supplies page)."}
        spec = importlib.util.spec_from_file_location("solver_supplies_ro", mod_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        st = mod.Store(path, ctx.cfg("supplies") or {})
        d = st.load()
        s = st.summary(d)
        h = d["household"]
        items = [{"name": i.get("name", "")[:40], "category": i.get("category"), "qty": i.get("qty"),
                  "unit": i.get("unit", "")[:12], "flags": i.get("flags", [])} for i in s["items"][:40]]
        return {"household": {k: h.get(k) for k in ("people", "pets", "kcal", "water_l")},
                "water": s["water"], "food": s["food"], "fuel": s["fuel"], "batteries": s["batteries"],
                "counts": s["counts"], "items": items}
    return read


# ─────────────────────────────── the worker ───────────────────────────────

def _settings_for(entry: dict | None, c: dict) -> loop.Settings:
    st = loop.Settings(max_steps=c["max_steps"])
    if entry:
        th = entry.get("thinking", "none")
        st.thinking = th if th in ("soft", "always", "none") else "none"
    n = c["context_tokens"] or (S.models.slot_ctx() if S.models else None) or 4096
    st.context_tokens = max(2048, int(n))
    return st


def _tool_ctx(ctx, member: bool) -> tools.ToolContext:
    c = conf(ctx)
    tc = tools.ToolContext(member=member)
    tc.kiwix_search = ctx.kiwix_search
    tc.read_article = tools.article_reader(str(_raw(ctx, "portal").get("kiwix", "http://127.0.0.1:8081/kiwix")))
    if c["python"] and S.sandbox is not None and S.sandbox.available():
        tc.sandbox = S.sandbox
    if member:
        tc.supplies = _supplies_reader(ctx)
    return tc


def _solve_once(ctx, problem: str, member: bool, entry, progress, cancel, stats) -> dict:
    c = conf(ctx)
    t0 = time.monotonic()
    deadline_cancel, done = threading.Event(), threading.Event()

    def watchdog():
        while not done.wait(2):
            if cancel.is_set():
                return
            if time.monotonic() - t0 > c["job_timeout"]:
                deadline_cancel.set()
                cancel.set()
                return
    threading.Thread(target=watchdog, daemon=True, name="solver-watchdog").start()
    gen = {"tokens": 0}

    def on_tokens(n):
        gen["tokens"] = n
        stats(tokens=n)
    sv = loop.Solver(S.llm, _tool_ctx(ctx, member), _settings_for(entry, c), progress=progress, cancel=cancel,
                     on_tokens=on_tokens)
    try:
        out = sv.solve(problem)
    except loop.Cancelled:
        if deadline_cancel.is_set():
            raise RuntimeError("This took longer than the time limit and was stopped.") from None
        raise
    finally:
        done.set()
    res = out.as_dict()
    res["gen_seconds"] = round(out.gen_seconds, 4)
    stats(tokens=out.tokens, gen_seconds=out.gen_seconds)
    return res


def runner(job, progress, cancel, stats):
    ctx = S.ctx
    p = job.get("payload") or {}
    if job["kind"] == "bench":
        return _bench(ctx, p.get("model"), progress, cancel, stats)
    problem = str(p.get("problem", ""))[:MAX_PROBLEM]
    member = bool(p.get("member"))
    cls = loop.classify(problem)
    if cls["writing"]:
        out = loop.Outcome(redirect=True, answer=loop.REDIRECT)
        return out.as_dict()
    if not S.llm.health():
        raise RuntimeError("The AI model isn't running right now. The calculators below still work without it.")
    role = "reasoning"
    if cls["code"] and S.models.active("coder") and S.models.installed(S.models.active("coder")):
        role = "coder"
    progress("model", "Getting the AI model ready…")
    info = S.models.ensure(role, progress)
    try:
        entry = info.get("entry")
        stats(model=(entry or {}).get("title") or info.get("model") or "the loaded model")
        if info.get("note"):
            progress("note", info["note"])
        res = _solve_once(ctx, problem, member, entry, progress, cancel, stats)
        if info.get("note"):
            res.setdefault("warnings", []).insert(0, info["note"])
        res["model"] = (entry or {}).get("title") or info.get("model")
        return res
    finally:
        S.models.release()


def _bench(ctx, mid, progress, cancel, stats):
    m = S.models
    e = m.entry(mid or "")
    if not e:
        raise RuntimeError("unknown model")
    with m.lock:
        m._busy += 1
        try:
            progress("model", f"Loading {e['title']} for the self-test…")
            ok, why = m.load(mid, progress)
            if not ok:
                raise RuntimeError(f"Couldn't load {e['title']}: {why}")
            stats(model=e["title"])
            tot = {"tokens": 0, "secs": 0.0}

            def solve(q):
                r = _solve_once(ctx, q, False, e, lambda *a, **k: None, cancel,
                                lambda **kw: stats(tokens=tot["tokens"] + kw.get("tokens", 0)))
                tot["tokens"] += r.get("tokens", 0)
                tot["secs"] += r.get("gen_seconds", 0.0)
                return r
            res = bench.run(solve, progress, cancel)
            if not cancel.is_set():
                m.record_bench(mid, res)
            stats(tokens=tot["tokens"], gen_seconds=tot["secs"])
            return {"bench": res, "model": e["title"], "answer": f"Self-test: {res['score']}/{res['total']} right, "
                    f"{res['tps']} tokens/s", "sources": [], "warnings": []}
        finally:
            m._busy -= 1
            m._last_use = m.clock()


# ─────────────────────────────── HTTP ───────────────────────────────

def info(req, ctx, args):
    c = conf(ctx)
    up = S.llm.health() if S.llm else False
    loaded = S.models.loaded() if (up and S.models) else None
    e = S.models.entry(loaded) if loaded else None
    sb_ok, sb_why = S.sandbox.status() if (S.sandbox and c["python"]) else (False, "switched off")
    out = {"enabled": c["enabled"], "ai": up, "model": (e or {}).get("title") or loaded,
           "python": sb_ok, "member": bool(args["member"]), "admin": bool(args["admin"]),
           "queue": S.queue.stats() if S.queue else {}, "max_problem": MAX_PROBLEM,
           "recent": S.queue.recent(_owner(args)) if S.queue else [],
           "supplies": bool(args["member"])}
    if args["admin"]:
        out["python_note"] = sb_why
    return out


def submit(req, ctx, args):
    c = conf(ctx)
    if not c["enabled"]:
        return 403, {"error": "The problem solver is switched off on this Badger."}
    d = args["data"] or {}
    problem = d.get("problem")
    if not isinstance(problem, str) or len(problem.strip()) < 3:
        return 400, {"error": "Describe the problem (with numbers and units if there are any)."}
    if len(problem) > MAX_PROBLEM:
        return 400, {"error": f"That's too long: keep it under {MAX_PROBLEM} characters."}
    problem = problem.strip()
    if loop.classify(problem)["writing"]:
        return {"redirect": loop.REDIRECT}
    owner = _owner(args)
    if S.queue.active_for(owner) >= 1:
        return 409, {"error": "You already have a problem being solved. Wait for it, or cancel it first."}
    if not _limit(S.hits, args["ip"], c["per_ip_per_hour"], 3600):
        return 429, {"error": "That's a lot of problems in a row: wait a while and try again."}, {"Retry-After": "600"}
    try:
        job, pos = S.queue.submit("solve", {"problem": problem, "member": bool(args["member"])}, owner)
    except jobs.QueueFull:
        return 503, {"error": "The solver is busy with other people's problems. Try again in a few minutes.",
                     "busy": True}, {"Retry-After": "120"}
    return {"id": job["id"], "position": pos}


def _since(args) -> int:
    v = (args["qs"].get("since") or ["0"])[0]
    return int(v) if v.isdigit() and len(v) < 6 else 0


def job_status(req, ctx, args):
    jid = (args["qs"].get("id") or [""])[0]
    job = S.queue.get(jid, _owner(args)) if S.queue else None
    if not job:
        return 404, {"error": "That problem has expired or isn't yours. Submit it again."}
    now = time.time()
    end = job.get("finished") or now
    start = job.get("started")
    since = _since(args)
    res = job.get("result") or {}
    gs = float(job.get("gen_seconds") or res.get("gen_seconds") or 0)
    toks = int(job.get("tokens") or res.get("tokens") or 0)
    return {"id": job["id"], "kind": job["kind"], "state": job["state"], "position": job["position"],
            "problem": str((job.get("payload") or {}).get("problem", "")) if job["kind"] == "solve" else "",
            "events": job["events"][since:] if since <= len(job["events"]) else [], "event_count": len(job["events"]),
            "elapsed": round(end - start, 1) if start else 0, "waited": round((start or now) - job["created"], 1),
            "tokens": toks, "tps": round(toks / gs, 2) if gs > 0 else 0.0, "model": job.get("model"),
            "result": res if job["state"] == "done" else None, "error": job.get("error")}


def cancel(req, ctx, args):
    jid = (args["data"] or {}).get("id")
    if not isinstance(jid, str):
        return 400, {"error": "which job?"}
    ok = S.queue.cancel(jid, _owner(args))
    return ({"ok": True} if ok else (404, {"ok": False, "error": "Nothing to cancel."}))


def calculators(req, ctx, args):
    return {"calculators": tools.catalogue()}


def calc(req, ctx, args):
    c = conf(ctx)
    if not _limit(S.calc_hits, args["ip"], c["calc_per_10min"], 600):
        return 429, {"error": "Too many calculations in a row: wait a minute."}, {"Retry-After": "60"}
    d = args["data"] or {}
    name, a = d.get("tool"), d.get("args", {})
    try:
        return {"ok": True, "result": tools.run_direct(name, a)}
    except (tools.ToolError, ValueError, ArithmeticError, TypeError, RecursionError) as e:
        return 400, {"ok": False, "error": str(e)[:300] or "That didn't work."}


def models_get(req, ctx, args):
    ov = S.models.overview()
    ov["benchmark"] = [{"id": p["id"], "topic": p["topic"]} for p in bench.PROBLEMS]
    return ov


def models_post(req, ctx, args):
    d = args["data"] or {}
    op, role, mid = d.get("op"), d.get("role"), d.get("id")
    if op not in ("select", "rollback", "bench", "load"):
        return 400, {"error": "op must be select, rollback, bench or load"}
    if role is not None and role not in models.ROLES:
        return 400, {"error": "unknown role"}
    if mid is not None and (not isinstance(mid, str) or not models.ID_RE.match(mid)):
        return 400, {"error": "bad model id"}
    if op == "select":
        ok, msg = S.models.select(role, mid, force=d.get("force") is True)
        return ({"ok": True, "message": msg} if ok else (409, {"ok": False, "error": msg}))
    if op == "rollback":
        ok, msg = S.models.rollback(role)
        return ({"ok": True, "message": msg} if ok else (409, {"ok": False, "error": msg}))
    if not mid or not S.models.entry(mid):
        return 400, {"error": "which model?"}
    if not S.models.installed(mid):
        return 409, {"error": "That model isn't downloaded yet."}
    ok, why = S.models.fits(mid)
    if not ok:
        return 409, {"error": why}
    if op == "load":
        if not S.models.lock.acquire(timeout=1):
            return 409, {"error": "The AI is busy with a job; try again when it's done."}
        try:
            ok, msg = S.models.load(mid)
        finally:
            S.models.lock.release()
        return ({"ok": True, "message": f"{mid} loaded"} if ok else (409, {"ok": False, "error": msg}))
    try:
        job, pos = S.queue.submit("bench", {"model": mid}, _owner(args))
    except jobs.QueueFull:
        return 503, {"error": "The queue is full; try again later."}
    return {"ok": True, "id": job["id"], "position": pos}


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


# ─────────────────────────────── setup ───────────────────────────────

def setup(ctx):
    c = conf(ctx)
    S.ctx = ctx
    portal, upd_raw, ai = _raw(ctx, "portal"), _raw(ctx, "upd"), _raw(ctx, "ai")
    S.llm = LLM(str(portal.get("llama", "http://127.0.0.1:8090/ai")), token_timeout=c["token_timeout"],
                total_timeout=c["job_timeout"])
    defaults = {}
    for role, key in (("fast", "model"), ("endurance", "endurance_model")):
        if isinstance(ai.get(key), str):
            defaults[role] = ai[key]
    roles_cfg = (ctx.cfg() or {}).get("models", {})
    if isinstance(roles_cfg, dict):
        defaults.update({k: v for k, v in roles_cfg.items() if k in models.ROLES and isinstance(v, str)})
    S.models = models.ModelManager(
        catalog=Path(upd_raw.get("catalog", "/opt/prepper/content/catalog.toml")),
        data_root=Path(upd_raw.get("data_root", "/srv/prepper")),
        state_file=Path(upd_raw.get("state_file", "/var/lib/prepper/upd-state.json")),
        store=ctx.data_dir() / "models.json", llm=S.llm, ctl=ctx.ctl, defaults=defaults, reserve_mb=c["reserve_mb"],
        atomic_write=ctx.atomic_write, log=ctx.log)
    S.sandbox = sandbox.SandboxClient(c["sandbox_socket"], verify=True)
    S.queue = jobs.JobQueue(ctx.data_dir() / "jobs", runner, max_queue=c["max_queue"],
                            ttl=c["job_ttl_hours"] * 3600, atomic_write=ctx.atomic_write)
    if S.idle is None or not S.idle.is_alive():
        def idle():
            while True:
                time.sleep(60)
                try:
                    S.models.idle_check(conf(ctx)["idle_minutes"])
                except Exception as e:  # noqa: BLE001 — never kill the thread
                    ctx.log(f"idle check: {type(e).__name__}: {e}")
        S.idle = threading.Thread(target=idle, daemon=True, name="solver-idle")
        S.idle.start()


ROUTES = [
    {"method": "GET", "path": "/api/solver/info", "handler": info, "access": allowed},
    {"method": "POST", "path": "/api/solver/solve", "handler": submit, "access": allowed},
    {"method": "GET", "path": "/api/solver/job", "handler": job_status, "access": allowed},
    {"method": "POST", "path": "/api/solver/cancel", "handler": cancel, "access": allowed},
    {"method": "GET", "path": "/api/solver/calculators", "handler": calculators, "access": allowed},
    {"method": "POST", "path": "/api/solver/calc", "handler": calc, "access": allowed},
    {"method": "GET", "path": "/api/solver/models", "handler": models_get, "access": "admin"},
    {"method": "POST", "path": "/api/solver/models", "handler": models_post, "access": "admin"},
    {"method": "POST", "path": "/api/solver/settings", "handler": save_settings, "access": "admin"},
]
