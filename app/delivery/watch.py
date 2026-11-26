"""File watch: one metadata walk per watched session per sweep.

The UI never polls the file list. The server sweeps, diffs against its own
snapshot, and publishes the delta on the session's hub: nothing changed ⇒
nothing is published ⇒ zero bytes cross the phone link. The timer sweeps a
session only while a human is subscribed (``consumer_count > 0``); a listing
fetch is the other sweep driver, and it publishes its delta too — every
sweep advances the shared snapshot, so one that discarded its delta would
steal a change from the session's other watchers. A sweep reads no bytes
beyond the once-per-version sha/mime caches in the store.

``stable`` is a meter, never a gate: unchanged ``(size, mtime)`` across two
sweeps, or an mtime older than the grace (so an app restart does not badge
every existing artifact as still being written). An unstable file stays
fully downloadable.

Snapshots are memory-only, deliberately: with no previous snapshot the
first sweep seeds and publishes nothing, so a client always takes the full
listing once (the fetch that also carries its ``etag``) and then lives on
deltas.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from app.delivery.store import SessionFiles

log = logging.getLogger("servitor.delivery.watch")

# What `stable` compares: the bytes are not moving. Metadata arriving (the
# background sha) does NOT make a file unstable — the content did not change.
_STABLE_FIELDS = ("size", "mtime")

# What a delta compares: a rewrite (size/mtime), the sha filling in from
# null (the background hash finishes after the file first appears), and
# the stable flip (a file that stops moving is the ONE change that moves
# none of the content fields — without it the UI's "writing…" chip lifts
# only on an unrelated change or a reload).
_SIG_FIELDS = ("size", "mtime", "sha256", "mime", "stable")


def _sig(entry: dict[str, Any]) -> tuple:
    return tuple(entry.get(f) for f in _SIG_FIELDS)


def _content(entry: dict[str, Any]) -> tuple:
    return tuple(entry.get(f) for f in _STABLE_FIELDS)


def _brief(entry: dict[str, Any]) -> dict[str, Any]:
    """A delta entry: enough to render a row, small enough to send freely."""
    return {
        "path": entry["path"],
        "size": entry["size"],
        "mtime": entry["mtime"],
        "sha256": entry.get("sha256"),
        "mime": entry.get("mime"),
        "stable": entry.get("stable"),
    }


class FileWatch:
    """Per-session outbox snapshots, stability, and change deltas."""

    def __init__(self, stable_grace_sec: float = 60.0) -> None:
        self.grace = stable_grace_sec
        self._snaps: dict[str, dict[str, dict]] = {}
        # ONE lock per session, not one for all: the timer's sweep and a
        # listing fetch share a session's snapshot (walk, diff, and snapshot
        # write are that session's critical section, or a stale write can
        # swallow a delta), but two sessions' sweeps must not queue behind
        # each other. The guard serializes the lock dict itself; an entry
        # dies with its session (release → forget).
        self._locks: dict[str, threading.Lock] = {}
        self._lock_guard = threading.Lock()
        # released sessions, by id: a sweep that got past its LIVE check
        # before a release must not reseed a dead session's snapshot after
        # forget popped it, so forget leaves this record (read under the
        # session's lock) and a sweep that finds it drops its walk. The set
        # only grows: a released id is never re-allocated.
        self._dead: set[str] = set()
        # one shared lock for dead sessions: they need no mutual exclusion,
        # and a per-session lock minted here would be the residue the dead
        # set exists to prevent
        self._dead_lock = threading.Lock()

    def _lock_for(self, sid: str) -> threading.Lock:
        with self._lock_guard:
            if sid in self._dead:
                return self._dead_lock
            lock = self._locks.get(sid)
            if lock is None:
                lock = threading.Lock()
                self._locks[sid] = lock
            return lock

    def sweep(self, sid: str, sfiles: SessionFiles) -> tuple[list[dict], dict | None]:
        """One sweep: ``(outbox entries with stable, delta or None)``.

        Blocking (walks the outbox) — call it in a thread. Serialized per
        SESSION: the snapshot read/diff/write must not interleave, but a
        sweep of another session neither waits for it nor is waited on by
        it. The delta is ``None`` when nothing moved since the previous
        sweep, and on the very first sweep (no baseline to diff).

        A released session (``forget`` already ran) answers ``([], None)``:
        its walk is dropped, and its snapshot is not reseeded.
        """
        with self._lock_for(sid):
            if sid in self._dead:
                # released under us: seeding now would resurrect exactly the
                # snapshot forget removed
                return [], None
            entries = sfiles.list_outbox()
            now = time.time()
            current = {e["path"]: e for e in entries}
            prev = self._snaps.get(sid)
            for e in entries:
                old = prev.get(e["path"]) if prev is not None else None
                e["stable"] = bool(
                    e["mtime"] < now - self.grace
                    or (old is not None and _content(old) == _content(e))
                )
            # _snaps membership is read under _lock_guard (watched()); a
            # mutation must not race that iteration, so it goes under the
            # guard too — session lock -> guard, the order forget() uses
            with self._lock_guard:
                self._snaps[sid] = current
            if prev is None:
                return entries, None
            delta = _diff(prev, current, now)
            return entries, delta

    def forget(self, sid: str) -> None:
        """Drop a released session's snapshot and record it as dead. Under
        the session's own lock, so a sweep already in flight writes its
        snapshot before the pop, not after, and a sweep that takes the lock
        after this sees the dead record and drops its walk instead of
        reseeding (a released id is never reused: the record is permanent by
        construction)."""
        with self._lock_for(sid):
            with self._lock_guard:
                self._locks.pop(sid, None)
                self._dead.add(sid)
                self._snaps.pop(sid, None)

    def watched(self) -> list[str]:
        with self._lock_guard:
            return sorted(self._snaps)


def _diff(prev: dict[str, dict], cur: dict[str, dict], now: float) -> dict | None:
    added = [_brief(v) for k, v in cur.items() if k not in prev]
    removed = [k for k in prev if k not in cur]
    changed = [
        _brief(v) for k, v in cur.items() if k in prev and _sig(prev[k]) != _sig(v)
    ]
    if not (added or removed or changed):
        return None
    return {"added": added, "removed": removed, "changed": changed, "at": now}
