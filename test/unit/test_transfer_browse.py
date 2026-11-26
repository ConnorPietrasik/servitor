"""Unit: staged (resumable) uploads, rename/move, and archive download.

One shape in common: the app must finish a transfer it started, must not move
a file somewhere it would no longer be readable, and must hand back exactly
what the human can see — never more.

Pinned:
- chunks land AT the offset the client names; a mismatch is 409 carrying the
  server's size, and resuming from that number finishes the upload; the sync
  append holds its part lock across (check, write) — two racing sync appends
  yield one winner, one conflict, and no interleaved part;
- a staged upload survives an app restart (a fresh Delivery over the same
  state volume lists it and finishes it) and dies only on explicit cancel or
  its TTL; a chunk whose part vanished under it is a 404 like any unknown id,
  never a 500 from the catch-all;
- the sha recorded at ``complete`` is the hash of the bytes actually on
  disk, whatever the client sent or a corrupted chunk did on the way in;
- rename resolves both sides through the download's gate: contained, no
  symlink, destination must not exist; an upload's manifest entry moves with
  the file (a divergent entry would make the upload unreadable); ``to`` may
  omit the area prefix (the URL says the area) and lands in the source's
  area, while an absolute form must name that area;
- an archive is built from the gated listing and streamed, with no copy of
  the tree written to the volume first.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import threading
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from _filesapp import make_client, slot_identity, upload

from app.delivery.store import FileNotDelivered, SessionFiles
from app.delivery.upload import STAGED_TTL_SEC, Staged, StagedError
from app.iso.layout import state_paths

CHUNK = 4096


def _stage(client, name="movie.bin", sid="s1") -> str:
    r = client.post(f"/api/v1/sessions/{sid}/staged?name={name}")
    assert r.status_code == 201, r.text
    return r.json()["upload_id"]


def _put(client, uid, offset, body, sid="s1"):
    return client.put(
        f"/api/v1/sessions/{sid}/staged/{uid}/chunk?offset={offset}",
        content=body,
        headers={"content-type": "application/octet-stream"},
    )


def _artifact(root, rel, body=b"artifact", sid="s1") -> Path:
    p = Path(state_paths(str(root)).outbox_for(sid)) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body)
    return p


def _zip(response) -> zipfile.ZipFile:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    return zipfile.ZipFile(io.BytesIO(response.content))


def _zip_of(client, path) -> zipfile.ZipFile:
    return _zip(client.get(f"/api/v1/sessions/s1/files/archive?path={path}"))


# ---------------------------------------------------------------------- #
# staged uploads: offsets, resume, complete
# ---------------------------------------------------------------------- #


def test_a_staged_upload_lands_chunk_by_chunk(tmp_path):
    client, _st, root = make_client(tmp_path)
    body = bytes(range(256)) * 40
    uid = _stage(client)
    off = 0
    for i in range(0, len(body), CHUNK):
        r = _put(client, uid, off, body[i : i + CHUNK])
        assert r.status_code == 200, r.text
        off = r.json()["offset"]
    assert off == len(body)
    r = client.post(f"/api/v1/sessions/s1/staged/{uid}/complete")
    assert r.status_code == 201, r.text
    got = r.json()
    assert got["sha256"] == hashlib.sha256(body).hexdigest()
    assert got["size"] == len(body) and got["mime"]  # sniffed, not assumed
    assert [u["name"] for u in client.get("/api/v1/sessions/s1/files").json()["uploads"]] == [
        "movie.bin"
    ]
    assert client.get("/api/v1/sessions/s1/files/uploads/movie.bin").content == body
    # delivery consumed the staging area: nothing is left to sweep, and the
    # bytes are the store's (one copy, linked in)
    assert client.get("/api/v1/sessions/s1/staged").json()["staged"] == []
    assert list(Path(root).glob("staging/*")) == []
    assert list(Path(root).glob("blobs/*/*"))


def test_a_body_that_dies_mid_stream_leaves_what_landed_and_resumes(tmp_path):
    """The socket can die mid-body: a dropped connection is not an abort.
    What made it to the part stays on disk, fsynced, and the offset protocol
    resumes from the server's size — never a re-upload, never a silent gap."""
    body = os.urandom(300_000)
    staged = Staged(tmp_path, "s1")
    uid, off = staged.create("mid.bin")

    async def dying_body():
        for chunk in (body[:40_000], body[40_000:80_000], body[80_000:100_000]):
            yield chunk
        raise ConnectionError("the radio gave up")

    async def finish():
        await staged.append_stream(uid, 0, dying_body())

    with pytest.raises(ConnectionError):
        asyncio.run(finish())
    # exactly what was yielded is on disk and durable (the resume trusts it)
    assert staged.size(uid) == 100_000

    async def rest():
        yield body[100_000 :]

    async def resume():
        return await staged.append_stream(uid, 100_000, rest())

    assert asyncio.run(resume()) == len(body)
    part, sha, size = staged.verify(uid)
    assert size == len(body) and sha == hashlib.sha256(body).hexdigest()


def test_a_dead_writer_fails_fast_instead_of_stalling_the_put(tmp_path, monkeypatch):
    """The writer thread can die while the client is still streaming (a
    cancel/TTL sweep that takes the part, a disk that refuses the write).
    The pump must not let the reader block on a queue the dead worker will
    never drain: with a body deeper than the queue, a blocking put stalled
    the request forever and leaked a thread the process could not exit
    with. The pump raises the WRITER's error instead.

    The body is 40 x 1 MB on purpose — deeper than the 32-item queue, which
    is the shape that fills it; a 1 MB-per-request chunk (the UI's protocol)
    never reaches the wall and so never exercised this path."""
    import builtins

    import app.delivery.upload as up

    staged = Staged(tmp_path, "s1")
    uid, off = staged.create("big.bin")
    real_open = builtins.open

    def dead_open(path, *a, **k):
        if str(path).endswith("/part"):
            raise FileNotFoundError("the part raced away")
        return real_open(path, *a, **k)

    monkeypatch.setattr(up, "open", dead_open, raising=False)

    async def body():
        for _ in range(40):
            yield b"x" * (1024 * 1024)

    async def go():
        return await staged.append_stream(uid, off, body())

    with pytest.raises(FileNotFoundError):
        asyncio.run(asyncio.wait_for(go(), timeout=10))


def test_complete_holds_the_lock_that_chunks_wait_on(tmp_path):
    """complete hashes the part and then MOVES it; a chunk landing between
    the two would store the part under a sha of different bytes. The route
    therefore runs the whole verify → deliver → drop under the part's append
    lock — pinned here as the property it relies on: while that lock is
    held, an append to the same part cannot land (a distinct lock would let
    it), and it lands exactly once after the hold is released."""
    staged = Staged(tmp_path, "s1")
    uid, off = staged.create("lock.bin")

    async def run():
        with staged.part_lock(uid) as lock:
            async with lock:
                async def body():
                    yield b"0123456789"

                task = asyncio.create_task(staged.append_stream(uid, 0, body()))
                await asyncio.sleep(0.1)
                assert not task.done(), "the chunk must wait for the lock"
                assert staged.size(uid) == 0, "no byte may land while held"
        return await asyncio.wait_for(task, timeout=5)

    assert asyncio.run(run()) == 10
    assert staged.size(uid) == 10


