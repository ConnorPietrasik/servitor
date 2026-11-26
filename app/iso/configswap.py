"""Versioned config swap: stage + rename; a live lower is never
mutated.

The synced agent config lives in versioned dirs under ``config/`` with an
``active`` pointer (a symlink, flipped atomically). A live session's
lower is whatever version it booted from (config reaches a session only
at spawn), so a version is useful exactly as long as some session runs
on it — and as the rollback target (the sync records the previous active
in ``.previous-active``; the update's automatic rollback flips the
pointer back to it). A swap: stage a fresh copy in ``config/.staging-...``
(same filesystem), rename it into place (atomic), flip the ``active``
symlink via a temp symlink + rename (atomic). Unreferenced versions are
reclaimed by :func:`prune` (release step + GC tick) — everything a prune
must keep is what a session or a rollback can still need.

Two naming schemes coexist: the boot path stages ``v<N>`` (this module),
the host sync's content-addressed dirs are ``c<sha12>`` (created by
deploy/sync.sh's guest script, which does its own stage + flip and
cannot take this process's lock — the ``.swapping-<version>`` marker it
writes around the flip is the fence: a prune skips its whole pass while
a fresh marker exists, and a crash-stale marker expires after
``SWAP_MARKER_TTL_SEC``).
"""
from __future__ import annotations

import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, field


class ConfigSwapError(RuntimeError):
    pass

_VERSION_RE = re.compile(r"^v(\d+)$")

#: A version dir's NAME: the boot path's ``v<N>`` or the host sync's
#: content-addressed ``c<sha12>``. Anything else under config/ is not a
#: version (the active pointer, .previous-active, staging debris) and a
#: prune leaves it alone.
_VERSION_DIR_RE = re.compile(r"^(?:v\d+|c[0-9a-f]{12})$")

#: deploy/sync.sh's guest script writes ``.swapping-<version>`` before it
#: makes the new version dir and removes it after the pointer flip; while
#: a FRESH marker exists, a swap is in flight and a prune must not delete
#: a version that is about to become active. A marker outlives a crash: a
#: re-sync re-runs the swap block and removes it; the NO-OP branch removes
#: it once past this TTL (a FRESH one may belong to a concurrent live run,
#: so it is never removed early). The TTL is also the backstop when no
#: sync runs at all: no swap takes an hour, so an older marker is stale.
SWAP_MARKER_PREFIX = ".swapping-"
SWAP_MARKER_TTL_SEC = 3600.0

# Serializes the app-side swap (:func:`stage_and_swap`) against
# :func:`prune`: a prune must never observe the swap's rename-then-flip
# window (the sync-side flip is fenced by the marker instead — it runs
# in another process's shell).
_CONFIG_LOCK = threading.Lock()


@dataclass
class PruneResult:
    pruned: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)  # (version, reason)


def list_versions(config_root: str) -> list[str]:
    try:
        entries = os.listdir(config_root)
    except FileNotFoundError:
        return []
    out = []
    for e in entries:
        if _VERSION_RE.match(e) and os.path.isdir(os.path.join(config_root, e)):
            out.append(e)
    return sorted(out, key=lambda v: int(_VERSION_RE.match(v).group(1)))  # type: ignore[union-attr]


def next_version(config_root: str) -> str:
    versions = list_versions(config_root)
    n = max((int(_VERSION_RE.match(v).group(1)) for v in versions), default=0) + 1  # type: ignore[union-attr]
    return f"v{n}"


def active_version(config_root: str) -> str | None:
    link = os.path.join(config_root, "active")
    if not os.path.islink(link):
        return None
    return os.readlink(link)


def active_dir(config_root: str) -> str | None:
    v = active_version(config_root)
    return os.path.join(config_root, v) if v else None


def stage_and_swap(config_root: str, source_dir: str) -> str:
    """Swap in a new version from ``source_dir``; return the version name.

    Raises ConfigSwapError on any failure BEFORE the pointer flips; a
    failure after the rename leaves the new version present but not
    active (detectable by doctor; a prune reclaims it once unreferenced
    — the flip's own failure is the unreferenced case).

    Runs under the config lock: a concurrent prune must not observe the
    rename-then-flip window (the new version exists, is not active yet,
    and looks orphaned for a moment)."""
    if not os.path.isdir(source_dir):
        raise ConfigSwapError(f"source config dir missing: {source_dir}")
    with _CONFIG_LOCK:
        return _stage_and_swap_locked(config_root, source_dir)


