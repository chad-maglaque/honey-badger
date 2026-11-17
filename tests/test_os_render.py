#!/usr/bin/env python3
"""
Tests for os/render.py: every system file rendered from prepper.toml.
No root, no network.   python3.11 tests/test_os_render.py -v
"""
from __future__ import annotations

import ipaddress
import re
import sys
import tomllib
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from os_helpers import PY, REPO, TempDirCase, example_config, run  # noqa: E402

sys.path.insert(0, str(REPO / "os"))
import render  # noqa: E402

FILES = {
    "hostapd": "etc/hostapd/prepper.conf",
    "dnsmasq": "etc/dnsmasq.d/prepper.conf",
    "nginx": "etc/nginx/sites-available/prepper",
    "env": "etc/default/prepper",
    "nm": "etc/NetworkManager/conf.d/99-prepper.conf",
    "chrony": "etc/chrony/conf.d/prepper-gps.conf",
    "timer": "etc/systemd/system/upd-apply.timer.d/prepper-window.conf",
    "hostname": "etc/hostname",
    "hosts": "etc/hosts",
}


def kv(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for line in text.splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out.setdefault(k, []).append(v)
    return out


class RenderCLI(TempDirCase):
    """Runs render.py as prepperctl does, into a scratch root."""

    def render(self, text: str, iface: str = "wlan0", expect_rc: int = 0, marker: bool = False):
        cfg = self.tmp / "prepper.toml"
        cfg.write_text(text)
        root = self.tmp / "root"
        if marker:
            (root / "var/lib/prepper").mkdir(parents=True, exist_ok=True)
            (root / "var/lib/prepper/ap-wlan0").touch()
        r = run([PY, str(REPO / "os/render.py"), "--config", str(cfg), "--root", str(root), "--ap-iface", iface])
        self.assertEqual(r.returncode, expect_rc, r.stdout + r.stderr)
        self.r = r
        if expect_rc:
            return None
        return {k: (root / p).read_text() for k, p in FILES.items()}

    # — the shipped example —
    def test_example_config_onboard(self):
        f = self.render(example_config())
        h = kv(f["hostapd"])
        self.assertEqual(h["interface"], ["wlan0"])
        self.assertEqual(h["ssid"], ["HoneyBadger"])
        self.assertEqual(h["hw_mode"], ["g"])
        self.assertEqual(h["channel"], ["6"])
        self.assertNotIn("wpa", h)                       # empty passphrase = open network
        self.assertIn("proxy_pass http://127.0.0.1:8090;", f["nginx"])
        self.assertIn("location ~ ^/content/kolibri(/|$) { deny all; }", f["nginx"])
        self.assertIn("alias /srv/prepper/;", f["nginx"])
        self.assertIn("no-resolv", f["dnsmasq"])
        self.assertIn("address=/#/10.10.10.10", f["dnsmasq"])
        self.assertEqual(f["hostname"], "badger\n")
        self.assertIn("10.10.10.10\tbadger.lan badger.local box.lan", f["hosts"])
        self.assertIn("# No window", f["timer"])
        # onboard radio, no marker yet: wlan0 stays with NetworkManager (uplink)
        self.assertNotIn("unmanaged-devices", f["nm"])

    def test_file_modes(self):
        self.render(example_config(network={"passphrase": "correct horse"}))
        root = self.tmp / "root"
        self.assertEqual((root / FILES["hostapd"]).stat().st_mode & 0o777, 0o600)    # holds the passphrase
        self.assertEqual((root / FILES["nginx"]).stat().st_mode & 0o777, 0o644)
        self.assertEqual([p.name for p in (root / "etc/hostapd").iterdir()], ["prepper.conf"])  # no temp left

    def test_rerender_is_idempotent(self):
        a = self.render(example_config())
        b = self.render(example_config())
        self.assertEqual(a, b)

    # — Wi-Fi security per interface —
    def test_passphrase_wlan0_wpa2_only(self):
        h = kv(self.render(example_config(network={"passphrase": "12345678"}))["hostapd"])
        self.assertEqual(h["wpa"], ["2"])
        self.assertEqual(h["wpa_passphrase"], ["12345678"])
        self.assertEqual(h["wpa_key_mgmt"], ["WPA-PSK"])           # brcmfmac AP: no SAE
        self.assertNotIn("sae_require_mfp", h)
        self.assertNotIn("ieee80211w", h)

    def test_passphrase_usb_wpa2_wpa3(self):
        h = kv(self.render(example_config(network={"passphrase": "x" * 63}), iface="wlan1")["hostapd"])
        self.assertEqual(h["wpa_passphrase"], ["x" * 63])
        self.assertEqual(h["wpa_key_mgmt"], ["WPA-PSK SAE"])
        self.assertEqual(h["ieee80211w"], ["1"])
        self.assertEqual(h["hw_mode"], ["a"])
        self.assertEqual(h["channel"], ["36"])
        self.assertEqual(h["ht_capab"], ["[HT40+][SHORT-GI-20][SHORT-GI-40]"])

    def test_usb_24ghz_when_channel_5ghz_zero(self):
        h = kv(self.render(example_config(network={"channel_5ghz": 0}), iface="wlx00c0ca123456")["hostapd"])
        self.assertEqual(h["hw_mode"], ["g"])
        self.assertEqual(h["interface"], ["wlx00c0ca123456"])

    def test_usb_without_channel_5ghz_key(self):
        # Used to raise KeyError: the default (36) was used for the test but n["channel_5ghz"] for the value.
        text = re.sub(r"(?m)^channel_5ghz.*\n", "", example_config())
        h = kv(self.render(text, iface="wlan1")["hostapd"])
        self.assertEqual(h["channel"], ["36"])

    def test_ht40_direction(self):
        self.assertEqual(render.ht40(36), "[HT40+]")
        self.assertEqual(render.ht40(40), "[HT40-]")
        self.assertEqual(render.ht40(149), "[HT40+]")
        self.assertEqual(render.ht40(153), "[HT40-]")
        self.assertEqual(render.ht40(165), "")
        h = kv(self.render(example_config(network={"channel_5ghz": 40}), iface="wlan1")["hostapd"])
        self.assertTrue(h["ht_capab"][0].startswith("[HT40-]"))

    def test_passphrase_limits(self):
        for bad in ("1234567", "x" * 64):
            self.render(example_config(network={"passphrase": bad}), expect_rc=2)
            self.assertIn("passphrase", self.r.stderr)
            self.assertFalse((self.tmp / "root" / FILES["hostapd"]).exists(), "nothing written on error")

    def test_newline_injection_rejected(self):
        cases = [
            {"passphrase": "goodpass\nwpa=0"},
            {"passphrase": "pässwörd1"},                        # not ASCII: hostapd wants 8–63 ASCII
            {"ssid": "Badger\ninterface=eth0"},
            {"ssid": "Badger\rx"},
            {"ssid": "Badger x"},
            {"ssid": ""},
            {"ssid": "x" * 33},
            {"ssid": "é" * 17},                                 # 17 chars, 34 bytes
            {"hostname": "bad\nhost"},
            {"hostname": "bad host"},
            {"hostname": "-bad"},
            {"country": "us\n"},
            {"country": "USA"},
            {"address": "10.10.10.10\nserver=6.6.6.6"},
            {"address": "10.10.10.255"},
            {"dhcp_range": ["10.10.10.50", "10.10.11.250"]},
            {"dhcp_range": ["10.10.10.250", "10.10.10.50"]},
            {"dhcp_range": ["10.10.10.5", "10.10.10.20"]},      # contains the AP address
            {"captive": "popup; return 200"},
            {"max_clients": "64\n"},
        ]
        for net in cases:
            with self.subTest(net=net):
                self.render(example_config(network=net), expect_rc=2)
        for upd in ({"data_root": "/srv/my data"}, {"data_root": "/srv/x;}"}, {"data_root": "relative"},
                    {"data_root": "/srv/../etc"}):
            with self.subTest(upd=upd):
                self.render(example_config(upd=upd), expect_rc=2)
        for ai in ({"model": "x$(reboot)"}, {"model": "a b"}, {"endurance_model": "x\nPATH=/tmp"},
                   {"threads": "4; rm"}):
            with self.subTest(ai=ai):
                self.render(example_config(ai=ai), expect_rc=2)
        self.render(example_config(), iface="wlan0\nx", expect_rc=2)
        self.render(example_config(), iface="../etc", expect_rc=2)

    def test_ssid_spaces_quotes_unicode(self):
        f = self.render(example_config(network={"ssid": 'Bob\'s "Net" #1'}))
        self.assertIn('ssid=Bob\'s "Net" #1\n', f["hostapd"])
        f = self.render(example_config(network={"ssid": " edge "}))
        h = kv(f["hostapd"])
        self.assertEqual(h["ssid2"], [b" edge ".hex()])
        self.assertNotIn("ssid", h)
        self.assertNotIn("utf8_ssid", h)
        f = self.render(example_config(network={"ssid": "Café 🦡"}))
        h = kv(f["hostapd"])
        self.assertEqual(bytes.fromhex(h["ssid2"][0]).decode(), "Café 🦡")
        self.assertEqual(h["utf8_ssid"], ["1"])
        # every rendered file is still one directive per line
        for text in f.values():
            self.assertNotIn("\r", text)

    def test_passphrase_with_shell_and_quote_chars(self):
        psk = 'a"b\'c $(id) `x` #;\\'
        h = self.render(example_config(network={"passphrase": psk}))["hostapd"]
        self.assertIn(f"wpa_passphrase={psk}\n", h)
        self.assertEqual(h.count("\n"), len(h.splitlines()))

    # — consistency of addresses across files —
    def test_subnet_consistent(self):
        net = {"address": "192.168.77.1", "dhcp_range": ["192.168.77.100", "192.168.77.200"], "hostname": "den"}
        f = self.render(example_config(network=net), iface="wlan1")
        env = dict(line.split("=", 1) for line in f["env"].splitlines() if line)
        self.assertEqual(env["AP_ADDRESS"], "192.168.77.1")
        self.assertEqual(env["AP_SUBNET"], "192.168.77.0/24")
        self.assertEqual(env["AP_IFACE"], "wlan1")
        d = kv(f["dnsmasq"])
        self.assertEqual(d["interface"], ["wlan1"])
        self.assertEqual(d["dhcp-range"], ["192.168.77.100,192.168.77.200,255.255.255.0,12h"])
        self.assertIn("option:router,192.168.77.1", d["dhcp-option"])
        net_ = ipaddress.ip_network(env["AP_SUBNET"])
        for ip in ("192.168.77.100", "192.168.77.200", env["AP_ADDRESS"]):
            self.assertIn(ipaddress.ip_address(ip), net_)
        self.assertIn("allow 192.168.77.0/24", f["chrony"])
        self.assertIn("return 302 http://den.lan/#/devices;", f["nginx"])
        self.assertIn("192.168.77.1\tden.lan den.local box.lan", f["hosts"])
        self.assertEqual(kv(f["hostapd"])["interface"], ["wlan1"])

    def test_env_file_is_inert_for_bash(self):
        f = self.render(example_config(upd={"data_root": "/mnt/data-1/prepper"}))
        for line in f["env"].splitlines():
            if line:
                self.assertRegex(line, r"^[A-Z_]+=[A-Za-z0-9._/+:-]+$")
        self.assertIn("LLAMA_MODEL=/mnt/data-1/prepper/models/model_qwen3_4b.current", f["env"])

    # — NetworkManager —
    def test_network_manager_usb_releases_only_adapter(self):
        f = self.render(example_config(), iface="wlan1")
        self.assertIn("unmanaged-devices=interface-name:wlan1", f["nm"])
        self.assertNotIn("wlan0", f["nm"])

    def test_network_manager_wlan0_after_marker(self):
        f = self.render(example_config(), iface="wlan0", marker=True)
        self.assertIn("[keyfile]\nunmanaged-devices=interface-name:wlan0", f["nm"])

    # — captive / https / uplink —
    def test_tls_only_with_certificate(self):
        n = self.render(example_config(network={"https": True}))["nginx"]
        self.assertNotIn("listen 443", n)
        self.assertIn("no TLS certificate yet", self.r.stderr)
        tls = self.tmp / "root/etc/prepper/tls"
        tls.mkdir(parents=True)
        (tls / "server.crt").write_text("x"); (tls / "server.key").write_text("x")
        n = self.render(example_config(network={"https": True}))["nginx"]
        self.assertIn("listen 443 ssl default_server;", n)

    def test_api_forwards_host(self):
        n = self.render(example_config())["nginx"]
        api = n.split("location /api/ {", 1)[1].split("}", 1)[0]
        self.assertIn("proxy_set_header Host $host;", api)
        self.assertIn("proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;", api)
        self.assertIn("client_body_timeout 10s;", n)

    def test_upload_size_only_raised_for_the_api(self):
        n = self.render(example_config())["nginx"]
        api = n.split("location /api/ {", 1)[1].split("\n    }", 1)[0]
        self.assertIn("client_max_body_size 26m;", api)
        server_level = n.split("location", 1)[0]
        self.assertIn("client_max_body_size 1m;", server_level)
        self.assertEqual(n.count("client_max_body_size"), 2)          # 1m everywhere, 26m for /api/ only

    def test_api_passes_scheme_and_drops_the_panel_key(self):
        api = self.render(example_config())["nginx"].split("location /api/ {", 1)[1].split("\n    }", 1)[0]
        self.assertIn("proxy_set_header X-Forwarded-Proto $scheme;", api)
        self.assertIn('proxy_set_header X-Badger-Panel "";', api)     # a phone can never present the panel key

    def test_third_party_apps_cannot_call_the_launcher_api(self):
        n = self.render(example_config())["nginx"]
        for prefix in ("/ai/", "/learn/", "/books/", "/video/", "/kiwix/"):
            block = n.split(f"location {prefix} ", 1)[1].split("\n    location", 1)[0]
            with self.subTest(prefix=prefix):
                self.assertIn(f"connect-src $scheme://$host{prefix} ws://$host{prefix} wss://$host{prefix};", block)
                self.assertIn(f"frame-src $scheme://$host{prefix};", block)
                self.assertNotIn("/api/", block)
        apps = n.split("location /apps/", 1)[1].split("\n", 1)[0]
        self.assertNotIn("connect-src", apps)                        # the trusted viewers fetch /content/

    def test_untrusted_files_sandboxed(self):
        n = self.render(example_config())["nginx"]
        csp = 'Content-Security-Policy "sandbox allow-scripts allow-popups allow-forms allow-downloads" always'
        for loc in ("location /files/", "location /sync/", "location /content/", "location /kiwix/content/"):
            block = n.split(loc, 1)[1].split("\n    location /", 1)[0]
            self.assertIn(csp, block, loc)
            self.assertIn("X-Content-Type-Options nosniff", block, loc)
        self.assertIn("location ~* \\.(html?|xhtml|svg|svgz|xml|xsl)$", n.split("location /files/", 1)[1])
        apps = n.split("location /apps/", 1)[1].split("\n", 1)[0]
        self.assertNotIn("sandbox", apps, "the vendored viewers need the launcher's origin")
        self.assertIn("proxy_pass http://127.0.0.1:8081;", n.split("location /kiwix/content/", 1)[1])

    def test_force_24ghz(self):
        cfg = self.tmp / "c.toml"
        cfg.write_text(example_config())
        r = run([PY, str(REPO / "os/render.py"), "--config", str(cfg), "--root", str(self.tmp / "r"),
                 "--ap-iface", "wlan1", "--force-24ghz"])
        self.assertEqual(r.returncode, 0, r.stderr)
        h = kv((self.tmp / "r" / FILES["hostapd"]).read_text())
        self.assertEqual((h["hw_mode"], h["channel"]), (["g"], ["6"]))

    def test_quiet_captive_probes(self):
        f = self.render(example_config(network={"captive": "quiet", "https": False}))
        n = f["nginx"]
        self.assertIn("location = /generate_204 { return 204; }", n)
        self.assertIn('location = /success.txt { default_type text/plain; return 200 "success\\n"; }', n)
        self.assertIn('return 200 "<meta http-equiv=\\"refresh\\"', n)
        self.assertNotIn("listen 443", n)
        self.assertEqual(n.count("{"), n.count("}"))

    def test_share_uplink_dns(self):
        d = self.render(example_config(network={"share_uplink": True}))["dnsmasq"]
        self.assertIn("server=1.1.1.1", d)
        self.assertNotIn("address=/#/", d)
        # a string "false" is not true
        d = self.render(example_config(network={"share_uplink": "false"}))["dnsmasq"]
        self.assertIn("no-resolv", d)

    def test_uplink_wifi_never_rendered_into_system_files(self):
        text = example_config(network={"uplink_wifi": [{"ssid": "Home\nx", "psk": "uplinksecret"}]})
        f = self.render(text, iface="wlan1")
        for name, body in f.items():
            self.assertNotIn("uplinksecret", body, name)

    # — upd timer drop-in —
    def test_window(self):
        t = self.render(example_config(upd={"window": "01:00-06:00"}))["timer"]
        self.assertIn("[Timer]\nOnCalendar=\nOnCalendar=Sun 01:00\nRandomizedDelaySec=120min\n", t)
        t = self.render(example_config(upd={"window": "23:30-00:30"}))["timer"]
        self.assertIn("OnCalendar=Sun 23:30\nRandomizedDelaySec=30min", t)

    def test_window_malformed_ignored(self):
        for w in ("25:00-06:00", "01:00-06:75", "soon", "01:00-06:00\nOnCalendar=minutely"):
            with self.subTest(w=w):
                t = self.render(example_config(upd={"window": w}))["timer"]
                self.assertIn("# No window", t)
                self.assertNotIn("OnCalendar=", t)

    def test_unparseable_config(self):
        self.render("this is = = not toml", expect_rc=2)
        self.assertIn("cannot read", self.r.stderr)


class RenderUnits(unittest.TestCase):
    """The renderer functions directly (what tests and prepperctl rely on)."""

    def cfg(self, **sections):
        c = tomllib.loads(example_config(**sections))
        return c

    def test_check_accepts_example(self):
        render.check(self.cfg(), "wlan0")

    def test_eight_and_63(self):
        for psk in ("a" * 8, "a" * 63):
            self.assertIn(f"wpa_passphrase={psk}", render.hostapd(self.cfg(network={"passphrase": psk}), "wlan1"))

    def test_hostapd_rejects_directly(self):
        with self.assertRaises(render.ConfigError):
            render.hostapd(self.cfg(network={"passphrase": "short"}), "wlan0")
        with self.assertRaises(render.ConfigError):
            render.hostapd(self.cfg(network={"ssid": "a\nb"}), "wlan0")

    def test_nginx_str(self):
        self.assertEqual(render._nginx_str('a"b\\c\n'), '"a\\"b\\\\c\\n"')


if __name__ == "__main__":
    unittest.main()
