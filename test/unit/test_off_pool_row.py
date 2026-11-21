"""F43: a registry row on a slot outside --slots must not take down gc_tick
or 500 the operator's release attempt.

Shrink or rename --slots and an old ACTIVE row can point at a slot name the
pool no longer holds. Pre-fix, three consequences compounded:

1. gc_tick's idle pass had no pool-ownership guard (the disk-pressure and
   slot-pressure passes both filter `row.slot in owned`): the orphan row,
   idle past the cutoff, raised KeyError from Lifecycle._release's bare
   `pool.get(row.slot)` out of gc_tick on EVERY tick. The exception is
   caught by _gc_loop's bare except (logged, swallowed), so the disk-
   pressure section BELOW the idle pass was never reached again — the
   volume lost its automatic protection permanently.

2. The orphan row was unreleaseable through the API: POST /release and the
   panel release action call the same pool.get and 500'd via the catch-all
   handler. The operator's only remedy was hand-editing the registry.

3. Nothing surfaced the condition: doctor walked pool -> rows
   (check_quarantined_slots), never rows -> pool.

Post-fix: the idle pass carries the same `owned` guard as its siblings;
release() refuses an off-pool row with SlotOutsidePool (mapped to 409, not
the 500 KeyError); doctor reports the stuck row (slots.off_pool_rows,
WARN — the operator restores the slot name in --slots or edits the
registry).

Root-free: real registry (sqlite in tmp_path), real SlotPool keyset, real
Lifecycle, real routes; release_session's process/fs work is faked behind
the same await shape the real one has.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import httpx
import pytest

import app.lifecycle as lcmod
import app.iso.slots as slotsmod
from app.api.app import build_app
from app.doctor import PASS, WARN, Report, check_off_pool_rows
from app.iso.layout import state_paths
from app.iso.slots import Slot, SlotOutsidePool, SlotPool
from app.lifecycle import (
    FREE,
    LIVE,
    Lifecycle,
    LifecycleConfig,
    PiInstall,
)
from app.registry import Registry

OLD = 30 * 86400  # gc_idle_days defaults to 14: 30 days back is always idle


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


def _make_lc(tmp_path, monkeypatch, pool_names) -> Lifecycle:
    """Real Lifecycle + real registry + real pool (the pool's keyset is
    EXACTLY the --slots list — the shrink/rename scenario)."""
    reg = Registry(str(tmp_path / "registry.db"))
    cfg = LifecycleConfig(
        state=state_paths(str(tmp_path)),
        pool=_real_pool(monkeypatch, pool_names),
        registry=reg,
        pi=PiInstall(node_bin="/usr/bin/node", cli_js="/x/cli.js", ext_path=None),
        api_key="k",
    )
    return Lifecycle(cfg)


def _backdate(reg: Registry, session_id: str, seconds: float) -> None:
    """last_activity only (updated_at stays fresh, so the orphan sweep's
    `updated_at > cutoff` skip holds and no fs work happens)."""
    reg.db.execute(
        "UPDATE sessions SET last_activity=? WHERE id=?",
        (time.time() - seconds, session_id),
    )
    reg.db.commit()


def _install_fake_release(monkeypatch, holder: SimpleNamespace) -> None:
    """release_session with the real step 1 and step 7 (pool state
    machine + hooks); no process/fs work."""

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


def _holder() -> SimpleNamespace:
    return SimpleNamespace(calls=[])


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _app(lc: Lifecycle):
    return build_app(
        SimpleNamespace(registry=lc.cfg.registry, lc=lc, boot_report=0),
        SimpleNamespace(browser_origin=None, heartbeat_secs=15),
    )


def _add_orphan_row(lc: Lifecycle, sid: str = "s9", slot: str = "slot9") -> None:
    """The trigger: --slots shrank/renamed, leaving an old LIVE row whose
    slot name the pool no longer holds."""
    lc.cfg.registry.upsert_session(sid, slot=slot, state=LIVE)
    _backdate(lc.cfg.registry, sid, OLD)


# ---------------------------------------------------------------------- #
# the mechanism, pinned in-suite                                          #
# ---------------------------------------------------------------------- #


def test_pool_get_off_pool_name_is_the_keyerror(tmp_path, monkeypatch):
    """The finding's mechanism: a bare dict lookup on the --slots keyset.
    This is what pre-fix escaped every release path."""
    pool = _real_pool(monkeypatch, ("slot1", "slot2"))
    with pytest.raises(KeyError):
        pool.get("slot9")


# ---------------------------------------------------------------------- #
# gc_tick: the idle pass's ownership guard                                #
# ---------------------------------------------------------------------- #


def test_gc_tick_off_pool_row_does_not_abort_disk_pressure(tmp_path, monkeypatch):
    """THE regression: an idle orphan row on a slot outside the pool must
    not raise out of gc_tick and skip the disk-pressure section. Pre-fix
    the KeyError killed the tick every interval; the owned idle-free row
    below it was NEVER released, permanently — the volume lost its
    automatic protection after the first failure. Post-fix the idle pass
    skips the orphan (same guard as its two siblings) and the disk-
    pressure pass releases the owned row."""
    lc = _make_lc(tmp_path, monkeypatch, ("slot1", "slot2"))
    _add_orphan_row(lc)  # LIVE, slot9, 30 days idle: the pre-fix killer
    lc.cfg.registry.upsert_session("s1", slot="slot1", state=LIVE)
    lc.cfg.pool.allocate("s1")
    # s1 is NOT an idle candidate (fresh last_activity): only the disk-
    # pressure pass may release it — the pass pre-fix never reached
    monkeypatch.setattr(lc, "_disk_near_full", lambda: True)
    holder = _holder()
    _install_fake_release(monkeypatch, holder)

    released = _run(lc.gc_tick())

    assert released == ["s1"]  # pre-fix: KeyError('slot9'), nothing released
    assert holder.calls == [("s1", "disk pressure")]  # the branch WAS reached
    assert lc.cfg.registry.get("s9").state == LIVE  # orphan survives, unharmed
    assert lc.cfg.registry.get("s1").state == FREE


def test_gc_tick_idle_gc_still_releases_owned_idle_rows(tmp_path, monkeypatch):
    """The guard must not blunt the normal path: an owned idle row is
    released by the idle pass as before (and nothing is released off-pool
    or from the disk pass while the disk has headroom)."""
    lc = _make_lc(tmp_path, monkeypatch, ("slot1",))
    lc.cfg.registry.upsert_session("s1", slot="slot1", state=LIVE)
    lc.cfg.pool.allocate("s1")
    _backdate(lc.cfg.registry, "s1", OLD)
    monkeypatch.setattr(lc, "_disk_near_full", lambda: False)
    holder = _holder()
    _install_fake_release(monkeypatch, holder)

    released = _run(lc.gc_tick())

    assert released == ["s1"]
    assert holder.calls == [("s1", "idle GC")]
    assert lc.cfg.registry.get("s1").state == FREE


# ---------------------------------------------------------------------- #
# release(): typed refusal, not a KeyError out of the funnel             #
# ---------------------------------------------------------------------- #


def test_release_off_pool_row_refused_typed(tmp_path, monkeypatch):
    """Every release path funnels through _release: the off-pool row is
    refused with SlotOutsidePool (a typed RuntimeError the API maps to
    4xx), never the bare KeyError that pre-fix 500'd both routes and
    aborted gc_tick."""
    lc = _make_lc(tmp_path, monkeypatch, ("slot1",))
    _add_orphan_row(lc)
    holder = _holder()
    _install_fake_release(monkeypatch, holder)

    with pytest.raises(SlotOutsidePool, match="not in this app's pool"):
        _run(lc.release("s9", reason="user close"))

    assert holder.calls == []  # the cleanup path never started
    assert lc.cfg.registry.get("s9").state == LIVE  # row untouched, still fixable
    assert "s9" not in lc._release_tasks  # no in-flight entry left behind


# ---------------------------------------------------------------------- #
# the routes: 409 with the remedy, not the 500 KeyError                  #
# ---------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "url, body",
    [
        ("/api/v1/sessions/s9/release", {"reason": "user close"}),
        ("/panel/sessions/s9/action", {"action": "release"}),
    ],
    ids=["api-release", "panel-release"],
)
def test_release_routes_off_pool_row_409_not_500(
    tmp_path, monkeypatch, url, body
):
    """The operator's own close attempt pre-fix: 500 {"error": "KeyError:
    'slot9'"} from the catch-all handler — a server fault for what is a
    config-vs-registry conflict. Post-fix: 409 with the remedy, on both
    routes."""
    lc = _make_lc(tmp_path, monkeypatch, ("slot1",))
    _add_orphan_row(lc)
    holder = _holder()
    _install_fake_release(monkeypatch, holder)
    app = _app(lc)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post(url, json=body)

    resp = _run(scenario())

    assert resp.status_code == 409, resp.text  # pre-fix: 500 KeyError
    detail = resp.json()["detail"]
    assert "slot9" in detail and "--slots" in detail  # the remedy, not a raw type
    assert holder.calls == []  # nothing was half-released


# ---------------------------------------------------------------------- #
# doctor: the rows -> pool view (check_quarantined_slots is pool -> rows)#
# ---------------------------------------------------------------------- #


def test_doctor_flags_rows_on_slots_outside_the_pool(tmp_path, monkeypatch):
    """The condition that pre-fix had no visible surface: doctor walks
    pool -> rows (quarantined slots), never rows -> pool. Post-fix an
    ACTIVE row on a slot outside --slots is a WARN with the remedy."""
    reg = Registry(str(tmp_path / "registry.db"))
    reg.upsert_session("s9", slot="slot9", state=LIVE)      # off-pool, active
    reg.upsert_session("s1", slot="slot1", state=LIVE)      # on-pool: clean
    args = SimpleNamespace(slots=["slot1", "slot2"])
    rep = Report()
    check_off_pool_rows(rep, args, reg)

    results = {r.name: r for r in rep.results}
    assert results["slots.off_pool_rows"].status == WARN
    assert "slot9" in results["slots.off_pool_rows"].detail
    assert "--slots" in results["slots.off_pool_rows"].detail


def test_doctor_passes_when_all_rows_on_pool_slots(tmp_path):
    """The steady state: every active row's slot is in --slots -> PASS."""
    reg = Registry(str(tmp_path / "registry.db"))
    reg.upsert_session("s1", slot="slot1", state=LIVE)
    reg.upsert_session("s2", slot="slot2", state=FREE)  # terminal: never flagged
    args = SimpleNamespace(slots=["slot1", "slot2"])
    rep = Report()
    check_off_pool_rows(rep, args, reg)

    (result,) = rep.results
    assert result.name == "slots.off_pool_rows"
    assert result.status == PASS
