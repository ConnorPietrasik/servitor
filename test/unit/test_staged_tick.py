"""Unit: the GC tick is where a staged upload's TTL actually bites.

The boot sweep only sees what was already dead at boot. A client that walks
away while the app is up (radio dropped, tab closed) leaves its part in
staging — on a long-running service that is a multi-GB leak until the next
restart unless the tick sweeps it. Pinned: an abandoned upload past its TTL
goes on the tick, and an upload its client is still feeding does not.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import app.iso.slots as slotsmod
from app.delivery.service import Delivery
from app.delivery.upload import STAGED_TTL_SEC, Staged
from app.iso.layout import state_paths
from app.iso.slots import Slot, SlotPool
from app.lifecycle import Lifecycle, LifecycleConfig, PiInstall
from app.registry import Registry


def _make_lc(tmp_path, monkeypatch) -> tuple[Lifecycle, "object"]:
    reg = Registry(str(tmp_path / "registry.db"))
    monkeypatch.setattr(
        slotsmod,
        "discover_slot",
        lambda name: Slot(name=name, uid=1000, gid=1000, home=f"/home/{name}"),
    )
    cfg = LifecycleConfig(
        state=state_paths(str(tmp_path)),
        pool=SlotPool(("slot1",)),
        registry=reg,
        pi=PiInstall(node_bin="/usr/bin/node", cli_js="/x/cli.js", ext_path=None),
        api_key="k",
        delivery=Delivery(str(tmp_path)),
    )
    lc = Lifecycle(cfg)
    # no release pass may fire in these tests
    monkeypatch.setattr(lc, "_disk_near_full", lambda: False)
    return lc


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _backdate(staged: Staged, uid: str, seconds_ago: float) -> None:
    """The meta's `updated` is the only thing the TTL reads — backdate it
    directly (the write path always stamps now)."""
    meta = staged.root / uid / "meta.json"
    m = json.loads(meta.read_text())
    m["updated"] = time.time() - seconds_ago
    meta.write_text(json.dumps(m))


def test_tick_sweeps_a_staged_upload_its_client_abandoned(tmp_path, monkeypatch):
    lc = _make_lc(tmp_path, monkeypatch)
    staged = Staged(str(tmp_path), "s1")
    uid, off = staged.create("gone.bin")
    staged.append(uid, off, [b"x" * 1024])
    _backdate(staged, uid, STAGED_TTL_SEC + 3600)
    assert (staged.root / uid / "part").is_file()
    _run(lc.gc_tick())
    assert not (staged.root / uid).exists()


def test_tick_leaves_a_staged_upload_its_client_is_still_on(tmp_path, monkeypatch):
    lc = _make_lc(tmp_path, monkeypatch)
    staged = Staged(str(tmp_path), "s1")
    uid, off = staged.create("active.bin")
    staged.append(uid, off, [b"x" * 1024])
    _run(lc.gc_tick())
    assert staged.size(uid) == 1024, "an in-flight upload survives the tick"


def test_the_tick_removes_a_staging_dir_the_app_never_made(tmp_path, monkeypatch):
    """Junk under staging/ whose name is not a session id: the app did not
    put it there, so it is deleted. The sweep itself must not blow up on it
    — this pass runs FIRST in the GC tick (and again, unguarded, in the boot
    sweep), so a failure here would stall idle release, disk pressure, and
    every later tick, and crash app boot."""
    lc = _make_lc(tmp_path, monkeypatch)
    stray = Path(tmp_path) / "staging" / "not a session id"
    stray.mkdir(parents=True)
    (stray / "part").write_bytes(b"junk")
    assert lc.cfg.delivery.gc_staged() == ["not a session id"]
    assert not stray.exists()
    _run(lc.gc_tick())  # the full tick runs the same pass first: no raise


# ---------------------------------------------------------------------- #
# residue: a directory without a part is not an upload
# ---------------------------------------------------------------------- #


def test_a_staged_dir_without_a_part_is_residue_not_a_resume(tmp_path):
    """A crash between the adopt (the part moved into the store) and the
    drop leaves the meta behind. The resume list must not offer it back as
    a size-0 upload the client would re-send, and the sweep must not wait
    out the TTL for bytes that can never resume."""
    staged = Staged(str(tmp_path), "s1")
    uid, off = staged.create("zombie.bin")
    staged.append(uid, off, [b"x" * 16])
    (staged.root / uid / "part").unlink()  # the adopt, with the drop never run
    assert staged.list() == [], "a partless directory is not a resume"
    # swept even though its meta is fresh (within any TTL): nothing can
    # ever make it resumable
    assert staged.gc(ttl_sec=0.000001) == [uid]
    assert not (staged.root / uid).exists()


def test_gc_reports_only_what_it_actually_removed(tmp_path):
    """Junk goes by shape: a directory by rmtree, a non-directory entry —
    a stray file, a leftover symlink, a planted FIFO — by unlink (a FIFO
    is the one shape rmtree cannot take; routed to it the entry would sit
    under staging/ forever, the sweep retrying its no-op every tick). A
    removal is reported only when the entry is GONE after the sweep."""
    staged = Staged(str(tmp_path), "s1")
    staged.root.mkdir(parents=True, exist_ok=True)
    fifo = staged.root / "weird-name"  # not an upload id: the junk branch
    os.mkfifo(fifo)
    stray = staged.root / "another-stray"
    stray.mkdir()
    (stray / "x").write_bytes(b"x")
    assert sorted(staged.gc(ttl_sec=0.000001)) == ["another-stray", "weird-name"]
    assert not fifo.exists(), "a FIFO is a non-directory entry: unlink takes it"
    assert not stray.exists()
