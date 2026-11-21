"""F24: the registry GC the registry docstring promised never ran.

The module docstring says a dead `free` row "stays in the table ...
until a registry GC deletes it", and Registry.delete_released exists
for exactly that. But gc_tick only did the orphan-file sweep and the
two release passes — nothing ever called delete_released (it had no
caller at all). Consequences:

1. FREE rows accumulated forever: one dead row per released session,
   monotonically, with no other deletion path (delete() is only for
   failed starts).

2. Every gc_tick rescanned every dead row: both release passes
   iterate list_sessions() (include_released=True by default) and the
   disk-pressure pass filters state == LIVE — so the FREE rows are in
   the scan set on every tick, skipped one comparison at a time. Small
   per row, but it is scanning work for rows that no consumer reads:
   the API list excludes released rows and the detail 404s them.

Post-fix: gc_tick's final pass drops FREE rows whose updated_at (the
moment they turned FREE) is past FREE_ROW_RETENTION_SEC, and logs the
count. The retention window keeps a just-released row visible to
operators/doctor for a week; the docstring's promise now holds.

Root-free: real registry (sqlite in tmp_path), real SlotPool keyset,
real Lifecycle, real gc_tick; only _disk_near_full is stubbed (False —
no release pass may fire in these tests).
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

import app.iso.slots as slotsmod
import app.lifecycle as lcmod
from app.iso.layout import state_paths
from app.iso.slots import Slot, SlotPool
from app.lifecycle import FREE, LIVE, Lifecycle, LifecycleConfig, PiInstall
from app.lifecycle import FREE_ROW_RETENTION_SEC
from app.registry import Registry

PAST_RETENTION = FREE_ROW_RETENTION_SEC + 86_400  # a day beyond the cutoff
OLD = 30 * 86_400  # gc_idle_days defaults to 14: 30 days back is always idle


# ---------------------------------------------------------------------- #
# fixtures / helpers                                                      #
# ---------------------------------------------------------------------- #


def _real_pool(monkeypatch, names) -> SlotPool:
    """The REAL SlotPool without passwd discovery: the fixture's slot
    users do not exist on the test host."""
    monkeypatch.setattr(
        slotsmod,
        "discover_slot",
        lambda name: Slot(name=name, uid=1000, gid=1000, home=f"/home/{name}"),
    )
    return SlotPool(tuple(names))


def _make_lc(tmp_path, monkeypatch) -> Lifecycle:
    reg = Registry(str(tmp_path / "registry.db"))
    cfg = LifecycleConfig(
        state=state_paths(str(tmp_path)),
        pool=_real_pool(monkeypatch, ("slot1",)),
        registry=reg,
        pi=PiInstall(node_bin="/usr/bin/node", cli_js="/x/cli.js", ext_path=None),
        api_key="k",
    )
    lc = Lifecycle(cfg)
    # no release pass fires in these tests: disk has headroom
    monkeypatch.setattr(lc, "_disk_near_full", lambda: False)
    return lc


def _set_free(lc: Lifecycle, sid: str, seconds_ago: float) -> None:
    """A row in FREE state with updated_at backdated seconds_ago.

    Raw SQL (as _backdate does in the F43 suite): LIVE→FREE is not a
    single legal transition (it goes through CLEANING), and the point
    under test is the row's state + updated_at, not the release path."""
    lc.cfg.registry.upsert_session(sid, slot="slot1", state=LIVE)
    lc.cfg.registry.db.execute(
        "UPDATE sessions SET state=?, updated_at=? WHERE id=?",
        (FREE, time.time() - seconds_ago, sid),
    )
    lc.cfg.registry.db.commit()


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _install_fake_release(monkeypatch, holder: SimpleNamespace) -> None:
    """release_session with the real step 1 and step 7 (pool state
    machine + hooks); no process/fs work. Same shape as the F43 suite."""

    async def fake_release_session(
        *, session_id, slot, state, pool, hooks,
        driver=None, mount_ops=None, reason="",
    ):
        holder.calls.append((session_id, reason))
        pool.mark_cleaning(session_id)  # step 1: sync, before any await
        hooks.mark_draining(session_id, reason)
        await asyncio.sleep(0)          # the real first await shape
        pool.verify_free(session_id)    # step 7: sweep verified clean
        hooks.mark_released(session_id, quarantined=False, detail="done")
        return SimpleNamespace(released=True, quarantined=False, errors=[])

    monkeypatch.setattr(lcmod, "release_session", fake_release_session)


