"""Doctor: the delivery-area self-checks (blob store, kept shelf, outbox bind).

These are the three things that can only be seen from the filesystem, and each
one fails in a way no request would ever surface:

- a blob whose name is not its own sha256 (or sitting in the wrong bucket) can
  never be found again — the store is addressed by content, so a misfiled blob
  is an upload that "vanished" while its bytes sit unreclaimable forever;
- a kept shelf that is not on the state volume is a keep that dies on the next
  boot, and nothing at request time would ever notice;
- a live session mounted without its outbox bind writes artifacts into the
  overlay upper: the app never lists them and they die with the scratch.

The mount check is simulated with an ``ismount`` stub (an unprivileged tree
cannot mount); everything else runs against a real tmp state volume.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

from app.doctor import FAIL, PASS, WARN, Report, check_delivery, check_outbox_mounts
from app.iso.layout import state_paths


def _store(root, *shas: str) -> None:
    for sha in shas:
        d = Path(state_paths(str(root)).blobs) / sha[:2]
        d.mkdir(parents=True, exist_ok=True)
        (d / sha).write_bytes(b"payload")


def _one(rep: Report, name: str) -> tuple[str, str]:
    hits = [r for r in rep.results if r.name == name]
    assert len(hits) == 1, [(r.name, r.status) for r in rep.results]
    return hits[0].status, hits[0].detail


# ---------------------------------------------------------------------- #
# the blob store's shape
# ---------------------------------------------------------------------- #


def test_a_correctly_addressed_blob_store_passes(tmp_path):
    sha = hashlib.sha256(b"payload").hexdigest()
    _store(tmp_path, sha)
    shelf = Path(state_paths(str(tmp_path)).keep) / "s1"
    shelf.mkdir(parents=True)
    os.link(Path(state_paths(str(tmp_path)).blobs, sha[:2], sha), shelf / "kept.bin")
    rep = Report()
    check_delivery(rep, str(tmp_path))
    status, detail = _one(rep, "delivery.blobs")
    assert status == PASS
    assert "1 blob(s)" in detail and "every blob linked" in detail
    status, detail = _one(rep, "delivery.keep")
    assert status == PASS and "1 kept file(s)" in detail


def test_a_misfiled_blob_fails_because_it_can_never_be_found_again(tmp_path):
    good = hashlib.sha256(b"payload").hexdigest()
    _store(tmp_path, good)
    blobs = Path(state_paths(str(tmp_path)).blobs)
    (blobs / "zz").mkdir()
    (blobs / "zz" / good).write_bytes(b"payload")            # wrong bucket
    (blobs / good[:2] / "deadbeef").write_bytes(b"payload")  # not an address
    rep = Report()
    check_delivery(rep, str(tmp_path))
    status, detail = _one(rep, "delivery.blobs")
    assert status == FAIL
    assert "not their own address" in detail


def test_unlinked_blobs_are_reported_as_reclaimable(tmp_path):
    sha = hashlib.sha256(b"payload").hexdigest()
    _store(tmp_path, sha)
    blob = Path(state_paths(str(tmp_path)).blobs, sha[:2], sha)
    os.utime(blob, (0, 0))  # mtime older than any grace window
    rep = Report()
    check_delivery(rep, str(tmp_path))
    status, detail = _one(rep, "delivery.blobs")
    # nlink is 1 (nothing links it): reclaimable. The utime above made the
    # MTIME ancient but the kernel stamped a fresh CTIME on the same write —
    # the GC's clock is max(mtime, ctime), so the GC holds this blob inside
    # the grace, and the doctor must agree: an alarm on mtime alone would
    # blame a healthy tick for a blob that is seconds from being re-linked.
    assert status == PASS
    assert "unlinked" in detail and "grace window" in detail
    assert "reclaim tick is not running" not in detail


def test_a_blob_old_on_both_clocks_flags_the_reclaim_tick(tmp_path, monkeypatch):
    """The alarm the previous test does not raise: an orphan whose mtime AND
    ctime are past the grace window is one the GC will not take either —
    which means the tick is not running (the service is down or broken).
    ctime cannot be backdated, so the clock moves instead: the doctor's own
    ``time.time`` is monkeypatched far into the future, where a real blob's
    both stamps are ancient."""
    import app.doctor as doctor_mod

    sha = hashlib.sha256(b"payload").hexdigest()
    _store(tmp_path, sha)
    monkeypatch.setattr(doctor_mod.time, "time", lambda: 10_000_000_000.0)
    Path(state_paths(str(tmp_path)).keep).mkdir(parents=True, exist_ok=True)
    rep = Report()
    check_delivery(rep, str(tmp_path))
    status, detail = _one(rep, "delivery.blobs")
    assert status == WARN
    assert "unlinked" in detail and "reclaim tick is not running" in detail


def test_an_in_flight_adopt_temp_is_not_a_misfiled_blob(tmp_path):
    """A keep/attach of an artifact the store has not held runs a multi-GB
    copy through ``blobs/.adopt-*`` — and the doctor runs live (the sync's
    post-push pass, the update's certification gate) while that copy is in
    flight. The temp shape is dead weight only at boot (the sweep takes it
    then); failing it would block or roll back a good deploy."""
    sha = hashlib.sha256(b"payload").hexdigest()
    _store(tmp_path, sha)
    blobs = Path(state_paths(str(tmp_path)).blobs)
    (blobs / ".adopt-0123456789ab").write_bytes(b"in flight")
    Path(state_paths(str(tmp_path)).keep).mkdir(parents=True, exist_ok=True)
    rep = Report()
    check_delivery(rep, str(tmp_path))
    status, detail = _one(rep, "delivery.blobs")
    assert status == PASS, detail
    assert "1 blob(s)" in detail and ".adopt-" not in detail


def test_a_missing_shelf_warns_and_a_broken_one_fails(tmp_path):
    rep = Report()
    check_delivery(rep, str(tmp_path / "nope"))  # no keep dir at all
    status, detail = _one(rep, "delivery.keep")
    assert status == "WARN" and "does not exist yet" in detail

    (tmp_path / "keep").write_text("not a directory")  # a shelf that cannot hold
    rep = Report()
    check_delivery(rep, str(tmp_path))
    status, detail = _one(rep, "delivery.keep")
    assert status == FAIL and "not a directory" in detail


def test_a_keep_on_another_device_fails(tmp_path, monkeypatch):
    """The shelf must sit on the state volume. A different st_dev means
    something else is mounted there — and a kept file there is not kept."""
    os.makedirs(state_paths(str(tmp_path)).keep, exist_ok=True)
    real = os.stat

    def fake_stat(path, *a, **kw):
        st = real(path, *a, **kw)
        if str(path).endswith("keep"):
            return SimpleNamespace(st_dev=st.st_dev + 1, st_mode=st.st_mode)
        return st

    monkeypatch.setattr(os, "stat", fake_stat)
    rep = Report()
    check_delivery(rep, str(tmp_path))
    status, detail = _one(rep, "delivery.keep")
    assert status == FAIL and "different device" in detail


# ---------------------------------------------------------------------- #
# the outbox bind on live sessions
# ---------------------------------------------------------------------- #


def _row(sid, state="live", slot="slot3"):
    return SimpleNamespace(id=sid, state=state, slot=slot)


def test_an_unmounted_session_says_nothing(tmp_path):
    """Unprivileged dev, or a session that was released: no workdir mount at
    all is not a broken mount plan."""
    reg = SimpleNamespace(list_sessions=lambda: [_row("s1")])
    rep = Report()
    check_outbox_mounts(rep, str(tmp_path), reg)
    assert _one(rep, "mounts.outbox")[0] == PASS


def test_a_workdir_mounted_without_the_outbox_bind_fails(tmp_path, monkeypatch):
    scratch = Path(tmp_path) / "scratch" / "s1"
    (scratch / "workdir").mkdir(parents=True)
    (scratch / "workdir" / "outbox").mkdir()
    monkeypatch.setattr(os.path, "ismount", lambda p: p.endswith("workdir"))
    reg = SimpleNamespace(list_sessions=lambda: [_row("s1")])
    rep = Report()
    check_outbox_mounts(rep, str(tmp_path), reg)
    status, detail = _one(rep, "mounts.outbox")
    assert status == FAIL and "s1" in detail


def test_a_session_with_both_mounts_passes(tmp_path, monkeypatch):
    scratch = Path(tmp_path) / "scratch" / "s1"
    (scratch / "workdir" / "outbox").mkdir(parents=True)
    monkeypatch.setattr(os.path, "ismount", lambda p: "/workdir" in p)
    reg = SimpleNamespace(list_sessions=lambda: [_row("s1"), _row("s2", state="free")])
    rep = Report()
    check_outbox_mounts(rep, str(tmp_path), reg)
    assert _one(rep, "mounts.outbox")[0] == PASS
