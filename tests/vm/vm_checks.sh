#!/bin/bash
# Honey Badger — integration checks, run as root INSIDE a Debian 13 arm64 test VM after os/install.sh and a reboot.
#
# The VM needs three simulated radios (mac80211_hwsim radios=3): wlan1 plays the USB Wi-Fi adapter (set
# ap_iface = "wlan1" and a passphrase in /etc/prepper/prepper.toml), wlan0 the onboard radio, and wlan2 a phone,
# which this script moves into its own network namespace. Hardware-only parts (GPIO, UPS gauge, SDR, LoRa, OLED)
# can't be emulated; they're checked to fail safely.
#
#   sudo bash tests/vm/vm_checks.sh [passphrase] [admin-pin]
set -u
PSK="${1:-badgertest123}"; PIN="${2:-246813}"; SSID=HoneyBadger; AP=10.10.10.10
pass=0; fail=0
ok()   { echo "PASS  $*"; pass=$((pass + 1)); }
bad()  { echo "FAIL  $*"; fail=$((fail + 1)); }
check() { local name=$1; shift; if "$@" >/dev/null 2>&1; then ok "$name"; else bad "$name"; fi; }
IW=/usr/sbin/iw
APIF=$(. /etc/default/prepper 2>/dev/null; echo "${AP_IFACE:-wlan1}")

