"""
speechlib — offline speech for the Honey Badger: speech-to-text and text-to-speech.

Standard library only. The heavy lifting is done by three programs that os/install.d/speech.sh
puts on the Badger (nothing here talks to the internet):

  ffmpeg        any phone recording or radio capture → 16 kHz mono 16-bit WAV
  whisper-cli   whisper.cpp, model ggml-base.en-q5_1.bin (English, ~57 MB, quantized)
  piper         offline neural text-to-speech, voice en_US-lessac-low (~60 MB, 16 kHz)

Use from another feature or service (the internal helper):

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "speech"))   # from portal/features/
    import speechlib
    s = speechlib.Settings.from_config(ctx.cfg("voice"))      # same paths as Voice mode; or Settings()
    if speechlib.available(s)["tts"]:
        wav = speechlib.say("Boil water before drinking it.", s)      # bytes, audio/wav
    text = speechlib.transcribe_file("/path/question.m4a", s)        # str ("" if nothing was said)

Every program runs with a timeout in its own process group (the whole group is killed on a timeout),
gets its text on stdin rather than in argv, and works in a private temporary folder. Errors are raised
as SpeechError with a message a family can read.
"""
from __future__ import annotations

import array
import contextlib
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

HOME = Path(os.environ.get("PREPPER_SPEECH_HOME", "/opt/prepper-speech"))
INSTALL_HINT = ("Speech isn't installed on this Badger yet. An administrator can add it "
                "(about 300 MB, needs the internet once) with:  sudo bash /opt/prepper/os/install.d/speech.sh")


class SpeechError(Exception):
    """Something a person can read: why speech did not work this time."""


def _str(v, default: str) -> str:
    return v if isinstance(v, str) and 0 < len(v) <= 400 and "\x00" not in v else default


def _int(v, default: int, lo: int, hi: int) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return default
    return max(lo, min(hi, int(v)))


@dataclass
class Settings:
    ffmpeg: str = ""
    whisper: str = str(HOME / "bin" / "whisper-cli")
    whisper_model: str = str(HOME / "models" / "ggml-base.en-q5_1.bin")
    whisper_threads: int = 4
    piper: str = str(HOME / "piper" / "piper")
    piper_voice: str = str(HOME / "voices" / "en_US-lessac-low.onnx")
    convert_timeout: int = 60
    stt_timeout: int = 120
    tts_timeout: int = 60
    tts_max_chars: int = 1200

    def __post_init__(self):
        if not self.ffmpeg:
            self.ffmpeg = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"

    @classmethod
    def from_config(cls, sec: dict | None, **overrides) -> "Settings":
        """Settings from a feature's prepper.toml section (keys as in the dataclass); bad values → defaults."""
        sec = sec if isinstance(sec, dict) else {}
        d = cls()
        s = cls(
            ffmpeg=_str(sec.get("ffmpeg"), d.ffmpeg),
            whisper=_str(sec.get("whisper"), d.whisper),
            whisper_model=_str(sec.get("whisper_model"), d.whisper_model),
            whisper_threads=_int(sec.get("whisper_threads"), d.whisper_threads, 1, 8),
            piper=_str(sec.get("piper"), d.piper),
            piper_voice=_str(sec.get("piper_voice"), d.piper_voice),
            convert_timeout=_int(sec.get("convert_timeout"), d.convert_timeout, 5, 600),
            stt_timeout=_int(sec.get("stt_timeout"), d.stt_timeout, 5, 3600),
            tts_timeout=_int(sec.get("tts_timeout"), d.tts_timeout, 5, 600),
            tts_max_chars=_int(sec.get("tts_max_chars"), d.tts_max_chars, 50, 5000),
        )
        for k, v in overrides.items():
            setattr(s, k, v)
        return s


def _exe(p: str) -> bool:
    return bool(p) and os.path.isfile(p) and os.access(p, os.X_OK)


