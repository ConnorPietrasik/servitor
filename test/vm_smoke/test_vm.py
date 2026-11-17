"""Vm-smoke: the root-only isolation half.

Runs as root on the dev laptop (loud-skip in CI — never silently green):

- setuid spawn: child uid asserted from /proc (the Invariant-7 primitive
  that makes uid separation enforceable).
- chown handoff: the app (root) owns files until it hands them off.
- cross-uid EPERM: a slot process cannot write to a root-owned dir, read
  another slot's files, or kill another slot's process — the uid
  separation is real, not convention.
- the REAL sweep-by-owner end to end (find / -user slotN on the live
  filesystem).
- the FULL release() path: one call — drain, unmount, sweep, free —
  leaves zero residue (the single cleanup path).
- sweep failure ⇒ QUARANTINE (the slot is not freed while the sweep
  cannot verify clean; retryable after the blocker is cleared).

Slots: slot3 (+slot4 for the cross-uid pair).
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from app.iso import mounts as M
from app.iso.layout import SessionMountPaths, state_paths
from app.iso.slots import SlotPool
from app.iso.spawn import slot_identity, spawn_setuid
from app.iso.sweep import sweep_by_owner, find_owner_paths

from test.conftest import require_root_or_skip

SLOT = "slot3"
SLOT_B = "slot4"


@pytest.fixture
def vm_env(run_root):
    require_root_or_skip("vm-smoke (setuid, chown, real sweep, full release)")
    state = state_paths(str(run_root / "state"))
    for area in ("config", "lib", "inbox", "scratch", "sessions"):
        (Path(state.root) / area).mkdir(parents=True, exist_ok=True)
    toolkit = run_root / "toolkit"
    toolkit.mkdir(exist_ok=True)
    (toolkit / "toolkit-file.txt").write_text("lower\n")
    cfgsrc = run_root / "cfgsrc"
    cfgsrc.mkdir(exist_ok=True)
    (cfgsrc / "SYSTEM.md").write_text("[servitor-config v1]\n")
    (cfgsrc / "settings.json").write_text("{}\n")
    (cfgsrc / "models.json").write_text(
        '{"providers":{"mock":{"baseUrl":"http://127.0.0.1:1/v1","api":"openai-completions",'
        '"models":[{"id":"mock","name":"mock","contextWindow":1000}]}}}\n'
    )
    yield {"state": state, "toolkit": str(toolkit), "run_root": run_root}
    try:
        M.aunmount_all_safe(ALL_OPS)
    except Exception:
        pass


ALL_OPS: list = []


def _mount(session_id: str, state, toolkit: str):
    from app.iso import configswap

    sp = SessionMountPaths.for_session(session_id, state.scratch)
    v = configswap.stage_and_swap(str(state.config), str(Path(state.config).parent / "cfgsrc"))
    lower = configswap.require_active_dir(str(state.config))
    M.ensure_session_dirs(sp, state)
    ident = slot_identity(SLOT)
    ops = M.mount_session(sp, lower, state, ident.uid, ident.gid, toolkit)
    ALL_OPS.clear()
    ALL_OPS.extend(ops)
    return sp, ops


def _slot_process(slot: str, cmd: str) -> int:
    """Spawn a slot-uid process via the setuid primitive (proves the
    spawn works for real processes), return its pid."""
    ident = slot_identity(slot)
    proc = subprocess.Popen(
        ["setpriv", f"--reuid={ident.uid}", f"--regid={ident.gid}", "--clear-groups", "sh", "-c", cmd]
    )
    return proc.pid


def test_setuid_spawn_asserts_uid(vm_env):
    """spawn_setuid: the child's uid is ASSERTED from /proc (the
    Invariant-7 primitive — the helper refuses a mismatch by killing the
    child). The test re-reads the uid itself and proves the child really
    ran as the slot (the shell reports its own uid)."""
    import asyncio

    async def _inner():
        proc = await spawn_setuid(
            ["/bin/sh", "-c", f"echo uid-$(id -u) > /tmp/sv-{SLOT}-whoami"],
            slot=slot_identity(SLOT),
            cwd="/tmp",
            env={"PATH": "/usr/bin:/bin"},
        )
        # independently re-read the child's uid (the test asserts too)
        real_uid = None
        with open(f"/proc/{proc.pid}/status") as f:
            for line in f:
                if line.startswith("Uid:"):
                    real_uid = int(line.split()[1])
        assert real_uid == slot_identity(SLOT).uid
        await proc.wait()
        assert proc.returncode == 0

    asyncio.run(_inner())
    line = Path(f"/tmp/sv-{SLOT}-whoami").read_text().strip()
    assert line == f"uid-{slot_identity(SLOT).uid}"  # the shell saw the slot uid
    os.unlink(f"/tmp/sv-{SLOT}-whoami")


def test_chown_handoff_root_owns_until_handed(vm_env):
    """The app (root) owns files until it hands them off; a 0600
    root-owned file is unreadable to the slot until chowned (then
    readable, atomically: the rename-then-chown ordering in
    handoff_upload means the final name never exists without the owner)."""
    state = vm_env["state"]
    inbox = state.inbox_for("s-handoff")
    os.makedirs(inbox, exist_ok=True)
    # staged file (root, 0600): the slot cannot read it
    staged = os.path.join(state.root, "lib", f"handoff-{SLOT}.tmp")
    with open(staged, "w") as f:
        f.write("secret payload")
    os.chmod(staged, 0o600)
    r = subprocess.run(
        ["setpriv", f"--reuid={slot_identity(SLOT).uid}", f"--regid={slot_identity(SLOT).gid}",
         "--clear-groups", "sh", "-c", f"cat {staged}"],
        capture_output=True, text=True,
    )
    assert r.returncode != 0  # EACCES before handoff
    # hand off: rename (atomic, same fs) + chown to the slot
    final = os.path.join(inbox, "upload.txt")
    os.rename(staged, final)
    os.chown(final, slot_identity(SLOT).uid, slot_identity(SLOT).gid)
    r2 = subprocess.run(
        ["setpriv", f"--reuid={slot_identity(SLOT).uid}", f"--regid={slot_identity(SLOT).gid}",
         "--clear-groups", "sh", "-c", f"cat {final}"],
        capture_output=True, text=True,
    )
    assert r2.returncode == 0 and r2.stdout == "secret payload"
    os.unlink(final)


def test_cross_uid_perm_denied(vm_env):
    """Uid separation is ENFORCED: slot3 cannot write into a root-owned
    dir, read slot4's files, or kill slot4's process (EPERM, not
    convention)."""
    ident3 = slot_identity(SLOT)
    ident4 = slot_identity(SLOT_B)
    # (a) write into a root-owned 0755 dir: EACCES
    d = vm_env["run_root"] / "root-only-dir"
    d.mkdir(exist_ok=True)
    r = subprocess.run(
        ["setpriv", f"--reuid={ident3.uid}", f"--regid={ident3.gid}", "--clear-groups",
         "sh", "-c", f"echo x > {d}/x.txt"],
        capture_output=True, text=True,
    )
    assert r.returncode != 0, "slot wrote into a root-owned dir"
    # (b) read another slot's 0600 file: EACCES
    other = Path(f"/home/{SLOT_B}/sv-secret")
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text("slot4 secret")
    os.chown(other, ident4.uid, ident4.gid)
    os.chmod(other, 0o600)
    r2 = subprocess.run(
        ["setpriv", f"--reuid={ident3.uid}", f"--regid={ident3.gid}", "--clear-groups",
         "sh", "-c", f"cat {other}"],
        capture_output=True, text=True,
    )
    assert r2.returncode != 0, "slot3 read slot4's file"
    # (c) kill another slot's process: EPERM
    victim = _slot_process(SLOT_B, "sleep 30")
    time.sleep(0.4)
    r3 = subprocess.run(
        ["setpriv", f"--reuid={ident3.uid}", f"--regid={ident3.gid}", "--clear-groups",
         "sh", "-c", f"kill {victim}"],
        capture_output=True, text=True,
    )
    assert r3.returncode != 0 and "Operation not permitted" in r3.stderr
    # the victim survived and is still slot4
    vstat = os.stat(f"/proc/{victim}")
    assert vstat.st_uid == ident4.uid
    subprocess.run(["kill", "-9", str(victim)], capture_output=True)
    other.unlink(missing_ok=True)
    d.rmdir()


def test_real_sweep_by_owner_live(vm_env):
    """The real sweep: find / -user slotN across the live filesystem —
    home CONTENTS, /tmp, and a stray under the run root all get swept
    (the home dir itself is kept — the passwd entry); the verification
    pass comes back empty outside the kept home."""
    ident = slot_identity(SLOT)
    home = f"/home/{SLOT}"
    drops = [
        Path(home) / "sv-live-1",
        Path(f"/tmp/sv-live-{SLOT}-2"),
        Path(str(vm_env["run_root"])) / "sv-live-3",
    ]
    for p in drops:
        p.mkdir(parents=True, exist_ok=True)
        (p / "f.bin").write_bytes(b"x" * 1000)
        for q in (p, p / "f.bin"):
            os.chown(q, ident.uid, ident.gid)
    res = sweep_by_owner(ident.uid, (), keep_dir=home)
    assert res.ok, f"sweep errors: {res.errors}"
    assert all(not p.exists() for p in drops), [str(p) for p in drops if p.exists()]
    assert os.path.isdir(home)  # the home dir itself survived the sweep
    # independent final probe: the uid owns exactly the kept home dir now
    leftovers = find_owner_paths(ident.uid)
    assert [p.rstrip("/") for p in leftovers] == [home.rstrip("/")], leftovers


def test_full_release_path_leaves_zero_residue(run_root, pi_install):
    """The FULL release(): one call — drain, kill strays, unmount, sweep,
    free — leaves zero residue. This is the gate the rest of the system
    relies on: a released session is GONE (mounts, areas, slot, strays)."""
    import asyncio
    import shutil

    from conformance.harness import Harness

    require_root_or_skip("full release path")

    async def _inner():
        slot_home_test = Path(f"/home/{SLOT}/servitor-test")
        state_root = slot_home_test / run_root.name / "state"
        h = Harness(root=state_root, pi=pi_install, slot_name=SLOT)
        h.stage_config()
        try:
            live = await h.start_session()
            await h.prompt_and_settle(live, "release me please")
            sid = live.session_id
            workdir = live.sp.workdir
            sessions_dir = os.path.join(h.state.sessions, sid)
            out = await h.release(live)
            assert out.released, f"release failed: {out.errors}"
            assert not out.quarantined
            # zero residue, each axis asserted
            assert workdir not in M.mounted_targets(), "workdir still mounted"
            assert f"{workdir}/agent-dir" not in M.mounted_targets()
            assert not os.path.exists(workdir), "workdir dir survived"
            assert not os.path.exists(sessions_dir), "session transcript dir survived"
            assert not os.path.exists(os.path.join(h.state.scratch, sid)), "scratch area survived"
            assert h.pool.by_session(sid) is None, "slot not freed"
            # no slotN-owned file survives the sweep anywhere but the kept home
            ident = slot_identity(SLOT)
            leftovers = [p for p in find_owner_paths(ident.uid)
                         if p.rstrip("/") != f"/home/{SLOT}"]
            assert not leftovers, f"release left slotN-owned files: {leftovers[:10]}"
        finally:
            h.stop()

    asyncio.run(_inner())
    shutil.rmtree(Path(f"/home/{SLOT}/servitor-test") / run_root.name, ignore_errors=True)


def test_sweep_failure_quarantines_the_slot(run_root, pi_install):
    """A sweep that CANNOT verify clean (an immutable, undeletable
    slotN-owned file outside the exclusions) quarantines the slot instead
    of freeing it; the slot returns only when the retry re-sweep verifies
    clean (retryable; a human unblocks the file, the next release
    retries — the pool's quarantined -> cleaning -> free path)."""
    from app.iso.slots import NoFreeSlot, SlotQuarantined

    require_root_or_skip("quarantine path")
    ident = slot_identity(SLOT)
    home = f"/home/{SLOT}"
    # plant an immutable slot-owned file OUTSIDE the excluded areas:
    # /var/tmp is a real dir, not pruned by the sweep
    plant = Path(f"/var/tmp/sv-quarantine-{SLOT}")
    plant.mkdir(parents=True, exist_ok=True)
    (plant / "stuck.bin").write_bytes(b"stuck")
    for q in (plant, plant / "stuck.bin"):
        os.chown(q, ident.uid, ident.gid)
    subprocess.run(["chattr", "+i", str(plant / "stuck.bin")], check=True)
    try:
        pool = SlotPool((SLOT,))
        slot = pool.allocate("sess-q")
        assert slot.name == SLOT
        # the sweep must report the error and NOT verify clean
        res = sweep_by_owner(ident.uid, (), keep_dir=home)
        assert not res.ok, "sweep claimed clean with an undeletable file present"
        assert res.errors
        # release's decision: sweep not clean ⇒ quarantine (the pool API
        # raises AND records — the panel shows the state)
        with pytest.raises(SlotQuarantined):
            pool.quarantine("sess-q", "sweep could not verify clean")
        assert pool.get(SLOT).state == "quarantined"
        # a quarantined slot is NOT allocatable
        with pytest.raises(NoFreeSlot):
            pool.allocate("sess-q2")
        # the human clears the blocker; the retry path: the SAME session's
        # release re-enters (quarantined -> cleaning), re-sweeps, and frees
        subprocess.run(["chattr", "-i", str(plant / "stuck.bin")], check=True)
        res2 = sweep_by_owner(ident.uid, (), keep_dir=home)
        assert res2.ok, f"retry sweep failed: {res2.errors}"
        pool.mark_cleaning("sess-q")  # quarantined -> cleaning (retry)
        pool.verify_free("sess-q")  # cleaning -> free (verified)
        assert pool.get(SLOT).state == "free"
    finally:
        subprocess.run(["chattr", "-i", str(plant / "stuck.bin")], capture_output=True)
        import shutil

        shutil.rmtree(plant, ignore_errors=True)
        # belt: the slot must own nothing when the test ends
        leftovers = find_owner_paths(ident.uid)
        stuck = [p for p in leftovers if p.rstrip("/") != home.rstrip("/")]
        assert not stuck, stuck
