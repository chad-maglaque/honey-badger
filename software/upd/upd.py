#!/usr/bin/env python3
"""
upd — content updater for the Honey Badger.

Standard library only (Python 3.11+). Works online, over the LAN from another
disk ("peer"), or completely offline from a signed USB bundle ("sneakernet").

  upd check   [--json] [--offline]       work out what changed upstream (metadata only)
  upd apply   [--only ID ...] [--now] [--dry-run]
  upd status                             what is installed
  upd export  DEST [--only ID ...]       write a signed bundle for another disk
  upd import  SRC  [--only ID ...]       install from a bundle (USB stick, folder)
  upd adopt   ID [PATH]                  register files you copied in for a manual item
  upd gc                                 remove leftovers from interrupted runs
  upd manifest                           rebuild the peer-sync manifest

How differences are determined, per source type
  kiwix    Kiwix OPDS catalog (date-versioned ZIMs, sha256 from .meta4),
           falls back to the mirror's directory listing when the catalog is down
  http     ETag / Last-Modified / Content-Length fingerprint, or newest file
           matching a version regex in a directory listing
  hf       Hugging Face X-Linked-Etag (that header *is* the file's sha256)
  git      `git ls-remote` commit id; applied as a shallow fetch (true delta)
  rsync    rsync dry-run itemisation; applied with --link-dest (true delta)
  pmtiles  newest Protomaps daily build; applied with `pmtiles extract`
  manual   content you supply yourself; tracked, never fetched
  peer     another disk's /sync/manifest.json, checked before the internet
  zimdiff  a peer or bundle that ships OLD→NEW .zimdiff patches (see export --diffs)

Transfer methods, best first: zimpatch → peer copy → rsync delta (--fuzzy
against the installed file) → git delta → resumable HTTP with sha256 verify.
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import errno
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import stat
import sys
import tempfile
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable

VERSION = "1.0.0"
UA = f"ultimate-prepper-disk-upd/{VERSION}"
DEFAULT_CONFIG = "/etc/prepper/prepper.toml"
SIG_NAMESPACE = "prepper-update"
# Trust is scoped by what an item can do (red-team finding 8). A peer/bundle signer listed in allowed_signers
# as "prepper-update" may bring CONTENT (ZIMs, maps, documents, datasets: shown sandboxed or parsed as data);
# only one listed as "prepper-code" may also bring CODE — web apps, Android APKs, AI models — and even then a
# file must match the catalog's own pin where the catalog has one. Items pinned to a git commit never come
# from a peer at all (a copied tree can't prove its commit): they come from upstream, checked against the pin.
CODE_KINDS = frozenset({"app", "apk", "model"})
SCOPE_PRINCIPALS = (("code", "prepper-code"), ("content", "prepper-update"))

KIND_DIRS = {
    "zim": "zim", "pmtiles": "maps", "pdfset": "docs", "epubset": "books",
    "videoset": "video", "audioset": "audio", "model": "models", "app": "apps",
    "kolibri": "kolibri", "dataset": "data", "apk": "apk",
}


# ───────────────────────────── config & catalog ─────────────────────────────

@dataclasses.dataclass
class Config:
    data_root: Path
    state_file: Path
    catalog: Path
    report_dir: Path
    tier: str = "ultimate"
    budget_gb: float = 0.0                # >0: laptop/tablet edition — fill by priority
    pins: tuple[str, ...] = ()            # always included first in budget mode
    languages: tuple[str, ...] = ("eng",)
    reserve_gb: float = 20.0
    max_rate_kbps: int = 0
    window: str = ""                      # "01:00-06:00"; empty = any time
    keep_previous: bool = False
    auto_apply: bool = False
    peers: tuple[str, ...] = ()
    require_signed_peers: bool = True
    allowed_signers: Path = Path("/etc/prepper/allowed_signers")
    signing_key: Path = Path("/etc/prepper/keys/disk_ed25519")
    disk_id: str = "prepper-disk"
    kiwix_opds: str = "https://library.kiwix.org/catalog/v2/entries"
    kiwix_http: str = "https://download.kiwix.org/zim/"
    kiwix_rsync: str = "rsync://master.download.kiwix.org/download.kiwix.org/zim/"
    protomaps_builds: str = "https://build-metadata.protomaps.dev/builds.json"
    protomaps_http: str = "https://build.protomaps.com/"
    timeout: int = 30
    hooks: dict[str, str] = dataclasses.field(default_factory=dict)
    # [device] provisioning: "owner" (built by the people who use it: everything in the
    # tier, as always) or "sale" (built to be sold: "personal" items wait for the owner's
    # acceptance, recorded by the launcher in personal_file). See personal_allowed().
    provisioning: str = "owner"
    personal_file: Path = Path("/var/lib/prepper/portal/personal-content.json")

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        p = Path(path)
        raw: dict[str, Any] = {}
        device: dict[str, Any] = {}
        if p.exists():
            doc = tomllib.loads(p.read_text())
            raw = doc.get("upd", {})
            device = doc.get("device", {}) if isinstance(doc.get("device"), dict) else {}
        base = Path(raw.get("data_root", "/srv/prepper"))
        cfg = cls(
            data_root=base,
            state_file=Path(raw.get("state_file", "/var/lib/prepper/upd-state.json")),
            catalog=Path(raw.get("catalog", "/opt/prepper/content/catalog.toml")),
            report_dir=Path(raw.get("report_dir", "/var/lib/prepper/reports")),
        )
        for f in dataclasses.fields(cls):
            if f.name in raw and f.name not in ("data_root", "state_file", "catalog", "report_dir", "provisioning"):
                v = raw[f.name]
                try:
                    if f.name in ("peers", "languages", "pins"):
                        if isinstance(v, str):
                            raise TypeError("expected a list")
                        v = tuple(str(x) for x in v)
                    elif f.name in ("allowed_signers", "signing_key", "personal_file"):
                        v = Path(v)
                    elif f.name in ("budget_gb", "reserve_gb"):
                        v = float(v)
                        if math.isnan(v):              # NaN would silently disable every comparison
                            raise ValueError("not a number")
                    elif f.name in ("max_rate_kbps", "timeout"):
                        v = int(v)
                    elif f.name in ("keep_previous", "auto_apply", "require_signed_peers"):
                        if not isinstance(v, bool):
                            raise TypeError("expected true or false")
                    elif f.name == "hooks" and not isinstance(v, dict):
                        raise TypeError("expected a table")
                except (TypeError, ValueError) as e:
                    raise SystemExit(f"upd: bad [upd] {f.name} = {v!r} in {p}: {e}") from None
                setattr(cfg, f.name, v)
        # relative paths in the config are relative to the config file
        cfg.provisioning = provisioning_mode(device.get("provisioning", "owner"), p)
        for name in ("catalog", "state_file", "report_dir", "allowed_signers", "signing_key", "personal_file"):
            val = getattr(cfg, name)
            if not val.is_absolute():
                setattr(cfg, name, (p.parent / val).resolve())
        if not cfg.data_root.is_absolute():
            cfg.data_root = (p.parent / cfg.data_root).resolve()
        return cfg


@dataclasses.dataclass
class Item:
    id: str
    title: str
    category: str
    kind: str
    source: dict[str, Any]
    size_gb: float
    tiers: list[str]
    renderer: str
    lang: str = "eng"
    description: str = ""
    license: str = ""
    priority: int = 5          # 1 = keep even on the smallest device … 9 = nice to have
    use: str = "personal"      # "any" | "personal" (non-commercial only); unknown counts as personal

    @property
    def dir(self) -> str:
        return KIND_DIRS.get(self.kind, self.kind)


def load_catalog(path: Path) -> list[Item]:
    data = tomllib.loads(path.read_text())
    items = []
    seen = set()
    for raw in data.get("item", []):
        if raw["id"] in seen:
            raise SystemExit(f"catalog: duplicate id {raw['id']}")
        seen.add(raw["id"])
        items.append(Item(
            id=raw["id"], title=raw["title"], category=raw["category"],
            kind=raw["kind"], source=raw.get("source", {"type": "manual"}),
            size_gb=float(raw.get("size_gb", 0)), tiers=list(raw.get("tiers", [])),
            renderer=raw.get("renderer", raw["kind"]), lang=raw.get("lang", "eng"),
            description=raw.get("description", ""), license=raw.get("license", ""),
            priority=int(raw.get("priority", 5)),
            use=item_use(raw.get("use"), raw.get("license", "")),
        ))
    return items


def wanted(items: Iterable[Item], cfg: Config, record: dict | None = None) -> list[Item]:
    """Items this device should hold.

    Badger editions take everything tagged with their tier. Laptop / tablet
    editions set budget_gb: pins go first, then items by priority (smallest
    first within a priority) until the budget is spent. Anything left out is
    still reachable live from the Badger over Wi-Fi.

    Items whose licence is for personal use only are left out — before tiers,
    pins and the budget are looked at, so nothing can select them back in —
    unless personal_allowed() says this Badger may hold them.
    """
    if record is None:
        record = load_personal_record(cfg)
    items = [i for i in items if personal_allowed(i, cfg, record)]
    return _select(items, cfg)


def _select(items: list[Item], cfg: Config) -> list[Item]:
    langs = set(cfg.languages) | {"mul", "zxx"}
    pool = [i for i in items if cfg.tier in i.tiers and i.lang in langs]
    if cfg.budget_gb <= 0:
        return pool
    by_id = {i.id: i for i in items}
    chosen, used = [], 0.0
    for pid in cfg.pins:                       # pins may come from outside the tier (never past the licence rule)
        if pid in by_id and by_id[pid] not in chosen:
            chosen.append(by_id[pid]); used += by_id[pid].size_gb
    for i in sorted(pool, key=lambda i: (i.priority, i.size_gb)):
        if i in chosen:
            continue
        if used + i.size_gb <= cfg.budget_gb:
            chosen.append(i); used += i.size_gb
    return chosen


# ─────────────────── personal-use (non-commercial) content ───────────────────
#
# Some catalog items are licensed for non-commercial / personal use only
# (`use = "personal"`). A Badger built to be sold ([device] provisioning =
# "sale", `prepper-build --for-sale`) never fetches them by itself: the owner is
# offered them at first setup, and only what the owner accepts (recorded by the
# launcher in [upd] personal_file) is downloaded — by the owner's own Badger.
# Badgers built by the people who use them ("owner", the default) hold them as
# always, unless the owner removed them. This is enforced in wanted(), so every
# path that installs content (internet, peers, USB bundles, first pull, clone)
# obeys it, and adopt() checks it too.

USES = ("any", "personal")
PROVISIONING = ("owner", "sale")
# Any licence naming NC, "non-commercial" or "personal use" is personal-use
# content, whatever the catalog's `use` says.
NONCOMMERCIAL = re.compile(r"(?<![A-Za-z])NC(?![A-Za-z])|non-?commercial|personal[ -]use", re.I)
PERSONAL_RECORD_LIMIT = 256 << 10
PERSONAL_NOTICE = 1            # bump when the wording the owner agrees to changes (static/app.js)
_personal_lock = threading.Lock()


def licence_is_noncommercial(text: Any) -> bool:
    return isinstance(text, str) and bool(NONCOMMERCIAL.search(text))


def item_use(value: Any, licence: Any) -> str:
    """The catalog's `use`, failing safe: missing, misspelt or contradicted by
    the licence text → "personal"."""
    if licence_is_noncommercial(licence):
        return "personal"
    return value if isinstance(value, str) and value in USES else "personal"


def provisioning_mode(value: Any, source: Any = None) -> str:
    if isinstance(value, str) and value in PROVISIONING:
        return value
    log(f"[device] provisioning = {value!r} in {source} is not \"owner\" or \"sale\"; "
        "treating this Badger as built for sale (personal-use content waits for the owner)")
    return "sale"


def clean_personal_record(rec: Any) -> dict:
    """Only well-formed fields of the launcher's record survive; anything else
    reads as "nothing accepted"."""
    if not isinstance(rec, dict):
        return {}
    out: dict[str, Any] = {}
    acc = rec.get("accepted")
    if isinstance(acc, dict) and isinstance(acc.get("items"), list):
        items = []
        for x in acc["items"][:1000]:
            if isinstance(x, dict) and isinstance(x.get("id"), str) and SAFE_ID.fullmatch(x["id"]):
                items.append({k: x[k] for k in ("id", "title", "license") if isinstance(x.get(k), str)}
                             | ({"size_gb": x["size_gb"]} if isinstance(x.get("size_gb"), (int, float))
                                and not isinstance(x.get("size_gb"), bool) else {}))
        meta = {k: acc[k] for k in ("at", "by", "ip") if isinstance(acc.get(k), str)}
        if isinstance(acc.get("notice"), int) and not isinstance(acc.get("notice"), bool):
            meta["notice"] = acc["notice"]
        if isinstance(acc.get("licences"), list):
            meta["licences"] = [s for s in acc["licences"][:100] if isinstance(s, str)]
        out["accepted"] = {**meta, "items": items}
    for k in ("later_at", "withdrawn_at"):
        if isinstance(rec.get(k), str) and rec[k]:
            out[k] = rec[k][:40]
    if isinstance(rec.get("history"), list):
        out["history"] = [x for x in rec["history"][-50:] if isinstance(x, dict)]
    return out


def load_personal_record(cfg: Config) -> dict:
    """The owner's decision about personal-use content ({} = none yet, or the
    file can't be read: nothing is accepted)."""
    p = cfg.personal_file
    try:
        if p.is_symlink() or not p.is_file() or p.stat().st_size > PERSONAL_RECORD_LIMIT:
            return {}
        return clean_personal_record(json.loads(p.read_text(encoding="utf-8")))
    except (OSError, ValueError, RecursionError):
        return {}


