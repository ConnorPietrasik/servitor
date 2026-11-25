"""Disk-pressure eviction is ordered by what a session HOLDS.

Age alone is the wrong order: when the volume is nearly full, releasing the
oldest session because it is the oldest — and it holds 3 MB — destroys a
session for nothing while the meter barely moves. The pass must take the
largest reclaimable footprint first, and it must never consider a stable or
running session, or a keep (which lives outside every session area and is not
releasable at all).

The bytes are deduped *within* a session; a blob shared between two sessions
is counted for both, which is optimistic for ordering. The volume's own meter,
not this estimate, decides when the pass stops — pinned so the estimate is
not mistaken for an accounting.
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

from app.iso.layout import state_paths
from app.lifecycle import Lifecycle


def _rows(*specs):
    """(id, last_activity) in the order release would consider them."""
    return [
        SimpleNamespace(id=sid, last_activity=when, state="live", slot="slot1", stable=False)
        for sid, when in specs
    ]


def _order(tmp_path, rows):
    lc = SimpleNamespace(cfg=SimpleNamespace(state=state_paths(str(tmp_path))))
    return [(row.id, footprint) for row, footprint in Lifecycle._eviction_order(lc, rows)]


def _put(tmp_path, sid, area, name, size):
    state = state_paths(str(tmp_path))
    root = {"outbox": state.outbox_for, "scratch": state.scratch_for, "inbox": state.inbox_for}[area]
    d = root(sid)
    Path(d).mkdir(parents=True, exist_ok=True)
    (Path(d) / name).write_bytes(b"x" * size)



def test_the_biggest_footprint_goes_first_not_the_oldest(tmp_path):
    _put(tmp_path, "small-old", "outbox", "note.md", 1024)
    _put(tmp_path, "big-young", "scratch", "video.bin", 5 * 1024 * 1024)
    _put(tmp_path, "middle", "outbox", "data.csv", 100 * 1024)
    rows = _rows(("small-old", 100.0), ("big-young", 300.0), ("middle", 200.0))
    order = _order(tmp_path, rows)
    assert [sid for sid, _ in order] == ["big-young", "middle", "small-old"]
    assert order[0][1] == (1, 5 * 1024 * 1024)


def test_oldest_breaks_a_tie(tmp_path):
    _put(tmp_path, "newer", "outbox", "a.bin", 4096)
    _put(tmp_path, "older", "outbox", "b.bin", 4096)
    order = _order(tmp_path, _rows(("newer", 500.0), ("older", 100.0)))
    assert [sid for sid, _ in order] == ["older", "newer"]


def test_a_hardlinked_copy_is_counted_once_within_a_session(tmp_path):
    """Two links to one inode in one session's areas is one set of bytes — the
    figure shown is what release reports as discarded, not what `du` sums."""
    state = state_paths(str(tmp_path))
    _put(tmp_path, "s1", "outbox", "a.bin", 64 * 1024)
    Path(state.scratch_for("s1")).mkdir(parents=True, exist_ok=True)
    os.link(Path(state.outbox_for("s1"), "a.bin"), Path(state.scratch_for("s1"), "a-copy.bin"))
    order = _order(tmp_path, _rows(("s1", 1.0)))
    assert order[0][1] == (2, 64 * 1024)  # two files, one set of bytes


def test_an_empty_session_is_last_not_skipped(tmp_path):
    """A candidate with nothing to free is still releasable — the pass stops on
    the volume's meter, so an empty session must be ordered, not dropped (a
    skipped row would leave the pass spinning on a full volume forever)."""
    rows = _rows(("empty", 1.0), ("full", 2.0))
    _put(tmp_path, "full", "scratch", "x.bin", 2048)
    order = _order(tmp_path, rows)
    assert [sid for sid, _ in order] == ["full", "empty"]
