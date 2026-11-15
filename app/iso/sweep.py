"""Sweep by owner (PLAN §5).

``find / -user slotN`` catches what a session dropped *outside*
``scratch/`` (``/tmp``, ``/var/tmp``, ``/``) and makes slot release a
clean handover. With a shared uid, "everything this session touched"
would be unanswerable — with a distinct slot uid it is one command.

The sweep is a REQUIRED step of release: a slot's uid is never handed to
another session before it is verified clean. A slot that fails its sweep
is quarantined — visible in the panel, retryable, never reused.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field

#: pseudo-filesystems the owner walk never descends into
PSEUDO_FS = ("/proc", "/sys", "/dev", "/run", "/boot/efi")


@dataclass
class SweepResult:
    uid: int
    deleted: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)  # (path, reason)
    verified_clean: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors and self.verified_clean


def find_owner_paths(uid: int, exclude_prefixes: tuple[str, ...] = ()) -> list[str]:
    """Every path on / owned by uid, excluding the given prefixes (and the
    pseudo-filesystems). Called twice by the sweep: before (what to delete)
    and after (verification — must be empty)."""
    return _find(uid, exclude_prefixes)


def _find(uid: int, exclude_prefixes: tuple[str, ...]) -> list[str]:
    args = ["find", "/"]
    excludes = tuple(PSEUDO_FS) + tuple(exclude_prefixes)
    if excludes:
        args += ["(", "-path", excludes[0]]
        for p in excludes[1:]:
            args += ["-o", "-path", p]
        args += [")", "-prune", "-o"]
    args += ["-user", str(uid), "-print"]
    out = subprocess.run(args, capture_output=True, text=True)
    # find exits non-zero on permission errors etc.; we run as root, so
    # stderr is diagnostic only — the stdout list is authoritative
    return [line for line in out.stdout.splitlines() if line]


def sweep_by_owner(
    uid: int,
    exclude_prefixes: tuple[str, ...] = (),
    *,
    keep_dir: str | None = None,
) -> SweepResult:
    """Delete everything the uid owns outside the excluded prefixes, then
    verify the walk is empty.

    ``exclude_prefixes`` are the areas release deletes WHOLESALE in its
    next step (scratch/<s>/, inbox/<s>/, sessions/<s>/) — pruned from the
    walk entirely.

    ``keep_dir`` (the slot's HOME): its CONTENTS are swept, but the 0700
    home dir itself is never deleted (the passwd entry's home must exist
    for the next session) and never fails the verification.
    """
    result = SweepResult(uid=uid)
    paths = find_owner_paths(uid, exclude_prefixes)
    # deepest first: a child must die before its parent dir is rmtree'd
    paths.sort(key=lambda p: (p.count("/"), p), reverse=True)
    mounted = _mounted_targets()
    for path in paths:
        if keep_dir is not None and path.rstrip("/") == keep_dir.rstrip("/"):
            continue  # the home dir itself: kept, contents are swept
        if path in mounted:
            result.errors.append((path, "is a mountpoint (unmount first)"))
            continue
        try:
            if os.path.islink(path) or os.path.isfile(path):
                os.unlink(path)
            elif os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.unlink(path)  # fifo/socket/device the session dropped
            result.deleted.append(path)
        except OSError as exc:
            result.errors.append((path, str(exc)))
    # verification pass: the uid must own nothing outside excludes
    # (the kept home dir itself is tolerated)
    leftovers = find_owner_paths(uid, exclude_prefixes)
    if keep_dir is not None:
        leftovers = [p for p in leftovers if p.rstrip("/") != keep_dir.rstrip("/")]
    result.verified_clean = not leftovers
    if leftovers:
        result.errors.append(
            ("(verification)", f"{len(leftovers)} paths still owned by uid {uid}: " + ", ".join(leftovers[:5]))
        )
    return result


def _mounted_targets() -> set[str]:
    out: set[str] = set()
    try:
        with open("/proc/mounts", "r", encoding="ascii") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    out.add(parts[1])
    except OSError:
        pass
    return out


def kill_strays_by_uid(uid: int, *, term_grace: float = 2.0) -> list[int]:
    """Kill every live process of the uid (crash-orphans included).

    This is release step 3's primitive, also used by boot reconciliation
    (PLAN §5: the respawn path re-checks its slot uid for a live process
    and kills it first if present — invariant 7 is enforced at the spawn
    boundary, not assumed). TERM first, KILL after grace; the session's
    own pi child is handled by the driver's ladder in release step 2.
    Returns the pids killed.
    """
    import os as _os
    import signal
    import time

    pids = _pids_of_uid(uid)
    for pid in pids:
        try:
            _os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    if pids:
        deadline = time.monotonic() + term_grace
        while time.monotonic() < deadline and _pids_of_uid(uid):
            time.sleep(0.05)
    survivors = _pids_of_uid(uid)
    for pid in survivors:
        try:
            _os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        time.sleep(0.02)
    return pids + survivors


def _pids_of_uid(uid: int) -> list[int]:
    out: list[int] = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/status", "rb") as f:
                for line in f:
                    if line.startswith(b"Uid:"):
                        if int(line.split()[1]) == uid:
                            out.append(int(entry))
                        break
        except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError):
            continue
    return out
