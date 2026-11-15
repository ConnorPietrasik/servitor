"""Servitor environment contract — the single source for env var names.

PLAN stage-0 gate: every env var the app reads is named in `.env.example`,
and nothing hardcodes an address/port/path. This module is the one place
env vars are read; app code goes through :class:`Config` and never touches
``os.environ`` directly (the gate AST-scans for violations).

Settled values (PLAN §13) appear here as *documented* defaults for the
ops parameters only. Addresses, ports, subnets, and the state-volume root
have no defaults: they come from the env file, full stop.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

# Every env var the app (and the deploy scripts it drives) reads, exactly
# once. test/check_contract.py asserts this set equals the names in
# .env.example and that no env read in app/ escapes it.
ENV_NAMES: tuple[str, ...] = (
    # --- machine credentials (the only secrets; invariant 5: env-only) ---
    "OMNISSIAH_API_KEY",  # router bearer (host's LLM_API_KEY, PLAN §13)
    "EXA_API_KEY",  # search extension key; env-first in pi-web-access 0.47.0
    # --- network contract (the edge is the only auth gate) ---
    "SERVITOR_BIND",
    "SERVITOR_PORT",
    "SERVITOR_EDGE_SUBNET",
    "SERVITOR_OPS_SUBNET",
    "SERVITOR_BROWSER_ORIGIN",  # the one origin allowed to use the API from a browser (CORS)
    # --- state volume ---
    "SERVITOR_ROOT",
    # --- lifecycle / delivery (ops parameters, never capability caps) ---
    "SERVITOR_GC_IDLE_DAYS",
    "SERVITOR_DRAIN_GRACE_SECS",
    "SERVITOR_COALESCE_MS",
    "SERVITOR_HEARTBEAT_SECS",
    "SERVITOR_SSE_RING_BYTES",  # per-session SSE delivery cap (memory bound; default 50 MB)
    # --- version selections (npm semver specs, exact by default; D6) ---
    "SERVITOR_PI_VERSION",
    "SERVITOR_PI_WEB_ACCESS_VERSION",
    # --- deploy-time intent (doctor asserts; A4) ---
    "SERVITOR_INTENDED_MODEL_ID",
    "SERVITOR_INTENDED_CONTEXT_WINDOW",
)


class ConfigError(RuntimeError):
    """A required env var is missing or malformed (fail closed)."""


@dataclass(frozen=True)
class Config:
    """Validated runtime configuration. Immutable after load."""

    # network contract
    bind: str  # ingress address; no default — env-only
    port: int  # the one published port; no default — env-only
    edge_subnet: str | None  # None = standalone (loopback behind a local proxy)
    ops_subnet: str | None  # None = standalone
    browser_origin: str | None  # the one browser origin (CORS allow-list); None = API-only
    # state volume
    root: str  # real ext4 path; no default — env-only
    # lifecycle / delivery (settled defaults, PLAN §13 — ops, not caps)
    gc_idle_days: int = 14
    drain_grace_secs: int = 90
    coalesce_ms: int = 1000
    heartbeat_secs: int = 15
    sse_ring_bytes: int | None = None  # per-session SSE cap; None = 50 MB default
    # version selections (semver specs, exact by default)
    pi_version: str = "1.5.2"
    pi_web_access_version: str = "0.47.0"
    # deploy-time intent
    intended_model_id: str = "qwen35"
    intended_context_window: int = 250000
    # machine credentials (may be absent at app boot; spawn fails closed
    # without OMNISSIAH_API_KEY — model.id == "unknown", PLAN §4)
    omnissiah_api_key: str | None = None
    exa_api_key: str | None = None


def _require(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"{name} is required (see .env.example). No default: addresses, "
            f"ports, and paths come from the env file only."
        )
    return value


def _optional(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name, "").strip()
    return value or None


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    value = env.get(name, "").strip()
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from exc
    if parsed <= 0:
        raise ConfigError(f"{name} must be positive, got {parsed}")
    return parsed


def _optional_int(env: Mapping[str, str], name: str) -> int | None:
    """An optional positive integer (absent/empty → None)."""
    value = env.get(name, "").strip()
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from exc
    if parsed <= 0:
        raise ConfigError(f"{name} must be positive, got {parsed}")
    return parsed


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Load and validate config from an env mapping (defaults to os.environ)."""
    if env is None:
        env = os.environ

    port_raw = _require(env, "SERVITOR_PORT")
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise ConfigError(f"SERVITOR_PORT must be an integer, got {port_raw!r}") from exc
    if not (1 <= port <= 65535):
        raise ConfigError(f"SERVITOR_PORT out of range: {port}")

    # INVARIANT (PLAN §5): the app runs privileged (root on the guest).
    # The agent never is. This is a boot assertion, not a hint.
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        # Laptop dev may run unprivileged for pure-protocol work, but any
        # iso/rpc spawn path re-asserts root before setuid/mount work.
        pass

    return Config(
        bind=_require(env, "SERVITOR_BIND"),
        port=port,
        edge_subnet=_optional(env, "SERVITOR_EDGE_SUBNET"),
        ops_subnet=_optional(env, "SERVITOR_OPS_SUBNET"),
        browser_origin=_optional(env, "SERVITOR_BROWSER_ORIGIN"),
        root=_require(env, "SERVITOR_ROOT"),
        gc_idle_days=_int(env, "SERVITOR_GC_IDLE_DAYS", 14),
        drain_grace_secs=_int(env, "SERVITOR_DRAIN_GRACE_SECS", 90),
        coalesce_ms=_int(env, "SERVITOR_COALESCE_MS", 1000),
        heartbeat_secs=_int(env, "SERVITOR_HEARTBEAT_SECS", 15),
        sse_ring_bytes=_optional_int(env, "SERVITOR_SSE_RING_BYTES"),
        pi_version=_require(env, "SERVITOR_PI_VERSION"),
        pi_web_access_version=_require(env, "SERVITOR_PI_WEB_ACCESS_VERSION"),
        intended_model_id=_require(env, "SERVITOR_INTENDED_MODEL_ID"),
        intended_context_window=_int(
            env, "SERVITOR_INTENDED_CONTEXT_WINDOW", 250000
        ),
        omnissiah_api_key=_optional(env, "OMNISSIAH_API_KEY"),
        exa_api_key=_optional(env, "EXA_API_KEY"),
    )


