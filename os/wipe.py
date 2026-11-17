#!/usr/bin/env python3
"""
wipe — the Badger's PANIC WIPE and HANDOVER / RESALE RESET (run as root by prepperctl).

  wipe.py --root / --conf /etc/prepper/prepper.toml --ctl /opt/prepper/bin/prepperctl \
          --mode wipe|handover --trigger admin|duress|panel|panel-duress|deadman|cli [--sale]
  wipe.py ... --resume [--boot]        finish a wipe cut short by a power cut, or its deferred part
  wipe.py ... deadman-check|deadman-status|deadman-arm DAYS|deadman-confirm CODE|deadman-off
  wipe.py verify-pin                   stdin: stored hash, then a secret → exit 0 if they match

Prime directive: the Badger must never be turned against its owners. A raider holding the box must
not get the family's roster, boards, supplies and cache locations, keys, or the households it heard
on the mesh — and the box must still look like a plain offline library afterwards (the "decoy").

CRYPTO-ERASE FIRST. On flash storage (NVMe, SD) overwriting or deleting a file does not reliably
erase it: wear levelling keeps old copies in blocks the OS can't address. So the order is:

  1. destroy the small KEY files first (sealed-record key wraps, the vault's wrapped key, the
     family radio link, the panel key/lock, the device signing key + its passphrase, the old Wi-Fi
     password kept for a pending change). Sub-second. Whatever was encrypted with them is
     unreadable from then on, even from an image of the raw flash taken afterwards;
  2. ask the mesh bridge to put NEW random keys on every private channel of the radio (the radio
     holds its own copy of the keys, which no file deletion on the Pi reaches);
  3. remove the family data (members, boards, aid log, supplies, skills, missing, sealed records,
     mesh peers/nodes/telemetry/pending, the vault's blobs), deny-by-default: everything in the
     portal's folder goes except a short keep-list (library state, harmless feature caches);
  4. uplink Wi-Fi passwords and NetworkManager uplink profiles, logs, the journal, DHCP leases;
  5. (deferred for a duress wipe, see below) the TLS key → a fresh certificate, a new hotspot
     password shown only on the front panel (or Wi-Fi off), and a restart of the launcher, which
     drops keys and sessions held in its memory.

Every step is idempotent, keeps going on errors, and is recorded in a tiny receipt kept in RAM
only (/run/prepper/wipe-receipt.json, root 0600, gone at power-off). A marker in
/var/lib/prepper/wipe-pending.json (no secrets) lets `prepperctl wipe-resume` (at boot) finish a
wipe a power cut interrupted; it is removed when the wipe is done.

DURESS (the duress PIN, the duress passphrase, the panel duress code) must look like nothing happened,
and people come first. What it does is the owner's choice ([security] duress_action, or the admin page's
choice in portal-settings.json "duress_action"):
  * "lock" (DEFAULT, recoverable): nothing is destroyed. The sealed records and the vault get a HARD LOCK
    (a flag, no secret): the passphrases alone no longer open them — only the recovery keys, after which
    the passphrases work again. No restart, no Wi-Fi change, no posture change, no receipt, no marker.
    The launcher shows the made-up household (software/common/decoy.py) to every session until the owner
    types the recovery key.
  * "wipe": the panic wipe below. Steps that a person standing there would notice (Wi-Fi password change
    kicking phones off, the launcher restarting, https going away) wait `[wipe] duress_delay_min` (default
    10) minutes; the rest runs at once. The launcher shows the made-up household meanwhile, and the wipe
    puts that same made-up household in place of the family's data, so the box never looks emptied.

HANDOVER (resale): everything the wipe does, never deferred, plus factory defaults: a new neutral
hotspot name and password (the builder's rules), no admin PIN (the new owner sets one), no duress
PIN, dead-man off, no peers or trusted Badgers, a new signing identity, home position removed,
and on a unit built for sale (or with --sale) personal-use content withdrawn and removed.

What survives, and the limits, are in docs/SECURITY.md → "Panic wipe".
Standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib.util
import inspect
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

TRIGGERS = ("admin", "duress", "panel", "panel-duress", "deadman", "cli")
DURESS_TRIGGERS = ("duress", "panel-duress")
PANEL_TRIGGERS = ("panel", "panel-duress")
WIFI_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"          # no look-alikes (the same as wifi-rotate)
SSID_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"         # builder/prepper-build neutral_ssid()
PORTAL_DIR = "/var/lib/prepper/portal"
ALLOWED_ROOTS = ("/var/lib/prepper/", "/run/prepper/", "/etc/prepper/")   # key paths ([wipe] key_paths)
DATA_ROOTS = ("/var/lib/prepper/", "/run/prepper/")                       # the launcher's data files
# Where the sealed-records module keeps its wrapped private keys. sealed.destroy_keys() is the
# authority (called first); these are removed as well, whatever the module says, plus [wipe] key_paths.
SEALED_KEY_PATHS = ("/etc/prepper/sealed", "/etc/prepper/keys/sealed", "/var/lib/prepper/sealed",
                    "/var/lib/prepper/portal/sealed", "/var/lib/prepper/portal/keys")
DEFAULT_KEEP_FEATURES = ("forecast", "satellites")       # caches with nothing about the family in them
# Kept in the portal folder by a panic wipe: radio silence (a wipe never makes the Badger LOUDER). The
# posture is rewritten instead: Lockdown after a panic wipe, Guarded (the default) after a handover reset.
KEEP_PORTAL = ("posture.json", "mesh-silent", "decoy.json")     # (decoy.json: harmless by design, kept ready)
# What the mesh bridge keeps in RAM (/run/prepper, or [portal] run_dir): positions, Badgers heard, telemetry,
# the family and circle links (= keys), the radio's Bluetooth PIN, circle traffic, family radio lines not yet
# sealed onto the board by the launcher.
RUN_MESH_FILES = ("mesh-nodes.json", "mesh-telemetry.jsonl", "mesh-channel.json", "mesh-bt-pin.json", "mesh-peers.json",
                  "mesh-circle.json", "circle-status.json", "circle-ask.json", "family-inbox.jsonl")
RUN_KEY_FILES = ("mesh-channel.json", "mesh-bt-pin.json", "mesh-circle.json")
SCRUB_MAX_FILE = 4 * 1024 * 1024                          # overwrite files up to this size before unlinking …
SCRUB_BUDGET = 64 * 1024 * 1024                           # … and at most this much in one wipe (speed)
MESH_FRESH_S = 30.0                                       # the bridge touches /run/prepper/mesh.ok every second
DEADMAN_MIN_DAYS, DEADMAN_MAX_DAYS = 3, 365
DEADMAN_STEP_MAX = 2 * 3600 + 600                         # one timer run counts at most this much idle time
DEADMAN_WARN_S = 48 * 3600


# ───────────────────────────── small helpers ─────────────────────────────────

def now() -> float:
    return time.time()


def _num(v, default: float, lo: float, hi: float) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    if x != x:
        return default
    return max(lo, min(hi, x))


def random_passphrase() -> str:
    s = "".join(secrets.choice(WIFI_ALPHABET) for _ in range(12))
    return f"{s[:4]}-{s[4:8]}-{s[8:]}"


def neutral_ssid() -> str:
    return "Home-" + "".join(secrets.choice(SSID_ALPHABET) for _ in range(4))


def verify_pin(pin: str, stored) -> bool:
    """The portal's verify_pin (scrypt, or the older PBKDF2), mirrored so prepperctl can check a new
    duress PIN against the admin PIN without starting the portal."""
    if not isinstance(pin, str) or not isinstance(stored, str) or not pin or not stored:
        return False
    parts = stored.split("$")
    try:
        if parts[0] == "scrypt" and len(parts) == 6:
            n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
            if not (2 ** 10 <= n <= 2 ** 20 and n & (n - 1) == 0 and 1 <= r <= 32 and 1 <= p <= 4) \
                    or 128 * n * r > 256 * 1024 * 1024:
                return False
            got = hashlib.scrypt(pin.encode("utf-8", "surrogatepass"), salt=parts[4].encode(), n=n, r=r, p=p,
                                 dklen=32, maxmem=256 * 1024 * 1024 + 1024 * 1024).hex()
            return hmac.compare_digest(got, parts[5])
        if parts[0] == "pbkdf2" and len(parts) == 3:
            got = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8", "surrogatepass"), parts[1].encode(), 200_000).hex()
            return hmac.compare_digest(f"pbkdf2${parts[1]}${got}", stored)
    except (ValueError, MemoryError):
        return False
    return False


# ── prepper.toml text edits (only inside the named [section]; written through conf_write) ──

def _section(text: str, name: str):
    return re.search(r"(?ms)^\[" + re.escape(name) + r"\][ \t]*(?:#[^\n]*)?\n(.*?)(?=^\[|\Z)", text)


def _value_end(body: str, start: int) -> int:
    """End of a TOML value starting at body[start:] — follows brackets/braces across lines (arrays
    of inline tables, as the builder writes uplink_wifi), honours quoted strings."""
    depth, i, q = 0, start, None
    while i < len(body):
        c = body[i]
        if q:
            if c == "\\" and q == '"':
                i += 2
                continue
            if c == q:
                q = None
        elif c in "\"'":
            q = c
        elif c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
        elif c == "#" and depth == 0:
            nl = body.find("\n", i)
            return len(body) if nl < 0 else nl
        elif c == "\n" and depth <= 0:
            return i
        i += 1
    return len(body)


def set_key(text: str, section: str, key: str, value: str | None) -> str:
    """value = a TOML literal ('"x"', '[]', '0'); None removes the key."""
    m = _section(text, section)
    line = None if value is None else f"{key} = {value}"
    if not m:
        if line is None:
            return text
        return text.rstrip("\n") + f"\n\n[{section}]\n{line}\n"
    body = m.group(1)
    km = re.search(r"(?m)^" + re.escape(key) + r"[ \t]*=[ \t]*", body)
    if km:
        end = _value_end(body, km.end())
        if line is None:
            nl = end + 1 if end < len(body) and body[end] == "\n" else end
            body = body[:km.start()] + body[nl:]
        else:
            body = body[:km.start()] + line + body[end:]
    elif line is not None:
        body = line + "\n" + body
    return text[:m.start(1)] + body + text[m.end(1):]


def strip_wifis(text: str) -> str:
    """Remove a `wifis:` block (netplan / cloud-init network-config YAML) — the uplink Wi-Fi password
    the builder may have put there. Indentation-based: the key line and everything indented deeper."""
    out, skip = [], None
    for ln in text.splitlines(keepends=True):
        body = ln.rstrip("\r\n")
        ind = len(body) - len(body.lstrip(" "))
        if skip is not None:
            if not body.strip() or ind > skip:
                continue
            skip = None
        if re.match(r"^\s*wifis:\s*(#.*)?$", body):
            skip = ind
            continue
        out.append(ln)
    return "".join(out)


# ───────────────────────────── the wipe ──────────────────────────────────────

class Wipe:
    def __init__(self, root: str, conf: str, ctl: str, mode: str = "wipe", trigger: str = "cli",
                 sale: bool = False, clock=now, sleep=time.sleep):
        self.root = Path(root or "/")
        self.conf = Path(conf)
        self.ctl = ctl
        self.mode = mode if mode in ("wipe", "handover") else "wipe"
        self.trigger = trigger if trigger in TRIGGERS else "cli"
        self.sale = sale
        self.clock, self.sleep = clock, sleep
        self.steps: dict[str, str] = {}
        self.notes: list[str] = []
        self.errors = 0
        self.scrubbed = 0
        self.started = clock()
        self.radio = "not asked"
        self.mesh_req_id = None
        self.mesh_req_at = 0.0
        self.mesh_was_running = False
        self.action_override = None                       # (resume: a marker exists only for a wipe)
        self.reload()
        self.receipt_path = self.p("/run/prepper/wipe-receipt.json")
        self.marker_path = self.p("/var/lib/prepper/wipe-pending.json")

    def reload(self) -> None:
        try:
            self.cfg = tomllib.loads(self.conf.read_text())
        except (OSError, ValueError):
            self.cfg = {}
            self.notes.append("prepper.toml could not be read: defaults used")
        w = self.cfg.get("wipe", {})
        self.w = w if isinstance(w, dict) else {}
        portal = self.cfg.get("portal", {})
        self.portal = portal if isinstance(portal, dict) else {}
        net = self.cfg.get("network", {})
        self.net = net if isinstance(net, dict) else {}

    # ── settings ([wipe] in prepper.toml; every key optional) ──
    @property
    def duress(self) -> bool:
        return self.trigger in DURESS_TRIGGERS and self.mode == "wipe"

    def opt(self, key: str, choices: tuple, default: str) -> str:
        v = self.w.get(key, default)
        return v if v in choices else default

    @property
    def wifi_mode(self) -> str:
        return "new-name" if self.mode == "handover" else self.opt("wifi", ("new-password", "off", "keep"), "new-password")

    @property
    def radio_mode(self) -> str:
        v = self.opt("radio", ("rekey", "factory-reset", "leave"), "rekey")
        return "factory-reset" if self.mode == "handover" and v != "leave" else v

    @property
    def duress_action(self) -> str:
        """"lock" (default) or "wipe" — software/common/decoy.py duress_action() reads the same two places."""
        if self.action_override in ("lock", "wipe"):
            return self.action_override
        try:
            s = json.loads(self.portal_files()["settings_file"].read_text())
            v = s.get("duress_action") if isinstance(s, dict) else None
            if v in ("lock", "wipe"):
                return v
        except (OSError, ValueError, RecursionError):
            pass
        sec = self.cfg.get("security") if isinstance(self.cfg.get("security"), dict) else {}
        return sec.get("duress_action") if sec.get("duress_action") in ("lock", "wipe") else "lock"

    @property
    def mesh_after(self) -> str:
        return "keep" if self.mode == "handover" else self.opt("mesh_after", ("off", "keep"), "off")

    @property
    def mesh_wait(self) -> float:
        return _num(self.w.get("mesh_wait_s", 8), 8.0, 0.0, 30.0)

    @property
    def duress_delay(self) -> float:
        return _num(self.w.get("duress_delay_min", 10), 10.0, 1.0, 240.0) * 60

    @property
    def keep_features(self) -> set[str]:
        v = self.w.get("keep_features", list(DEFAULT_KEEP_FEATURES))
        if not isinstance(v, list):
            v = list(DEFAULT_KEEP_FEATURES)
        return {x for x in v if isinstance(x, str) and re.fullmatch(r"[a-z0-9_-]{1,40}", x) and x != "vault"}

    # ── paths ──
    def p(self, path: str) -> Path:
        return self.root / str(path).lstrip("/")

    def cfgpath(self, key: str, default: str) -> Path:
        """A [portal] path, inside the Badger's own folders only (a typo must never aim the wipe at /)."""
        v = self.portal.get(key)
        if isinstance(v, str) and os.path.isabs(v) and ".." not in Path(v).parts \
                and any(v.startswith(r) for r in DATA_ROOTS):
            return self.p(v)
        return self.p(default)

    def portal_files(self) -> dict[str, Path]:
        """Every file the launcher and the mesh bridge keep, at its configured place ([portal] overrides),
        else its default: radio state in RAM (/run/prepper, or [portal] run_dir), the rest in the portal
        folder. Old copies of the radio files left in the portal folder go with the folder sweep."""
        d = PORTAL_DIR
        names = {"members_file": "members.json", "board_file": "board.jsonl", "family_board_file": "family-board.jsonl",
                 "mesh_outbox": "mesh-outbox.jsonl", "mesh_pending": "mesh-pending.json",
                 "mesh_pair_file": "mesh-pair.json", "mesh_rotate_request": "mesh-rotate.json",
                 "mesh_rotate_result": "mesh-rotate-result.json", "settings_file": "portal-settings.json",
                 "panel_lock_arm": "panel-lock-arm.json", "posture_file": "posture.json",
                 "mesh_radio_request": "mesh-radio-req.json", "mesh_radio_result": "mesh-radio-res.json"}
        run = {"mesh_nodes": "mesh-nodes.json", "mesh_peers": "mesh-peers.json", "mesh_telemetry": "mesh-telemetry.jsonl",
               "mesh_channel_link": "mesh-channel.json", "mesh_bt_pin_file": "mesh-bt-pin.json",
               "mesh_circle_link": "mesh-circle.json", "circle_status_file": "circle-status.json",
               "circle_ask_file": "circle-ask.json", "family_inbox": "family-inbox.jsonl"}
        v = self.portal.get("run_dir")
        rd = v.rstrip("/") if isinstance(v, str) and os.path.isabs(v) and ".." not in Path(v).parts \
            and any((v.rstrip("/") + "/").startswith(r) for r in DATA_ROOTS) else "/run/prepper"
        out = {k: self.cfgpath(k, f"{d}/{v}") for k, v in names.items()}
        out.update({k: self.cfgpath(k, f"{rd}/{v}") for k, v in run.items()})
        return out

    def run_dir(self) -> Path:
        return self.cfgpath("run_dir", "/run/prepper")

    def scrub_run_mesh(self, names=RUN_MESH_FILES) -> int:
        """The radio-state files in RAM ([portal] run_dir and the default /run/prepper) and any older copy
        in the portal folder (from a bridge before radio state moved to RAM)."""
        dirs = {self.run_dir(), self.p("/run/prepper"), self.p(PORTAL_DIR)}
        return sum(bool(self.scrub(d / n)) for d in sorted(dirs) for n in names)

    def features_dir(self) -> Path:
        return self.cfgpath("features_data", f"{PORTAL_DIR}/features")

    def personal_file(self) -> Path:
        upd = self.cfg.get("upd", {}) if isinstance(self.cfg.get("upd"), dict) else {}
        v = upd.get("personal_file")
        if isinstance(v, str) and os.path.isabs(v) and any(v.startswith(r) for r in ALLOWED_ROOTS):
            return self.p(v)
        return self.p(f"{PORTAL_DIR}/personal-content.json")

    # ── file removal ──
    def scrub(self, path: Path) -> bool:
        """Overwrite a small file with random bytes, fsync, unlink (on flash the overwrite is best effort —
        the crypto-erase is what counts). A symlink: only the link goes. A directory: everything in it."""
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return False
        try:                                              # never the configuration itself (or a folder holding it)
            ap, conf = os.path.abspath(path), os.path.abspath(self.conf)
            if ap == conf or conf.startswith(ap.rstrip(os.sep) + os.sep):
                self.notes.append(f"refused to remove {path} (holds prepper.toml)")
                return False
        except (OSError, ValueError):
            return False
        if stat.S_ISDIR(st.st_mode):
            for dirpath, dirnames, filenames in os.walk(path, topdown=False, followlinks=False):
                for f in filenames:
                    self.scrub(Path(dirpath) / f)
                for d in dirnames:
                    dp = Path(dirpath) / d
                    if dp.is_symlink():
                        dp.unlink()
                    else:
                        try:
                            os.rmdir(dp)
                        except OSError:
                            shutil.rmtree(dp, ignore_errors=True)
            try:
                os.rmdir(path)
            except OSError:
                shutil.rmtree(path, ignore_errors=True)
            return True
        if stat.S_ISREG(st.st_mode) and 0 < st.st_size <= SCRUB_MAX_FILE and self.scrubbed + st.st_size <= SCRUB_BUDGET:
            try:
                fd = os.open(path, os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0))
                try:
                    left = st.st_size
                    while left > 0:
                        n = min(left, 1 << 20)
                        os.write(fd, os.urandom(n))
                        left -= n
                    os.fsync(fd)
                finally:
                    os.close(fd)
                self.scrubbed += st.st_size
            except OSError:
                pass                                      # (read-only, odd file): unlinking still happens
        os.unlink(path)
        return True

    def scrub_glob(self, folder: Path, pattern: str) -> int:
        n = 0
        try:
            for f in sorted(folder.glob(pattern)):
                n += bool(self.scrub(f))
        except OSError:
            pass
        return n

    # ── commands ──
    def env(self) -> dict:
        e = dict(os.environ)
        e.pop("SUDO_USER", None)                         # verbs we call run as root, not "from the portal"
        return e

    def run(self, *argv: str, timeout: float = 20, input: str | None = None) -> int:  # noqa: A002
        try:
            r = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout, input=input,
                               env=self.env(), check=False)
            return r.returncode
        except FileNotFoundError:
            return 127
        except (OSError, subprocess.SubprocessError):
            return 1

    def ctl_run(self, *args: str, timeout: float = 180) -> int:
        return self.run(self.ctl, *args, timeout=timeout)

    def step(self, name: str, fn) -> None:
        try:
            r = fn()
            self.steps[name] = r if isinstance(r, str) and r else "ok"
        except Exception as e:  # noqa: BLE001 — the wipe keeps going whatever one step does
            self.errors += 1
            self.steps[name] = f"error: {type(e).__name__}: {str(e)[:160]}"
        self.write_receipt()

    # ── receipt (RAM) and marker (disk, no secrets) ──
    def write_json(self, path: Path, obj: dict, mode: int = 0o600) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + ".")
        with os.fdopen(fd, "w") as fh:
            json.dump(obj, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)

    def write_receipt(self, done: bool = False, b_due: float | None = None) -> None:
        rec = {"v": 1, "mode": self.mode, "trigger": self.trigger, "started": int(self.started),
               "finished": int(self.clock()) if done else None, "deferred_until": int(b_due) if b_due else None,
               "radio": self.radio, "errors": self.errors, "steps": self.steps, "notes": self.notes[-20:]}
        try:
            self.write_json(self.receipt_path, rec)
        except OSError:
            pass

    def write_marker(self, stage: str, b_due: float | None = None) -> None:
        try:
            self.write_json(self.marker_path, {"v": 1, "mode": self.mode, "trigger": self.trigger, "sale": self.sale,
                                               "stage": stage, "b_due": b_due, "started": self.started})
        except OSError as e:
            self.notes.append(f"could not write the resume marker ({e})")

    # ═════════════════════════ steps ═════════════════════════

    def load_sealed(self):
        cands = [self.p("/opt/prepper/software/common/sealed.py")]
        if self.root == Path("/"):
            cands.append(Path(__file__).resolve().parents[1] / "software" / "common" / "sealed.py")
        for c in cands:
            if c.is_file():
                spec = importlib.util.spec_from_file_location("badger_sealed_for_wipe", c)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                return mod
        return None

    def sealed_keys(self) -> str:
        """1a. Sealed records: the module's own destroy_keys() first (it knows its files), then the
        documented key folders and [wipe] key_paths, whatever the module did."""
        said = "sealed module not installed"
        try:
            mod = self.load_sealed()
        except Exception as e:  # noqa: BLE001 — a broken module must not stop the wipe
            mod, said = None, f"sealed module failed to load ({type(e).__name__})"
        extra: list[str] = []
        if mod is not None:
            fn = getattr(mod, "destroy_keys", None)
            for attr in ("KEY_PATHS", "KEY_FILES"):
                v = getattr(mod, attr, None)
                if isinstance(v, (list, tuple)):
                    extra += [str(x) for x in v if isinstance(x, (str, os.PathLike))]
            if callable(fn):
                try:
                    params = inspect.signature(fn).parameters
                    if "root" in params:
                        fn(root=str(self.root))
                        said = "sealed.destroy_keys() done"
                    elif self.root == Path("/"):
                        fn()
                        said = "sealed.destroy_keys() done"
                    else:
                        said = "sealed.destroy_keys() not called (test root)"
                except Exception as e:  # noqa: BLE001
                    self.errors += 1
                    said = f"sealed.destroy_keys() failed: {type(e).__name__}"
            else:
                said = "sealed module has no destroy_keys()"
        paths = list(SEALED_KEY_PATHS) + extra
        kp = self.w.get("key_paths", [])
        if isinstance(kp, list):
            paths += [x for x in kp if isinstance(x, str)]
        n = 0
        for x in paths:
            x = str(x)
            if os.path.isabs(x) and ".." not in Path(x).parts and any(x.startswith(r) for r in ALLOWED_ROOTS):
                n += bool(self.scrub(self.p(x)))
        return f"{said}; {n} key path(s) removed"

    def vault_keys(self) -> str:
        """1b. The family vault: its passphrase- and recovery-wrapped key (vault.json). Without it the
        blobs are random bytes, even from an image of the flash taken later."""
        v = self.features_dir() / "vault"
        n = 0
        for f in ("vault.json", "vault.json.bak"):
            n += bool(self.scrub(v / f))
        n += self.scrub_glob(v, ".vault.json*")
        return f"{n} wrapped-key file(s) destroyed"

    # ── duress ──
    def sealed_dir(self) -> Path:
        sec = self.cfg.get("sealed") if isinstance(self.cfg.get("sealed"), dict) else {}
        v = sec.get("dir")
        if isinstance(v, str) and os.path.isabs(v) and ".." not in Path(v).parts and any(v.startswith(r) for r in DATA_ROOTS):
            return self.p(v)
        return self.portal_files()["members_file"].parent / "sealed"

    def duress_lock(self) -> int:
        """Duress "lock": the hard-lock flags only (sealed records, vault). Nothing destroyed, nothing restarted,
        no Wi-Fi or posture change, no receipt or marker (prepperctl wipe-status must not tell)."""
        n = 0
        try:
            mod = self.load_module("sealed", "software/common/sealed.py")
            if mod is not None and callable(getattr(mod, "hard_lock_in", None)):
                n += bool(mod.hard_lock_in(self.sealed_dir()))
        except Exception:  # noqa: BLE001 — the vault still gets its flag
            self.errors += 1
        try:
            vc = self.load_module("_vault_crypto", "software/portal/features/_vault_crypto.py")
            if vc is not None:
                n += bool(vc.hard_lock_file(self.features_dir() / "vault" / "vault.json"))
        except Exception:  # noqa: BLE001
            self.errors += 1
        self.steps["lock"] = f"{n} lock(s) set"
        return 0

    def load_module(self, name: str, rel: str):
        cands = [self.p("/opt/prepper/" + rel)]
        if self.root == Path("/") or not cands[0].is_file():
            cands.append(Path(__file__).resolve().parents[1] / rel)
        for c in cands:
            if c.is_file():
                spec = importlib.util.spec_from_file_location(f"badger_{name}_for_wipe", c)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                return mod
        return None

    def decoy_snapshot(self) -> dict:
        """Before the family data goes (duress wipe): the shape of the family (ids, device key HASHES, radios —
        no names) so the made-up household keeps every family phone working."""
        try:
            mod = self.load_module("decoy", "software/common/decoy.py")
            return {"skeleton": mod.member_skeleton(self.portal_files()["members_file"]) if mod else []}
        except Exception:  # noqa: BLE001
            return {"skeleton": []}

    def install_decoy(self, snap: dict) -> str:
        """After a duress wipe the box must not look emptied: the made-up household (decoy.json) goes where the
        family's data was — members (same phones, made-up names), family board, supplies. (Not the community
        board: a wipe removes it like any board.)"""
        mod = self.load_module("decoy", "software/common/decoy.py")
        if mod is None:
            return "decoy module not installed"
        files = self.portal_files()
        pdir = files["members_file"].parent
        prof = mod.ensure(pdir / "decoy.json")
        paths = {"members": files["members_file"], "family_board": files["family_board_file"],
                 "supplies": self.features_dir() / "supplies" / "supplies.json"}
        mod.materialize(prof, paths, snap.get("skeleton") or [], self.started)
        if os.geteuid() == 0:
            try:
                st = pdir.stat()
                for f in list(paths.values()) + [pdir / "decoy.json", self.features_dir(), self.features_dir() / "supplies"]:
                    if f.exists():
                        os.chown(f, st.st_uid, st.st_gid)
            except OSError:
                pass
        return f"made-up household in place ({len(snap.get('skeleton') or [])} member(s))"

    def mesh_running(self) -> bool:
        try:
            return self.clock() - self.p("/run/prepper/mesh.ok").stat().st_mtime < MESH_FRESH_S
        except OSError:
            return False

    def mesh_request(self) -> str:
        """2. Ask the bridge (it owns the radio's serial link) for new random keys on every private
        channel (family + circle), through the usual key-change request with a "wipe" flag."""
        if self.radio_mode == "leave":
            self.radio = "left as it is ([wipe] radio = \"leave\")"
            return "skipped: [wipe] radio = leave"
        files = self.portal_files()
        req = files["mesh_rotate_request"]
        self.mesh_was_running = self.mesh_running()
        rid = secrets.token_hex(6)
        self.mesh_req_id, self.mesh_req_at = rid, self.clock()
        doc = {"id": rid, "at": self.mesh_req_at, "wipe": True, "channels": "private",
               "factory_reset": self.radio_mode == "factory-reset", "forget_link": True}
        req.parent.mkdir(parents=True, exist_ok=True)
        self.write_json(req, doc, 0o640)
        if os.geteuid() == 0:
            try:                                          # the bridge runs as the portal folder's owner
                st = req.parent.stat()
                os.chown(req, st.st_uid, st.st_gid)
            except OSError:
                pass
        if not self.mesh_was_running:
            self.radio = ("NOT re-keyed: the radio bridge isn't running. The radio still holds the family and "
                          "circle keys: unplug it, or factory-reset it (Meshtastic app → Radio config → Factory reset)")
            return "bridge not running: request left for 2 minutes"
        self.radio = "asked for new keys (waiting)"
        return "request sent"

    def device_keys(self, tls: bool) -> str:
        """3. Keys and secrets kept in files: the family radio link (= the channel key), the radio's
        Bluetooth PIN, the panel key + lock, the device signing key and its passphrase file (and its line
        in allowed_signers), the old Wi-Fi password of a pending change, first-boot leftovers, and — unless
        deferred (duress) — the TLS keys."""
        files = self.portal_files()
        n = 0
        for k in ("mesh_channel_link", "mesh_bt_pin_file", "mesh_pair_file", "mesh_circle_link", "circle_ask_file"):
            n += bool(self.scrub(files[k]))
        n += self.scrub_run_mesh(RUN_KEY_FILES)
        keys = self.p("/etc/prepper/keys")
        pub = keys / "disk_ed25519.pub"
        try:
            line = pub.read_text().split()
            own = line[1] if len(line) >= 2 and line[0] == "ssh-ed25519" else None
        except OSError:
            own = None
        if own:
            signers = self.p("/etc/prepper/allowed_signers")
            try:
                kept = [ln for ln in signers.read_text().splitlines() if own not in ln]
                self.write_json_text(signers, "\n".join(kept) + ("\n" if kept else ""), 0o644)
            except OSError:
                pass
        for f in ("disk_ed25519", "disk_ed25519.pass", "disk_ed25519.pass.tmp", "disk_ed25519.pub"):
            n += bool(self.scrub(keys / f))
        for f in ("/etc/prepper/panel.key", "/etc/prepper/panel.key.sha256", "/etc/prepper/panel-lock.json",
                  "/etc/prepper/admin_pin.pending", "/var/lib/prepper/wifi-rotate-pending.json",
                  "/var/lib/prepper/deadman-state.json", "/run/prepper/.deadman-pending"):
            n += bool(self.scrub(self.p(f)))
        n += self.scrub_glob(self.p("/var/lib/prepper"), ".wifi-rotate.*")
        self.run("systemctl", "stop", "prepper-wifi-revert.timer", timeout=10)
        if (self.p("/var/lib/prepper/firstboot.done")).exists():
            n += bool(self.scrub(self.p("/boot/firmware/prepper")))
        if tls:
            n += self.tls_keys()
        return f"{n} file(s) destroyed"

    def write_json_text(self, path: Path, text: str, mode: int) -> None:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + ".")
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)

    def tls_keys(self) -> int:
        d = self.p("/etc/prepper/tls")
        n = 0
        for f in ("ca.key", "server.key", "ca.srl", "server.csr", "ext.cnf", "server.crt", "ca.crt"):
            n += bool(self.scrub(d / f))
        return n

    def family_data(self) -> str:
        """4. The family's data, deny-by-default: everything in the portal's folder goes except the
        library-side keep-list. The pending radio request stays until the bridge has read it."""
        files = self.portal_files()
        keep = {files["mesh_rotate_request"], files["mesh_rotate_result"]}   # (the bridge may answer meanwhile)
        if self.mode == "wipe":
            keep |= {self.p(f"{PORTAL_DIR}/{n}") for n in KEEP_PORTAL}
            keep.add(files["posture_file"])
        if self.mode == "wipe":
            keep.add(self.personal_file())                  # the licence decision (handover: removed later)
        feats = self.features_dir()
        kf = self.keep_features
        n = 0
        # The portal's own folder is swept whole; files configured elsewhere are removed one by one
        # (never their folder: a [portal] path under /etc/prepper must not take prepper.toml with it).
        pdir = self.p(PORTAL_DIR)
        if pdir.is_dir() and not pdir.is_symlink():
            for entry in sorted(pdir.iterdir()):
                if entry in keep or entry == feats:
                    continue
                n += bool(self.scrub(entry))
        for f in files.values():
            if f not in keep:
                n += bool(self.scrub(f))
        if feats.is_dir():
            for entry in sorted(feats.iterdir()):
                if entry.name in kf and entry.is_dir() and not entry.is_symlink():
                    continue
                n += bool(self.scrub(entry))
        # the radio feature's saved station position (the house, to a few metres)
        n += bool(self.scrub(self.p("/var/lib/prepper/sdr/position.json")))
        return f"{n} item(s) removed"

    def mesh_finish(self) -> str:
        """5. Wait (briefly, from when the request was sent) for the bridge's answer; then forget every
        radio file again — the bridge saves the NEW link after a key change, and nobody needs it."""
        files = self.portal_files()
        res_path = files["mesh_rotate_result"]
        out = "no request"
        if self.mesh_req_id:
            deadline = self.mesh_req_at + self.mesh_wait
            got = None
            while self.mesh_was_running:
                try:
                    d = json.loads(res_path.read_text())
                    if isinstance(d, dict) and d.get("id") == self.mesh_req_id:
                        got = d
                        break
                except (OSError, ValueError):
                    pass
                if self.clock() >= deadline:
                    break
                self.sleep(0.2)
            if got is None and self.mesh_was_running:
                self.radio = ("NOT confirmed: the radio bridge didn't answer in time. To be sure, unplug the radio "
                              "or factory-reset it (Meshtastic app → Radio config → Factory reset)")
                out = "no answer from the bridge"
            elif got is not None and got.get("ok"):
                if got.get("factory_reset"):
                    self.radio = "factory-reset by the bridge (all keys and the radio's node list are gone)"
                elif got.get("wipe"):
                    chans = got.get("channels") if isinstance(got.get("channels"), list) else []
                    self.radio = (f"new random keys on the radio's private channels {chans}" if chans else
                                  "no private channel on the radio (nothing to re-key)")
                else:
                    self.radio = ("family channel re-keyed (this bridge doesn't know the wipe request yet: a circle "
                                  "channel keeps its key — unplug or factory-reset the radio to be sure)")
                out = "radio answered"
            elif got is not None:
                chans = got.get("channels") if isinstance(got.get("channels"), list) else []
                self.radio = ((f"new keys on channels {chans} only; " if chans else "") +
                              f"the bridge could not change every key ({str(got.get('error'))[:120]}): unplug the radio")
                out = "radio refused"
        if self.mesh_after == "off":
            self.run("systemctl", "stop", "prepper-meshbridge", timeout=20)
            self.run("systemctl", "disable", "prepper-meshbridge", timeout=20)
        for k in ("mesh_rotate_result", "mesh_channel_link", "mesh_bt_pin_file", "mesh_pair_file", "mesh_nodes",
                  "mesh_peers", "mesh_telemetry", "mesh_outbox", "mesh_pending", "mesh_circle_link",
                  "circle_status_file", "circle_ask_file"):
            self.scrub(files[k])
        self.scrub_run_mesh()
        if not self.mesh_was_running or self.mesh_after == "off":
            self.scrub(files["mesh_rotate_request"])      # never acted on later by a bridge that comes back
        return out

    def posture(self) -> str:
        """A panic wipe leaves the Badger in LOCKDOWN (nothing beyond the family, radio muted, Wi-Fi password
        on — fail closed: whoever holds it next gets the quietest box); a handover leaves it GUARDED (the
        shipped default) for the new owner. The posture.json contract of software/common/posture.py."""
        level = "guarded" if self.mode == "handover" else "lockdown"
        path = self.portal_files()["posture_file"]
        path.parent.mkdir(parents=True, exist_ok=True)
        self.write_json(path, {"level": level, "changed_at": float(self.clock()),
                               "by": "handover reset" if self.mode == "handover" else "panic wipe"}, 0o640)
        if os.geteuid() == 0:
            try:
                st = path.parent.stat()
                os.chown(path, st.st_uid, st.st_gid)       # the launcher writes it next time
            except OSError:
                pass
        if self.mode == "handover":
            self.scrub(self.p(f"{PORTAL_DIR}/mesh-silent"))
        return f"posture: {level}"

    def uplink(self) -> str:
        """6. Uplink Wi-Fi: [network] uplink_wifi, NetworkManager client profiles (files and live), and the
        builder's netplan / cloud-init network-config Wi-Fi block."""
        n = 0
        try:
            t = self.conf.read_text()
            m = _section(t, "network")
            if m and re.search(r"(?m)^uplink_wifi[ \t]*=", m.group(1)):
                t2 = set_key(t, "network", "uplink_wifi", "[]")
                if t2 != t:
                    self.conf_write(t2)
                    n += 1
        except OSError as e:
            self.notes.append(f"prepper.toml uplink not cleared: {e}")
        nm = self.p("/etc/NetworkManager/system-connections")
        if nm.is_dir():
            for f in sorted(nm.iterdir()):
                try:
                    body = f.read_text(errors="replace") if f.is_file() and f.stat().st_size < 65536 else ""
                except OSError:
                    body = ""
                if f.name.startswith("prepper-uplink-") or re.search(r"(?m)^type=(wifi|802-11-wireless)\s*$", body):
                    n += bool(self.scrub(f))
        names = []
        try:
            r = subprocess.run(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"], capture_output=True,
                               text=True, timeout=10, env=self.env(), check=False)
            for ln in r.stdout.splitlines():
                name, _, typ = ln.rpartition(":")
                if typ in ("802-11-wireless", "wifi") and name:
                    names.append(name.replace("\\:", ":"))
        except (OSError, subprocess.SubprocessError):
            pass
        for name in names:
            self.run("nmcli", "connection", "delete", "id", name, timeout=15)
            n += 1
        for y in sorted(self.p("/etc/netplan").glob("*.yaml")) + [self.p("/boot/firmware/network-config")]:
            try:
                t = y.read_text()
            except OSError:
                continue
            t2 = strip_wifis(t)
            if t2 != t:
                mode = y.stat().st_mode & 0o777
                self.write_json_text(y, t2, mode)
                n += 1
        self.run("nmcli", "connection", "reload", timeout=10)
        return f"{n} uplink item(s) removed"

    def logs(self) -> str:
        """7. Logs: the journal (RAM on a Badger, but rotate + vacuum anyway), nginx, the first-boot log."""
        rc1 = self.run("journalctl", "--rotate", timeout=15)
        rc2 = self.run("journalctl", "--vacuum-time=1s", timeout=20)
        n = self.scrub_glob(self.p("/var/log/nginx"), "*.log*")
        n += bool(self.scrub(self.p("/var/log/prepper-firstboot.log")))
        self.run("systemctl", "kill", "-s", "USR1", "nginx", timeout=10)     # reopen its (gone) log files
        return f"journal {'vacuumed' if rc1 == 0 and rc2 == 0 else 'NOT vacuumed (journalctl failed)'}; {n} log file(s) removed"

    def leases(self) -> str:
        """8. DHCP leases (phone names, MAC addresses): dnsmasq keeps them in memory too, so stop, remove, start."""
        self.run("systemctl", "stop", "dnsmasq", timeout=20)
        n = 0
        for f in ("/run/prepper-dnsmasq/leases", "/var/lib/misc/dnsmasq.leases"):
            n += bool(self.scrub(self.p(f)))
        rc = self.run("systemctl", "start", "dnsmasq", timeout=20)
        return f"{n} lease file(s) removed" + ("" if rc == 0 else "; dnsmasq did not restart")

    # ── phase B ──
    def tls_new(self) -> str:
        n = self.tls_keys()
        if self.net.get("https", True) is False:
            return f"{n} TLS file(s) removed (https is off)"
        rc = self.ctl_run("gen-tls", timeout=120)
        return f"{n} TLS file(s) removed; " + ("a new certificate (new local CA)" if rc == 0 else "NO new certificate (gen-tls failed: https off until it runs)")

    def conf_write(self, text: str) -> None:
        spec = importlib.util.spec_from_file_location("badger_conf_write", Path(__file__).with_name("conf_write.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.conf_write(str(self.conf), text)

    def wifi(self) -> str:
        """The hotspot password: a new random one (shown only on the front panel: Join Wi-Fi), or Wi-Fi off
        ([wipe] wifi = "off"), or unchanged ("keep"). Handover: also a new neutral name."""
        mode = self.wifi_mode
        if mode in ("keep", "off"):
            return f"skipped here ([wipe] wifi = {mode})"
        t = self.conf.read_text()
        sec = self.net.get("security", "auto")
        if mode == "new-name":
            t = set_key(t, "network", "ssid", json.dumps(neutral_ssid()))
            t = set_key(t, "network", "security", '"auto"')
            t = set_key(t, "network", "passphrase", json.dumps(random_passphrase()))
            self.conf_write(t)
            return "new hotspot name and password (front panel → Join Wi-Fi)"
        if sec in ("owe", "open", "owe-transition"):
            return f"skipped: the hotspot has no password (security = {sec})"
        self.conf_write(set_key(t, "network", "passphrase", json.dumps(random_passphrase())))
        return "new hotspot password (front panel → Join Wi-Fi)"

    def apply(self) -> str:
        self.reload()
        rc = self.ctl_run("apply-config", timeout=240)
        out = "settings applied, launcher restarted" if rc == 0 else f"apply-config failed (exit {rc})"
        if self.wifi_mode == "off":
            out += "; Wi-Fi off" if self.ctl_run("wifi-off", timeout=30) == 0 else "; Wi-Fi could NOT be switched off"
        if self.trigger not in PANEL_TRIGGERS:
            self.run("systemctl", "try-restart", "prepper-panel", timeout=20)
        return out

    # ── handover only ──
    def factory_config(self) -> str:
        t = self.conf.read_text()
        dev = self.cfg.get("device", {}) if isinstance(self.cfg.get("device"), dict) else {}
        was_sale = dev.get("provisioning") == "sale" or self.sale
        t = set_key(t, "portal", "admin_pin", '""')
        t = set_key(t, "portal", "duress_pin", None)
        if _section(t, "wipe"):
            t = set_key(t, "wipe", "deadman_days", "0")
        if _section(t, "upd"):
            t = set_key(t, "upd", "peers", "[]")
        for k in ("latitude", "longitude"):
            t = set_key(t, "time", k, None)
        if was_sale:
            t = set_key(t, "device", "provisioning", '"sale"')
        self.conf_write(t)
        self.sale = was_sale
        for f in ("/var/lib/prepper/deadman-state.json", "/run/prepper/.deadman-pending"):
            self.scrub(self.p(f))
        self.run("systemctl", "disable", "--now", "prepper-deadman.timer", timeout=20)
        signers = self.p("/etc/prepper/allowed_signers")
        if signers.exists():                              # the old owner's Badgers can't push content any more
            self.write_json_text(signers, "", 0o644)
        return "factory settings (no admin PIN, no duress PIN, dead-man off, no peers" + \
            (", sale unit)" if was_sale else ")")

    def personal_content(self) -> str:
        """A unit built for sale goes back to "sale": personal-use content withdrawn and removed, and the
        acceptance record gone so the next owner is asked again."""
        pf = self.personal_file()
        dev = self.cfg.get("device", {}) if isinstance(self.cfg.get("device"), dict) else {}
        self.sale = self.sale or dev.get("provisioning") == "sale"
        if not self.sale:
            self.scrub(pf)
            return "acceptance record removed (owner-built unit: personal-use content kept — use --sale to remove it)"
        rec = {"format": 1, "withdrawn_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               "history": [{"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": "removed", "by": "handover reset"}]}
        pf.parent.mkdir(parents=True, exist_ok=True)
        self.write_json(pf, rec, 0o640)
        if self.root != Path("/"):
            return "withdrawn (test root: items are removed by the next update)"
        try:
            spec = importlib.util.spec_from_file_location("badger_upd_for_wipe", self.p("/opt/prepper/software/upd/upd.py"))
            upd = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(upd)
            cfg = upd.Config.load(self.conf)
            with upd.run_lock(cfg):
                removed = upd.prune_withdrawn(cfg, upd.State(cfg.state_file), upd.load_catalog(cfg.catalog),
                                              upd.load_personal_record(cfg))
        except Exception as e:  # noqa: BLE001 — e.g. an update holds the lock: the next update removes them
            return f"withdrawn; removal postponed to the next update ({type(e).__name__})"
        self.scrub(pf)
        return f"{len(removed)} personal-use item(s) removed; the next owner is asked again"

    def identity(self) -> str:
        rc = self.ctl_run("gen-key", timeout=60)
        return "a new signing identity" if rc == 0 else f"gen-key failed (exit {rc}); install.sh makes one next time"

    # ═════════════════════════ phases ═════════════════════════

    def phase_a(self) -> None:
        defer = self.duress
        snap = self.decoy_snapshot() if defer else None
        self.step("sealed-keys", self.sealed_keys)
        self.step("vault-keys", self.vault_keys)
        self.step("mesh-request", self.mesh_request)
        self.step("device-keys", lambda: self.device_keys(tls=not defer))
        self.step("family-data", self.family_data)
        if defer:
            self.step("decoy", lambda: self.install_decoy(snap))
        if not defer:
            self.step("posture", self.posture)
        if self.mode == "handover":
            self.step("personal-content", self.personal_content)
        self.step("mesh", self.mesh_finish)
        self.step("uplink", self.uplink)
        self.step("logs", self.logs)
        self.step("leases", self.leases)

    def phase_b(self) -> None:
        if self.mode == "handover":
            self.step("factory-config", self.factory_config)
            self.step("identity", self.identity)
        if self.duress:
            self.step("posture", self.posture)            # (a duress wipe changes nothing visible until now)
        self.step("tls", self.tls_new)
        self.step("wifi", self.wifi)
        self.step("apply", self.apply)
        if self.mode == "wipe" and self.trigger in DURESS_TRIGGERS:
            # keys and tokens held in memory went with the restart; the journal got the restart's lines
            self.run("journalctl", "--rotate", timeout=15)
            self.run("journalctl", "--vacuum-time=1s", timeout=20)

    def schedule_b(self, due: float) -> bool:
        secs = max(30, int(due - self.clock()))
        rc = self.run("systemd-run", "--quiet", "--collect", f"--unit=prepper-wipe-finish-{int(self.clock())}",
                      f"--on-active={secs}s", self.ctl, "wipe-finish", timeout=20)
        return rc == 0

    def run_all(self) -> int:
        if self.duress and self.duress_action == "lock":
            return self.duress_lock()
        self.write_marker("a")
        self.write_receipt()
        self.phase_a()
        if self.duress:
            due = self.clock() + self.duress_delay
            self.write_marker("b", due)
            if not self.schedule_b(due):
                self.notes.append("the delayed part could not be scheduled: wipe-resume runs it at the next boot")
            self.write_receipt(done=False, b_due=due)
            return 0
        self.write_marker("b", self.clock())
        self.phase_b()
        self.finish()
        return 0

    def finish(self) -> None:
        try:
            self.marker_path.unlink()
        except OSError:
            pass
        self.write_receipt(done=True)

    def summary(self) -> str:
        if self.duress and "lock" in self.steps:
            return "done"
        bad = [k for k, v in self.steps.items() if v.startswith("error")]
        return (f"{'handover reset' if self.mode == 'handover' else 'wipe'}: "
                f"{len(self.steps) - len(bad)} steps done" + (f", {len(bad)} with errors ({', '.join(bad)})" if bad else "")
                + f"; radio: {self.radio}")


def resume(args, boot: bool) -> int:
    """At boot (and from the delayed timer): finish what a wipe left undone."""
    marker = Path(args.root or "/") / "var/lib/prepper/wipe-pending.json"
    try:
        m = json.loads(marker.read_text())
    except (OSError, ValueError):
        print("no wipe waiting")
        return 0
    if not isinstance(m, dict):
        m = {}
    w = Wipe(args.root, args.conf, args.ctl, m.get("mode", "wipe"), m.get("trigger", "cli"), bool(m.get("sale")))
    w.action_override = "wipe"                            # a duress "lock" never leaves a marker
    if m.get("stage") != "b":
        w.notes.append("resumed after an interruption")
        w.run_all()
        print(w.summary())
        return 0
    due = _num(m.get("b_due"), 0.0, 0.0, 1e12)
    if boot and due > w.clock() + 5:
        w.schedule_b(due)
        print("the rest of the wipe is scheduled again")
        return 0
    w.phase_b()
    w.finish()
    print(w.summary())
    return 0


# ───────────────────────────── dead-man switch ───────────────────────────────
# Default off. Armed in two steps (deadman-arm DAYS → a code; deadman-confirm CODE within 10 min),
# which also sets [wipe] deadman_days. An hourly timer (prepper-deadman.timer) counts time the Badger
# has been RUNNING without a check-in (a member's device or an admin login: the launcher bumps a
# counter in checkin.json). Only uptime counts — a wall-clock jump (GPS fix after a flat RTC) can't
# fire it, and a Badger that is switched off never counts down.

class DeadMan:
    def __init__(self, root: str, conf: str, ctl: str, clock=now):
        self.root, self.conf, self.ctl, self.clock = Path(root or "/"), Path(conf), ctl, clock
        self.state_path = self.p("/var/lib/prepper/deadman-state.json")
        self.pending_path = self.p("/run/prepper/.deadman-pending")
        self.checkin_path = self.p(f"{PORTAL_DIR}/checkin.json")

    def p(self, path: str) -> Path:
        return self.root / path.lstrip("/")

    def days(self) -> int:
        try:
            v = tomllib.loads(self.conf.read_text()).get("wipe", {}).get("deadman_days", 0)
        except (OSError, ValueError, AttributeError):
            return 0
        return int(v) if isinstance(v, int) and not isinstance(v, bool) and DEADMAN_MIN_DAYS <= v <= DEADMAN_MAX_DAYS else 0

    def uptime(self) -> float:
        try:
            return float(self.p("/proc/uptime").read_text().split()[0])
        except (OSError, ValueError, IndexError):
            return -1.0

    def boot_id(self) -> str:
        try:
            return self.p("/proc/sys/kernel/random/boot_id").read_text().strip()[:64]
        except OSError:
            return ""

    def seq(self):
        try:
            d = json.loads(self.checkin_path.read_text())
            return d.get("seq") if isinstance(d, dict) else None
        except (OSError, ValueError):
            return None

    def state(self) -> dict | None:
        try:
            d = json.loads(self.state_path.read_text())
            return d if isinstance(d, dict) else None
        except (OSError, ValueError):
            return None

    def save(self, d: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.state_path.parent, prefix=".deadman.")
        with os.fdopen(fd, "w") as fh:
            json.dump(d, fh)
        os.chmod(tmp, 0o644)                    # the launcher reads it for its warning (no secrets)
        os.replace(tmp, self.state_path)

    def status(self) -> dict:
        days, st = self.days(), self.state()
        if not days or st is None:
            return {"armed": False, "days": 0}
        idle = _num(st.get("idle_s"), 0.0, 0.0, 1e9)
        left = max(0.0, days * 86400 - idle)
        return {"armed": True, "days": days, "idle_h": round(idle / 3600, 1), "left_h": round(left / 3600, 1),
                "warning": left <= DEADMAN_WARN_S}

    def arm(self, days: int) -> str:
        if not DEADMAN_MIN_DAYS <= days <= DEADMAN_MAX_DAYS:
            raise ValueError(f"days must be {DEADMAN_MIN_DAYS}–{DEADMAN_MAX_DAYS}")
        code = "".join(secrets.choice(WIFI_ALPHABET) for _ in range(6))
        self.pending_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.pending_path.parent, prefix=".dm.")
        with os.fdopen(fd, "w") as fh:
            json.dump({"days": days, "h": hashlib.sha256(code.encode()).hexdigest(), "until": self.clock() + 600}, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.pending_path)
        return code

    def confirm(self, code: str, writer) -> int:
        try:
            d = json.loads(self.pending_path.read_text())
        except (OSError, ValueError):
            raise ValueError("no dead-man setup is waiting (start again)") from None
        finally:
            try:
                self.pending_path.unlink()
            except OSError:
                pass
        if not (isinstance(d, dict) and isinstance(d.get("h"), str) and _num(d.get("until"), 0, 0, 1e12) >= self.clock()
                and hmac.compare_digest(hashlib.sha256(code.strip().upper().encode()).hexdigest(), d["h"])):
            raise ValueError("that code is wrong or too old; nothing was changed (start again)")
        days = int(d.get("days", 0))
        if not DEADMAN_MIN_DAYS <= days <= DEADMAN_MAX_DAYS:
            raise ValueError("bad setup record")
        writer(set_key(self.conf.read_text(), "wipe", "deadman_days", str(days)))
        self.save({"idle_s": 0.0, "seq": self.seq(), "boot": self.boot_id(), "uptime": self.uptime(), "armed_at": self.clock()})
        return days

    def off(self, writer) -> None:
        t = self.conf.read_text()
        if _section(t, "wipe"):
            writer(set_key(t, "wipe", "deadman_days", "0"))
        for f in (self.state_path, self.pending_path):
            try:
                f.unlink()
            except OSError:
                pass

    def check(self) -> tuple[str, dict]:
        """("off"|"reset"|"counting"|"fire", status). Called hourly by the timer."""
        days = self.days()
        st = self.state()
        if not days or st is None:
            return "off", {"armed": False}
        seq, boot, up = self.seq(), self.boot_id(), self.uptime()
        idle = _num(st.get("idle_s"), 0.0, 0.0, 1e9)
        what = "counting"
        if seq != st.get("seq"):
            idle, what = 0.0, "reset"
        elif boot and boot == st.get("boot") and up >= 0:
            idle += max(0.0, min(up - _num(st.get("uptime"), up, -1.0, 1e12), DEADMAN_STEP_MAX))
        st.update(idle_s=idle, seq=seq, boot=boot, uptime=up)
        self.save(st)
        if idle >= days * 86400:
            return "fire", self.status()
        return what, self.status()


# ───────────────────────────── command line ──────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["verify-pin"]:
        stored = sys.stdin.readline().strip()
        pin = sys.stdin.readline().rstrip("\r\n")
        return 0 if verify_pin(pin, stored) else 1
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default="/")
    ap.add_argument("--conf", default="/etc/prepper/prepper.toml")
    ap.add_argument("--ctl", default="/opt/prepper/bin/prepperctl")
    ap.add_argument("--mode", default="wipe", choices=("wipe", "handover"))
    ap.add_argument("--trigger", default="cli", choices=TRIGGERS)
    ap.add_argument("--sale", action="store_true")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--boot", action="store_true")
    ap.add_argument("deadman", nargs="*")
    a = ap.parse_args(argv)
    if a.deadman:
        dm = DeadMan(a.root, a.conf, a.ctl)
        verb = a.deadman[0]

        def writer(text: str) -> None:
            Wipe(a.root, a.conf, a.ctl).conf_write(text)
        try:
            if verb == "deadman-status":
                print(json.dumps(dm.status()))
                return 0
            if verb == "deadman-arm" and len(a.deadman) == 2 and a.deadman[1].isdigit():
                code = dm.arm(int(a.deadman[1]))
                print(f"confirm-code: {code}")
                return 0
            if verb == "deadman-confirm" and len(a.deadman) == 2:
                days = dm.confirm(a.deadman[1], writer)
                print(f"dead-man switch ARMED: the family data is wiped after {days} days of the Badger running "
                      "with no check-in (a family member's device or an admin login)")
                return 0
            if verb == "deadman-off":
                dm.off(writer)
                print("dead-man switch off")
                return 0
            if verb == "deadman-check":
                what, st = dm.check()
                if what == "fire":
                    w = Wipe(a.root, a.conf, a.ctl, "wipe", "deadman")
                    w.notes.append(f"dead-man switch: no check-in for {st.get('days')} days of running")
                    w.run_all()
                    try:
                        dm.state_path.unlink()
                    except OSError:
                        pass
                    print(w.summary())
                    return 0
                print(json.dumps({"result": what, **st}))
                return 0
        except ValueError as e:
            print(f"prepperctl: {e}", file=sys.stderr)
            return 2
        print("prepperctl: unknown dead-man command", file=sys.stderr)
        return 2
    if a.resume:
        return resume(a, a.boot)
    w = Wipe(a.root, a.conf, a.ctl, a.mode, a.trigger, a.sale)
    w.run_all()
    print(w.summary())
    return 0


if __name__ == "__main__":
    sys.exit(main())
