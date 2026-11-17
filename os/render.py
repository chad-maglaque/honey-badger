#!/usr/bin/env python3
"""
render — turn /etc/prepper/prepper.toml into every system config file.

  render.py [--config FILE] [--root DIR] [--ap-iface IFACE]

--root renders into a scratch tree instead of / (used by tests and the
builder's dry run). Idempotent: run it as often as you like; services are
restarted by prepperctl, not here.
"""
from __future__ import annotations

import argparse
import contextlib
import ipaddress
import os
import re
import sys
import tempfile
import tomllib
import unicodedata
from pathlib import Path

CAPTIVE_PROBES = {
    # path → (quiet-mode body, content type, status)
    "/hotspot-detect.html": ("<HTML><HEAD><TITLE>Success</TITLE></HEAD><BODY>Success</BODY></HTML>", "text/html", 200),
    "/library/test/success.html": ("<HTML><HEAD><TITLE>Success</TITLE></HEAD><BODY>Success</BODY></HTML>", "text/html", 200),
    "/generate_204": ("", "text/plain", 204),
    "/gen_204": ("", "text/plain", 204),
    "/connecttest.txt": ("Microsoft Connect Test", "text/plain", 200),
    "/ncsi.txt": ("Microsoft NCSI", "text/plain", 200),
    "/canonical.html": ('<meta http-equiv="refresh" content="0;url=https://support.mozilla.org/kb/captive-portal"/>', "text/html", 200),
    "/success.txt": ("success\n", "text/plain", 200),
}


class ConfigError(ValueError):
    """prepper.toml holds a value that cannot be written safely into a system file."""


