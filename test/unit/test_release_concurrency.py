"""A second release while one is in flight is idempotent, not a 500.

release_session's first step is pool.mark_cleaning (synchronous, before
any await — the slot is 'cleaning' the moment the release suspends at
its first await, driver.shutdown / the uid sweep). A second release of
the same session on the same loop (a panel double-tap; a user close
racing _release_oldest_for_pressure or gc_tick, which can both pick the
same row) must not re-enter release_session: hitting mark_cleaning on
the already-'cleaning' slot raises SlotQuarantined, and nothing on the
path (Lifecycle.release, the /release route, the /panel action route)
catches it — it would reach the app-level 500 handler.

Mechanism, in-suite:
    pool.allocate('s1'); pool.mark_cleaning('s1'); pool.mark_cleaning('s1')
    -> SlotQuarantined: slot slot1 is cleaning — cannot mark cleaning
       (double release?)

The contract sits at the funnel every trigger already shares:
Lifecycle.release dedupes per session — a second caller shares the
in-flight task and gets the same outcome; release_session (the single
cleanup path) runs exactly once. The shared task is shielded: one
caller disconnecting, or a GC tick being cancelled, never cancels the
cleanup the other callers are waiting for.

Root-free: real registry (sqlite in tmp_path), real SlotPool state
machine (mark_cleaning/verify_free), real Lifecycle, real routes;
release_session's process/fs work is faked behind the same await shape
the real one has — mark BEFORE the first await, i.e. the window where
the double release lands.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

import app.lifecycle as lcmod
import app.iso.slots as slotsmod
from app.api.app import EDGE_KEY_HEADER, build_app
from app.iso.layout import state_paths
from app.iso.slots import Slot, SlotPool, SlotQuarantined
from app.lifecycle import (
    FREE,
    LIVE,
    Lifecycle,
    LifecycleConfig,
    PiInstall,
)
from app.registry import Registry

SLOT = "slot1"


# ---------------------------------------------------------------------- #
# fixtures / helpers                                                      #
# ---------------------------------------------------------------------- #


def _real_pool(monkeypatch) -> SlotPool:
    """The REAL SlotPool state machine (mark_cleaning/verify_free/
    by_session) without passwd discovery: the fixture's slot user does
    not exist on the test host."""
    monkeypatch.setattr(
        slotsmod,
        "discover_slot",
        lambda name: Slot(name=name, uid=1000, gid=1000, home=f"/home/{name}"),
    )
    return SlotPool((SLOT,))


def _make_lc(tmp_path, monkeypatch) -> Lifecycle:
    """Real Lifecycle + real registry + real pool; the process-shaped
    bits are faked, per the test_hub_lifecycle pattern."""
    reg = Registry(str(tmp_path / "registry.db"))
    cfg = LifecycleConfig(
        state=state_paths(str(tmp_path)),
        pool=_real_pool(monkeypatch),
        registry=reg,
        pi=PiInstall(node_bin="/usr/bin/node", cli_js="/x/cli.js", ext_path=None),
        api_key="k",
    )
    return Lifecycle(cfg)


def _install_fake_release(monkeypatch, holder: SimpleNamespace) -> None:
    """release_session with the REAL step 1 and step 7 (pool state
    machine + hooks) and the real first-await shape: the slot is
    'cleaning' (holder.reached) before the release suspends
    (holder.gate) — that window is where the double release lands.
    holder.calls records every re-entry."""

    async def fake_release_session(
        *, session_id, slot, state, pool, hooks,
        driver=None, mount_ops=None, reason="",
    ):
        holder.calls.append((session_id, reason))
        pool.mark_cleaning(session_id)  # step 1: sync, before any await
        hooks.mark_draining(session_id, reason)
        holder.reached.set()            # <- the double-release window opens
        await holder.gate.wait()        # the real first await (shutdown/sweep)
        pool.verify_free(session_id)    # step 7: sweep verified clean
        hooks.mark_released(session_id, quarantined=False, detail="done")
        return SimpleNamespace(released=True, quarantined=False, errors=[], config_pruned=[])

    monkeypatch.setattr(lcmod, "release_session", fake_release_session)


def _holder() -> SimpleNamespace:
    # py3.10+ primitives bind no loop at construction; created and
    # awaited on the one loop _run builds for the scenario
    return SimpleNamespace(calls=[], reached=asyncio.Event(), gate=asyncio.Event())


def _run(coro):
    """Run a coroutine on a fresh loop (the unit suite has no session loop)."""
    res = asyncio.new_event_loop()
    try:
        return res.run_until_complete(coro)
    finally:
        res.close()


# ---------------------------------------------------------------------- #
# the mechanism: the guard the dedup must sit in front of                 #
# ---------------------------------------------------------------------- #


def test_second_mark_cleaning_raises_the_double_release_guard(tmp_path, monkeypatch):
    """The mechanism run, pinned in-suite: mark_cleaning on an
    already-'cleaning' slot raises SlotQuarantined. With the funnel
    dedup, release_session never runs a second time, so the guard only
    fires for true state corruption."""
    pool = _real_pool(monkeypatch)
    pool.allocate("s1")
    pool.mark_cleaning("s1")
    with pytest.raises(SlotQuarantined, match="double release"):
        pool.mark_cleaning("s1")


# ---------------------------------------------------------------------- #
# Lifecycle.release: the dedup                                            #
# ---------------------------------------------------------------------- #


def test_second_release_shares_the_in_flight_task(tmp_path, monkeypatch):
    """Two concurrent releases of the same session: release_session runs
    ONCE, both callers get the SAME outcome, the slot lands free, the
    row lands FREE, and no in-flight entry is left behind."""
    lc = _make_lc(tmp_path, monkeypatch)
    lc.cfg.registry.upsert_session("s1", slot=SLOT, state=LIVE)
    lc.cfg.pool.allocate("s1")
    holder = _holder()
    _install_fake_release(monkeypatch, holder)

    async def scenario():
        t1 = asyncio.create_task(lc.release("s1", reason="user close"))
        await holder.reached.wait()   # release 1 suspended at its first await
        t2 = asyncio.create_task(lc.release("s1", reason="panel double-tap"))
        await asyncio.sleep(0)        # let the second call reach the dedup
        holder.gate.set()
        r1, r2 = await asyncio.gather(t1, t2)
        return r1, r2

    r1, r2 = _run(scenario())
    assert r1 is r2  # the shared task's single result
    assert r1.released is True and r1.quarantined is False
    assert holder.calls == [("s1", "user close")]  # the cleanup ran exactly once
    assert lc.cfg.pool.get(SLOT).state == "free"
    assert lc.cfg.registry.get("s1").state == FREE
    assert lc._release_tasks == {}  # the done entry was dropped


def test_cancelled_caller_does_not_cancel_the_cleanup(tmp_path, monkeypatch):
    """Shield semantics: the double-tap's first request disconnecting
    (handler cancelled) must not kill the cleanup the second caller is
    waiting for — and a background-failed release with no waiting
    caller is logged by the done callback, not left for GC to report
    as 'exception was never retrieved'."""
    lc = _make_lc(tmp_path, monkeypatch)
    lc.cfg.registry.upsert_session("s1", slot=SLOT, state=LIVE)
    lc.cfg.pool.allocate("s1")
    holder = _holder()
    _install_fake_release(monkeypatch, holder)

    async def scenario():
        t1 = asyncio.create_task(lc.release("s1", reason="user close"))
        await holder.reached.wait()
        t1.cancel()  # the first client goes away mid-release
        task = lc._release_tasks.get("s1")
        assert task is not None and not task.cancelled()  # still in flight
        holder.gate.set()
        await task  # the cleanup finishes for whoever is still waiting
        return task

    task = _run(scenario())
    assert task.exception() is None
    assert holder.calls == [("s1", "user close")]
    assert lc.cfg.pool.get(SLOT).state == "free"
    assert lc.cfg.registry.get("s1").state == FREE
    assert "s1" not in lc._release_tasks


# ---------------------------------------------------------------------- #
# the routes: /release and /panel action — 200, not 500                   #
# ---------------------------------------------------------------------- #


def test_double_release_across_both_routes_returns_200_not_500(tmp_path, monkeypatch):
    """End-to-end on one loop (httpx ASGITransport: both requests run
    in the test's loop, so the window is real): the first POST /release
    is in flight when the panel double-tap arrives via /panel action.
    Both requests get the release body of the ONE cleanup run."""
    lc = _make_lc(tmp_path, monkeypatch)
    lc.cfg.registry.upsert_session("s1", slot=SLOT, state=LIVE)
    lc.cfg.pool.allocate("s1")
    holder = _holder()
    _install_fake_release(monkeypatch, holder)

    app = build_app(
        SimpleNamespace(registry=lc.cfg.registry, lc=lc, boot_report=0),
        SimpleNamespace(browser_origin=None, heartbeat_secs=15, edge_key="unit-test-edge-key"),
    )

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://t",
            headers={EDGE_KEY_HEADER: "unit-test-edge-key"},
        ) as c:
            r1 = asyncio.create_task(
                c.post("/api/v1/sessions/s1/release", json={"reason": "user close"})
            )
            await holder.reached.wait()  # first release suspended at its await
            r2 = asyncio.create_task(
                c.post(
                    "/panel/sessions/s1/action",
                    json={"action": "release"},
                )
            )
            await asyncio.sleep(0)
            holder.gate.set()
            return await r1, await r2

    r1, r2 = _run(scenario())
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text  # not a 500 SlotQuarantined
    assert r1.json() == {"released": True, "quarantined": False, "errors": [], "config_pruned": []}
    assert r2.json() == r1.json()  # idempotent: same outcome, no re-run
    assert holder.calls == [("s1", "user close")]
    assert lc.cfg.pool.get(SLOT).state == "free"
    assert lc.cfg.registry.get("s1").state == FREE
