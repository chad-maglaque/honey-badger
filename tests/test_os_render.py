#!/usr/bin/env python3
"""
Tests for os/render.py: every system file rendered from prepper.toml.
No root, no network.   python3.11 tests/test_os_render.py -v
"""
from __future__ import annotations

import ipaddress
import json
import re
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from os_helpers import BASH, PY, REPO, TempDirCase, example_config, run  # noqa: E402

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
        self.assertEqual(h["ssid"], ["Home-Hotspot"])                # a neutral name (red-team finding 2)
        self.assertEqual(h["hw_mode"], ["g"])
        self.assertEqual(h["channel"], ["6"])
        # empty passphrase = Enhanced Open, which the onboard radio can't do: open, and it says so
        self.assertNotIn("wpa", h)
        self.assertIn("# security = owe is not supported by the onboard radio", f["hostapd"])
        self.assertEqual(h["ap_isolate"], ["1"])
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

    # — owe / owe-transition —
    def test_owe_only_is_a_single_owe_bss(self):
        h = self.render(example_config(network={"security": "owe", "passphrase": ""}), iface="wlan1")["hostapd"]
        self.assertNotIn("bss=", h)
        self.assertIn("wpa_key_mgmt=OWE", h)
        self.assertNotIn("owe_transition", h)

    def test_owe_transition_usb_two_bridged_bsses(self):
        f = self.render(example_config(network={"security": "owe-transition", "passphrase": "", "ssid": "Home-7Q4K"}),
                        iface="wlan1")
        first, second = f["hostapd"].split("bss=ap-owe\n")
        a, b = kv(first), kv(second)
        self.assertEqual(a["interface"], ["wlan1"])
        self.assertEqual(a["ssid"], ["Home-7Q4K"])                       # the open BSS keeps the real name
        self.assertNotIn("wpa", a)                                       # ...and is open
        self.assertEqual(a["owe_transition_ifname"], ["ap-owe"])
        self.assertEqual(a["bridge"], ["br-prepper"])
        self.assertEqual(a["ap_isolate"], ["1"])
        self.assertEqual(b["bridge"], ["br-prepper"])                    # both share one subnet
        self.assertEqual(b["ap_isolate"], ["1"])
        self.assertEqual(b["wpa_key_mgmt"], ["OWE"])
        self.assertEqual(b["ieee80211w"], ["2"])
        self.assertEqual(b["ignore_broadcast_ssid"], ["1"])              # the OWE twin is hidden
        self.assertEqual(b["owe_transition_ifname"], ["wlan1"])
        self.assertEqual(bytes.fromhex(b["ssid2"][0]), b"OWE-Home-7Q4K")
        self.assertNotIn("channel", b)                                   # radio settings only in the first section
        self.assertIn("EXPERIMENTAL", self.r.stderr)
        # DHCP, DNS and the captive portal listen on the bridge, not on the radio
        self.assertEqual(kv(f["dnsmasq"])["interface"], ["br-prepper"])
        env = kv(f["env"])
        self.assertEqual((env["AP_IFACE"], env["AP_LINK_IFACE"], env["AP_BRIDGE"]), (["wlan1"], ["br-prepper"], ["1"]))
        self.assertIn("interface-name:br-prepper", f["nm"])
        self.assertIn("interface-name:ap-owe", f["nm"])

    def test_owe_transition_long_ssid_fits(self):
        f = self.render(example_config(network={"security": "owe-transition", "passphrase": "", "ssid": "x" * 32}),
                        iface="wlan1")
        second = f["hostapd"].split("bss=ap-owe\n")[1]
        self.assertLessEqual(len(bytes.fromhex(kv(second)["ssid2"][0])), 32)

    def test_owe_transition_onboard_radio_falls_back_to_open(self):
        f = self.render(example_config(network={"security": "owe-transition", "passphrase": ""}), iface="wlan0")
        self.assertNotIn("bss=", f["hostapd"])
        self.assertNotIn("OWE", f["hostapd"].replace("owe-transition needs", ""))
        self.assertIn("owe-transition needs the USB adapter", f["hostapd"])
        self.assertEqual(kv(f["dnsmasq"])["interface"], ["wlan0"])
        self.assertEqual(kv(f["env"])["AP_BRIDGE"], ["0"])

    def test_non_transition_modes_are_not_bridged(self):
        for sec, psk in (("wpa2", "12345678"), ("owe", ""), ("open", "")):
            f = self.render(example_config(network={"security": sec, "passphrase": psk}), iface="wlan1")
            self.assertEqual(kv(f["env"])["AP_BRIDGE"], ["0"], sec)
            self.assertEqual(kv(f["dnsmasq"])["interface"], ["wlan1"], sec)
            self.assertNotIn("bridge=", f["hostapd"], sec)

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

    def test_api_limits_per_phone(self):
        n = self.render(example_config())["nginx"]
        head = n.split("server {", 1)[0]                      # http context (the site file is included there)
        self.assertIn("limit_conn_zone $binary_remote_addr zone=prepper_api_conn:1m;", head)
        self.assertIn('map $request_method $prepper_post_key { POST $binary_remote_addr; default ""; }', head)
        self.assertIn("limit_req_zone $prepper_post_key zone=prepper_api_post:1m rate=10r/s;", head)
        api = n.split("location /api/ {", 1)[1].split("\n    }", 1)[0]
        self.assertIn("limit_conn prepper_api_conn 20;", api)
        self.assertIn("limit_req zone=prepper_api_post burst=20 nodelay;", api)
        self.assertIn("limit_conn_status 429;", api)
        self.assertIn("limit_req_status 429;", api)
        self.assertIn("client_body_timeout 20s;", api)
        self.assertNotIn("client_body_timeout 60s", n)
        # the limits apply to /api/ only: static files, the library and the captive probes are untouched
        self.assertEqual(n.count("limit_conn prepper_api_conn"), 1)
        self.assertEqual(n.count("limit_req zone="), 1)

    def test_api_refusals_are_json(self):
        # nginx's own 413/429 (too big, too many) reach the launcher as JSON with plain words
        n = self.render(example_config())["nginx"]
        api = n.split("location /api/ {", 1)[1].split("\n    }", 1)[0]
        self.assertIn("error_page 413 = @api_too_large;", api)
        self.assertIn("error_page 429 = @api_busy;", api)
        for name, code in (("@api_too_large", "413"), ("@api_busy", "429")):
            loc = n.split(f"location {name} {{", 1)[1].split("\n    }", 1)[0]
            self.assertIn("default_type application/json;", loc)
            body = loc.split(f"return {code} '", 1)[1].rsplit("'", 1)[0]
            self.assertIn("error", json.loads(body))
        self.assertNotIn("proxy_intercept_errors", n)      # the portal's own JSON errors pass through untouched

    def test_kolibri_localhost_only(self):
        f = self.render(example_config())
        env = kv(f["env"])
        self.assertEqual(env["KOLIBRI_LISTEN_ADDRESS"], ["127.0.0.1"])
        self.assertEqual(env["KOLIBRI_HTTP_PORT"], ["8080"])
        self.assertEqual(env["KOLIBRI_ZIP_CONTENT_PORT"], ["8091"])
        self.assertEqual(env["KOLIBRI_ZIP_CONTENT_ORIGIN"], ["8089"])
        n = f["nginx"]
        self.assertIn("location /learn/ { proxy_pass http://127.0.0.1:8080;", n)
        zip_server = n.split("listen 8089;", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("proxy_pass http://127.0.0.1:8091;", zip_server)
        self.assertNotIn("listen 8080", n)
        self.assertNotIn("listen 8091", n)

    def test_hotspot_firewall_allows_exactly_nginx_ports(self):
        """prepperctl's hotspot allow-list must open every port nginx listens on, and nothing else on TCP
        but DNS."""
        tls = self.tmp / "root/etc/prepper/tls"
        tls.mkdir(parents=True)
        (tls / "server.crt").write_text("x"); (tls / "server.key").write_text("x")
        n = self.render(example_config(network={"https": True}))["nginx"]
        listen = {int(m) for m in re.findall(r"^\s*listen (?:\[::\]:)?(\d+)", n, re.M)}
        ctl = (REPO / "os/bin/prepperctl").read_text()
        tcp = {int(x) for x in re.search(r'HOTSPOT_TCP="([^"]*)"', ctl).group(1).split(",")}
        udp = {int(x) for x in re.search(r'HOTSPOT_UDP="([^"]*)"', ctl).group(1).split(",")}
        self.assertEqual(tcp - {53}, listen)
        self.assertEqual(udp, {53, 67, 123})
        self.assertNotIn(22, tcp)

    def test_upload_size_only_raised_for_the_api(self):
        n = self.render(example_config())["nginx"]
        api = n.split("location /api/ {", 1)[1].split("\n    }", 1)[0]
        self.assertIn("client_max_body_size 26m;", api)
        launcher = n.split("listen 80 default_server;", 1)[1]
        server_level = launcher.split("location", 1)[0]
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
        # red-team finding 8: /apps/ (code from upstream or a peer) runs on its own origin (port 8088/8443),
        # never on the launcher's; there it may fetch only from that origin (the library under /content/).
        apps_server = n.split("listen 8088;", 1)[1].split("\n}\n", 1)[0]
        apps = apps_server.split("location /apps/", 1)[1].split("\n", 1)[0]
        self.assertIn("connect-src 'self' blob: data:; frame-ancestors 'self'", apps)
        self.assertIn("location /content/", apps_server)
        self.assertNotIn("/api/", apps_server)
        launcher = n.split("listen 80 default_server;", 1)[1]
        self.assertIn("location /apps/   { return 302 $scheme://$host:$prepper_apps_port$request_uri; }", launcher)
        self.assertNotIn("alias /srv/prepper/apps/", launcher)

    def test_untrusted_files_sandboxed(self):
        n = self.render(example_config())["nginx"]
        csp = 'Content-Security-Policy "sandbox allow-scripts allow-popups allow-forms allow-downloads" always'
        n = n.split("listen 80 default_server;", 1)[1]                # the launcher's server block
        for loc in ("location /_usb/", "location /sync/", "location /content/", "location /kiwix/content/"):
            block = n.split(loc, 1)[1].split("\n    location /", 1)[0]
            self.assertIn(csp, block, loc)
            self.assertIn("X-Content-Type-Options nosniff", block, loc)
        self.assertIn("location ~* \\.(html?|xhtml|svg|svgz|xml|xsl)$", n.split("location /_usb/", 1)[1])
        # USB drives: no public listing; the portal checks membership, then X-Accel-Redirect to /_usb/
        self.assertIn("location /files/  { return 302 /#/files; }", n)
        self.assertIn("location /_usb/   { internal; alias /media/; disable_symlinks on;", n)
        self.assertNotIn("autoindex on; add_header X-Content-Type-Options nosniff always; location ~*", n)
        self.assertIn("access_log off;", n)
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
    # — /etc/hosts: only our marked block is ours —
    def test_hosts_keeps_other_lines(self):
        hosts = self.tmp / "root/etc/hosts"
        hosts.parent.mkdir(parents=True)
        hosts.write_text("127.0.0.1\tlocalhost\n127.0.1.1\tdebian\n192.168.1.5\tnas.home nas\n"
                         "10.0.0.9\tbadger.lan\n# my note\n::1\tlocalhost ip6-localhost\n")
        h = self.render(example_config())["hosts"]
        self.assertIn("192.168.1.5\tnas.home nas\n", h)
        self.assertIn("# my note\n", h)
        self.assertIn("127.0.0.1\tlocalhost\n", h)
        self.assertIn("::1\tlocalhost ip6-localhost\n", h)
        self.assertNotIn("debian", h)                      # the image's old hostname on 127.0.1.1
        self.assertNotIn("10.0.0.9", h)                    # a stale line for one of our names
        block = h.split(render.HOSTS_BEGIN + "\n", 1)[1].split(render.HOSTS_END, 1)[0]
        self.assertEqual(block, "127.0.1.1\tbadger\n10.10.10.10\tbadger.lan badger.local box.lan\n")
        # re-rendering (and a hostname change) replaces only the block
        hosts.write_text(h + "172.16.0.1\tadded.later\n")
        h2 = self.render(example_config(network={"hostname": "cabin"}))["hosts"]
        self.assertEqual(h2.count(render.HOSTS_BEGIN), 1)
        self.assertIn("172.16.0.1\tadded.later\n", h2)
        self.assertIn("10.10.10.10\tcabin.lan cabin.local box.lan\n", h2)
        self.assertNotIn("badger", h2)
        self.assertEqual(self.render(example_config(network={"hostname": "cabin"}))["hosts"], h2)

    def test_hosts_migrates_the_old_fully_owned_file(self):
        hosts = self.tmp / "root/etc/hosts"
        hosts.parent.mkdir(parents=True)
        hosts.write_text("127.0.0.1\tlocalhost\n127.0.1.1\tbadger\n10.10.10.10\tbadger.lan badger.local box.lan\n"
                         "::1\t\tlocalhost ip6-localhost ip6-loopback\n")
        h = self.render(example_config())["hosts"]
        self.assertEqual(h.count("127.0.1.1"), 1)
        self.assertEqual(h.count("box.lan"), 1)
        self.assertTrue(h.startswith("127.0.0.1\tlocalhost\n::1"))

    def test_hosts_from_nothing(self):
        h = self.render(example_config())["hosts"]
        self.assertTrue(h.startswith("127.0.0.1\tlocalhost\n"))
        self.assertIn(render.HOSTS_BEGIN, h)

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


def unit_ini(path: Path) -> dict[str, dict[str, list[str]]]:
    """A systemd unit / drop-in as {section: {key: [values…]}} (repeated keys kept in order)."""
    out: dict[str, dict[str, list[str]]] = {}
    sec = ""
    for line in path.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith(("#", ";")):
            continue
        if s.startswith("["):
            sec = s.strip("[]")
            out.setdefault(sec, {})
        elif "=" in s:
            k, v = s.split("=", 1)
            out.setdefault(sec, {}).setdefault(k.strip(), []).append(v.strip())
    return out


class SystemdUnits(unittest.TestCase):
    """The unit files and drop-ins install.sh puts in /etc/systemd/system (VM pressure-test findings)."""
    SYSTEMD = REPO / "os/systemd"

    def test_packaged_services_restart_after_a_crash(self):
        for unit in ("nginx", "dnsmasq", "chrony", "gpsd"):
            d = self.SYSTEMD / f"{unit}.service.d"
            confs = sorted(d.glob("*.conf"))
            self.assertTrue(confs, f"no drop-in for {unit}")
            svc = unit_ini(confs[0])["Service"]
            self.assertEqual(svc["Restart"], ["on-failure"], unit)
            self.assertEqual(svc["RestartSec"], ["2"], unit)

    def test_no_start_limit_for_launcher_services(self):
        self.assertEqual(unit_ini(self.SYSTEMD / "nginx.service.d/prepper-restart.conf")["Unit"]["StartLimitIntervalSec"],
                         ["0"])
        portal = unit_ini(self.SYSTEMD / "prepper-portal.service")
        self.assertEqual(portal["Unit"]["StartLimitIntervalSec"], ["0"])
        self.assertEqual(portal["Service"]["Restart"], ["always"])

    def test_installer_copies_drop_in_folders(self):
        sh = (REPO / "os/install.sh").read_text()
        self.assertIn('for d in "$OPT"/os/systemd/*.d; do', sh)
        self.assertIn('install -d -m 755 "/etc/systemd/system/$(basename "$d")"', sh)
        # the unit-file loop's globs must not pick up the drop-in folders
        for d in self.SYSTEMD.glob("*.d"):
            self.assertFalse(d.name.endswith((".service", ".timer", ".path")))

    def test_upd_auto_exit_75_is_not_a_failure_but_retried(self):
        u = unit_ini(self.SYSTEMD / "upd-auto.service")
        svc = u["Service"]
        self.assertEqual(svc["SuccessExitStatus"], ["75"])
        self.assertEqual(svc["RestartForceExitStatus"], ["75"])
        # systemd refuses RestartForceExitStatus= on Type=oneshot units
        self.assertNotEqual(svc.get("Type"), ["oneshot"])
        self.assertEqual(svc["RestartPreventExitStatus"], ["1"])
        self.assertIn("upd-check.service", u["Unit"]["After"])          # never together after a clock jump
        self.assertNotIn("upd-check.service", " ".join(u["Unit"].get("Wants", []) + u["Unit"].get("Requires", [])))
        for t in ("upd-check.timer", "upd-apply.timer"):
            self.assertTrue(unit_ini(self.SYSTEMD / t)["Timer"].get("RandomizedDelaySec"), t)

    def test_supplementary_groups_are_created(self):
        """A unit naming a group that doesn't exist (gpio on plain Debian) can't start at all."""
        sh = (REPO / "os/install.sh").read_text()
        m = re.search(r"^for g in ([a-z0-9 ]+); do\n  getent group \"\$g\" >/dev/null \|\| groupadd -rf", sh, re.M)
        self.assertTrue(m, "install.sh must create missing device groups")
        created = set(m.group(1).split())
        for unit in self.SYSTEMD.glob("*.service"):
            for groups in unit_ini(unit).get("Service", {}).get("SupplementaryGroups", []):
                for g in groups.split():
                    self.assertIn(g, created, f"{unit.name}: group {g} is not created by install.sh")
        self.assertIn('groupadd -rf "$g"', (REPO / "os/install.d/sensors.sh").read_text())


class InstallerChecks(unittest.TestCase):
    """install.sh / install.d details that can be checked without a Pi."""

    def test_llama_server_is_static_and_checked(self):
        sh = (REPO / "os/install.sh").read_text()
        self.assertIn("-DBUILD_SHARED_LIBS=OFF", sh)
        self.assertIn('"$OPT/llama/bin/llama-server" --version', sh)
        self.assertIn('ldd "$OPT/llama/bin/llama-server"', sh)
        self.assertIn('stamp llama "$LLAMA_CPP_REF-static"', sh)        # older (shared) builds are rebuilt
        self.assertIn("https_proxy=http://127.0.0.1:9", sh)              # no build-time UI download

    def test_llama_ui_option_filter(self):
        """Only whole-word UI options are switched off — never LLAMA_BUILD_SERVER ("BUILD" contains "UI")."""
        sh = (REPO / "os/install.sh").read_text()
        loop = sh[sh.index("    ui_off=()"):sh.index('    echo "llama.cpp: web UI options')]
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "l"
            (src / "tools/ui").mkdir(parents=True)
            (src / "scripts").mkdir()
            (src / "CMakeLists.txt").write_text('option(LLAMA_BUILD_SERVER "x" ON)\noption(LLAMA_BUILD_TOOLS "x" ON)\n')
            (src / "tools/ui/CMakeLists.txt").write_text(
                'option(LLAMA_BUILD_UI "b" ON)\noption( LLAMA_UI_DOWNLOAD "d" ON)\n'
                'option(LLAMA_UI_OFFLINE "o" OFF)\noption(LLAMA_SERVER_WEBUI "w" ON)\n')
            r = run([BASH, "-c", f'set -euo pipefail\nt={d}\n{loop}\necho "${{ui_off[*]}}"'])
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.split(),
                             ["-DLLAMA_BUILD_UI=OFF", "-DLLAMA_SERVER_WEBUI=OFF", "-DLLAMA_UI_DOWNLOAD=OFF"])

    def test_meshtastic_is_pinned(self):
        sh = (REPO / "os/install.sh").read_text()
        self.assertRegex(sh, r"(?m)^MESHTASTIC_PY=2\.7\.\d+$")
        self.assertIn('"meshtastic==$MESHTASTIC_PY"', sh)
        self.assertNotRegex(sh, r"pip\" install[^\n]* meshtastic( |$)")

    def apt_some(self, available: dict[str, str], *args: str):
        """Run sdr.sh's apt_some with a fake apt-cache (policy per package) and apt-get."""
        frag = (REPO / "os/install.d/sdr.sh").read_text()
        funcs = frag[frag.index("apt_candidate() {"):frag.index("sdr_dirs() {")]
        with tempfile.TemporaryDirectory() as d:
            b = Path(d)
            (b / "apt-cache").write_text(
                "#!/bin/bash\n[ \"$1\" = policy ] || exit 2\n"
                "case \"$2\" in\n" + "".join(
                    f"  {p}) printf '%s:\\n  Installed: (none)\\n  Candidate: {c}\\n' \"$2\" ;;\n"
                    for p, c in available.items()) + "  *) ;;\nesac\nexit 0\n")
            (b / "apt-get").write_text(f'#!/bin/bash\necho "$*" > {d}/installed\n')
            for f in ("apt-cache", "apt-get"):
                (b / f).chmod(0o755)
            script = (f'set -euo pipefail\nPATH={d}:$PATH\nsdr_warn() {{ echo "!! sdr: $*"; }}\n{funcs}\n'
                      f'apt_some {" ".join(repr(a) for a in args)}\n'
                      'echo "missing=${APT_MISSING[*]:-}"\n'
                      'if apt_missing_any libvolk-dev libvolk2-dev; then echo volk-missing; fi\n')
            r = run([BASH, "-c", script])
            inst = (b / "installed").read_text().split() if (b / "installed").exists() else None
            return r, inst

    def test_apt_some_needs_a_real_candidate(self):
        # Debian 13: libvolk2-dev is only "referred to" (Candidate: (none)); libvolk-dev is real.
        r, inst = self.apt_some({"libvolk2-dev": "(none)", "libvolk-dev": "3.1.2-1", "cmake": "3.31.6-2"},
                                "cmake", "libvolk-dev|libvolk2-dev", "libnope-dev")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(inst, ["install", "-y", "-q", "--no-install-recommends", "cmake", "libvolk-dev"])
        self.assertIn("missing=libnope-dev", r.stdout)
        self.assertIn("not in this system's package archive (skipped): libnope-dev", r.stdout)
        self.assertNotIn("volk-missing", r.stdout)
        # Debian 12: the other name
        r, inst = self.apt_some({"libvolk2-dev": "2.5.2-1"}, "libvolk-dev|libvolk2-dev")
        self.assertEqual(inst[-1], "libvolk2-dev")
        # neither: reported, and SatDump's essentials check sees it
        r, inst = self.apt_some({"libvolk2-dev": "(none)", "cmake": "3"}, "cmake", "libvolk-dev|libvolk2-dev")
        self.assertIn("missing=libvolk-dev|libvolk2-dev", r.stdout)
        self.assertIn("volk-missing", r.stdout)
        self.assertEqual(inst[-1], "cmake")


if __name__ == "__main__":
    unittest.main()
