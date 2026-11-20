"""Sandbox-smoke: the isolation gates runnable as root (server or CI).

Overlay lower+upper/work on one fs; writes land in upper; whiteouts;
EROFS on the RO binds incl. models.json unlink-EBUSY; mount-before-spawn
ordering; teardown order; sweep walk; config swap atomicity (no live
lower mutated); state-fs-type assert; ancestor chain free of
context-file names.

Slot: slot2 (suite allocation, test/conftest.py). No pi process is
spawned — these prove the isolation PRIMITIVES the protocol suite runs
on (isolation is proven before the lifecycle builds on it).
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.iso import configswap, mounts as M
from app.iso.layout import SessionMountPaths, state_paths
from app.iso.slots import SlotPool
from app.iso.spawn import slot_identity
from app.iso.sweep import sweep_by_owner

from test.conftest import require_root_or_skip

SLOT = "slot2"


@pytest.fixture
def iso_env(run_root):
    require_root_or_skip("sandbox-smoke (overlay mounts, sweep, root)")
    state = state_paths(str(run_root / "state"))
    for area in ("config", "lib", "inbox", "scratch", "sessions"):
        (Path(state.root) / area).mkdir(parents=True, exist_ok=True)
    toolkit = run_root / "toolkit"
    toolkit.mkdir(exist_ok=True)
    (toolkit / "toolkit-file.txt").write_text("lower file\n")
    (toolkit / "AGENTS.md.must.not.exist").unlink(missing_ok=True)
    # a minimal config source for the versioned lower (mounts don't care
    # about the model — no spawn happens in this suite)
    cfgsrc = run_root / "cfgsrc"
    cfgsrc.mkdir(exist_ok=True)
    (cfgsrc / "SYSTEM.md").write_text("[servitor-config v1]\n")
    (cfgsrc / "settings.json").write_text("{}\n")
    (cfgsrc / "models.json").write_text(
        '{"providers":{"mock":{"baseUrl":"http://127.0.0.1:1/v1","api":"openai-completions",'
        '"models":[{"id":"mock","name":"mock","contextWindow":1000}]}}}\n'
    )
    yield {"state": state, "toolkit": str(toolkit), "run_root": run_root}
    # reap any mounts a test left (the gates assert teardown themselves)
    try:
        M.aunmount_all_safe(MOUNTED_OPS)
    except Exception:
        pass


MOUNTED_OPS: list = []


def _mount_session(env, session_id: str) -> tuple[SessionMountPaths, list[M.MountOp], str]:
    state = env["state"]
    sp = SessionMountPaths.for_session(session_id, state.scratch)
    lower = configswap.stage_and_swap(str(state.config), str(env["run_root"] / "cfgsrc"))
    lower_dir = configswap.require_active_dir(str(state.config))
    ident = slot_identity(SLOT)
    # make the staged config tree slot-owned (the bind SOURCE): the
    # agent's write denials are then attributable to the MOUNT (EROFS),
    # not the file's permission bits (EACCES). Production's contract is
    # the same, root-owned source.
    for dirpath, dirnames, filenames in os.walk(lower_dir):
        for name in dirnames + filenames:
            os.chown(os.path.join(dirpath, name), ident.uid, ident.gid)
    M.ensure_session_dirs(sp, state)
    ops = M.mount_session(sp, lower_dir, state, ident.uid, ident.gid, env["toolkit"])
    MOUNTED_OPS.clear()
    MOUNTED_OPS.extend(ops)
    return sp, ops, lower_dir


def _as_slot(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["setpriv", f"--reuid={slot_identity(SLOT).uid}", f"--regid={slot_identity(SLOT).gid}",
         "--clear-groups", *cmd],
        capture_output=True, text=True,
    )


def _ro_fs_type(path: str) -> str:
    out = subprocess.run(["findmnt", "-T", path, "-n", "-o", "FSTYPE"], capture_output=True, text=True)
    return out.stdout.strip()


def test_state_fs_type_is_real_not_overlay():
    """The state volume is a real filesystem, never overlay
    (overlayfs refuses upperdir/workdir on overlayfs)."""
    require_root_or_skip("state fs type assert")
    assert _ro_fs_type("/srv/servitor-test") != "overlay"
    assert _ro_fs_type("/home/slot2") != "overlay"


def test_overlay_writes_land_in_upper(iso_env):
    sp, ops, _ = _mount_session(iso_env, "s-overlay")
    try:
        # the merged view shows the lower
        assert os.path.isfile(f"{sp.workdir}/toolkit-file.txt")
        # a slot-uid write lands in the UPPER, not the lower
        r = _as_slot(["sh", "-c", f"echo slot-write > {sp.workdir}/new.txt"])
        assert r.returncode == 0, r.stderr
        assert os.path.isfile(f"{sp.upper}/new.txt")
        assert not os.path.exists(f"{iso_env['toolkit']}/new.txt")
        # the lower is untouched (a live lower is never mutated)
        assert Path(f"{iso_env['toolkit']}/toolkit-file.txt").read_text() == "lower file\n"
    finally:
        M.unmount_all(ops)


def test_overlay_whiteout(iso_env):
    sp, ops, _ = _mount_session(iso_env, "s-whiteout")
    try:
        # deleting a LOWER file from the merged view produces a whiteout
        # in the upper (character device 0:0), the lower file survives
        r = _as_slot(["sh", "-c", f"rm {sp.workdir}/toolkit-file.txt"])
        assert r.returncode == 0, r.stderr
        assert not os.path.exists(f"{sp.workdir}/toolkit-file.txt")
        whiteout = Path(sp.upper) / "toolkit-file.txt"
        assert whiteout.exists()
        import stat as st
        assert st.S_ISCHR(whiteout.lstat().st_mode)
        assert os.path.isfile(f"{iso_env['toolkit']}/toolkit-file.txt")  # lower intact
    finally:
        M.unmount_all(ops)


def test_models_json_ro_bind_erofs_and_ebusy(iso_env):
    """The single-file RO bind of models.json — write ⇒ EROFS,
    unlink ⇒ EBUSY, even as ROOT (the agent is not root; this proves the
    bind, and root's failure proves it is the MOUNT, not the uid)."""
    sp, ops, lower_dir = _mount_session(iso_env, "s-robind")
    try:
        target = f"{sp.agent_dir}/models.json"
        assert os.path.isfile(target)
        # write as root: EROFS
        with pytest.raises(OSError) as ei:
            with open(target, "a") as f:
                f.write("x")
        assert ei.value.errno == 30  # EROFS
        # unlink as root: EBUSY
        with pytest.raises(OSError) as ei2:
            os.unlink(target)
        assert ei2.value.errno == 16  # EBUSY
        # the agent (slot uid) gets EROFS too — the bind source is
        # slot-owned (the helper chowns the staged config tree), so the
        # refusal is the MOUNT's (EROFS), not the file's permission bits
        r = _as_slot(["sh", "-c", f"echo x >> {target}"])
        assert r.returncode != 0 and "Read-only" in r.stderr
    finally:
        M.unmount_all(ops)


def test_inbox_bind_is_the_same_directory(iso_env):
    sp, ops, _ = _mount_session(iso_env, "s-inbox")
    try:
        src = os.path.join(iso_env["state"].inbox_for("s-inbox"), "upload.bin")
        with open(src, "wb") as f:
            f.write(b"upload-bytes")
        os.chown(src, slot_identity(SLOT).uid, slot_identity(SLOT).gid)
        # visible in the merged workdir (bind-mounted in)
        with open(f"{sp.workdir}/inbox/upload.bin", "rb") as f:
            assert f.read() == b"upload-bytes"
        # and the reverse: a slot write in the merged inbox lands in the source
        r = _as_slot(["sh", "-c", f"echo from-agent > {sp.workdir}/inbox/agent.txt"])
        assert r.returncode == 0, r.stderr
        assert os.path.isfile(os.path.join(iso_env["state"].inbox_for("s-inbox"), "agent.txt"))
    finally:
        M.unmount_all(ops)


def test_mount_before_spawn_and_teardown_order(iso_env):
    """Mounts are up before ANY spawn (the harness enforces it; here: the
    plan's order is workdir → agent-dir → inbox → models.json, and
    teardown is the EXACT reverse, each unmount succeeding)."""
    sp, ops, _ = _mount_session(iso_env, "s-order")
    try:
        assert [op.kind for op in ops] == ["overlay", "overlay", "bind", "ro-bind-file"]
        # teardown order: reverse
        unmounted = M.unmount_all(ops)
        assert unmounted == [op.target for op in reversed(ops)]
        # after teardown the plain dirs remain (release deletes them)
        assert os.path.isdir(sp.workdir)
    finally:
        M.aunmount_all_safe(ops)


def test_sweep_walk_finds_and_deletes_everywhere(iso_env):
    """find / -user slotN catches the home, /tmp, and stray drops; the
    sweep deletes them and the verification pass comes back empty."""
    ident = slot_identity(SLOT)
    home = Path(f"/home/{SLOT}")
    stray_tmp = Path(f"/tmp/sv-sweep-{SLOT}-probe")
    stray_home = home / "sv-sweep-probe"
    for p in (stray_tmp, stray_home):
        p.mkdir(parents=True, exist_ok=True)
        (p / "junk.txt").write_text("junk")
    os.chown(stray_tmp, ident.uid, ident.gid)
    os.chown(stray_tmp / "junk.txt", ident.uid, ident.gid)
    os.chown(stray_home, ident.uid, ident.gid)
    os.chown(stray_home / "junk.txt", ident.uid, ident.gid)
    # an excluded prefix must survive the sweep
    keep = Path(f"/srv/servitor-test/sv-keep-{SLOT}")
    keep.mkdir(parents=True, exist_ok=True)
    os.chown(keep, ident.uid, ident.gid)
    try:
        res = sweep_by_owner(ident.uid, (str(keep),), keep_dir=str(home))
        assert res.ok, f"errors={res.errors}"
        assert not stray_tmp.exists() and not stray_home.exists()
        assert home.exists() and home.is_dir()  # the home dir itself kept
        assert keep.exists()  # excluded prefix pruned
    finally:
        shutil.rmtree(keep, ignore_errors=True)
        shutil.rmtree(stray_tmp, ignore_errors=True)
        shutil.rmtree(stray_home, ignore_errors=True)


def test_config_swap_atomicity_live_lower_untouched(iso_env):
    """Versioned swap: stage + rename; the pointer flips atomically; a
    LIVE lower (v1, held by a reader) is never mutated."""
    state = iso_env["state"]
    cfg_root = str(state.config)
    src = Path(cfg_root) / "src"
    src.mkdir()
    (src / "SYSTEM.md").write_text("[servitor-config v1]\n")
    (src / "settings.json").write_text("{}")
    v1 = configswap.stage_and_swap(cfg_root, str(src))
    v1_hash = hashlib.sha256((Path(cfg_root) / v1 / "SYSTEM.md").read_bytes()).hexdigest()
    # a "live" reader holds the v1 lower open (overlay lowers are the
    # version dirs; emulate: the fd + the dir contents must survive v2)
    with open(Path(cfg_root) / v1 / "SYSTEM.md", "rb") as held:
        (src / "SYSTEM.md").write_text("[servitor-config v2]\n")
        v2 = configswap.stage_and_swap(cfg_root, str(src))
        held.read()  # the live reader still reads v1's bytes
        assert configswap.active_version(cfg_root) == v2
        assert configswap.active_dir(cfg_root) == str(Path(cfg_root) / v2)
    # the live lower is byte-identical
    assert hashlib.sha256((Path(cfg_root) / v1 / "SYSTEM.md").read_bytes()).hexdigest() == v1_hash
    # the pointer is never dangling: every read yields a resolvable version
    for _ in range(50):
        v = configswap.active_version(cfg_root)
        assert v in (v1, v2) and os.path.isdir(Path(cfg_root) / v)


def test_ancestor_chain_free_of_context_files(iso_env):
    """The session tree root's ancestor chain (workdir → /) must contain
    no AGENTS.md/CLAUDE.md/.pi (doctor asserts; the launch flag removes
    them from loading — this is the belt)."""
    sp, ops, _ = _mount_session(iso_env, "s-ancestors")
    try:
        p = Path(sp.workdir)
        bad = []
        while True:
            for name in ("AGENTS.md", "CLAUDE.md", ".pi"):
                if (p / name).exists():
                    bad.append(str(p / name))
            parent = p.parent
            if parent == p:
                break
            p = parent
        assert not bad, f"context-file names in the ancestor chain: {bad}"
    finally:
        M.unmount_all(ops)
