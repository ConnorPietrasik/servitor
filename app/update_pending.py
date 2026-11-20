"""The update-pending flag.

Host-side ``sync.sh`` / ``update.sh`` write this file when a deployed
shape change (config / extension / keys / image) needs a service
restart and/or a doctor run. The app's in-process tick consumes it: a run
active -> defer (the flag survives, sessions get ``update_waiting``, the
tick retries); no run active -> apply the restart (the doctor runs on the
new instance's first idle tick, or inline for a doctor-only flag). A
doctor-only flag is cleared ONLY when the doctor passes: "file gone" is
the fact sync's post-clear edge probe trusts.

Crash safety: the restart bit is cleared BEFORE the restart is issued
(consume_restart), so a crash between clear and restart can never
double-restart; a crash mid-restart leaves a doctor-only flag for the new
instance.
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from pathlib import Path

__all__ = ["UpdatePending", "load", "save", "clear", "consume_restart"]


@dataclass
class UpdatePending:
    reason: str
    restart: bool = False
    doctor: bool = False
    ts: float = 0.0

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


def _lock_file(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


@contextmanager
def _locked(path: Path):
    """fcntl LOCK_EX around the whole read-modify-write."""
    lock = _lock_file(path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield fh
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


def _read(path: Path) -> UpdatePending | None:
    try:
        raw = path.read_text()
        d = json.loads(raw)
        return UpdatePending(
            reason=str(d.get("reason", "")),
            restart=bool(d.get("restart", False)),
            doctor=bool(d.get("doctor", False)),
            ts=float(d.get("ts", 0.0)),
        )
    except (OSError, ValueError):
        return None


def _write(path: Path, flag: UpdatePending | None) -> None:
    if flag is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".update-pending-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(flag.to_json())
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load(path: str | Path) -> UpdatePending | None:
    p = Path(path)
    with _locked(p):
        return _read(p)


def save(path: str | Path, flag: UpdatePending) -> None:
    p = Path(path)
    with _locked(p):
        _write(p, flag)


def clear(path: str | Path) -> None:
    p = Path(path)
    with _locked(p):
        _write(p, None)


def consume_restart(path: str | Path) -> UpdatePending | None:
    """Clear the restart bit (crash-safe: before the restart is issued).

    Returns the flag with restart=False (the doctor bit kept), or None if
    there was nothing to consume."""
    p = Path(path)
    with _locked(p):
        flag = _read(p)
        if flag is None:
            return None
        if not flag.restart:
            return flag
        flag.restart = False
        _write(p, flag)
        return flag
