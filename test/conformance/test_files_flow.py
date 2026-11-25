"""Conformance: the whole file life on real mounts.

Unit tests can prove the delivery layer's decisions; only a real slot uid, a
real inbox/outbox bind, and a real agent process can prove the system:

- an upload reaches the agent through its mount at the blob inode, root-owned,
  in a directory the agent does not own — so it can read it and nothing else;
- what the agent writes into ``outbox/`` IS the app's listing (the bind is the
  same directory, not a copy the app polls);
- a keep is the store's inode, never the slot's — so an agent that truncates
  the artifact afterwards cannot touch bytes a human made permanent;
- attach hands the target inbox entry the same inode (no second copy), an
  archive streams exactly what the listing shows, and a rename moves in place;
- release sweeps the session's areas and reports what it discarded, while the
  kept file outlives it on the volume.

Runs as root, so it can also ask what the slot's own uid can and cannot do,
in a forked child that has dropped to it.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import os
import pwd
import stat
import zipfile
from pathlib import Path

from app.delivery.archive import stream_zip
from app.delivery.service import Delivery
from app.delivery.upload import Staged

BODY = b"the human's spreadsheet\n" * 64
NOTE = b"made\n"  # what the scripted bash command writes


async def test_upload_artifact_keep_attach_release(harness):
    live = await harness.start_session()
    sid = live.session_id
    delivery = Delivery(harness.root)
    sf = delivery.files(harness.state, sid, live.slot)

    # ---- an upload lands once, and reaches the agent as a read-only link ----
    # the route's own three steps (create → pump → complete), not a wrapper
    async def stream():
        yield BODY

    staged = delivery.staged(sid)
    upl, _offset = await asyncio.to_thread(staged.create, "budget.csv")
    await staged.append_stream(upl, 0, stream())
    landed = await delivery.complete_staged(staged, upl, sf)
    assert landed.name == "budget.csv" and landed.size == len(BODY)
    in_mount = Path(live.sp.workdir) / "inbox" / landed.name
    st = os.stat(in_mount)
    assert st.st_size == len(BODY)
    assert os.stat(Path(harness.state.blob_for(landed.sha256))).st_ino == st.st_ino, (
        "the inbox entry is a link to the store, not a second copy"
    )
    assert st.st_uid == 0 and not st.st_mode & stat.S_IWOTH, "not the agent's to modify"
    inbox_dir = in_mount.parent
    assert os.stat(inbox_dir).st_uid == 0, "a slot-owned inbox would hand back unlink"
    # asked of the agent's own uid, not inferred from the mode bits
    assert not _can_as(live.slot.uid, in_mount, os.W_OK), "the upload must be read-only"
    assert not _can_as(live.slot.uid, inbox_dir, os.W_OK), "the inbox is read-only"

    # ---- a folder upload lands as the tree the human sent it in ----
    # the pick's relative path is the upload name; the agent sees the folder
    # through the same bind, root-owned like the inbox itself (its directory
    # bits are what keep it un-creatable, like the inbox root's)
    upl2, _offset = await asyncio.to_thread(staged.create, "binder/a.csv")
    await staged.append_stream(upl2, 0, stream())
    folder = await delivery.complete_staged(staged, upl2, sf)
    assert folder.name == "binder/a.csv"
    in_mount2 = Path(live.sp.workdir) / "inbox" / "binder" / "a.csv"
    assert os.stat(in_mount2).st_ino == os.stat(
        harness.state.blob_for(folder.sha256)
    ).st_ino, "the nested entry is the store's link too"
    binder = in_mount2.parent
    assert os.stat(binder).st_uid == 0
    assert not _can_as(live.slot.uid, binder, os.W_OK), "a slot-owned folder is creatable"

    # ---- what the agent writes into outbox/ IS the app's listing ----
    harness.router.set_script(
        {"tools": [{"name": "bash", "arguments": {"command": "printf '%s\\n' made > outbox/note.md"}}]}
    )
    await harness.prompt_and_settle(live, "write a note into the outbox")
    src = Path(harness.state.outbox_for(sid)) / "note.md"
    assert src.read_bytes() == NOTE, "the outbox bind must be the same directory"
    assert [r["path"] for r in sf.list_outbox()] == ["note.md"]

    # ---- keep: the store's inode, never the slot's ----
    kept = delivery.keep(sf, str(src), name="the-note.md")
    shelf = Path(delivery.keep_path(kept["path"]))
    kept_st = os.stat(shelf)
    assert kept_st.st_ino != os.stat(src).st_ino, "a keep must not point at the agent's inode"
    assert kept_st.st_uid == 0
    assert os.stat(harness.state.blob_for(kept["sha256"])).st_ino == kept_st.st_ino
    assert shelf.read_bytes() == NOTE

    # the agent rewrites the artifact in place (as its own uid); the kept bytes
    # are untouched, because they were never the same inode
    _run_as(live.slot.uid, _rewrite, str(src))
    assert os.stat(src).st_size != kept_st.st_size, "the rewrite must have happened"
    assert shelf.read_bytes() == NOTE, "a keep is not rewritable by a running agent"

    # ---- attach: the same inode again, no second copy ----
    attached = delivery.attach(sf, str(shelf), sha256=kept["sha256"], name="note-copy.md")
    assert os.stat(Path(sf.inbox) / attached.name).st_ino == kept_st.st_ino

    # ---- archive + rename, through the same gates ----
    name, entries = delivery.archive_entries(sf, "outbox")
    assert name == "outbox.zip" and [a for a, _ in entries] == ["note.md"]
    with zipfile.ZipFile(io.BytesIO(b"".join(stream_zip(entries)))) as zf:
        assert zf.testzip() is None
        assert zf.read("note.md") == NOTE
    moved = delivery.rename(sf, "outbox/note.md", "outbox/v2/note.md")
    assert moved["to"] == "v2/note.md"
    assert [r["path"] for r in sf.list_outbox()] == ["v2/note.md"]

    # ---- a staged (resumable) upload, delivered through the store ----
    staged = Staged(harness.root, sid)
    uid, off = staged.create("big.bin")
    off = staged.append(uid, off, [BODY[:1000]])
    off = staged.append(uid, off, [BODY[1000:]])
    part, sha, size = staged.verify(uid)
    assert sha == hashlib.sha256(BODY).hexdigest() and size == len(BODY)
    big = delivery.deliver_staged(part, "big.bin", sha, sf)
    assert os.stat(Path(live.sp.workdir) / "inbox" / big.name).st_size == len(BODY)

    # ---- release sweeps everything but the keep, and says what it cost ----
    outcome = await harness.release(live)
    assert outcome.released, outcome.errors
    # 4 uploads + the artifact + the staged upload's leftover meta.json
    # (the part itself moved into the blob store at delivery)
    assert outcome.discarded[0] >= 5, outcome.discarded
    assert outcome.discarded[1] > 0
    for gone in (
        harness.state.inbox_for(sid),
        harness.state.outbox_for(sid),
        harness.state.staging_for(sid),
        harness.state.scratch_for(sid),
    ):
        assert not Path(gone).exists(), gone
    # the kept file is byte-identical, listed, and the only survivor
    assert Path(delivery.keep_path(kept["path"])).read_bytes() == NOTE
    assert [r["path"] for r in delivery.list_keep()] == [kept["path"]]
    assert delivery.delete_keep(kept["path"]) == len(NOTE)
    assert delivery.list_keep() == []


# ---------------------------------------------------------------------- #
# asking as the slot uid
# ---------------------------------------------------------------------- #


def _rewrite(path: str) -> None:
    with open(path, "wb") as f:
        f.write(b"truncated and rewritten by the agent" * 200)


def _can_as(uid: int, path: str, mode: int) -> bool:
    """``os.access`` in a child that has dropped to the slot's uid — the
    honest answer to "can the agent do this", where the parent (root) would
    answer yes to everything."""
    return _fork_as(uid, lambda: bool(os.access(path, mode)))


def _run_as(uid: int, fn, *args) -> None:
    _fork_as(uid, lambda: fn(*args))


def _fork_as(uid: int, fn) -> bool:
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.setgid(pwd.getpwuid(uid).pw_gid)
            os.setuid(uid)
            ok = fn()
            os.write(write_fd, b"1" if ok else b"0")
        except Exception:
            os.write(write_fd, b"e")
        finally:
            os._exit(0)
    os.close(write_fd)
    out = os.read(read_fd, 1)
    os.close(read_fd)
    os.waitpid(pid, 0)
    assert out != b"e", f"child could not run {fn} as uid {uid}"
    return out == b"1"
