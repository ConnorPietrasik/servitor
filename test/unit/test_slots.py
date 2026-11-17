"""Unit: the slot pool — allocation, pinning, quarantine.

The invariants the rest of the system stands on:
- one live session = one slot (running OR awaiting release);
- a stable session pins its slot until closed; a RESPAWN reuses the
  slot it already pins (idempotent per session — the uid never re-rolls);
- a slot is freed ONLY after the sweep verified clean;
- a failed sweep quarantines: the slot is visible, retryable, and NEVER
  allocated again until the retry re-sweep verifies clean.
"""
from __future__ import annotations

import pytest

from app.iso.slots import NoFreeSlot, SlotPool, SlotQuarantined

NAMES = ("slot1", "slot2")


def test_allocate_takes_a_free_slot():
    pool = SlotPool(NAMES)
    s = pool.allocate("sess-a")
    assert s.name == NAMES[0]
    assert s.state == "allocated"
    assert pool.by_session("sess-a") is s


def test_respawn_reuses_the_pinned_slot():
    """Idempotent per session: a respawn of the same session must not
    re-roll the uid — it gets back the slot it already pins."""
    pool = SlotPool(NAMES)
    first = pool.allocate("sess-a")
    again = pool.allocate("sess-a")  # the respawn path
    assert again.name == first.name
    # a different session still gets a different slot
    other = pool.allocate("sess-b")
    assert other.name == NAMES[1]
    # and the pinned slot is never handed to a third session
    with pytest.raises(NoFreeSlot):
        pool.allocate("sess-c")


def test_stable_flag_sticks_through_reuse():
    pool = SlotPool(NAMES)
    pool.allocate("sess-a")
    s = pool.allocate("sess-a", stable=True)  # promoted on reuse
    assert s.stable is True
    pool.allocate("sess-a")  # reuse without the flag keeps it
    assert pool.by_session("sess-a").stable is True


def test_free_requires_cleaning_state():
    pool = SlotPool(NAMES)
    pool.allocate("sess-a")
    with pytest.raises(SlotQuarantined):
        pool.verify_free("sess-a")  # not cleaning yet
    pool.mark_cleaning("sess-a")
    pool.verify_free("sess-a")
    assert pool.by_session("sess-a") is None
    assert pool.get("slot1").state == "free"


def test_quarantine_blocks_allocation_until_retry_verifies():
    pool = SlotPool(("slot1",))  # single slot: quarantining it blocks ALL allocation
    pool.allocate("sess-a")
    pool.mark_cleaning("sess-a")
    # sweep failed: quarantine (records + raises)
    with pytest.raises(SlotQuarantined):
        pool.quarantine("sess-a", "sweep not verified clean")
    assert pool.get("slot1").state == "quarantined"
    # a quarantined slot is NOT allocatable
    with pytest.raises(NoFreeSlot):
        pool.allocate("sess-c")
    # the human clears the blocker; the retry re-enters the SAME session's
    # release: quarantined -> cleaning -> free
    pool.mark_cleaning("sess-a")
    assert pool.get("slot1").state == "cleaning"
    pool.verify_free("sess-a")
    assert pool.get("slot1").state == "free"


def test_no_free_slot_message_names_the_policy():
    pool = SlotPool(NAMES)
    pool.allocate("sess-a")
    pool.allocate("sess-b")
    with pytest.raises(NoFreeSlot) as ei:
        pool.allocate("sess-c")
    assert "oldest non-stable" in str(ei.value)
