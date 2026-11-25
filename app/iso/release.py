"""release() — the single cleanup path.

Every release trigger (UI close, GC timer, slot pressure, disk pressure)
goes through this one function. Steps, in order:

1. Mark ``draining`` in the registry BEFORE side effects (a crash
   mid-release must not hand out an unswept slot).
2. Kill the subprocess (the ladder) if alive. Pressure/GC never releases
   a session with a live run (the caller skips and reports).
3. Kill strays by uid (crash-orphans).
4. Unmount (reverse mount order).
5. Sweep by owner: find / -user slotN — home, /tmp, /var/tmp,
   /dev/shm, everything dropped outside scratch/ — and delete. ``keep/`` is
   excluded for completeness: everything in it is the store's root-owned
   blob inode (a keep links the app's copy of the bytes, never the
   session's file), which a sweep walking by slot uid cannot see, and the
   area is root-owned, so a slot could not have put anything else there.
6. Delete inbox/<s>/, outbox/<s>/, staging/<s>/, scratch/<s>/,
   sessions/<s>/ wholesale, by path. A released session's in-flight staged
   uploads have no API left to resume them — waiting for their TTL would
   bill dead bytes for 72 h. Deleting a link is all this is for shared
   bytes: a delivered upload's bytes live in the blob store and survive
   until nothing links them; a kept artifact's bytes live on in keep/.
7. Free the slot ONLY when the sweep verified empty. A slot that fails
   its sweep is quarantined: visible in the panel, retryable, never
   reused.

Release does ALL session cleanup; no other path deletes session state or
frees a slot.
"""
from __future__ import annotations

import logging
import os
import shutil
import stat
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.iso import mounts as M
from app.iso.layout import StatePaths
from app.iso.slots import Slot, SlotPool
from app.iso.sweep import kill_strays_by_uid, sweep_by_owner

log = logging.getLogger("servitor.iso.release")


class ReleaseHooks(Protocol):
    """The registry seam (tests use a fake). The registry is the only source of session truth."""

    def mark_draining(self, session_id: str, reason: str) -> None: ...

    def mark_released(self, session_id: str, *, quarantined: bool, detail: str) -> None: ...


@dataclass
class ReleaseOutcome:
    session_id: str
    slot: str
    released: bool
    quarantined: bool
    killed: list[int] = field(default_factory=list)
    unmounted: list[str] = field(default_factory=list)
    swept: list[str] = field(default_factory=list)
    discarded: tuple[int, int] = (0, 0)  # (files, bytes) deleted with the session
    errors: list[str] = field(default_factory=list)


def area_footprint(paths: tuple[str, ...]) -> tuple[int, int]:
    """What the human is about to lose: ``(names, bytes)`` under the areas
    release deletes.

    Every name is counted (two links to one inode are two files they will not
    find again), while bytes are counted once per inode — a blob shared with
    another session or a keep does not become free space here, and saying
    otherwise would cry wolf.

    Mount points are not descended into: a still-mounted session's merged
    views (its ``workdir``/``agent-dir`` overlays and the binds in them)
    expose the shared image lower, which a release frees for no one — and
    this walks mounted sessions (the disk-pressure order), not just the
    unmounted areas of a release in flight.

    Recorded in the outcome and the feed: "GC released my session" with no
    footprint is how a kept-too-late artifact becomes a mystery instead of
    a number.
    """
    files = total = 0
    seen: set[tuple[int, int]] = set()
    for root in paths:
        for dirpath, dirs, names in os.walk(root):
            dirs[:] = [d for d in dirs if not os.path.ismount(os.path.join(dirpath, d))]
            for name in names:
                try:
                    st = os.lstat(os.path.join(dirpath, name))
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue
                files += 1
                key = (st.st_dev, st.st_ino)
                if key in seen:
                    continue
                seen.add(key)
                total += st.st_size
    return files, total


