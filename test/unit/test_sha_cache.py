"""Unit: F37 — the outbox sha256 cache must be bounded.

``_SHA_CACHE`` is keyed ``(sid, rel, size, mtime)`` and the old code only
ever inserted: every file VERSION ever hashed (each rewrite of a growing
artifact between the 4 s outbox polls) kept its entry for the process's
life, and so did entries for deleted files.

Pinned here:
- churning one file never grows the cache past ``_SHA_CACHE_MAX``;
- the current version is still hashed and served (eviction is lossy only
  for superseded versions — an evicted live file is re-hashed once);
- a stable file keeps hitting the cache (no re-hash per poll).
"""
from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from pathlib import Path

import pytest

from app.delivery import store
from app.delivery.store import SessionFiles


def _files(tmp_path: Path) -> SessionFiles:
    sf = SessionFiles(
        session_dir=tmp_path / "sessions" / "s1",
        inbox=tmp_path / "inbox" / "s1",
        outbox=tmp_path / "outbox",
        slot=None,
    )
    sf.outbox.mkdir(parents=True)
    return sf


def _drain(timeout: float = 10.0) -> None:
    """Wait for all in-flight background hashes to finish."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with store._sha_lock:
            if not store._SHA_PENDING:
                return
        time.sleep(0.01)
    pytest.fail("background hashes did not finish in time")


@pytest.fixture
def _fresh_cache():
    saved_cache, saved_max = store._SHA_CACHE, store._SHA_CACHE_MAX
    store._SHA_CACHE, store._SHA_CACHE_MAX = OrderedDict(), 8
    yield store
    store._SHA_CACHE, store._SHA_CACHE_MAX = saved_cache, saved_max


def test_cache_bounded_under_rewrites(tmp_path, _fresh_cache):
    sf = _files(tmp_path)
    f = sf.outbox / "artifact.bin"

    for i in range(30):  # 30 versions, cap is 8
        f.write_bytes(bytes([i]) * (100 + i))
        sf.list_outbox()
        _drain()

    with _fresh_cache._sha_lock:
        assert len(_fresh_cache._SHA_CACHE) <= _fresh_cache._SHA_CACHE_MAX

    # the CURRENT version must still be hashed and served, not lost:
    # evicted versions are only re-fetched, correctness is intact
    rows = sf.list_outbox()
    _drain()
    rows = sf.list_outbox()
    assert rows[0]["sha256"] == hashlib.sha256(f.read_bytes()).hexdigest()


def test_stable_file_hits_cache_not_rehashed(tmp_path, _fresh_cache):
    sf = _files(tmp_path)
    f = sf.outbox / "stable.bin"
    f.write_bytes(b"hello outbox")

    sf.list_outbox()
    _drain()
    first = sf.list_outbox()[0]["sha256"]
    assert first == hashlib.sha256(b"hello outbox").hexdigest()

    # repeated polls of an unchanged file: same entry, no new pending work
    for _ in range(5):
        rows = sf.list_outbox()
        _drain()
        assert rows[0]["sha256"] == first
    with _fresh_cache._sha_lock:
        assert len(_fresh_cache._SHA_CACHE) == 1


def test_deleted_files_do_not_grow_cache_forever(tmp_path, _fresh_cache):
    sf = _files(tmp_path)
    for i in range(20):  # one file per version, each deleted after
        f = sf.outbox / f"v{i}.bin"
        f.write_bytes(bytes([i]) * 64)
        sf.list_outbox()
        _drain()
        f.unlink()
        sf.list_outbox()

    with _fresh_cache._sha_lock:
        assert len(_fresh_cache._SHA_CACHE) <= _fresh_cache._SHA_CACHE_MAX
