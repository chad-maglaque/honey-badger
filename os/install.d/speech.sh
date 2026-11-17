# shellcheck shell=bash
# Honey Badger — speech: ffmpeg + whisper.cpp (speech-to-text) + piper (text-to-speech).
# Used by the NOAA forecast-to-text feature (prepper-forecast) and Voice mode.
#
# Sourced by os/install.sh (with OPT, CONF and the toml/step helpers), or run on its own on the Pi:
#     sudo bash /opt/prepper/os/install.d/speech.sh
# Idempotent; every part is independent, and a part that fails is reported without stopping the rest.
#
# Everything lives in /opt/prepper-speech (outside /opt/prepper, which install.sh rsyncs with --delete):
#     bin/whisper-cli   models/ggml-base.en-q5_1.bin   piper/piper (+ its libs, espeak-ng-data)
#     voices/en_US-lessac-low.onnx(.json)
#
# DOWNLOAD INTEGRITY: every download is checked against a SHA-256 below. A hash that is still empty
# means "not verified yet": the file is NOT downloaded/installed and the script says what to do.
# Fill a hash by downloading the exact pinned URL once on a trusted machine and running sha256sum
# on it, then put it here (or pass it in the environment: sudo WHISPER_MODEL_SHA256=… bash speech.sh).

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

SPEECH=/opt/prepper-speech

# ── pinned versions ──────────────────────────────────────────────────────────
# whisper.cpp: built from a release tag. (Debian 13 "trixie" does not ship whisper.cpp as far as
# I know — it reached Debian, if at all, after trixie's freeze — so it is not taken from apt.)
WHISPER_CPP_REF=v1.7.6                      # https://github.com/ggml-org/whisper.cpp/releases (commit a8d002cfd879315632a579e73f0148d06959de36)
# Model: English-only "base", 5-bit quantized (~57 MB). Revision pinned by the hash below.
WHISPER_MODEL=ggml-base.en-q5_1.bin
# Pinned to a repo commit; the hash is HuggingFace's published LFS SHA-256 for this file (59,721,011 bytes).
WHISPER_MODEL_URL="https://huggingface.co/ggerganov/whisper.cpp/resolve/5359861c739e955e79d9a303bcbc70fb988958b1/$WHISPER_MODEL"
: "${WHISPER_MODEL_SHA256:=4baf70dd0d7c4247ba2b81fafd9c01005ac77c2f9ef064e00dcf195d0e2fdd2f}"

# piper: the last self-contained release of the C++ binary (bundles onnxruntime + espeak-ng data).
PIPER_VERSION=2023.11.14-2                  # https://github.com/rhasspy/piper/releases
PIPER_URL="https://github.com/rhasspy/piper/releases/download/$PIPER_VERSION/piper_linux_aarch64.tar.gz"
# No SHA-256 is published for this asset (2023 release, repo now archived; GitHub shows digest: null).
# Download it once on a trusted machine (expect 26,004,717 bytes), run sha256sum, and fill it in.
: "${PIPER_SHA256:=}"                       # <<< FILL IN
# Voice: US English "lessac", low quality = 16 kHz, small and quick on a Pi. piper-voices tag v1.0.0.
PIPER_VOICE=en_US-lessac-low
# Pinned to the commit of tag v1.0.0 (375a0fe6…). .onnx: HuggingFace's published LFS SHA-256 (63,201,294 bytes).
PIPER_VOICE_URL="https://huggingface.co/rhasspy/piper-voices/resolve/375a0fe641dea077c2a47b4e9a056d6da521eed3/en/en_US/lessac/low/$PIPER_VOICE.onnx"
: "${PIPER_VOICE_SHA256:=f7d01dde371555732c4c314111ac79672b1a5ce2fc19266ab42178fd8df7f375}"
# The small .onnx.json is plain git (not LFS), so only its git blob SHA-1 is published (4,882 bytes);
# it is checked with that unless a SHA-256 is given here.
: "${PIPER_VOICE_JSON_SHA256:=}"
PIPER_VOICE_JSON_GITSHA1=74579f6dc2d27a63130153a32e0c1224f5ccf24e

