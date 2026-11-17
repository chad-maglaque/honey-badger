"""
Sealed records — the portal's owner-facing API (the crypto is software/common/sealed.py).

  GET  /api/sealed            status (admin, family members, the Badger's own screen)
  POST /api/sealed  {"op": …}
       setup        {passphrase}                      → {recovery_key} (shown ONCE)      admin, secure
       unlock       {secret, confirm_lockdown?}       passphrase or recovery key        admin, secure
       lock         {}                                "Lock now"                         admin, member, panel
       change       {old, new}                        new owner passphrase               admin, secure
       recover      {recovery_key, new}               forgotten passphrase               admin, secure
       new_recovery {passphrase}                      → {recovery_key}                   admin, secure
       ack_recovery {}                                "I've written the recovery key down" admin

DURESS (docs/SECURITY.md → "Duress"): the duress passphrase typed where the owner passphrase is asked gets
exactly an unlock's reply (same status, same shape, same work: every try runs both scrypts) while the launcher
switches to the made-up household and runs the duress action (`duress` hooks from the launcher). In the decoy
every op answers like the real thing against the decoy's own sealer; only the REAL recovery key does something
real: it ends the hard lock, the decoy, and unlocks the real records.

"secure" = HTTPS (nginx says so) or the Badger itself (loopback, not via nginx): a passphrase typed over
plain HTTP on the hotspot could be read by anyone who knows the Wi-Fi password. The front panel (local
key) counts as the admin here. Wrong passphrases lock out like the vault (per address and Badger-wide,
the latter surviving a restart).
"""
from __future__ import annotations

import secrets
from pathlib import Path

import sealed

INSECURE = ("Your passphrase would cross the Wi-Fi unencrypted. Open the admin page with https:// "
            "(https://badger.lan), or unlock at the Badger itself.")


def options(cfg) -> dict:
    """Sealer options from prepper.toml ([sealed]; openssl falls back to [vault] openssl)."""
    raw = getattr(cfg, "raw", {}) or {}
    c, v = raw.get("sealed", {}) or {}, raw.get("vault", {}) or {}
    rel = (lambda x: (cfg.path.parent / x).resolve() if not Path(x).is_absolute() else Path(x))
    folder = rel(c["dir"]) if isinstance(c.get("dir"), str) and c["dir"] else cfg.members_file.parent / "sealed"
    n = c.get("scrypt_n", sealed.vc.SCRYPT_N)
    if isinstance(n, bool) or not isinstance(n, int) or n < 1024 or n > (1 << 20) or n & (n - 1):
        n = sealed.vc.SCRYPT_N
    idle = c.get("idle_minutes", 0)
    if isinstance(idle, bool) or not isinstance(idle, (int, float)) or idle < 0:
        idle = 0
    posture = c.get("posture_file")
    if not (isinstance(posture, str) and posture) and getattr(cfg, "posture_file", None):
        posture = str(cfg.posture_file)                  # [portal] posture_file (the posture switch's file)
    return {"folder": folder, "openssl": str(c.get("openssl") or v.get("openssl") or "/usr/bin/openssl"),
            "scrypt_n": n, "idle_minutes": min(float(idle), 7 * 24 * 60),
            "posture_file": rel(posture) if isinstance(posture, str) and posture else folder.parent / "posture.json"}


def get(h, s: sealed.Sealer):
    if not (h.is_admin() or h.member() or h.local_console()):
        return 401, {"error": "family members and the admin only"}
    st = s.status(h.client_ip())
    st["secure"] = bool(h.secure_transport())
    st["admin"] = bool(h.is_admin() or h.local_console())
    return 200, st


def _fake_recovery_key() -> str:
    return sealed.vc.format_recovery(secrets.token_bytes(32))


def _dummy_kdf_work(real: sealed.Sealer | None, secret) -> None:
    """The work of a passphrase try where there is nothing to check against (the decoy after a duress wipe)."""
    try:
        n = real.scrypt_n if real is not None else sealed.vc.SCRYPT_N
        for _ in range(2):
            sealed.vc.passphrase_material(str(secret), sealed.vc.new_kdf(n))
    except (ValueError, sealed.vc.CryptoError):
        pass


