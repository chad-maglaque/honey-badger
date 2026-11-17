# shellcheck shell=bash
# Honey Badger — radio features: aircraft (readsb, ADS-B 1090 MHz) and weather satellites
# (SatDump, Meteor-M LRPT 137 MHz) on SDR 2, plus the scheduler that shares the dongles
# (prepper-sdr.service) and the daily orbit-data refresh (prepper-tle.timer).
#
# Sourced by os/install.sh (with OPT, CONF and the toml/step helpers), or run on its own on the Pi:
#     sudo bash /opt/prepper/os/install.d/sdr.sh
# Idempotent; each part is independent, and a part that fails is reported without stopping the rest.
#
# SOURCE INTEGRITY: readsb and SatDump are built from source, fetched by exact git commit (a commit
# id is a hash of the whole source tree, so it pins the code like a SHA-256 pins a file). A commit
# that is still empty means "not verified yet": that program is NOT built and the script says what
# to do. To fill one: on a trusted machine, `git ls-remote <repo> refs/tags/<tag>` (use the peeled
# ^{} line for annotated tags), check the release notes, and paste the 40-character id below (or
# pass it in the environment: sudo READSB_COMMIT=… bash sdr.sh).
#
# Build time on a Pi 5: readsb ~1 min, SatDump ~30–45 min (once; a stamp skips it next time).

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

# ── pinned sources ───────────────────────────────────────────────────────────
# readsb (wiedehopf): actively maintained ADS-B decoder, lower CPU than dump1090-fa, selects the
# dongle by serial (--device BADGER-SDR2), writes aircraft.json; not packaged in Debian.
READSB_REPO=https://github.com/wiedehopf/readsb.git
READSB_TAG=v3.16                            # informational: the release the commit below belongs to
: "${READSB_COMMIT:=6927c35799b1edc6801344aca31b1b6d2a3532f4}"   # tag v3.16 ("release 3.16", 2025-08-18; verified)
# SatDump: decodes Meteor-M LRPT into pictures; CLI only (no GUI) on the Badger.
SATDUMP_REPO=https://github.com/SatDump/SatDump.git
SATDUMP_TAG=1.2.2                           # https://github.com/SatDump/SatDump/releases
: "${SATDUMP_COMMIT:=7aef0fe8441bc3eb440b1b6ba053556da5e40991}"  # tag 1.2.2 (latest stable, 2024-11-29; verified)

SDR_STATE=/var/lib/prepper/sdr
SRC=/usr/local/src/prepper-sdr
sdr_warn() { echo "!! sdr: $*"; }
sdr_stamp() { [ -f "/var/lib/prepper/stamp.$1" ] && [ "$(cat "/var/lib/prepper/stamp.$1")" = "$2" ]; }
sdr_setstamp() { mkdir -p /var/lib/prepper && echo "$2" > "/var/lib/prepper/stamp.$1"; }

air_on() { [ "$(toml features.aircraft)" != "False" ]; }
sat_on() { [ "$(toml features.satellites)" != "False" ]; }

# git_at_commit REPO COMMIT DIR — exactly that commit (shallow), or fail.
git_at_commit() {
  local repo=$1 commit=$2 dir=$3
  case "$commit" in
    *[!0-9a-f]*|"") sdr_warn "bad or missing commit id for $repo"; return 1 ;;
  esac
  [ "${#commit}" = 40 ] || { sdr_warn "commit id for $repo must be 40 hex characters"; return 1; }
  rm -rf "$dir" && mkdir -p "$dir" || return 1
  git -C "$dir" init -q &&
    git -C "$dir" remote add origin "$repo" &&
    git -C "$dir" fetch -q --depth 1 origin "$commit" &&
    git -C "$dir" checkout -q FETCH_HEAD || { sdr_warn "could not fetch $repo at $commit"; return 1; }
  [ "$(git -C "$dir" rev-parse HEAD)" = "$commit" ] || { sdr_warn "$repo: fetched commit does not match"; return 1; }
}

# apt_candidate PKG — true if the archive can actually install PKG. `apt-cache show` is not enough:
# it also succeeds for a name that is only referred to by other packages (libvolk2-dev on Debian 13,
# which has libvolk-dev instead), and apt-get then refuses the whole list.
apt_candidate() {
  local c
  c=$(apt-cache policy "$1" 2>/dev/null | awk '$1 == "Candidate:" { print $2; exit }')
  [ -n "$c" ] && [ "$c" != "(none)" ]
}

# apt_some PKG|ALT … — install whichever of these the archive has. An argument "a|b" means
# "a, or else b" (package names differ between Debian releases). Names with no candidate at all
# are skipped and listed in APT_MISSING (one entry per argument, as written).
APT_MISSING=()
apt_some() {
  local arg p pick have=()
  APT_MISSING=()
  for arg in "$@"; do
    pick=""
    for p in ${arg//|/ }; do
      if apt_candidate "$p"; then pick=$p; break; fi
    done
    if [ -n "$pick" ]; then have+=("$pick"); else APT_MISSING+=("$arg"); fi
  done
  [ "${#APT_MISSING[@]}" -eq 0 ] || sdr_warn "not in this system's package archive (skipped): ${APT_MISSING[*]}"
  [ "${#have[@]}" -gt 0 ] && DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends "${have[@]}"
}

# apt_missing_any NAME… — true if any of these (as written in an apt_some argument) was missing.
apt_missing_any() {
  local m n
  for n in "$@"; do
    for m in ${APT_MISSING[@]+"${APT_MISSING[@]}"}; do
      case "|$m|" in *"|$n|"*) return 0 ;; esac
    done
  done
  return 1
}

