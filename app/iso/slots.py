"""16 static slot uids.

One per LIVE session (running or awaiting release); a stable session pins
its slot until closed. Uids are discovered from the passwd database (created
at guest image build) — never hardcoded. ``$HOME`` follows the uid, not an
env var. Signals are uid-gated, not cgroup-gated.
"""
from __future__ import annotations

import pwd
from dataclasses import dataclass, field
from typing import Iterator

from app.iso.layout import SLOT_HOME, SLOT_NAMES


class NoFreeSlot(RuntimeError):
    """No free slot: release the oldest non-stable non-running session
    (the caller's policy); if none is releasable the start is refused."""


class SlotQuarantined(RuntimeError):
    """A slot failed its sweep verification. It is quarantined: visible in
    the panel, retryable, NEVER reused until clean."""


class SlotOutsidePool(RuntimeError):
    """A registry row points at a slot name this app's pool no longer
    holds (--slots was shrunk or renamed under it). The slot user's state
    cannot be resolved here, so release is refused with a typed error the
    API maps to 4xx — the bare pool.get KeyError that pre-fix escaped
    every release path (idle GC tick, POST /release, panel action) as a
    500, and permanently aborted gc_tick's disk-pressure section.
    Operator remedy: restore the slot name in --slots, or hand-edit the
    registry (doctor: slots.off_pool_rows)."""

    def __init__(self, session_id: str, slot: str) -> None:
        super().__init__(
            f"session {session_id} sits on slot {slot!r}, which is not in this "
            "app's pool (--slots) — release refused: restore the slot name in "
            "--slots or hand-edit the registry, then retry"
        )


@dataclass
class Slot:
    name: str
    uid: int
    gid: int
    home: str
    state: str = "free"  # free | allocated | cleaning | quarantined
    session_id: str | None = None
    stable: bool = False

    @property
    def home_path(self) -> str:
        return SLOT_HOME.format(slot=self.name)


def discover_slot(name: str) -> Slot:
    try:
        pw = pwd.getpwnam(name)
    except KeyError as exc:
        raise SlotQuarantined(
            f"slot user {name!r} missing from passwd (image build must create it)"
        ) from exc
    return Slot(name=name, uid=pw.pw_uid, gid=pw.pw_gid, home=pw.pw_dir)


class SlotPool:
    """The static pool. State lives in the registry (the only
    source of session truth); this is the in-process view iso code works
    against, rebuilt from the registry at boot."""

    def __init__(self, names: tuple[str, ...] | None = None) -> None:
        self.slots: dict[str, Slot] = {n: discover_slot(n) for n in (names or SLOT_NAMES)}

    def __iter__(self) -> Iterator[Slot]:
        return iter(self.slots.values())

    def get(self, name: str) -> Slot:
        return self.slots[name]

    def by_session(self, session_id: str) -> Slot | None:
        """The slot owning this session — allocated, cleaning, OR
        quarantined (a quarantined slot still owns that session's
        cleanup)."""
        for s in self.slots.values():
            if s.session_id == session_id and s.state in ("allocated", "cleaning", "quarantined"):
                return s
        return None

    def free_slots(self) -> list[Slot]:
        return [s for s in self.slots.values() if s.state == "free"]

    def allocate(self, session_id: str, *, stable: bool = False) -> Slot:
        # idempotent per session: a RESPAWN of a live session reuses the
        # slot it already pins (the respawn path never re-rolls the uid)
        for s in self.slots.values():
            if s.session_id == session_id and s.state == "allocated":
                s.stable = s.stable or stable
                return s
        for s in self.slots.values():
            if s.state == "free":
                s.state = "allocated"
                s.session_id = session_id
                s.stable = stable
                return s
        raise NoFreeSlot(
            "all 16 slots are live sessions (running or awaiting release); "
            "release the oldest non-stable non-running session, or refuse the start"
        )

    def mark_cleaning(self, session_id: str) -> Slot:
        s = self.by_session(session_id)
        if s is None:
            raise KeyError(session_id)
        if s.state not in ("allocated", "quarantined"):
            raise SlotQuarantined(
                f"slot {s.name} is {s.state} — cannot mark cleaning (double release?)"
            )
        # allocated -> cleaning (fresh release) or quarantined -> cleaning
        # (the retry path after a human unblocked the stuck file)
        s.state = "cleaning"
        return s

    def verify_free(self, session_id: str) -> Slot:
        """Free the slot — ONLY called after the sweep verified clean
        (release step 7)."""
        s = self.by_session(session_id)
        if s is None:
            raise KeyError(session_id)
        if s.state != "cleaning":
            raise SlotQuarantined(
                f"slot {s.name} is {s.state}, not cleaning — refusing to free it"
            )
        s.state = "free"
        s.session_id = None
        s.stable = False
        return s

    def quarantine(self, session_id: str, reason: str) -> Slot:
        s = self.by_session(session_id)
        if s is None:
            raise KeyError(session_id)
        s.state = "quarantined"
        s.stable = False
        # session_id is kept: the panel shows which session's junk is stuck
        # where (the sweep's deletion list is the audit trail)
        raise SlotQuarantined(f"slot {s.name} quarantined after release of {session_id}: {reason}")
