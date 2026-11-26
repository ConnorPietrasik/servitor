"""Unit: the strict session — registry flag, schema migration, launch-line
wiring (root-free).

Real registry (sqlite in tmp_path), real config swap (tmp tree), real
build_launch through Lifecycle._launch. The live behavior (the tools array
on the wire, the executed argv, the child env) is the conformance suite's
job (test/conformance/test_strict_mode.py) — it needs root + a real pi.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from app.iso import configswap
from app.iso.layout import SessionMountPaths, state_paths
from app.iso.slots import Slot, SlotPool
from app.lifecycle import Lifecycle, LifecycleConfig, PiInstall
from app.registry import ALLOCATED, LIVE, Registry

import app.iso.slots as slotsmod

SID = "s1"


def _real_pool(monkeypatch) -> SlotPool:
    monkeypatch.setattr(
        slotsmod,
        "discover_slot",
        lambda name: Slot(name=name, uid=1000, gid=1000, home=f"/home/{name}"),
    )
    return SlotPool(("slot1",))


def _make_lc(tmp_path, monkeypatch, *, exa_key: str | None = "exa-k") -> Lifecycle:
    reg = Registry(str(tmp_path / "registry.db"))
    cfg = LifecycleConfig(
        state=state_paths(str(tmp_path)),
        pool=_real_pool(monkeypatch),
        registry=reg,
        pi=PiInstall(
            node_bin="/usr/bin/node",
            cli_js="/x/cli.js",
            ext_path="/opt/pi-ext/pi-web-access",
        ),
        api_key="k",
        exa_key=exa_key,
    )
    return Lifecycle(cfg)


def _stage_config(lc: Lifecycle, *, with_strict_file: bool = True) -> str:
    """A versioned config version with (optionally) the strict prompt."""
    src = Path(lc.cfg.state.root) / "cfgsrc"
    src.mkdir(parents=True, exist_ok=True)
    (src / "SYSTEM.md").write_text("base prompt\n")
    (src / "models.json").write_text("{}")
    (src / "settings.json").write_text("{}")
    p = src / "STRICT_SYSTEM.md"
    if with_strict_file:
        p.write_text("strict prompt\n")
    elif p.exists():
        p.unlink()
    return configswap.stage_and_swap(lc.cfg.state.config, str(src))


# ---------------------------------------------------------------------- #
# registry: the flag + the migration                                      #
# ---------------------------------------------------------------------- #


def test_registry_strict_roundtrip_and_default(tmp_path):
    reg = Registry(str(tmp_path / "registry.db"))
    reg.upsert_session("a", slot="slot1", state=ALLOCATED, strict=True)
    reg.upsert_session("b", slot="slot1", state=ALLOCATED)
    assert reg.get("a").strict is True
    assert reg.get("b").strict is False  # default: the pre-strict shape
    reg.close()


def test_registry_migration_adds_strict_column(tmp_path):
    """A pre-strict volume (no column) must open, backfill 0, and accept
    strict rows — the guest state volume outlives this change."""
    p = tmp_path / "registry.db"
    db = sqlite3.connect(p)
    db.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            slot TEXT NOT NULL,
            state TEXT NOT NULL,
            stable INTEGER NOT NULL DEFAULT 0,
            interrupted INTEGER NOT NULL DEFAULT 0,
            session_file TEXT,
            config_version TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            last_activity REAL NOT NULL,
            update_waiting INTEGER NOT NULL DEFAULT 0,
            loaded_extensions TEXT NOT NULL DEFAULT '[]',
            new_extensions TEXT NOT NULL DEFAULT '[]'
        );
        CREATE TABLE versions (key TEXT PRIMARY KEY, value TEXT NOT NULL, ts REAL NOT NULL);
        """
    )
    now = time.time()
    db.execute(
        "INSERT INTO sessions (id, display_name, slot, state, created_at,"
        " updated_at, last_activity) VALUES ('old1','old','slot1','live',?,?,?)",
        (now, now, now),
    )
    db.commit()
    db.close()

    reg = Registry(p)
    assert reg.get("old1").strict is False  # backfilled, never strict
    reg.upsert_session("new1", slot="slot1", state=ALLOCATED, strict=True)
    assert reg.get("new1").strict is True
    reg.close()


