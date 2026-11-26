"""A release interrupted by an app crash must quarantine its slot.

A release flips the row LIVE/ALLOCATED -> DRAINING (then CLEANING) and
only flips the slot free once the sweep verifies clean. An app crash in
that window leaves a DRAINING/CLEANING row across restarts. The contract:
the row's slot is quarantined at boot (state quarantined, session
re-attached so the panel shows whose junk is stuck where, never
allocatable), the row stays where the crash left it, the retry path
(quarantined -> cleaning -> free) works, and a leftover slot-uid process
is killed as an orphan.

Note: at boot every pool slot has ``session_id=None``, so the session is
re-attached to the slot directly (a ``pool.quarantine(row.id)`` call would
raise KeyError).

Root-free: real Registry (sqlite in tmp_path) + real SlotPool
(getpwnam stood in, test_slots pattern) + real Lifecycle; the
process-shaped bits (_pids_of_uid, kill_strays_by_uid) are faked.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

import app.iso.mounts as M
import app.iso.slots as slots_mod
import app.lifecycle as lcmod
from app.iso.layout import SLOT_NAMES, SessionMountPaths, state_paths
from app.iso.slots import SlotPool
from app.lifecycle import (
    CLEANING,
    DRAINING,
    FREE,
    LIVE,
    Lifecycle,
    LifecycleConfig,
    PiInstall,
)
from app.registry import Registry

SLOTS = ("slot1", "slot2")
OLD = "old-session"
NEW = "new-session"


@pytest.fixture(autouse=True)
def _slot_users(monkeypatch):
    """Stand in for the guest's passwd entries (test_slots pattern)."""
    users = {
        name: SimpleNamespace(pw_uid=9001 + i, pw_gid=9001 + i,
                              pw_dir=f"/home/{name}")
        for i, name in enumerate(SLOT_NAMES)
    }

    def getpwnam(name):
        try:
            return users[name]
        except KeyError:
            raise KeyError(f"getpwnam(): name not found: {name!r}") from None

    monkeypatch.setattr(slots_mod.pwd, "getpwnam", getpwnam)


def _make_lc(tmp_path, monkeypatch, *, pids=None, killed=None):
    """A full Lifecycle (real registry, real pool) with the process scan
    faked: pids(kill) returns the given lists for the scanned uid."""
    reg = Registry(str(tmp_path / "registry.db"))
    pool = SlotPool(SLOTS)
    calls = {"killed": []}

    def fake_pids(uid):
        return list(pids or [])

    def fake_kill(uid):
        calls["killed"].append(uid)
        return list(killed or [])

    monkeypatch.setattr(Lifecycle, "_pids_of_uid", staticmethod(fake_pids))
    monkeypatch.setattr(lcmod, "kill_strays_by_uid", fake_kill)
    cfg = LifecycleConfig(
        state=state_paths(str(tmp_path)),
        pool=pool,
        registry=reg,
        pi=PiInstall(node_bin="/usr/bin/node", cli_js="/x/cli.js"),
        api_key="k",
    )
    return Lifecycle(cfg), calls


def _add_crashed_release(lc: Lifecycle, state: str) -> None:
    """A session whose release started, then the app crashed: the row is
    DRAINING/CLEANING while the sweep never ran."""
    reg = lc.cfg.registry
    reg.upsert_session(OLD, slot="slot1", state=LIVE)
    reg.set_state(OLD, state, cause="release: user close")
    assert reg.get(OLD).state == state


@pytest.mark.parametrize("state", [DRAINING, CLEANING])
def test_crashed_release_quarantines_the_slot(tmp_path, monkeypatch, state):
    lc, _ = _make_lc(tmp_path, monkeypatch)
    _add_crashed_release(lc, state)
    report = lc.boot_reconcile()

    slot = lc.cfg.pool.get("slot1")
    assert slot.state == "quarantined"
    assert slot.session_id == OLD  # the panel shows whose junk is stuck
    assert slot.stable is False
    # the row stays where the crash left it — the human re-runs release
    assert lc.cfg.registry.get(OLD).state == state
    # visible: feed event + boot report
    assert [e for e in lc.events.items() if e["kind"] == "slot_quarantined"]
    assert any(r["slot"] == "slot1" and r["session"] == OLD for r in report)
    # the dirty uid is never handed out (a slot is never handed out
    # before its sweep verifies clean)
    assert slot not in lc.cfg.pool.free_slots()
    fresh = lc.cfg.pool.allocate(NEW)
    assert fresh.name == "slot2" and fresh.session_id == NEW


def test_boot_reconcile_is_idempotent(tmp_path, monkeypatch):
    """A double boot must not double-record the quarantine."""
    lc, _ = _make_lc(tmp_path, monkeypatch)
    _add_crashed_release(lc, DRAINING)
    lc.boot_reconcile()
    lc.boot_reconcile()
    assert len([e for e in lc.events.items()
                if e["kind"] == "slot_quarantined"]) == 1
    assert lc.cfg.pool.get("slot1").state == "quarantined"


def test_crashed_release_with_orphan_kills_it(tmp_path, monkeypatch):
    """A crash between the registry flip and the release's kill step
    leaves the old session's process alive under the slot uid — it is an
    orphan (nothing respawns a DRAINING row) and gets killed."""
    lc, calls = _make_lc(tmp_path, monkeypatch, pids=[4242], killed=[4242])
    _add_crashed_release(lc, DRAINING)
    report = lc.boot_reconcile()

    slot_uid = lc.cfg.pool.get("slot1").uid
    assert calls["killed"] == [slot_uid]
    assert any(r["slot"] == "slot1" and r["killed"] == [4242] for r in report)
    assert lc.cfg.pool.get("slot1").state == "quarantined"
    # the run-level 'interrupted' flag belongs to respawnable rows only —
    # the quarantine records a release interruption
    assert lc.cfg.registry.get(OLD).interrupted is False