async def release_session(
    *,
    session_id: str,
    slot: Slot,
    state: StatePaths,
    pool: SlotPool,
    hooks: ReleaseHooks,
    driver: Any | None = None,  # PiSession | None (None = no live process)
    mount_ops: list[M.MountOp] | None = None,
    reason: str = "user close",
) -> ReleaseOutcome:
    """The single cleanup path. See the module docstring for the steps."""
    outcome = ReleaseOutcome(session_id=session_id, slot=slot.name, released=False, quarantined=False)

    # 1: draining BEFORE side effects
    pool.mark_cleaning(session_id)
    hooks.mark_draining(session_id, reason)

    # 2: kill the subprocess (ladder) if alive
    if driver is not None:
        try:
            code = await driver.shutdown()
            log.info("release %s: driver shutdown exit=%s", session_id, code)
        except Exception as exc:  # a dead/odd process must not block release
            outcome.errors.append(f"driver shutdown: {exc}")
            log.exception("release %s: driver shutdown failed", session_id)

    # 3: kill strays by uid (release step 3's primitive; boot reconciliation
    # reuses it). Runs in a thread: it is a blocking proc scan.
    import asyncio

    outcome.killed = await asyncio.to_thread(kill_strays_by_uid, slot.uid)

    # 4: unmount, reverse order
    if mount_ops:
        try:
            outcome.unmounted = await M.aunmount_all(mount_ops)
        except M.MountError as exc:
            outcome.errors.append(f"unmount: {exc}")
            # a stuck mount is not fatal to the sweep of files, but the
            # mountpoint will show up as a leftover: force-unmount once
            for op in reversed(mount_ops):
                if op.target not in outcome.unmounted:
                    try:
                        import subprocess

                        subprocess.run(["umount", "-l", op.target], capture_output=True)
                        outcome.unmounted.append(op.target)
                    except Exception:
                        pass

    # 5: sweep by owner (home contents, /tmp, everything outside the
    # areas below; the home dir itself is kept — the passwd entry). keep/
    # is excluded: its contents are the store's root-owned blob inodes,
    # which a sweep walking by slot uid cannot see anyway.
    excludes = (
        state.inbox_for(session_id),
        state.outbox_for(session_id),
        state.staging_for(session_id),
        state.scratch_for(session_id),
        state.sessions_for(session_id),
        state.keep,
    )
    sweep = await asyncio.to_thread(sweep_by_owner, slot.uid, excludes, keep_dir=slot.home_path)
    outcome.swept = sweep.deleted
    if sweep.errors:
        outcome.errors.extend(f"sweep {p}: {e}" for p, e in sweep.errors)

    # 6: delete the session areas wholesale — after measuring them, so the
    # release says what it discarded (kept bytes live outside these areas and
    # are never counted here)
    areas = (
        state.inbox_for(session_id),
        state.outbox_for(session_id),
        state.staging_for(session_id),
        state.scratch_for(session_id),
        state.sessions_for(session_id),
    )
    outcome.discarded = await asyncio.to_thread(area_footprint, areas)
    for area in areas:
        if os.path.lexists(area):
            try:
                shutil.rmtree(area)
            except OSError as exc:
                outcome.errors.append(f"rmtree {area}: {exc}")

    # 7: free the slot only when the sweep verified clean; else quarantine
    footprint = f"; discarded {outcome.discarded[0]} file(s) {_human(outcome.discarded[1])}"
    if sweep.ok:
        pool.verify_free(session_id)
        outcome.released = True
        hooks.mark_released(
            session_id,
            quarantined=False,
            detail=f"swept {len(sweep.deleted)} paths{footprint}",
        )
        log.info("release %s: slot %s freed (sweep clean)%s", session_id, slot.name, footprint)
    else:
        try:
            pool.quarantine(session_id, "; ".join(outcome.errors) or "sweep not verified clean")
        except Exception:
            pass
        outcome.quarantined = True
        hooks.mark_released(
            session_id,
            quarantined=True,
            detail=("; ".join(outcome.errors) or "sweep not verified clean") + footprint,
        )
        log.error("release %s: slot %s QUARANTINED", session_id, slot.name)
    return outcome


def _human(n: int) -> str:
    for unit, div in (("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if n >= div:
            return f"{n / div:.1f}{unit}"
    return f"{n}B"
