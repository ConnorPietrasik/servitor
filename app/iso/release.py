"""release() — the single cleanup path.

Every release trigger (UI close, GC timer, slot pressure, disk pressure)
goes through this one function. Steps, in order:

1. Mark ``draining`` in the registry BEFORE side effects (a crash
   mid-release must not hand out an unswept slot).
2. Kill the subprocess (the ladder) if alive. Pressure/GC never releases
   a session with a live run (the caller skips and reports).
3. Kill strays by uid (crash-orphans).
4. Unmount (reverse mount order).
5. Sweep by owner: find / -user slotN — home, /tmp, everything dropped
   outside scratch/ — and delete.
6. Delete inbox/<s>/, scratch/<s>/, sessions/<s>/ wholesale, by path.
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
    errors: list[str] = field(default_factory=list)


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
    # areas below; the home dir itself is kept — the passwd entry)
    excludes = (
        state.inbox_for(session_id),
        state.scratch_for(session_id),
        state.sessions_for(session_id),
    )
    sweep = await asyncio.to_thread(sweep_by_owner, slot.uid, excludes, keep_dir=slot.home_path)
    outcome.swept = sweep.deleted
    if sweep.errors:
        outcome.errors.extend(f"sweep {p}: {e}" for p, e in sweep.errors)

    # 6: delete the session areas wholesale
    for area in (
        state.inbox_for(session_id),
        state.scratch_for(session_id),
        state.sessions_for(session_id),
    ):
        if os.path.lexists(area):
            try:
                shutil.rmtree(area)
            except OSError as exc:
                outcome.errors.append(f"rmtree {area}: {exc}")

    # 7: free the slot only when the sweep verified clean; else quarantine
    if sweep.ok:
        pool.verify_free(session_id)
        outcome.released = True
        hooks.mark_released(session_id, quarantined=False, detail=f"swept {len(sweep.deleted)} paths")
        log.info("release %s: slot %s freed (sweep clean)", session_id, slot.name)
    else:
        try:
            pool.quarantine(session_id, "; ".join(outcome.errors) or "sweep not verified clean")
        except Exception:
            pass
        outcome.quarantined = True
        hooks.mark_released(session_id, quarantined=True, detail="; ".join(outcome.errors) or "sweep not verified clean")
        log.error("release %s: slot %s QUARANTINED", session_id, slot.name)
    return outcome
