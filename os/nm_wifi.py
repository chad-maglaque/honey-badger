#!/usr/bin/env python3
"""
nm_wifi — join a Wi-Fi uplink on wlan0 without the password ever appearing
in a command line (`nmcli dev wifi connect … password X` shows X to every
local user in `ps`).

  nm_wifi.py [--dir D] [--ifname wlan0] connect SSID     < password (one line; empty = open network)
  nm_wifi.py [--dir D] [--ifname wlan0] from-config prepper.toml
                                           # tries each [network] uplink_wifi entry until one connects

Writes a NetworkManager keyfile (0600, root) under D
(/etc/NetworkManager/system-connections), then `nmcli connection reload` and
`nmcli --wait T connection up id NAME`. Exit 0 once connected, 1 otherwise,
2 on bad input. Used by prepperctl (uplink-wifi, net-up) and firstboot.sh.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import tempfile
import tomllib
import unicodedata
from pathlib import Path

DEFAULT_DIR = "/etc/NetworkManager/system-connections"


def check_ssid(ssid: str) -> str:
    if not isinstance(ssid, str) or not ssid or len(ssid.encode()) > 32:
        raise ValueError("SSID must be 1–32 bytes")
    if any(unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp") for ch in ssid):
        raise ValueError("SSID must not contain control characters")
    return ssid


def check_psk(psk: str) -> str:
    if psk and not ((8 <= len(psk) <= 63 and all(0x20 <= ord(c) <= 0x7E for c in psk))
                    or (len(psk) == 64 and all(c in "0123456789abcdefABCDEF" for c in psk))):
        raise ValueError("Wi-Fi password must be 8–63 characters (or 64 hex digits)")
    return psk


def conn_name(ssid: str) -> str:
    return "prepper-uplink-" + hashlib.sha256(ssid.encode()).hexdigest()[:10]


def keyfile_value(s: str) -> str:
    """GKeyFile string escaping (\\\\ \\s \\n \\t \\r), as NetworkManager reads it."""
    out = s.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")
    return "\\s" + out[1:] if out.startswith(" ") else out


def keyfile(ssid: str, psk: str, ifname: str) -> str:
    # The SSID as a byte list ("72;111;…;"): unambiguous for any characters.
    lines = ["[connection]", f"id={conn_name(ssid)}", "type=wifi", f"interface-name={ifname}",
             "autoconnect=true", "autoconnect-priority=5", "",
             "[wifi]", "mode=infrastructure", "ssid=" + "".join(f"{b};" for b in ssid.encode()), ""]
    if psk:
        lines += ["[wifi-security]", "key-mgmt=wpa-psk", f"psk={keyfile_value(psk)}", ""]
    lines += ["[ipv4]", "method=auto", "", "[ipv6]", "method=auto", ""]
    return "\n".join(lines)


def write_keyfile(d: Path, ssid: str, psk: str, ifname: str) -> str:
    d.mkdir(parents=True, exist_ok=True)
    name = conn_name(ssid)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=f".{name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:          # mkstemp: 0600 from the start
            fh.write(keyfile(ssid, psk, ifname))
        os.replace(tmp, d / f"{name}.nmconnection")
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return name


def connect(d: Path, ssid: str, psk: str, ifname: str, timeout: int) -> bool:
    name = write_keyfile(d, check_ssid(ssid), check_psk(psk), ifname)
    try:
        subprocess.run(["nmcli", "connection", "reload"], check=False, timeout=30)
        r = subprocess.run(["nmcli", "--wait", str(timeout), "connection", "up", "id", name],
                           check=False, timeout=timeout + 15)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"nm_wifi: {e}", file=sys.stderr)
        return False
    return r.returncode == 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=DEFAULT_DIR)
    ap.add_argument("--ifname", default="wlan0")
    ap.add_argument("--timeout", type=int, default=60)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("connect"); c.add_argument("ssid")
    f = sub.add_parser("from-config"); f.add_argument("config")
    a = ap.parse_args()
    d = Path(a.dir)
    try:
        if a.cmd == "connect":
            psk = sys.stdin.readline().rstrip("\n") if not sys.stdin.isatty() else ""
            ok = connect(d, a.ssid, psk, a.ifname, a.timeout)
            print(f"{'joined' if ok else 'could not join'} {a.ssid!r}")
            return 0 if ok else 1
        nets = tomllib.loads(Path(a.config).read_text()).get("network", {}).get("uplink_wifi", [])
        for w in nets if isinstance(nets, list) else []:
            if not isinstance(w, dict):
                continue
            try:
                if connect(d, w.get("ssid", ""), str(w.get("psk", "") or ""), a.ifname, a.timeout):
                    print(f"joined uplink Wi-Fi {w['ssid']!r}")
                    return 0
            except ValueError as e:
                print(f"nm_wifi: skipping uplink_wifi entry: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"nm_wifi: {e}", file=sys.stderr)
        return 2
    except (OSError, tomllib.TOMLDecodeError) as e:
        print(f"nm_wifi: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