echo "== boot and services"
[ -z "$(systemctl --failed --no-legend)" ] && ok "no failed units" || bad "failed units: $(systemctl --failed --no-legend | awk '{print $2}' | tr '\n' ' ')"
for u in prepper-net prepper-portal nginx dnsmasq prepper-powerd chrony; do check "$u active" systemctl is-active --quiet "$u"; done
check "upd-check.timer scheduled" systemctl is-active --quiet upd-check.timer
check "upd-apply.timer scheduled" systemctl is-active --quiet upd-apply.timer
check "nginx config valid" nginx -t
for f in /etc/sudoers.d/*; do check "sudoers $(basename "$f") parses" visudo -cf "$f"; done
out=$(systemd-analyze verify /etc/systemd/system/prepper-*.service /etc/systemd/system/upd-*.{service,timer} 2>&1 | grep -v '^$')
[ -z "$out" ] && ok "systemd-analyze verify: clean" || bad "systemd-analyze verify: $out"

echo "== hotspot (started by systemd at boot, not by hand)"
for _ in $(seq 1 20); do $IW dev "$APIF" info 2>/dev/null | grep -q "type AP" && break; sleep 1; done
$IW dev "$APIF" info 2>/dev/null | grep -q "type AP" && ok "$APIF is an access point" || bad "$APIF is not an access point"
$IW dev "$APIF" info 2>/dev/null | grep -q "ssid $SSID" && ok "SSID $SSID" || bad "SSID not $SSID"
pgrep -x hostapd >/dev/null && ok "hostapd running" || bad "hostapd not running"

echo "== a phone joins"
PHY=$(for p in /sys/class/ieee80211/*; do [ -e "$p/device/net/wlan2" ] && basename "$p"; done)
ip netns list | grep -q '^phone' || ip netns add phone
[ -n "$PHY" ] && $IW phy "$PHY" set netns name phone
mkdir -p /etc/netns/phone; echo "nameserver $AP" > /etc/netns/phone/resolv.conf
N="ip netns exec phone"
$N ip link set lo up; $N ip link set wlan2 up
printf 'ctrl_interface=/run/wpa_phone\nnetwork={\n ssid="%s"\n psk="%s"\n key_mgmt=WPA-PSK SAE\n ieee80211w=1\n}\n' "$SSID" "$PSK" > /tmp/phone-wpa.conf
pkill -f "wpa_supplicant.*wlan2" 2>/dev/null; $N wpa_supplicant -B -i wlan2 -c /tmp/phone-wpa.conf >/dev/null
for _ in $(seq 1 30); do $N $IW dev wlan2 link | grep -q Connected && break; sleep 1; done
$N $IW dev wlan2 link | grep -q Connected && ok "phone associated (WPA2/WPA3)" || bad "phone did not associate"
$N timeout 25 dhclient -1 wlan2 >/dev/null 2>&1
ip4=$($N ip -4 -br a show wlan2 | awk '{print $3}')
[ -n "$ip4" ] && ok "DHCP lease $ip4" || bad "no DHCP lease"
dig1() { $N dig +short +time=3 @"$AP" "$1" A | tail -1; }
[ "$(dig1 badger.lan)" = "$AP" ] && ok "badger.lan resolves to $AP" || bad "badger.lan resolves to '$(dig1 badger.lan)'"
for h in connectivitycheck.gstatic.com captive.apple.com www.msftconnecttest.com; do
  code=$($N curl -s -o /dev/null --resolve "$h:80:$AP" -w "%{http_code}" --max-time 8 "http://$h/$( [ $h = captive.apple.com ] && echo hotspot-detect.html || ([ $h = www.msftconnecttest.com ] && echo connecttest.txt || echo generate_204))")
  [ "$code" = 302 ] && ok "captive probe $h → 302" || bad "captive probe $h → $code"
done
C="$N curl -s --max-time 10 --resolve badger.lan:80:$AP --resolve badger.lan:443:$AP"
[ "$($C -o /dev/null -w '%{http_code}' http://badger.lan/)" = 200 ] && ok "launcher over Wi-Fi" || bad "launcher over Wi-Fi"
[ "$($C -k -o /dev/null -w '%{http_code}' https://badger.lan/)" = 200 ] && ok "launcher over HTTPS" || bad "launcher over HTTPS"
$C http://badger.lan/api/status | grep -q '"clients": *1' && ok "/api/status counts the phone" || bad "/api/status client count"
$C http://badger.lan/ca.crt | grep -q "BEGIN CERTIFICATE" && ok "CA certificate served" || bad "CA certificate"

echo "== portal security over Wi-Fi"
J="$C -H Content-Type:application/json"
[ "$($J -o /dev/null -w '%{http_code}' -d '{"mode":"endurance"}' http://badger.lan/api/admin/power)" = 401 ] && ok "admin action without token → 401" || bad "admin action without token"
T=$($J -d "{\"pin\":\"$PIN\"}" http://badger.lan/api/admin/login | python3 -c 'import json,sys;print(json.load(sys.stdin).get("token",""))' 2>/dev/null)
[ -n "$T" ] && ok "login with the right PIN" || bad "login with the right PIN"
$J -H "X-Admin-Token: $T" -d '{"mode":"endurance"}' http://badger.lan/api/admin/power | grep -q '"ok": true' && ok "admin power → endurance (portal → sudo prepperctl)" || bad "admin power action"
[ "$(cat /var/lib/prepper/power-mode 2>/dev/null)" = endurance ] && ok "power-mode recorded" || bad "power-mode not recorded"
$J -H "X-Admin-Token: $T" -d '{"mode":"balanced"}' http://badger.lan/api/admin/power >/dev/null
$J -o /dev/null -H "X-Admin-Token: $T" -d '{}' http://badger.lan/api/admin/logout
[ "$($J -o /dev/null -w '%{http_code}' -H "X-Admin-Token: $T" -d '{"mode":"full"}' http://badger.lan/api/admin/power)" = 401 ] && ok "token dead after logout" || bad "token still works after logout"
codes=""; for _ in 1 2 3 4 5 6; do codes="$codes $($J -o /dev/null -w '%{http_code}' -d '{"pin":"000000"}' http://badger.lan/api/admin/login)"; done
echo "$codes" | grep -q 429 && ok "PIN lockout engages ($codes )" || bad "no PIN lockout ($codes )"
[ "$($J -o /dev/null -w '%{http_code}' -d "{\"pin\":\"$PIN\"}" http://badger.lan/api/admin/login)" = 429 ] && ok "right PIN refused during lockout" || bad "right PIN accepted during lockout"
head -c 2000000 /dev/zero | tr '\0' a > /tmp/big.json
[ "$($J -o /dev/null -w '%{http_code}' --data-binary @/tmp/big.json http://badger.lan/api/board)" = 413 ] && ok "2 MB body → 413" || bad "oversized body"
[ "$($J -o /dev/null -w '%{http_code}' -d '{nope' http://badger.lan/api/board)" = 400 ] && ok "bad JSON → 400" || bad "bad JSON"
[ "$($C -o /dev/null -w '%{http_code}' 'http://badger.lan/api/files?path=../../etc/passwd')" != 200 ] && ok "path traversal refused" || bad "path traversal"
$C 'http://badger.lan/files/../../etc/passwd' | grep -q "root:" && bad "nginx /files traversal" || ok "nginx /files traversal refused"
$C -sI "http://badger.lan/content/kolibri/" | head -1 | grep -qE " (403|404)" && ok "Kolibri database not served" || bad "Kolibri path reachable"
$C -sI -o /dev/null -w '%{http_code}' --max-time 3 "http://$AP:22" >/dev/null 2>&1; $N timeout 3 bash -c "</dev/tcp/$AP/22" 2>/dev/null && bad "SSH reachable from the hotspot" || ok "SSH closed on the hotspot"

echo "== privilege boundary"
for v in trust-maintainer set-pin gen-key finish-provisioning "build-status done 100 x" self-update-hook; do
  sudo -u prepper sudo -n /opt/prepper/bin/prepperctl $v </dev/null >/dev/null 2>&1 && bad "portal can run: $v" || ok "portal refused: $v"
done
sudo -u prepper sudo -n /bin/cat /etc/shadow >/dev/null 2>&1 && bad "portal can sudo arbitrary commands" || ok "portal can't sudo arbitrary commands"
touch /tmp/victim-root; echo original > /tmp/victim-root
sudo -u prepper ln -sfn /tmp/victim-root /run/prepper/llama-variant 2>/dev/null
sudo -u prepper sudo -n /opt/prepper/bin/prepperctl power-mode balanced >/dev/null 2>&1
[ "$(cat /tmp/victim-root)" = original ] && ok "root doesn't write through planted symlinks in /run/prepper" || bad "root wrote through a symlink planted in /run/prepper"
rm -f /tmp/victim-root; [ -L /run/prepper/llama-variant ] && rm -f /run/prepper/llama-variant
perm=$(stat -c '%a %U:%G' /etc/prepper/prepper.toml); [ "$perm" = "640 root:prepper" ] && ok "config 0640 root:prepper" || bad "config is $perm"
sudo -u kiwix cat /etc/prepper/prepper.toml >/dev/null 2>&1 && bad "kiwix user can read the config" || ok "other service users can't read the config"
[ ! -e /boot/firmware/prepper.toml ] && ok "no secrets left on the boot partition" || bad "prepper.toml still on the boot partition"
perm=$(stat -c '%a %U' /etc/prepper/panel.key 2>/dev/null); [ "$perm" = "600 root" ] && ok "panel key 0600 root" || bad "panel key is '$perm'"
perm=$(stat -c '%a %U:%G' /etc/prepper/panel.key.sha256 2>/dev/null); [ "$perm" = "640 root:prepper" ] && ok "panel key hash 0640 root:prepper" || bad "panel key hash is '$perm'"
sudo -u prepper cat /etc/prepper/panel.key >/dev/null 2>&1 && bad "the portal user can read the panel key" || ok "only root can read the panel key"
code=$(sudo -u prepper curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' -d '{}' http://127.0.0.1:8000/api/local/invite)
[ "$code" = 403 ] && ok "a local process without the panel key can't make invites" || bad "local invite without the key answered $code"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' -H "X-Badger-Panel: $(cat /etc/prepper/panel.key)" -d '{}' http://127.0.0.1:8000/api/local/invite)
[ "$code" = 200 ] && ok "the panel key makes invites" || bad "local invite with the key answered $code"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' -H "X-Badger-Panel: $(cat /etc/prepper/panel.key)" -d '{}' http://127.0.0.1/api/local/invite)
[ "$code" = 403 ] && ok "nginx drops the panel key" || bad "panel key through nginx answered $code"
grep -q 'ExecStartPost=-/opt/prepper/os/bin/prepperctl wifi-rearm' /etc/systemd/system/prepper-net.service && ok "Wi-Fi switch-back re-armed at boot" || bad "prepper-net doesn't re-arm the Wi-Fi switch-back"

echo "== hardware absent → fails safe"
journalctl -b -u prepper-powerd --no-pager | grep -qi "traceback" && bad "powerd crashed" || ok "powerd runs without UPS/meter hardware"
for u in prepper-panel prepper-alertd prepper-meshbridge; do
  st=$(systemctl show -p Result --value "$u"); [ "$st" = success ] && ok "$u exits cleanly without hardware" || bad "$u result: $st"
done
# No LoRa radio attached: the bridge must report "no radio", not a link. (meshtastic 2.7 hands back a half-built
# interface when no device exists; the bridge used to take that for a connected node.) The unit's Result says
# nothing about this — check its heartbeat and what the launcher shows.
[ ! -e /run/prepper/mesh.ok ] && ok "meshbridge heartbeat absent with no radio" || bad "meshbridge wrote its heartbeat (mesh.ok) with no radio attached"
mesh=$(curl -s --max-time 5 http://127.0.0.1:8000/api/status | python3 -c 'import json,sys; print(json.load(sys.stdin).get("services", {}).get("mesh"))' 2>/dev/null)
[ "$mesh" = False ] && ok "launcher shows the mesh not connected (no radio)" || bad "launcher status says mesh=$mesh with no radio attached"
if systemctl is-active --quiet prepper-meshbridge; then
  journalctl -b -u prepper-meshbridge --no-pager | grep -q "no Meshtastic" && ok "meshbridge logs that no radio was found" || bad "meshbridge never said it found no radio"
  journalctl -b -u prepper-meshbridge --no-pager | grep -q "meshbridge: connected" && bad "meshbridge claimed a connection with no radio" || ok "meshbridge didn't claim a connection"
  journalctl -b -u prepper-meshbridge --no-pager | grep -q "map snapshot failed" && bad "meshbridge map snapshot errors with no radio" || ok "no map snapshot errors without a radio"
fi
ok_restarts=$(systemctl show -p NRestarts --value prepper-powerd); [ "${ok_restarts:-0}" -lt 3 ] && ok "powerd not crash-looping (${ok_restarts} restarts)" || bad "powerd restarted $ok_restarts times"

echo
echo "RESULT: $pass passed, $fail failed"
[ "$fail" = 0 ]
