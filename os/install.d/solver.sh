# shellcheck shell=bash
# Honey Badger — Problem solver: the sandboxed Python runner.
#
# Sourced by os/install.sh (with OPT, CONF and the toml/step helpers), or run on its own on the Pi:
#     sudo bash /opt/prepper/os/install.d/solver.sh
# Idempotent and tolerant: a failure here leaves the solver working without its Python tool.
#
# Nothing is downloaded here. The AI models the solver uses are catalog items (content/catalog.toml,
# role = fast | reasoning | coder | endurance) fetched by upd like any other content.
#
# The runner is socket-activated (os/systemd/prepper-solver-sandbox.socket + @.service): each
# request runs in a fresh instance with a dynamic unprivileged user, no network, and the family's
# data inaccessible. The portal refuses to use it unless a probe proves that, so this script also
# runs the probe and prints the verdict.

: "${OPT:=/opt/prepper}"
: "${CONF:=/etc/prepper/prepper.toml}"
command -v step >/dev/null 2>&1 || step() { echo; echo "── $*"; }
command -v toml >/dev/null 2>&1 || toml() {
  python3 -c "import tomllib,sys
try: d=tomllib.load(open(sys.argv[2],'rb'))
except Exception: d={}
for k in sys.argv[1].split('.'): d=d.get(k,{}) if isinstance(d,dict) else {}
print(d if not isinstance(d,(dict,list)) else '')" "$1" "$CONF"
}

solver_warn() { echo "!! solver: $*"; }

solver_install() {
  step "Problem solver: sandboxed Python runner"
  if [ "$(toml features.solver)" = "False" ] || [ "$(toml solver.python)" = "False" ]; then
    systemctl disable --now prepper-solver-sandbox.socket >/dev/null 2>&1 || true
    echo "solver: Python runner switched off in $CONF"
    return 0
  fi
  command -v python3 >/dev/null 2>&1 || { solver_warn "python3 missing"; return 0; }
  local ver
  ver=$(systemctl --version 2>/dev/null | awk 'NR==1 {print $2}')
  case "$ver" in
    ''|*[!0-9]*) solver_warn "can't tell the systemd version; leaving the Python runner off"; return 0 ;;
  esac
  if [ "$ver" -lt 247 ]; then
    solver_warn "systemd $ver is too old for the sandbox (needs 247+: ProtectProc, PrivateIPC); Python runner stays off"
    return 0
  fi
  [ -f /etc/systemd/system/prepper-solver-sandbox.socket ] || [ -f /lib/systemd/system/prepper-solver-sandbox.socket ] \
    || [ -f "$OPT/os/systemd/prepper-solver-sandbox.socket" ] || { solver_warn "sandbox units not found"; return 0; }
  systemctl daemon-reload || true
  if ! systemctl enable --now prepper-solver-sandbox.socket >/dev/null 2>&1; then
    solver_warn "couldn't start prepper-solver-sandbox.socket (see: journalctl -u prepper-solver-sandbox.socket)"
    return 0
  fi
  # Probe as the portal's user, exactly as the portal will (prints "isolated" or the reason it isn't).
  local verdict
  verdict=$(runuser -u prepper -- python3 -I -c '
import sys
sys.path.insert(0, sys.argv[1])
from solver import sandbox
ok, why = sandbox.SandboxClient().status(refresh=True)
print(("OK: " if ok else "NOT ISOLATED: ") + why)
' "$OPT/software" 2>&1 | tail -n 1)
  echo "solver: sandbox probe: ${verdict:-no answer}"
  case "$verdict" in
    OK:*) ;;
    *) solver_warn "the Python tool will stay disabled until the probe passes (the solver works without it)" ;;
  esac
}

solver_install || solver_warn "install step failed; the solver works without its Python tool"
