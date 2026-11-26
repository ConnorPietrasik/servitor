"""The launch line and env allowlist — ONE constant surface.

CI asserts the exact argv by this builder's golden output, plus the
conformance sentinel tests as a standing regression (an argv check alone
would not catch a future pi default change).

Hard rules encoded here:
- ``--no-approve`` always; ``--approve`` NEVER appears.
- ``--no-context-files`` always.
- exactly one session-dir mechanism: ``--session-dir``, identically at
  spawn/respawn/probe; NEVER ``PI_CODING_AGENT_SESSION_DIR``.
- first spawn ``--session-id <minted uuid>``; respawn/probe
  ``--session <recorded sessionFile>`` (captured verbatim from get_state).
- NEVER ``--api-key`` (the key rides env only).
- NEVER ``PI_OFFLINE``; NEVER ``--no-extensions`` (baseline extension
  loading is deliberate; the compensating control is the new-extension
  warning).
- the search extension loads by explicit ``-e`` path — the one surface
  the agent cannot rewrite.
- a strict session is a launch-line boundary: ``--tools`` pinned to the
  read-only allowlist, ``--exclude-tools mcp__*``, the replacement
  ``--system-prompt STRICT_SYSTEM.md`` — all three together, no ``-e``
  extension, no EXA/web-cache env. The agent cannot rewrite its own argv.
- env is an explicit allowlist, never the app's inherited environment.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

#: Fixed flags, in order. The order is part of the golden argv.
LAUNCH_FLAGS: tuple[str, ...] = ("--mode", "rpc", "--no-approve", "--no-context-files")

#: Strict sessions: the COMPLETE, closed tool allowlist — the read-only
#: builtins only. A plain --tools list REPLACES the whole selection (it
#: beats settings.json defaultTools, also across /reload), so everything
#: not named here (bash, edit, write, web tools, codemode) is gone.
STRICT_TOOLS: tuple[str, ...] = ("read", "grep", "find", "ls")

#: A plain --tools list does not strip MCP tools (the synced config
#: declares none today); the pattern strips whatever a future mcp.json
#: would add, keeping the strict surface closed.
STRICT_EXCLUDE_TOOLS: tuple[str, ...] = ("mcp__*",)

#: The strict session's replacement system prompt: synced with the config
#: (the versioned lower), delivered by absolute path on argv — the one
#: surface the agent cannot rewrite. --system-prompt REPLACES the
#: agent-dir SYSTEM.md entirely (pi reads it or the file, never both).
STRICT_SYSTEM_NAME = "STRICT_SYSTEM.md"

#: Env keys a strict subprocess never gets: nothing loads them without the
#: search extension (the key stays env-only per subprocess — a strict
#: subprocess does not call search).
STRICT_ENV_DROPS: tuple[str, ...] = ("EXA_API_KEY", "PI_WEB_ACCESS_CACHE_ROOT")

#: Env keys that may ever appear in a pi subprocess environment, in order.
ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USER",
    "LANG",
    "PI_CODING_AGENT_DIR",
    "OMNISSIAH_API_KEY",
    "EXA_API_KEY",
    "PI_WEB_ACCESS_CACHE_ROOT",
)

#: Values that must never appear in argv (audit helpers for the gates).
FORBIDDEN_ARGV: tuple[str, ...] = (
    "--approve",
    "--api-key",
    "--no-extensions",
)

#: Env keys that must never be set for a pi subprocess.
FORBIDDEN_ENV: tuple[str, ...] = (
    "PI_CODING_AGENT_SESSION_DIR",
    "PI_OFFLINE",
)


@dataclass(frozen=True)
class Launch:
    """A fully-resolved launch: argv + env. Immutable; golden-testable."""

    argv: tuple[str, ...]
    env: tuple[tuple[str, str], ...]

    @property
    def env_dict(self) -> dict[str, str]:
        return dict(self.env)


def build_launch(
    *,
    node_bin: str,
    cli_js: str,
    session_dir: str,
    session_id: str | None = None,
    session_file: str | None = None,
    extension_paths: tuple[str, ...],
    home: str,
    user: str,
    agent_dir: str,
    omnissiah_api_key: str,
    exa_api_key: str | None,
    web_cache_root: str,
    path: str = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    lang: str = "C.UTF-8",
    strict: bool = False,
    strict_system_path: str | None = None,
) -> Launch:
    """Build the exact launch line for a spawn, respawn, or probe.
    ``session_id`` (first spawn) and ``session_file`` (respawn/probe) are
    mutually exclusive and exactly one must be given.

    ``strict`` pins the session to the read-only tool allowlist with no
    search extension and no EXA/web-cache env; ``strict_system_path`` is
    the absolute path of that config version's STRICT_SYSTEM.md (the
    replacement system prompt). The coupling is fail-closed: strict
    without the prompt path, the path without strict, a path that does not
    name STRICT_SYSTEM.md, or any extension on a strict line is a
    ValueError — the boundary lives here, in one place."""
    if (session_id is None) == (session_file is None):
        raise ValueError("exactly one of session_id / session_file is required")
    if strict and strict_system_path is None:
        raise ValueError("a strict launch requires strict_system_path")
    if strict and os.path.basename(strict_system_path) != STRICT_SYSTEM_NAME:
        raise ValueError(
            f"strict_system_path must name {STRICT_SYSTEM_NAME}: {strict_system_path}"
        )
    if not strict and strict_system_path is not None:
        raise ValueError("strict_system_path is a strict-session mechanism")
    if strict and extension_paths:
        raise ValueError(
            f"a strict launch loads no extensions: {extension_paths}"
        )

    argv: list[str] = [node_bin, cli_js, *LAUNCH_FLAGS, "--session-dir", session_dir]
    if session_id is not None:
        argv += ["--session-id", session_id]
    else:
        assert session_file is not None
        argv += ["--session", session_file]
    if strict:
        assert strict_system_path is not None
        argv += ["--tools", ",".join(STRICT_TOOLS)]
        argv += ["--exclude-tools", ",".join(STRICT_EXCLUDE_TOOLS)]
        argv += ["--system-prompt", strict_system_path]
    for ext in extension_paths:
        argv += ["-e", ext]

    env: list[tuple[str, str]] = [
        ("PATH", path),
        ("HOME", home),
        ("USER", user),
        ("LANG", lang),
        ("PI_CODING_AGENT_DIR", agent_dir),
        ("OMNISSIAH_API_KEY", omnissiah_api_key),
    ]
    if not strict:
        if exa_api_key is not None:
            env.append(("EXA_API_KEY", exa_api_key))
        env.append(("PI_WEB_ACCESS_CACHE_ROOT", web_cache_root))

    # the allowlist is closed: nothing else, ever (a strict launch is the
    # allowlist minus the web-extension keys)
    keys = [k for k, _ in env]
    assert all(k in ENV_ALLOWLIST for k in keys), keys
    assert not (set(keys) & set(FORBIDDEN_ENV)), set(keys) & set(FORBIDDEN_ENV)
    if strict:
        assert not (set(keys) & set(STRICT_ENV_DROPS)), keys
    for forbidden in FORBIDDEN_ARGV:
        assert forbidden not in argv, forbidden

    return Launch(argv=tuple(argv), env=tuple(env))
