# shellcheck shell=bash
# Sensor station (feature "sensors"): firmware switches for 1-Wire (and UART0 if a sensor uses
# it), group membership, and the sensord unit. Sourced by os/install.sh in a subshell with OPT,
# CONF, toml and step; every step tolerates failure so the rest of the install carries on.
#
# Why sensord is enabled even with no [[sensors.device]] configured: it finds a BME280/SHT
# sensor or a DS18B20 probe by itself when one is plugged in, and it shows readings that
# Meshtastic sensor nodes send over the mesh — neither needs any config. With nothing to read
# it wakes once a second, reads nothing and costs well under 0.1 % CPU.
# [sensors] enabled = false (or [features] sensors = false) leaves it disabled.

step "Sensors"

sensors_on=1
[ "$(toml sensors.enabled)" = "False" ] && sensors_on=0
[ "$(toml features.sensors)" = "False" ] && sensors_on=0

# gpioget (float switch) and i2c-tools come with the core packages; make sure anyway.
command -v gpioget >/dev/null 2>&1 || apt-get install -y -q --no-install-recommends gpiod >/dev/null 2>&1 \
  || echo "warning: could not install gpiod — a float switch can't be read"

# prepper-sensord.service names these in SupplementaryGroups=: a group that doesn't exist (gpio and
# i2c exist only on Raspberry Pi OS) stops the unit from starting at all, and it restart-loops. So
# create any that are missing (system group; -f: no error if it appeared meanwhile).
for g in i2c dialout gpio; do
  getent group "$g" >/dev/null 2>&1 || groupadd -rf "$g" 2>/dev/null \
    || echo "warning: could not create group $g — prepper-sensord may not start"
  if getent group "$g" >/dev/null 2>&1; then
    usermod -aG "$g" prepper 2>/dev/null || echo "warning: could not add prepper to group $g"
  fi
done

# Data folder: the portal's feature folder, shared with the portal (both run as prepper).
install -d -o prepper -g prepper -m 750 /var/lib/prepper/portal/features 2>/dev/null || true
install -d -o prepper -g prepper -m 750 /var/lib/prepper/portal/features/sensors 2>/dev/null || true

# Firmware: only append what is missing, never touch existing lines. install.sh appends its own
# settings to the end of the same file; a line after a [pi4]/[cm5]… filter would only apply to
# that model, so an [all] goes first when the last section header isn't one.
CFGTXT=${PREPPER_CFGTXT:-/boot/firmware/config.txt}       # override for tests only
sensors_cfg() {
  grep -qE "$1" "$CFGTXT" 2>/dev/null && return 0
  last=$(grep -E '^\[[^]]+\]' "$CFGTXT" 2>/dev/null | tail -n 1 || true)
  if [ -n "$last" ] && [ "$last" != "[all]" ]; then echo "[all]" >> "$CFGTXT"; fi
  echo "$2" >> "$CFGTXT" && echo "   added to config.txt: $2 (takes effect after a reboot)"
}
if [ "$sensors_on" = 1 ] && [ -f "$CFGTXT" ] && [ -w "$CFGTXT" ]; then
  if [ "$(toml sensors.onewire)" != "False" ]; then
    w1pin=$(toml sensors.onewire_gpio)
    case "$w1pin" in ''|*[!0-9]*) w1pin=4 ;; esac
    # any existing w1-gpio overlay (maybe on another pin) is left alone
    sensors_cfg '^[[:space:]]*dtoverlay=w1-gpio' "dtoverlay=w1-gpio,gpiopin=$w1pin" || true
  fi
  # UART0 on GPIO 14/15 only when a configured sensor uses /dev/ttyAMA0
  uses_uart=$(python3 - "$CONF" <<'PY' 2>/dev/null || true
import sys, tomllib
try:
    d = tomllib.load(open(sys.argv[1], "rb")).get("sensors", {}).get("device", [])
    print(int(any(isinstance(x, dict) and str(x.get("port", "")) in ("/dev/ttyAMA0", "/dev/serial0") for x in d)))
except Exception:
    print(0)
PY
)
  if [ "$uses_uart" = 1 ]; then
    sensors_cfg '^[[:space:]]*dtparam=uart0=on' "dtparam=uart0=on" || true
    if grep -qE 'console=(serial0|ttyAMA0)' "$(dirname "$CFGTXT")/cmdline.txt" 2>/dev/null; then
      echo "warning: the serial console is on UART0 — remove console=serial0,… from /boot/firmware/cmdline.txt for the ultrasonic sensor"
    fi
  fi
elif [ "$sensors_on" = 1 ]; then
  echo "note: no writable $CFGTXT (not a Raspberry Pi?) — 1-Wire not enabled; I²C sensors still work if the bus is on"
fi

if [ "$sensors_on" = 1 ]; then
  systemctl enable prepper-sensord >/dev/null 2>&1 || echo "warning: could not enable prepper-sensord"
  systemctl try-restart prepper-sensord >/dev/null 2>&1 || true
else
  systemctl disable --now prepper-sensord >/dev/null 2>&1 || true
fi