def _decoy_post(h, s: sealed.Sealer, data: dict, duress, op, admin: bool, ip: str):
    """The sealed-records page during a duress: every answer looks real; only the REAL recovery key acts."""
    if op == "lock":
        if not (admin or h.member()):
            return 401, {"error": "family members and the admin only"}
        s.lock_all("locked")
        return 200, {"ok": True, "status": s.status(ip)}
    if op not in ("setup", "unlock", "change", "recover", "new_recovery", "ack_recovery"):
        return 400, {"error": "unknown op"}
    if not admin:
        return 401, {"error": "admin login required"}
    if op == "ack_recovery":
        return 200, {"ok": True, "status": s.status(ip)}
    if not h.secure_transport():
        return 403, {"error": INSECURE, "insecure": True}
    real = duress.real
    real_ok = real is not None and real.is_setup()
    try:
        if op == "setup":
            raise sealed.Refused(409, "Sealed records are already set up. Unlock them with the owner passphrase.")
        if op == "unlock":
            secret = data.get("secret")
            if not isinstance(secret, str) or not secret.strip() or len(secret) > 400:
                raise sealed.Refused(400, "Enter the owner passphrase (or the recovery key).")
            if real_ok and sealed.vc.parse_recovery(secret) is not None:
                how = real.unlock(secret, ip, confirm_lockdown=True)
                if how == "recovery":
                    duress.recovered()
                    return 200, {"ok": True, "via": "recovery", "status": real.status(ip)}
            elif real_ok:
                real.check_secret(secret, ip)              # passphrase or duress passphrase: wrong ones refused
            else:
                _dummy_kdf_work(real, secret)
            s.open_up()
            return 200, {"ok": True, "via": "passphrase", "status": s.status(ip)}
        if op == "recover":
            new = data.get("new")
            bad = sealed.passphrase_problem(new)
            if bad:
                raise sealed.Refused(400, bad)
            if sealed.vc.parse_recovery(data.get("recovery_key")) is None:
                raise sealed.Refused(400, "That doesn't look like a recovery key (13 groups of 4 letters and digits).")
            if not real_ok:
                raise sealed.Refused(403, "That recovery key isn't right.")
            real.recover(data.get("recovery_key"), new, ip, confirm_lockdown=True)
            duress.recovered()
            return 200, {"ok": True, "status": real.status(ip)}
        secret = data.get("old") if op == "change" else data.get("passphrase")
        if op == "change":
            bad = sealed.passphrase_problem(data.get("new"))
            if bad:
                raise sealed.Refused(400, bad)
        if real_ok:
            real.check_secret(secret, ip, allow_recovery=op == "change")
        else:
            _dummy_kdf_work(real, secret)
        if op == "change":
            return 200, {"ok": True, "status": s.status(ip)}
        return 200, {"ok": True, "recovery_key": _fake_recovery_key(), "status": s.status(ip)}
    except sealed.Refused as e:
        return e.status, {"error": e.message, **{k: v for k, v in e.extra.items() if k != "hard_locked"}}
    except sealed.SealError as e:
        return 503, {"error": f"Sealed records aren't working: {e}"}


def post(h, s: sealed.Sealer, data: dict, pin_matches=None, duress=None):
    """pin_matches(passphrase) → True when it is the admin PIN (refused as an owner passphrase).
    duress (from the launcher): .decoy (a duress is showing the made-up household), .real (the real sealer),
    .trigger() → the decoy's sealer (the duress passphrase was typed), .recovered() (the real recovery key)."""
    op = data.get("op") if isinstance(data, dict) else None
    admin = bool(h.is_admin() or h.local_console())
    ip = h.client_ip()
    if duress is not None and duress.decoy:
        return _decoy_post(h, s, data, duress, op, admin, ip)
    if op == "lock":
        if not (admin or h.member()):
            return 401, {"error": "family members and the admin only"}
        s.lock_all("locked")
        return 200, {"ok": True, "status": s.status(ip)}
    if op not in ("setup", "unlock", "change", "recover", "new_recovery", "ack_recovery"):
        return 400, {"error": "unknown op"}
    if not admin:
        return 401, {"error": "admin login required"}
    if op == "ack_recovery":
        s.ack_recovery()
        return 200, {"ok": True, "status": s.status(ip)}
    if not h.secure_transport():
        return 403, {"error": INSECURE, "insecure": True}

    def not_pin(pw):
        if isinstance(pw, str) and pin_matches is not None and pin_matches(pw):
            raise sealed.Refused(400, "Don't use the admin PIN as the owner passphrase: pick something different.")
    try:
        if op == "setup":
            not_pin(data.get("passphrase"))
            res = s.setup(data.get("passphrase"))
            return 200, {"ok": True, "recovery_key": res["recovery_key"], "migrated": res["migrated"],
                         "status": s.status(ip)}
        if op == "unlock":
            how = s.unlock(data.get("secret"), ip, confirm_lockdown=data.get("confirm_lockdown") is True)
            if how in ("duress", "hard-locked"):
                if duress is None:
                    raise sealed.Refused(403, "That passphrase isn't right.")
                ds = duress.trigger()
                return 200, {"ok": True, "via": "passphrase", "status": ds.status(ip)}
            return 200, {"ok": True, "via": how, "status": s.status(ip)}
        if op == "change":
            not_pin(data.get("new"))
            try:
                s.change_passphrase(data.get("old"), data.get("new"), ip)
            except sealed.DuressSecret:
                if duress is None:
                    raise sealed.Refused(403, "That passphrase isn't right.") from None
                return 200, {"ok": True, "status": duress.trigger().status(ip)}
            return 200, {"ok": True, "status": s.status(ip)}
        if op == "recover":
            not_pin(data.get("new"))
            s.recover(data.get("recovery_key"), data.get("new"), ip,
                      confirm_lockdown=data.get("confirm_lockdown") is True)
            return 200, {"ok": True, "status": s.status(ip)}
        try:
            rk = s.new_recovery_key(data.get("passphrase"), ip)
        except sealed.DuressSecret:
            if duress is None:
                raise sealed.Refused(403, "That passphrase isn't right.") from None
            return 200, {"ok": True, "recovery_key": _fake_recovery_key(), "status": duress.trigger().status(ip)}
        return 200, {"ok": True, "recovery_key": rk, "status": s.status(ip)}
    except sealed.Refused as e:
        return e.status, {"error": e.message, **e.extra}
    except sealed.SealError as e:
        return 503, {"error": f"Sealed records aren't working: {e}"}