def _stage_and_swap_locked(config_root: str, source_dir: str) -> str:
    os.makedirs(config_root, exist_ok=True)
    version = next_version(config_root)
    staging = os.path.join(config_root, f".staging-{version}")
    final = os.path.join(config_root, version)
    if os.path.exists(staging):
        shutil.rmtree(staging)
    try:
        shutil.copytree(source_dir, staging)
        os.rename(staging, final)  # atomic: same filesystem
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise ConfigSwapError(f"staging failed: {exc}") from exc
    # atomic pointer flip
    tmp_link = os.path.join(config_root, f".active-tmp-{os.getpid()}")
    link = os.path.join(config_root, "active")
    try:
        if os.path.islink(tmp_link) or os.path.exists(tmp_link):
            os.unlink(tmp_link)
        os.symlink(version, tmp_link)
        os.replace(tmp_link, link)
    except OSError as exc:
        if os.path.islink(tmp_link):
            os.unlink(tmp_link)
        raise ConfigSwapError(f"pointer flip failed: {exc}") from exc
    return version


def require_active_dir(config_root: str) -> str:
    d = active_dir(config_root)
    if d is None or not os.path.isdir(d):
        raise ConfigSwapError(
            "no active config version (sync has not run yet?)"
        )
    return d


# ---------------------------------------------------------------------- #
# version reclamation: a version is useful only while pinned or rollable
# ---------------------------------------------------------------------- #


def version_dirs(config_root: str) -> list[str]:
    """Every version dir under ``config_root`` (both naming schemes)."""
    try:
        entries = os.listdir(config_root)
    except FileNotFoundError:
        return []
    return sorted(
        e
        for e in entries
        if _VERSION_DIR_RE.match(e) and os.path.isdir(os.path.join(config_root, e))
    )


def previous_active_version(config_root: str) -> str | None:
    """The version the sync recorded as previous-active — the update's
    automatic rollback (deploy/update.sh) flips the pointer back to it,
    so a prune keeps it: deleting it would leave the rollback pointing at
    a missing dir."""
    try:
        with open(os.path.join(config_root, ".previous-active")) as f:
            v = f.read().strip()
    except OSError:
        return None
    return v or None


def _swap_in_flight(config_root: str) -> bool:
    """True while a sync-side swap is in flight (a fresh ``.swapping-*``
    marker; the fence contract is on the constants above)."""
    try:
        entries = os.listdir(config_root)
    except FileNotFoundError:
        return False
    for e in entries:
        if not e.startswith(SWAP_MARKER_PREFIX):
            continue
        try:
            age = time.time() - os.lstat(os.path.join(config_root, e)).st_mtime
        except OSError:
            return True  # vanished mid-check: treat as in flight
        if age < SWAP_MARKER_TTL_SEC:
            return True
    return False


def prune(config_root: str, pinned: set[str]) -> PruneResult:
    """Delete the version dirs no one references; keep the rest.

    Kept: ``active`` (the next spawn's source — its usefulness is not
    session-attached), the ``.previous-active`` rollback target, and every
    name in ``pinned`` (the caller's union of the live sessions' recorded
    versions and the mounted overlay lowerdirs — the mount is the
    session's config truth; it outlives the process and the app).

    The pass runs under the config lock (an app-side swap cannot
    interleave its rename-then-flip) and is skipped entirely while a
    sync-side swap is in flight. An absent config root is a no-op
    (every read below already tolerates it). A per-version rmtree
    failure is recorded, never raised — the version retries on the
    next pass."""
    res = PruneResult()
    with _CONFIG_LOCK:
        if _swap_in_flight(config_root):
            return res
        keep = set(pinned)
        act = active_version(config_root)
        if act:
            keep.add(act)
        prev = previous_active_version(config_root)
        if prev:
            keep.add(prev)
        for v in version_dirs(config_root):
            if v in keep:
                continue
            try:
                shutil.rmtree(os.path.join(config_root, v))
                res.pruned.append(v)
            except OSError as exc:
                res.errors.append((v, str(exc)))
    return res
