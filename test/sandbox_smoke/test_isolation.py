"""Sandbox-smoke: the isolation gates runnable as root (server or CI).

Overlay lower+upper/work on one fs; writes land in upper; whiteouts;
EROFS on the RO binds incl. models.json unlink-EBUSY; the inbox bind is
read-only to the agent and the outbox bind is its writable handoff; the
delivery read-gate refuses a planted HARDLINK (the one shape
resolve-and-contain cannot see) with real uids — on every path that lists,
serves, archives, renames, or unlinks — and refuses a link the agent made to
an upload's blob; mount-before-spawn
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

from app.delivery.service import Delivery
from app.delivery.store import FileNotDelivered, SessionFiles
from app.delivery.usage import area_usage
from app.iso import configswap, mounts as M
from app.iso.layout import STATE_AREAS, SessionMountPaths, state_paths
from app.iso.release import area_footprint
from app.iso.slots import SlotPool
from app.iso.spawn import slot_identity
from app.iso.sweep import sweep_by_owner

from test.conftest import require_root_or_skip

SLOT = "slot2"


@pytest.fixture
def iso_env(run_root):
    require_root_or_skip("sandbox-smoke (overlay mounts, sweep, root)")
    state = state_paths(str(run_root / "state"))
    for area in STATE_AREAS:
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


def test_inbox_bind_is_read_only_to_the_agent(iso_env):
    """What the human sent is the agent's to read and never to lose.

    The bind source is root-owned inside a root-owned directory, so every
    mutation the agent might try is refused by the kernel rather than by a
    rule in a prompt: create, in-place write, unlink, replace-by-rename.
    Reading and hardlinking OUT still work — that is what an agent needs an
    upload for, and it cannot disturb the bytes it shares with the store.
    """
    sp, ops, _ = _mount_session(iso_env, "s-inbox")
    inbox_src = iso_env["state"].inbox_for("s-inbox")
    try:
        src = os.path.join(inbox_src, "upload.bin")
        with open(src, "wb") as f:
            f.write(b"upload-bytes")  # root-owned, as a delivery leaves it
        with open(f"{sp.workdir}/inbox/upload.bin", "rb") as f:
            assert f.read() == b"upload-bytes"  # visible through the bind

        for verb, cmd in (
            ("create", f"echo from-agent > {sp.workdir}/inbox/agent.txt"),
            ("append", f"echo x >> {sp.workdir}/inbox/upload.bin"),
            ("overwrite", f"echo x > {sp.workdir}/inbox/upload.bin"),
            ("unlink", f"rm -f {sp.workdir}/inbox/upload.bin"),
            ("rename-over", f"echo x > {sp.workdir}/new && mv {sp.workdir}/new {sp.workdir}/inbox/upload.bin"),
            ("mkdir", f"mkdir {sp.workdir}/inbox/sub"),
        ):
            r = _as_slot(["sh", "-c", cmd])
            assert r.returncode != 0, f"{verb} succeeded: {r.stderr}"
        assert open(src, "rb").read() == b"upload-bytes"
        assert not os.path.exists(os.path.join(inbox_src, "agent.txt"))
        assert not os.path.exists(os.path.join(inbox_src, "sub"))

        # ... and the useful half of read-only: a copy into its own area works
        r = _as_slot(["sh", "-c", f"cp {sp.workdir}/inbox/upload.bin {sp.workdir}/outbox/copy.bin"])
        assert r.returncode == 0, r.stderr
        assert open(f"{sp.workdir}/outbox/copy.bin", "rb").read() == b"upload-bytes"
    finally:
        M.unmount_all(ops)


def test_outbox_bind_is_the_agents_handoff_area(iso_env):
    """The outbox is a bind of a state-volume directory, not a directory in
    the overlay upper: the app sees the artifacts while the session is still
    mounted, and a KEEP is a hardlink on one filesystem instead of a copy."""
    sp, ops, _ = _mount_session(iso_env, "s-outbox")
    out_src = iso_env["state"].outbox_for("s-outbox")
    try:
        r = _as_slot(["sh", "-c", f"mkdir -p {sp.workdir}/outbox/sub && echo artifact > {sp.workdir}/outbox/sub/report.md"])
        assert r.returncode == 0, r.stderr
        assert open(os.path.join(out_src, "sub", "report.md")).read() == "artifact\n"
        keep = os.path.join(iso_env["state"].keep, "s-outbox", "report.md")
        os.makedirs(os.path.dirname(keep), exist_ok=True)
        os.link(os.path.join(out_src, "sub", "report.md"), keep)  # zero-copy keep
        # the session's area goes away (release deletes it wholesale)...
        shutil.rmtree(out_src)
        # ...and what the human kept is untouched: the link holds the bytes
        assert open(keep).read() == "artifact\n"
    finally:
        M.unmount_all(ops)


def test_a_human_made_outbox_folder_stays_writable_by_the_agent(iso_env):
    """A human rename into a new folder creates it as root. The folder must
    be handed to the slot, or the human's own action erects an EACCES wall
    the agent's next write into that folder runs into — and the outbox is
    the agent's handoff, not a museum."""
    sp, ops, _ = _mount_session(iso_env, "s-fold")
    state = iso_env["state"]
    try:
        r = _as_slot(["sh", "-c", f"echo mine > {sp.workdir}/outbox/mine.txt"])
        assert r.returncode == 0, r.stderr
        sf = SessionFiles(
            session_dir=state.sessions_for("s-fold"),
            inbox=state.inbox_for("s-fold"),
            outbox=state.outbox_for("s-fold"),
            slot=slot_identity(SLOT),
        )
        assert sf.rename_outbox("mine.txt", "outbox/deep/sub/mine.txt")["to"] == "deep/sub/mine.txt"
        for folder in ("deep", "deep/sub"):
            st = os.stat(os.path.join(state.outbox_for("s-fold"), folder))
            assert st.st_uid == slot_identity(SLOT).uid, (
                f"{folder} is the agent's to work in, not a root wall"
            )
        r = _as_slot(["sh", "-c", f"echo next > {sp.workdir}/outbox/deep/sub/next.txt"])
        assert r.returncode == 0, r.stderr
        assert open(os.path.join(state.outbox_for("s-fold"), "deep/sub/next.txt")).read() == "next\n"
    finally:
        M.unmount_all(ops)


