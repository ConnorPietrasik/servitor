"""Unit: the outbox sweep — deltas instead of client polling.

The contract: a phone on a metered plan must not pay for a listing that did
not change. The server sweeps a WATCHED session, diffs against its own
snapshot, and publishes only the diff — so the steady state is ZERO bytes,
and the only full listing is the one the client asked for.

Pinned:
- the first sweep seeds and publishes nothing (no baseline to diff);
- a new artifact, a rewrite, a removal, and the background sha filling in
  each publish exactly one delta;
- an unchanged sweep publishes nothing at all;
- ``stable``: mtime past the grace, or size+mtime held across two sweeps —
  a meter, and a fresh file with a fresh mtime starts out unstable;
- ``files_tick`` sweeps ONLY sessions with an SSE subscriber, publishes on
  that session's hub, and never touches a session nobody watches;
- release (``forget``) drops the snapshot: the dict cannot grow with the
  number of sessions that ever ran.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from types import SimpleNamespace

import pytest

from app.api.sse import SseHub
from app.delivery import store
from app.delivery.service import Delivery
from app.delivery.store import SessionFiles
from app.delivery.watch import FileWatch
from app.iso.layout import state_paths
from app.lifecycle import Lifecycle
from app.registry import CLEANING, DRAINING, FREE, LIVE, Registry


def _files(tmp_path, grace: float = 0.0) -> tuple[FileWatch, SessionFiles]:
    watch = FileWatch(grace)
    sf = SessionFiles(
        session_dir=tmp_path / "sessions" / "s1",
        inbox=tmp_path / "inbox" / "s1",
        outbox=tmp_path / "outbox",
        slot=None,
    )
    sf.outbox.mkdir(parents=True, exist_ok=True)
    return watch, sf


def _settled(timeout: float = 10.0) -> None:
    """Wait for the background sha threads to finish.

    Needed by the sweeps that assert 'nothing changed': a hash that LANDS
    between two sweeps is itself a change (and must publish one), so a test
    about silence has to start from a settled file.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with store._sha_lock:
            if not store._SHA_PENDING:
                return
        time.sleep(0.01)
    pytest.fail("background hashes did not finish in time")


# ---------------------------------------------------------------------- #
# sweep / deltas
# ---------------------------------------------------------------------- #


def test_first_sweep_seeds_and_publishes_nothing(tmp_path):
    watch, sf = _files(tmp_path)
    (sf.outbox / "a.md").write_bytes(b"a")
    entries, delta = watch.sweep("s1", sf)
    assert [e["path"] for e in entries] == ["a.md"]
    assert delta is None  # no baseline: the client's fetch carries the list


def test_nothing_changed_publishes_nothing(tmp_path):
    watch, sf = _files(tmp_path)
    (sf.outbox / "a.md").write_bytes(b"a")
    watch.sweep("s1", sf)
    _settled()  # the hash landing after the seed IS a change…
    watch.sweep("s1", sf)  # …this sweep records the settled version
    _entries, delta = watch.sweep("s1", sf)
    assert delta is None


def test_new_artifact_is_an_added(tmp_path):
    watch, sf = _files(tmp_path)
    watch.sweep("s1", sf)  # seed empty
    (sf.outbox / "report.md").write_bytes(b"hello")
    _e, delta = watch.sweep("s1", sf)
    assert [a["path"] for a in delta["added"]] == ["report.md"]
    assert delta["added"][0]["size"] == 5
    assert delta["removed"] == [] and delta["changed"] == []


def test_rewrite_and_removal_are_reported(tmp_path):
    watch, sf = _files(tmp_path)
    (sf.outbox / "a.md").write_bytes(b"a")
    (sf.outbox / "gone.md").write_bytes(b"g")
    watch.sweep("s1", sf)
    time.sleep(0.01)
    (sf.outbox / "a.md").write_bytes(b"a" * 40)
    (sf.outbox / "gone.md").unlink()

    _e, delta = watch.sweep("s1", sf)

    assert [c["path"] for c in delta["changed"]] == ["a.md"]
    assert delta["removed"] == ["gone.md"]
    assert delta["added"] == []


