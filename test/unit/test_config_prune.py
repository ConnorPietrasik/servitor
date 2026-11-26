"""Config version prune: a version dies with its last session.

- prune removes version dirs that are neither active, nor the recorded
  rollback target (.previous-active), nor pinned — in BOTH naming
  schemes (the boot path's v<N>, the host sync's content-addressed
  c<sha12>) — and leaves non-version entries alone.
- prune skips its whole pass while a fresh .swapping- marker exists
  (a sync-side swap in flight: the new version dir is about to become
  active); a crash-stale marker (past the TTL) is ignored.
- an app-side swap (stage_and_swap) and prune serialize on the config
  lock; a version a second swap orphaned is reclaimed by the next prune.
- the registry pin is live rows only (ALLOCATED/LIVE; terminal rows and
  NULLs pin nothing), and the spawn-time recording (set_config_version)
  persists.
- the release-level pin set = row pins ∪ mounted overlay lowerdirs under
  the config root; release_session's step 6.5 prunes the unreferenced
  rest (root-free: the sweep/stray primitives faked, as in
  test_symlink_escape).
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

from app.iso import configswap
from app.iso.layout import state_paths
from app.iso.slots import SlotPool
from app.iso.sweep import SweepResult
from app.registry import ALLOCATED, DRAINING, FREE, LIVE, Registry

# content-addressed version names (the sync's c<sha12> scheme)
CA = "c" + "a" * 12
CB = "c" + "b" * 12
CC = "c" + "c" * 12


def _mk_version(root: Path, name: str) -> None:
    d = root / name
    d.mkdir()
    (d / "SYSTEM.md").write_text(f"config {name}\n")


def _mk_active(root: Path, name: str) -> None:
    (root / "active").symlink_to(name)


# ---------------------------------------------------------------------- #
# configswap.prune                                                       #
# ---------------------------------------------------------------------- #


def test_prune_deletes_unreferenced_keeps_active_pinned_rollback(tmp_path):
    root = tmp_path / "config"
    root.mkdir()
    for v in ("v1", "v2", CA, CB, CC):
        _mk_version(root, v)
    _mk_active(root, CB)
    (root / ".previous-active").write_text("v2")
    (root / "notes.txt").write_text("not a version\n")

    res = configswap.prune(str(root), pinned={CC})

    assert res.pruned == [CA, "v1"]  # version_dirs sorts: c-names before v-names
    assert res.errors == []
    # kept: active, the rollback target, the pinned one
    assert (root / "v2").is_dir() and (root / CB).is_dir() and (root / CC).is_dir()
    # non-version entries are never touched
    assert (root / "notes.txt").exists() and (root / ".previous-active").exists()


def test_prune_missing_root_is_a_noop(tmp_path):
    res = configswap.prune(str(tmp_path / "no-such-root"), set())
    assert res.pruned == [] and res.errors == []


def test_prune_skips_pass_while_swap_marker_fresh(tmp_path):
    root = tmp_path / "config"
    root.mkdir()
    _mk_version(root, "v1")
    _mk_version(root, CB)
    _mk_active(root, CB)
    marker = root / f"{configswap.SWAP_MARKER_PREFIX}{'d' * 12}"
    marker.touch()

    res = configswap.prune(str(root), set())
    assert res.pruned == [] and (root / "v1").is_dir()  # the pass was skipped

    # a crash-stale marker (past the TTL) is debris: the pass proceeds
    old = time.time() - configswap.SWAP_MARKER_TTL_SEC - 1
    os.utime(marker, (old, old))
    res = configswap.prune(str(root), set())
    assert res.pruned == ["v1"] and not (root / "v1").exists()


def test_swap_orphan_is_reclaimed(tmp_path):
    root = tmp_path / "config"
    root.mkdir()
    src = tmp_path / "src"
    src.mkdir()
    (src / "SYSTEM.md").write_text("first\n")
    v1 = configswap.stage_and_swap(str(root), str(src))
    assert v1 == "v1"
    (src / "SYSTEM.md").write_text("second\n")
    v2 = configswap.stage_and_swap(str(root), str(src))
    assert v2 == "v2"

    res = configswap.prune(str(root), set())

    # the first swap's version is orphaned by the second (no session ran
    # on it) — reclaimed; the active one survives
    assert res.pruned == ["v1"]
    assert (root / "v2").is_dir() and configswap.active_version(str(root)) == "v2"


# ---------------------------------------------------------------------- #
# registry pins                                                           #
# ---------------------------------------------------------------------- #


def test_registry_pin_is_live_rows_only(tmp_path):
    reg = Registry(str(tmp_path / "registry.db"))
    reg.upsert_session("s-live", slot="slot1", state=LIVE, config_version="v3")
    reg.upsert_session("s-alloc", slot="slot2", state=ALLOCATED, config_version="v3")
    reg.upsert_session("s-drain", slot="slot3", state=DRAINING, config_version="v1")
    reg.upsert_session("s-free", slot="slot4", state=FREE, config_version="v1")
    reg.upsert_session("s-null", slot="slot5", state=LIVE)  # NULL: never recorded

    assert reg.pinned_config_versions() == {"v3"}


def test_set_config_version_persists_as_the_pin(tmp_path):
    reg = Registry(str(tmp_path / "registry.db"))
    reg.upsert_session("s1", slot="slot1", state=ALLOCATED)
    assert reg.get("s1").config_version is None

    reg.set_config_version("s1", CC)

    assert reg.get("s1").config_version == CC
    assert reg.pinned_config_versions() == {CC}


# ---------------------------------------------------------------------- #
# the pin-set union + release step 6.5                                    #
# ---------------------------------------------------------------------- #


def test_pinned_versions_unions_rows_and_mounts(tmp_path, monkeypatch):
    from app.iso import release as release_mod

    root = tmp_path / "config"
    root.mkdir()
    monkeypatch.setattr(
        "app.iso.mounts.overlay_lower_dirs",
        lambda: {str(root / "v9"), "/opt/toolkit", str(tmp_path / "elsewhere" / "v1")},
    )

    pinned = release_mod.pinned_versions(str(root), {"v1"})

    # only the lowerdir UNDER the config root joins — as the version NAME
    # (prune matches names, not paths); row pins pass through
    assert pinned == {"v1", "v9"}


def _release_fixture(tmp_path, monkeypatch, pinned: set[str]):
    """A hermetic release_session: real steps (areas, slot state machine,
    config prune), faked process/fs primitives (no root, no / sweep)."""
    from app.iso import release as release_mod

    monkeypatch.setattr(
        "app.iso.slots.pwd.getpwnam",
        lambda name: SimpleNamespace(
            pw_uid=9001, pw_gid=9001, pw_dir=f"/home/{name}"
        ),
    )
    state = state_paths(str(tmp_path / "state"))
    cfg_root = Path(state.config)
    cfg_root.mkdir(parents=True)
    _mk_version(cfg_root, "v1")  # an orphaned old version
    _mk_version(cfg_root, "v2")
    _mk_active(cfg_root, "v2")
    for area in ("lib", "inbox", "outbox", "staging", "scratch", "sessions", "blobs", "keep"):
        Path(state.root, area).mkdir(parents=True, exist_ok=True)

    pool = SlotPool(("slot1",))
    slot = pool.allocate("s1")

    class Hooks:
        def __init__(self):
            self.drained: list[str] = []
            self.released: list[tuple[str, bool]] = []

        def mark_draining(self, session_id: str, reason: str) -> None:
            self.drained.append(session_id)

        def mark_released(self, session_id: str, *, quarantined: bool, detail: str) -> None:
            self.released.append((session_id, quarantined))

        def pinned_config_versions(self) -> set[str]:
            return pinned

    monkeypatch.setattr(release_mod, "kill_strays_by_uid", lambda uid: [])
    monkeypatch.setattr(
        release_mod, "sweep_by_owner",
        lambda uid, excludes=(), **k: SweepResult(uid=uid, verified_clean=True),
    )
    return release_mod, state, pool, slot, Hooks()


async def test_release_prunes_the_unreferenced_version(tmp_path, monkeypatch):
    """Step 6.5: with no other session pinning v1, the release reclaims
    it; v2 (active) survives; the slot is freed (a prune outcome does not
    quarantine)."""
    release_mod, state, pool, slot, hooks = _release_fixture(tmp_path, monkeypatch, set())

    outcome = await release_mod.release_session(
        session_id="s1", slot=slot, state=state, pool=pool, hooks=hooks,
        driver=None, mount_ops=None, reason="test",
    )

    assert outcome.released is True and outcome.quarantined is False
    assert outcome.config_pruned == ["v1"]
    assert not (Path(state.config) / "v1").exists()
    assert (Path(state.config) / "v2").is_dir()
    assert pool.get("slot1").state == "free"


async def test_release_keeps_a_version_other_sessions_pin(tmp_path, monkeypatch):
    """A version another live session recorded is kept even when nothing
    is mounted on it here (the row pin is the durable half of the union)."""
    release_mod, state, pool, slot, hooks = _release_fixture(
        tmp_path, monkeypatch, {"v1"}
    )

    outcome = await release_mod.release_session(
        session_id="s1", slot=slot, state=state, pool=pool, hooks=hooks,
        driver=None, mount_ops=None, reason="test",
    )

    assert outcome.released is True
    assert outcome.config_pruned == []
    assert (Path(state.config) / "v1").is_dir()
    assert (Path(state.config) / "v2").is_dir()
