#!/bin/bash
# First boot on a freshly flashed Badger. The builder put the software bundle and
# prepper.toml on the boot partition; this installs everything, brings up the
# Wi-Fi so you can watch progress from a phone, reboots once so the firmware
# settings take effect, then the Badger downloads its own content straight to its
# drive (prepper-firstpull.service) — nothing passes through or is stored on the laptop.
#
# If anything fails, the next boot tries again (the unit stays enabled until
# /var/lib/prepper/firstboot.done exists). Secrets never stay on the FAT boot
# partition either way: anyone holding the drive could read them there.
set -euo pipefail
# Test seam: PREPPER_TEST_ROOT prefixes every absolute path (boot partition,
# /etc, /var, /opt) so tests can run this unprivileged against a scratch tree
# with system commands stubbed on PATH. Ignored when running as root.
R=""
if [ -n "${PREPPER_TEST_ROOT:-}" ] && [ "$(id -u)" != 0 ]; then R=$PREPPER_TEST_ROOT; fi
BOOT=$R/boot/firmware
SEED=$BOOT/prepper
ETC=$R/etc/prepper
STATE=$R/var/lib/prepper
OPT=$R/opt/prepper
CONF=$ETC/prepper.toml
LOG=$R/var/log/prepper-firstboot.log
mkdir -p "$(dirname "$LOG")"
exec > >(tee -a "$LOG") 2>&1
echo "== Honey Badger first boot $(date '+%Y-%m-%dT%H:%M:%S%z')"
ctl=$OPT/os/bin/prepperctl
export PREPPER_FIRSTBOOT=1        # install.sh: provisioning is not done when you finish

scrub() { [ -f "$1" ] && { shred -u "$1" 2>/dev/null || rm -f "$1"; }; return 0; }
scrub_boot() {
  # Each one goes only once its copy in /etc exists, so a failure early on
  # cannot lose the config the retry needs.
  cmp -s "$SEED/prepper.toml" "$CONF" && scrub "$SEED/prepper.toml"
  cmp -s "$SEED/proxy.env" "$ETC/proxy.env" && scrub "$SEED/proxy.env"
  cmp -s "$SEED/admin_pin" "$ETC/admin_pin.pending" && scrub "$SEED/admin_pin"
  # cloud-init seed: user-data holds the SSH password hash, network-config the
  # uplink Wi-Fi passphrase. cloud-init has applied both by now (account in
  # /etc/shadow, network in /etc/netplan) and NoCloud reads the seed again
  # only for a new instance-id, so an empty user-data is all it needs.
  # (Its first line is "#cloud-config" too, so compare the whole file.)
  if [ -f "$BOOT/user-data" ] && [ "$(cat "$BOOT/user-data")" != "#cloud-config" ]; then
    scrub "$BOOT/user-data"; printf '#cloud-config\n' > "$BOOT/user-data"
  fi
  scrub "$BOOT/network-config"
  return 0
}
on_exit() {
  rc=$?
  scrub_boot || true
  if [ "$rc" -ne 0 ]; then
    echo "== first boot FAILED (exit $rc) $(date '+%Y-%m-%dT%H:%M:%S%z') — will retry on the next boot"
    "$ctl" build-status error 0 "Setup failed — see /var/log/prepper-firstboot.log; retrying at next boot" 2>/dev/null || true
  fi
}
trap on_exit EXIT

mkdir -p "$OPT" "$ETC" "$STATE"
chmod 755 "$STATE"
if [ -e "$STATE/firstboot.done" ]; then
  # Already provisioned (run again by hand, or the unit's condition was
  # bypassed): only make sure no secret is left on the boot partition.
  echo "== first boot already done — nothing to do"
  exit 0
fi
# Provisioning starts (over): wlan0 may be the download link again, so it is
# not the hotspot until net-up decides so (see prepperctl net-up).
rm -f "$STATE/ap-wlan0" "$STATE/ap-deferred-since"
if [ -f "$SEED/software.tar.gz" ]; then
  tar -xzf "$SEED/software.tar.gz" -C "$OPT"