def accepted_ids(record: dict) -> set[str]:
    if record.get("withdrawn_at"):
        return set()
    return {x["id"] for x in (record.get("accepted") or {}).get("items", [])}


def personal_allowed(item: Item, cfg: Config, record: dict) -> bool:
    """May this Badger hold `item`? Everything licensed for any use: yes. Personal-
    use items: not after the owner removed them; on an owner-built Badger, yes;
    on a Badger built for sale, only the ones the owner accepted."""
    if item.use == "any" and not licence_is_noncommercial(item.license):
        return True
    if record.get("withdrawn_at"):
        return False
    if cfg.provisioning == "owner":
        return True
    return item.id in accepted_ids(record)


def held_personal(items: Iterable[Item], cfg: Config, record: dict | None = None) -> list[Item]:
    """Personal-use items this Badger's tier, pins and budget would select but the
    licence rule holds back (they wait for the owner's acceptance)."""
    record = load_personal_record(cfg) if record is None else record
    return [i for i in _select(list(items), cfg) if not personal_allowed(i, cfg, record)]


def personal_offer(cfg: Config, items: list[Item] | None = None, state: "State | None" = None,
                   record: dict | None = None) -> dict:
    """Everything the launcher shows about personal-use content."""
    items = load_catalog(cfg.catalog) if items is None else items
    state = State(cfg.state_file) if state is None else state
    record = load_personal_record(cfg) if record is None else record
    acc = accepted_ids(record)
    selectable = {i.id for i in _select(items, cfg)}
    rows, pending = [], []
    for i in items:
        if i.use == "any" and not licence_is_noncommercial(i.license):
            continue
        installed = bool(state.get(i.id))
        if i.id not in selectable and not installed and i.id not in acc:
            continue
        allowed = personal_allowed(i, cfg, record)
        manual = i.source.get("type", "manual") == "manual"
        if i.id in selectable and not allowed:
            pending.append(i.id)
        rows.append({"id": i.id, "title": i.title, "category": i.category, "license": i.license,
                     "size_gb": i.size_gb, "manual": manual, "allowed": allowed,
                     "accepted": i.id in acc, "installed": installed, "in_tier": i.id in selectable})
    withdrawn = bool(record.get("withdrawn_at"))
    if not pending or (cfg.provisioning == "owner" and not withdrawn):
        offer = None
    elif cfg.provisioning == "sale" and not acc and not record.get("later_at") and not withdrawn:
        offer = "first-run"
    else:
        offer = "notice"
    installed_ids = [r["id"] for r in rows if r["installed"]]
    acc_meta = record.get("accepted") if not withdrawn else None
    return {
        "provisioning": cfg.provisioning,
        "offer": offer,
        "items": rows,
        "pending": pending,
        "pending_gb": round(sum(r["size_gb"] for r in rows if r["id"] in pending and not r["manual"]), 3),
        "installed": installed_ids,
        "accepted": ({k: v for k, v in acc_meta.items() if k != "items"} | {"items": sorted(acc)})
        if acc_meta else None,
        "later_at": record.get("later_at"),
        "withdrawn_at": record.get("withdrawn_at"),
        "notice": PERSONAL_NOTICE,
        # A unit built for sale is ready to sell only while it holds no personal-use
        # content and nobody has accepted on the buyer's behalf.
        "sale_ready": cfg.provisioning == "sale" and not installed_ids and not acc,
    }


def record_personal_decision(cfg: Config, action: str, *, items: list[str] | None = None,
                             by: str = "", ip: str = "") -> dict:
    """The owner's choice, from the launcher: "accept" (all pending items, or the
    listed ones), "later" (not now: keep offering in Settings / Status) or
    "withdraw" (remove them; the next `upd apply` deletes them from the drive).
    Returns the new record. ValueError for a bad request."""
    if action not in ("accept", "later", "withdraw"):
        raise ValueError("unknown action")
    with _personal_lock:
        catalog = load_catalog(cfg.catalog)
        record = load_personal_record(cfg)
        offer = personal_offer(cfg, catalog, record=record)
        now = now_iso()
        who = re.sub(r"[\x00-\x1f\x7f]", "", by).strip()[:64] or "the owner (admin)"
        hist = list(record.get("history", []))
        if action == "accept":
            pending = offer["pending"]
            if items is not None:
                if not isinstance(items, list) or len(items) > 1000 \
                        or not all(isinstance(x, str) for x in items):
                    raise ValueError("items must be a list of ids")
                bad = sorted(set(items) - set(pending))
                if bad:
                    raise ValueError("not offered on this Badger: " + ", ".join(bad[:5]))
                chosen = [x for x in pending if x in set(items)]
            else:
                chosen = list(pending)
            if not chosen:
                raise ValueError("nothing to accept")
            by_id = {i.id: i for i in catalog}
            keep = [] if record.get("withdrawn_at") else list((record.get("accepted") or {}).get("items", []))
            have = {x["id"] for x in keep}
            keep += [{"id": x, "title": by_id[x].title, "license": by_id[x].license,
                      "size_gb": by_id[x].size_gb} for x in chosen if x not in have]
            record["accepted"] = {"at": now, "by": who, "ip": ip[:64], "notice": PERSONAL_NOTICE,
                                  "licences": sorted({x.get("license", "") for x in keep}), "items": keep}
            record.pop("withdrawn_at", None)
            hist.append({"at": now, "event": "accepted", "by": who, "ip": ip[:64], "items": chosen,
                         "licences": sorted({by_id[x].license for x in chosen}), "notice": PERSONAL_NOTICE})
        elif action == "later":
            record["later_at"] = now
            hist.append({"at": now, "event": "not now", "by": who, "ip": ip[:64]})
        else:
            removed = sorted(accepted_ids(record)) or offer["installed"]
            record.pop("accepted", None)
            record["withdrawn_at"] = now
            hist.append({"at": now, "event": "removed", "by": who, "ip": ip[:64], "items": removed})
        record["history"] = hist[-50:]
        record["format"] = 1
        cfg.personal_file.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(cfg.personal_file, json.dumps(record, indent=2, sort_keys=True))
        return clean_personal_record(record)


def prune_withdrawn(cfg: Config, state: "State", items: list[Item], record: dict) -> list[str]:
    """After the owner removed personal-use content (and only then — never just
    because of the provisioning mode), delete those items from the drive, once
    per removal. Paths come from the ledger and must stay inside data_root."""
    at = record.get("withdrawn_at")
    if not at or state.data.get("personal_pruned") == at:
        return []
    by_id = {i.id: i for i in items}
    root = cfg.data_root.resolve()
    removed, hooks = [], {}
    for item_id, rec in sorted(state.data["items"].items()):
        it = by_id.get(item_id)
        if it is None or personal_allowed(it, cfg, record):
            continue
        for rel in (rec.get("path"), (rec.get("previous") or {}).get("path")):
            rel = safe_rel(rel)
            if not rel:
                continue
            p = cfg.data_root / rel
            try:
                parent = p.parent.resolve()
            except OSError:
                continue
            if parent == root or root in parent.parents:
                remove_path(parent / p.name)           # a symlink: only the link goes
        cur = cfg.data_root / it.dir / f"{it.id}.current"
        if cur.is_symlink():
            cur.unlink()
        state.data["items"].pop(item_id, None)
        state.data["history"].append({"at": now_iso(), "id": item_id, "version": rec.get("version"),
                                      "event": "removed (personal-use licence withdrawn)"})
        removed.append(item_id)
        cmd = hook_for(it, cfg)
        if cmd and cmd not in hooks and "self-update-hook" not in cmd:
            hooks[cmd] = it
        log(f"removed {item_id} (the owner withdrew personal-use content)")
    state.data["personal_pruned"] = at
    state.data["history"] = state.data["history"][-500:]
    state.save()
    for it in hooks.values():                         # e.g. rebuild the Kiwix library without them
        run_hook(it, cfg, None)
    return removed


# ───────────────────────────────── state ─────────────────────────────────────