def test_the_background_sha_filling_in_is_a_change(tmp_path, monkeypatch):
    """A file appears with sha256 null (the hash runs in the background);
    when it lands, the UI must be told — otherwise it shows 'computing…'
    until some unrelated change happens to bump the listing."""
    watch, sf = _files(tmp_path, grace=10_000)
    monkeypatch.setattr(store, "_SHA_CACHE", {})
    (sf.outbox / "big.bin").write_bytes(b"z" * 64)
    first, _ = watch.sweep("s1", sf)
    assert first[0]["sha256"] is None

    for _ in range(400):  # the hash is a background thread: wait for it
        if store._SHA_CACHE:
            break
        time.sleep(0.01)
    assert store._SHA_CACHE, "background hash never ran"

    _e, delta = watch.sweep("s1", sf)
    assert delta is not None and [c["path"] for c in delta["changed"]] == ["big.bin"]
    assert delta["changed"][0]["sha256"]


# ---------------------------------------------------------------------- #
# the stable badge (a meter, not a gate)
# ---------------------------------------------------------------------- #


def test_fresh_file_is_unstable_until_its_size_mtime_hold(tmp_path):
    watch, sf = _files(tmp_path, grace=10_000)  # grace far in the future
    (sf.outbox / "writing.bin").write_bytes(b"1")
    first, _ = watch.sweep("s1", sf)
    assert first[0]["stable"] is False  # first sighting: nothing held yet

    _settled()
    second, _ = watch.sweep("s1", sf)
    assert second[0]["stable"] is True  # unchanged across two sweeps


def test_a_landing_sha_never_marks_a_file_unstable(tmp_path, monkeypatch):
    """`stable` answers 'is the agent still writing this?' — a hash that
    finishes afterwards is metadata arriving, not content moving."""
    monkeypatch.setattr(store, "_SHA_CACHE", {})
    watch, sf = _files(tmp_path, grace=10_000)
    (sf.outbox / "art.bin").write_bytes(b"y" * 48)
    assert watch.sweep("s1", sf)[0][0]["sha256"] is None
    _settled()
    second, delta = watch.sweep("s1", sf)
    assert second[0]["stable"] is True
    assert delta is not None  # the sha arriving is still worth publishing


def test_mtime_past_the_grace_is_stable_on_the_first_sweep(tmp_path):
    """An app restart must not badge every existing artifact as still being
    written: age alone settles it."""
    watch, sf = _files(tmp_path, grace=1.0)
    f = sf.outbox / "old.bin"
    f.write_bytes(b"1")
    old = time.time() - 3600
    os.utime(f, (old, old))
    entries, _ = watch.sweep("s1", sf)
    assert entries[0]["stable"] is True


def test_a_file_being_written_keeps_changing_stays_unstable(tmp_path):
    watch, sf = _files(tmp_path, grace=10_000)
    f = sf.outbox / "growing.bin"
    f.write_bytes(b"1")
    assert watch.sweep("s1", sf)[0][0]["stable"] is False
    f.write_bytes(b"1" * 900)
    assert watch.sweep("s1", sf)[0][0]["stable"] is False


def test_stabilizing_with_no_other_change_is_published(tmp_path, monkeypatch):
    """The sha is already cached at first sighting (a settled file), so
    size/mtime/sha/mime never move — the only delta this file will ever
    produce is stable False→True. It must ride the stream: without it in the
    sig, the UI's 'writing…' chip lifts only on an unrelated change or a
    reload."""
    import hashlib

    watch, sf = _files(tmp_path, grace=10_000)  # grace far away: only the hold can settle
    body = b"z" * 32
    f = sf.outbox / "done.bin"
    f.write_bytes(body)
    st = f.stat()
    monkeypatch.setitem(
        store._SHA_CACHE,
        ("s1", "done.bin", st.st_size, st.st_mtime),
        hashlib.sha256(body).hexdigest(),
    )
    first, d1 = watch.sweep("s1", sf)
    assert first[0]["sha256"] and first[0]["stable"] is False and d1 is None
    second, d2 = watch.sweep("s1", sf)
    assert second[0]["stable"] is True
    assert d2 is not None, "a pure stable flip publishes nothing: the badge sticks"
    assert [c["path"] for c in d2["changed"]] == ["done.bin"]
    assert d2["changed"][0]["stable"] is True