def test_complete_waits_for_a_chunk_that_is_still_writing(tmp_path, monkeypatch):
    """The route-level half of the part-lock contract: complete must not
    hash and move a part while a chunk is still landing — a torn read would
    store bytes under the sha of different bytes. The writer thread is held
    MID-WRITE (first megabyte down, lock still held); a complete that arrives
    then must be blocked on the lock — and then still deliver the whole
    file's sha, which without the lock it would hash at 1 MB of 16.

    The two requests run on ONE loop as raw ASGI calls: production serves
    every request from the app's single loop (what the part lock relies on),
    and the TestClient gives each request its own loop, where a lock held by
    one request's loop can never wake another's waiter."""
    import app.delivery.upload as up
    from app.api.app import build_app

    body = os.urandom(16 * 1024 * 1024)
    client, app_state, _root = make_client(tmp_path)
    uid = client.post("/api/v1/sessions/s1/staged?name=movie.bin").json()["upload_id"]
    app = build_app(
        app_state, SimpleNamespace(browser_origin=None, edge_key="k", heartbeat_secs=15)
    )

    partial_written = threading.Event()
    release = threading.Event()
    real_pump = up._pump_to_part

    def stuck_pump(part, offset, q, result, done):
        # the first megabyte lands, then the writer stops mid-file — the
        # part lock append_stream holds stays held until it returns
        try:
            with open(part, "rb+") as f:
                f.write(body[: 1024 * 1024])
                f.flush()
            partial_written.set()
            release.wait(timeout=15)
            real_pump(part, offset, q, result, done)
        except BaseException as exc:
            result.append(exc)
            done.set()

    monkeypatch.setattr(up, "_pump_to_part", stuck_pump)

    async def drive():
        put_task = asyncio.ensure_future(
            _asgi(app, "PUT", f"/api/v1/sessions/s1/staged/{uid}/chunk?offset=0", body)
        )
        try:
            assert await asyncio.to_thread(partial_written.wait, 10), (
                "the writer never reached mid-write"
            )
            complete_task = asyncio.ensure_future(
                _asgi(app, "POST", f"/api/v1/sessions/s1/staged/{uid}/complete")
            )
            await asyncio.sleep(0.3)  # the writer is still stuck: complete must wait on the lock
            assert not complete_task.done(), "complete ran while the chunk was still landing"
            put_res, complete_res = await asyncio.wait_for(
                asyncio.gather(put_task, complete_task), timeout=15
            )
        finally:
            release.set()
        return put_res, complete_res

    (put_status, _put_body), (complete_status, complete_body) = asyncio.run(drive())
    assert put_status == 200, put_status
    assert complete_status == 201, complete_body[:200]
    got = json.loads(complete_body)
    assert got["sha256"] == hashlib.sha256(body).hexdigest()
    assert got["size"] == len(body)


async def _asgi(app, method, path, body=b"", headers=()):
    """One raw ASGI http call on the caller's loop. Returns
    ``(status, body)`` — no portal, no TestClient: the loop is the test's,
    so concurrent calls on it are concurrent on ONE loop, as in production."""
    route, _, query = path.partition("?")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": route,
        "raw_path": route.encode(),
        "query_string": query.encode(),
        "headers": [
            (b"x-servitor-edge-key", b"k"),
            *[(k.lower().encode(), v.encode()) for k, v in headers],
        ],
        "client": ("127.0.0.1", 0),
        "server": ("test", 0),
        "root_path": "",
    }
    sent: list[dict] = []
    body_sent = False

    async def receive():
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.sleep(3600)  # the body is exhausted; a disconnect is a lie

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, payload


def test_a_slow_disk_does_not_stall_the_loop(tmp_path, monkeypatch):
    """The part's fsync is the call that can take seconds on a loaded disk.
    It must run off the loop: while it blocks, the loop that serves every
    run stays free — an upload slows only itself.

    Pinned with a poisoned fsync that blocks its caller for 1.5 s: an
    unrelated loop task must run at ~0.3 s, long before either slow sync
    (the part's, in the writer thread; the meta's, in a to_thread) returns.
    """
    from app.delivery.upload import Staged

    real_fsync = os.fsync

    def slow_fsync(fd):
        time.sleep(1.5)  # block the calling thread, not the loop
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", slow_fsync)
    staged = Staged(tmp_path, "s1")
    uid, off = staged.create("slow.bin")  # its meta fsync is slow too: pre-t0

    async def run():
        other_ran_at = {}

        async def other():
            await asyncio.sleep(0.3)
            other_ran_at["t"] = time.monotonic()

        async def body():
            yield os.urandom(300_000)

        t0 = time.monotonic()
        other_task = asyncio.create_task(other())
        await asyncio.wait_for(staged.append_stream(uid, off, body()), timeout=8)
        await other_task
        return other_ran_at["t"] - t0

    assert asyncio.run(run()) < 1.0


def test_a_fresh_staged_upload_reports_zero_to_a_would_be_resume(tmp_path):
    """A create with no chunk landed yet: the resume list still carries the
    server's on-disk size (0). A resume offered from that row must start at
    0 (the offset protocol self-heals the rest), never at an unknown offset
    — which would skip the pump and deliver a 0-byte file under the real
    name."""
    client, _st, _root = make_client(tmp_path)
    uid = _stage(client, name="big.bin")
    rows = client.get("/api/v1/sessions/s1/staged").json()["staged"]
    assert len(rows) == 1
    assert rows[0]["upload_id"] == uid and rows[0]["name"] == "big.bin"
    assert rows[0]["size"] == 0
    _put(client, uid, 0, b"hello")
    rows = client.get("/api/v1/sessions/s1/staged").json()["staged"]
    assert rows[0]["size"] == 5


def test_an_offset_mismatch_answers_with_the_number_to_resume_from(tmp_path):
    """A phone that lost a chunk neither re-sends the file nor double-writes
    the bytes that already landed."""
    client, _st, _root = make_client(tmp_path)
    uid = _stage(client)
    assert _put(client, uid, 0, b"AAAA").json() == {"offset": 4}
    # the same chunk again (a retry whose response was lost): refused, with
    # the server's size so the client knows where it actually is
    r = _put(client, uid, 0, b"AAAA")
    assert r.status_code == 409
    assert r.json() == {"offset": 4}
    # and a wild guess is refused too — nothing is written at the wrong place
    assert _put(client, uid, 99, b"ZZZZ").status_code == 409
    assert _put(client, uid, 4, b"BBBB").json() == {"offset": 8}
    assert client.post(f"/api/v1/sessions/s1/staged/{uid}/complete").json()["size"] == 8
    assert client.get("/api/v1/sessions/s1/files/uploads/movie.bin").content == b"AAAABBBB"