def test_a_planted_hardlink_in_the_outbox_is_not_root_readable(iso_env):
    """The exact channel the ownership gate closes, with REAL uids.

    A hardlink is a plain regular file inside the directory: resolve-then-
    contain passes it, and the listing's symlink skip does not fire. Only
    ``st_uid == slot.uid`` tells it apart from the agent's own work — so this
    asserts both halves: the planted link is invisible and unservable, and a
    file the session really wrote still is served.
    """
    sp, ops, _ = _mount_session(iso_env, "s-link")
    ident = slot_identity(SLOT)
    sf = SessionFiles(
        session_dir=iso_env["state"].sessions_for("s-link"),
        inbox=iso_env["state"].inbox_for("s-link"),
        outbox=iso_env["state"].outbox_for("s-link"),
        slot=ident,
    )
    try:
        r = _as_slot(["sh", "-c", f"ln /etc/hostname {sp.workdir}/outbox/steal.txt"])
        assert r.returncode == 0, r.stderr  # the agent CAN do this much
        r = _as_slot(["sh", "-c", f"echo mine > {sp.workdir}/outbox/mine.txt"])
        assert r.returncode == 0, r.stderr

        assert [e["path"] for e in sf.list_outbox()] == ["mine.txt"]
        with pytest.raises(FileNotDelivered):
            sf.outbox_path("steal.txt")
        assert sf.outbox_path("mine.txt").is_file()

        # every path that reads or unlinks must carry the same gate, not just
        # the download: an archive built by walking the directory would hand
        # out /etc/hostname, and a rename/delete would be a root-powered way
        # to move or destroy an inode the session never owned
        with pytest.raises(FileNotDelivered):
            sf.delete_outbox("steal.txt")
        with pytest.raises(FileNotDelivered):
            sf.rename_outbox("steal.txt", "outbox/renamed.txt")
        assert Path("/etc/hostname").exists()
        _delivery_entries = Delivery(iso_env["state"].root).archive_entries(sf, "outbox")
        assert [a for a, _ in _delivery_entries[1]] == ["mine.txt"]
        # and renaming the session's OWN file works (the gate is ownership,
        # not a blanket refusal)
        assert sf.rename_outbox("mine.txt", "outbox/sub/mine.txt")["to"] == "sub/mine.txt"
    finally:
        M.unmount_all(ops)


