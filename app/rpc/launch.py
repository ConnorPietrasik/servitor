"""The launch line and env allowlist — ONE constant surface.

CI asserts the exact argv by this builder's golden output, plus the
conformance sentinel tests as a standing regression (an argv check alone
would not catch a future pi default change).

Hard rules encoded here:
- ``--no-approve`` always; ``--approve`` NEVER appears.
- ``--no-context-files`` always (injection's favorite filenames).
- exactly one session-dir mechanism: the ``--session-dir`` flag, identically
  at spawn/respawn/probe; NEVER ``PI_CODING_AGENT_SESSION_DIR``.
- first spawn ``--session-id <minted uuid>``; respawn/probe
  ``--session <recorded sessionFile>`` — captured verbatim from get_state.
- NEVER ``--api-key`` (the key rides env only — invariant 5).
- NEVER ``PI_OFFLINE`` (disables only catalog refresh; buys nothing for a
  static models.json deploy).
- NEVER ``--no-extensions`` (baseline extension loading is deliberate;
  the compensating control is the new-extension warning).
- the search extension loads by explicit ``-e`` path — the one surface the
  agent cannot rewrite.
- env is an explicit allowlist, never the app's inherited environment.
"""
from __future__ import annotations

from dataclasses import dataclass

#: Fixed flags, in order. The order is part of the golden argv.
LAUNCH_FLAGS: tuple[str, ...] = ("--mode", "rpc", "--no-approve", "--no-context-files")

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
) -> Launch:
    """Build the exact launch line for a spawn, respawn, or probe.

    ``session_id`` (first spawn) and ``session_file`` (respawn/probe) are
    mutually exclusive and exactly one must be given.
    """
    if (session_id is None) == (session_file is None):
        raise ValueError("exactly one of session_id / session_file is required")

    argv: list[str] = [node_bin, cli_js, *LAUNCH_FLAGS, "--session-dir", session_dir]
    if session_id is not None:
        argv += ["--session-id", session_id]
    else:
        assert session_file is not None
        argv += ["--session", session_file]
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
    if exa_api_key is not None:
        env.append(("EXA_API_KEY", exa_api_key))
    env.append(("PI_WEB_ACCESS_CACHE_ROOT", web_cache_root))

    # the allowlist is closed: nothing else, ever
    keys = [k for k, _ in env]
    assert all(k in ENV_ALLOWLIST for k in keys), keys
    assert not (set(keys) & set(FORBIDDEN_ENV)), set(keys) & set(FORBIDDEN_ENV)
    for forbidden in FORBIDDEN_ARGV:
        assert forbidden not in argv, forbidden

    return Launch(argv=tuple(argv), env=tuple(env))
