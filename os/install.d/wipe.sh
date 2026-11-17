# shellcheck shell=bash
# Honey Badger — panic wipe / handover reset: boot-time resume, the dead-man timer if armed, and the made-up
# household a duress session shows (always ready: docs/SECURITY.md → "Duress").
#
# Sourced by os/install.sh (with OPT, CONF and the toml/step helpers), or run on its own on the Pi:
#     sudo bash /opt/prepper/os/install.d/wipe.sh
# Nothing is downloaded. Idempotent; a failure here never stops the rest of the install.

: "${OPT:=/opt/prepper}"
: "${CONF:=/etc/prepper/prepper.toml}"
command -v step >/dev/null 2>&1 || step() { echo; echo "── $*"; }

wipe_install() {
  step "Panic wipe: finish an interrupted wipe at boot"
  systemctl enable prepper-wipe-resume.service >/dev/null 2>&1 || echo "!! wipe: could not enable prepper-wipe-resume"
  # The dead-man timer runs only while armed (prepperctl deadman-set … --confirm enables it).
  if [ -s /var/lib/prepper/deadman-state.json ]; then
    systemctl enable prepper-deadman.timer >/dev/null 2>&1 || true
  else
    systemctl disable prepper-deadman.timer >/dev/null 2>&1 || true
  fi
  step "Duress: the made-up household (kept if one exists)"
  local pdir=/var/lib/prepper/portal
  if [ -d "$pdir" ]; then
    if id prepper >/dev/null 2>&1; then
      runuser -u prepper -- python3 "$OPT/software/common/decoy.py" ensure "$pdir/decoy.json" \
        || echo "!! decoy: not made now (the launcher makes it on its first start)"
    else
      python3 "$OPT/software/common/decoy.py" ensure "$pdir/decoy.json" || true
    fi
  fi
}
wipe_install || true