class State:
    """Installed-content ledger. Written atomically; never half-updated."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = {"items": {}, "history": []}
        if path.exists():
            self.data = json.loads(path.read_text())
            self.data.setdefault("items", {})
            self.data.setdefault("history", [])

    def get(self, item_id: str) -> dict | None:
        return self.data["items"].get(item_id)

    def put(self, item_id: str, rec: dict, event: str) -> None:
        """Record and save; if the save fails (disk full), memory is rolled back
        too, so a later successful save can't persist a record whose files the
        caller has meanwhile put back."""
        before_items, before_hist = dict(self.data["items"]), list(self.data["history"])
        self.data["items"][item_id] = rec
        self.data["history"].append({"at": now_iso(), "id": item_id, "event": event,
                                     "version": rec.get("version")})
        self.data["history"] = self.data["history"][-500:]
        try:
            self.save()
        except BaseException:
            self.data["items"], self.data["history"] = before_items, before_hist
            raise

    def drop(self, item_id: str) -> None:
        self.data["items"].pop(item_id, None)
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self.path, json.dumps(self.data, indent=2, sort_keys=True))


# ─────────────────────────────── utilities ───────────────────────────────────

def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def log(msg: str) -> None:
    print(f"[upd {dt.datetime.now():%H:%M:%S}] {msg}", file=sys.stderr, flush=True)


def atomic_write(path: Path, text: str, mode: int = 0o666) -> None:
    """Write-then-rename. The scratch name is unique per process and thread
    (two portal threads saving settings at once used to share one .tmp and
    trip over each other), is created O_EXCL (never follows a planted
    symlink), respects the umask like a plain open(), and is removed if the
    write fails (e.g. ENOSPC) so the old file stays exactly as it was.
    mode: e.g. 0o640 for private data (the umask can only narrow it further)."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with contextlib.suppress(FileNotFoundError):
        tmp.unlink()
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def human(n: float | int | None) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def sha256_file(path: Path, bufsize: int = 4 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(bufsize):
            h.update(chunk)
    return h.hexdigest()


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


def free_bytes(path: Path) -> int:
    path.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(path).free


def http(url: str, method: str = "GET", timeout: int = 30, headers: dict | None = None):
    req = urllib.request.Request(url, method=method, headers={"User-Agent": UA, **(headers or {})})
    return urllib.request.urlopen(req, timeout=timeout)


FETCH_LIMIT = 128 << 20        # the whole Kiwix OPDS catalog is tens of MB; nothing we read is bigger


class TooLarge(ValueError):
    pass


def fetch_bytes(url: str, timeout: int = 30, limit: int = FETCH_LIMIT) -> bytes:
    """A small resource (listing, catalog, manifest), refused past `limit`
    bytes so a hostile peer or bundle can't exhaust memory with a huge one."""
    if url.startswith("file://") or url.startswith("/"):
        p = Path(url.removeprefix("file://"))
        with open(p, "rb") as fh:
            if os.fstat(fh.fileno()).st_size > limit:
                raise TooLarge(f"{p}: larger than {human(limit)}")
            data = fh.read(limit + 1)
    else:
        with http(url, timeout=timeout) as r:
            data = r.read(limit + 1)
    if len(data) > limit:
        raise TooLarge(f"{url}: larger than {human(limit)}")
    return data


def fetch_text(url: str, timeout: int = 30, limit: int = FETCH_LIMIT) -> str:
    return fetch_bytes(url, timeout, limit).decode("utf-8", "replace")


def in_window(window: str, at: dt.datetime | None = None) -> bool:
    if not window:
        return True
    at = at or dt.datetime.now()
    start_s, end_s = window.split("-")
    start = dt.time.fromisoformat(start_s.strip())
    end = dt.time.fromisoformat(end_s.strip())
    t = at.time()
    return start <= t < end if start <= end else (t >= start or t < end)


def vkey(v: Any) -> tuple:
    """Natural sort key: '2026-07a' < '2026-08', '99' < '133'. Letters compare
    case-insensitively; None/'' sort below everything; non-strings (a bare
    TOML integer) are compared by their text."""
    s = "" if v is None else str(v)
    return tuple((0, int(t)) if t.isdigit() else (1, t.lower()) for t in re.findall(r"\d+|[A-Za-z]+", s))


def vnewer(a: Any, b: Any) -> bool:
    return vkey(a) > vkey(b)


def strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


class Busy(SystemExit):
    """Another upd run holds the lock (exits 1 with this message if uncaught)."""


@contextlib.contextmanager
def run_lock(cfg: Config):
    lock_path = cfg.state_file.parent / "upd.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Busy("upd: another update is already running") from None
        fh.write(str(os.getpid()))
        fh.flush()
        yield


# ─────────────────────────────── resolvers ───────────────────────────────────

@dataclasses.dataclass
class Remote:
    version: str
    filename: str
    url: str
    size: int | None = None
    sha256: str | None = None
    comparable: bool = True                # versions sort as strings (dates)
    rsync_url: str | None = None
    diff_url: str | None = None            # OLD→NEW zimdiff, if someone offers one
    diff_size: int | None = None
    origin: str = "upstream"               # upstream | peer:<url> | bundle:<path>
    meta: dict[str, Any] = dataclasses.field(default_factory=dict)


class Offline(Exception):
    pass


def _net_errors():
    return (urllib.error.URLError, TimeoutError, ConnectionError, OSError)


def resolve(item: Item, cfg: Config) -> Remote | None:
    t = item.source.get("type", "manual")
    fn = RESOLVERS.get(t)
    if fn is None:
        raise ValueError(f"{item.id}: unknown source type {t!r}")
    return fn(item, cfg)


def resolve_kiwix(item: Item, cfg: Config) -> Remote:
    name = item.source.get("name", item.id)
    subdir = item.source.get("path", name.split("_", 1)[0] + "/")
    try:
        return _kiwix_from_opds(name, subdir, cfg)
    except _net_errors() + (ET.ParseError, LookupError) as e:
        log(f"{item.id}: OPDS lookup failed ({e}); trying mirror listing")
    return _kiwix_from_listing(name, subdir, cfg)


FLAVOURS = ("maxi", "nopic", "mini")
ZIM_VER = r"\d{4}-\d{2}[a-z]?"
_OPDS_FULL: dict[str, str] = {}          # whole-catalog fetches, once per run


def _best_opds_match(xml: str, name: str) -> Remote | None:
    """Newest entry whose download file is `{name}_{YYYY-MM}.zim`.

    Matching on the href's file stem, not on the OPDS `name` field, because the
    two differ for some books (gardenology.org_en_all_… is listed as
    name=gardenology_en_all) and flavours live in a separate field."""
    pat = re.compile(rf"^{re.escape(name)}_({ZIM_VER})\.zim$")
    best = None
    # ET never resolves external entities; the expat it links refuses billion-laughs
    # expansion; fetch_text caps the size.
    for entry in ET.fromstring(xml):  # noqa: S314
        if strip_ns(entry.tag) != "entry":
            continue
        fields = {strip_ns(c.tag): (c.text or "").strip() for c in entry}
        link = None
        for c in entry:
            if strip_ns(c.tag) == "link" and c.get("type") == "application/x-zim":
                link = c
        if link is None:
            continue
        url = link.get("href", "").removesuffix(".meta4")
        filename = url.rsplit("/", 1)[-1]
        m = pat.match(filename)
        if not m:
            continue
        cand = Remote(version=m.group(1), filename=filename, url=url,
                      size=int(link.get("length")) if (link.get("length") or "").isdigit() else None,
                      meta={k: fields[k] for k in ("articleCount", "mediaCount", "updated",
                                                     "title", "flavour") if k in fields})
        if best is None or vnewer(cand.version, best.version):
            best = cand
    return best


def kiwix_subdir_from_url(url: str) -> str | None:
    """'https://…/zim/books/x_2026-11a.zim' → 'books/' (where the mirror keeps it now)."""
    path = urllib.parse.urlparse(url).path
    if "/zim/" not in path:
        return None
    sub = path.split("/zim/", 1)[1].rpartition("/")[0]
    if not sub or ".." in sub.split("/"):
        return None
    return sub.strip("/") + "/"


def _kiwix_from_opds(name: str, subdir: str, cfg: Config) -> Remote:
    """`name` is the file stem without the date, e.g. wikipedia_en_all_maxi.
    The OPDS catalog stores that as name=wikipedia_en_all + flavour=maxi, so
    ask for that name first; if the catalog lists the book under a different
    name, look through the whole catalog (one fetch per run)."""
    base, _, flav = name.rpartition("_")
    if flav not in FLAVOURS:
        base = name
    q = urllib.parse.urlencode({"name": base, "count": "50"})
    best = _best_opds_match(fetch_text(f"{cfg.kiwix_opds}?{q}", cfg.timeout), name)
    if best is None:
        if cfg.kiwix_opds not in _OPDS_FULL:
            _OPDS_FULL[cfg.kiwix_opds] = fetch_text(f"{cfg.kiwix_opds}?count=-1", max(cfg.timeout, 120))
        best = _best_opds_match(_OPDS_FULL[cfg.kiwix_opds], name)
    if best is None:
        raise LookupError(f"{name} not in catalog")
    # The OPDS href says where the file lives today (Gutenberg LCC moved to zim/books/);
    # the catalog's `path` is only the fallback.
    _kiwix_enrich(best, kiwix_subdir_from_url(best.url) or subdir, cfg)
    return best


def _kiwix_from_listing(name: str, subdir: str, cfg: Config) -> Remote:
    base = urllib.parse.urljoin(cfg.kiwix_http, subdir)
    try:
        html = fetch_text(base, cfg.timeout)
    except _net_errors() as e:
        raise Offline(str(e)) from e
    pat = re.compile(rf'href="({re.escape(name)}_({ZIM_VER})\.zim)"')
    found = sorted({(m.group(2), m.group(1)) for m in pat.finditer(html)}, key=lambda t: vkey(t[0]))
    if not found:
        raise LookupError(f"{name}: no files in {base}")
    version, filename = found[-1]
    r = Remote(version=version, filename=filename, url=urllib.parse.urljoin(base, filename))
    _kiwix_enrich(r, subdir, cfg)
    return r


def _kiwix_enrich(r: Remote, subdir: str, cfg: Config) -> None:
    """sha256 + size from the metalink; rsync path for delta transfer."""
    try:
        meta = ET.fromstring(fetch_text(r.url + ".meta4", cfg.timeout, 1 << 20))  # noqa: S314 (see _best_opds_match)
        for el in meta.iter():
            tag = strip_ns(el.tag)
            if tag == "hash" and el.get("type") == "sha-256":
                r.sha256 = (el.text or "").strip()
            elif tag == "size" and el.text:
                r.size = int(el.text)
    except _net_errors() + (ET.ParseError,):
        pass
    if cfg.kiwix_rsync:
        # (not urljoin: it doesn't know the rsync:// scheme and drops the host)
        r.rsync_url = cfg.kiwix_rsync.rstrip("/") + "/" + subdir.strip("/") + "/" + r.filename


def resolve_http(item: Item, cfg: Config) -> Remote:
    src = item.source
    try:
        if "index" in src:                                 # newest file in a listing
            html = fetch_text(src["index"], cfg.timeout)
            pat = re.compile(src["pattern"])               # must have a group named v
            found = sorted({(m.group("v"), m.group(0)) for m in pat.finditer(html)},
                           key=lambda t: vkey(t[0]))
            if not found:
                raise LookupError(f"{item.id}: nothing matches {src['pattern']}")
            version, filename = found[-1]
            url = urllib.parse.urljoin(src.get("download_base", src["index"]), filename)
            r = Remote(version=version, filename=filename.rsplit("/", 1)[-1], url=url)
            with contextlib.suppress(*_net_errors()):
                with http(url, "HEAD", cfg.timeout) as h:
                    r.size = int(h.headers.get("Content-Length") or 0) or None
        else:                                              # fixed URL, fingerprint it
            url = src["url"]
            with http(url, "HEAD", cfg.timeout) as h:
                fp = h.headers.get("ETag") or h.headers.get("Last-Modified") \
                    or h.headers.get("Content-Length") or ""
                size = int(h.headers.get("Content-Length") or 0) or None
            r = Remote(version=fp.strip('"'), filename=src.get("filename", url.rsplit("/", 1)[-1]),
                       url=url, size=size, comparable=False)
    except _net_errors() as e:
        raise Offline(str(e)) from e
    if "sha256_url" in src:
        with contextlib.suppress(*_net_errors()):
            r.sha256 = fetch_text(src["sha256_url"].format(filename=r.filename), cfg.timeout).split()[0]
    return r


def resolve_hf(item: Item, cfg: Config) -> Remote:
    src = item.source
    url = f"https://huggingface.co/{src['repo']}/resolve/{src.get('revision', 'main')}/{src['file']}"
    try:
        # Don't follow the redirect: the first response carries the LFS sha256.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(NoRedirect)
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
        try:
            resp = opener.open(req, timeout=cfg.timeout)
            headers = resp.headers
        except urllib.error.HTTPError as e:            # 302 lands here
            headers = e.headers
        etag = (headers.get("X-Linked-Etag") or headers.get("ETag") or "").strip('"')
        size = int(headers.get("X-Linked-Size") or 0) or None
    except _net_errors() as e:
        raise Offline(str(e)) from e
    sha = etag if re.fullmatch(r"[0-9a-f]{64}", etag) else None
    pin = src.get("sha256")
    if pin is not None:
        if not (isinstance(pin, str) and re.fullmatch(r"[0-9a-f]{64}", pin)):
            raise ValueError(f"catalog: {item.id} sha256 must be 64 lowercase hex characters")
        if sha != pin:
            raise ValueError(f"{item.id}: Hugging Face's file checksum is {(sha or 'missing')[:12]}, not the pinned "
                             f"{pin[:12]} — not downloaded (update the catalog pin after checking the model)")
    return Remote(version=etag[:16], filename=src["file"].rsplit("/", 1)[-1], url=url,
                  size=size, sha256=sha, comparable=False)


GIT_COMMIT_RE = re.compile(r"[0-9a-f]{40}")


def resolve_git(item: Item, cfg: Config) -> Remote:
    """A git item: the commit its ref points at. With `commit = "<40 hex>"` in the catalog (code we run on
    the family's phones, e.g. Kiwix JS) that commit is the only one ever installed: if upstream's ref has
    moved — a new release, or someone else's code — nothing changes until the catalog is updated."""
    if not have("git"):
        raise Offline("git not installed")
    ref = item.source.get("ref", "HEAD")
    pin = item.source.get("commit")
    if pin is not None and not (isinstance(pin, str) and GIT_COMMIT_RE.fullmatch(pin)):
        raise ValueError(f"catalog: {item.id} commit must be a full 40-character commit id")
    try:
        out = subprocess.run(["git", "ls-remote", item.source["url"], ref, ref + "^{}"], capture_output=True,
                             text=True, timeout=cfg.timeout, check=True).stdout
    except (subprocess.SubprocessError, OSError) as e:
        raise Offline(str(e)) from e
    rows = [ln.split() for ln in out.splitlines() if len(ln.split()) == 2]
    peeled = [sha for sha, name in rows if name.endswith("^{}")]
    sha = (peeled or [r[0] for r in rows] or [""])[0]
    if pin:
        if sha != pin:
            raise ValueError(f"{item.id}: upstream {ref} is now {sha[:12] or 'missing'}, not the pinned commit "
                             f"{pin[:12]} — not installed (update the catalog pin after checking the release)")
        sha = pin
    return Remote(version=sha[:12], filename=item.id, url=item.source["url"], comparable=False,
                  meta={"ref": ref, "commit": sha, **({"pinned": True} if pin else {})})


def git_checkout_ok(target: Path, commit: str) -> None:
    """After a fetch/clone: HEAD must be the pinned commit and the work tree exactly its tree."""
    head = subprocess.run(["git", "-C", str(target), "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True).stdout.strip()
    dirty = subprocess.run(["git", "-C", str(target), "status", "--porcelain", "--untracked-files=all"],
                           capture_output=True, text=True, check=True).stdout.strip()
    if head != commit or dirty:
        raise RuntimeError(f"git checkout is {head[:12]}{' with local changes' if dirty else ''}, "
                           f"not the pinned {commit[:12]}")


def resolve_rsync(item: Item, cfg: Config) -> Remote:
    """Version = hash of the remote tree's listing (names, sizes, mtimes)."""
    if not have("rsync"):
        raise Offline("rsync not installed")
    url = item.source["url"].rstrip("/") + "/"
    try:
        out = subprocess.run(["rsync", "-r", "--list-only", url], capture_output=True,
                             text=True, timeout=max(cfg.timeout, 120), check=True).stdout
    except (subprocess.SubprocessError, OSError) as e:
        raise Offline(str(e)) from e
    total = 0
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and not line.startswith("d"):
            with contextlib.suppress(ValueError):
                total += int(parts[1].replace(",", ""))
    digest = hashlib.sha256(out.encode()).hexdigest()
    return Remote(version=digest[:16], filename=item.id, url=url, size=total, comparable=False,
                  rsync_url=url)


def resolve_pmtiles(item: Item, cfg: Config) -> Remote:
    src = item.source
    try:
        builds = json.loads(fetch_text(src.get("builds", cfg.protomaps_builds), cfg.timeout))
    except _net_errors() + (json.JSONDecodeError,) as e:
        raise Offline(str(e)) from e
    keys = sorted(b["key"] for b in builds if b.get("key", "").endswith(".pmtiles"))
    if not keys:
        raise LookupError("no protomaps builds listed")
    key = keys[-1]
    version = key.removesuffix(".pmtiles")
    return Remote(version=version, filename=f"{item.id}.pmtiles",
                  url=urllib.parse.urljoin(src.get("base", cfg.protomaps_http), key),
                  size=int(item.size_gb * 1e9) or None,
                  meta={"bbox": src.get("bbox"), "maxzoom": src.get("maxzoom")})


def resolve_manual(item: Item, cfg: Config) -> None:
    return None


RESOLVERS = {
    "kiwix": resolve_kiwix, "http": resolve_http, "hf": resolve_hf, "git": resolve_git,
    "rsync": resolve_rsync, "pmtiles": resolve_pmtiles, "manual": resolve_manual,
}


# ───────────────────────── peers & bundles (offline) ─────────────────────────

def signer_scope(data: bytes, sig: bytes | None, cfg: Config) -> str | None:
    """Who may have signed exactly these bytes: "code" (a "prepper-code" signer), "content" (a
    "prepper-update" signer) or None (nobody we trust). The caller then parses the same bytes, so
    nothing can swap the file between check and use. (ssh-keygen wants the signature as a file: it
    goes in a private temp dir.)"""
    if not sig or not cfg.allowed_signers.exists() or not have("ssh-keygen"):
        return None
    with tempfile.TemporaryDirectory(prefix="upd-verify-") as d:
        sigp = Path(d) / "manifest.json.sig"
        sigp.write_bytes(sig)
        for scope, principal in SCOPE_PRINCIPALS:
            res = subprocess.run(["ssh-keygen", "-Y", "verify", "-f", str(cfg.allowed_signers),
                                  "-I", principal, "-n", SIG_NAMESPACE, "-s", str(sigp)],
                                 input=data, capture_output=True, check=False)
            if res.returncode == 0:
                return scope
    return None


def verify_bytes(data: bytes, sig: bytes | None, cfg: Config) -> bool:
    """Is `sig` a trusted signature (any scope) over exactly these bytes?"""
    return signer_scope(data, sig, cfg) is not None


def verify_signature(manifest: Path, cfg: Config) -> bool:
    sig = manifest.with_name(manifest.name + ".sig")
    if not sig.exists():
        return False
    return verify_bytes(manifest.read_bytes(), sig.read_bytes(), cfg)


def sign_file(path: Path, cfg: Config) -> bool:
    """Write path.sig. Signs a scratch copy and renames the fresh signature into
    place: `ssh-keygen -Y sign` asks "Overwrite?" when the .sig already exists,
    reads EOF as "no" and still exits 0, which used to leave a stale signature
    that made every peer reject this disk after its first update."""
    if not cfg.signing_key.exists() or not have("ssh-keygen"):
        log("no signing key; bundle will be unsigned (receivers may refuse it)")
        return False
    sig = path.with_name(path.name + ".sig")
    scratch = path.with_name(f".{path.name}.signing")
    scratch_sig = scratch.with_name(scratch.name + ".sig")
    try:
        for p in (scratch, scratch_sig):
            with contextlib.suppress(FileNotFoundError):
                p.unlink()
        shutil.copyfile(path, scratch)
        env, ask_dir = None, None
        pass_file = cfg.signing_key.with_name(cfg.signing_key.name + ".pass")
        if pass_file.is_file():
            # prepperctl gen-key encrypts the device key; its passphrase file goes to ssh-keygen through
            # SSH_ASKPASS (never argv, never the environment)
            ask_dir = tempfile.mkdtemp(prefix="upd-ask-")
            ask = Path(ask_dir) / "ask"
            ask.write_text("#!/bin/sh\nexec cat " + shlex.quote(str(pass_file)) + "\n")
            ask.chmod(0o700)
            env = dict(os.environ, SSH_ASKPASS=str(ask), SSH_ASKPASS_REQUIRE="force", DISPLAY="none")
        try:
            res = subprocess.run(["ssh-keygen", "-Y", "sign", "-f", str(cfg.signing_key),
                                  "-n", SIG_NAMESPACE, str(scratch)],
                                 capture_output=True, stdin=subprocess.DEVNULL, env=env,
                                 start_new_session=True, timeout=60)   # no tty: never wait on a passphrase prompt
            err = res.stderr.decode(errors="replace").strip()
            ok = res.returncode == 0
        except subprocess.TimeoutExpired:
            ok, err = False, "ssh-keygen timed out (passphrase-protected key?)"
        finally:
            if ask_dir:
                shutil.rmtree(ask_dir, ignore_errors=True)
        # The scratch .sig did not exist before this run, so if it exists now it is new.
        if not ok or not scratch_sig.exists() or scratch_sig.stat().st_size == 0:
            log(f"signing {path.name} failed: {err}")
            with contextlib.suppress(FileNotFoundError):
                sig.unlink()                   # better unsigned than signed for other content
            return False
        os.replace(scratch_sig, sig)
        return True
    finally:
        for p in (scratch, scratch_sig):
            with contextlib.suppress(FileNotFoundError):
                p.unlink()


def safe_name(name: Any) -> bool:
    """A plain file name: no directories, no leading dot, no NUL."""
    return (isinstance(name, str) and name not in ("", ".", "..") and name == Path(name).name
            and not name.startswith(".") and "\x00" not in name and "/" not in name and "\\" not in name)


def safe_rel(rel: Any) -> str | None:
    """A relative path that cannot climb out of its base (no absolute, no '..')."""
    if not isinstance(rel, str) or not rel or "\x00" in rel or "\\" in rel or rel.startswith("/"):
        return None
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    return rel


def offer_location(base: str, rel: Any) -> str | None:
    """base + rel, confined to base. For a bundle on disk, symlinks are resolved
    too, so a link inside the bundle can't pull in /etc/shadow."""
    rel = safe_rel(rel)
    if rel is None:
        return None
    if base.startswith("http"):
        return base + urllib.parse.quote(rel)
    root = Path(base).resolve()
    p = (root / rel).resolve()
    return str(p) if root in p.parents else None


_OBTAINED_MIN = dt.datetime.min.replace(tzinfo=dt.timezone.utc)


def parse_ts(s: Any) -> dt.datetime:
    try:
        t = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        t = t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
        t.astimezone(dt.timezone.utc)          # offsets at the edge of the range overflow here, not later
        return t
    except (ValueError, OverflowError):
        return _OBTAINED_MIN


SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
MANIFEST_LIMIT = 16 << 20                 # a manifest for thousands of items is well under 1 MB


def _nonneg_int(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def _peer_marks_path(cfg: Config) -> Path:
    return cfg.state_file.parent / "peer-manifests.json"


def load_offer(base: str, cfg: Config, tmpdir: Path | None = None) -> dict[str, Remote]:
    """Read a peer (http://…/sync) or bundle (/media/usb/…) manifest → {id: Remote}.

    Every entry must carry a content hash that the signed manifest vouches for:
    `sha256` for a file, `tree_sha256` (see tree_sha256()) for a directory —
    otherwise a file swapped on the USB stick, or a whole app tree with its
    .git, would ride in under a genuine signature. (`tmpdir` is unused: the
    manifest is never written to disk before it is verified and parsed.)"""
    is_http = base.startswith("http")
    base = base.rstrip("/") + "/"
    # Read once into memory, verify those bytes, parse those bytes: there is no
    # file in between for a concurrent run (or anyone) to swap after the check.
    try:
        raw = fetch_bytes(base + "manifest.json", cfg.timeout, MANIFEST_LIMIT)
        sig = None
        with contextlib.suppress(*_net_errors(), FileNotFoundError, TooLarge):
            sig = fetch_bytes(base + "manifest.json.sig", cfg.timeout, 64 << 10)
    except TooLarge as e:
        log(f"offer {base}: {e} — ignored")
        return {}
    except _net_errors() + (FileNotFoundError,) as e:
        log(f"offer {base}: unreachable ({e})")
        return {}
    scope = signer_scope(raw, sig, cfg)
    signed = scope is not None
    if cfg.require_signed_peers and not signed:
        log(f"offer {base}: manifest not signed by a trusted key — ignored")
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError) as e:
        log(f"offer {base}: unreadable manifest ({type(e).__name__}) — ignored")
        return {}
    if not isinstance(data, dict) or not isinstance(data.get("items", []), list):
        log(f"offer {base}: manifest is not a {{..., items: [...]}} object — ignored")
        return {}
    # Replay guard: `generated` is inside the signed manifest. Never accept a
    # manifest older than the newest one already accepted from the same disk,
    # so an old signed manifest can't be served again to push a downgrade.
    disk_id, generated = str(data.get("disk_id", "")), data.get("generated")
    marks_path = _peer_marks_path(cfg)
    marks = {}
    with contextlib.suppress(OSError, json.JSONDecodeError):
        marks = json.loads(marks_path.read_text())
    if signed and disk_id and generated:
        last = marks.get(disk_id)
        if last and parse_ts(generated) < parse_ts(last):
            log(f"offer {base}: manifest from {disk_id} generated {generated}, older than "
                f"{last} already seen — replay ignored")
            return {}
        # A peer whose clock runs ahead would otherwise set a mark that locks
        # out every honest manifest it writes until real time catches up (for a
        # clock set to 2099: for ever). Remember at most "now" — unless our own
        # clock is clearly unset (no RTC battery, no GPS yet).
        now = dt.datetime.now(dt.timezone.utc)
        keep = generated
        if now.year >= 2024 and parse_ts(generated) > now:
            keep = now.replace(microsecond=0).isoformat()
        if last != keep and (not last or parse_ts(keep) > parse_ts(last)):
            marks[disk_id] = keep
            with contextlib.suppress(OSError):
                marks_path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write(marks_path, json.dumps(marks, indent=2, sort_keys=True))
    origin = ("peer:" if is_http else "bundle:") + base
    offers = {}
    for rec in data.get("items", []):
        if not isinstance(rec, dict) or not isinstance(rec.get("id"), str):
            continue
        url = offer_location(base, rec.get("path"))
        sha = rec.get("sha256")
        tree = rec.get("tree_sha256")
        comparable = rec.get("comparable")
        is_dir = url is not None and not is_http and Path(url).is_dir()
        if not SAFE_ID.fullmatch(rec["id"]) or not safe_name(rec.get("filename")) or url is None \
                or not isinstance(rec.get("version"), str) or not rec["version"] \
                or (sha is not None and not (isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha))) \
                or (tree is not None and not (isinstance(tree, str) and re.fullmatch(r"[0-9a-f]{64}", tree))) \
                or (is_dir and tree is None) or (not is_dir and sha is None) \
                or (rec.get("size") is not None and _nonneg_int(rec.get("size")) is None) \
                or (comparable is not None and not isinstance(comparable, bool)):
            log(f"offer {base}: rejected unsafe or unverifiable entry {str(rec.get('id'))[:60]!r} "
                f"(filename={str(rec.get('filename'))[:60]!r}, path={str(rec.get('path'))[:80]!r}, "
                f"{'tree_sha256' if is_dir else 'sha256'}={'present' if (tree if is_dir else sha) else 'missing'})")
            continue
        if rec["id"] in offers:
            log(f"offer {base}: duplicate entry for {rec['id']!r} — keeping the first")
            continue
        r = Remote(version=rec["version"], filename=rec["filename"], url=url, size=_nonneg_int(rec.get("size")),
                   sha256=sha, comparable=comparable is not False,   # missing/null: like a fresh record
                   origin=origin,
                   meta={"obtained": rec.get("obtained"), "signed": signed, "scope": scope,
                         "manifest_generated": generated, "disk_id": disk_id,
                         **({"tree_sha256": tree} if is_dir else {})})
        diffs = rec.get("diffs") or []
        for d in diffs if isinstance(diffs, list) else []:        # zimdiff patches this offer carries
            durl = offer_location(base, d.get("path")) if isinstance(d, dict) else None
            if durl is None or not safe_name(Path(str(d.get("path"))).name) or not isinstance(d.get("from"), str):
                continue
            r.meta.setdefault("diffs", []).append({"from": d["from"], "path": d["path"],
                                                   "size": _nonneg_int(d.get("size")), "url": durl})
        offers[rec["id"]] = r
    return offers


# ─────────────────────────────────── plan ────────────────────────────────────

@dataclasses.dataclass
class PlanEntry:
    item: Item
    local: dict | None
    remote: Remote | None
    status: str          # up-to-date | update | install | manual | offline | error
    method: str = ""
    download_bytes: int | None = None
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "id": self.item.id, "title": self.item.title, "category": self.item.category,
            "status": self.status, "method": self.method,
            "local_version": (self.local or {}).get("version"),
            "remote_version": self.remote.version if self.remote else None,
            "remote_filename": self.remote.filename if self.remote else None,
            "origin": self.remote.origin if self.remote else None,
            "download_bytes": self.download_bytes, "download_human": human(self.download_bytes),
            "size": self.remote.size if self.remote else None, "note": self.note,
            "meta": self.remote.meta if self.remote else {},
        }


def is_newer(remote: Remote, local: dict | None) -> bool:
    if not local:
        return True
    if remote.sha256 and remote.sha256 == local.get("sha256"):
        return False
    if remote.comparable and local.get("comparable", True):
        return vnewer(remote.version, local.get("version", ""))
    return remote.version != local.get("version")


def offer_is_newer(offer: Remote, local: dict | None) -> bool:
    """Like is_newer, for what another disk or a bundle offers when upstream
    can't vouch for it. Versions that don't sort (hf/http ETags, git and rsync
    hashes) only say "different", so a peer still holding the old build would
    pull us back down — and then we'd pull it back up, forever. For those,
    trust the newer `obtained` time (when that copy first came from upstream)."""
    if not local:
        return True
    if offer.sha256 and offer.sha256 == local.get("sha256"):
        return False
    if offer.comparable and local.get("comparable", True):
        return vnewer(offer.version, local.get("version", ""))
    if offer.version == local.get("version"):
        return False
    return parse_ts(offer.meta.get("obtained")) > parse_ts(local.get("obtained"))


def _as_local(r: Remote) -> dict:
    return {"version": r.version, "comparable": r.comparable, "sha256": r.sha256,
            "obtained": r.meta.get("obtained")}


def choose_method(item: Item, local: dict | None, r: Remote, cfg: Config) -> tuple[str, int | None]:
    """Pick the cheapest way to get from what we have to `r`, and estimate bytes."""
    if r.origin.startswith(("peer:", "bundle:")):
        for d in r.meta.get("diffs", []):
            if local and d.get("from") == local.get("version") and have("zimpatch"):
                return "zimpatch", d.get("size")
        return ("copy" if r.origin.startswith("bundle:") else "peer"), r.size
    t = item.source.get("type")
    if t == "git":
        return "git-delta", None
    if t == "rsync":
        return "rsync-tree", r.size
    if t == "pmtiles":
        return "pmtiles-extract", r.size
    if item.kind == "zim" and local and r.rsync_url and have("rsync"):
        # rsync --fuzzy uses the installed ZIM as the basis file. ZIM clusters are
        # compressed, so savings vary: unchanged clusters/images match, text rarely.
        return "rsync-fuzzy", r.size
    return "http", r.size


def offer_refused(item: Item, offer: Remote, cfg: Config) -> str | None:
    """Why a peer/bundle offer may not be used for this item (None: it may). See CODE_KINDS.
    With require_signed_peers = false the owner has said "take whatever is plugged in": unsigned offers
    then count as fully trusted (documented in docs/SECURITY.md); a signed one keeps its signer's scope."""
    if item.source.get("commit"):
        return "pinned to a git commit — only installed from upstream, checked against the pin"
    pin = item.source.get("sha256")
    if isinstance(pin, str) and pin:
        return None if offer.sha256 == pin else "its file does not match the catalog's sha256 pin"
    scope = offer.meta.get("scope")
    if scope is None and not offer.meta.get("signed") and not cfg.require_signed_peers:
        scope = "code"
    if item.kind in CODE_KINDS and scope != "code":
        return ("it is code (app, APK or AI model) and the signer is trusted for content only "
                "(allowed_signers \"prepper-update\"; code needs \"prepper-code\")")
    return None


def build_plan(cfg: Config, offline: bool = False, only: set[str] | None = None,
               offers: dict[str, Remote] | None = None) -> list[PlanEntry]:
    items = load_catalog(cfg.catalog)
    state = State(cfg.state_file)
    tmpdir = cfg.state_file.parent / "tmp"
    tmpdir.mkdir(parents=True, exist_ok=True)

    peer_offers: dict[str, Remote] = dict(offers or {})
    for peer in cfg.peers:
        for k, v in load_offer(peer, cfg, tmpdir).items():
            if k not in peer_offers or offer_is_newer(v, _as_local(peer_offers[k])):
                peer_offers[k] = v

    record = load_personal_record(cfg)
    for i in held_personal(items, cfg, record):
        if i.id in peer_offers and (not only or i.id in only):
            log(f"not taking {i.id} from {peer_offers[i.id].origin}: licensed for personal use only "
                "— the owner must accept it first (launcher: Settings)")
    plan = []
    for item in wanted(items, cfg, record):
        if only and item.id not in only:
            continue
        local = state.get(item.id)
        remote: Remote | None = None
        note = ""
        if not offline and item.source.get("type", "manual") != "manual":
            try:
                remote = resolve(item, cfg)
            except Offline as e:
                note = f"upstream unreachable: {e}"
            except Exception as e:  # noqa: BLE001 — a broken entry must not stop the run
                plan.append(PlanEntry(item, local, None, "error", note=f"{type(e).__name__}: {e}"))
                continue
        offer = peer_offers.get(item.id)
        why = offer_refused(item, offer, cfg) if offer else None
        if why:
            log(f"not taking {item.id} from {offer.origin}: {why}")
            offer = None
        vouched = True              # upstream (or a sortable version) says this is current
        if offer and (remote is None or offer.version == remote.version
                      or (offer.comparable and remote.comparable and vnewer(offer.version, remote.version))):
            if remote and offer.version == remote.version and not offer.sha256:
                offer.sha256 = remote.sha256
            vouched = remote is not None and offer.version == remote.version
            remote = offer          # same bytes, but over the LAN / from USB
        if item.source.get("type", "manual") == "manual" and not offer:
            plan.append(PlanEntry(item, local, None, "manual",
                                  note="supplied by you — copy into " + item.dir + "/"))
            continue
        if remote is None:
            plan.append(PlanEntry(item, local, None, "offline", note=note or "no source reachable"))
            continue
        newer = is_newer(remote, local) if vouched or remote.origin == "upstream" \
            else offer_is_newer(remote, local)
        if not newer:
            plan.append(PlanEntry(item, local, remote, "up-to-date", download_bytes=0))
            continue
        method, est = choose_method(item, local, remote, cfg)
        delta_note = ""
        if local and remote.meta.get("articleCount") and local.get("meta", {}).get("articleCount"):
            delta = int(remote.meta["articleCount"]) - int(local["meta"]["articleCount"])
            delta_note = f"{delta:+,} articles"
        if local and remote.size and local.get("size"):
            delta_note += (", " if delta_note else "") + f"size {human(remote.size - local['size'])}"
        plan.append(PlanEntry(item, local, remote, "update" if local else "install",
                              method=method, download_bytes=est, note=delta_note))
    return plan


def write_report(cfg: Config, plan: list[PlanEntry]) -> Path:
    cfg.report_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "generated": now_iso(),
        "tier": cfg.tier,
        "counts": {s: sum(1 for p in plan if p.status == s)
                   for s in ("up-to-date", "update", "install", "manual", "offline", "error")},
        "download_bytes": sum(p.download_bytes or 0 for p in plan if p.status in ("update", "install")),
        "free_bytes": free_bytes(cfg.data_root),
        "items": [p.as_dict() for p in plan],
        "provisioning": cfg.provisioning,
    }
    try:                     # personal-use items waiting for the owner's acceptance (not downloaded)
        summary["personal_held"] = [i.id for i in held_personal(load_catalog(cfg.catalog), cfg)]
    except (OSError, tomllib.TOMLDecodeError, KeyError, ValueError):
        summary["personal_held"] = []
    summary["download_human"] = human(summary["download_bytes"])
    path = cfg.report_dir / "last-check.json"
    atomic_write(path, json.dumps(summary, indent=2))
    return path