# ---------------------------------------------------------------------- #
# gc_tick: the registry GC pass                                           #
# ---------------------------------------------------------------------- #


def test_gc_tick_drops_free_rows_past_retention(tmp_path, monkeypatch):
    """THE regression: a dead FREE row older than the retention window
    must be deleted by gc_tick. Pre-fix nothing ever called
    delete_released, so the row survived every tick, forever."""
    lc = _make_lc(tmp_path, monkeypatch)
    _set_free(lc, "s-old", PAST_RETENTION)
    # a live row must be untouched by the GC pass (it is not FREE)
    lc.cfg.registry.upsert_session("s-live", slot="slot1", state=LIVE)

    released = _run(lc.gc_tick())

    assert released == []  # nothing was RELEASED, only GC'd
    assert lc.cfg.registry.get("s-old") is None  # pre-fix: row still FREE
    assert lc.cfg.registry.get("s-live").state == LIVE


def test_gc_tick_keeps_recent_free_rows(tmp_path, monkeypatch):
    """The retention window: a just-released row survives the tick
    (operators/doctor can still see what ran on the slot), and only the
    old ones go. A fresh FREE row is also exactly what the release
    path leaves behind, so it must not be swept in the same tick."""
    lc = _make_lc(tmp_path, monkeypatch)
    _set_free(lc, "s-recent", 3600)  # an hour old: inside retention
    _set_free(lc, "s-old", PAST_RETENTION)

    _run(lc.gc_tick())

    assert lc.cfg.registry.get("s-recent").state == FREE
    assert lc.cfg.registry.get("s-old") is None


def test_gc_tick_never_drops_non_free_rows(tmp_path, monkeypatch):
    """delete_released filters state = FREE only: an idle LIVE row (or
    any non-FREE row) survives the GC pass no matter how old — it is
    the idle/disk passes' job, and only under their own rules."""
    lc = _make_lc(tmp_path, monkeypatch)
    lc.cfg.registry.upsert_session("s-idle", slot="slot1", state=LIVE)
    lc.cfg.pool.allocate("s-idle")
    lc.cfg.registry.db.execute(
        "UPDATE sessions SET last_activity=?, updated_at=? WHERE id=?",
        (time.time() - OLD, time.time() - OLD, "s-idle"),
    )
    lc.cfg.registry.db.commit()
    holder = SimpleNamespace(calls=[])
    _install_fake_release(monkeypatch, holder)

    released = _run(lc.gc_tick())

    # the idle pass releases it (that pass is live in this test); the
    # point is the GC pass deletes only what is already FREE
    assert released == ["s-idle"]
    assert holder.calls == [("s-idle", "idle GC")]
    assert lc.cfg.registry.get("s-idle").state == FREE
    # and a second tick (row now FREE, still within retention) keeps it
    _run(lc.gc_tick())
    assert lc.cfg.registry.get("s-idle").state == FREE


# ---------------------------------------------------------------------- #
# the mechanism, pinned in-suite                                          #
# ---------------------------------------------------------------------- #


def test_delete_released_targets_only_old_free_rows(tmp_path, monkeypatch):
    """Registry.delete_released's SQL is the whole fix's correctness:
    state = FREE AND updated_at < cutoff. A FREE row at/after the cutoff
    stays; a non-FREE row before it stays."""
    lc = _make_lc(tmp_path, monkeypatch)
    _set_free(lc, "s-free-old", PAST_RETENTION)
    _set_free(lc, "s-free-new", 1.0)
    lc.cfg.registry.upsert_session("s-live", slot="slot1", state=LIVE)
    lc.cfg.registry.db.execute(
        "UPDATE sessions SET updated_at=? WHERE id=?",
        (time.time() - PAST_RETENTION, "s-live"),
    )
    lc.cfg.registry.db.commit()

    dropped = lc.cfg.registry.delete_released(time.time() - FREE_ROW_RETENTION_SEC)

    assert dropped == 1
    assert lc.cfg.registry.get("s-free-old") is None
    assert lc.cfg.registry.get("s-free-new").state == FREE
    assert lc.cfg.registry.get("s-live").state == LIVE