sdr_dirs() {
  step "Radio: folders"
  install -d -o prepper -g prepper -m 750 "$SDR_STATE" "$SDR_STATE/images"
  chown -R prepper:prepper "$SDR_STATE"       # install.sh re-roots /var/lib/prepper; the scheduler writes here
}

sdr_readsb() {
  air_on || return 0
  step "Radio: aircraft decoder (readsb)"
  if [ -z "$READSB_COMMIT" ]; then
    sdr_warn "readsb not built: READSB_COMMIT (release $READSB_TAG) is not set in os/install.d/sdr.sh (see the top of the file)."
    return 1
  fi
  if sdr_stamp readsb "$READSB_COMMIT" && [ -x /usr/local/bin/readsb ]; then return 0; fi
  apt_some git build-essential pkg-config librtlsdr-dev libusb-1.0-0-dev libncurses-dev zlib1g-dev libzstd-dev \
    || { sdr_warn "readsb not built: its build packages did not install"; return 1; }
  git_at_commit "$READSB_REPO" "$READSB_COMMIT" "$SRC/readsb" || return 1
  make -C "$SRC/readsb" -j3 RTLSDR=yes >/dev/null || { sdr_warn "readsb build failed"; return 1; }
  install -m 755 "$SRC/readsb/readsb" /usr/local/bin/readsb && sdr_setstamp readsb "$READSB_COMMIT"
}

sdr_satdump() {
  sat_on || return 0
  step "Radio: weather-satellite decoder (SatDump — this takes a while)"
  if [ -z "$SATDUMP_COMMIT" ]; then
    sdr_warn "SatDump not built: SATDUMP_COMMIT (release $SATDUMP_TAG) is not set in os/install.d/sdr.sh (see the top of the file)."
    return 1
  fi
  if sdr_stamp satdump "$SATDUMP_COMMIT" && [ -x /usr/local/bin/satdump ]; then return 0; fi
  # libvolk-dev on Debian 13 (trixie), libvolk2-dev on Debian 12 (bookworm).
  if ! apt_some git cmake g++ pkg-config libfftw3-dev libpng-dev libtiff-dev libjemalloc-dev libcurl4-openssl-dev \
    "libvolk-dev|libvolk2-dev" libnng-dev librtlsdr-dev libzstd-dev libhdf5-dev libomp-dev ocl-icd-opencl-dev \
    libsqlite3-dev libarmadillo-dev; then
    sdr_warn "SatDump not built: its build packages did not install (weather-satellite pictures stay off; aircraft still work)."
    return 1
  fi
  # Without these SatDump can't be built at all: say so now rather than after a 45-minute build.
  if apt_missing_any git cmake g++ libfftw3-dev libpng-dev libcurl4-openssl-dev libvolk-dev libvolk2-dev; then
    sdr_warn "SatDump not built: required packages are missing from this system's archive (${APT_MISSING[*]})."
    sdr_warn "Weather-satellite pictures stay off; everything else works. Re-run this script after an OS upgrade."
    return 1
  fi
  git_at_commit "$SATDUMP_REPO" "$SATDUMP_COMMIT" "$SRC/satdump" || return 1
  cmake -S "$SRC/satdump" -B "$SRC/satdump/build" -DCMAKE_BUILD_TYPE=Release -DBUILD_GUI=OFF \
    -DCMAKE_INSTALL_PREFIX=/usr/local >/dev/null || { sdr_warn "SatDump configure failed (weather-satellite pictures stay off; re-run to retry)"; return 1; }
  # 3 jobs: a 4 GB Pi runs out of memory with more
  cmake --build "$SRC/satdump/build" -j3 >/dev/null || { sdr_warn "SatDump build failed (weather-satellite pictures stay off; re-run to retry)"; return 1; }
  cmake --install "$SRC/satdump/build" >/dev/null && ldconfig && sdr_setstamp satdump "$SATDUMP_COMMIT"
}

sdr_tle() {
  sat_on || return 0
  step "Radio: initial orbit data for the weather satellites"
  # A builder can drop a TLE file here (e.g. when building offline); it is imported first.
  if [ -f /etc/prepper/tle-initial.txt ]; then
    runuser -u prepper -- python3 "$OPT/software/sdr/tlefile.py" import /etc/prepper/tle-initial.txt ||
      sdr_warn "/etc/prepper/tle-initial.txt had no usable orbit data"
  fi
  # The install has internet: fetch a fresh set now (the timer keeps it fresh whenever online).
  runuser -u prepper -- python3 "$OPT/software/sdr/tlefile.py" fetch || true
  runuser -u prepper -- python3 "$OPT/software/sdr/tlefile.py" show || true
}

sdr_enable() {
  step "Radio: services"
  if air_on || sat_on; then
    systemctl enable prepper-sdr.service >/dev/null && systemctl restart prepper-sdr.service 2>/dev/null || true
  else
    systemctl disable --now prepper-sdr.service >/dev/null 2>&1 || true
  fi
  if sat_on; then
    systemctl enable prepper-tle.timer >/dev/null && systemctl start prepper-tle.timer 2>/dev/null || true
  else
    systemctl disable --now prepper-tle.timer >/dev/null 2>&1 || true
  fi
}

sdr_failed=0
for part in sdr_dirs sdr_readsb sdr_satdump sdr_tle sdr_enable; do
  "$part" || { sdr_warn "$part did not complete"; sdr_failed=1; }
done
[ "$sdr_failed" = 0 ]