speech_warn() { echo "!! speech: $*"; }

# fetch_verified URL DEST SHA256 — download to DEST only if it matches; refuses with no hash.
fetch_verified() {
  local url=$1 dest=$2 want=$3 part
  if [ -z "$want" ]; then
    speech_warn "not downloading $(basename "$dest"): its SHA-256 is not set in os/install.d/speech.sh."
    speech_warn "  download $url on a trusted machine, run sha256sum on it, and set the matching *_SHA256 variable."
    return 1
  fi
  if [ -f "$dest" ] && [ "$(sha256sum "$dest" | cut -d' ' -f1)" = "$want" ]; then
    return 0
  fi
  part="$dest.part"
  mkdir -p "$(dirname "$dest")"
  curl -fL --retry 5 --retry-delay 5 -C - -o "$part" "$url" || { speech_warn "download failed: $url"; return 1; }
  if [ "$(sha256sum "$part" | cut -d' ' -f1)" != "$want" ]; then
    speech_warn "$(basename "$dest"): checksum does not match — deleted, not installed."
    rm -f "$part"
    return 1
  fi
  mv -f "$part" "$dest"
}

# fetch_verified_gitblob URL DEST SHA1 — for small files HuggingFace keeps in plain git: checks the
# git blob id (sha1 of "blob <size>\0" + content), the only hash published for them.
fetch_verified_gitblob() {
  local url=$1 dest=$2 want=$3 part
  gitblob() { python3 -c 'import hashlib,sys; d=open(sys.argv[1],"rb").read(); print(hashlib.sha1(b"blob %d\0" % len(d) + d).hexdigest())' "$1"; }
  if [ -f "$dest" ] && [ "$(gitblob "$dest")" = "$want" ]; then return 0; fi
  part="$dest.part"
  mkdir -p "$(dirname "$dest")"
  curl -fL --retry 5 --retry-delay 5 -o "$part" "$url" || { speech_warn "download failed: $url"; return 1; }
  if [ "$(gitblob "$part")" != "$want" ]; then
    speech_warn "$(basename "$dest"): checksum does not match — deleted, not installed."
    rm -f "$part"; return 1
  fi
  mv -f "$part" "$dest"
}

speech_ffmpeg() {
  step "Speech: ffmpeg"
  command -v ffmpeg >/dev/null 2>&1 && return 0
  DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends ffmpeg
}

speech_whisper() {
  step "Speech: whisper.cpp $WHISPER_CPP_REF (builds on the Pi; a few minutes)"
  local stampf="$SPEECH/.stamp.whisper" t
  if [ -x "$SPEECH/bin/whisper-cli" ] && [ "$(cat "$stampf" 2>/dev/null)" = "$WHISPER_CPP_REF" ]; then
    return 0
  fi
  command -v cmake >/dev/null 2>&1 || DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends cmake build-essential git
  t=$(mktemp -d) || return 1
  if git clone -q --depth 1 --branch "$WHISPER_CPP_REF" https://github.com/ggml-org/whisper.cpp "$t/w" &&
     cmake -S "$t/w" -B "$t/w/build" -DCMAKE_BUILD_TYPE=Release -DGGML_NATIVE=ON -DBUILD_SHARED_LIBS=OFF \
       -DWHISPER_BUILD_TESTS=OFF -DWHISPER_BUILD_SERVER=OFF -DWHISPER_SDL2=OFF >/dev/null &&
     cmake --build "$t/w/build" --target whisper-cli -j4; then
    install -d -m 755 "$SPEECH/bin"
    install -m 755 "$t/w/build/bin/whisper-cli" "$SPEECH/bin/whisper-cli"
    echo "$WHISPER_CPP_REF" > "$stampf"
    rm -rf "$t"
  else
    rm -rf "$t"
    speech_warn "whisper.cpp $WHISPER_CPP_REF did not build"
    return 1
  fi
}

