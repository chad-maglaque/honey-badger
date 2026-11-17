"""
Shared helpers for the OS-layer tests (render, prepperctl, firstboot, builder).

Stub commands: `Stubs(dir)` writes fake `systemctl`, `nmcli`, … into a bin
directory that goes first on PATH. Each stub appends one JSON record
[name, argv, stdin-or-null] to $STUB_LOG and exits with $STUB_RC_<name>
(default 0) after printing $STUB_OUT_<name>. Standard library only.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PY = sys.executable                     # the interpreter running the tests (3.11+: tomllib)
BASH = shutil.which("bash") or "/bin/bash"

STUB_NAMES = (
    "systemctl", "nmcli", "chronyc", "hostnamectl", "timedatectl", "ip", "rfkill", "iw", "nft",
    "sysctl", "systemd-run", "kiwix-manage", "findmnt", "umount", "sync", "git", "rsync",
    "apt-get", "usermod", "visudo",     # (not shred: firstboot must really delete)
)

STUB = r'''#!{py}
import json, os, sys
name = os.path.basename(sys.argv[0])
key = name.replace("-", "_")
data = sys.stdin.read() if name in os.environ.get("STUB_STDIN", "").split(",") else None
with open(os.environ["STUB_LOG"], "a") as fh:
    fh.write(json.dumps([name, sys.argv[1:], data]) + "\n")
out = os.environ.get("STUB_OUT_" + key)
if out:
    sys.stdout.write(out if out.endswith("\n") else out + "\n")
sys.exit(int(os.environ.get("STUB_RC_" + key, "0")))
'''


class Stubs:
    def __init__(self, base: Path, names=STUB_NAMES):
        self.bin = base / "stubbin"
        self.bin.mkdir(parents=True, exist_ok=True)
        self.log = base / "stub.log"
        self.log.touch()
        for n in names:
            p = self.bin / n
            p.write_text(STUB.format(py=PY))
            p.chmod(0o755)
        # `python3` for scripts that call it by name (firstboot.sh) → this interpreter.
        (self.bin / "python3").symlink_to(PY)

    def env(self, **extra) -> dict:
        env = dict(os.environ)
        for k in list(env):
            if k.startswith(("STUB_", "PREPPER_", "SUDO_")):
                del env[k]
        env.update(PATH=f"{self.bin}:{env.get('PATH', '/usr/bin:/bin')}", STUB_LOG=str(self.log),
                   LC_ALL="C", LANG="C")
        env.update({k: str(v) for k, v in extra.items()})
        return env

    def calls(self, name: str | None = None) -> list[tuple[str, list[str], str | None]]:
        out = []
        for line in self.log.read_text().splitlines():
            n, args, data = json.loads(line)
            if name is None or n == name:
                out.append((n, args, data))
        return out

    def argvs(self, name: str) -> list[list[str]]:
        return [a for _, a, _ in self.calls(name)]

    def clear(self) -> None:
        self.log.write_text("")


class TempDirCase(unittest.TestCase):
    """A fresh temp directory per test, removed afterwards."""

    def setUp(self):
        # resolve(): on macOS /var is a symlink to /private/var, and scripts compare resolved paths.
        self.tmp = Path(tempfile.mkdtemp(prefix="prepper-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)


def run(argv: list[str], env: dict | None = None, input: str | None = None, cwd=None,
        timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(argv, env=env, input=input, capture_output=True, text=True, cwd=cwd,
                          timeout=timeout, stdin=None if input is not None else subprocess.DEVNULL)


def toml_value(v) -> str:
    """A TOML literal for str/int/float/bool/list/dict (dict → inline table)."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        # JSON escapes are valid TOML escapes, except \ud83e-style surrogate pairs: keep UTF-8 as is.
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{k} = {toml_value(x)}" for k, x in v.items()) + " }"
    raise TypeError(type(v))


def example_config(**sections) -> str:
    """The shipped example with selected keys replaced: example_config(network={"ssid": "X"})."""
    import re
    t = (REPO / "os" / "prepper.toml.example").read_text()
    for section, kv in sections.items():
        for key, value in kv.items():
            val = toml_value(value)
            pat = re.compile(rf"(?ms)(^\[{re.escape(section)}\]\n.*?^){re.escape(key)}\s*=.*?$")
            t, n = pat.subn(lambda m, k=key, v=val: f"{m.group(1)}{k} = {v}", t, count=1)
            if not n:
                t = re.sub(rf"(?m)^\[{re.escape(section)}\]\n", lambda m, k=key, v=val: f"{m.group(0)}{k} = {v}\n", t,
                           count=1)
    return t