# Everything rendered here lands in files parsed line by line (hostapd,
# dnsmasq, nginx) or sourced by bash as root (/etc/default/prepper), so every
# user-supplied value is checked against a strict pattern first: a newline in
# an SSID must never become a second hostapd directive, a "$(…)" in a model
# name never a command in /etc/default/prepper.
HOSTNAME_RE = re.compile(r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
COUNTRY_RE = re.compile(r"[A-Z]{2}")
IFACE_RE = re.compile(r"[A-Za-z0-9_.-]{1,15}")
SAFE_PATH_RE = re.compile(r"/[A-Za-z0-9._+/-]*")
ID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")


def _fail(msg: str):
    raise ConfigError(msg)


def _match(rx: re.Pattern, value, what: str) -> str:
    v = str(value)
    if not rx.fullmatch(v):
        _fail(f"{what} = {v!r} is not valid")
    return v


def _int(value, what: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(f"{what} = {value!r} must be a whole number")
    v = value
    if not lo <= v <= hi:
        _fail(f"{what} = {v} must be between {lo} and {hi}")
    return v


def hostname(c: dict) -> str:
    h = _match(HOSTNAME_RE, c["network"].get("hostname", "badger"), "network.hostname")
    if h.isdigit():
        _fail("network.hostname must not be all digits")
    return h.lower()


def ap_address(c: dict) -> str:
    a = str(c["network"].get("address", "10.10.10.10"))
    try:
        ip = ipaddress.IPv4Address(a)
    except ValueError:
        _fail(f"network.address = {a!r} is not an IPv4 address")
    net = ipaddress.ip_interface(f"{ip}/24").network
    if ip in (net.network_address, net.broadcast_address):
        _fail(f"network.address = {a} is the network or broadcast address of its /24")
    return str(ip)


def dhcp_range(c: dict) -> tuple[str, str]:
    r = c["network"].get("dhcp_range", ["10.10.10.50", "10.10.10.250"])
    if not isinstance(r, list) or len(r) != 2:
        _fail("network.dhcp_range must be a list of two addresses")
    addr = ipaddress.IPv4Address(ap_address(c))
    net = ipaddress.ip_interface(f"{addr}/24").network
    try:
        lo, hi = (ipaddress.IPv4Address(str(x)) for x in r)
    except ValueError:
        _fail(f"network.dhcp_range = {r!r}: not IPv4 addresses")
    if lo not in net or hi not in net or lo > hi or lo == net.network_address or hi == net.broadcast_address:
        _fail(f"network.dhcp_range {lo}–{hi} must lie inside {net} (the address's /24), low to high")
    if lo <= addr <= hi:
        _fail(f"network.address {addr} lies inside network.dhcp_range {lo}–{hi}")
    return str(lo), str(hi)


def country(c: dict) -> str:
    return _match(COUNTRY_RE, c["network"].get("country", "US"), "network.country")


DEFAULT_SSID = "Home-Hotspot"
# A Wi-Fi name that says "prepared household with a stocked server" to every wardriver.
PRODUCT_SSID_RE = re.compile(r"honey\s*-?_?badger|prepper", re.I)


def ssid(c: dict) -> str:
    s = c["network"].get("ssid", DEFAULT_SSID)
    if not isinstance(s, str) or not s:
        _fail("network.ssid must be a non-empty string")
    if len(s.encode()) > 32:
        _fail(f"network.ssid {s!r} is longer than 32 bytes")
    if any(unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp") for ch in s):
        _fail(f"network.ssid {s!r} contains control characters")
    return s


def passphrase(c: dict) -> str:
    psk = c["network"].get("passphrase", "")
    if not isinstance(psk, str):
        _fail("network.passphrase must be a string")
    if psk and not (8 <= len(psk) <= 63 and all(0x20 <= ord(ch) <= 0x7E for ch in psk)):
        _fail("network.passphrase must be 8–63 printable ASCII characters (or empty for no password)")
    return psk


SECURITY_MODES = ("auto", "wpa2", "owe", "open", "owe-transition")
BRIDGE = "br-prepper"        # owe-transition: both BSSes are ports of this bridge, which holds the AP address
OWE_IFACE = "ap-owe"         # owe-transition: the hidden OWE BSS (hostapd creates it)


def security(c: dict) -> str:
    """The hotspot's protection: "wpa2" (a password), "owe" (Enhanced Open: no password, but every
    phone's traffic is encrypted — phones from about 2019 on: iOS 13+, Android 10+) or "open" (no
    password, no encryption: anyone in range reads everything). [network] security = "auto" (the
    default) means wpa2 with a passphrase, owe without one; "open" must be asked for by name.
    "owe" is OWE-ONLY: a phone older than ~2019 does not see a network it can join. "owe-transition"
    (EXPERIMENTAL, USB adapter only) adds an open twin for those phones; see hostapd()."""
    mode = c["network"].get("security", "auto")
    if mode not in SECURITY_MODES:
        _fail(f"network.security must be one of {', '.join(SECURITY_MODES)}")
    psk = passphrase(c)
    if mode == "auto":
        return "wpa2" if psk else "owe"
    if mode == "wpa2" and not psk:
        _fail("network.security = \"wpa2\" needs a network.passphrase (8–63 characters)")
    return mode


def bridged(c: dict, iface: str) -> bool:
    """owe-transition on a USB adapter runs two BSSes bridged together; the onboard radio can't do OWE at all."""
    return security(c) == "owe-transition" and iface != "wlan0"


def l3_iface(c: dict, iface: str) -> str:
    """The interface that carries the AP address, DHCP and the firewall rules."""
    return BRIDGE if bridged(c, iface) else iface


def data_root(c: dict) -> str:
    d = _match(SAFE_PATH_RE, c["upd"].get("data_root", "/srv/prepper"), "upd.data_root")
    if "/../" in d + "/" or d.rstrip("/") == "":
        _fail(f"upd.data_root = {d!r} is not valid")
    return d.rstrip("/")


def check(c: dict, iface: str) -> None:
    """Validate everything the renderers below use; raises ConfigError."""
    _match(IFACE_RE, iface, "AP interface")
    hostname(c); ap_address(c); dhcp_range(c); country(c); ssid(c); passphrase(c); security(c); data_root(c)
    n, ai = c["network"], c.get("ai", {})
    _int(n.get("max_clients", 64), "network.max_clients", 1, 2007)
    _int(n.get("channel_24ghz", 6), "network.channel_24ghz", 1, 14)
    ch5 = _int(n.get("channel_5ghz", 36), "network.channel_5ghz", 0, 177)
    if ch5 and ch5 < 32:
        _fail(f"network.channel_5ghz = {ch5} is not a 5 GHz channel (0 = use 2.4 GHz)")
    if n.get("captive", "popup") not in ("popup", "quiet"):
        _fail("network.captive must be popup or quiet")
    try:
        if float(n.get("ap_defer_hours", 12)) < 0:
            raise ValueError
    except (TypeError, ValueError):
        _fail("network.ap_defer_hours must be a number >= 0")
    _match(ID_RE, ai.get("model", "model_qwen3_4b"), "ai.model")
    _match(ID_RE, ai.get("endurance_model", "model_qwen25_1_5b"), "ai.endurance_model")
    _int(ai.get("threads", 4), "ai.threads", 1, 64)
    _int(ai.get("context", 8192), "ai.context", 256, 1 << 20)


def pick_ap_iface(pref: str) -> str:
    """auto → first Wi-Fi interface whose driver isn't the onboard brcmfmac."""
    if pref and pref != "auto":
        return pref
    best = "wlan0"
    for dev in sorted(Path("/sys/class/net").glob("wl*")):
        drv = dev / "device" / "driver"
        name = os.path.basename(os.path.realpath(drv)) if drv.exists() else ""
        if name and name != "brcmfmac":
            return dev.name
    return best


def write(root: Path, path: str, text: str, mode: int = 0o644) -> None:
    dest = root / path.lstrip("/")
    dest.parent.mkdir(parents=True, exist_ok=True)
    # mkstemp (O_EXCL, random name) rather than a fixed ".name.tmp": never
    # follow a planted symlink, and two renders at once cannot interleave.
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    print(f"  wrote {dest}")


def ht40(channel: int) -> str:
    """HT40 secondary-channel direction for a 5 GHz primary channel: 36/44/52…
    pair upwards, 40/48/56… downwards; 165+ has no 40 MHz partner."""
    if channel >= 165:
        return ""
    return "[HT40+]" if (channel // 4) % 2 else "[HT40-]"


def hostapd(c: dict, iface: str, force_24ghz: bool = False) -> str:
    n = c["network"]
    _match(IFACE_RE, iface, "AP interface")
    usb = iface != "wlan0"
    ch5 = _int(n.get("channel_5ghz", 36), "network.channel_5ghz", 0, 177)
    five = usb and ch5 > 0 and not force_24ghz      # force_24ghz: net-up's fallback when 5 GHz fails
    name = ssid(c)
    if re.fullmatch(r"[\x21-\x7e]([\x20-\x7e]*[\x21-\x7e])?", name):
        ssid_lines = [f"ssid={name}"]
    else:
        # Spaces at the ends or non-ASCII: hostapd's hex form is unambiguous.
        ssid_lines = [f"ssid2={name.encode().hex()}"] + (["utf8_ssid=1"] if not name.isascii() else [])
    max_sta = _int(n.get('max_clients', 64), 'network.max_clients', 1, 2007)
    lines = [
        f"interface={iface}", "driver=nl80211", *ssid_lines,
        f"country_code={country(c)}", "ieee80211d=1",
        f"max_num_sta={max_sta}",
        # ap_isolate: phones on the hotspot talk only to the Badger, never to each other (no ARP spoofing or
        # port scans of the family's phones by a visitor; the launcher and captive portal only need the Badger)
        "wmm_enabled=1", "ignore_broadcast_ssid=0", "ap_isolate=1",
    ]
    if five:
        cap = ht40(ch5)
        lines += ["hw_mode=a", f"channel={ch5}", "ieee80211n=1", "ieee80211ac=1",
                  "ieee80211ax=1", f"ht_capab={cap}[SHORT-GI-20][SHORT-GI-40]" if cap else "ht_capab=[SHORT-GI-20]",
                  "vht_oper_chwidth=0", "he_oper_chwidth=0"]
    else:
        lines += ["hw_mode=g", f"channel={_int(n.get('channel_24ghz', 6), 'network.channel_24ghz', 1, 14)}",
                  "ieee80211n=1"]
    psk, mode = passphrase(c), security(c)
    if mode == "owe-transition" and not usb:
        lines.append("# security = owe-transition needs the USB adapter: the onboard radio runs an open network (no password)")
    elif mode == "owe-transition":
        # EXPERIMENTAL (untested on hardware). OWE transition mode (RFC 8110 / Wi-Fi Enhanced Open): this
        # first BSS is OPEN under the real name, so phones older than ~2019 can join; its twin below is a
        # HIDDEN OWE BSS that newer phones are steered to (they get a per-phone key). Both are ports of one
        # bridge (prepperctl net-up creates it) so they share the AP subnet, DHCP and captive portal, and
        # both isolate their clients from each other. A phone that joins the open twin is NOT encrypted.
        owe_name = ("OWE-" + name).encode()[:32]
        lines += [f"bridge={BRIDGE}", f"owe_transition_ifname={OWE_IFACE}"]
        lines += ["", f"bss={OWE_IFACE}", f"ssid2={owe_name.hex()}", "ignore_broadcast_ssid=1",
                  f"max_num_sta={max_sta}", "wmm_enabled=1", "ap_isolate=1", f"bridge={BRIDGE}",
                  "wpa=2", "wpa_key_mgmt=OWE", "rsn_pairwise=CCMP", "ieee80211w=2", "owe_groups=19",
                  f"owe_transition_ifname={iface}"]
    elif mode == "owe" and not usb:
        # The onboard brcmfmac radio can't do OWE in AP mode: it would refuse to start, leaving no hotspot
        # at all. Run open instead (the portal treats owe and open alike: no secrets over plain http).
        lines.append("# security = owe is not supported by the onboard radio: open network (no password)")
    elif mode == "owe":
        # Enhanced Open (OWE, RFC 8110): no password, but each phone gets its own encryption key, so nobody
        # in range can read another phone's traffic. Management frames protected (required by OWE).
        # OWE-only: a phone older than ~2019 can't join (use "owe-transition" for those, experimental).
        lines += ["wpa=2", "wpa_key_mgmt=OWE", "rsn_pairwise=CCMP", "ieee80211w=2", "owe_groups=19"]
    if mode == "wpa2":
        lines += ["wpa=2", f"wpa_passphrase={psk}", "rsn_pairwise=CCMP"]
        if usb:
            # WPA2 + WPA3 transition: old phones join with WPA2, new ones use SAE.
            lines += ["wpa_key_mgmt=WPA-PSK SAE", "ieee80211w=1", "sae_require_mfp=1"]
        else:
            # The onboard brcmfmac radio does not do SAE in AP mode: offering it
            # makes some phones fail to join. WPA2-PSK only.
            lines += ["wpa_key_mgmt=WPA-PSK"]
    return "\n".join(lines) + "\n"


def dnsmasq(c: dict, iface: str) -> str:
    n = c["network"]
    _match(IFACE_RE, iface, "AP interface")
    addr, host = ap_address(c), hostname(c)
    lo, hi = dhcp_range(c)
    out = [
        f"interface={l3_iface(c, iface)}", "bind-dynamic", "domain-needed", "bogus-priv",
        f"dhcp-range={lo},{hi},255.255.255.0,12h", f"dhcp-option=option:router,{addr}",
        f"dhcp-option=option:dns-server,{addr}", "dhcp-authoritative",
        # leases (with each phone's name) in RAM only, and no per-phone DHCP lines in the log
        "dhcp-leasefile=/run/prepper-dnsmasq/leases", "quiet-dhcp",
        "domain=lan", "local=/lan/", f"address=/{host}.lan/{addr}", f"address=/{host}.local/{addr}",
        f"address=/box.lan/{addr}",
    ]
    if n.get("share_uplink") is True:
        out += ["server=1.1.1.1", "server=9.9.9.9"]
    else:
        out += ["no-resolv", f"address=/#/{addr}"]   # every name → the Badger (offline mode)
    return "\n".join(x for x in out if x) + "\n"


def _nginx_str(body: str) -> str:
    """A double-quoted nginx string (nginx understands \\" \\\\ \\n \\r \\t)."""
    return '"' + body.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r") \
        .replace("\t", "\\t") + '"'


# Files nobody vetted (USB sticks under /files/, content, peer sync) are served
# on the launcher's origin: HTML/SVG/XML there runs sandboxed in an opaque
# origin, so it cannot read the launcher's storage (the admin token) or call
# the API with its cookies.
SANDBOX = ('location ~* \\.(html?|xhtml|svg|svgz|xml|xsl)$ { '
           'add_header Content-Security-Policy "sandbox allow-scripts allow-popups allow-forms allow-downloads" always; '
           'add_header X-Content-Type-Options nosniff always; }')


# Third-party web apps served on the launcher's origin (llama.cpp's UI, Kolibri, Kavita, Jellyfin,
# the kiwix reader). The launcher's credentials are HttpOnly cookies, so these apps can't read them;
# this policy also stops their scripts from calling the launcher's API (or framing the launcher)
# with those cookies: fetch/XHR/WebSocket, frames and workers only within the app's own path.
def app_csp(prefix: str) -> str:
    own = f"$scheme://$host{prefix}"
    sock = f"{own} ws://$host{prefix} wss://$host{prefix}"
    return (f'add_header Content-Security-Policy "connect-src {sock}; frame-src {own}; child-src {own}; '
            f'worker-src {own} blob:; frame-ancestors \'self\'" always;')


APPS_PORT, APPS_TLS_PORT = 8088, 8443


def apps_server(data: str, tls: bool) -> str:
    """The web apps under data_root/apps (pdf.js, foliate-js, Kiwix JS, …) on their OWN origin
    (another port): code fetched from upstream (or a peer) must never run on the launcher's origin,
    where it could open the launcher in a window and script it with the family's sign-in. Here it can
    read the library (/content/, public anyway) and nothing else; the launcher's /apps/ links redirect."""
    ssl = (f"""    listen {APPS_TLS_PORT} ssl;
    listen [::]:{APPS_TLS_PORT} ssl;
    ssl_certificate     /etc/prepper/tls/server.crt;
    ssl_certificate_key /etc/prepper/tls/server.key;
    ssl_protocols TLSv1.2 TLSv1.3;
""" if tls else "")
    return f"""server {{
    listen {APPS_PORT};
    listen [::]:{APPS_PORT};
{ssl}    server_name _;
    access_log off;
    root /nonexistent;
    charset utf-8;
    location /apps/    {{ alias {data}/apps/; disable_symlinks off; {APPS_CSP} add_header X-Content-Type-Options nosniff always; }}
    location /content/ {{
        alias {data}/;
        disable_symlinks off;
        add_header X-Content-Type-Options nosniff always;
        location ~ /\\.(staging|git) {{ deny all; }}
        location ~ ^/content/kolibri(/|$) {{ deny all; }}
        {SANDBOX}
    }}
    location / {{ return 404; }}
}}
"""


# The apps' own origin: they may fetch from it (the library under /content/), frame only themselves,
# and are never framed by another page.
APPS_CSP = ('add_header Content-Security-Policy "connect-src \'self\' blob: data:; frame-ancestors \'self\'; '
            'form-action \'self\'" always;')


def nginx(c: dict, tls_ready: bool = True) -> str:
    n = c["network"]
    data = data_root(c)
    host = hostname(c)
    popup = n.get("captive", "popup") == "popup"
    probes = []
    for path, (body, ctype, code) in CAPTIVE_PROBES.items():
        if popup:
            probes.append(f"    location = {path} {{ return 302 http://{host}.lan/#/devices; }}")
        elif code == 204:
            probes.append(f"    location = {path} {{ return 204; }}")
        else:
            probes.append(f'    location = {path} {{ default_type {ctype}; return 200 {_nginx_str(body)}; }}')
    tls = ""
    https = n.get("https", True) is not False and tls_ready
    # Only with a certificate on disk: "listen 443 ssl" without one stops
    # nginx from starting at all (prepperctl gen-tls re-renders once it exists).
    if https:
        tls = """    listen 443 ssl default_server;
    listen [::]:443 ssl default_server;
    ssl_certificate     /etc/prepper/tls/server.crt;
    ssl_certificate_key /etc/prepper/tls/server.key;
    ssl_protocols TLSv1.2 TLSv1.3;
"""
    return f"""# Rendered by /opt/prepper/os/render.py — edit prepper.toml, not this file.
map $scheme $prepper_apps_port {{ https {APPS_TLS_PORT if https else APPS_PORT}; default {APPS_PORT}; }}

{apps_server(data, https)}
server {{
    listen 80 default_server;
    listen [::]:80 default_server;
{tls}    server_name _;
    # No visit diary: no access log (visitor addresses, phone models, every URL incl. one-time
    # download links), and only serious errors in the error log.
    access_log off;
    error_log /var/log/nginx/error.log crit;
    root /opt/prepper/software/portal/static;
    index index.html;
    charset utf-8;
    gzip on;
    gzip_types text/css application/javascript application/json image/svg+xml application/xml;
    client_max_body_size 1m;           # everywhere but /api/ (below)
    client_body_timeout 10s;
    client_header_timeout 10s;

    # Captive-portal probes from phones and laptops
{chr(10).join(probes)}

    location = /ca.crt {{ alias /etc/prepper/tls/ca.crt; default_type application/x-x509-ca-cert; }}
    location = /sw.js  {{ add_header Cache-Control "no-cache"; }}

    location /api/ {{
        proxy_pass http://127.0.0.1:8000;
        # Feature uploads (voice notes, the document vault) up to 25 MB; the portal enforces each
        # route's own limit and keeps its 1 MB cap on JSON bodies.
        client_max_body_size 26m;
        client_body_timeout 60s;
        # The portal compares Origin with Host on admin POSTs: pass the real one.
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        # http or https: new secrets (Wi-Fi password, radio key) are only sent over https.
        proxy_set_header X-Forwarded-Proto $scheme;
        # Only the front panel, straight to the portal, may present the panel key.
        proxy_set_header X-Badger-Panel "";
        proxy_buffering off;               # /api/ask streams tokens
        proxy_request_buffering off;       # stream big uploads to the portal instead of spooling them
        proxy_read_timeout 600s;
    }}
    location /kiwix/ {{
        proxy_pass http://127.0.0.1:8081;
        proxy_set_header Host $host;
        proxy_read_timeout 120s;
        add_header X-Content-Type-Options nosniff always;
        {app_csp("/kiwix/")}
    }}
    # Raw ZIM articles (the reader UI at /kiwix/viewer stays unsandboxed).
    location /kiwix/content/ {{
        proxy_pass http://127.0.0.1:8081;
        proxy_set_header Host $host;
        proxy_read_timeout 120s;
        add_header Content-Security-Policy "sandbox allow-scripts allow-popups allow-forms allow-downloads" always;
        add_header X-Content-Type-Options nosniff always;
    }}
    # Raw content: ZIM downloads, PMTiles (range requests), PDFs, EPUBs, video.
    location /content/ {{
        alias {data}/;
        autoindex on;
        disable_symlinks off;
        add_header X-Content-Type-Options nosniff always;
        location ~ /\\.(staging|git) {{ deny all; }}
        # Kolibri keeps its database in /var/lib/kolibri; never serve an old copy from here.
        location ~ ^/content/kolibri(/|$) {{ deny all; }}
        location ~* \\.(pmtiles)$ {{ add_header Access-Control-Allow-Origin *; add_header Cache-Control "public, max-age=3600"; add_header X-Content-Type-Options nosniff always; }}
        {SANDBOX}
    }}
    location /sync/   {{ alias {data}/sync/; disable_symlinks off; add_header X-Content-Type-Options nosniff always; {SANDBOX} }}     # peer Badgers pull from here
    # /apps/ (pdf.js, foliate-js, Kiwix JS…) lives on its own origin (apps_server above), never here.
    location /apps/   {{ return 302 $scheme://$host:$prepper_apps_port$request_uri; }}
    location /vendor/ {{ alias /opt/prepper/vendor/; expires 30d; }}
    # USB drives are the family's files: browsed in the launcher (USB & files) by members and the admin.
    # The portal checks who is asking, then hands the transfer to /_usb/ (internal: no direct URL).
    location /files/  {{ return 302 /#/files; }}
    location /_usb/   {{ internal; alias /media/; disable_symlinks on; add_header X-Content-Type-Options nosniff always; {SANDBOX} }}

    location /ai/ {{
        # No trailing slash: llama-server runs with --api-prefix /ai, so the
        # prefix must reach it unchanged.
        proxy_pass http://127.0.0.1:8090;
        proxy_buffering off;
        proxy_read_timeout 600s;
        {app_csp("/ai/")}
    }}
    location /learn/ {{ proxy_pass http://127.0.0.1:8080; proxy_set_header Host $host; proxy_read_timeout 300s; {app_csp("/learn/")} }}
    location /books/ {{ proxy_pass http://127.0.0.1:5000; proxy_set_header Host $host; {app_csp("/books/")} }}
    location /video/ {{ proxy_pass http://127.0.0.1:8096; proxy_set_header Host $host; proxy_buffering off; {app_csp("/video/")} }}
}}
"""


def ap_subnet(c: dict) -> str:
    """The clients' /24 (address and netmask are fixed at /24 throughout)."""
    return str(ipaddress.ip_interface(f"{ap_address(c)}/24").network)


def env_file(c: dict, iface: str) -> str:
    # Sourced by bash (prepperctl, llama-start) and read by systemd
    # (EnvironmentFile=): every value is from a strict character set, so no
    # quoting is needed and none can run a command.
    n, ai = c["network"], c.get("ai", {})
    data = data_root(c)
    _match(IFACE_RE, iface, "AP interface")
    return "\n".join([
        "PREPPER_CONFIG=/etc/prepper/prepper.toml",
        f"AP_IFACE={iface}",
        f"AP_LINK_IFACE={l3_iface(c, iface)}",
        f"AP_BRIDGE={1 if bridged(c, iface) else 0}",
        f"AP_OWE_IFACE={OWE_IFACE}",
        f"AP_ADDRESS={ap_address(c)}",
        f"AP_SUBNET={ap_subnet(c)}",
        f"AP_DEFER_HOURS={float(n.get('ap_defer_hours', 12)):g}",
        f"DATA_ROOT={data}",
        f"KIWIX_LIBRARY={data}/zim/library.xml",
        f"LLAMA_MODEL={data}/models/{_match(ID_RE, ai.get('model', 'model_qwen3_4b'), 'ai.model')}.current",
        f"LLAMA_ENDURANCE_MODEL={data}/models/"
        f"{_match(ID_RE, ai.get('endurance_model', 'model_qwen25_1_5b'), 'ai.endurance_model')}.current",
        f"LLAMA_THREADS={_int(ai.get('threads', 4), 'ai.threads', 1, 64)}",
        f"LLAMA_CTX={_int(ai.get('context', 8192), 'ai.context', 256, 1 << 20)}",
        # Not under data_root: nginx serves data_root at /content/, and Kolibri's
        # home holds its database (accounts, password hashes).
        "KOLIBRI_HOME=/var/lib/kolibri",
        "KOLIBRI_URL_PATH_PREFIX=/learn/",
        "",
    ])


def network_manager(iface: str, wlan0_is_ap: bool, bridge: bool = False) -> str:
    """Hand the AP radio to hostapd. The onboard wlan0 is released only once
    `prepperctl net-up` has decided it becomes the hotspot (it writes the
    ap-wlan0 marker): while provisioning it may be our only download link, and
    NetworkManager reads this file at boot, so writing it early would cut the
    uplink on a reboot mid-download."""
    _match(IFACE_RE, iface, "AP interface")
    if iface == "wlan0" and not wlan0_is_ap:
        return "[keyfile]\n# wlan0 stays with NetworkManager (uplink) until the hotspot takes it over.\n"
    extra = f";interface-name:{BRIDGE};interface-name:{OWE_IFACE}" if bridge else ""
    return f"[keyfile]\nunmanaged-devices=interface-name:{iface}{extra}\n"


def chrony_gps(c: dict) -> str:
    return ("# GPS time via gpsd shared memory; the Pi 5 RTC (with its battery) bridges reboots.\n"
            "refclock SHM 0 refid GPS precision 1e-1 offset 0.15 delay 0.2 poll 4\n"
            "refclock SHM 1 refid PPS precision 1e-7 lock GPS poll 4\n"
            "makestep 1 -1\n"
            f"allow {ap_subnet(c)}\n"            # serve time to clients on the Badger's Wi-Fi
            "local stratum 10\n")


def update_timer(c: dict) -> str:
    """Drop-in for upd-apply.timer: with [upd] window set, start the weekly
    download inside the window (upd.py, run without --now, skips outside it)."""
    head = "# Rendered by /opt/prepper/os/render.py from [upd] window.\n"
    w = str(c["upd"].get("window", "") or "")
    m = re.fullmatch(r"(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})", w)
    if not m:
        if w:
            print(f"render: ignoring malformed [upd] window {w!r} (want HH:MM-HH:MM)", file=sys.stderr)
        return head + "# No window: the unit's own schedule applies.\n"
    h1, m1, h2, m2 = (int(x) for x in m.groups())
    if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
        print(f"render: ignoring out-of-range [upd] window {w!r} (want HH:MM-HH:MM, 00:00–23:59)", file=sys.stderr)
        return head + "# No window: the unit's own schedule applies.\n"
    length = ((h2 * 60 + m2) - (h1 * 60 + m1)) % (24 * 60) or 24 * 60
    spread = max(0, min(120, length // 2))      # stay well inside the window
    return head + f"[Timer]\nOnCalendar=\nOnCalendar=Sun {h1:02d}:{m1:02d}\nRandomizedDelaySec={spread}min\n"


def render_all(c: dict, root: Path, iface: str, force_24ghz: bool = False) -> None:
    c.setdefault("network", {}); c.setdefault("upd", {})
    check(c, iface)                  # nothing is written unless everything is valid
    print(f"render: AP on {iface}")
    if PRODUCT_SSID_RE.search(ssid(c)):
        print(f"render: the Wi-Fi name {ssid(c)!r} tells everyone in range what this box is — consider a "
              "neutral one ([network] ssid)", file=sys.stderr)
    if security(c) == "owe-transition":
        print("render: security = owe-transition is EXPERIMENTAL (open twin + hidden OWE twin, bridged); "
              "untested on hardware" + ("" if iface != "wlan0" else " — and the onboard radio runs an open network"),
              file=sys.stderr)
    if security(c) != "wpa2":
        print(f"render: the hotspot has no password (security = {security(c)}) — family secrets are only "
              "sent over https://", file=sys.stderr)
    write(root, "/etc/hostapd/prepper.conf", hostapd(c, iface, force_24ghz), 0o600)
    write(root, "/etc/dnsmasq.d/prepper.conf", dnsmasq(c, iface))
    tls = root / "etc/prepper/tls"
    tls_ready = (tls / "server.crt").is_file() and (tls / "server.key").is_file()
    if c["network"].get("https", True) is not False and not tls_ready:
        print("render: no TLS certificate yet — HTTPS stays off until `prepperctl gen-tls`", file=sys.stderr)
    write(root, "/etc/nginx/sites-available/prepper", nginx(c, tls_ready))
    write(root, "/etc/default/prepper", env_file(c, iface))
    wlan0_is_ap = (root / "var/lib/prepper/ap-wlan0").exists()
    write(root, "/etc/NetworkManager/conf.d/99-prepper.conf", network_manager(iface, wlan0_is_ap, bridged(c, iface)))
    write(root, "/etc/chrony/conf.d/prepper-gps.conf", chrony_gps(c))
    write(root, "/etc/systemd/system/upd-apply.timer.d/prepper-window.conf", update_timer(c))
    host, addr = hostname(c), ap_address(c)
    write(root, "/etc/hostname", host + "\n")
    write(root, "/etc/hosts", f"127.0.0.1\tlocalhost\n127.0.1.1\t{host}\n"
                              f"{addr}\t{host}.lan {host}.local box.lan\n"
                              "::1\t\tlocalhost ip6-localhost ip6-loopback\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="/etc/prepper/prepper.toml")
    ap.add_argument("--root", default="/")
    ap.add_argument("--ap-iface", default="")
    ap.add_argument("--force-24ghz", action="store_true", help="AP on 2.4 GHz even on a USB adapter (fallback)")
    args = ap.parse_args()
    try:
        c = tomllib.loads(Path(args.config).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        print(f"render: cannot read {args.config}: {e}", file=sys.stderr)
        return 2
    c.setdefault("network", {}); c.setdefault("upd", {})
    try:
        iface = args.ap_iface or pick_ap_iface(str(c["network"].get("ap_iface", "auto")))
        render_all(c, Path(args.root), iface, args.force_24ghz)
    except ConfigError as e:
        print(f"render: {args.config}: {e} — nothing written", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