def load_http_config(env: Mapping[str, str] | None = None, *, root: str) -> Config:
    """Config for the HTTP front door (stage 3).

    Requires only the network contract (SERVITOR_BIND / SERVITOR_PORT —
    no defaults, env-only) and the state root (from the --state arg when
    the env file does not set it). The ops parameters and version picks
    fall back to their documented defaults: the pi install itself is
    resolved by the caller (CLI), so the semver specs are not needed to
    serve HTTP.
    """
    if env is None:
        env = os.environ
    port_raw = _require(env, "SERVITOR_PORT")
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise ConfigError(f"SERVITOR_PORT must be an integer, got {port_raw!r}") from exc
    if not (1 <= port <= 65535):
        raise ConfigError(f"SERVITOR_PORT out of range: {port}")
    return Config(
        bind=_require(env, "SERVITOR_BIND"),
        port=port,
        edge_subnet=_optional(env, "SERVITOR_EDGE_SUBNET"),
        ops_subnet=_optional(env, "SERVITOR_OPS_SUBNET"),
        browser_origin=_optional(env, "SERVITOR_BROWSER_ORIGIN"),
        root=root,
        gc_idle_days=_int(env, "SERVITOR_GC_IDLE_DAYS", 14),
        drain_grace_secs=_int(env, "SERVITOR_DRAIN_GRACE_SECS", 90),
        coalesce_ms=_int(env, "SERVITOR_COALESCE_MS", 1000),
        heartbeat_secs=_int(env, "SERVITOR_HEARTBEAT_SECS", 15),
        sse_ring_bytes=_optional_int(env, "SERVITOR_SSE_RING_BYTES"),
        pi_version=_optional(env, "SERVITOR_PI_VERSION") or "1.5.2",
        pi_web_access_version=_optional(env, "SERVITOR_PI_WEB_ACCESS_VERSION") or "0.47.0",
        intended_model_id=_optional(env, "SERVITOR_INTENDED_MODEL_ID") or "qwen35",
        intended_context_window=_int(env, "SERVITOR_INTENDED_CONTEXT_WINDOW", 250000),
        omnissiah_api_key=_optional(env, "OMNISSIAH_API_KEY"),
        exa_api_key=_optional(env, "EXA_API_KEY"),
    )