fi
# 0600 for now; install.sh makes it 0640 root:prepper once that group exists.
if [ -f "$SEED/prepper.toml" ]; then install -m 600 "$SEED/prepper.toml" "$CONF"; fi
[ -f "$CONF" ] || { echo "no prepper.toml on the boot partition or in /etc/prepper"; exit 1; }
if [ -f "$SEED/proxy.env" ]; then install -m 600 "$SEED/proxy.env" "$ETC/proxy.env"; fi
if [ -f "$SEED/admin_pin" ]; then install -m 600 "$SEED/admin_pin" "$ETC/admin_pin.pending"; fi
scrub_boot
chmod +x "$OPT"/os/bin/* || true
toml() { python3 -c 'import tomllib,sys;d=tomllib.load(open(sys.argv[1],"rb"))
for k in sys.argv[2].split("."): d=d.get(k,{}) if isinstance(d,dict) else {}
print(d if not isinstance(d,(dict,list)) else "")' "$CONF" "$1"; }
set_pin() {  # from the pending file on stdin, never as an argument (visible in ps)
  [ -f "$ETC/admin_pin.pending" ] || return 0
  if "$ctl" set-pin < "$ETC/admin_pin.pending"; then
    scrub "$ETC/admin_pin.pending"
  else
    echo "warning: could not set the admin PIN yet (will retry)"
  fi
  return 0
}

# The admin PIN goes into prepper.toml straight away (hashed) — it needs
# only Python, not the install.
set_pin

# ── Network for the downloads. This runs with NetworkManager up (unlike the
# Bookworm firstrun.sh, which runs before it starts), so it is the place to
# join the uplink Wi-Fi and to set up the laptop-relay cable.
rfkill unblock wlan 2>/dev/null || true
iw reg set "$(toml network.country)" 2>/dev/null || true
if [ -f "$ETC/proxy.env" ]; then
  # Laptop relay: if the builder is acting as a pass-through proxy, use it.
  set -a
  # shellcheck source=/dev/null
  . "$ETC/proxy.env"
  set +a
  http_proxy=${http_proxy:-}; https_proxy=${https_proxy:-$http_proxy}
  # Written into apt's config inside quotes: refuse anything but a plain URL.
  if [[ "$http_proxy" =~ ^https?://[A-Za-z0-9.:@_-]+/?$ && "$https_proxy" =~ ^https?://[A-Za-z0-9.:@_-]+/?$ ]]; then
    mkdir -p "$R/etc/apt/apt.conf.d"
    printf 'Acquire::http::Proxy "%s";\nAcquire::https::Proxy "%s";\n' "$http_proxy" "$https_proxy" > "$R/etc/apt/apt.conf.d/99prepper-proxy"
    chmod 644 "$R/etc/apt/apt.conf.d/99prepper-proxy"
    git config --system http.proxy "$http_proxy"
  else
    echo "warning: ignoring malformed proxy in proxy.env"
  fi
  # Direct cable to the laptop: no DHCP there, so give eth0 the relay address
  # (cloud-init images already have it from network-config).
  if [ -n "${PREPPER_RELAY_ADDR:-}" ] && ! ip -4 -o addr show dev eth0 2>/dev/null | grep -q inet; then
    nmcli con add type ethernet ifname eth0 con-name prepper-relay connection.autoconnect-priority 10 \
      ipv4.method manual ipv4.addresses "$PREPPER_RELAY_ADDR" ipv6.method ignore >/dev/null 2>&1 || true
    nmcli con up prepper-relay >/dev/null 2>&1 || echo "warning: could not bring up the relay link on eth0"
  fi
fi
if ! nmcli -t -f DEVICE,STATE dev 2>/dev/null | grep -qx 'wlan0:connected'; then
  # A 0600 NetworkManager profile per network: the password never appears in a command line.
  python3 "$OPT/os/nm_wifi.py" --dir "$R/etc/NetworkManager/system-connections" --timeout 90 from-config "$CONF" || true
fi

"$ctl" build-status installing 2 "Installing system packages"
bash "$OPT/os/install.sh"

if [ -f "$SEED/trusted_keys" ]; then
  # A bad line is reported, not fatal: failing here would repeat on every boot.
  while IFS= read -r k || [ -n "$k" ]; do
    [ -z "$k" ] || "$ctl" trust "$k" || echo "warning: could not trust key: ${k:0:40}…"
  done < "$SEED/trusted_keys"
fi
set_pin      # the early attempt failed? The install is complete now.

# Reboot once before downloading: config.txt (PCIe, I²C) and the EEPROM
# settings install.sh wrote only take effect on a fresh boot. The content
# pull then starts by itself and, being resumable, picks up again after any
# later reboot until it has run to the end once.
touch "$STATE/first-pull-pending"
"$ctl" build-status content 10 "Restarting, then downloading content"
systemctl enable prepper-firstpull.service upd-apply.timer upd-check.timer
rm -f "$SEED/software.tar.gz" "$SEED/trusted_keys"
touch "$STATE/firstboot.done"
systemctl disable prepper-firstboot.service 2>/dev/null || true
scrub_boot
echo "== first boot done $(date '+%Y-%m-%dT%H:%M:%S%z') — rebooting"
trap - EXIT
systemctl --no-block reboot
