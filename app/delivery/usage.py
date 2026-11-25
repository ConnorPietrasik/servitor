"""State-volume usage meters — bytes per area and per session.

A meter, not a cap: nothing here limits anything, it is what the human
sees before deciding to delete, keep, or download.

Area/volume numbers are **inode-deduped across the whole report**: a
hardlinked blob reached from two sessions is one copy of bytes on disk,
and a per-area du would bill it twice. The per-session numbers are the
opposite view on purpose — bytes that session *references*, so "this
session holds 4 GB" never reads "0 because someone else shares them" —
and ``shared_bytes`` reports the difference.

``keep`` is metered as an area, never per session (see PER_SESSION_AREAS).
"""
from __future__ import annotations

import logging
import os
import stat
import threading
import time

log = logging.getLogger("servitor.delivery.usage")

#: areas whose children are session ids (bucketed into ``by_session``) —
#: exactly the per-session areas ``release()`` deletes, so a row always says
#: what releasing would free. ``keep`` is deliberately NOT one: its buckets
#: are per-session *prefixes* of bytes that outlive the session, so
#: attributing them to a live session row would be a lie — it is reported
#: as its own area.
PER_SESSION_AREAS = ("inbox", "outbox", "staging", "scratch", "sessions")

#: walked FIRST, before the rest in name order. The store owns the bytes every
#: other area merely references, so shared inodes are billed to it and the
#: other areas honestly read "0 extra bytes" — not to an alphabetical accident
#: about which directory the walk reached first.
BILL_FIRST_AREAS = ("blobs",)


def _du(
    path: str,
    seen: set[tuple[int, int]],
    seen_local: set[tuple[int, int]] | None = None,
) -> tuple[int, int, int, int]:
    """``(billed_bytes, files, dup_bytes, referenced_bytes)`` under ``path``.

    ``seen`` is the report-wide inode set: an inode already billed elsewhere
    counts 0 here and lands in ``dup_bytes`` instead — billing it twice
    would make the area totals lie. ``seen_local`` (given on a per-session
    walk) adds the opposite view in the SAME pass: bytes the session
    *references*, deduped only within it — a shared blob bills to the store
    once and still counts for every session that references it, so "this
    session holds 4 GB" never reads "0 because someone else shares them".

    Links are neither followed nor counted (their target is billed where it
    lives). Neither are MOUNT POINTS descended into: a session's merged
    views (its ``workdir``/``agent-dir`` overlays and the ``inbox``/
    ``outbox`` binds in them) are windows onto bytes billed at their real
    locations — the uppers below, the bind sources in their own areas, the
    shared image lower that no release ever frees. Walking a mount would
    bill the whole toolkit to every session and count each bind's files
    twice.
    """
    total = files = dup = ref = 0
    for dirpath, dirnames, names in os.walk(path):
        dirnames[:] = [
            d for d in dirnames if not os.path.ismount(os.path.join(dirpath, d))
        ]
        for name in names:
            p = os.path.join(dirpath, name)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            files += 1
            key = (st.st_dev, st.st_ino)
            if seen_local is not None and key not in seen_local:
                seen_local.add(key)
                ref += st.st_size
            if key in seen:
                dup += st.st_size
                continue
            seen.add(key)
            total += st.st_size
    return total, files, dup, ref


def area_usage(root: str) -> dict:
    """Full state-volume breakdown: per top-level area, per session, and
    the filesystem's own totals. Blocking — call in a thread."""
    seen: set[tuple[int, int]] = set()
    areas: dict[str, dict] = {}
    by_session: dict[str, dict[str, int]] = {}
    loose = 0
    dup_bytes = 0
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return {"error": "state volume unreadable", "areas": {}}
    names = [n for n in BILL_FIRST_AREAS if n in names] + [
        n for n in names if n not in BILL_FIRST_AREAS
    ]
    for name in names:
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            try:
                loose += os.path.getsize(path)
            except OSError:
                pass
            continue
        total = files = 0
        if name in PER_SESSION_AREAS:
            try:
                kids = sorted(os.listdir(path))
            except OSError:
                kids = []
            for sid in kids:
                spath = os.path.join(path, sid)
                # one walk, both views: the report-wide dedup for the area
                # total AND the session's referenced bytes (its own dedup)
                # — a second walk of the same tree would bill the meter
                # double the metadata cost for one number
                b, f, d, rb = _du(spath, seen, set())
                total += b
                files += f
                dup_bytes += d
                entry = by_session.setdefault(sid, {})
                entry[name] = rb
                entry["files"] = entry.get("files", 0) + f
        else:
            total, files, d, _ref = _du(path, seen)
            dup_bytes += d
        areas[name] = {"bytes": total, "files": files}
    try:
        sv = os.statvfs(root)
        fs = {
            "total": sv.f_blocks * sv.f_frsize,
            "free": sv.f_bavail * sv.f_frsize,
            "used": (sv.f_blocks - sv.f_bfree) * sv.f_frsize,
        }
    except OSError:
        fs = {}
    return {
        "areas": areas,
        "by_session": by_session,
        # what dedup saved: bytes referenced more than once, stored once
        "shared_bytes": dup_bytes,
        "loose_bytes": loose,
        "fs": fs,
        "at": time.time(),
    }


class UsageMeter:
    """TTL-cached :func:`area_usage`.

    Cached because it is walked for the panel/healthz and the sessions list,
    and a full metadata walk of a volume holding multi-GB artifacts is not
    a per-request thing. The TTL is a staleness bound on a METER — never a
    limit on anything.

    The walk runs under a lock (requests arrive from worker threads): a
    caller that arrives while the first walk is running waits for its result
    instead of paying for a second full walk of the same tree.
    """

    def __init__(self, root: str, ttl_sec: float = 30.0) -> None:
        self.root = root
        self.ttl = ttl_sec
        self._at = 0.0
        self._report: dict | None = None
        self._lock = threading.Lock()

    def report(self) -> dict:
        with self._lock:
            now = time.monotonic()
            if self._report is not None and now - self._at < self.ttl:
                return self._report
            self._report = area_usage(self.root)
            self._at = now
            return self._report

    def invalidate(self) -> None:
        """Force the next report to re-walk (after a delete or a release —
        the human just changed the number and expects to see it move)."""
        with self._lock:
            self._report = None