def test_an_agent_cannot_make_the_app_unlink_or_rename_its_own_uploads(iso_env):
    """An upload is a root-owned blob link, and the agent can put another link
    to the same inode in its own outbox (the store is world-readable and
    hardlinking needs no write permission). That must buy it nothing: the app
    neither serves, lists, archives, renames, nor unlinks an inode the session
    does not own — the bytes stay the app's. Otherwise ``ln inbox/x outbox/x``
    plus a delete would be a root-powered unlink of a file the human asked to
    keep."""
    sp, ops, _ = _mount_session(iso_env, "s-hl")
    ident = slot_identity(SLOT)
    state = iso_env["state"]
    delivery = Delivery(state.root)
    sf = delivery.files(state, "s-hl", ident)
    try:

        async def stream():
            yield b"the human sent this\n"

        # the route's own three steps (create → pump → complete)
        staged = delivery.staged("s-hl")

        async def upload():
            uid, _off = await asyncio.to_thread(staged.create, "sent.txt")
            await staged.append_stream(uid, 0, stream())
            return await delivery.complete_staged(staged, uid, sf)

        landed = asyncio.run(upload())
        r = _as_slot(["sh", "-c", f"ln {sp.workdir}/inbox/{landed.name} {sp.workdir}/outbox/x"
                      f"; cp -l {sp.workdir}/inbox/{landed.name} {sp.workdir}/outbox/y"])
        assert r.returncode == 0, r.stderr

        assert [u["name"] for u in sf.list_uploads()] == ["sent.txt"]
        assert sf.list_outbox() == [], "a blob link in the outbox is not the session's"
        for name in ("x", "y"):
            with pytest.raises(FileNotDelivered):
                sf.delete_outbox(name)
            with pytest.raises(FileNotDelivered):
                sf.rename_outbox(name, f"outbox/moved-{name}")
        # the upload itself is still there, intact, and still the human's
        assert sf.upload_path(landed.name).read_bytes() == b"the human sent this\n"
    finally:
        M.unmount_all(ops)


def test_mount_before_spawn_and_teardown_order(iso_env):
    """Mounts are up before ANY spawn (the harness enforces it; here: the
    plan's order is workdir → agent-dir → inbox → outbox → models.json, and
    teardown is the EXACT reverse, each unmount succeeding)."""
    sp, ops, _ = _mount_session(iso_env, "s-order")
    try:
        assert [op.kind for op in ops] == [
            "overlay", "overlay", "bind", "bind", "ro-bind-file",
        ]
        # the two binds are the session's two file areas, in the workdir
        assert [op.target for op in ops][2:4] == [
            f"{sp.workdir}/inbox", f"{sp.workdir}/outbox",
        ]
        assert [op.options for op in ops][2:4] == [
            iso_env["state"].inbox_for("s-order"),
            iso_env["state"].outbox_for("s-order"),
        ]
        # teardown order: reverse
        unmounted = M.unmount_all(ops)
        assert unmounted == [op.target for op in reversed(ops)]
        # after teardown the plain dirs remain (release deletes them)
        assert os.path.isdir(sp.workdir)
    finally:
        M.aunmount_all_safe(ops)