def test_concurrent_sync_appends_serialize_on_the_part_lock(tmp_path):
    """Two sync appends at the same offset race: the part lock makes it
    exactly one winner. The loser's check runs after the winner's write, so
    it sees the moved size and conflicts; the part holds one whole chunk —
    without the lock, both checks can pass at size 0 and the bytes interleave."""
    from app.delivery.upload import OffsetMismatch

    staged = Staged(tmp_path, "s1")
    uid, _off = staged.create("race.bin")
    a, b = b"A" * 64_000, b"B" * 64_000
    outcomes: list[tuple] = []
    barrier = threading.Barrier(2)

    def do(body: bytes) -> None:
        barrier.wait()
        try:
            outcomes.append(("ok", staged.append(uid, 0, [body])))
        except OffsetMismatch as e:
            outcomes.append(("conflict", e.size))

    t1 = threading.Thread(target=do, args=(a,))
    t2 = threading.Thread(target=do, args=(b,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert sorted(k for k, _ in outcomes) == ["conflict", "ok"], outcomes
    assert staged.size(uid) == len(a)  # one chunk landed, not both
    body = (staged.root / uid / "part").read_bytes()
    assert body in (a, b)  # no interleave


def test_a_chunk_for_a_part_that_just_vanished_is_a_404_not_a_500(tmp_path):
    """The part can be deleted under an in-flight chunk request (a concurrent
    complete or cancel takes it): the narrowest race — the id still
    resolves, the part is already gone. The route must answer the same 404
    it gives for an unknown id; a FileNotFoundError leaking to the catch-all
    would be a 500 plus log noise for an ordinary race."""
    client, _st, root = make_client(tmp_path)
    uid = _stage(client)
    (Path(root) / "staging" / "s1" / uid / "part").unlink()
    r = _put(client, uid, 0, b"too late")
    assert r.status_code == 404, r.text


def test_the_resume_list_races_its_own_cleanup_as_200_not_a_500(tmp_path, monkeypatch):
    """The resume list's listdir and its per-entry size read are not atomic:
    a complete, cancel, or TTL sweep that lands in the window takes the
    directory away after the entry was listed. The route must answer 200
    with the row GONE — a vanished upload is not a resume to offer (it can
    never succeed) — not a StagedError leaking to the catch-all: the list is
    what a page reload offers the human, and a 500 there reads as "the
    server lost my upload"."""
    client, _st, _root = make_client(tmp_path)
    uid = _stage(client)
    real_dir = Staged._dir

    def _dir_vanished(self, upload_id):
        if upload_id == uid:
            raise StagedError(f"no such staged upload: {upload_id}")
        return real_dir(self, upload_id)

    monkeypatch.setattr(Staged, "_dir", _dir_vanished)
    r = client.get("/api/v1/sessions/s1/staged")
    assert r.status_code == 200, r.text
    assert r.json()["staged"] == []


def test_a_client_disconnect_mid_body_is_a_499_not_a_500(tmp_path, monkeypatch):
    """Starlette raises ClientDisconnect when the client closes a body it
    declared: that is the radio dropping, the case this subsystem exists
    for. The routes must name it (499, one info line) instead of letting it
    fall into the unhandled-error handler — a 500 with a traceback for a
    dropped phone call. The one-shot's 499 must hand back the resume
    handle: a one-shot client never learned the upload_id, and without it
    the staged bytes are unfinishable until the TTL."""
    from starlette.requests import ClientDisconnect

    client, app_state, _root = make_client(tmp_path)

    async def _disc(self, upload_id, offset, aiter):
        raise ClientDisconnect()

    monkeypatch.setattr(Staged, "append_stream", _disc)

    r = client.post("/api/v1/sessions/s1/files/upload?name=drops.txt", content=b"half")
    assert r.status_code == 499, r.text
    body = r.json()
    assert body["upload_id"]  # the handle a resume needs
    assert body["offset"] == 0  # the drop was before any byte landed

    r = _put(client, "0" * 32, 0, b"half")
    assert r.status_code == 499, r.text
    body = r.json()
    # the chunk's 499 carries the same resume handle as the one-shot's:
    # the offset is what landed (nothing, here), and the PUT resumes from it
    assert body["upload_id"] == "0" * 32
    assert body["offset"] == 0
    assert body["resume"].endswith("chunk?offset=0")



def test_a_staged_upload_survives_an_app_restart(tmp_path):
    """A resumable upload interrupted by a restart is not garbage: a fresh
    process over the same state volume lists it and finishes it.

    ``make_client(tmp_path)`` twice is exactly that — the same ``state/`` root,
    a new Delivery, a new app.
    """
    client, _st, _root = make_client(tmp_path)
    uid = _stage(client)
    _put(client, uid, 0, b"first half ")

    again, st2, _ = make_client(tmp_path)
    assert st2 is not _st
    # boot reconciliation sweeps interrupted single-shot uploads, not these
    assert st2.delivery.gc_boot()["staging_swept"] == []
    rows = again.get("/api/v1/sessions/s1/staged").json()["staged"]
    assert [r["upload_id"] for r in rows] == [uid]
    assert rows[0]["name"] == "movie.bin" and rows[0]["size"] == len(b"first half ")
    assert _put(again, uid, len(b"first half "), b"second half").json() == {"offset": 22}
    got = again.post(f"/api/v1/sessions/s1/staged/{uid}/complete").json()
    assert got["sha256"] == hashlib.sha256(b"first half second half").hexdigest()
    assert again.get("/api/v1/sessions/s1/files/uploads/movie.bin").content == (
        b"first half second half"
    )


def test_cancel_is_the_only_early_death_and_a_socket_drop_is_not_one(tmp_path):
    client, _st, _root = make_client(tmp_path)
    uid = _stage(client, name="cancelme.bin")
    _put(client, uid, 0, b"some bytes")
    # nothing but an explicit cancel (or the TTL) removes it: no request here
    # dropped a connection, and nothing disappeared
    assert client.get("/api/v1/sessions/s1/staged").json()["staged"]
    assert client.delete(f"/api/v1/sessions/s1/staged/{uid}").json() == {"cancelled": uid}
    assert client.get("/api/v1/sessions/s1/staged").json()["staged"] == []
    assert _put(client, uid, 10, b"x").status_code == 404
    assert client.post(f"/api/v1/sessions/s1/staged/{uid}/complete").status_code == 404
    assert client.delete(f"/api/v1/sessions/s1/staged/{uid}").status_code == 404


def test_complete_hashes_what_landed_not_what_was_claimed(tmp_path):
    """A chunk that arrived corrupted must not deliver a file whose recorded
    sha disagrees with its bytes: the hash is taken at complete, over the
    whole file, and that hash is what the manifest and the blob store use."""
    client, _st, root = make_client(tmp_path)
    uid = _stage(client, name="tampered.bin")
    _put(client, uid, 0, b"original bytes")
    part = Path(root) / "staging" / "s1" / uid / "part"
    with open(part, "r+b") as f:  # corruption on the volume itself
        f.seek(0)
        f.write(b"CORRUPTED by")
    on_disk = part.read_bytes()
    got = client.post(f"/api/v1/sessions/s1/staged/{uid}/complete").json()
    assert got["sha256"] == hashlib.sha256(on_disk).hexdigest()
    assert got["size"] == len(on_disk)
    assert client.get("/api/v1/sessions/s1/files/uploads/tampered.bin").content == on_disk


def test_staged_ttl_sweeps_only_what_nobody_resumed(tmp_path):
    client, st, root = make_client(tmp_path)
    _stage(client, name="fresh.bin")
    stale = _stage(client, name="stale.bin")
    meta = Path(root) / "staging" / "s1" / stale / "meta.json"
    m = json.loads(meta.read_text())
    m["updated"] = time.time() - STAGED_TTL_SEC - 10
    meta.write_text(json.dumps(m))
    assert st.delivery.gc_staged() == [stale]
    names = [r["name"] for r in client.get("/api/v1/sessions/s1/staged").json()["staged"]]
    assert names == ["fresh.bin"]  # an upload in progress is never swept


def test_an_upload_id_is_a_name_not_a_path(tmp_path):
    from app.delivery.upload import StagedError

    client, _st, root = make_client(tmp_path)
    for bogus in ("..", ".", "../../s2", "../..", ".hidden", "", "a/b", "a\\b", "x" * 65):
        with pytest.raises(StagedError, match="not a session id"):
            Staged(root, bogus)
    # an id of the wrong shape is a 404, whatever it was trying to be
    for bad in ("x" * 40, "DEADBEEF" * 4, "0" * 31):
        assert client.put(f"/api/v1/sessions/s1/staged/{bad}/chunk?offset=0", content=b"x").status_code == 404
        assert client.delete(f"/api/v1/sessions/s1/staged/{bad}").status_code == 404
    # a real-shaped id that does not exist is a 404, not a 500
    assert client.delete(f"/api/v1/sessions/s1/staged/{'0' * 32}").status_code == 404
    # and one session's upload is not another session's to touch
    uid = _stage(client)
    assert client.delete(f"/api/v1/sessions/s2/staged/{uid}").status_code == 404
    assert _put(client, uid, 0, b"still mine", sid="s2").status_code == 404


# ---------------------------------------------------------------------- #
# rename / move
# ---------------------------------------------------------------------- #


def test_rename_an_artifact_updates_the_listing(tmp_path):
    client, _st, root = make_client(tmp_path)
    _artifact(root, "report.md")
    r = client.patch("/api/v1/sessions/s1/files/outbox/report.md", json={"to": "outbox/report-v2.md"})
    assert r.status_code == 200, r.text
    assert r.json()["to"] == "report-v2.md"
    assert [f["path"] for f in client.get("/api/v1/sessions/s1/files").json()["outbox"]] == [
        "report-v2.md"
    ]
    assert client.get("/api/v1/sessions/s1/files/outbox/report-v2.md").content == b"artifact"
    assert client.get("/api/v1/sessions/s1/files/outbox/report.md").status_code == 404


def test_moving_an_artifact_into_a_folder_creates_the_folder(tmp_path):
    client, _st, root = make_client(tmp_path)
    _artifact(root, "one.txt")
    _artifact(root, "two.txt")
    for name in ("one.txt", "two.txt"):
        r = client.patch(f"/api/v1/sessions/s1/files/outbox/{name}", json={"to": f"outbox/v2/{name}"})
        assert r.status_code == 200, r.text
    paths = sorted(f["path"] for f in client.get("/api/v1/sessions/s1/files").json()["outbox"])
    assert paths == ["v2/one.txt", "v2/two.txt"]


def test_folders_a_rename_creates_are_handed_to_the_slot(tmp_path, monkeypatch):
    """The app runs as root: a folder a human rename creates must be chowned
    to the slot, or the agent's next write into it is an EACCES wall the
    human's own action erected (the sandbox suite pins the real-uid half)."""
    calls: list[str] = []
    monkeypatch.setattr(
        "app.delivery.store.os.chown",
        lambda path, uid, gid: calls.append(str(path)),
    )
    client, _st, root = make_client(
        tmp_path, slot=os.getuid(), slot_ident=slot_identity(os.getuid()), monkeypatch=monkeypatch
    )
    _artifact(root, "one.txt")
    r = client.patch(
        "/api/v1/sessions/s1/files/outbox/one.txt", json={"to": "outbox/a/b/one.txt"}
    )
    assert r.status_code == 200, r.text
    outbox = Path(state_paths(str(root)).outbox_for("s1"))
    assert str(outbox / "a") in calls, "a created folder must be handed to the slot"
    assert str(outbox / "a/b") in calls
    assert str(outbox) not in calls, "an existing area is not re-chowned"


def test_rename_refuses_to_replace_a_file_the_human_can_see(tmp_path):
    client, _st, root = make_client(tmp_path)
    keepme = _artifact(root, "keepme.md", b"precious")
    _artifact(root, "typo.md", b"scratch")
    r = client.patch("/api/v1/sessions/s1/files/outbox/typo.md", json={"to": "outbox/keepme.md"})
    assert r.status_code == 404
    assert keepme.read_bytes() == b"precious"
    assert (Path(state_paths(str(root)).outbox_for("s1")) / "typo.md").exists()


def test_a_rename_to_the_same_path_is_a_no_op_not_a_404(tmp_path):
    """The destination-exists rule must not fire on the source itself: the
    UI's rename box pre-filled with the current name is the common case for
    'I only wanted to confirm', and it must not read as an error (the
    upload rename already treated itself as a no-op — the outbox rename had
    drifted the other way)."""
    client, _st, root = make_client(tmp_path)
    f = _artifact(root, "v2/report.md", b"# r\n")
    r = client.patch(
        "/api/v1/sessions/s1/files/outbox/v2/report.md", json={"to": "v2/report.md"}
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"from": "v2/report.md", "to": "v2/report.md", "size": len(b"# r\n")}
    assert f.read_bytes() == b"# r\n"  # the file is exactly where it was
    # and the bare form, which the UI sends for a flat file
    _artifact(root, "loose.md", b"loose")
    r = client.patch("/api/v1/sessions/s1/files/outbox/loose.md", json={"to": "loose.md"})
    assert r.status_code == 200, r.text
    assert r.json()["to"] == "loose.md"


def test_rename_cannot_escape_or_go_through_a_symlink(tmp_path):
    client, _st, root = make_client(tmp_path)
    outbox = Path(state_paths(str(root)).outbox_for("s1"))
    _artifact(root, "a.txt")
    outside = Path(tmp_path) / "outside.txt"
    outside.write_bytes(b"not yours")
    for bad in ("../outside", "outbox/../../outside", "/etc/x", "outbox/./x", "outbox", ""):
        assert client.patch(
            "/api/v1/sessions/s1/files/outbox/a.txt", json={"to": bad}
        ).status_code == 404, bad
    assert outside.read_bytes() == b"not yours"
    assert (outbox / "a.txt").exists()
    # a planted symlink in the outbox: the app (root) must not write through it
    (outbox / "link").symlink_to(tmp_path)
    _artifact(root, "b.txt")
    r = client.patch("/api/v1/sessions/s1/files/outbox/b.txt", json={"to": "outbox/link/escaped.txt"})
    assert r.status_code == 404
    assert not (Path(tmp_path) / "escaped.txt").exists()
    assert (outbox / "b.txt").exists()


def test_rename_across_areas_is_not_a_thing(tmp_path):
    """uploads → outbox is a copy, not a rename: delivery and the store own
    those paths, and an in-place path change is neither."""
    client, _st, _root = make_client(tmp_path)
    upload(client, "a.txt", b"hello")
    r = client.patch("/api/v1/sessions/s1/files/uploads/a.txt", json={"to": "outbox/a.txt"})
    assert r.status_code == 404
    assert client.get("/api/v1/sessions/s1/files/uploads/a.txt").content == b"hello"


def test_a_bare_to_stays_in_the_sources_own_area(tmp_path):
    """The URL already says the area: a ``to`` without a prefix (a name, or
    a subpath) lands in the source's own area, and the explicit prefix says
    the same thing — not a different destination."""
    client, _st, root = make_client(tmp_path)
    _artifact(root, "report.md")
    r = client.patch("/api/v1/sessions/s1/files/outbox/report.md", json={"to": "v2/report-v2.md"})
    assert r.status_code == 200, r.text
    assert r.json()["to"] == "v2/report-v2.md"
    r = client.patch("/api/v1/sessions/s1/files/outbox/v2/report-v2.md", json={"to": "final.md"})
    assert r.status_code == 200, r.text
    paths = [f["path"] for f in client.get("/api/v1/sessions/s1/files").json()["outbox"]]
    assert paths == ["final.md"]
    # the explicit forms (with or without a leading slash) name the same place
    r = client.patch("/api/v1/sessions/s1/files/outbox/final.md", json={"to": "outbox/renamed.md"})
    assert r.status_code == 200, r.text
    r = client.patch("/api/v1/sessions/s1/files/outbox/renamed.md", json={"to": "/outbox/plain.md"})
    assert r.status_code == 200, r.text
    paths = [f["path"] for f in client.get("/api/v1/sessions/s1/files").json()["outbox"]]
    assert paths == ["plain.md"]
    # the bare form on the upload side keeps the manifest row with the file
    upload(client, "a.txt", b"hello")
    r = client.patch("/api/v1/sessions/s1/files/uploads/a.txt", json={"to": "b.txt"})
    assert r.status_code == 200, r.text
    # same response shape as the outbox side: paths within the area
    assert r.json() == {"from": "a.txt", "to": "b.txt", "size": len(b"hello")}
    assert [u["name"] for u in client.get("/api/v1/sessions/s1/files").json()["uploads"]] == [
        "b.txt"
    ]


def test_an_absolute_to_must_name_the_sources_area(tmp_path):
    """A leading slash is the explicit form, not decoration: it must name
    the source's own area. ``/etc/x`` is not ``outbox/etc/x``, and the other
    area's prefix is a cross-area move, which is not a rename."""
    client, _st, root = make_client(tmp_path)
    _artifact(root, "a.txt")
    upload(client, "u.txt", b"hello")
    for bad in ("/etc/x", "/uploads/a.txt", "/nonexistent/x"):
        r = client.patch("/api/v1/sessions/s1/files/outbox/a.txt", json={"to": bad})
        assert r.status_code == 404, bad
    r = client.patch("/api/v1/sessions/s1/files/uploads/u.txt", json={"to": "/outbox/u.txt"})
    assert r.status_code == 404
    assert client.get("/api/v1/sessions/s1/files/uploads/u.txt").content == b"hello"
    # the one absolute form that is not a cross: the source's own area
    r = client.patch("/api/v1/sessions/s1/files/outbox/a.txt", json={"to": "/outbox/z.txt"})
    assert r.status_code == 200, r.text
    assert [f["path"] for f in client.get("/api/v1/sessions/s1/files").json()["outbox"]] == [
        "z.txt"
    ]


def test_renaming_an_upload_moves_the_manifest_row_with_the_file(tmp_path):
    """The manifest entry is what proves the inode is the app's blob. If it
    disagreed with the file on disk the upload would stop being readable, so
    the two move together or not at all."""
    client, _st, root = make_client(tmp_path)
    upload(client, ".env", b"SECRET=1")  # lands as "env" (_safe_name)
    r = client.patch("/api/v1/sessions/s1/files/uploads/env", json={"to": "uploads/prod.env"})
    assert r.status_code == 200, r.text
    # `from` is the path the file actually lived under ("env"), within the
    # area — the outbox side's response shape, no prefix on either
    assert r.json() == {"from": "env", "to": "prod.env", "size": len(b"SECRET=1")}
    assert [u["name"] for u in client.get("/api/v1/sessions/s1/files").json()["uploads"]] == [
        "prod.env"
    ]
    assert client.get("/api/v1/sessions/s1/files/uploads/prod.env").content == b"SECRET=1"
    session_dir = Path(state_paths(str(root)).sessions_for("s1"))
    manifests = list(session_dir.glob("*.json"))
    assert len(manifests) == 1, [p.name for p in manifests]
    assert [u["name"] for u in json.loads(manifests[0].read_text())["uploads"]] == ["prod.env"]


def test_rename_applies_the_download_gate(tmp_path):
    """Rename is the download's resolver on both sides: a file the session's
    uid does not own is not renamable, exactly as it is not listable. The
    identity is injected — an unprivileged test tree cannot chown, so the
    slot's uid simply is not the uid these files were created with."""
    sf = SessionFiles(
        session_dir=tmp_path / "sessions" / "s1",
        inbox=tmp_path / "inbox" / "s1",
        outbox=tmp_path / "outbox",
        slot=SimpleNamespace(name="slot9", uid=4242, gid=4242, home="/x"),
    )
    sf.outbox.mkdir(parents=True, exist_ok=True)
    (sf.outbox / "foreign.md").write_bytes(b"someone else's")
    assert sf.list_outbox() == []
    with pytest.raises(FileNotDelivered):
        sf.rename_outbox("foreign.md", "outbox/mine.md")
    assert (sf.outbox / "foreign.md").exists()
    assert not (sf.outbox / "mine.md").exists()


# ---------------------------------------------------------------------- #
# archive
# ---------------------------------------------------------------------- #


def test_a_folder_downloads_as_one_archive_with_its_shape(tmp_path):
    client, _st, root = make_client(tmp_path)
    _artifact(root, "v2/report.md", b"# report\n")
    _artifact(root, "v2/data/rows.csv", b"a,b\n1,2\n")
    _artifact(root, "loose.txt", b"loose")
    z = _zip(client.get("/api/v1/sessions/s1/files/archive?path=outbox/v2"))
    assert sorted(z.namelist()) == ["data/rows.csv", "report.md"]
    assert z.read("report.md") == b"# report\n"
    assert z.testzip() is None
    whole = _zip_of(client, "outbox")
    assert sorted(whole.namelist()) == ["loose.txt", "v2/data/rows.csv", "v2/report.md"]
    named = client.get("/api/v1/sessions/s1/files/archive?path=outbox")
    assert named.headers["content-disposition"] == "attachment; filename=" + chr(34) + "outbox.zip" + chr(34)


def test_one_file_downloads_as_an_archive_too(tmp_path):
    client, _st, root = make_client(tmp_path)
    _artifact(root, "v2/report.md", b"# report\n")
    z = _zip(client.get("/api/v1/sessions/s1/files/archive?path=outbox/v2/report.md"))
    assert z.namelist() == ["report.md"]


def test_the_archive_of_uploads_holds_the_uploads(tmp_path):
    client, _st, _root = make_client(tmp_path)
    upload(client, "a.txt", b"alpha")
    upload(client, "b.txt", b"beta")
    z = _zip(client.get("/api/v1/sessions/s1/files/archive?path=uploads"))
    assert sorted(z.namelist()) == ["a.txt", "b.txt"]
    assert z.read("a.txt") == b"alpha"


def test_one_upload_downloads_as_an_archive_too(tmp_path):
    """The archive's \"one file\" shape covers uploads as well as outbox:
    the entry resolves through the same gated listing a download uses, and
    a name the session does not hold is a 404, not an empty zip —
    including a folder that holds no uploads."""
    client, _st, _root = make_client(tmp_path)
    upload(client, "report.csv", b"a,b\n1,2\n")
    r = client.get("/api/v1/sessions/s1/files/archive?path=uploads/report.csv")
    z = _zip(r)
    assert z.namelist() == ["report.csv"]
    assert z.read("report.csv") == b"a,b\n1,2\n"
    # a raw-dot ask finds its landed row the same way the download's gate does
    upload(client, ".env", b"secret=1\n")
    assert (
        client.get("/api/v1/sessions/s1/files/archive?path=uploads/.env").status_code == 200
    )
    assert (
        client.get("/api/v1/sessions/s1/files/archive?path=uploads/ghost.csv").status_code
        == 404
    )
    # a folder-shaped name holds a folder of uploads: the archive of the
    # folder is the folder's files, relative to it
    upload(client, "photos/a.jpg", b"\xff\xd8\xff\xe0a")
    upload(client, "photos/b.jpg", b"\xff\xd8\xff\xe0b")
    z = _zip(client.get("/api/v1/sessions/s1/files/archive?path=uploads/photos"))
    assert sorted(z.namelist()) == ["a.jpg", "b.jpg"]
    assert z.read("a.jpg") == b"\xff\xd8\xff\xe0a"
    # a folder the listing does not hold is still a 404
    assert (
        client.get("/api/v1/sessions/s1/files/archive?path=uploads/empty/ghost.csv").status_code
        == 404
    )


def test_a_folder_upload_lands_as_a_tree(tmp_path):
    """A folder pick (the UI sends the relative path) lands in the inbox as
    the tree the human organized: same basenames in different folders coexist
    instead of sufficing each other, the manifest records the paths, and the
    listing, download, delete, and rename all address the paths."""
    client, _st, root = make_client(tmp_path)
    inbox = Path(state_paths(str(root)).inbox_for("s1"))
    upload(client, "a/notes.txt", b"one")
    upload(client, "b/notes.txt", b"two")
    # same basename, two folders: both land, no suffix
    assert (inbox / "a" / "notes.txt").read_bytes() == b"one"
    assert (inbox / "b" / "notes.txt").read_bytes() == b"two"
    names = [u["name"] for u in client.get("/api/v1/sessions/s1/files").json()["uploads"]]
    assert sorted(names) == ["a/notes.txt", "b/notes.txt"]
    # downloadable under the path
    assert (
        client.get("/api/v1/sessions/s1/files/uploads/a/notes.txt").content == b"one"
    )
    # a same-basename collision in the SAME folder still suffixes
    upload(client, "a/notes.txt", b"one-replaced?")
    assert (inbox / "a" / "notes.txt").read_bytes() == b"one"
    assert (inbox / "a" / "notes.1.txt").read_bytes() == b"one-replaced?"
    # rename into a folder the app creates
    upload(client, "loose.txt", b"loose")
    r = client.patch("/api/v1/sessions/s1/files/uploads/loose.txt", json={"to": "a/loose.txt"})
    assert r.status_code == 200, r.text
    assert r.json() == {"from": "loose.txt", "to": "a/loose.txt", "size": len(b"loose")}
    assert (inbox / "a" / "loose.txt").read_bytes() == b"loose"
    assert not (inbox / "loose.txt").exists()
    # a rename that would replace is refused, not silent
    r = client.patch("/api/v1/sessions/s1/files/uploads/b/notes.txt", json={"to": "a/notes.txt"})
    assert r.status_code == 404
    # delete the last file of a folder: the empty folder goes with it
    assert client.delete("/api/v1/sessions/s1/files/uploads/b/notes.txt").status_code == 200
    assert not (inbox / "b").exists()
    assert (inbox / "a").is_dir()
    # traversal components are refused at the door (400, before any byte
    # lands), not silently flattened
    assert upload(client, "../../evil.txt", b"nope").status_code == 400
    assert not (root / "evil.txt").exists()
    assert not (tmp_path / "evil.txt").exists()


def test_the_archive_holds_the_listing_not_the_directory(tmp_path):
    """The archive's candidate set is the gated listing, so whatever the
    listing drops never reaches the zip: a symlink out of the area, a name
    that is not a regular file.

    (The ownership half of that gate — a root-owned file the session does not
    own, and a planted hardlink — is proven with real ownership in the
    sandbox-smoke suite.)
    """
    client, _st, root = make_client(tmp_path)
    outbox = Path(state_paths(str(root)).outbox_for("s1"))
    (outbox / "mine.txt").write_bytes(b"mine")
    secret = Path(tmp_path) / "secret.txt"
    secret.write_bytes(b"do not archive")
    (outbox / "link.txt").symlink_to(secret)
    os.mkfifo(outbox / "fifo")
    z = _zip(client.get("/api/v1/sessions/s1/files/archive?path=outbox"))
    assert z.namelist() == ["mine.txt"]
    assert secret.read_bytes() == b"do not archive"


def test_a_multi_select_walks_each_area_once(tmp_path, monkeypatch):
    """N selections are N views of ONE listing, not N walks: an archive of
    a multi-GB tree must not re-descend it once per selected file."""
    client, _st, root = make_client(tmp_path)
    for name in ("a.bin", "b.bin", "c.bin"):
        _artifact(root, name, b"x")
    calls = {"outbox": 0, "uploads": 0}
    real_outbox = SessionFiles.list_outbox
    real_uploads = SessionFiles.list_uploads

    def counting_outbox(self):
        calls["outbox"] += 1
        return real_outbox(self)

    def counting_uploads(self):
        calls["uploads"] += 1
        return real_uploads(self)

    monkeypatch.setattr(SessionFiles, "list_outbox", counting_outbox)
    monkeypatch.setattr(SessionFiles, "list_uploads", counting_uploads)
    z = _zip(
        client.get(
            "/api/v1/sessions/s1/files/archive"
            "?path=outbox/a.bin&path=outbox/b.bin&path=outbox/c.bin"
        )
    )
    assert sorted(z.namelist()) == ["a.bin", "b.bin", "c.bin"]
    assert calls["outbox"] == 1
    assert calls["uploads"] == 0


def test_archive_names_are_utf8_to_the_reader(tmp_path):
    """A non-ASCII arcname must round-trip: the UTF-8 flag (bit 11) is the
    reader's only cue that a name is not CP437. Without it, Python's zipfile
    — and every phone reader that follows the spec the same way — decodes
    'é' as mojibake and the human's filename is lost."""
    client, _st, root = make_client(tmp_path)
    outbox = Path(state_paths(str(root)).outbox_for("s1"))
    (outbox / "café").mkdir(parents=True, exist_ok=True)
    (outbox / "café" / "名前.txt").write_bytes(b"utf8\n")
    z = _zip(client.get("/api/v1/sessions/s1/files/archive?path=outbox"))
    assert z.namelist() == ["café/名前.txt"]
    assert z.read("café/名前.txt") == b"utf8\n"


def test_archiving_streams_and_writes_nothing_to_the_volume(tmp_path):
    """No staging copy: a tree the size of the free space must still come out.

    Asserted by what the volume holds before and after the request, and by the
    bytes being a complete, correct archive.
    """
    client, _st, root = make_client(tmp_path)
    blob = os.urandom(2 * 1024 * 1024)
    _artifact(root, "big/blob.bin", blob)
    before = {p for p in Path(root).rglob("*") if p.is_file()}
    z = _zip(client.get("/api/v1/sessions/s1/files/archive?path=outbox"))
    assert z.read("big/blob.bin") == blob
    after = {p for p in Path(root).rglob("*") if p.is_file()}
    assert after == before


def test_archiving_nothing_is_a_404_not_an_empty_zip(tmp_path):
    client, _st, _root = make_client(tmp_path)
    assert client.get("/api/v1/sessions/s1/files/archive?path=outbox/nope").status_code == 404
    assert client.get("/api/v1/sessions/s1/files/archive?path=nonsense").status_code == 404


def test_an_unknown_archive_selection_is_the_vanished_file_shape(tmp_path):
    """A selection naming nothing that resolves raises FileNotDelivered —
    the same shape as a named upload the listing does not hold — so the
    outbox branch and the upload branch cannot drift apart (one
    vanished-file 404, the other a bare 400-class DeliveryError)."""
    _c, app_state, _root = make_client(tmp_path)
    sf = app_state.delivery.files(app_state.state, "s1", None)
    with pytest.raises(FileNotDelivered):
        app_state.delivery.archive_entries(sf, "outbox/nope")
    with pytest.raises(FileNotDelivered):
        app_state.delivery.archive_entries(sf, "uploads/nope")


def test_an_entry_that_grows_past_its_promise_aborts_with_a_reason(tmp_path, monkeypatch):
    """The zip64 decision is made from stat, before the bytes leave disk —
    the right call, but a file still being written (an unstable file
    downloads by design) can grow past the 32-bit fields it was promised.
    The guard detects the overflow at the first moment the true sizes are
    known and aborts with a reason — the client sees a truncated download
    the log explains, never a zip a reader would reject as corrupt
    (struct.error over an in-flight response, no context at all)."""
    from app.delivery import archive as arch

    client, app_state, root = make_client(tmp_path)
    body = os.urandom(5000)
    p = _artifact(root, "grow.bin", body)

    real_stat = os.stat

    def small_stat(path, *a, **k):
        st = real_stat(path, *a, **k)
        if os.path.abspath(str(path)) == os.path.abspath(str(p)):
            return SimpleNamespace(st_size=100, st_mtime=st.st_mtime, st_mode=st.st_mode)
        return st

    monkeypatch.setattr(arch.os, "stat", small_stat)
    sfiles = app_state.delivery.files(app_state.state, "s1", None)
    _name, entries = app_state.delivery.archive_entries(sfiles, ["outbox"])
    # a low threshold plays the 4 GiB boundary: the (small) stat hint says
    # classic, the real 5000 bytes do not fit the promise
    with pytest.raises(arch.ArchiveError):
        list(arch.stream_zip(entries, zip64_threshold=1000))
    # and the honest stat still streams a valid classic zip
    monkeypatch.undo()
    z = _zip_of(client, "outbox")
    assert z.read("grow.bin") == body


# ---------------------------------------------------------------------- #
# same-name uploads: the inbox never silently replaces a different file
# ---------------------------------------------------------------------- #


def test_a_second_upload_with_the_same_name_lands_beside_the_first(tmp_path):
    """Folder uploads make same-basename pairs routine (a/notes.txt and
    b/notes.txt both send as ``notes.txt``). The phone paid for both files;
    the inbox must hold both. The second delivery lands beside the first,
    keep-shelf style (``notes.1.txt``) — never on top of it.
    """
    client, _st, root = make_client(tmp_path)
    assert upload(client, "notes.txt", b"from folder A").status_code == 201
    r2 = upload(client, "notes.txt", b"from folder B")
    assert r2.status_code == 201
    assert r2.json()["name"] == "notes.1.txt", r2.json()

    names = sorted(
        u["name"]
        for u in client.get("/api/v1/sessions/s1/files").json()["uploads"]
    )
    assert names == ["notes.1.txt", "notes.txt"]
    assert (
        client.get("/api/v1/sessions/s1/files/uploads/notes.txt").content
        == b"from folder A"
    )
    assert (
        client.get("/api/v1/sessions/s1/files/uploads/notes.1.txt").content
        == b"from folder B"
    )
    # and a third one keeps counting (notes.2.txt)
    r3 = upload(client, "notes.txt", b"from folder C")
    assert r3.json()["name"] == "notes.2.txt", r3.json()
    assert len(list(Path(root).glob("blobs/*/*"))) == 3  # three distinct byte sets


def test_reuploading_identical_bytes_keeps_the_name_and_the_copy(tmp_path):
    """The dedup is the exception the replace is allowed for: the same bytes
    re-landing on their own inode cost no name change and no second copy.
    """
    client, _st, root = make_client(tmp_path)
    body = b"the same file twice"
    r1 = upload(client, "same.bin", body)
    r2 = upload(client, "same.bin", body)
    assert r1.json()["sha256"] == r2.json()["sha256"]
    assert r2.json()["name"] == "same.bin", r2.json()
    uploads = client.get("/api/v1/sessions/s1/files").json()["uploads"]
    assert [u["name"] for u in uploads] == ["same.bin"]
    blobs = list(Path(root).glob("blobs/*/*"))
    assert len(blobs) == 1  # one copy of the bytes, ever


def test_an_attach_lands_beside_a_different_file_of_the_same_name(tmp_path):
    """Attach has the same promise as upload: it never silently replaces a
    different file the target's human already sees.
    """
    client, _st, root = make_client(tmp_path)
    assert upload(client, "data.csv", b"mine", sid="s1").status_code == 201
    assert upload(client, "data.csv", b"theirs", sid="s2").status_code == 201
    r = client.post(
        "/api/v1/sessions/s1/files/attach",
        json={"source": "s2", "path": "uploads/data.csv"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["name"] == "data.1.csv", r.json()
    assert (
        client.get("/api/v1/sessions/s1/files/uploads/data.csv").content == b"mine"
    )
    assert (
        client.get("/api/v1/sessions/s1/files/uploads/data.1.csv").content
        == b"theirs"
    )


# ---------------------------------------------------------------------- #
# multi-select archive: exactly the selected entries, named for their dir
# ---------------------------------------------------------------------- #


def test_a_multi_select_archives_exactly_what_was_selected(tmp_path):
    """Repeated `path=` archives the union of the selections, each entry
    still gated, named for the deepest directory they all share, with the
    entries relative to it.
    """
    client, _st, root = make_client(tmp_path)
    _artifact(root, "v2/a.md", b"a")
    _artifact(root, "v2/b.md", b"b")
    _artifact(root, "v2/sub/c.md", b"c")
    _artifact(root, "other.md", b"o")

    r = client.get(
        "/api/v1/sessions/s1/files/archive"
        "?path=outbox/v2/a.md&path=outbox/v2/b.md&path=outbox/v2/sub/c.md"
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-disposition"].endswith('filename="v2.zip"')
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert sorted(z.namelist()) == ["a.md", "b.md", "sub/c.md"]
    assert z.read("sub/c.md") == b"c"  # nothing else, nothing planted


def test_a_multi_select_without_a_shared_dir_earns_the_neutral_name(tmp_path):
    client, _st, root = make_client(tmp_path)
    _artifact(root, "v2/a.md", b"a")
    _artifact(root, "loose.md", b"l")
    r = client.get(
        "/api/v1/sessions/s1/files/archive?path=outbox/v2/a.md&path=outbox/loose.md"
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-disposition"].endswith('filename="files.zip"')
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert sorted(z.namelist()) == ["loose.md", "v2/a.md"]  # shape preserved


def test_a_selection_and_its_folder_do_not_duplicate_the_file(tmp_path):
    """Selecting `v2/a.md` AND `v2/` (or the whole outbox) puts `a.md` in
    the archive once, not twice."""
    client, _st, root = make_client(tmp_path)
    _artifact(root, "v2/a.md", b"a")
    _artifact(root, "v2/b.md", b"b")
    r = client.get(
        "/api/v1/sessions/s1/files/archive?path=outbox/v2/a.md&path=outbox"
    )
    assert r.status_code == 200, r.text
    # every selected entry sits under v2/ — the archive is v2/, entries
    # relative to it, a.md exactly once
    assert r.headers["content-disposition"].endswith('filename="v2.zip"')
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert sorted(z.namelist()) == ["a.md", "b.md"]


def test_a_mixed_selection_never_holds_two_files_under_one_name(tmp_path):
    """An outbox root file and an upload of the SAME name are one name in
    the archive (a mixed selection keeps full paths in both areas). A
    collision there is not a rename: readers keep only the LAST same-named
    zip entry, so without a suffix the first file would vanish from the
    archive without a trace. Both bytes must come out."""
    client, _st, root = make_client(tmp_path)
    _artifact(root, "loose.md", b"outbox bytes")
    assert upload(client, "loose.md", b"upload bytes").status_code == 201
    z = _zip(
        client.get(
            "/api/v1/sessions/s1/files/archive"
            "?path=outbox/loose.md&path=uploads/loose.md"
        )
    )
    names = z.namelist()
    assert len(names) == 2 and len(set(names)) == 2, names
    bodies = sorted(z.read(n) for n in names)
    assert bodies == [b"outbox bytes", b"upload bytes"]


# ---------------------------------------------------------------------- #
# one-shot upload: the staged pump, nothing else
# ---------------------------------------------------------------------- #


def test_a_one_shot_upload_leaves_no_staging_residue_when_it_lands(tmp_path):
    """The one-shot path is the staged path without the client: the part
    lands in the session's staging area on the way in, and complete drops
    it — a finished one-shot upload leaves the volume exactly as clean as a
    finished staged one (no part, no meta, nothing for the TTL to find).
    """
    client, _st, root = make_client(tmp_path)
    assert upload(client, "oneshot.bin", b"streamed in one request").status_code == 201
    assert list(Path(root).glob("staging/*")) == []
    assert (
        client.get("/api/v1/sessions/s1/files/uploads/oneshot.bin").content
        == b"streamed in one request"
    )


def test_a_one_shot_stream_that_dies_leaves_a_resumable_part(tmp_path):
    """The one-shot's dead stream is not an abort: what landed stays on disk
    (in the session's staging area, resumable by the offset protocol until
    its TTL) — never a partial file in the inbox, never nothing at all.
    """
    client, app_state, root = make_client(tmp_path)

    async def dying():
        yield b"0123456789" * 10
        raise ConnectionError("the radio gave up")

    # drive the same three steps the route drives (create → pump → complete)
    sf = app_state.delivery.files(app_state.state, "s1", None)
    staged = app_state.delivery.staged("s1")

    async def go():
        uid, _off = await asyncio.to_thread(staged.create, "dying.bin")
        await staged.append_stream(uid, 0, dying())
        await app_state.delivery.complete_staged(staged, uid, sf)

    with pytest.raises(ConnectionError):
        asyncio.run(go())

    parts = list(Path(app_state.state.staging_for("s1")).glob("*/part"))
    assert len(parts) == 1
    assert parts[0].read_bytes() == b"0123456789" * 10  # what landed is on disk
    # …and nothing half-visible in the inbox
    assert client.get("/api/v1/sessions/s1/files").json()["uploads"] == []


# ---------------------------------------------------------------------- #
# a file that vanished after its gate passed: the gate's 404, never a 500
# ---------------------------------------------------------------------- #


def test_rename_of_a_file_unlinked_under_the_gate_is_a_404_not_a_500(tmp_path, monkeypatch):
    """The gate and the link are two syscalls apart, and the outbox is
    agent-writable: an artifact unlinked in the window answers the gate's
    404, never a 500 off a vanished path — and the destination is never
    created."""
    client, _st, root = make_client(tmp_path)
    _artifact(root, "v2/ready.md")
    real_link = os.link

    def vanished(src, dst, *a, **k):
        if str(src).endswith("/ready.md"):
            raise FileNotFoundError(src)
        return real_link(src, dst, *a, **k)

    monkeypatch.setattr(os, "link", vanished)
    r = client.patch(
        "/api/v1/sessions/s1/files/outbox/v2/ready.md", json={"to": "v2/done.md"}
    )
    assert r.status_code == 404, r.text
    assert not (Path(state_paths(str(root)).outbox_for("s1")) / "v2/done.md").exists()


def test_rename_of_an_upload_unlinked_under_the_gate_is_a_404_not_a_500(tmp_path, monkeypatch):
    """The same window on the inbox side (a rename racing a delete from
    another tab): 404, and the manifest row stays put under its old name."""
    client, _st, root = make_client(tmp_path)
    assert upload(client, "doc.txt").status_code == 201
    real_link = os.link

    def vanished(src, dst, *a, **k):
        if str(src).endswith("/doc.txt"):
            raise FileNotFoundError(src)
        return real_link(src, dst, *a, **k)

    monkeypatch.setattr(os, "link", vanished)
    r = client.patch(
        "/api/v1/sessions/s1/files/uploads/doc.txt", json={"to": "final.txt"}
    )
    assert r.status_code == 404, r.text
    assert [u["name"] for u in client.get("/api/v1/sessions/s1/files").json()["uploads"]] == [
        "doc.txt"
    ]
