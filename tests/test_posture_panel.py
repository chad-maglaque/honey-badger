"""
Front panel: the posture on the home screen, the Posture menu (behind the panel code), the
"What it reveals" screen with its fixes, and the circle link QR — against a tiny fake portal.
Run: python3.11 tests/test_posture_panel.py
"""
from __future__ import annotations

import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "software" / "panel"))
TMP = Path(tempfile.mkdtemp(prefix="posture-panel-"))
os.environ.setdefault("PANEL_TIMEFMT", str(TMP / "timefmt"))
import panel  # noqa: E402


class FakeDisplay:
    def __init__(self):
        self.qrs, self.texts = [], []

    def lines(self, rows, title=None, sel=None):
        self.texts.append((list(rows), title, sel))

    def qr(self, data, caption):
        self.qrs.append((data, caption))

    def sleep(self, on):
        pass


class Portal:
    def __init__(self):
        self.level = "guarded"
        self.posts = []
        me = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, obj, code=200):
                raw = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.headers.get("X-Badger-Panel") != "k":
                    return self.reply({"error": "no"}, 403)
                if self.path == "/api/status":
                    return self.reply({"posture": {"level": me.level, "label": me.level.title()}, "services": {}})
                if self.path == "/api/posture":
                    return self.reply({"level": me.level})
                if self.path == "/api/reveal":
                    return self.reply({"items": [
                        {"id": "wifi_password", "state": "warn", "title": "Wi-Fi has no password",
                         "fix": {"action": "wifi_password", "label": "Give the Wi-Fi a password"}},
                        {"id": "posture", "state": "ok", "title": "Posture: Guarded", "fix": None},
                        {"id": "wifi_name", "state": "warn", "title": "Wi-Fi name draws ‮attention", "fix": None}, 5]})
                return self.reply({"error": "not found"}, 404)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                me.posts.append((self.path, body))
                if self.path == "/api/posture/panel":
                    me.level = body["level"]
                    return self.reply({"ok": True, "wifi": {"action": "password", "ok": True} if body["level"] == "lockdown" else None})
                if self.path == "/api/reveal/fix":
                    return self.reply({"ok": True, "message": "The Wi-Fi gets a password in about 10 seconds."})
                return self.reply({"error": "no"}, 404)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class PanelPosture(unittest.TestCase):
    def setUp(self):
        self.portal = Portal()
        self.old = (panel.API, dict(panel._key_cache))
        panel.API = self.portal.url
        panel._key_cache["key"] = "k"
        self.tmp = Path(tempfile.mkdtemp(prefix="pp-"))
        self.d = FakeDisplay()
        self.p = panel.Panel(self.d, {"network": {"ssid": "Home-K7QX"}, "portal": {"run_dir": str(self.tmp)},
                                      "panel": {"lock_file": str(self.tmp / "lock.json")}})

    def tearDown(self):
        panel.API = self.old[0]
        panel._key_cache.clear()
        panel._key_cache.update(self.old[1])
        self.portal.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def labels(self):
        return [r[0] for r in self.p.stack[-1][1]]

    def press_row(self, text):
        rows = self.p.stack[-1][1]
        i = [r[0] for r in rows].index(text)
        self.p.sel = i
        self.p.press()

    def test_home_shows_posture(self):
        self.p.draw()
        self.assertIn("Posture: Guarded", self.d.texts[-1][0])
        self.portal.level = "lockdown"
        self.p.draw()
        self.assertIn("Posture: Lockdown", self.d.texts[-1][0])

    def test_menu_and_change(self):
        self.p.press()                                      # home → menu
        self.assertIn("Posture: Guarded", self.labels())
        self.press_row("Posture: Guarded")
        self.assertEqual(self.p.stack[-1][0], "Posture")
        self.assertEqual(self.labels()[0], "Now: Guarded")
        self.press_row("Lockdown")
        self.assertEqual(self.p.screen, "confirm")
        self.p.sel = 1
        self.p.press()                                      # Yes
        self.assertEqual(self.portal.posts[-1], ("/api/posture/panel", {"level": "lockdown"}))
        self.assertIn("Lockdown.", " ".join(self.d.texts[-1][0]))

    def test_lockdown_wifi_off_choice(self):
        self.p.posture_menu()
        self.press_row("Lockdown, Wi-Fi off")
        self.p.sel = 1
        self.p.press()
        self.assertEqual(self.portal.posts[-1][1], {"level": "lockdown", "wifi": "off"})

    def test_posture_behind_the_code(self):
        self.p.plock.set("2468")
        self.p.press()
        self.press_row("Posture: Guarded")
        self.assertEqual(self.p.screen, "code")
        self.assertEqual(self.portal.posts, [])

    def test_reveal_screen_and_fix(self):
        self.p.reveal_screen()
        rows = self.labels()
        self.assertEqual(rows[:3], ["! Wi-Fi has no password", "ok Posture: Guarded", "! Wi-Fi name draws attention"])
        self.press_row("! Wi-Fi has no password")
        self.assertEqual(self.p.screen, "confirm")
        self.p.sel = 1
        self.p.press()
        self.assertEqual(self.portal.posts[-1], ("/api/reveal/fix", {"action": "wifi_password"}))
        self.press_row("! Wi-Fi name draws attention") if self.p.screen == "menu" else None

    def test_reveal_screen_when_portal_down(self):
        panel.API = "http://127.0.0.1:9"
        self.p.reveal_screen()
        self.assertIn("Can't check now:", self.d.texts[-1][0])

    def test_circle_link_qr(self):
        self.p.circle_link_qr()
        self.assertEqual(self.d.qrs, [])
        (self.tmp / "mesh-circle.json").write_text(json.dumps({"url": "https://meshtastic.org/e/#CIRCLE", "at": time.time()}))
        self.p.circle_link_qr()
        self.assertEqual(self.d.qrs[-1][0], "https://meshtastic.org/e/#CIRCLE")
        self.p.plock.set("2468")
        self.p.unlocked_until = 0
        self.p.circle_link_qr()
        self.assertEqual(self.p.screen, "code")

    def test_radio_files_default_to_ram(self):
        p = panel.Panel(FakeDisplay(), {})
        self.assertEqual(str(p.link_file), "/run/prepper/mesh-channel.json")
        self.assertEqual(str(p.bt_pin_file), "/run/prepper/mesh-bt-pin.json")
        p = panel.Panel(FakeDisplay(), {"portal": {"mesh_channel_link": "/x/link.json"}})
        self.assertEqual(str(p.link_file), "/x/link.json")
        self.assertIn("posture", panel.DEFAULT_PROTECT)


if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
