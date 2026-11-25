"""Staged (resumable) uploads: a phone on a bad link finishes its upload.

``<state>/staging/<session>/<upload_id>/{part,meta.json}`` — the bytes so
far, on disk, plus what they are. A chunk is appended **only at the offset
the client says it is appending at**: a mismatch answers 409 with the
server's size, the client resumes from there, and no byte is ever written
twice or skipped — the whole difference between "resumable" and "please
re-upload your 4 GB because the call dropped at 3.9 GB".

Why disk and offsets instead of one streaming POST: the radio drops
mid-body and a stream that dies at the socket is a dead stream (the human
re-pays for every byte); a dropped connection is not an abort (invariant 4)
— cancel is an explicit DELETE, never a side effect of a socket dying; and
the app can restart underneath an in-flight upload (the staging tree is on
the state volume; a staged upload survives it, swept by TTL, not at boot).

The sha256 is computed at ``complete``, over the WHOLE file — never
incrementally across requests (a resumed stream cannot be trusted to have
hashed the same bytes it appended, and a hash state is not portable). A
mid-chunk corruption or an interleaved writer cannot deliver a file whose
recorded sha does not match its bytes: the delivered name appears only after
that final hash, and the bytes land through the blob store like every other
delivery (one copy, hardlinked in).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import queue
import re
import shutil
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from app.delivery.store import DeliveryError, _safe_relpath

_UPLOAD_ID = re.compile(r"^[0-9a-f]{32}$")
# a single, non-dotted path component. The app mints the ids (uuid4), but
# this is the boundary for any id that reaches a path: deliberately NOT a
# strict shape, so a future id format cannot silently break uploads — it
# only refuses what a path must never be: a traversal or a hidden name.
_SESSION_ID = re.compile(r"^(?!\.{1,2}$)[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: a staged upload nobody touched in this long is dead weight (its client is
#: gone); the TTL is a bound on STALENESS, never a limit on an upload in
#: progress — an upload being appended to has a fresh `updated` on every chunk.
STAGED_TTL_SEC = 72 * 3600.0

# One append lock per staged part, with a holder count. The entry is dropped
# when the last holder leaves, so a long-running process keeps a lock only
# for an upload being appended to right now, never one per upload ever
# attempted. The count makes the drop safe: a waiter counted itself in
# before it blocked, so the entry can never be pulled out from under it.
# This is the SYNC half of the pair: ``append`` holds it across
# (check, write, finish). The HTTP chunk path serializes on the asyncio twin
# below — a threading lock held across the body's awaits would freeze the
# loop — and the route never drives both on one part.
_lock_guard = threading.Lock()
_chunk_locks: dict[str, tuple[threading.Lock, int]] = {}


class StagedError(RuntimeError):
    """Any staged-upload failure the route answers 404/400 with."""


class OffsetMismatch(StagedError):
    """The client's offset is not the server's size.

    Carries the server's size so the client can resume from it instead of
    guessing — 409 without a number to resume from is just a re-upload.
    """

    def __init__(self, size: int):
        super().__init__(f"offset mismatch: server holds {size} bytes")
        self.size = size


@contextmanager
def _chunk_lock(key: str):
    with _lock_guard:
        lock, n = _chunk_locks.get(key, (threading.Lock(), 0))
        _chunk_locks[key] = (lock, n + 1)
    try:
        yield lock
    finally:
        with _lock_guard:
            lock, n = _chunk_locks[key]
            if n <= 1:
                del _chunk_locks[key]
            else:
                _chunk_locks[key] = (lock, n - 1)


# The async twin of _chunk_lock, for append_stream only: that path awaits the
# body read ON THE EVENT LOOP, so a threading lock held across those awaits
# would freeze the loop the moment a second concurrent appender for the same
# part arrived — both handlers live on the one loop thread, and the second
# would block the loop until the first finishes, which it can never do. An
# asyncio lock serializes the same (check, write) pair without ever blocking
# the loop. The sync append keeps the threading lock — its callers never
# share the loop. Same holder-count rule: the entry dies with its last
# holder, so a lock is never reused across two event loops (a bound lock is
# not portable to a fresh one).
_alock_guard = threading.Lock()
_alock_by_part: dict[str, tuple[asyncio.Lock, int]] = {}


@contextmanager
def _astream_lock(key: str):
    with _alock_guard:
        lock, n = _alock_by_part.get(key, (asyncio.Lock(), 0))
        _alock_by_part[key] = (lock, n + 1)
    try:
        yield lock
    finally:
        with _alock_guard:
            lock, n = _alock_by_part[key]
            if n <= 1:
                del _alock_by_part[key]
            else:
                _alock_by_part[key] = (lock, n - 1)


#: how much of the body is batched before one hand-off to the writer thread
#: (the module's 1 MB idiom — a thread hop per 10 KB socket chunk would be
#: pure overhead)
_PUMP_BATCH = 1024 * 1024
#: the pump's queue depth: a full queue back-pressures the UPLOAD (its puts
#: stall) and never the loop. With the UI's 1 MB-per-request protocol the
#: queue holds at most a couple of items; the bound is what keeps a
#: pathological single-request multi-GB upload from buffering the whole thing
_STREAM_DEPTH = 32
#: how often a stalled put re-asks whether the writer is still alive
_PUMP_LIVENESS_POLL_SEC = 0.1


def _pump_to_part(
    part: Path, offset: int, q: "queue.Queue", result: list, done: threading.Event
) -> None:
    """The writer half of append_stream, in its own thread.

    The fsync is the call that can take seconds on a loaded disk; it belongs
    here, where a stall only slows THIS upload, not the loop that serves
    every run. Chunks arrive in order from the one reader; the sentinel
    (None) ends the stream — including after the reader's death, in which
    case we still fsync what landed, because the on-disk size is the number
    the offset protocol resumes from and it must not lie.

    ``done`` is set on EVERY exit (success, failure, even a killed loop): a
    put that stalls on a full queue must be able to tell "the writer is
    still chewing a slow disk" from "the writer is gone and this queue is
    now a dead end" — a queue full in front of a dead worker is the one
    shape a blocking ``q.put`` turns into a hung request and a leaked
    thread the process cannot exit without.
    """
    try:
        with open(part, "r+b") as f:
            f.seek(offset)
            written = 0
            while True:
                chunk = q.get()
                if chunk is None:
                    break
                f.write(chunk)
                written += len(chunk)
            f.flush()
            os.fsync(f.fileno())
        result.append(written)
    except BaseException as exc:
        result.append(exc)  # reported through result; the join must not mask it
    finally:
        done.set()


def _worker_fault(result: list) -> BaseException:
    """The writer is gone: surface its error (or a plain one — ``done`` is
    only ever set after a result append, so the fallback should not fire)."""
    if result and isinstance(result[0], BaseException):
        return result[0]
    return StagedError("the upload writer died without recording why")


class Staged:
    """Staged uploads of ONE session (the ``state/staging/<sid>`` area).

    No cross-session reach: every id is resolved inside this session's
    staging directory, and an id that is not 32 hex chars is refused before it
    touches the filesystem (so ``..`` can never be an upload id).
    """

    def __init__(self, root: str | Path, session_id: str):
        if not _SESSION_ID.fullmatch(session_id or ""):
            # the id arrives in a URL path; the registry gates it first, and
            # this is the second lock — a session id must never be a path.
            raise StagedError(f"not a session id: {session_id!r}")
        self.root = Path(root) / "staging" / session_id
        self.session_id = session_id

    # ------------------------------------------------------------------ #
    # ids and paths
    # ------------------------------------------------------------------ #

    def _dir(self, upload_id: str) -> Path:
        if not _UPLOAD_ID.fullmatch(upload_id or ""):
            raise StagedError(f"not an upload id: {upload_id!r}")
        d = self.root / upload_id
        if not d.is_dir():
            raise StagedError(f"no such staged upload: {upload_id}")
        return d

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #

    def create(self, name: str) -> tuple[str, int]:
        """Open a staged upload. Returns ``(upload_id, offset)``.

        The name is a path within the inbox (a folder upload keeps its tree
        — the UI sends the relative path) and is validated here, not at
        complete: a path the store will refuse to land (a NUL, an all-dot
        component, a traversal) must not get a part, a meta, and a 72-hour
        TTL of residue before the refusal — the request is a 400 before
        anything touches the volume.
        """
        try:
            _safe_relpath(name)
        except DeliveryError as e:
            raise StagedError(str(e)) from None
        uid = uuid.uuid4().hex
        d = self.root / uid
        d.mkdir(parents=True, exist_ok=False)
        (d / "part").touch()
        self._write_meta(d, name=name, created=time.time())
        return uid, 0

    def append(self, upload_id: str, offset: int, chunks) -> int:
        """Append an iterator of byte chunks AT ``offset``. Returns new size.

        ``offset`` is verified BEFORE anything is written; the bytes are
        written at that exact position and fsynced, so a chunk the client is
        told landed is on disk (a resume that trusts a missing chunk would
        silently truncate the file).
        """
        d = self._dir(upload_id)
        part = d / "part"
        if offset < 0:
            raise StagedError("offset must not be negative")
        with _chunk_lock(str(part)) as lock:
            with lock:
                self._check_offset(part, offset)
                with open(part, "r+b") as f:
                    f.seek(offset)
                    written = self._write(f, chunks)
                    self._sync(f)
                return self._finish(d, offset, written)

    async def append_stream(self, upload_id: str, offset: int, aiter) -> int:
        """The same append over an async byte stream (a request body).

        The loop only READS the body — small socket chunks, one await at a
        time. The bytes are handed to a writer thread through a bounded
        queue, so a slow disk back-pressures THIS upload (its puts stall)
        instead of stalling the loop that serves every run, and at most a
        few 1 MB items (the queue's depth) ever sit in RAM — never the whole
        body. A put NEVER blocks on the queue alone: the writer is the queue's
        only consumer, so a full queue is polled against the writer's
        ``done`` event — a slow disk drains and the wait ends, a dead writer
        (a cancel/TTL sweep that took the part, a disk failure) raises ITS
        error instead of hanging the request and leaking a thread the
        process could not exit with. A reader that dies mid-body (the client
        gave up) ends the stream: what landed is fsynced and the on-disk
        size stays the number a resume trusts. The lock is the one
        :meth:`part_lock` hands to ``complete``: hash and move never
        interleave with a chunk landing.
        """
        d = self._dir(upload_id)
        part = d / "part"
        if offset < 0:
            raise StagedError("offset must not be negative")
        with _astream_lock(str(part)) as alock:
            async with alock:
                self._check_offset(part, offset)
                q: "queue.Queue" = queue.Queue(maxsize=_STREAM_DEPTH)
                result: list = []
                done = threading.Event()
                worker = threading.Thread(
                    target=_pump_to_part,
                    args=(part, offset, q, result, done),
                    daemon=True,
                )
                worker.start()

                def _push(item) -> None:
                    """Hand one item to the writer without ever blocking past
                    its death (runs in a thread; the loop stays free).

                    Single producer (this reader) / single consumer (the
                    worker), so ``full()`` + ``put_nowait`` is race-free:
                    nothing but the worker ever removes an item. A full
                    queue in front of a LIVE writer is the designed
                    back-pressure and polls until it drains; in front of a
                    DEAD one it is a dead end — raise the writer's error
                    and let the caller answer.
                    """
                    while q.full():
                        if done.is_set():
                            raise _worker_fault(result)
                        time.sleep(_PUMP_LIVENESS_POLL_SEC)
                    q.put_nowait(item)

                buf = bytearray()
                try:
                    async for chunk in aiter:
                        if not isinstance(chunk, (bytes, bytearray)):
                            chunk = bytes(chunk)
                        if not chunk:
                            continue
                        buf.extend(chunk)
                        if len(buf) >= _PUMP_BATCH:
                            await asyncio.to_thread(_push, bytes(buf))
                            buf.clear()
                    if buf:
                        await asyncio.to_thread(_push, bytes(buf))
                        buf.clear()
                    await asyncio.to_thread(_push, None)
                except BaseException:
                    # the reader died (client gave up, task cancelled): hand
                    # over what is still buffered, end the stream, and wait
                    # for the worker so what landed is on disk before
                    # anything reads the size. Every step is best-effort —
                    # on a dying loop the executor is gone and the daemon
                    # worker dies with the process; the part's on-disk size
                    # is still the number a resume trusts
                    try:
                        if buf:
                            await asyncio.to_thread(_push, bytes(buf))
                        await asyncio.to_thread(_push, None)
                        await asyncio.to_thread(worker.join)
                    except BaseException:
                        pass  # executor already gone: the daemon worker dies with it
                    raise
                await asyncio.to_thread(worker.join)
                written = result[0]
                if isinstance(written, BaseException):
                    raise written
                return await asyncio.to_thread(self._finish, d, offset, written)

    # -- append internals ---------------------------------------------- #

    @staticmethod
    def _check_offset(part: Path, offset: int) -> None:
        size = part.stat().st_size
        if size != offset:
            raise OffsetMismatch(size)

    @staticmethod
    def _write(f, chunks) -> int:
        written = 0
        for chunk in chunks:
            if not chunk:
                continue
            f.write(chunk)
            written += len(chunk)
        return written

    @staticmethod
    def _sync(f) -> None:
        f.flush()
        os.fsync(f.fileno())

    def _finish(self, d: Path, offset: int, written: int) -> int:
        size = offset + written
        self._touch_meta(d, size=size)
        return size

    def size(self, upload_id: str) -> int:
        return (self._dir(upload_id) / "part").stat().st_size

    def list(self) -> list[dict]:
        """What is staged right now — the resume list after a reload or
        restart.

        ``size`` is the PART's on-disk size, not meta's: meta has no size
        until the first chunk lands, and a resume that is offered one anyway
        must start at 0 (the offset protocol self-heals the rest), never at
        an unknown number. An entry that vanishes between the listdir and
        the size read (a concurrent complete/cancel/TTL sweep) is DROPPED,
        not raised: a resume of it can never succeed, and the list is what
        a reload offers the human — it must not 500 on the ordinary race.
        """
        rows: list[dict] = []
        try:
            entries = sorted(os.listdir(self.root))
        except OSError:
            return rows
        for uid in entries:
            if not _UPLOAD_ID.fullmatch(uid):
                continue
            d = self.root / uid
            # a directory with no part is residue, not an upload: a crash
            # between the adopt (part moved into the store) and the drop left
            # the meta behind, or a crash between mkdir and touch. Nothing
            # can resume from it, so the resume list must not offer it back
            # as a size-0 upload the client would re-send.
            if not os.path.lexists(d / "part"):
                continue
            meta = self._read_meta(d)
            if meta is None:
                continue
            try:
                size = self.size(uid)
            except StagedError:
                continue  # the directory went after the entry was listed
            except OSError:
                size = 0
            rows.append({"upload_id": uid, **meta, "size": size})
        rows.sort(key=lambda r: -r.get("updated", 0))
        return rows

    def cancel(self, upload_id: str) -> None:
        """Explicit cancel — the only way a staged upload dies early."""
        shutil.rmtree(self._dir(upload_id), ignore_errors=True)

    def part_lock(self, upload_id: str):
        """The part's append lock, as a context manager yielding the lock.

        The SAME per-part asyncio lock ``append_stream`` holds across its
        (check, write) pair — ``complete`` must hold it too: it hashes the
        part and then moves it, and a chunk landing between the two would
        store the part under a sha of different bytes (the blob's name is
        its promise about its bytes). Use as
        ``with staged.part_lock(uid) as lock: async with lock: ...``.
        The key is the part's string path — the same key ``append_stream``
        registers under (a Path object and its string are distinct keys).
        """
        return _astream_lock(str(self._dir(upload_id) / "part"))

    def verify(self, upload_id: str) -> tuple[Path, str, int]:
        """The staged part + the sha over ALL its bytes + its size.

        Called once, at complete (under :meth:`part_lock`): the sha is
        computed here (not per chunk), so what gets delivered is exactly
        what is on disk, whatever happened to the requests that put it
        there.
        """
        d = self._dir(upload_id)
        part = d / "part"
        h = hashlib.sha256()
        size = 0
        with open(part, "rb") as f:
            while True:
                b = f.read(1024 * 1024)
                if not b:
                    break
                h.update(b)
                size += len(b)
        # the size is whatever was hashed, not whatever meta claimed: meta is
        # a hint for the resume UI, this is the file
        return part, h.hexdigest(), size

    def name_of(self, upload_id: str) -> str:
        meta = self._read_meta(self._dir(upload_id)) or {}
        return str(meta.get("name", "upload"))

    def drop(self, upload_id: str) -> None:
        """Remove a staged upload without ceremony (post-delivery teardown),
        and the session's staging dir with it when it was the last one."""
        shutil.rmtree(self.root / upload_id, ignore_errors=True)
        try:
            self.root.rmdir()  # only when it is empty
        except OSError:
            pass

    # ------------------------------------------------------------------ #
    # sweep
    # ------------------------------------------------------------------ #

    def gc(self, ttl_sec: float = STAGED_TTL_SEC) -> list[str]:
        """Drop staged uploads nobody has touched in ``ttl_sec``.

        Runs at boot and on the GC tick. Note what it does NOT do: sweep
        everything at boot (a resumable upload must survive a restart), and
        touch anything in progress.
        """
        removed: list[str] = []
        now = time.time()
        try:
            entries = os.listdir(self.root)
        except OSError:
            return removed
        for uid in entries:
            d = self.root / uid
            if not _UPLOAD_ID.fullmatch(uid) or not d.is_dir():
                # not an upload (a stray name, a leftover symlink, a planted
                # FIFO): junk. Removal by shape — a real directory by
                # rmtree, every non-directory entry by unlink: a FIFO is
                # the one shape rmtree cannot take, and routed to it the
                # entry would sit here forever, the sweep retrying its
                # no-op every tick. Report only what was still there AND is
                # gone now — listdir and the removal are not atomic, and an
                # entry that vanished in between (or that the removal could
                # not take) is not a removal this sweep made.
                was_there = d.is_symlink() or d.exists()
                if d.is_dir() and not d.is_symlink():
                    shutil.rmtree(d, ignore_errors=True)
                else:
                    d.unlink(missing_ok=True)
                if was_there and not (d.is_symlink() or d.exists()):
                    removed.append(uid)
                continue
            if not os.path.lexists(d / "part"):
                # the part was adopted into the store (a crash left the
                # directory without its drop) or was never touched: residue,
                # swept regardless of TTL — it will never become resumable
                shutil.rmtree(d, ignore_errors=True)
                if not (d.is_symlink() or d.exists()):
                    removed.append(uid)
                continue
            meta = self._read_meta(d) or {}
            if now - float(meta.get("updated", 0)) < ttl_sec:
                continue
            shutil.rmtree(d, ignore_errors=True)
            if not (d.is_symlink() or d.exists()):
                removed.append(uid)
        if removed:
            try:
                self.root.rmdir()  # only when emptied
            except OSError:
                pass
        return removed

    # ------------------------------------------------------------------ #
    # meta
    # ------------------------------------------------------------------ #

    @staticmethod
    def _meta_path(d: Path) -> Path:
        return d / "meta.json"

    def _read_meta(self, d: Path) -> dict | None:
        try:
            with open(self._meta_path(d), "r", encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            return None
        return meta if isinstance(meta, dict) else None

    def _write_meta(self, d: Path, **fields) -> None:
        meta = self._read_meta(d) or {}
        meta.update(fields)
        meta.setdefault("name", "upload")
        meta["updated"] = time.time()
        tmp = self._meta_path(d).with_suffix(f".tmp-{uuid.uuid4().hex[:6]}")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._meta_path(d))

    def _touch_meta(self, d: Path, **fields) -> None:
        try:
            self._write_meta(d, **fields)
        except OSError:
            pass  # the part is fsynced and authoritative; meta is a hint
