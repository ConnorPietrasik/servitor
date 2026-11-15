"""Versioned config swap (PLAN §5): stage + rename; a live lower is never
mutated.

The synced agent config lives in versioned dirs ``config/v<N>/`` under the
state volume, with an ``active`` pointer (a symlink, flipped atomically).
A live session's lower is whatever version it booted from (invariant 10:
config reaches a session only at spawn — a live session keeps the config
it started with). A swap therefore:

1. stages a fresh copy in ``config/.staging-v<N+1>`` (same filesystem),
2. renames it to ``config/v<N+1>`` (atomic),
3. flips the ``active`` symlink via a temp symlink + rename (atomic).

Previous versions are untouched — they are the lowers of live sessions
and the rollback source.
"""
from __future__ import annotations

import os
import re
import shutil


class ConfigSwapError(RuntimeError):
    pass

_VERSION_RE = re.compile(r"^v(\d+)$")


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
    failure after the rename (which can only be the pointer flip itself)
    leaves the new version present but not active — detectable by doctor
    (config version on disk vs active pointer).
    """
    if not os.path.isdir(source_dir):
        raise ConfigSwapError(f"source config dir missing: {source_dir}")
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
