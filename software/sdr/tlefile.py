#!/usr/bin/env python3
"""
tlefile — orbit data (two-line element sets) for the weather-satellite feature.

    tlefile.py fetch            download fresh TLEs from Celestrak for every satellite in
                            [satellites] (prepper-tle.timer, daily; quiet when offline)
    tlefile.py import FILE      load TLEs from a file (a USB stick, a friend's download)
    tlefile.py show             what is loaded and how old it is

Two files feed the predictions: state/tle.txt (downloaded) and state/tle-upload.txt (pasted
by an admin in the launcher, or imported). For each satellite the set with the newest epoch
wins, so an old upload never overrides a fresh download and vice versa.

Age = now - the TLE's own epoch. Meteor-M orbits change slowly; predictions stay within a
few seconds for about a week, drift to around a minute after a few weeks, and are marked
approximate past [satellites] tle_stale_days (14) and not used for recording past
tle_max_days (60).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sdrcommon as C  # noqa: E402
import orbit  # noqa: E402

MAX_TLE_BYTES = 64_000
USER_AGENT = "HoneyBadger-TLE/1.0"


def files() -> list[Path]:
    d = C.state_dir()
    return [d / "tle.txt", d / "tle-upload.txt"]


def load(sats_cfg: list[dict] | None = None) -> dict[int, orbit.Satellite]:
    """{norad: Satellite} with the newest epoch per satellite across both files, limited to the
    configured satellites (all of them if sats_cfg is None). Names come from the settings."""
    want = {s["norad"]: s["name"] for s in sats_cfg} if sats_cfg else None
    best: dict[int, orbit.Satellite] = {}
    for f in files():
        try:
            raw = f.read_bytes()[:MAX_TLE_BYTES].decode("ascii", "replace")
        except OSError:
            continue
        sats, _ = orbit.parse_tle_text(raw)
        for s in sats:
            if want is not None and s.norad not in want:
                continue
            if s.norad not in best or s.epoch_jd > best[s.norad].epoch_jd:
                if want:
                    s.name = want[s.norad]
                best[s.norad] = s
    return best


def info(sats_cfg: list[dict], stale_days: float = 14, max_days: float = 60, now: float | None = None) -> list[dict]:
    """Per configured satellite: loaded?, epoch, age_days, approximate (stale), too_old."""
    now = now or time.time()
    have = load(sats_cfg)
    out = []
    for s in sats_cfg:
        sat = have.get(s["norad"])
        if not sat:
            out.append({"name": s["name"], "norad": s["norad"], "loaded": False})
            continue
        age = (now - sat.epoch_unix) / 86400.0
        out.append({"name": s["name"], "norad": s["norad"], "loaded": True, "epoch": round(sat.epoch_unix),
                    "age_days": round(age, 1), "approximate": age > stale_days, "too_old": age > max_days})
    return out


def _merge_write(path: Path, new: list[orbit.Satellite]) -> None:
    """Write `new` into `path`, keeping what was there for satellites not in `new`."""
    keep: dict[int, orbit.Satellite] = {}
    try:
        old, _ = orbit.parse_tle_text(path.read_bytes()[:MAX_TLE_BYTES].decode("ascii", "replace"))
        keep = {s.norad: s for s in old}
    except OSError:
        pass
    for s in new:
        if s.norad not in keep or s.epoch_jd >= keep[s.norad].epoch_jd:
            keep[s.norad] = s
    def label(s):
        n = " ".join(s.name.split()) or f"NORAD {s.norad}"
        return n if not n.startswith(("1 ", "2 ")) else f"SAT {n}"
    text = "".join(f"{label(s)}\n{s.line1}\n{s.line2}\n" for s in sorted(keep.values(), key=lambda s: s.norad)[:100])
    C.atomic_write(path, text, 0o644)


def import_text(text: str, dest: Path | None = None, allowed: set[int] | None = None) -> tuple[list[str], list[str]]:
    """Validate and store pasted/imported TLE text. Returns (names stored, problems)."""
    if len(text) > MAX_TLE_BYTES:
        return [], ["too much text — paste just the satellites you need"]
    sats, problems = orbit.parse_tle_text(text, limit=50)
    if allowed is not None:
        skipped = [s for s in sats if s.norad not in allowed]
        sats = [s for s in sats if s.norad in allowed]
        problems += [f"{s.name}: not in the satellite list (NORAD {s.norad}) — skipped" for s in skipped]
    now = time.time()
    future = [s for s in sats if s.epoch_unix > now + 3 * 86400]
    sats = [s for s in sats if s not in future]
    problems += [f"{s.name}: dated in the future — check the Badger's clock" for s in future]
    if sats:
        _merge_write(dest or files()[1], sats)
    return [s.name for s in sats], problems


def fetch(sats_cfg: list[dict], url_tpl: str, dest: Path | None = None, timeout: float = 20,
          opener=urllib.request.urlopen) -> tuple[list[str], list[str]]:
    """Download one TLE per configured satellite. Returns (names updated, problems)."""
    got, problems = [], []
    for s in sats_cfg:
        url = url_tpl.replace("{norad}", str(int(s["norad"])))
        if not url.startswith("https://"):
            problems.append(f"{s['name']}: only https:// TLE sources are used")
            continue
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with opener(req, timeout=timeout) as r:
                raw = r.read(MAX_TLE_BYTES).decode("ascii", "replace")
        except (urllib.error.URLError, OSError, TimeoutError, ValueError) as e:
            problems.append(f"{s['name']}: {getattr(e, 'reason', e)}")
            continue
        sats, bad = orbit.parse_tle_text(raw, limit=5)
        match = [x for x in sats if x.norad == s["norad"]]
        if not match:
            problems.append(f"{s['name']}: no usable TLE in the answer" + (f" ({bad[0]})" if bad else ""))
            continue
        sat = max(match, key=lambda x: x.epoch_jd)
        sat.name = s["name"]
        got.append(sat)
    if got:
        _merge_write(dest or files()[0], got)
    return [s.name for s in got], problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch")
    imp = sub.add_parser("import")
    imp.add_argument("file")
    sub.add_parser("show")
    a = ap.parse_args(argv)
    cfg = C.settings()["satellites"]
    if a.cmd == "fetch":
        names, problems = fetch(cfg["list"], cfg["tle_url"])
        for p in problems:
            print(f"tle: {p}", file=sys.stderr)
        if names:
            print("tle: updated " + ", ".join(names))
        return 0                                  # offline is normal for the Badger: never a failed unit
    if a.cmd == "import":
        try:
            text = Path(a.file).read_bytes()[:MAX_TLE_BYTES + 1].decode("ascii", "replace")
        except OSError as e:
            print(f"tle: {e}", file=sys.stderr)
            return 1
        names, problems = import_text(text, allowed={s["norad"] for s in cfg["list"]})
        for p in problems:
            print(f"tle: {p}", file=sys.stderr)
        print("tle: imported " + (", ".join(names) or "nothing"))
        return 0 if names else 1
    for row in info(cfg["list"], cfg["tle_stale_days"], cfg["tle_max_days"]):
        if not row["loaded"]:
            print(f"{row['name']:<16} no orbit data")
        else:
            flag = " (too old to use)" if row["too_old"] else (" (approximate)" if row["approximate"] else "")
            print(f"{row['name']:<16} {row['age_days']:>6.1f} days old{flag}")
    return 0


if __name__ == "__main__":
    os.umask(0o027)
    sys.exit(main())