def test_crashed_release_retry_path_works(tmp_path, monkeypatch):
    """The retry path: quarantined -> cleaning -> free. After boot
    quarantined the slot (with the session re-attached), a re-run release
    must be able to mark it cleaning and free it."""
    lc, _ = _make_lc(tmp_path, monkeypatch)
    _add_crashed_release(lc, DRAINING)
    lc.boot_reconcile()

    s = lc.cfg.pool.mark_cleaning(OLD)  # the release retry, step 1
    assert s.name == "slot1" and s.state == "cleaning"
    lc.cfg.pool.verify_free(OLD)
    assert lc.cfg.pool.get("slot1").state == "free"
    assert lc.cfg.pool.get("slot1").session_id is None


def test_live_row_still_rebuilt(tmp_path, monkeypatch):
    """Regression: the stage2 crash-drill shape (a LIVE row — the app
    died mid-run, not mid-release) is unchanged: the slot is re-attached
    for respawn, no quarantine, and the untouched slot stays free."""
    lc, _ = _make_lc(tmp_path, monkeypatch)
    lc.cfg.registry.upsert_session("live-sess", slot="slot1", state=LIVE)
    lc.boot_reconcile()

    s1 = lc.cfg.pool.get("slot1")
    assert s1.state == "allocated" and s1.session_id == "live-sess"
    s2 = lc.cfg.pool.get("slot2")
    assert s2.state == "free" and s2.session_id is None
    assert not [e for e in lc.events.items() if e["kind"] == "slot_quarantined"]
    assert lc.cfg.registry.get("live-sess").interrupted is False


def test_released_slot_stays_free(tmp_path, monkeypatch):
    """A completed release (FREE row) leaves its slot free and unowned —
    and out of the boot report entirely."""
    lc, _ = _make_lc(tmp_path, monkeypatch)
    lc.cfg.registry.upsert_session("done", slot="slot1", state=FREE)
    report = lc.boot_reconcile()

    s1 = lc.cfg.pool.get("slot1")
    assert s1.state == "free" and s1.session_id is None
    assert all(r["slot"] != "slot1" for r in report)


# ---------------------------------------------------------------------- #
# the late lib bind: a workdir a pre-lib plan left MOUNTED across an app
# update gets the bind at boot, and release() can unmount it
# ---------------------------------------------------------------------- #


def test_boot_adds_the_missing_lib_bind_to_a_mounted_workdir(tmp_path, monkeypatch):
    """A LIVE session whose workdir survived the update mounted by a
    pre-lib plan (mounted, no lib bind) gets the bind added at boot. The
    op rides a driver-less Live — the shape the pickup path already
    assumes — so release() unmounts it."""
    lc, _ = _make_lc(tmp_path, monkeypatch)
    lc.cfg.registry.upsert_session("live-sess", slot="slot1", state=LIVE)

    sp = SessionMountPaths.for_session("live-sess", str(lc.cfg.state.scratch))
    # the workdir is up (a pre-lib plan's), the lib bind is not
    monkeypatch.setattr(M, "mounted_targets", lambda: {sp.workdir})
    monkeypatch.setattr(os.path, "ismount", lambda p: p == sp.workdir)
    added = []
    fake_op = SimpleNamespace(
        kind="ro-bind", target=f"{sp.workdir}/lib", options="")
    monkeypatch.setattr(
        M, "ensure_lib_bind", lambda sp_, state: (added.append(sp_), [fake_op])[1]
    )

    lc.boot_reconcile()

    assert len(added) == 1  # the bind was added, exactly once
    live = lc.live.get("live-sess")
    assert live is not None and live.driver is None
    assert live.mount_ops == [fake_op]  # release() unmounts it
    assert lc.cfg.pool.get("slot1").state == "allocated"
    # a second boot (idempotence drill) does not lose the teardown op
    lc.boot_reconcile()
    assert lc.live["live-sess"].mount_ops == [fake_op]


def test_boot_leaves_a_current_plan_workdir_alone(tmp_path, monkeypatch):
    """A workdir whose plan already has the lib bind (the steady state)
    costs one ismount and no mount, and no Live entry is minted."""
    lc, _ = _make_lc(tmp_path, monkeypatch)
    lc.cfg.registry.upsert_session("live-sess", slot="slot1", state=LIVE)

    sp = SessionMountPaths.for_session("live-sess", str(lc.cfg.state.scratch))
    monkeypatch.setattr(M, "mounted_targets", lambda: {sp.workdir})
    monkeypatch.setattr(os.path, "ismount", lambda p: True)  # lib is up
    calls = []
    monkeypatch.setattr(M, "ensure_lib_bind", lambda sp_, state: calls.append(1))

    lc.boot_reconcile()

    assert calls == []  # the bind is up: ensure_lib_bind never even runs
    assert lc.live == {}  # nothing to record for teardown


def test_boot_skips_an_unmounted_workdir(tmp_path, monkeypatch):
    """A session that is not mounted at all (rebooted VM, unprivileged dev)
    says nothing: the full plan mounts it fresh on pickup."""
    lc, _ = _make_lc(tmp_path, monkeypatch)
    lc.cfg.registry.upsert_session("live-sess", slot="slot1", state=LIVE)

    calls = []
    monkeypatch.setattr(M, "ensure_lib_bind", lambda sp_, state: calls.append(1))

    lc.boot_reconcile()  # real /proc/mounts: no tmp workdir is ever mounted

    assert calls == []
    assert lc.live == {}