# ---------------------------------------------------------------------- #
# lifecycle: who gets swept, and where the delta goes
# ---------------------------------------------------------------------- #


def _lc(tmp_path) -> Lifecycle:
    """Lifecycle with __init__ skipped (the test_drain pattern): the parts
    files_tick touches, nothing else."""
    root = tmp_path / "state"
    lc = Lifecycle.__new__(Lifecycle)
    lc.cfg = SimpleNamespace(
        registry=Registry(str(root / "registry.db")),
        state=state_paths(str(root)),
        delivery=Delivery(str(root)),
        files_sweep_sec=4.0,
    )
    lc.shutting_down = False
    lc._hubs = {}
    lc.files = FileWatch(stable_grace_sec=10_000)
    return lc


def _row(lc: Lifecycle, sid: str) -> None:
    lc.cfg.registry.upsert_session(session_id=sid, display_name=sid, slot="slot1")
    lc.cfg.registry.set_state(sid, LIVE)


def _slot_ok(monkeypatch) -> None:
    """Make the row's slot resolvable: the sweep binds its root reads to the
    slot's uid, and a test tree's files are owned by the running uid — so
    the identity comes to the test instead of the other way round."""
    ident = SimpleNamespace(
        name="slot1", uid=os.getuid(), gid=os.getgid(), home="/nonexistent"
    )
    monkeypatch.setattr("app.lifecycle.slot_identity", lambda _name: ident)


def _sfiles(lc: Lifecycle, sid: str) -> SessionFiles:
    return lc.cfg.delivery.files(lc.cfg.state, sid, None)


def test_tick_publishes_a_delta_only_for_a_watched_session(tmp_path, monkeypatch):
    _slot_ok(monkeypatch)
    lc = _lc(tmp_path)
    _row(lc, "s1")
    sf = _sfiles(lc, "s1")
    sf.outbox.mkdir(parents=True, exist_ok=True)
    lc.files.sweep("s1", sf)  # seed the baseline

    hub = SseHub("s1")
    sub, _replay, _gap = hub.subscribe(0)  # a subscriber: someone is watching
    lc._hubs["s1"] = hub

    (sf.outbox / "new.bin").write_bytes(b"new bytes")
    noisy = asyncio.run(lc.files_tick())

    assert noisy == ["s1"]
    # the payload is the delta and nothing else (the phone pays for change,
    # never for a listing it already has)
    body = next(json.loads(e.data) for e in hub._ring if e.name == "files_changed")
    assert [a["path"] for a in body["added"]] == ["new.bin"]


def test_tick_does_not_sweep_a_session_nobody_watches(tmp_path, monkeypatch):
    """Zero subscribers ⇒ zero walks: no subscribers means nobody can
    receive a delta, and walking a multi-GB outbox for nobody is waste."""
    _slot_ok(monkeypatch)
    lc = _lc(tmp_path)
    _row(lc, "s1")
    hub = SseHub("s1")  # subscribed by nobody
    lc._hubs["s1"] = hub
    walks = []
    monkeypatch.setattr(
        FileWatch, "sweep", lambda self, sid, sfiles: walks.append(sid) or ([], None)
    )
    assert asyncio.run(lc.files_tick()) == []
    assert walks == []


