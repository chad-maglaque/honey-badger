#!/usr/bin/env python3
"""
Build a fake installed Badger in DIR and print the command to run the launcher
against it on a laptop (standalone mode, no nginx/kiwix/llama needed).

  python3 tests/dev_fixture.py /tmp/prepper-dev
  PREPPER_CONFIG=/tmp/prepper-dev/prepper.toml python3 software/portal/server.py
  open http://127.0.0.1:8765          (admin PIN 123456)
"""
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "software/upd"))
import upd  # noqa: E402

root = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/prepper-dev").resolve()
data = root / "data"
state = root / "state"
for d in ("zim", "maps", "docs/hesperian_books/Where There Is No Doctor", "models", "apk", "video/s2"):
    (data / d).mkdir(parents=True, exist_ok=True)
state.mkdir(parents=True, exist_ok=True)
portal = state / "portal"           # mirrors /var/lib/prepper/portal/ (the portal's writable files)
portal.mkdir(exist_ok=True)
PIN = "123456"                      # the portal refuses PINs shorter than 6 characters

installed = {
    "mdwiki_en_all_maxi": ("zim", "mdwiki_en_all_maxi_2025-11.zim"),
    "ifixit_en_all": ("zim", "ifixit_en_all_2025-12.zim"),
    "armypubs_en_all": ("zim", "armypubs_en_all_2024-12.zim"),
    "ready.gov_en_all": ("zim", "ready.gov_en_all_2026-10.zim"),
    "wikipedia_en_all_maxi": ("zim", "wikipedia_en_all_maxi_2026-08.zim"),
    "zimgit-water_en": ("zim", "zimgit-water_en_2024-08.zim"),
    "maps_north_america": ("maps", "maps_north_america.pmtiles"),
    "model_qwen25_1_5b": ("models", "qwen2.5-1.5b-instruct-q4_k_m.gguf"),
    "apk_trail_sense": ("apk", "com.kylecorry.trail_sense_133.apk"),
}
items = {}
for item_id, (sub, fn) in installed.items():
    (data / sub / fn).write_bytes(b"fake")
    m = __import__("re").search(r"_(\d{4}-\d{2})\.zim$", fn)
    items[item_id] = {"version": m.group(1) if m else "1", "filename": fn, "path": f"{sub}/{fn}",
                      "size": 4, "installed": upd.now_iso(), "origin": "upstream", "method": "http"}
for n in ("Chapter 1 - Health care.pdf", "Chapter 10 - First aid.pdf"):
    (data / "docs/hesperian_books/Where There Is No Doctor" / n).write_bytes(b"%PDF-1.4 fake")
items["hesperian_books"] = {"version": "manual", "filename": "hesperian_books", "path": "docs/hesperian_books",
                            "size": 26, "installed": upd.now_iso(), "origin": "manual"}
(state / "upd-state.json").write_text(json.dumps({"items": items, "history": [
    {"at": upd.now_iso(), "id": "ready.gov_en_all", "event": "updated", "version": "2026-10"}]}, indent=2))
(state / "reports").mkdir(exist_ok=True)
(state / "reports/last-check.json").write_text(json.dumps({
    "generated": upd.now_iso(), "tier": "base", "download_human": "3.4 GB",
    "counts": {"up-to-date": 41, "update": 2, "install": 1, "manual": 3, "offline": 0, "error": 0},
    "items": [{"id": "ifixit_en_all", "title": "iFixit repair guides", "status": "update", "method": "rsync-fuzzy",
               "local_version": "2025-12", "remote_version": "2026-03", "download_human": "3.5 GB", "note": "+1,204 articles"}]}))
(state / "build.json").write_text(json.dumps({"phase": "content", "pct": 42, "message": "Downloading Wikipedia (English, complete)",
                                              "done": 61, "total": 115, "current": "wikipedia_en_all_maxi"}))
(portal / "board.jsonl").write_text("\n".join(json.dumps(m) for m in [
    {"at": "2026-11-16T18:02:00-0700", "name": "Garcia family", "text": "All 5 OK. At the church on 3rd St. Have extra water.", "via": "local"},
    {"at": "2026-11-16T18:20:00-0700", "name": "K7ABC", "text": "Bridge on Route 9 is out. Use county road 12.", "via": "mesh-in"},
]) + "\n")
(root / "power.json").write_text(json.dumps({"percent": 76.5, "voltage": 3.94, "external": False, "charging": False,
                                             "runtime_min": 412, "mode": "balanced", "at": int(time.time()),
                                             "input_v": 19.8, "input_a": 0.71, "input_w": 14.1}))
(root / "alerts.json").write_text(json.dumps([{"id": "t1", "code": "SVR", "event": "Severe Thunderstorm Warning",
    "areas": "3 areas in CO", "expires": "Mon 21:30", "expires_ts": time.time() + 7200,
    "text": "Tune a weather radio for details."}]))
(root / "prepper.toml").write_text(f"""
[network]
hostname = "badger"
ssid = "HoneyBadger"

[upd]
data_root = "{data}"
catalog = "{REPO}/content/catalog.toml"
state_file = "{state}/upd-state.json"
report_dir = "{state}/reports"
tier = "base"
languages = ["eng"]

[portal]
listen = "127.0.0.1:8765"
standalone = true
static = "{REPO}/software/portal/static"
renderers = "{REPO}/content/renderers.toml"
settings_file = "{portal}/portal-settings.json"
features_data = "{portal}/features"
admin_pin = "{__import__('subprocess').run([sys.executable, str(REPO / 'software/portal/server.py'), 'hash-pin', PIN], capture_output=True, text=True).stdout.strip()}"
power_file = "{root}/power.json"
alerts_file = "{root}/alerts.json"
build_file = "{state}/build.json"
board_file = "{portal}/board.jsonl"
mesh_outbox = "{portal}/mesh-outbox.jsonl"
family_board_file = "{portal}/family-board.jsonl"
members_file = "{portal}/members.json"
mesh_nodes = "{portal}/mesh-nodes.json"
ctl = "/nonexistent/prepperctl"
""")
print(f"PREPPER_CONFIG={root}/prepper.toml python3 {REPO}/software/portal/server.py   # admin PIN {PIN}")
