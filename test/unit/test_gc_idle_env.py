"""SERVITOR_GC_IDLE_DAYS must reach the running lifecycle.

Regression: load_http_config parsed and validated the env value into
Config.gc_idle_days, but run_http copied only cfg.sse_ring_bytes into the
lifecycle — LifecycleConfig.gc_idle_days came solely from the --gc-idle-days
CLI default (14.0), which the guest unit never passes (ExecStart is
--state/--config-src only). An operator override was a silent no-op, and
the positive-value validation at boot made it look honoured.

run_http is exercised directly with a stub app object and fake lazy imports
(uvicorn, app.api.app) — no server, no network, no fastapi required.
"""
from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace

from app.main import App


class _FakeLifecycle:
    def __init__(self) -> None:
        # mirrors the CLI-fed defaults: this is the value that would have
        # silently won without the env wiring
        self.cfg = SimpleNamespace(sse_ring_bytes=None, gc_idle_days=14.0)

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
    mirrors) — keeps both caps asserted in one place."""
    monkeypatch.setenv("SERVITOR_SSE_RING_BYTES", "123")
    app = _run_http(monkeypatch)
    assert app.lc.cfg.sse_ring_bytes == 123