def test_tick_ignores_a_released_session(tmp_path, monkeypatch):
    _slot_ok(monkeypatch)
    lc = _lc(tmp_path)
    _row(lc, "s1")
    for gone in (DRAINING, CLEANING, FREE):
        lc.cfg.registry.set_state("s1", gone)
    hub = SseHub("s1")
    hub.subscribe(0)
    lc._hubs["s1"] = hub
    walks: list[str] = []
    monkeypatch.setattr(
        FileWatch, "sweep", lambda self, sid, sfiles: walks.append(sid) or ([], None)
    )
    assert asyncio.run(lc.files_tick()) == []
    assert walks == []


def test_tick_skips_a_session_whose_slot_this_host_cannot_resolve(tmp_path, monkeypatch):
    """The sweep runs on a timer with root privileges. A row whose slot this
    host cannot resolve has no uid to bind the reads to — walking that
    outbox would list/hash/serve inodes the ownership gate is there to keep
    root away from (a planted hardlink passes resolve-and-contain). The API
    keeps its dev/no-slot fallback for interactive reads; the timer skips."""
    lc = _lc(tmp_path)
    lc.cfg.registry.upsert_session(
        session_id="s1", display_name="s1", slot="slot404"
    )
    lc.cfg.registry.set_state("s1", LIVE)
    sf = lc.cfg.delivery.files(lc.cfg.state, "s1", None)
    sf.outbox.mkdir(parents=True, exist_ok=True)
    (sf.outbox / "planted.txt").write_bytes(b"a hardlink to somebody else's bytes")
    hub = SseHub("s1")
    hub.subscribe(0)  # a watcher is present: only the slot may stop the sweep
    lc._hubs["s1"] = hub

    walks: list[str] = []
    monkeypatch.setattr(
        FileWatch, "sweep", lambda self, sid, sfiles: walks.append(sid) or ([], None)
    )
    assert asyncio.run(lc.files_tick()) == []
    assert walks == []
    assert [e.name for e in hub._ring] == []


def test_forget_bounds_the_snapshot_dict(tmp_path):
    watch, sf = _files(tmp_path)
    watch.sweep("s1", sf)
    assert watch.watched() == ["s1"]
    watch.forget("s1")
    assert watch.watched() == []


def test_a_sweep_after_forget_cannot_reseed_a_released_session(tmp_path):
    """The timer checks LIVE before sweeping, then walks in a thread: a
    release that lands in the window must not be undone by the walk. A
    sweep that finds the dead record (forget's) drops its walk instead of
    reseeding the snapshot release just removed."""
    watch, sf = _files(tmp_path)
    watch.sweep("s1", sf)
    watch.forget("s1")
    entries, delta = watch.sweep("s1", sf)
    assert entries == [] and delta is None
    assert watch.watched() == []


def test_sweeps_and_forgets_racing_leave_no_dead_snapshot(tmp_path):
    """The order the scheduler picks must not matter: sweep-then-forget
    (forget pops what the sweep wrote) and forget-then-sweep (the dead
    record drops the walk) both end with no snapshot for the released
    session."""
    watch, sf = _files(tmp_path)
    for _ in range(8):
        errors = []

        def do_sweep():
            try:
                watch.sweep("s1", sf)
            except Exception as e:  # a race must not surface as a 500 either
                errors.append(e)

        a = threading.Thread(target=do_sweep)
        b = threading.Thread(target=watch.forget, args=("s1",))
        a.start()
        b.start()
        a.join()
        b.join()
        assert not errors, errors
        assert watch.watched() == []


