"""
AI model roles and safe switching.

Catalog model entries (content/catalog.toml, kind = "model") declare
    role = "fast" | "reasoning" | "coder" | "endurance"
    min_ram_gb, ctx (context the model is run with), thinking = "soft" | "always" | "none",
    params_b, quant, verified (false until the file's sha256 and licence were checked by a person)
New models arrive like any catalog item (signed software updates, USB bundles, other Badgers);
upd downloads them to <data_root>/models/<id>.current.

Design: ONE llama-server, ONE model in memory at a time. An 8 GB Pi 5 can't hold the 8B reasoning
model next to the 4B fast model with room left for the OS, kiwix and the page cache, and the Pi's
memory bandwidth is the bottleneck anyway: two models generating at once would each run at half
speed. So the solver swaps the model for a deep job (under a lock, only when the new one fits in
free RAM), and swaps back to the fast model after `idle_minutes` without deep work, so Ask stays
quick. The swap itself is one privileged action, `prepperctl ai-model <catalog-id>` (see the
report: it writes /run/prepper/llama-model and restarts llama-server); without it the solver uses
whatever model is loaded and says so.

Promotion rule: a model becomes a role's active model only if its self-test benchmark score is at
least the current one's (an admin may override with force). Rollback = back to the previous model.
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import tomllib
from pathlib import Path

ROLES = ("fast", "reasoning", "coder", "endurance")
ROLE_TEXT = {"fast": "Fast (Ask the Library)", "reasoning": "Reasoning (deep solve)", "coder": "Coder",
             "endurance": "Endurance (battery)"}
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
RESERVE_MB = 1000                  # left for the OS, portal, kiwix-serve and page cache while a model runs


def kv_mb(entry: dict) -> float:
    """Working memory besides the weights: KV cache (f16) for the context it runs with, plus ~300 MB
    of compute buffers. Catalog entries may state kv_mb_per_1k (MB per 1000 tokens of context);
    otherwise ~144 MB/1k for 3B+ models (Qwen3 4B/8B: 36 layers × 8 KV heads × 128 × 2 × 2 bytes)
    and ~28 MB/1k below that."""
    per = entry.get("kv_mb_per_1k") or (144.0 if (entry.get("params_b") or 4) >= 3 else 28.0)
    return float(per) * (entry.get("ctx") or 4096) / 1000 + 300


def meminfo(path: str = "/proc/meminfo") -> dict:
    out = {}
    try:
        with open(path) as fh:
            for line in fh:
                k, _, rest = line.partition(":")
                parts = rest.split()
                if parts and parts[0].isdigit():
                    out[k.strip()] = int(parts[0]) // 1024        # kB → MB
    except OSError:
        return {}
    return {"total_mb": out.get("MemTotal"), "available_mb": out.get("MemAvailable", out.get("MemFree"))}


class ModelManager:
    def __init__(self, catalog: Path, data_root: Path, state_file: Path, store: Path, llm, ctl=None,
                 meminfo_path: str = "/proc/meminfo", variant_file: str = "/run/prepper/llama-variant",
                 defaults: dict | None = None, reserve_mb: int = RESERVE_MB, swap_timeout: float = 240.0,
                 clock=time.time, sleep=time.sleep, atomic_write=None, log=None):
        self.catalog_path = Path(catalog)
        self.data_root = Path(data_root)
        self.state_file = Path(state_file)
        self.store = Path(store)
        self.llm = llm
        self.ctl = ctl
        self.meminfo_path = meminfo_path
        self.variant_file = variant_file
        self.defaults = dict(defaults or {})
        self.reserve_mb = reserve_mb
        self.swap_timeout = swap_timeout
        self.clock = clock
        self.sleep = sleep
        self.atomic_write = atomic_write
        self.log = log or (lambda m: None)
        self.lock = threading.RLock()          # held while swapping and while a job uses a model
        self._busy = 0
        self._last_use = clock()
        self._switch_ok_until = 0.0            # after a failed switch: don't retry for a while
        self._switch_error = ""
        self._loaded_cache: tuple[float, str | None] | None = None

    # — catalog & disk —
    def catalog(self) -> list[dict]:
        try:
            raw = tomllib.loads(self.catalog_path.read_text())
        except (OSError, ValueError):
            return []
        out = []
        for it in raw.get("item", []):
            if not isinstance(it, dict) or it.get("kind") != "model" or not ID_RE.match(str(it.get("id", ""))):
                continue
            role = it.get("role", [])
            roles = [role] if isinstance(role, str) else [r for r in role if isinstance(r, str)] if isinstance(role, list) else []
            out.append({"id": it["id"], "title": str(it.get("title", it["id"])), "roles": [r for r in roles if r in ROLES],
                        "size_gb": float(it.get("size_gb", 0) or 0), "license": str(it.get("license", "")),
                        "use": str(it.get("use", "personal")), "min_ram_gb": float(it.get("min_ram_gb", 0) or 0),
                        "ctx": int(it.get("ctx", 4096) or 4096), "thinking": str(it.get("thinking", "none")),
                        "params_b": float(it.get("params_b", 0) or 0), "quant": str(it.get("quant", "")),
                        "kv_mb_per_1k": float(it.get("kv_mb_per_1k", 0) or 0),
                        "verified": it.get("verified", True) is True, "description": str(it.get("description", ""))})
        return out

    def entry(self, mid: str) -> dict | None:
        return next((m for m in self.catalog() if m["id"] == mid), None)

    def installed(self, mid: str) -> dict | None:
        if not ID_RE.match(mid or ""):
            return None
        p = self.data_root / "models" / f"{mid}.current"
        try:
            st = p.stat()
        except OSError:
            return None
        rec = {}
        try:
            rec = (json.loads(self.state_file.read_text()).get("items") or {}).get(mid) or {}
        except (OSError, ValueError, AttributeError):
            rec = {}
        prev = rec.get("previous") if isinstance(rec.get("previous"), dict) else None
        return {"size_mb": st.st_size // (1024 * 1024), "version": str(rec.get("version", ""))[:40],
                "installed": str(rec.get("installed", ""))[:40], "previous_version": (prev or {}).get("version")}

    # — selections & benchmarks (our own small JSON store) —
    def _read(self) -> dict:
        try:
            d = json.loads(self.store.read_text())
        except (OSError, ValueError):
            d = {}
        d = d if isinstance(d, dict) else {}
        d.setdefault("roles", {})
        d.setdefault("bench", {})
        return d

    def _write(self, d: dict) -> None:
        self.store.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(d, indent=1)
        if self.atomic_write:
            self.atomic_write(self.store, text)
        else:
            tmp = self.store.with_suffix(".tmp")
            tmp.write_text(text)
            os.replace(tmp, self.store)
        try:
            os.chmod(self.store, 0o600)
        except OSError:
            pass

    def active(self, role: str) -> str | None:
        r = self._read()["roles"].get(role) or {}
        mid = r.get("active") or self.defaults.get(role)
        if not mid:
            mid = next((m["id"] for m in self.catalog() if role in m["roles"]), None)
        return mid

    def bench(self, mid: str) -> dict | None:
        b = self._read()["bench"].get(mid)
        return b if isinstance(b, dict) else None

    def record_bench(self, mid: str, result: dict) -> None:
        with self.lock:
            d = self._read()
            d["bench"][mid] = {k: result.get(k) for k in ("score", "correct", "total", "tps", "seconds", "at", "details")}
            self._write(d)

    def select(self, role: str, mid: str, force: bool = False) -> tuple[bool, str]:
        if role not in ROLES:
            return False, "unknown role"
        e = self.entry(mid)
        if not e:
            return False, "that model isn't in the catalog"
        if role not in e["roles"]:
            return False, f"{e['title']} isn't meant for the {role} role"
        if not self.installed(mid):
            return False, f"{e['title']} isn't downloaded yet (Updates downloads it when it's in this Badger's tier or pinned)"
        mem = meminfo(self.meminfo_path)
        if e["min_ram_gb"] and mem.get("total_mb") and mem["total_mb"] < e["min_ram_gb"] * 1024 * 0.93:
            return False, f"{e['title']} needs {e['min_ram_gb']:g} GB of memory; this Badger has {mem['total_mb'] / 1024:.1f} GB"
        cur = self.active(role)
        if cur == mid:
            return True, "already active"
        if not force:
            nb, cb = self.bench(mid), self.bench(cur) if cur else None
            if nb is None:
                return False, f"Run the self-test on {e['title']} first (a model is only promoted if it scores at least as well)"
            if cb is not None and (nb.get("score") or 0) < (cb.get("score") or 0):
                return False, (f"{e['title']} scored {nb.get('score')} on the self-test, below the current model's "
                               f"{cb.get('score')}: not promoted (an admin can force it)")
        with self.lock:
            d = self._read()
            r = d["roles"].setdefault(role, {})
            r["previous"] = cur
            r["active"] = mid
            r["changed"] = self.clock()
            d["roles"][role] = r
            self._write(d)
        return True, f"{e['title']} is now the {role} model"

    def rollback(self, role: str) -> tuple[bool, str]:
        with self.lock:
            d = self._read()
            r = d["roles"].get(role) or {}
            prev = r.get("previous")
            if not prev:
                return False, "there is no previous model to go back to"
            if not self.installed(prev):
                return False, "the previous model isn't on the drive any more"
            r["previous"], r["active"], r["changed"] = r.get("active") or self.active(role), prev, self.clock()
            d["roles"][role] = r
            self._write(d)
        return True, f"back to {prev}"

    # — memory —
    def need_mb(self, mid: str) -> float:
        e = self.entry(mid) or {}
        inst = self.installed(mid) or {}
        size = inst.get("size_mb") or (e.get("size_gb", 0) * 1e9 / 2 ** 20)     # catalog GB are decimal; meminfo MB are MiB
        return size + kv_mb(e) + self.reserve_mb

    def fits(self, mid: str) -> tuple[bool, str]:
        """Free RAM after unloading the current model must hold the new one, its KV cache and the reserve."""
        mem = meminfo(self.meminfo_path)
        if not mem.get("available_mb"):
            return False, "can't read free memory (/proc/meminfo); not loading a model blind"
        e = self.entry(mid) or {}
        if e.get("min_ram_gb") and mem.get("total_mb") and mem["total_mb"] < e["min_ram_gb"] * 1024 * 0.93:
            return False, f"{e.get('title', mid)} needs a Badger with {e['min_ram_gb']:g} GB of memory"
        avail = mem["available_mb"]
        cur = self.loaded()
        if cur and cur != mid:
            ce = self.entry(cur) or {}
            avail += kv_mb(ce) if ce else 0      # its KV cache is freed; its file pages count as cache already
        need = self.need_mb(mid)
        if need > avail:
            return False, (f"Not enough free memory for {e.get('title', mid)}: it needs about {need / 1024:.1f} GB "
                           f"(model, working memory and {self.reserve_mb / 1024:.1f} GB for the rest of the Badger), "
                           f"{avail / 1024:.1f} GB is free. Close Kolibri/Jellyfin or use a smaller model.")
        return True, "fits"

    # — what's loaded —
    def loaded(self, fresh: bool = False) -> str | None:
        now = self.clock()
        if not fresh and self._loaded_cache and now - self._loaded_cache[0] < 10:
            return self._loaded_cache[1]
        props = self.llm.props() if self.llm else {}
        path = str(props.get("model_path", "") or "")
        mid = None
        if path:
            base = os.path.basename(path)
            if base.endswith(".current"):
                mid = base[: -len(".current")]
            else:
                ids = [m["id"] for m in self.catalog()]
                mid = next((i for i in ids if i in path), None) or base[:80]
        self._loaded_cache = (now, mid)
        return mid

    def endurance_mode(self) -> bool:
        try:
            return Path(self.variant_file).read_text().strip() == "endurance"
        except OSError:
            return False

    def slot_ctx(self) -> int | None:
        p = self.llm.props() if self.llm else {}
        try:
            n = int((p.get("default_generation_settings") or {}).get("n_ctx") or 0)
        except (TypeError, ValueError):
            n = 0
        return n or None

    # — switching —
    def switch_supported(self) -> bool:
        return self.ctl is not None and self.clock() >= self._switch_ok_until

    def load(self, mid: str, progress=None) -> tuple[bool, str]:
        """Make `mid` the loaded model. Call with self.lock held (ensure() does)."""
        progress = progress or (lambda *a, **k: None)
        if self.loaded(fresh=True) == mid:
            return True, "loaded"
        e = self.entry(mid)
        if not e or not self.installed(mid):
            return False, "that model isn't downloaded"
        ok, why = self.fits(mid)
        if not ok:
            return False, why
        if not self.switch_supported():
            return False, self._switch_error or "this Badger can't switch AI models yet"
        progress("model", f"Loading {e['title']} (this takes a minute)…")
        rc, out = self.ctl("ai-model", mid)
        if rc != 0 or "would run" in out:
            self._switch_ok_until = self.clock() + 600
            self._switch_error = "this Badger's system helper can't switch AI models yet (needs a software update)"
            self.log(f"ai-model {mid} failed: rc={rc} {out[-200:]}")
            return False, self._switch_error
        deadline = self.clock() + self.swap_timeout
        while self.clock() < deadline:
            self.sleep(2)
            if self.llm.health() and self.loaded(fresh=True) == mid:
                self._last_use = self.clock()
                return True, "loaded"
        self._switch_ok_until = self.clock() + 300
        self._switch_error = "the new model didn't start in time"
        return False, self._switch_error

    def ensure(self, role: str, progress=None) -> dict:
        """For a job: load the role's model if possible; otherwise use what's loaded. Returns
        {"model": id, "entry": {...}, "note": "..."}. Caller must call release() when done."""
        with self.lock:
            self._busy += 1
            self._last_use = self.clock()
            want = self.active(role)
            note = ""
            if self.endurance_mode() and role != "endurance":
                note = "Battery-saving mode is on, so the small model is answering (slower thinking, less reliable)."
            elif want and self.installed(want):
                ok, why = self.load(want, progress)
                if not ok:
                    note = f"Using the model already loaded: {why}"
            elif want:
                note = f"The {role} model isn't downloaded yet; using the model already loaded."
            cur = self.loaded(fresh=True)
            return {"model": cur, "entry": self.entry(cur) if cur else None, "note": note}

    def release(self) -> None:
        with self.lock:
            self._busy = max(0, self._busy - 1)
            self._last_use = self.clock()

    def idle_check(self, idle_minutes: float) -> str | None:
        """Called periodically: swap back to the fast model after idle deep work."""
        if not self.switch_supported():
            return None
        if not self.lock.acquire(blocking=False):
            return None
        try:
            if self._busy or self.clock() - self._last_use < idle_minutes * 60:
                return None
            fast = self.active("endurance" if self.endurance_mode() else "fast")
            cur = self.loaded(fresh=True)
            if not fast or not cur or cur == fast or not self.installed(fast):
                return None
            ok, _ = self.load(fast)
            return fast if ok else None
        finally:
            self.lock.release()

    # — the admin panel —
    def overview(self) -> dict:
        cat = self.catalog()
        mem = meminfo(self.meminfo_path)
        d = self._read()
        roles = []
        for role in ROLES:
            r = d["roles"].get(role) or {}
            cands = []
            for m in cat:
                if role not in m["roles"]:
                    continue
                inst = self.installed(m["id"])
                fit = self.fits(m["id"]) if inst else (None, "")
                cands.append({**m, "installed": inst, "bench": d["bench"].get(m["id"]),
                              "fits": fit[0], "fit_note": fit[1] if fit[0] is False else ""})
            roles.append({"role": role, "label": ROLE_TEXT[role], "active": self.active(role),
                          "previous": r.get("previous"), "candidates": cands})
        return {"roles": roles, "loaded": self.loaded(), "memory": mem, "endurance": self.endurance_mode(),
                "switching": self.switch_supported(), "switch_note": "" if self.switch_supported() else self._switch_error,
                "reserve_mb": self.reserve_mb}


def finite(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