# ──────────────────────────────── transfers ──────────────────────────────────

def tree_sha256(root: Path) -> str:
    """Deterministic hash of a directory tree: every relative path (sorted),
    and for files their size and sha256. Symlinks, devices, sockets and FIFOs
    are refused (ValueError) — a tree we hash is a tree we can vouch for.
    Mode bits are left out on purpose: bundles live on FAT/exFAT sticks,
    which can't keep them, and every honest tree would then fail to verify."""
    h = hashlib.sha256(b"prepper-tree-v1\n")
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"{root}: not a directory")
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(dirnames + filenames):
            p = os.path.join(dirpath, name)
            rel = os.path.relpath(p, root).replace(os.sep, "/")
            st = os.lstat(p)
            if stat.S_ISDIR(st.st_mode):
                h.update(b"d " + json.dumps(rel).encode() + b"\n")
            elif stat.S_ISREG(st.st_mode):
                h.update(b"f " + json.dumps(rel).encode() + b" %d " % st.st_size
                         + sha256_file(Path(p)).encode() + b"\n")
            else:
                kind = "symlink" if stat.S_ISLNK(st.st_mode) else "special file"
                raise ValueError(f"{rel}: {kind} in a tree (only files and folders can be verified)")
    return h.hexdigest()


def _copy_bundle_tree(src: Path, dst: Path) -> None:
    """Copy a tree from a bundle. Symlinks are kept only when they are relative
    and stay inside the tree: an absolute link, or one that climbs out
    (../../etc/shadow), would otherwise be published under /content/ by nginx."""
    root = src.resolve()

    def ignore(d: str, names: list[str]) -> set[str]:
        out = set()
        for n in names:
            p = Path(d) / n
            if p.is_symlink():
                target = os.readlink(p)
                r = p.resolve()
                if os.path.isabs(target) or not (r == root or root in r.parents):
                    log(f"  skipping symlink {p.relative_to(src)} -> {target} (points outside the bundle tree)")
                    out.add(n)
        return out
    shutil.copytree(src, dst, symlinks=True, ignore=ignore)