speech_model() {
  # SPEECH_DOWNLOADS=off (environment) skips every model/voice download, e.g. for test installs
  if [ "${SPEECH_DOWNLOADS:-on}" = off ]; then speech_warn "downloads switched off (SPEECH_DOWNLOADS=off)"; return 0; fi
  step "Speech: recognition model $WHISPER_MODEL"
  fetch_verified "$WHISPER_MODEL_URL" "$SPEECH/models/$WHISPER_MODEL" "$WHISPER_MODEL_SHA256"
}

speech_piper() {
  # SPEECH_DOWNLOADS=off (environment) skips every model/voice download, e.g. for test installs
  if [ "${SPEECH_DOWNLOADS:-on}" = off ]; then speech_warn "downloads switched off (SPEECH_DOWNLOADS=off)"; return 0; fi
  step "Speech: piper $PIPER_VERSION and voice $PIPER_VOICE"
  local stampf="$SPEECH/.stamp.piper" t ok=0
  if ! { [ -x "$SPEECH/piper/piper" ] && [ "$(cat "$stampf" 2>/dev/null)" = "$PIPER_VERSION" ]; }; then
    t=$(mktemp -d) || return 1
    if fetch_verified "$PIPER_URL" "$t/piper.tgz" "$PIPER_SHA256" && tar -xzf "$t/piper.tgz" -C "$t" && [ -x "$t/piper/piper" ]; then
      rm -rf "$SPEECH/piper"
      mv "$t/piper" "$SPEECH/piper"
      echo "$PIPER_VERSION" > "$stampf"
    else
      ok=1
    fi
    rm -rf "$t"
  fi
  fetch_verified "$PIPER_VOICE_URL" "$SPEECH/voices/$PIPER_VOICE.onnx" "$PIPER_VOICE_SHA256" || ok=1
  if [ -n "$PIPER_VOICE_JSON_SHA256" ]; then
    fetch_verified "$PIPER_VOICE_URL.json" "$SPEECH/voices/$PIPER_VOICE.onnx.json" "$PIPER_VOICE_JSON_SHA256" || ok=1
  else
    fetch_verified_gitblob "$PIPER_VOICE_URL.json" "$SPEECH/voices/$PIPER_VOICE.onnx.json" "$PIPER_VOICE_JSON_GITSHA1" || ok=1
  fi
  return "$ok"
}

speech_units() {
  step "Speech: forecast service"
  # The portal owns /var/lib/prepper/portal; make the forecast folder now so the hardened service
  # (ReadWritePaths= this folder only) can write to it before the portal has ever run.
  install -d -o prepper -g prepper -m 750 /var/lib/prepper/portal/features 2>/dev/null || true
  install -d -o prepper -g prepper -m 750 /var/lib/prepper/portal/features/forecast
  # install.sh copies *.service and *.timer; the path unit ("Update now") is installed here.
  install -m 644 "$OPT/os/systemd/prepper-speech-forecast-now.path" /etc/systemd/system/
  systemctl daemon-reload
  if [ "$(toml features.forecast)" != "False" ] && [ "$(toml radio.sdr_weather)" != "off" ]; then
    systemctl enable --now prepper-forecast.timer prepper-speech-forecast-now.path >/dev/null 2>&1 || true
  else
    systemctl disable --now prepper-forecast.timer prepper-speech-forecast-now.path >/dev/null 2>&1 || true
  fi
}

speech_main() {
  local failed=0
  install -d -m 755 "$SPEECH" "$SPEECH/models" "$SPEECH/voices"
  speech_ffmpeg  || { failed=1; speech_warn "ffmpeg not installed — speech stays off"; }
  speech_whisper || failed=1
  speech_model   || failed=1
  speech_piper   || { failed=1; speech_warn "spoken answers stay off (Voice mode shows text only)"; }
  chmod -R a+rX "$SPEECH"
  speech_units   || failed=1
  if [ "$failed" = 0 ]; then echo "speech: ready"; else speech_warn "incomplete — see the messages above; re-run to retry"; fi
  return "$failed"
}

speech_main
