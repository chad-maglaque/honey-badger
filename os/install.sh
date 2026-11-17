#!/bin/bash
# Honey Badger — installer for Raspberry Pi OS Lite 64-bit (Trixie or
# Bookworm) on a Pi 5. Idempotent; safe to re-run. --upgrade skips slow steps
# whose pinned version hasn't changed.
#
#   sudo bash os/install.sh            # full install
#   sudo bash os/install.sh --upgrade  # after a software update
set -euo pipefail
# --upgrade is accepted for prepperctl self-update-hook; every slow step is
# already skipped when its pinned version stamp is unchanged, so it changes nothing else.
case "${1:-}" in ""|--upgrade) ;; *) echo "usage: install.sh [--upgrade]"; exit 2 ;; esac
HERE=$(cd "$(dirname "$0")/.." && pwd)
OPT=/opt/prepper
CONF=/etc/prepper/prepper.toml
. "$HERE/os/versions.env"
[ "$(id -u)" = 0 ] || { echo "run as root (sudo)"; exit 1; }
[ "$(uname -m)" = aarch64 ] || echo "warning: not aarch64 — binary downloads below assume a 64-bit Pi"
step() { echo; echo "── $*"; [ -x "$OPT/os/bin/prepperctl" ] && "$OPT/os/bin/prepperctl" build-status installing "${PCT:-5}" "$*" 2>/dev/null || true; }
fetch() { curl -fL --retry 5 --retry-delay 5 -C - -o "$2" "$1"; }   # resumable
toml() { python3 -c "import tomllib,sys;d=tomllib.load(open('$CONF','rb'))
for k in sys.argv[1].split('.'): d=d.get(k,{}) if isinstance(d,dict) else {}
print(d if not isinstance(d,(dict,list)) else '')" "$1"; }
stamp() { [ -f "/var/lib/prepper/stamp.$1" ] && [ "$(cat "/var/lib/prepper/stamp.$1")" = "$2" ]; }
setstamp() { echo "$2" > "/var/lib/prepper/stamp.$1"; }
sysuser() {  # sysuser NAME HOME — a locked, login-less service account
  id "$1" >/dev/null 2>&1 || useradd --system --home-dir "$2" --no-create-home --shell /usr/sbin/nologin "$1"
}

PCT=3 step "System packages"
export DEBIAN_FRONTEND=noninteractive
# Core: the Badger does not work without these — a missing one fails the install.
CORE_PKGS=(nginx hostapd dnsmasq python3 python3-venv python3-pip git rsync curl ca-certificates
  iw rfkill wireless-regdb chrony gpsd gpsd-clients pps-tools i2c-tools gpiod openssl nftables
  zim-tools rtl-sdr multimon-ng exfatprogs ntfs-3g cmake build-essential pkg-config
  python3-pil fonts-dejavu-core unzip)
# Pi hardware extras (front panel / GPIO); some exist only in the Raspberry Pi
# OS archive. Each is installed if the archive has it, otherwise skipped with
# a warning (plain Debian, a test VM) — on Pi OS all of them are found.
PI_PKGS=(python3-gpiozero python3-lgpio)
apt-get update -q
apt-get install -y -q --no-install-recommends "${CORE_PKGS[@]}"
pi_found=()
for p in "${PI_PKGS[@]}"; do
  if apt-cache show "$p" >/dev/null 2>&1; then pi_found+=("$p")
  else echo "warning: package $p is not in this system's archive (not Raspberry Pi OS?) — skipped"; fi
done
if [ "${#pi_found[@]}" -gt 0 ]; then
  apt-get install -y -q --no-install-recommends "${pi_found[@]}"
fi
systemctl unmask hostapd >/dev/null 2>&1 || true
# We run prepper-hostapd (os/systemd), started by `prepperctl net-up`, not the
# packaged hostapd units. dnsmasq is the packaged unit with our drop-in config:
# disable it at boot (net-up starts it) but never stop a running one here —
# an upgrade would cut DHCP/DNS for every phone until the install finished.
systemctl disable --now hostapd >/dev/null 2>&1 || true
systemctl disable dnsmasq >/dev/null 2>&1 || true

PCT=5 step "Users, folders, software"
# One account per service: the portal/mesh/alerts ("prepper", the only one
# allowed to sudo prepperctl), kiwix-serve, llama-server and Kolibri each
# get their own, so a bug in one cannot reach the others' files or root.
sysuser prepper /var/lib/prepper/portal
usermod -d /var/lib/prepper/portal prepper 2>/dev/null || true     # older installs: home was /var/lib/prepper
# One group at a time: usermod -aG fails as a whole if any group is missing
# (i2c and gpio exist only on Raspberry Pi OS), which would drop dialout too.
for g in dialout plugdev i2c gpio; do
  getent group "$g" >/dev/null && usermod -aG "$g" prepper || echo "warning: group $g missing — prepper not added"
done
sysuser kiwix /nonexistent
sysuser llama /nonexistent
sysuser kolibri /var/lib/kolibri
# /var/lib/prepper is root's (build status, update state, power mode, stamps):
# if "prepper" could write here it could plant symlinks that root later
# writes through. The portal gets its own subfolder.
mkdir -p /etc/prepper /var/lib/prepper
chown root:root /var/lib/prepper; chmod 755 /var/lib/prepper
find /var/lib/prepper -mindepth 1 -maxdepth 1 -type l -delete            # nothing root writes may be a link
install -d -m 750 -o prepper -g prepper /var/lib/prepper/portal
for f in board.jsonl mesh-outbox.jsonl portal-settings.json; do          # move files from older layouts
  if [ -f "/var/lib/prepper/$f" ] && [ ! -e "/var/lib/prepper/portal/$f" ]; then
    mv "/var/lib/prepper/$f" "/var/lib/prepper/portal/$f"; chown prepper:prepper "/var/lib/prepper/portal/$f"
  fi
done
find /var/lib/prepper \( -path /var/lib/prepper/portal -o -path /var/lib/prepper/sdr \) -prune -o -user prepper -exec chown -h root:root {} +
install -d -m 750 -o kolibri -g kolibri /var/lib/kolibri
# /run/prepper: heartbeats and alerts from the prepper-user daemons, power
# status and the AI variant from root. Sticky (1775): prepper cannot delete or
# replace root's files there, and root only ever writes them by mkstemp +
# rename, so a planted symlink is replaced, never followed.
echo "d /run/prepper 1775 root prepper -" > /etc/tmpfiles.d/prepper.conf
systemd-tmpfiles --create /etc/tmpfiles.d/prepper.conf
# (The new software itself is synced into /opt/prepper only after every
# download below has worked, so a failed download never leaves it half-applied.)
# 0640 root:prepper: the portal, mesh bridge and alert decoder (user prepper)
# read it; it holds the PIN hash and Wi-Fi passphrases, so nobody else may.
if [ ! -f "$CONF" ]; then
  install -m 640 -o root -g prepper "$HERE/os/prepper.toml.example" "$CONF"
  # A hand-run install (no builder): a random Wi-Fi password, never an unprotected hotspot by default.
  # Shown at the end of this script and on the front panel (Join Wi-Fi).
  python3 - "$CONF" <<'PY'
import re, secrets, sys
p = sys.argv[1]
t = open(p, encoding="utf-8").read()
a = "abcdefghjkmnpqrstuvwxyz23456789"
pw = "-".join("".join(secrets.choice(a) for _ in range(4)) for _ in range(3))
t = re.sub(r'(?m)^passphrase = ""', 'passphrase = "' + pw + '"', t, count=1)
open(p, "w", encoding="utf-8").write(t)
PY
fi
chown root:prepper "$CONF"; chmod 640 "$CONF"
# Configs from older releases: portal files moved to /var/lib/prepper/portal
# (the portal can no longer write /var/lib/prepper) and llama-server's API
# lives under /ai. Rewritten in place, keeping owner and mode.
python3 - "$CONF" <<'EOF'
import os, re, sys, tomllib
path = sys.argv[1]; t = old = open(path).read()
t = t.replace('"/var/lib/prepper/portal-settings.json"', '"/var/lib/prepper/portal/portal-settings.json"')
t = re.sub(r'(?m)^llama = "http://127\.0\.0\.1:8090/?"', 'llama = "http://127.0.0.1:8090/ai"', t)
# Hooks are keyed by item id now: the self-update hook belongs to prepper_software only.
t = re.sub(r'(?m)^app = "/opt/prepper/bin/prepperctl self-update-hook"',
           'prepper_software = "/opt/prepper/bin/prepperctl self-update-hook"', t)
portal = tomllib.loads(t).get("portal", {})
for key, name in (("mesh_outbox", "mesh-outbox.jsonl"), ("board_file", "board.jsonl")):
    if key not in portal:
        t = re.sub(r"(?m)^(\[portal\]\n)", rf'\g<1>{key} = "/var/lib/prepper/portal/{name}"\n', t, count=1)
if t != old:
    tomllib.loads(t)
    st = os.stat(path)
    with open(path + ".tmp", "w") as fh:
        fh.write(t)
    os.chown(path + ".tmp", st.st_uid, st.st_gid); os.chmod(path + ".tmp", st.st_mode & 0o7777)
    os.replace(path + ".tmp", path)
    print("prepper.toml: moved portal files to /var/lib/prepper/portal, AI under /ai")
EOF
DATA=$(toml upd.data_root); DATA=${DATA:-/srv/prepper}
mkdir -p "$DATA"/{zim,maps,docs,books,video,audio,models,apps,apk,data,kolibri,sync,.staging}
# Older installs kept Kolibri's home (database included) in $DATA/kolibri,
# which nginx serves at /content/. Move it out of reach.
if [ -e "$DATA/kolibri/db.sqlite3" ] || [ -e "$DATA/kolibri/options.ini" ]; then
  if [ -z "$(ls -A /var/lib/kolibri)" ]; then
    find "$DATA/kolibri" -mindepth 1 -maxdepth 1 -exec mv -t /var/lib/kolibri/ {} +
    chown -R kolibri:kolibri /var/lib/kolibri
  else
    echo "warning: Kolibri data in both $DATA/kolibri and /var/lib/kolibri — remove the old copy by hand"
  fi
fi
install -m 440 "$HERE/os/systemd/prepper-sudoers" /etc/sudoers.d/prepper
visudo -cf /etc/sudoers.d/prepper >/dev/null

PCT=6 step "Kiwix tools $KIWIX_TOOLS"
if ! stamp kiwix "$KIWIX_TOOLS"; then
  t=$(mktemp -d); fetch "https://download.kiwix.org/release/kiwix-tools/kiwix-tools_linux-aarch64-$KIWIX_TOOLS.tar.gz" "$t/k.tgz"
  tar -xzf "$t/k.tgz" -C "$t"; install -m 755 "$t"/kiwix-tools_*/kiwix-* /usr/local/bin/; rm -rf "$t"
  setstamp kiwix "$KIWIX_TOOLS"
fi

PCT=7 step "PMTiles CLI $GO_PMTILES"
if ! stamp pmtiles "$GO_PMTILES"; then
  t=$(mktemp -d); fetch "https://github.com/protomaps/go-pmtiles/releases/download/v$GO_PMTILES/go-pmtiles_${GO_PMTILES}_Linux_arm64.tar.gz" "$t/p.tgz"
  tar -xzf "$t/p.tgz" -C "$t"; install -m 755 "$t/pmtiles" /usr/local/bin/pmtiles; rm -rf "$t"
  setstamp pmtiles "$GO_PMTILES"
fi

PCT=8 step "Web viewers (maps, PDF, eBooks) — vendored, nothing loads from the internet at runtime"
V=$OPT/vendor; mkdir -p "$V"
npm_get() {  # npm_get <package> <version> <dest-dir>  (no node needed: registry tarballs)
  local pkg=$1 ver=$2 dest=$3 base t; base=${pkg##*/}; t=$(mktemp -d)
  fetch "https://registry.npmjs.org/$pkg/-/$base-$ver.tgz" "$t/p.tgz"
  rm -rf "$dest"; mkdir -p "$dest"; tar -xzf "$t/p.tgz" -C "$t"; cp -r "$t/package/dist/." "$dest/"; rm -rf "$t"
}
key="$MAPLIBRE_GL-$PMTILES_JS-$PROTOMAPS_BASEMAPS-$LEAFLET-$PROTOMAPS_LEAFLET-$PDFJS"
if ! stamp vendor "$key"; then
  npm_get maplibre-gl "$MAPLIBRE_GL" "$V/maplibre-gl"
  npm_get pmtiles "$PMTILES_JS" "$V/pmtiles"
  npm_get @protomaps/basemaps "$PROTOMAPS_BASEMAPS" "$V/basemaps"
  npm_get leaflet "$LEAFLET" "$V/leaflet"
  npm_get protomaps-leaflet "$PROTOMAPS_LEAFLET" "$V/protomaps-leaflet"
  t=$(mktemp -d); git clone -q --depth 1 --branch "$BASEMAPS_ASSETS_REF" https://github.com/protomaps/basemaps-assets "$t/a"
  cp -r "$t/a/fonts" "$t/a/sprites" "$V/basemaps/"; rm -rf "$t"
  t=$(mktemp -d); fetch "https://github.com/mozilla/pdf.js/releases/download/v$PDFJS/pdfjs-$PDFJS-dist.zip" "$t/p.zip"
  rm -rf "$DATA/apps/pdfjs"; mkdir -p "$DATA/apps/pdfjs"; unzip -q "$t/p.zip" -d "$DATA/apps/pdfjs"; rm -rf "$t"
  # foliate-js is code served to phones: fetch exactly the pinned commit and verify it (a full 40-hex SHA, never a branch).
  case "$FOLIATE_JS_REF" in *[!0-9a-f]*|"") echo "FOLIATE_JS_REF must be a full commit hash" >&2; exit 1;; esac
  [ "${#FOLIATE_JS_REF}" -eq 40 ] || { echo "FOLIATE_JS_REF must be a 40-character commit hash" >&2; exit 1; }
  rm -rf "$DATA/apps/foliate-js"; mkdir -p "$DATA/apps/foliate-js"
  git -C "$DATA/apps/foliate-js" init -q
  git -C "$DATA/apps/foliate-js" fetch -q --depth 1 https://github.com/johnfactotum/foliate-js "$FOLIATE_JS_REF"
  git -C "$DATA/apps/foliate-js" checkout -q --detach FETCH_HEAD
  [ "$(git -C "$DATA/apps/foliate-js" rev-parse HEAD)" = "$FOLIATE_JS_REF" ] || { echo "foliate-js: wrong commit checked out" >&2; exit 1; }
  setstamp vendor "$key"
fi

if [ "$(toml ai.enabled)" != "False" ]; then
  PCT=9 step "llama.cpp $LLAMA_CPP_REF (builds on the Pi; ~15 min the first time)"
  if ! stamp llama "$LLAMA_CPP_REF"; then
    t=$(mktemp -d); git clone -q --depth 1 --branch "$LLAMA_CPP_REF" https://github.com/ggml-org/llama.cpp "$t/l"
    # LLAMA_OPENSSL=OFF: the server only listens on localhost behind nginx and
    # never downloads models itself (LLAMA_CURL is gone since v1.0.0).
    cmake -S "$t/l" -B "$t/l/build" -DCMAKE_BUILD_TYPE=Release -DGGML_NATIVE=ON -DLLAMA_OPENSSL=OFF -DLLAMA_BUILD_TESTS=OFF >/dev/null
    cmake --build "$t/l/build" --target llama-server -j4
    mkdir -p "$OPT/llama/bin"; install -m 755 "$t/l/build/bin/llama-server" "$OPT/llama/bin/"
    rm -rf "$t"; setstamp llama "$LLAMA_CPP_REF"
  fi
fi

PCT=9 step "Python environment (panel, mesh bridge, Kolibri)"
[ -d "$OPT/venv" ] || python3 -m venv --system-site-packages "$OPT/venv"
"$OPT/venv/bin/pip" install -q --upgrade luma.oled meshtastic qrcode
if [ "$(toml features.kolibri)" = "True" ] && ! stamp kolibri "$KOLIBRI"; then
  if "$OPT/venv/bin/pip" install -q "kolibri==$KOLIBRI"; then setstamp kolibri "$KOLIBRI"
  else echo "warning: Kolibri $KOLIBRI did not install — courses stay off; re-run install.sh to retry"; fi
fi
if stamp kolibri "$KOLIBRI"; then    # only write a unit for a Kolibri that actually installed
  cat > /etc/systemd/system/kolibri.service <<UNIT
[Unit]
Description=Kolibri learning platform
After=network.target
[Service]
User=kolibri
EnvironmentFile=/etc/default/prepper
ExecStart=$OPT/venv/bin/kolibri start --foreground --port 8080
Restart=on-failure
RestartSec=10
ProtectSystem=strict
ReadWritePaths=/var/lib/kolibri
ProtectHome=true
PrivateTmp=true
NoNewPrivileges=true
[Install]
WantedBy=multi-user.target
UNIT
fi

PCT=9 step "Raspberry Pi firmware settings"
CFGTXT=/boot/firmware/config.txt
if [ -f "$CFGTXT" ]; then
  add_cfg() { grep -qxF "$1" "$CFGTXT" || echo "$1" >> "$CFGTXT"; }
else
  echo "warning: no $CFGTXT (not a Raspberry Pi?) — firmware settings skipped"
  add_cfg() { :; }
fi
add_cfg "# prepper-disk"
add_cfg "dtparam=pciex1"                 # NVMe HAT (Gen 2 — Gen 3 is not certified on the Pi 5)
add_cfg "dtparam=i2c_arm=on"             # UPS fuel gauge + front-panel display
# /dev/i2c-1 for powerd's raw reads needs the i2c-dev driver; raspi-config
# would add it to /etc/modules, but we never run raspi-config.
echo i2c-dev > /etc/modules-load.d/prepper-i2c.conf
modprobe i2c-dev 2>/dev/null || true
add_cfg "usb_max_current_enable=1"       # UPS HAT supplies 5 A; let USB devices draw 1.6 A
add_cfg "dtoverlay=disable-bt"           # Bluetooth unused; saves power
if [ "$(toml power.rtc_battery)" = "ml2020" ]; then add_cfg "dtparam=rtc_bbat_vchg=3000000"; fi   # only for the rechargeable cell!
if command -v rpi-eeprom-config >/dev/null; then
  # Only when a setting differs: --apply stages a bootloader flash for the next
  # boot, which must not happen on every software update.
  t=$(mktemp); rpi-eeprom-config > "$t" || : > "$t"
  have=$(grep -E '^(POWER_OFF_ON_HALT|PSU_MAX_CURRENT|BOOT_ORDER)=' "$t" | sort -t= -k1,1 | tr '\n' ' ' | sed 's/ $//')
  if [ "$have" != "BOOT_ORDER=0xf16 POWER_OFF_ON_HALT=1 PSU_MAX_CURRENT=5000" ]; then
    sed -i '/^POWER_OFF_ON_HALT=/d;/^PSU_MAX_CURRENT=/d;/^BOOT_ORDER=/d' "$t"
    # NVMe, then SD — no USB boot: nobody with the box in hand boots their own OS from a stick
    printf 'POWER_OFF_ON_HALT=1\nPSU_MAX_CURRENT=5000\nBOOT_ORDER=0xf16\n' >> "$t"
    rpi-eeprom-config --apply "$t" >/dev/null || echo "warning: could not stage the EEPROM settings"
  fi
  rm -f "$t"
else
  echo "note: rpi-eeprom-config not found (not a Pi 5?) — EEPROM settings skipped"
fi
# No swap on the NVMe: the document vault holds decrypted pieces in the portal's memory, and those
# must never be written to disk. (zram swap, in RAM, is fine and left alone.)
if command -v dphys-swapfile >/dev/null 2>&1; then
  dphys-swapfile swapoff 2>/dev/null || true
  systemctl disable --now dphys-swapfile >/dev/null 2>&1 || true
fi
mkdir -p /etc/systemd/system.conf.d /etc/systemd/journald.conf.d
printf '[Manager]\nRuntimeWatchdogSec=15\nRebootWatchdogSec=2min\n' > /etc/systemd/system.conf.d/prepper-watchdog.conf
# The journal lives in RAM only (gone at power-off): no diary of who came and went for whoever holds the disk.
printf '[Journal]\nStorage=volatile\nRuntimeMaxUse=48M\n' > /etc/systemd/journald.conf.d/prepper.conf
rm -rf /var/log/journal                                       # an older persistent journal
systemctl restart systemd-journald 2>/dev/null || true
# Same for nginx (render.py turns its access log off) and the DHCP leases (phone names, kept in /run).
rm -f /var/log/nginx/access.log* /var/lib/misc/dnsmasq.leases
echo "d /run/prepper-dnsmasq 0750 dnsmasq root -" > /etc/tmpfiles.d/prepper-dnsmasq.conf
systemd-tmpfiles --create /etc/tmpfiles.d/prepper-dnsmasq.conf 2>/dev/null || true
# USB sticks mount under /media for file sharing and update bundles.
cat > /etc/udev/rules.d/99-prepper-usb.rules <<'RULE'
ACTION=="add", SUBSYSTEMS=="usb", SUBSYSTEM=="block", ENV{ID_FS_USAGE}=="filesystem", RUN{program}+="/usr/bin/systemd-mount --no-block --automount=yes --collect -o noexec,nosuid,nodev $devnode /media/usb-%k"
RULE
# Weather alerts: start the decoder when an RTL-SDR is plugged in (it exits
# quietly when none is present instead of restart-looping).
if [ "$(toml radio.sdr_weather)" != "off" ]; then
  cat > /etc/udev/rules.d/99-prepper-sdr.rules <<'RULE'
ACTION=="add", SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ATTR{idVendor}=="0bda", ATTR{idProduct}=="2832|2838", TAG+="systemd", ENV{SYSTEMD_WANTS}+="prepper-alertd.service"
RULE
else
  rm -f /etc/udev/rules.d/99-prepper-sdr.rules
fi
udevadm control --reload || true

PCT=9 step "Software"
# Every download above worked: now sync the new software into place.
# Excludes: built or linked here by this script — --delete must not remove them.
if [ "$HERE" != "$OPT" ]; then
  rsync -a --delete --exclude /.git --exclude /venv --exclude /llama --exclude /vendor --exclude /bin "$HERE/" "$OPT/"
fi
ln -sfn "$OPT/os/bin" "$OPT/bin"
chmod 755 "$OPT"/os/bin/*

PCT=9 step "Configuration, keys, certificates"
python3 "$OPT/os/render.py" --config "$CONF"
ln -sfn /etc/nginx/sites-available/prepper /etc/nginx/sites-enabled/prepper
rm -f /etc/nginx/sites-enabled/default
[ "$(toml network.https)" != "False" ] && [ ! -f /etc/prepper/tls/server.crt ] && "$OPT/os/bin/prepperctl" gen-tls
[ -f /etc/prepper/keys/disk_ed25519 ] || "$OPT/os/bin/prepperctl" gen-key
grep -q '^server ' /etc/chrony/chrony.conf || true
for u in "$OPT"/os/systemd/*.service "$OPT"/os/systemd/*.timer "$OPT"/os/systemd/*.path; do
  [ -f "$u" ] || continue                     # (a pattern with no match stays literal)
  install -m 644 "$u" /etc/systemd/system/
done
# The front panel's key (root-only) and its hash for the portal (root:prepper 0640): how the
# portal tells the Badger's own screen from any other local process (see docs/SECURITY.md).
python3 "$OPT/software/panel/panel.py" --make-key || echo "warning: could not make the front panel's key (panel invites stay off)"
systemctl daemon-reload
# Feature install fragments: each os/install.d/*.sh installs and enables what one feature needs
# (packages, models, units). They run with OPT, CONF and the toml/step helpers available, and a
# failing fragment is reported but never stops the rest of the install.
for frag in "$OPT"/os/install.d/*.sh; do
  [ -f "$frag" ] || continue
  echo "-- feature: $(basename "$frag" .sh)"
  # shellcheck source=/dev/null
  ( . "$frag" ) || echo "!! feature $(basename "$frag" .sh) did not install completely (the rest of the Badger is fine)"
done

PCT=9 step "Enable services"
systemctl enable prepper-net nginx prepper-portal kiwix-serve prepper-powerd prepper-power-restore chrony gpsd \
  upd-check.timer upd-apply.timer >/dev/null
[ "$(toml ai.enabled)" != "False" ] && systemctl enable llama-server >/dev/null
[ "$(toml panel.enabled)" != "False" ] && [ "$(toml panel.display)" != "none" ] && systemctl enable prepper-panel >/dev/null
[ "$(toml features.kolibri)" = "True" ] && [ -f /etc/systemd/system/kolibri.service ] && systemctl enable kolibri >/dev/null 2>&1 || true
[ "$(toml radio.mesh)" != "off" ] && systemctl enable prepper-meshbridge >/dev/null
[ "$(toml radio.sdr_weather)" != "off" ] && systemctl enable prepper-alertd >/dev/null
"$OPT/os/bin/prepperctl" net-up || echo "(Wi-Fi AP will come up on next boot)"
systemctl restart nginx prepper-portal prepper-powerd || true
for u in kiwix-serve llama-server prepper-meshbridge prepper-alertd kolibri; do   # pick up new units/users
  systemctl try-restart "$u" 2>/dev/null || true
done
systemctl start --no-block kiwix-serve || true      # skips itself until a ZIM exists
[ "$(toml ai.enabled)" != "False" ] && systemctl start --no-block llama-server || true   # idles until a model lands
"$OPT/os/bin/prepperctl" power-restore >/dev/null || true     # the saved mode; first install: [power] default_mode
# A software update (or a hand-run install) is finished now; while
# provisioning, the first content download decides when the build is done.
if [ -z "${PREPPER_FIRSTBOOT:-}" ] && [ ! -e /var/lib/prepper/first-pull-pending ]; then
  "$OPT/os/bin/prepperctl" build-status "done" 100 "Ready" || true
fi
psk=$(toml network.passphrase)
echo; echo "Done. Join Wi-Fi '$(toml network.ssid)'${psk:+ (password: $psk)} and open http://$(toml network.hostname).lan"
[ -z "$(toml portal.admin_pin)" ] && echo "Set an admin PIN:  sudo prepperctl set-pin"
exit 0
