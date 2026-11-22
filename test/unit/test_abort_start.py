"""The failed-start path is the single cleanup path.

A start that fails partway (spawn, get_state) must route its abort
through ``release_session`` rather than a second, smaller cleanup: the
spawned child is shut down, strays are killed for the uid, the mounts
go down with failures reported, the uid's files are swept, the state
areas are deleted wholesale, and the slot is freed only when the sweep
verified clean. A failed sweep quarantines (never hands the uid back),
and the DRAINING row stays for panel/doctor visibility and a re-release
retry — exactly as on a stuck release.

Root-free: real registry + real SlotPool + real release_session; the
process-shaped bits (stray kill, owner sweep, driver, mounts) are
faked per the unit-suite pattern.
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.lifecycle as lcmod
from app.iso import mounts as M
from app.iso import release as release_mod
from app.iso.layout import state_paths
from app.iso.slots import SlotPool
from app.iso.sweep import SweepResult
from app.lifecycle import DRAINING, Lifecycle, LifecycleConfig, PiInstall
from app.registry import Registry

SLOT = "slot1"
UID = 9001
SID = "s-f44"


# ---------------------------------------------------------------------- #
# fakes                                                                   #
# ---------------------------------------------------------------------- #


class FakeOp:
    """A mounted target whose teardown is recorded, not run."""

    def __init__(self, target: str):
        self.target = target
        self.unmounted = False

    def unmount(self) -> None:
        self.unmounted = True


class FakeDriver:
    """Process-shaped fake: a spawned child whose get_state can fail."""

    def __init__(self, *, state_error: Exception | None = None):
        self.started = False
        self.state_error = state_error
        self.shutdown_calls = 0
        self.close_calls = 0

    async def start(self) -> None:
        self.started = True

    async def get_state(self) -> dict:
        if self.state_error is not None:
            raise self.state_error
        return {"sessionFile": f"/x/{SID}.jsonl", "sessionId": SID}

    async def shutdown(self, **kw) -> int:
        self.shutdown_calls += 1
        return 0

    async def close(self) -> None:
        self.close_calls += 1


# ---------------------------------------------------------------------- #
# fixture                                                                   #
# ---------------------------------------------------------------------- #


def _make_lc(tmp_path, monkeypatch):
    """Real Lifecycle + real SlotPool + real registry + real
    release_session, with the process-shaped primitives faked: the stray
    kill and the owner sweep (a real sweep would `find /` on the host,
    non-hermetically — and could delete another user's files)."""
    monkeypatch.setattr(
        "app.iso.slots.pwd.getpwnam",
        lambda name: SimpleNamespace(
            pw_uid=UID, pw_gid=UID, pw_dir=f"/home/{name}"),
    )
    state = state_paths(str(tmp_path / "state"))
    for area in (state.config, state.lib, state.inbox, state.scratch,
                 state.sessions):
        Path(area).mkdir(parents=True, exist_ok=True)
    Path(state.config, "v1").mkdir()
    os.symlink("v1", str(Path(state.config) / "active"))

    pool = SlotPool((SLOT,))
    reg = Registry(str(tmp_path / "registry.db"))
    cfg = LifecycleConfig(
        state=state,
        pool=pool,
        registry=reg,
        pi=PiInstall(node_bin="/usr/bin/node", cli_js="/opt/pi/cli.js",
                     ext_path=None),
        api_key="k",
    )
    lc = Lifecycle(cfg)
    monkeypatch.setattr(lcmod, "_new_session_id", lambda: SID)

    calls = {"clean": True, "kill": [], "sweep": []}

    def fake_kill(uid, **kw):
        calls["kill"].append(uid)
        return []

    def fake_sweep(uid, excludes=(), **kw):
        calls["sweep"].append((uid, tuple(excludes), kw))
        if calls["clean"]:
            return SweepResult(uid=uid, verified_clean=True)
        return SweepResult(
            uid=uid,
            errors=[(f"/tmp/litter-{uid}", "EPERM (simulated)")],
            verified_clean=False,
        )

    monkeypatch.setattr(release_mod, "kill_strays_by_uid", fake_kill)
    monkeypatch.setattr(release_mod, "sweep_by_owner", fake_sweep)
    return lc, state, pool, reg, calls


def _areas(state) -> tuple[str, str, str]:
    return (
        state.inbox_for(SID),
        state.scratch_for(SID),
        state.sessions_for(SID),
    )


def _feed_kinds(lc) -> list[str]:
    """Feed event kinds for the session — the panel's app-level view."""
    return [i["kind"] for i in lc.events.items()
            if i["session_id"] == SID]


# ---------------------------------------------------------------------- #
# the single cleanup path, end to end                                       #
# ---------------------------------------------------------------------- #


async def test_failed_start_after_spawn_cleans_and_frees(tmp_path, monkeypatch):
    """The realistic trigger: the child spawned, get_state timed out
    (cold start). Everything the start already did is rolled back:
    child shut down (the driver was retained at start()), mounts down,
    strays killed for the uid, state areas deleted, slot freed — and
    only because the sweep verified clean."""
    lc, state, pool, reg, calls = _make_lc(tmp_path, monkeypatch)
    mounted = {}

    def fake_mount(sp, lower, st, uid, gid, tk):
        ops = [FakeOp(sp.workdir)]
        mounted["ops"] = ops
        return ops

    monkeypatch.setattr(M, "mount_session", fake_mount)
    driver = FakeDriver(state_error=TimeoutError("get_state (simulated)"))
    lc._make_driver = lambda sid, sf, live: driver

    with pytest.raises(TimeoutError):
        await lc.start_session()

    # the spawned child was shut down and closed (the retained driver)
    assert driver.started
    assert driver.shutdown_calls == 1
    assert driver.close_calls == 1
    # the mounts went down via release (reverse order, reported)
    assert all(op.unmounted for op in mounted["ops"])
    # strays killed for the slot uid; the sweep excluded the three areas
    assert calls["kill"] == [UID]
    uid, excludes, kw = calls["sweep"][0]
    assert uid == UID
    assert excludes == _areas(state)
    assert kw == {"keep_dir": pool.get(SLOT).home_path}
    # the state areas are GONE (deleted wholesale, not just the row)
    for area in _areas(state):
        assert not os.path.lexists(area)
    # slot back to the pool — via verify_free, i.e. sweep-verified
    assert pool.get(SLOT).state == "free"
    assert pool.get(SLOT).session_id is None
    # row and live entry gone, no hub left behind
    assert reg.get(SID) is None
    assert lc.live == {}
    assert SID not in lc._hubs
    # no ghost release events: the session never existed and the row is
    # gone — release_start/released point at an id nothing resolves
    kinds = _feed_kinds(lc)
    assert "release_start" not in kinds
    assert "released" not in kinds


async def test_failed_start_before_spawn_still_sweeps_and_deletes(tmp_path,
                                                                   monkeypatch):
    """A pre-spawn failure (mount/tamper gate) leaks nothing either:
    the areas were created before the failure and the abort still runs
    the full release — sweep, rmtree, sweep-verified free."""
    lc, state, pool, reg, calls = _make_lc(tmp_path, monkeypatch)

    def boom(*a, **k):
        raise M.MountError("tamper gate (simulated)")

    monkeypatch.setattr(M, "mount_session", boom)

    with pytest.raises(M.MountError):
        await lc.start_session()

    # the areas existed (real ensure_session_dirs) and are gone now
    for area in _areas(state):
        assert not os.path.lexists(area)
    assert calls["kill"] == [UID]
    assert calls["sweep"][0][0] == UID
    assert pool.get(SLOT).state == "free"
    assert pool.get(SLOT).session_id is None
    assert reg.get(SID) is None
    assert lc.live == {}
    # no ghost release events, as on the post-spawn path
    kinds = _feed_kinds(lc)
    assert "release_start" not in kinds
    assert "released" not in kinds


async def test_failed_start_dirty_sweep_quarantines_and_retry_releases(
        tmp_path, monkeypatch):
    """A sweep that cannot verify clean never frees the slot (a slot is
    never handed out before its sweep verifies clean): the slot is
    quarantined — visible, retryable, never reused — and the DRAINING row
    STAYS (unlike the clean path), so the panel/doctor see it and an
    operator re-runs release (quarantined -> cleaning is accepted by
    mark_cleaning)."""
    lc, state, pool, reg, calls = _make_lc(tmp_path, monkeypatch)
    calls["clean"] = False

    def boom(*a, **k):
        raise M.MountError("tamper gate (simulated)")

    monkeypatch.setattr(M, "mount_session", boom)

    with pytest.raises(M.MountError):
        await lc.start_session()

    slot = pool.get(SLOT)
    # NOT freed: quarantined, still owning the session's cleanup
    assert slot.state == "quarantined"
    assert slot.session_id == SID
    # the DRAINING row stays: visibility + the retry path
    row = reg.get(SID)
    assert row is not None and row.state == DRAINING
    assert lc.live == {}
    # the areas were still deleted wholesale (release step 6 runs before
    # the step-7 free/quarantine decision)
    for area in _areas(state):
        assert not os.path.lexists(area)
    # feed (checked BEFORE the retry — the retry is a real release and
    # events release_start/released as it should): no release-shaped
    # ghosts for the never-existed session, but the quarantine IS seen —
    # the row stays, and this is where it is visible
    kinds = _feed_kinds(lc)
    assert "release_start" not in kinds
    assert "released" not in kinds
    assert "slot_quarantined" in kinds

    # the retry: clear the blocker, re-run release
    calls["clean"] = True
    outcome = await lc.release(SID)
    assert outcome.released is True
    assert pool.get(SLOT).state == "free"
    assert pool.get(SLOT).session_id is None
    assert reg.get(SID).state == "free"


def test_no_second_free_path_exists():
    """Structural guard: the only way a slot returns to the pool is
    ``verify_free`` (release step 7, sweep-verified). The unverified
    free path (SlotPool.release_slot_if_owned) must not exist."""
    assert not hasattr(SlotPool, "release_slot_if_owned")
    assert hasattr(SlotPool, "verify_free")
