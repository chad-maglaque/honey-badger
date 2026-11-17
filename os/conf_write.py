#!/usr/bin/env python3
"""
conf_write — replace prepper.toml atomically with the text on stdin.

  conf_write.py PATH < new.toml

Used by prepperctl (add-peer, set-pin). Keeps the file's owner, group and
mode (0640 root:prepper): a plain open(tmp, "w") would create it 0644 and
leave the admin PIN hash and Wi-Fi passphrases world-readable. Refuses an
empty text or one that does not parse, so a bug upstream can never leave the
Badger without a config. Exit status 0 on success, 1 with a message otherwise.
"""
from __future__ import annotations

import os
import sys
import tomllib


def conf_write(path: str, text: str) -> None:
    if not text.strip():
        raise ValueError("refusing to write an empty config")
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"refusing to write a config that does not parse ({e})") from None
    st = os.stat(path)                       # the file must exist: its owner and mode are kept
    tmp = path + ".tmp"
    try:
        os.unlink(tmp)                       # left over from a crash (or planted): never follow it
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chown(tmp, st.st_uid, st.st_gid)
        os.chmod(tmp, st.st_mode & 0o7777)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    dfd = os.open(os.path.dirname(os.path.abspath(path)), os.O_RDONLY)
    try:
        os.fsync(dfd)                        # the rename survives a power cut
    finally:
        os.close(dfd)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: conf_write.py PATH < new.toml", file=sys.stderr)
        return 2
    try:
        conf_write(sys.argv[1], sys.stdin.read())
    except (OSError, ValueError, UnicodeDecodeError) as e:
        print(f"prepperctl: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