def available(s: Settings) -> dict:
    """What works right now: {"stt", "tts", "missing": [...], "hint"}."""
    missing = []
    if not _exe(s.ffmpeg):
        missing.append("ffmpeg")
    if not _exe(s.whisper):
        missing.append("whisper.cpp")
    if not os.path.isfile(s.whisper_model):
        missing.append("speech-recognition model")
    if not _exe(s.piper):
        missing.append("piper")
    if not (os.path.isfile(s.piper_voice) and os.path.isfile(s.piper_voice + ".json")):
        missing.append("piper voice")
    stt = not ({"ffmpeg", "whisper.cpp", "speech-recognition model"} & set(missing))
    tts = not ({"piper", "piper voice"} & set(missing))
    return {"stt": stt, "tts": tts, "missing": missing, "hint": "" if (stt and tts) else INSTALL_HINT}


# ─────────────────────────────── running programs ────────────────────────────

def run(cmd: list[str], timeout: float, stdin: bytes | None = None, cwd: str | None = None,
        out_limit: int = 2_000_000) -> tuple[int, bytes, bytes]:
    """Run one program; kill its whole process group on timeout (→ SpeechError)."""
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL", "HOME", "TMPDIR")
           or k.startswith("FAKE_")}                     # FAKE_*: test doubles read their script from it
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, env=env,
                             start_new_session=True)
    except OSError as e:
        raise SpeechError(f"could not start {Path(cmd[0]).name}: {e.strerror or e}") from None
    try:
        out, err = p.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            os.killpg(p.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            p.communicate(timeout=5)
        raise SpeechError(f"{Path(cmd[0]).name} took too long (over {int(timeout)} s)") from None
    return p.returncode, out[:out_limit], err[-4000:]


# ─────────────────────────────── audio ───────────────────────────────────────

def sniff_audio(head: bytes) -> str | None:
    """The container of an uploaded recording, from its first bytes; None if it isn't audio we take."""
    if len(head) < 12:
        return None
    if head[:4] == b"\x1aE\xdf\xa3":
        return "webm"                        # Chrome / Android MediaRecorder (Opus)
    if head[:4] == b"OggS":
        return "ogg"                         # Firefox MediaRecorder (Opus)
    if head[4:8] == b"ftyp":
        return "mp4"                         # Safari MediaRecorder (AAC), iPhone Voice Memos (.m4a), .3gp
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[:4] == b"fLaC":
        return "flac"
    if head[:5] == b"#!AMR":
        return "amr"                         # older Android recorders
    if head[:4] == b"caff":
        return "caf"                         # Apple Core Audio
    if head[:3] == b"ID3" or (head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
        return "mp3"                         # MP3, or AAC in ADTS
    return None


def to_wav16k(src: str | Path, dst: str | Path, s: Settings, max_seconds: float | None = None) -> None:
    """Any recording → 16 kHz mono 16-bit WAV (what whisper.cpp wants), cut to max_seconds."""
    cmd = [s.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-vn"]
    if max_seconds:
        cmd += ["-t", f"{float(max_seconds):.1f}"]
    cmd += ["-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-f", "wav", str(dst)]
    rc, _, err = run(cmd, s.convert_timeout)
    if rc != 0 or not os.path.isfile(dst):
        print(f"speechlib: ffmpeg failed ({rc}): {err.decode('utf-8', 'replace')[-300:]}", file=sys.stderr)
        raise SpeechError("couldn't read that recording (the file may be damaged or in an unusual format)")


def wav_info(path: str | Path, max_frames: int = 30 * 16000) -> dict:
    """Length and loudness of a 16-bit WAV: {"seconds", "rms", "rate"} (rms over at most max_frames)."""
    try:
        with wave.open(str(path), "rb") as w:
            rate, n, width, ch = w.getframerate(), w.getnframes(), w.getsampwidth(), w.getnchannels()
            if width != 2 or rate <= 0:
                raise SpeechError("unexpected audio format")
            raw = w.readframes(min(n, max_frames))
    except (wave.Error, EOFError, OSError) as e:
        raise SpeechError(f"couldn't read the audio ({e})") from None
    a = array.array("h")
    a.frombytes(raw[: len(raw) - len(raw) % 2])
    if sys.byteorder == "big":
        a.byteswap()
    if ch > 1:
        a = a[::ch]
    step = max(1, len(a) // 200_000)                       # loudness from ≤200k samples is plenty
    sub = a[::step]
    rms = math.sqrt(sum(x * x for x in sub) / len(sub)) if len(sub) else 0.0
    return {"seconds": n / rate, "rms": rms, "rate": rate}


# ─────────────────────────────── speech-to-text ──────────────────────────────

_NOISE = re.compile(r"\[[^\]]{0,40}\]|\([^)]{0,40}\)|♪+|\*[^*]{0,40}\*")


def clean_transcript(text: str) -> str:
    """Drop whisper's non-speech marks ([BLANK_AUDIO], (static), ♪ …) and tidy spaces."""
    text = _NOISE.sub(" ", text or "")
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def transcribe(wav16k: str | Path, s: Settings, threads: int | None = None, prompt: str | None = None,
               timeout: float | None = None) -> str:
    """16 kHz WAV → English text with whisper.cpp. "" when nothing intelligible was said."""
    if not (_exe(s.whisper) and os.path.isfile(s.whisper_model)):
        raise SpeechError("speech recognition (whisper.cpp) isn't installed")
    with tempfile.TemporaryDirectory(prefix="stt-") as tmp:
        base = os.path.join(tmp, "out")
        cmd = [s.whisper, "-m", s.whisper_model, "-f", str(wav16k), "-t", str(threads or s.whisper_threads),
               "-l", "en", "-nt", "-np", "-otxt", "-of", base]
        if prompt:
            cmd += ["--prompt", prompt[:400]]
        rc, out, err = run(cmd, timeout or s.stt_timeout, cwd=tmp)
        if rc != 0:
            print(f"speechlib: whisper failed ({rc}): {err.decode('utf-8', 'replace')[-300:]}", file=sys.stderr)
            raise SpeechError("speech recognition failed on this recording")
        try:
            text = Path(base + ".txt").read_text(encoding="utf-8", errors="replace")[:200_000]
        except OSError:
            text = out.decode("utf-8", "replace")
    return clean_transcript(text)


def transcribe_file(src: str | Path, s: Settings, max_seconds: float | None = None,
                    threads: int | None = None, prompt: str | None = None) -> str:
    """Any recording → text (converts with ffmpeg first)."""
    with tempfile.TemporaryDirectory(prefix="stt-") as tmp:
        wav = os.path.join(tmp, "in.wav")
        to_wav16k(src, wav, s, max_seconds)
        return transcribe(wav, s, threads=threads, prompt=prompt)


# ─────────────────────────────── text-to-speech ──────────────────────────────

def speech_text(text: str, limit: int = 1200) -> str:
    """Text as it should be read aloud: no [1] citations, markdown, URLs or emoji; at most `limit`
    characters, cut at a sentence end."""
    t = re.sub(r"\[\d{1,2}(?:\s*[,–-]\s*\d{1,2})*\]", "", text or "")
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"[*_#`>|~]+", " ", t)
    t = re.sub(r"[^\x09\x0a\x0d\x20-\x7e -ɏ‘-”–—°]", " ", t)
    t = re.sub(r"\s*\n\s*[-•]\s*", ". ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > limit:
        cut = t[:limit]
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        t = cut[: end + 1] if end > limit // 3 else cut.rsplit(" ", 1)[0] + "…"
    return t


def synthesize(text: str, out_wav: str | Path, s: Settings) -> Path:
    """Text → WAV file with piper. The text goes in on stdin."""
    if not (_exe(s.piper) and os.path.isfile(s.piper_voice)):
        raise SpeechError("text-to-speech (piper) isn't installed")
    t = speech_text(text, s.tts_max_chars)
    if not t:
        raise SpeechError("nothing to say")
    out_wav = Path(out_wav)
    rc, _, err = run([s.piper, "--model", s.piper_voice, "--output_file", str(out_wav)],
                     s.tts_timeout, stdin=(t + "\n").encode("utf-8"))
    if rc != 0 or not out_wav.is_file() or out_wav.stat().st_size < 44:
        print(f"speechlib: piper failed ({rc}): {err.decode('utf-8', 'replace')[-300:]}", file=sys.stderr)
        raise SpeechError("couldn't make the spoken answer")
    with contextlib.suppress(OSError):
        os.chmod(out_wav, 0o640)
    return out_wav


def say(text: str, s: Settings | None = None) -> bytes:
    """Text → WAV bytes (audio/wav). The one-call helper for other features."""
    s = s or Settings()
    with tempfile.TemporaryDirectory(prefix="tts-") as tmp:
        return synthesize(text, os.path.join(tmp, "say.wav"), s).read_bytes()