def test_a_planted_fifo_is_not_listed_and_never_hashed(tmp_path):
    """The slot owns the outbox, and it can plant a FIFO there. A plain
    open() of a FIFO BLOCKS (the reader waits for a writer that never
    comes) — so the listing must not offer it (a row the download gate
    would 404 anyway) and must not queue the background pass for it
    (a worker hung like that is lost from the pool for the process's
    life: four FIFOs and no outbox on any session ever gets its sha).
    Pinned both at the listing and in the pool itself (defense in depth:
    a job that reaches the pool with a non-regular path must come back,
    not hang)."""
    import os as _os

    watch, sf = _files(tmp_path)
    (sf.outbox / "mine.txt").write_bytes(b"mine")
    _os.mkfifo(sf.outbox / "trap")
    entries, _delta = watch.sweep("s1", sf)
    assert [e["path"] for e in entries] == ["mine.txt"], "a FIFO is not an artifact"
    with store._sha_lock:
        # a job for the regular file may be in flight; one for the FIFO must
        # not exist at all
        assert all(k[1] != "trap" for k in store._SHA_PENDING), (
            "no background pass may be queued for a FIFO"
        )

    # the pool's own guard: a job for a non-regular path must not open it
    key = ("s1", "trap", 0, time.time())
    with store._sha_lock:
        store._SHA_PENDING.add(key)
    store._process_outbox_version(key, str(sf.outbox / "trap"))
    with store._sha_lock:
        assert key not in store._SHA_PENDING, "the job must come back, not hang"
        assert key not in store._SHA_CACHE, "a FIFO has no bytes to hash"


def test_sweeps_of_different_sessions_do_not_share_a_lock(tmp_path):
    """The lock is per session, not global: two sessions' sweeps must not
    queue behind each other (a walk of one chatty outbox — and the
    background passes it triggers — must not delay the other sessions'
    deltas), while the timer's sweep and a listing fetch of the SAME
    session still serialize on its snapshot."""
    watch, sf = _files(tmp_path)
    assert watch._lock_for("s1") is watch._lock_for("s1")
    assert watch._lock_for("s1") is not watch._lock_for("s2")

    # a sweep of s1 in flight (holding its lock) must not block a sweep of
    # s2: under one global lock it would wait the whole hold out
    gate = threading.Event()

    def hold_s1():
        with watch._lock_for("s1"):
            gate.set()
            time.sleep(0.3)

    t = threading.Thread(target=hold_s1)
    t.start()
    assert gate.wait(5.0)
    started = time.monotonic()
    entries, _delta = watch.sweep("s2", _files(tmp_path)[1])
    assert time.monotonic() - started < 0.2, "s2's sweep must not wait for s1's"
    t.join()
    assert [e["path"] for e in entries] == []

    # forget drops the lock entry too: bounded by the live-session count
    watch.sweep("s1", sf)
    watch.forget("s1")
    assert "s1" not in watch._locks


def test_a_mimetype_is_sniffed_once_per_version(tmp_path, monkeypatch):
    """The listing runs every sweep; re-reading 8 KB of every file each time
    is SSD wear for a number that only moves when the file does. The sniff
    rides the SAME background pass as the hash: the row's mime is null until
    the pass lands (the delta publishes the fill-in), and no later sweep
    re-reads a version it already knows."""
    calls: list[str] = []
    monkeypatch.setattr(store, "_MIME_CACHE", {})
    monkeypatch.setattr(store, "sniff_mime", lambda path, **k: calls.append(path) or "text/plain")
    watch, sf = _files(tmp_path)
    (sf.outbox / "a.txt").write_bytes(b"hello")
    entries, delta = watch.sweep("s1", sf)
    assert [e["mime"] for e in entries] == [None], "null until the background pass lands"
    assert delta is None  # first sweep: seed
    _settled()  # the background pass (hash + sniff) finishes
    entries, delta = watch.sweep("s1", sf)
    assert [e["mime"] for e in entries] == ["text/plain"]
    # the fill-in from null is the one change it ever publishes
    assert [c["path"] for c in delta["changed"]] == ["a.txt"]
    assert delta["changed"][0]["mime"] == "text/plain"
    _entries, delta = watch.sweep("s1", sf)
    assert delta is None, "a known version costs no re-read"
    assert len(calls) == 1

    (sf.outbox / "a.txt").write_bytes(b"hello world")  # a new version
    watch.sweep("s1", sf)
    _settled()
    entries, _ = watch.sweep("s1", sf)
    assert [e["mime"] for e in entries] == ["text/plain"]
    assert len(calls) == 2