def test_sweep_walk_finds_and_deletes_everywhere(iso_env):
    """find / -user slotN catches the home, /tmp, /dev/shm, and stray
drops; the sweep deletes them and the verification pass comes back
empty. (/dev/shm: world-writable tmpfs a slot can litter — the walk
must not prune /dev wholesale.)"""
    ident = slot_identity(SLOT)
    home = Path(f"/home/{SLOT}")
    stray_tmp = Path(f"/tmp/sv-sweep-{SLOT}-probe")
    stray_home = home / "sv-sweep-probe"
    stray_shm = Path(f"/dev/shm/sv-sweep-{SLOT}-probe")
    for p in (stray_tmp, stray_home, stray_shm):
        p.mkdir(parents=True, exist_ok=True)
        (p / "junk.txt").write_text("junk")
    for p in (stray_tmp, stray_home, stray_shm):
        os.chown(p, ident.uid, ident.gid)
        os.chown(p / "junk.txt", ident.uid, ident.gid)
    # an excluded prefix must survive the sweep
    keep = Path(f"/srv/servitor-test/sv-keep-{SLOT}")
    keep.mkdir(parents=True, exist_ok=True)
    os.chown(keep, ident.uid, ident.gid)
    try:
        res = sweep_by_owner(ident.uid, (str(keep),), keep_dir=str(home))
        assert res.ok, f"errors={res.errors}"
        assert not stray_tmp.exists() and not stray_home.exists()
        assert not stray_shm.exists()  # /dev/shm litter swept
        assert home.exists() and home.is_dir()  # the home dir itself kept
        assert keep.exists()  # excluded prefix pruned
    finally:
        shutil.rmtree(keep, ignore_errors=True)
        shutil.rmtree(stray_tmp, ignore_errors=True)
        shutil.rmtree(stray_home, ignore_errors=True)
        shutil.rmtree(stray_shm, ignore_errors=True)


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

def test_meters_skip_the_merged_view(iso_env):
    """The usage meter and the release footprint walk a session's areas —
    and under a LIVE session, ``scratch`` holds the merged views (the
    ``workdir``/``agent-dir`` overlays and, in ``workdir``, the inbox/outbox
    bind sources). Those views expose the shared toolkit lower — image
    content no release ever frees — and the bind sources again. Walking a
    mount would bill the toolkit to every session's held bytes and count
    each bind's files twice, so both meters skip mount points: a session's
    number is its own bytes (the uppers, the bind sources), each once.

    This is the production shape the unit tests cannot model: only here is
    ``workdir`` an actual mount, not a plain directory.
    """
    sp, ops, _ = _mount_session(iso_env, "s-meter")
    try:
        state = iso_env["state"]
        # the session's real bytes: one file in each bind source, one in
        # the overlay upper; the toolkit lower already holds 11 shared bytes
        out_src = Path(state.outbox_for("s-meter"))
        out_src.mkdir(parents=True, exist_ok=True)
        (out_src / "report.md").write_bytes(b"x" * 100)
        in_src = Path(state.inbox_for("s-meter"))
        in_src.mkdir(parents=True, exist_ok=True)
        (in_src / "upload.bin").write_bytes(b"y" * 200)
        (Path(sp.upper) / "scratch.txt").write_bytes(b"z" * 50)
        # the merged view really does expose all three of those PLUS the
        # toolkit — the precondition the skip exists for
        assert os.path.isfile(f"{sp.workdir}/toolkit-file.txt")
        assert os.path.isfile(f"{sp.workdir}/outbox/report.md")

        report = area_usage(str(state.root))
        s = report["by_session"]["s-meter"]
        assert s["outbox"] == 100, f"bind source counted once: {s}"
        assert s["inbox"] == 200, s
        assert s["scratch"] == 50, (
            f"the merged view leaked into the meter (toolkit lower and/or "
            f"bind sources counted): {s}"
        )

        # the footprint is walked while the session is STILL mounted — the
        # disk-pressure order's shape — and must say exactly what a release
        # frees: the three files, nothing from the image
        files, total = area_footprint(
            (
                state.inbox_for("s-meter"),
                state.outbox_for("s-meter"),
                state.staging_for("s-meter"),
                state.scratch_for("s-meter"),
                state.sessions_for("s-meter"),
            )
        )
        assert (files, total) == (3, 350), (files, total)
    finally:
        M.unmount_all(ops)
