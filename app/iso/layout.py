"""Guest layout constants and state-volume layout.

Two distinct kinds of path live here, and only here:

1. **Image-baked guest paths** — defined by the guest image build
   (``deploy/guest/``); these constants mirror it. ``/opt/toolkit`` and
   the session tree roots must stay free of AGENTS.md/SYSTEM.md/.pi
   context files (they are overlay lowers / cwd ancestors of every
   session).
2. **State-volume areas** — relative to ``SERVITOR_ROOT`` (the only
   env-supplied path). Computed by :func:`state_paths`; never hardcoded
   elsewhere.

Nothing in this module is an address or a port.
"""
from __future__ import annotations

from dataclasses import dataclass

# --- image-baked guest paths -----------------------------------------------

# Baked toolkit — overlay lower of every session workdir. Root-owned 0755,
# a plain directory, never a mount. Must contain no context files
# (image gate asserts).
TOOLKIT_DIR = "/opt/toolkit"

# pi-web-access install root. Created by the sync pipeline (stage +
# rename), never baked into the image.
PI_EXT_DIR = "/opt/pi-ext/pi-web-access"

# npm prefix for the pinned pi install. The absolute CLI path is a
# constant in the harness — never PATH resolution.
PI_PREFIX = "/opt/servitor/pi"
NODE_BIN = "/usr/bin/node"  # LTS, pinned by the image build
PI_CLI_JS = f"{PI_PREFIX}/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js"

# App install (baked into the image; sync updates only agent-config/ + lib/).
APP_HOME = "/opt/servitor/app"

# 0600 env file consumed by the systemd unit (the key slice).
ENV_FILE = "/etc/servitor/servitor.env"

# --- slots ------------------------------------------------------------------

# 16 static slot uids, one per live session. Fixed uids, real passwd homes,
# created at image build; uids are discovered from passwd at runtime.
SLOT_NAMES: tuple[str, ...] = tuple(f"slot{i}" for i in range(1, 17))
SLOT_HOME = "/home/{slot}"

# The DOCTOR PROBE slot — the one slot OUTSIDE the app's pool. The doctor's
# no-prompt spawn check owns it exclusively, so the app's uid-gated kills
# (boot reconciliation, respawn, release strays) can never SIGTERM the
# probe's pi child. Created at image build (uid 9000).
PROBE_SLOT = "slot0"

# --- state volume -----------------------------------------------------------

#: Area names under SERVITOR_ROOT, in the order they are created.
STATE_AREAS: tuple[str, ...] = ("config", "lib", "inbox", "scratch", "sessions")

#: Context-file names that must never exist under the session tree root's
#: ancestors or the toolkit lower (--no-context-files removes them from
#: loading; the doctor asserts the chains are free of them).
CONTEXT_FILE_NAMES: tuple[str, ...] = ("AGENTS.md", "CLAUDE.md", ".pi")


@dataclass(frozen=True)
class StatePaths:
    """Absolute state-volume paths derived from SERVITOR_ROOT."""

    root: str
    config: str  # versioned dirs config/v<N>/ + active pointer
    lib: str  # durable, human-curated; never GC'd
    inbox: str  # inbox/<session>/
    scratch: str  # scratch/<session>/ (overlay uppers, venv, web cache)
    sessions: str  # sessions/<session>/ (pi transcripts, flat)

    def inbox_for(self, session_id: str) -> str:
        return f"{self.inbox}/{session_id}"

    def scratch_for(self, session_id: str) -> str:
        return f"{self.scratch}/{session_id}"

    def sessions_for(self, session_id: str) -> str:
        return f"{self.sessions}/{session_id}"


def state_paths(root: str) -> StatePaths:
    root = root.rstrip("/")
    return StatePaths(
        root=root,
        config=f"{root}/config",
        lib=f"{root}/lib",
        inbox=f"{root}/inbox",
        scratch=f"{root}/scratch",
        sessions=f"{root}/sessions",
    )


@dataclass(frozen=True)
class SessionMountPaths:
    """Per-session scratch layout.

    Mounted at allocate, before any spawn/respawn; live for the session
    lifetime (idle-kill never unmounts — only release does).
    """

    session_id: str
    scratch: str  # scratch/<s>/
    workdir: str  # merged workdir (the recorded cwd)
    upper: str  # workdir overlay upper
    work: str  # workdir overlay workdir
    agent_dir: str  # merged agent dir (PI_CODING_AGENT_DIR)
    agent_upper: str
    agent_work: str
    web_cache: str  # PI_WEB_ACCESS_CACHE_ROOT

    @classmethod
    def for_session(cls, session_id: str, scratch_root: str) -> "SessionMountPaths":
        scratch = f"{scratch_root}/{session_id}"
        return cls(
            session_id=session_id,
            scratch=scratch,
            workdir=f"{scratch}/workdir",
            upper=f"{scratch}/upper",
            work=f"{scratch}/work",
            agent_dir=f"{scratch}/agent-dir",
            agent_upper=f"{scratch}/agent-upper",
            agent_work=f"{scratch}/agent-work",
            web_cache=f"{scratch}/web-cache",
        )
