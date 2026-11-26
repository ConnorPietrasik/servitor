"""SERVITOR_GC_IDLE_DAYS must reach the running lifecycle.

The guest unit passes only --state/--config-src: without the wiring,
LifecycleConfig.gc_idle_days would come solely from the --gc-idle-days CLI
default, and an operator override would be a silent no-op (the
positive-value validation at boot makes it look honoured).

run_http is exercised directly with a stub app object and fake lazy imports
(uvicorn, app.api.app) — no server, no network, no fastapi required.
"""
from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace

from app.api.sse import EVENT_BUFFER_CAP
from app.delivery.watch import FileWatch
from app.main import App


class _FakeLifecycle:
    def __init__(self) -> None:
        # the CLI-fed defaults: the value that would win without the env wiring
        self.cfg = SimpleNamespace(
            sse_ring_bytes=None,
            gc_idle_days=14.0,
            files_sweep_sec=4.0,
            stable_grace_sec=60.0,
        )
        # minted at init with the pre-env cap, like the real Lifecycle:
        # the assertion below is that run_http re-applies the tuned cap
        self.global_hub = SimpleNamespace(cap=EVENT_BUFFER_CAP)
        self.files = FileWatch()

    def start_update_tick(self) -> None:
        pass


def _fake_app() -> SimpleNamespace:
    return SimpleNamespace(
        args=SimpleNamespace(state="/tmp/servitor-gc-idle-test"),
        lc=_FakeLifecycle(),
        http_server=None,
    )


def _patch_lazy_imports(monkeypatch) -> None:
    uv = types.ModuleType("uvicorn")

    class _Config:
        def __init__(self, app, **kw):
            self.app = app
            self.kw = kw

    class _Server:
        def __init__(self, config):
            self.config = config

        async def serve(self) -> None:
            return None

    uv.Config = _Config
    uv.Server = _Server
    monkeypatch.setitem(sys.modules, "uvicorn", uv)

    api = types.ModuleType("app.api.app")

    def build_app(app_state, cfg):
        return object()

    api.build_app = build_app
    monkeypatch.setitem(sys.modules, "app.api.app", api)


def _run_http(monkeypatch) -> SimpleNamespace:
    _patch_lazy_imports(monkeypatch)
    monkeypatch.setenv("SERVITOR_BIND", "127.0.0.1")
    monkeypatch.setenv("SERVITOR_PORT", "8931")
    app = _fake_app()
    asyncio.run(App.run_http(app))
    return app


def test_gc_idle_days_env_reaches_lifecycle(monkeypatch):
    """The documented operator override must reach the GC tick, not die
    at the Config -> LifecycleConfig boundary."""
    monkeypatch.setenv("SERVITOR_GC_IDLE_DAYS", "3")
    app = _run_http(monkeypatch)
    assert app.lc.cfg.gc_idle_days == 3.0


def test_gc_idle_days_defaults_to_14_without_env(monkeypatch):
    """Unset (the default deployment): the documented default of 14 days
    still holds."""
    monkeypatch.delenv("SERVITOR_GC_IDLE_DAYS", raising=False)
    app = _run_http(monkeypatch)
    assert app.lc.cfg.gc_idle_days == 14.0


def test_sse_ring_bytes_env_reaches_lifecycle(monkeypatch):
    """The sibling wiring that already worked (the pattern this fix
    mirrors) — keeps both caps asserted in one place. The global stream's
    hub is the one minted BEFORE the env lands on cfg: without the
    re-apply in run_http it would keep the 50 MB default while every
    session hub honoured the tuned value."""
    monkeypatch.setenv("SERVITOR_SSE_RING_BYTES", "123")
    app = _run_http(monkeypatch)
    assert app.lc.cfg.sse_ring_bytes == 123
    assert app.lc.global_hub.cap == 123


def test_file_sweep_knobs_reach_lifecycle(monkeypatch):
    """Same boundary, the file-watch knobs: the sweep interval drives the
    loop and the grace drives the `stable` badge — both must land, or the
    documented .env lines are decoration. ``grace`` reaching the FileWatch
    matters as much as the cfg field (the badge reads the watch, not the
    config)."""
    monkeypatch.setenv("SERVITOR_FILES_SWEEP_SEC", "11")
    monkeypatch.setenv("SERVITOR_STABLE_GRACE_SEC", "120")
    app = _run_http(monkeypatch)
    assert app.lc.cfg.files_sweep_sec == 11
    assert app.lc.cfg.stable_grace_sec == 120
    assert app.lc.files.grace == 120.0