def test_strict_is_create_time_the_update_branch_never_flips_it(tmp_path):
    reg = Registry(str(tmp_path / "registry.db"))
    reg.upsert_session("s", slot="slot1", state=ALLOCATED, strict=True)
    # a re-upsert (non-free row) must not be able to clear the flag
    reg.upsert_session("s", slot="slot1", state=LIVE, strict=False)
    assert reg.get("s").strict is True
    reg.close()


# ---------------------------------------------------------------------- #
# lifecycle: the strict launch line, resolved from the row                #
# ---------------------------------------------------------------------- #


def test_launch_strict_argv_and_env(tmp_path, monkeypatch):
    lc = _make_lc(tmp_path, monkeypatch)
    lc.cfg.registry.upsert_session(SID, slot="slot1", state=ALLOCATED, strict=True)
    _stage_config(lc)
    sp = SessionMountPaths.for_session(SID, lc.cfg.state.scratch)

    l = lc._launch(SID, None, sp)
    argv = list(l.argv)
    assert argv[argv.index("--tools") + 1] == "read,grep,find,ls"
    assert argv[argv.index("--exclude-tools") + 1] == "mcp__*"
    p = argv[argv.index("--system-prompt") + 1]
    assert p.endswith("/STRICT_SYSTEM.md")
    # the prompt is the version this session runs: the active lower here
    # (no live mount in the unit world)
    active = configswap.active_dir(lc.cfg.state.config)
    assert p == f"{active}/STRICT_SYSTEM.md"
    assert "-e" not in argv  # no search extension
    # env: the model key stays env-only; the web-extension keys are gone
    assert l.env_dict["OMNISSIAH_API_KEY"] == "k"
    assert "EXA_API_KEY" not in l.env_dict
    assert "PI_WEB_ACCESS_CACHE_ROOT" not in l.env_dict


def test_launch_non_strict_unchanged(tmp_path, monkeypatch):
    lc = _make_lc(tmp_path, monkeypatch)
    lc.cfg.registry.upsert_session(SID, slot="slot1", state=ALLOCATED)
    _stage_config(lc)
    sp = SessionMountPaths.for_session(SID, lc.cfg.state.scratch)

    l = lc._launch(SID, None, sp)
    argv = list(l.argv)
    assert "--tools" not in argv
    assert "--system-prompt" not in argv
    assert "-e" in argv  # the search extension loads as before
    assert l.env_dict["EXA_API_KEY"] == "exa-k"
    assert "PI_WEB_ACCESS_CACHE_ROOT" in l.env_dict


def test_launch_strict_missing_prompt_file_fails_loud(tmp_path, monkeypatch):
    lc = _make_lc(tmp_path, monkeypatch)
    lc.cfg.registry.upsert_session(SID, slot="slot1", state=ALLOCATED, strict=True)
    _stage_config(lc, with_strict_file=False)
    sp = SessionMountPaths.for_session(SID, lc.cfg.state.scratch)
    with pytest.raises(RuntimeError, match="STRICT_SYSTEM.md"):
        lc._launch(SID, None, sp)


def test_record_extensions_strict_loads_none(tmp_path, monkeypatch):
    lc = _make_lc(tmp_path, monkeypatch)
    lc.cfg.registry.upsert_session(SID, slot="slot1", state=ALLOCATED, strict=True)
    lc.cfg.registry.upsert_session("s2", slot="slot1", state=ALLOCATED)
    _stage_config(lc)
    sp = SessionMountPaths.for_session(SID, lc.cfg.state.scratch)
    lc._record_extensions(SID, sp)
    assert lc.cfg.registry.get(SID).loaded_extensions == ()
    sp2 = SessionMountPaths.for_session("s2", lc.cfg.state.scratch)
    lc._record_extensions("s2", sp2)
    assert lc.cfg.registry.get("s2").loaded_extensions == ("pi-web-access",)