def http_download(url: str, dest: Path, max_rate_kbps: int = 0, timeout: int = 60,
                  expected_size: int | None = None, verifiable: bool = False) -> None:
    """Resumable download to dest (.part while in flight), optional rate cap.

    expected_size: refuse to write more than this (a peer or bundle that lies
    about the size can't fill the disk past the reserve upd checked for).
    verifiable: the caller checks a sha256 afterwards, so a partial file may be
    resumed even when the server gave no validator to guard the resume with."""
    part = dest.with_name(dest.name + ".part")
    if url.startswith("file://") or url.startswith("/"):
        src = Path(url.removeprefix("file://"))
        if part.is_dir() and not part.is_symlink():
            shutil.rmtree(part)
        else:
            remove_path(part)
        if src.is_dir():                       # bundles carry trees (apps, doc sets) as folders
            _copy_bundle_tree(src, part)
        else:
            if expected_size is not None and src.stat().st_size != expected_size:
                raise RuntimeError(f"size mismatch for {dest.name}: bundle file is {src.stat().st_size} bytes, "
                                   f"manifest says {expected_size}")
            shutil.copyfile(src, part)
        if dest.exists() and dest.is_dir() and not dest.is_symlink():
            shutil.rmtree(dest)
        os.replace(part, dest)
        return
    # The validator of the response the .part came from, so a resume can say
    # "only if it's still the same file" (If-Range) instead of splicing two versions.
    vfile = dest.with_name(dest.name + ".part.json")
    pos = part.stat().st_size if part.exists() else 0
    saved: dict = {}
    if pos:
        with contextlib.suppress(OSError, json.JSONDecodeError):
            saved = json.loads(vfile.read_text())
        if not isinstance(saved, dict):
            saved = {}
        if saved.get("url") not in (None, url):
            log(f"  {dest.name}: partial download came from another URL; starting over")
            pos, saved = 0, {}
        elif not (saved.get("etag") or "").strip() and not saved.get("last_modified") and not verifiable:
            # Without a validator (or a sha256 to check afterwards) nothing would
            # stop a changed file being spliced onto the old half.
            log(f"  {dest.name}: partial download can't be validated; starting over")
            pos, saved = 0, {}
        if not pos:
            part.unlink()
    if expected_size and pos > expected_size:
        log(f"  {dest.name}: partial download is larger than the file; starting over")
        part.unlink()
        pos, saved = 0, {}
    if expected_size and pos == expected_size:
        os.replace(part, dest)
        with contextlib.suppress(FileNotFoundError):
            vfile.unlink()
        return
    headers = {}
    if pos:
        headers["Range"] = f"bytes={pos}-"
        etag = saved.get("etag") or ""
        if etag and not etag.startswith("W/"):           # If-Range needs a strong validator
            headers["If-Range"] = etag
        elif saved.get("last_modified"):
            headers["If-Range"] = saved["last_modified"]
    try:
        resp = http(url, timeout=timeout, headers=headers)
    except urllib.error.HTTPError as e:
        if e.code != 416 or not pos:
            raise
        log(f"  {dest.name}: server refused the resume point; starting over")
        part.unlink()
        pos, headers = 0, {}
        resp = http(url, timeout=timeout)
    if pos and resp.status == 206:
        # A 206 must continue exactly where the .part ends; anything else
        # (a server that ignores the Range start, a proxy) would corrupt it.
        m = re.fullmatch(r"\s*bytes\s+(\d+)-(\d+)/(\d+|\*)\s*", resp.headers.get("Content-Range") or "")
        if not m or int(m.group(1)) != pos:
            resp.close()
            log(f"  {dest.name}: server answered the resume with the wrong range; starting over")
            part.unlink()
            pos, headers = 0, {}
            resp = http(url, timeout=timeout)
    with resp as r:
        if pos and r.status == 200:
            log(f"  {dest.name}: file changed upstream (or no range support); discarding partial download")
        mode = "ab" if pos and r.status == 206 else "wb"
        if mode == "wb":
            pos = 0
            atomic_write(vfile, json.dumps({"url": url, "etag": r.headers.get("ETag"),
                                            "last_modified": r.headers.get("Last-Modified")}))
        try:
            clen = int(r.headers.get("Content-Length") or 0)
        except ValueError:
            clen = 0
        total = pos + max(clen, 0)
        started, got, last = time.monotonic(), 0, 0.0
        with open(part, mode) as out:
            while chunk := r.read(1 << 20):
                if expected_size and pos + got + len(chunk) > expected_size:
                    out.close()
                    part.unlink()
                    with contextlib.suppress(FileNotFoundError):
                        vfile.unlink()
                    raise RuntimeError(f"{dest.name}: server sent more than the expected {expected_size} bytes; "
                                       f"download discarded")
                out.write(chunk)
                got += len(chunk)
                if max_rate_kbps:
                    want = got / (max_rate_kbps * 1024)
                    lag = want - (time.monotonic() - started)
                    if lag > 0:
                        time.sleep(lag)
                if time.monotonic() - last > 15:
                    last = time.monotonic()
                    pct = f" {100 * (pos + got) / total:.1f}%" if total else ""
                    log(f"  {dest.name}: {human(pos + got)}{pct}")
            out.flush()
            os.fsync(out.fileno())
    os.replace(part, dest)
    with contextlib.suppress(FileNotFoundError):
        vfile.unlink()


