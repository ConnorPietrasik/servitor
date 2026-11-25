"""Guest layout constants and state-volume layout.

Two distinct kinds of path live here, and only here:

1. **Image-baked guest paths** — defined by the guest image build
   (``deploy/guest/``); these constants mirror it. ``/opt/toolkit`` and
   the session tree roots must stay free of AGENTS.md/SYSTEM.md/.pi
   context files (they are overlay lowers / cwd ancestors of every
   session).
2. **State-volume areas** — relative to the state-volume root (the
   CLI's ``--state``). Computed by :func:`state_paths`; never hardcoded
   elsewhere. Every per-session area is one directory named by session id:
   ``inbox/`` and ``outbox/`` are the two BIND SOURCES the agent sees as
   ``./inbox`` (read-only: root-owned, in a root-owned directory) and
   ``./outbox`` (its handoff area); ``blobs/`` holds one copy of every
   delivered file's bytes, hardlinked into whichever sessions need them;
   ``keep/`` holds the bytes a human chose to keep, outside every session
   area so ``release()`` cannot reach them.

Nothing in this module is an address or a port.
"""
from __future__ import annotations

from dataclasses import dataclass

# --- image-baked guest paths -----------------------------------------------

# Baked toolkit — overlay lower of every session workdir. Root-owned 0755,
# a plain directory, never a mount. Must contain no context files (image gate
# asserts).
TOOLKIT_DIR = "/opt/toolkit"

# pi-web-access install root. Created by the sync pipeline (stage + rename),
# never baked into the image.
PI_EXT_DIR = "/opt/pi-ext/pi-web-access"

# npm prefix for the pinned pi install. The absolute CLI path is a constant in
# the harness — never PATH resolution.
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

#: The state-volume area names. The image creates the whole set at build
#: time (build.sh derives it from this tuple), and the service creates each
#: area on first use as well — a new area added here is therefore never
#: missing from an image or a first boot. No boot pass walks this tuple.
# staging: upload parts in flight — one-shot AND resumable, per session
#          (swept by TTL; a released session's parts are deleted with the
#          session — a released session's upload has no API left to resume it).
# blobs: content-addressed store (one copy of every upload's bytes).
# keep:    human-kept artifacts/uploads — outside every session area, so
#          release() cannot reach them and the owner sweep excludes them.
STATE_AREAS: tuple[str, ...] = (
    "config",
    "lib",
    "inbox",
    "outbox",
    "staging",
    "blobs",
    "keep",
    "scratch",
    "sessions",
)

#: Context-file names that must never exist under the session tree root's
#: ancestors or the toolkit lower (--no-context-files removes them from
#: loading; the doctor asserts the chains are free of them).
CONTEXT_FILE_NAMES: tuple[str, ...] = ("AGENTS.md", "CLAUDE.md", ".pi")


@dataclass(frozen=True)
class StatePaths:
    """Absolute state-volume paths derived from the state-volume root."""

    root: str
    config: str  # versioned dirs config/v<N>/ + active pointer
    lib: str  # durable, human-curated; never GC'd
    inbox: str  # inbox/<session>/ (bind source; root-owned, agent read-only)
    outbox: str  # outbox/<session>/ (bind source; the agent's handoff area)
    staging: str  # staging/<session>/ (resumable uploads in flight)
    blobs: str  # blobs/<sha[0:2]>/<sha> — one copy of every delivered file
    keep: str  # keep/<session>/<name> — human-kept bytes, never GC'd
    scratch: str  # scratch/<session>/ (overlay uppers, venv, web cache)
    sessions: str  # sessions/<session>/ (pi transcripts, flat)

    def inbox_for(self, session_id: str) -> str:
        return f"{self.inbox}/{session_id}"

    def outbox_for(self, session_id: str) -> str:
        return f"{self.outbox}/{session_id}"

    def staging_for(self, session_id: str) -> str:
        return f"{self.staging}/{session_id}"

    def scratch_for(self, session_id: str) -> str:
        return f"{self.scratch}/{session_id}"

    def sessions_for(self, session_id: str) -> str:
        return f"{self.sessions}/{session_id}"

    def keep_for(self, session_id: str) -> str:
        """The kept bytes a human chose FROM this session: the FULL session
        id is the bucket, so two sessions can never share one (an 8-char
        prefix would eventually collide, and the path IS the provenance).
        Names inside cannot collide across sessions either way.
        """
        return f"{self.keep}/{session_id}"

    def blob_for(self, sha256: str) -> str:
        """The blob path for a sha256 (two-level fanout: one directory per
        256 prefixes keeps a large store listable)."""
        return f"{self.blobs}/{sha256[:2]}/{sha256}"

def state_paths(root: str) -> StatePaths:
    root = root.rstrip("/")
    return StatePaths(
        root=root,
        config=f"{root}/config",
        lib=f"{root}/lib",
        inbox=f"{root}/inbox",
        outbox=f"{root}/outbox",
        staging=f"{root}/staging",
        blobs=f"{root}/blobs",
        keep=f"{root}/keep",
        scratch=f"{root}/scratch",
        sessions=f"{root}/sessions",
    )


@dataclass(frozen=True)
class SessionMountPaths:
    """Per-session scratch layout (paths under ``scratch/<s>/``)."""

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