def remove_path(p: Path) -> None:
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(p)
    else:
        with contextlib.suppress(FileNotFoundError):
            p.unlink()


def drop_stale_parts(staging: Path, keep: str | None) -> None:
    """Partial downloads of versions we no longer want would sit on the disk forever."""
    if not staging.is_dir():
        return
    for p in staging.iterdir():
        for suffix in (".part", ".part.json"):
            if p.name.endswith(suffix) and p.name[:-len(suffix)] != keep:
                log(f"  removing stale partial download {p.name}")
                remove_path(p)


def rsync(args: list[str], cfg: Config) -> None:
    cmd = ["rsync", "--partial", "--times", "--human-readable", "--timeout=120"]
    if cfg.max_rate_kbps:
        cmd.append(f"--bwlimit={cfg.max_rate_kbps}")
    subprocess.run(cmd + args, check=True)


def transfer(p: PlanEntry, cfg: Config, staging: Path) -> Path:
    """Get the new version into staging. Returns the staged path (file or dir)."""
    r, item = p.remote, p.item
    if r is None:
        raise ValueError(f"{item.id}: nothing to transfer")
    if not safe_name(r.filename):
        # Upstream names come from listings and URLs; one ending in "/" or
        # holding "../" must not turn into a path outside staging.
        raise RuntimeError(f"{item.id}: refusing unsafe file name {r.filename!r}")
    if staging.is_symlink():
        staging.unlink()
    staging.mkdir(parents=True, exist_ok=True)
    dest = staging / r.filename
    current = installed_path(p.local, cfg)
    drop_stale_parts(staging, r.filename)

    if p.method == "zimpatch":
        d = next(d for d in r.meta["diffs"] if d["from"] == p.local["version"])
        patch = staging / Path(d["path"]).name
        http_download(d["url"], patch, cfg.max_rate_kbps)
        subprocess.run(["zimpatch", str(current), str(patch), str(dest)], check=True)
        patch.unlink()
    elif p.method == "rsync-fuzzy":
        basis = staging / current.name if current else dest
        if current and current.exists() and not basis.exists():
            os.link(current, basis)        # same filesystem: free, lets --fuzzy find it
        try:
            rsync(["--fuzzy", r.rsync_url, str(staging) + "/"], cfg)
        except subprocess.CalledProcessError:
            log("  rsync failed; falling back to HTTP")
            http_download(r.url, dest, cfg.max_rate_kbps, expected_size=r.size, verifiable=bool(r.sha256))
        finally:
            if basis.exists() and basis.name != dest.name:
                basis.unlink()
    elif p.method == "rsync-tree":
        dest = staging / f"{item.id}-{r.version}"
        link = ["--link-dest", str(current)] if current and current.is_dir() else []
        rsync(["-r", "--delete", *link, r.rsync_url, str(dest) + "/"], cfg)
    elif p.method == "git-delta":
        target = cfg.data_root / item.dir / item.id
        ref = r.meta.get("ref", "HEAD")
        pin = r.meta.get("commit") if r.meta.get("pinned") else None
        fsck = ["-c", "transfer.fsckObjects=true"]          # every object checked against its hash
        if (target / ".git").exists():
            subprocess.run(["git", *fsck, "-C", str(target), "fetch", "--depth", "1", "origin", ref], check=True)
            subprocess.run(["git", "-C", str(target), "reset", "--hard", "FETCH_HEAD"], check=True)
            if pin:
                subprocess.run(["git", "-C", str(target), "clean", "-fdx"], check=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            branch = ["--branch", ref.removeprefix("refs/tags/").removeprefix("refs/heads/")] \
                if ref != "HEAD" and pin else []
            subprocess.run(["git", *fsck, "clone", "--depth", "1", *branch, r.url, str(target)], check=True)
        if pin:
            git_checkout_ok(target, pin)
        return target
    elif p.method == "pmtiles-extract":
        if not have("pmtiles"):
            raise RuntimeError("pmtiles CLI not installed (github.com/protomaps/go-pmtiles)")
        cmd = ["pmtiles", "extract", r.url, str(dest)]
        if r.meta.get("bbox"):
            cmd.append("--bbox=" + ",".join(str(x) for x in r.meta["bbox"]))
        if r.meta.get("maxzoom"):
            cmd.append(f"--maxzoom={r.meta['maxzoom']}")
        subprocess.run(cmd, check=True)
    else:  # http, peer, copy
        http_download(r.url, dest, cfg.max_rate_kbps, expected_size=r.size, verifiable=bool(r.sha256))

    if dest.is_dir() and not dest.is_symlink() and r.origin.startswith(("peer:", "bundle:")):
        want = r.meta.get("tree_sha256")
        try:
            got = tree_sha256(dest) if want else None
        except ValueError as e:
            got = f"unverifiable ({e})"
        if not want or got != want:
            remove_path(dest)
            raise RuntimeError(f"tree hash mismatch for {r.filename}: {got} != {want} — not installed")
    if dest.is_file():
        if r.sha256:
            got = sha256_file(dest)
            if got != r.sha256:
                dest.unlink()
                raise RuntimeError(f"sha256 mismatch for {r.filename}: {got} != {r.sha256}")
        elif r.size and dest.stat().st_size != r.size and p.method != "pmtiles-extract":
            dest.unlink()                      # (left behind, it would sit in staging for ever)
            raise RuntimeError(f"size mismatch for {r.filename}")
    return dest


def installed_path(local: dict | None, cfg: Config) -> Path | None:
    if not local or not local.get("path"):
        return None
    return cfg.data_root / local["path"]


def install(p: PlanEntry, staged: Path, cfg: Config, state: State) -> None:
    """Move staged content into place, swap atomically, retire the old version."""
    item, r = p.item, p.remote
    if r is None:
        raise ValueError(f"{item.id}: nothing to install")
    target_dir = cfg.data_root / item.dir
    target_dir.mkdir(parents=True, exist_ok=True)
    old = installed_path(p.local, cfg)

    retire: list[Path] = []
    aside: Path | None = None
    if p.method == "git-delta":
        final = staged
    else:
        final = target_dir / (staged.name if staged.is_dir() else r.filename)
        if final.is_dir() and not final.is_symlink():
            # A directory can't be renamed over a non-empty one: move the old tree
            # aside, move the new one in, and delete the old one only at the end.
            aside = target_dir / f".{final.name}.old-{os.getpid()}"
            remove_path(aside)
            os.replace(final, aside)
            retire.append(aside)
        elif (final.exists() or final.is_symlink()) and staged.is_dir():
            final.unlink()
        try:
            os.replace(staged, final)            # a file replaces a file atomically
        except OSError:
            if aside:                            # put the old tree back
                os.replace(aside, final)
            raise

    def roll_back() -> None:
        """The swap happened but couldn't be recorded (disk full writing the
        ledger or the link): put the previous version back as it was, so the
        stable name, the files and the ledger keep agreeing."""
        if p.method == "git-delta":
            return
        with contextlib.suppress(OSError):
            if aside is not None:
                remove_path(final)
                os.replace(aside, final)
            elif old is None or final != old:
                remove_path(final)
            if old is not None and old.parent == target_dir and (old.exists() or old.is_symlink()):
                point_current(target_dir, item.id, old.name)
            elif old is None:
                cur = target_dir / f"{item.id}.current"
                if cur.is_symlink() and os.readlink(cur) == final.name:
                    cur.unlink()

    if p.method != "git-delta":
        try:
            point_current(target_dir, item.id, final.name)
        except BaseException:
            roll_back()
            raise

    rec = {
        "version": r.version, "comparable": r.comparable, "filename": final.name,
        "path": str(final.relative_to(cfg.data_root)), "size": r.size or path_size(final),
        "sha256": r.sha256, "installed": now_iso(), "origin": r.origin,
        "method": p.method, "meta": r.meta, "obtained": r.meta.get("obtained") or now_iso(),
    }
    cmd = hook_for(item, cfg)
    if cmd:
        # Recorded before it runs: if power drops in between, the next apply runs it.
        rec["hook"] = {"cmd": cmd, "status": "pending", "at": now_iso()}
    if old and (old.exists() or old.is_symlink()) and old != final:
        if cfg.keep_previous:
            rec["previous"] = {"version": p.local.get("version"), "path": p.local.get("path")}
        else:
            retire.append(old)
    # Record the new version first: if power drops now, the ledger points at
    # what is actually on disk, and gc / the next run clean up the leftovers.
    try:
        state.put(item.id, rec, "updated" if p.local else "installed")
    except BaseException:
        roll_back()
        raise
    for x in retire:
        remove_path(x)
    run_hook(item, cfg, state)


def point_current(target_dir: Path, item_id: str, name: str) -> None:
    """Stable `<id>.current` name for renderers that don't care about versions (maps, models)."""
    if name == f"{item_id}.current":
        return
    stable = target_dir / f"{item_id}.current"
    tmp_link = target_dir / f".{item_id}.current.tmp"
    with contextlib.suppress(FileNotFoundError):
        tmp_link.unlink()
    tmp_link.symlink_to(name)
    os.replace(tmp_link, stable)


def path_size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


SELF_UPDATE_ID = "prepper_software"


def hook_for(item: Item, cfg: Config) -> str | None:
    """[upd.hooks] keys are item ids (preferred) or kinds. An id key wins. A
    kind key covering "app" used to run the software self-update after every
    kiwix-js commit too, so a kind-keyed hook that calls self-update-hook only
    ever fires for the software item itself."""
    cmd = cfg.hooks.get(item.id)
    if cmd is None:
        cmd = cfg.hooks.get(item.kind)
        if cmd and "self-update-hook" in cmd and item.id != SELF_UPDATE_ID:
            return None
    return str(cmd) if cmd else None


def run_hook(item: Item, cfg: Config, state: State | None = None) -> bool:
    """Post-install hook (rebuild the Kiwix library, restart a renderer, self-
    update). Its outcome is kept in the ledger (rec["hook"]) so a failure shows
    in `upd status` and the hook is run again on the next apply."""
    cmd = hook_for(item, cfg)
    if not cmd:
        return True
    log(f"  hook[{item.id}]: {cmd}")
    try:
        rc = subprocess.run(cmd, shell=True, check=False).returncode  # noqa: S602 — admin-written hook from root-owned prepper.toml
    except OSError as e:
        log(f"  hook[{item.id}] could not start: {e}")
        rc = 127
    if rc:
        log(f"  hook[{item.id}] failed (exit {rc}); it will be retried on the next apply")
    rec = state.get(item.id) if state else None
    if rec is not None:
        rec["hook"] = {"cmd": cmd, "status": "ok" if rc == 0 else "failed", "rc": rc, "at": now_iso()}
        with contextlib.suppress(OSError):
            state.save()
    return rc == 0


def retry_hooks(cfg: Config, state: State, skip: set[str] = frozenset()) -> None:
    """Run again every hook left pending (power cut) or failed by an earlier run
    (`skip`: items whose hook already ran during this apply)."""
    try:
        items = {i.id: i for i in load_catalog(cfg.catalog)}
    except (OSError, tomllib.TOMLDecodeError):
        return
    for item_id, rec in sorted(state.data["items"].items()):
        h = rec.get("hook") or {}
        if item_id in items and item_id not in skip and h.get("status") in ("pending", "failed"):
            log(f"retrying {h.get('status')} hook for {item_id}")
            run_hook(items[item_id], cfg, state)


# ─────────────────────────────── commands ────────────────────────────────────

def print_plan(plan: list[PlanEntry]) -> None:
    icons = {"up-to-date": "✓", "update": "↑", "install": "+", "manual": "•",
             "offline": "?", "error": "!"}
    w = max((len(p.item.id) for p in plan), default=10)
    for p in plan:
        lv = (p.local or {}).get("version", "—")
        rv = p.remote.version if p.remote else "—"
        extra = f"  {p.method} ~{human(p.download_bytes)}" if p.status in ("update", "install") else ""
        print(f" {icons.get(p.status, ' ')} {p.item.id:<{w}}  {str(lv)[:16]:>16} → {str(rv)[:16]:<16}"
              f"{extra}  {p.note}")
    todo = [p for p in plan if p.status in ("update", "install")]
    print(f"\n {len(todo)} to fetch, ~{human(sum(p.download_bytes or 0 for p in todo))}; "
          f"{sum(p.status == 'up-to-date' for p in plan)} current; "
          f"{sum(p.status in ('offline', 'error') for p in plan)} unreachable/errored")


EX_TEMPFAIL = 75      # sysexits.h: "try again later" — the lock is busy, or nothing could be reached


def nothing_reachable(plan: list[PlanEntry]) -> bool:
    """Every item that has a source was unreachable: nothing could be checked
    (no network yet, DNS down). A plan where *some* sources answered was evaluated."""
    sourced = [p for p in plan if p.status != "manual"]
    return bool(sourced) and all(p.status == "offline" for p in sourced)


def cmd_check(cfg: Config, args) -> int:
    try:
        with run_lock(cfg):                    # (load_offer, write_report and the marks file are shared with apply)
            plan = build_plan(cfg, offline=args.offline, only=set(args.only or []) or None)
            path = write_report(cfg, plan)
    except Busy as e:
        log(str(e))
        return EX_TEMPFAIL
    if args.json:
        print(path.read_text())
    else:
        print_plan(plan)
    return 0


def apply_plan(cfg: Config, plan: list[PlanEntry], dry_run: bool) -> int:
    state = State(cfg.state_file)
    todo = [p for p in plan if p.status in ("update", "install")]
    # Smallest first: the most items become current soonest if power or link drops.
    todo.sort(key=lambda p: p.download_bytes or 0)
    failures = 0
    installed: set[str] = set()
    for p in todo:
        need = (p.remote.size or 0) + int(cfg.reserve_gb * 1e9)
        free = free_bytes(cfg.data_root)
        label = f"{p.item.id} {(p.local or {}).get('version', 'new')} → {p.remote.version} via {p.method}"
        if p.method not in ("git-delta",) and need > free:
            log(f"SKIP {label}: needs {human(need)} incl. reserve, {human(free)} free")
            failures += 1
            continue
        if dry_run:
            log(f"would {label} (~{human(p.download_bytes)})")
            continue
        log(f"BEGIN {label}")
        staging = cfg.data_root / ".staging" / p.item.id
        try:
            staged = transfer(p, cfg, staging)
            install(p, staged, cfg, state)
            installed.add(p.item.id)
            log(f"DONE  {p.item.id}")
        except Exception as e:  # noqa: BLE001 — keep going with the rest
            failures += 1
            log(f"FAIL  {p.item.id}: {type(e).__name__}: {e}  (partial download kept for resume)")
        finally:
            with contextlib.suppress(OSError):
                staging.rmdir()
    if not dry_run:
        retry_hooks(cfg, state, skip=installed)
        write_manifest(cfg)
    return 1 if failures else 0


def cmd_apply(cfg: Config, args) -> int:
    """0: the plan was evaluated and every transfer/install succeeded (or there
    was nothing to do); 1: something failed; 75: try again later — the update
    lock is busy, or no source at all could be reached."""
    if not args.now and not in_window(cfg.window):
        log(f"outside update window {cfg.window}; use --now to override")
        return 0
    try:
        with run_lock(cfg):
            if not args.dry_run:
                prune_withdrawn(cfg, State(cfg.state_file), load_catalog(cfg.catalog), load_personal_record(cfg))
            plan = build_plan(cfg, offline=args.offline, only=set(args.only or []) or None)
            write_report(cfg, plan)
            rc = apply_plan(cfg, plan, args.dry_run)
    except Busy as e:
        log(str(e))
        return EX_TEMPFAIL
    if nothing_reachable(plan):
        log("no source could be reached (offline?) — nothing was checked; try again later")
        return EX_TEMPFAIL
    return rc


def cmd_import(cfg: Config, args) -> int:
    try:
        with run_lock(cfg):
            offers = load_offer(str(Path(args.src).resolve()), cfg)
            if not offers:
                log("nothing importable (missing, unsigned, or untrusted bundle)")
                return 1
            if not args.dry_run:
                prune_withdrawn(cfg, State(cfg.state_file), load_catalog(cfg.catalog), load_personal_record(cfg))
            plan = build_plan(cfg, offline=True, only=set(args.only or []) or None, offers=offers)
            print_plan(plan)
            return apply_plan(cfg, plan, args.dry_run)
    except Busy as e:
        log(str(e))
        return EX_TEMPFAIL


def content_hashes(cfg: Config, state: State, item_id: str, rec: dict, src: Path) -> dict | None:
    """The hash a receiver will insist on: sha256 for a file (computed once and
    kept in the ledger when upstream gave none — pmtiles, plain http items),
    tree_sha256 for a directory. None if the item can't be vouched for."""
    if src.is_file():
        if not rec.get("sha256"):
            log(f"  hashing {item_id} (no sha256 from upstream; done once)")
            rec = {**rec, "sha256": sha256_file(src)}
            state.data["items"][item_id] = rec
            state.save()
        return {"sha256": rec["sha256"]}
    try:
        return {"tree_sha256": tree_sha256(src)}
    except (ValueError, OSError) as e:
        log(f"  {item_id}: can't vouch for this tree ({e}); not shared")
        return None


def cmd_export(cfg: Config, args) -> int:
    """Copy installed content + signed manifest to DEST (a USB drive)."""
    try:
        with run_lock(cfg):
            return _export(cfg, args)
    except Busy as e:
        log(str(e))
        return EX_TEMPFAIL


def _export(cfg: Config, args) -> int:
    state = State(cfg.state_file)
    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    only = set(args.only or [])
    items = []
    for item_id, rec in sorted(state.data["items"].items()):
        if only and item_id not in only:
            continue
        src = cfg.data_root / rec["path"]
        if not src.exists():
            continue
        hashes = content_hashes(cfg, state, item_id, rec, src)
        if hashes is None:
            continue
        rec = state.get(item_id)
        rel = Path("files") / rec["path"]
        out = dest / rel
        size = path_size(src)
        if src.is_dir():
            # A tree must arrive exactly as hashed: leftovers from an earlier
            # export in the same folder would make it fail verification.
            same = False
            with contextlib.suppress(ValueError, OSError):
                same = out.is_dir() and tree_sha256(out) == hashes["tree_sha256"]
        else:
            same = out.is_file() and out.stat().st_size == size and sha256_file(out) == hashes["sha256"]
        if not same:
            if free_bytes(dest) < size:
                log(f"skip {item_id}: not enough room on {dest}")
                continue
            log(f"copy {item_id} ({human(size)})")
            out.parent.mkdir(parents=True, exist_ok=True)
            if out.exists() or out.is_symlink():
                remove_path(out)
            if src.is_dir():
                shutil.copytree(src, out)
            else:
                shutil.copyfile(src, out)
        entry = {k: rec.get(k) for k in ("version", "filename", "size", "comparable", "obtained")}
        entry.update(id=item_id, path=str(rel), **hashes)
        diffs = []
        for d in (args.diffs or []):                 # --diffs OLDVERSION for zimdiff patches
            prev = rec.get("previous")
            if prev and prev["version"] == d and have("zimdiff") and src.is_file():
                patch = dest / "files" / "diffs" / f"{item_id}_{d}_{rec['version']}.zimdiff"
                patch.parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(["zimdiff", str(cfg.data_root / prev["path"]), str(src), str(patch)],
                               check=True)
                diffs.append({"from": d, "path": str(patch.relative_to(dest)),
                              "size": patch.stat().st_size})
        if diffs:
            entry["diffs"] = diffs
        items.append(entry)
    manifest = dest / "manifest.json"
    atomic_write(manifest, json.dumps({"disk_id": cfg.disk_id, "generated": now_iso(),
                                       "format": 1, "items": items}, indent=2))
    signed = sign_file(manifest, cfg)
    log(f"bundle written: {len(items)} items, {'signed' if signed else 'UNSIGNED'} → {dest}")
    return 0


def write_manifest(cfg: Config) -> Path:
    """Publish what this disk holds so other disks on the LAN can pull from it.
    nginx serves {data_root}/sync/ at http://<disk>/sync/ — files are symlinks.
    Every entry carries a sha256 (computed once if upstream gave none)."""
    state = State(cfg.state_file)
    sync = cfg.data_root / "sync"
    files = sync / "files"
    if files.exists() or files.is_symlink():
        remove_path(files)
    files.mkdir(parents=True)
    items = []
    for item_id, rec in sorted(state.data["items"].items()):
        src = cfg.data_root / rec["path"]
        if not src.is_file() or not safe_name(rec.get("filename")):
            continue                            # trees travel in bundles (with a tree hash), not here
        link = files / rec["filename"]
        if link.is_symlink():
            log(f"manifest: {item_id} shares a file name with another item; not published")
            continue
        hashes = content_hashes(cfg, state, item_id, rec, src)
        rec = state.get(item_id)
        link.symlink_to(src)
        entry = {k: rec.get(k) for k in ("version", "filename", "size", "comparable", "obtained")}
        entry.update(id=item_id, path=f"files/{rec['filename']}", **(hashes or {}))
        items.append(entry)
    manifest = sync / "manifest.json"
    atomic_write(manifest, json.dumps({"disk_id": cfg.disk_id, "generated": now_iso(),
                                       "format": 1, "items": items}, indent=2))
    sign_file(manifest, cfg)
    return manifest


def cmd_status(cfg: Config, args) -> int:
    state = State(cfg.state_file)
    items = {i.id: i for i in load_catalog(cfg.catalog)}
    total = 0
    for item_id, rec in sorted(state.data["items"].items()):
        total += rec.get("size") or 0
        title = items[item_id].title if item_id in items else "(not in catalog)"
        print(f" {item_id:<38} {str(rec.get('version'))[:16]:<16} {human(rec.get('size')):>9}  {title}")
        h = rec.get("hook") or {}
        if h.get("status") in ("pending", "failed"):
            print(f"   ! post-install hook {h['status']}" + (f" (exit {h.get('rc')})" if "rc" in h else "")
                  + f" at {h.get('at')}: {h.get('cmd')}  — retried on the next apply")
    du = shutil.disk_usage(cfg.data_root) if cfg.data_root.exists() else None
    held = held_personal(list(items.values()), cfg)
    if cfg.provisioning == "sale" or held:
        print(f"\n provisioning: {cfg.provisioning}"
              + (f"; {len(held)} personal-use items wait for the owner's acceptance: "
                 + ", ".join(i.id for i in held) if held else ""))
    print(f"\n {len(state.data['items'])} items, {human(total)}"
          + (f"; {human(du.free)} free of {human(du.total)}" if du else ""))
    return 0


def cmd_gc(cfg: Config, args) -> int:
    """Remove leftovers: staging dirs of items no longer wanted, partial downloads
    of versions that are no longer the newest (per the last check) or already
    installed, and old trees left aside by an interrupted install.

    Holds the update lock: run beside `upd apply`, it used to delete the
    staging dir of an item between its download finishing and its install.
    Never follows a symlink out of data_root: a link found in staging is
    removed as a link, never descended into."""
    try:
        with run_lock(cfg):
            return _gc(cfg)
    except Busy:
        log("gc: an update is running; nothing removed (try again later)")
        return 0


def _gc(cfg: Config) -> int:
    staging = cfg.data_root / ".staging"
    if staging.is_symlink():
        log(f"removing {staging} (a symlink; staging is always a real directory)")
        staging.unlink()
    state = State(cfg.state_file)
    report = {}
    with contextlib.suppress(OSError, json.JSONDecodeError):
        report = {i["id"]: i for i in json.loads((cfg.report_dir / "last-check.json").read_text()).get("items", [])}
    try:
        wanted_ids = {i.id for i in wanted(load_catalog(cfg.catalog), cfg)}
    except (OSError, tomllib.TOMLDecodeError):
        wanted_ids = None
    if staging.exists():
        for p in staging.iterdir():
            if p.is_symlink() or not p.is_dir() or (wanted_ids is not None and p.name not in wanted_ids):
                log(f"removing {p}")
                remove_path(p)                 # a symlink: only the link goes
                continue
            rec = state.get(p.name) or {}
            newest = (report.get(p.name) or {}).get("remote_filename")
            for part in p.glob("*.part"):
                if part.is_symlink():
                    log(f"removing {p.name}/{part.name} (a symlink)")
                    part.unlink()
                    continue
                fn = part.name.removesuffix(".part")
                if fn == rec.get("filename") or (newest and fn != newest):
                    log(f"removing stale partial download {p.name}/{part.name}")
                    remove_path(part)
                    remove_path(part.with_name(part.name + ".json"))
                else:
                    log(f"keeping partial download for resume: {p.name}/{part.name}")
            if not any(p.glob("*.part")):
                log(f"removing {p}")
                shutil.rmtree(p, ignore_errors=True)
    if cfg.data_root.exists():
        root = cfg.data_root.resolve()
        for kind_dir in cfg.data_root.iterdir():
            if kind_dir.is_dir() and not kind_dir.name.startswith("."):
                kd = kind_dir.resolve()
                if not (kd == root or root in kd.parents):
                    continue                   # a kind dir linked to another disk: not ours to sweep
                for aside in kind_dir.glob(".*.old-*"):
                    log(f"removing {aside} (left by an interrupted install)")
                    remove_path(aside)
    return 0


def cmd_adopt(cfg: Config, args) -> int:
    """Register content you copied in by hand (manual catalog items) so the
    launcher shows it and peers/bundles can share it."""
    with run_lock(cfg):
        return _adopt(cfg, args)


def _adopt(cfg: Config, args) -> int:
    items = {i.id: i for i in load_catalog(cfg.catalog)}
    item = items.get(args.id)
    if not item:
        raise SystemExit(f"upd: no catalog item {args.id!r}")
    if not personal_allowed(item, cfg, load_personal_record(cfg)):
        raise SystemExit(f"upd: {item.id} is licensed for personal use only ({item.license}). This Badger was "
                         "built for sale: its owner must accept the licence first (launcher → Settings)")
    target = Path(args.path) if args.path else cfg.data_root / item.dir / item.id
    if not target.is_absolute():
        target = cfg.data_root / target
    if not target.exists():
        raise SystemExit(f"upd: copy the files to {target} first")
    try:
        # Resolved, so "data/../etc" or a symlink out of data_root is refused.
        rel = target.resolve().relative_to(cfg.data_root.resolve())
        if not rel.parts:
            raise ValueError("data_root itself")
    except ValueError:
        raise SystemExit(f"upd: {target} must be inside {cfg.data_root}") from None
    state = State(cfg.state_file)
    version = dt.date.today().isoformat()
    if target.parent != cfg.data_root:
        point_current(target.parent, item.id, target.name)      # same stable name install() makes
    state.put(item.id, {"version": version, "comparable": True, "filename": target.name, "path": str(rel),
                        "size": path_size(target), "sha256": sha256_file(target) if target.is_file() else None,
                        "installed": now_iso(), "origin": "manual", "method": "adopt", "meta": {},
                        "obtained": now_iso()}, "adopted")
    run_hook(item, cfg, state)
    write_manifest(cfg)
    print(f"adopted {item.id}: {rel} ({human(path_size(target))})")
    return 0


def cmd_manifest(cfg: Config, args) -> int:
    with run_lock(cfg):
        print(write_manifest(cfg))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="upd", description=__doc__.split("\n\n")[0])
    ap.add_argument("-c", "--config", default=os.environ.get("PREPPER_CONFIG", DEFAULT_CONFIG))
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("check"); s.add_argument("--json", action="store_true")
    s.add_argument("--offline", action="store_true"); s.add_argument("--only", nargs="*")
    s = sub.add_parser("apply"); s.add_argument("--only", nargs="*")
    s.add_argument("--now", action="store_true"); s.add_argument("--dry-run", action="store_true")
    s.add_argument("--offline", action="store_true", help="peers only, no internet")
    s = sub.add_parser("import"); s.add_argument("src"); s.add_argument("--only", nargs="*")
    s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("export"); s.add_argument("dest"); s.add_argument("--only", nargs="*")
    s.add_argument("--diffs", nargs="*", help="also write zimdiff patches from these old versions")
    s = sub.add_parser("adopt", help="register hand-copied content for a manual item")
    s.add_argument("id"); s.add_argument("path", nargs="?")
    sub.add_parser("status"); sub.add_parser("gc"); sub.add_parser("manifest")
    args = ap.parse_args(argv)
    cfg = Config.load(args.config)
    try:
        return {"check": cmd_check, "apply": cmd_apply, "import": cmd_import, "export": cmd_export,
                "status": cmd_status, "gc": cmd_gc, "manifest": cmd_manifest, "adopt": cmd_adopt}[args.cmd](cfg, args)
    except OSError as e:
        # A full disk (or a read-only / vanished one) is a state, not a bug: one clear line, no traceback.
        # Every write goes through atomic_write/temp files, so nothing half-written is left behind.
        if e.errno in (errno.ENOSPC, errno.EDQUOT):
            log(f"error: the disk is full ({e.filename or 'no space left'}). Free some space and run it again.")
            return 3
        if e.errno == errno.EROFS:
            log(f"error: the disk is read-only ({e.filename or e}). Check the drive and run it again.")
            return 3
        raise


if __name__ == "__main__":
    sys.exit(main())
