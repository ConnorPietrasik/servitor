"""The session registry — SQLite, the ONLY source of session truth.

State machine (validated — a crash mid-release must not hand out an
unswept slot):

    allocated ── spawn ok ──▶ live ── release step 1 ──▶ draining
    live ─────────────────────────────────────────────────▶ cleaning*
    draining ──────────────────────────────────────────▶ cleaning
    cleaning ── sweep verified empty ──▶ free
    cleaning ── sweep failed ──▶ (slot quarantined; row stays draining)

    * live→cleaning: release of a never-spawned allocated row.

The `free` row is a DEAD row: slot released, transcript areas deleted.
The API treats released sessions as gone (the list excludes them, the
detail 404s) — the row lingers only so operators/doctor can see what
recently ran on a slot — until a registry GC deletes it (Lifecycle.gc_tick
→ delete_released, after the retention window).
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# registry states
ALLOCATED = "allocated"
LIVE = "live"
DRAINING = "draining"
CLEANING = "cleaning"
FREE = "free"

_STATES = (ALLOCATED, LIVE, DRAINING, CLEANING, FREE)

# allowed transitions (from, to)
_ALLOWED: tuple[tuple[str, str], ...] = (
    (ALLOCATED, LIVE),
    (ALLOCATED, DRAINING),  # release before the first spawn ever succeeded
    (LIVE, DRAINING),
    (DRAINING, CLEANING),
    (LIVE, CLEANING),       # release of a live row whose process is already dead
    (CLEANING, FREE),
)


class RegistryError(RuntimeError):
    pass


class InvalidTransition(RegistryError):
    pass


@dataclass
class SessionRow:
    id: str
    display_name: str
    slot: str
    state: str
    stable: bool = False
    interrupted: bool = False
    session_file: Optional[str] = None
    config_version: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    update_waiting: bool = False
    loaded_extensions: tuple[str, ...] = ()
    new_extensions: tuple[str, ...] = ()

    def is_running_or_awaiting_release(self) -> bool:
        """'16 slots = 16 live sessions (running OR awaiting release)'."""
        return self.state in (ALLOCATED, LIVE, DRAINING, CLEANING)


class Registry:
    def __init__(self, path: str | Path):
        self.path = str(path)
        p = Path(self.path)
        if p.parent != Path("."):
            p.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self._create()

    def _create(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
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
            CREATE INDEX IF NOT EXISTS idx_sessions_slot ON sessions(slot);
            CREATE INDEX IF NOT EXISTS idx_sessions_state ON sessions(state);
            CREATE TABLE IF NOT EXISTS versions (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                ts REAL NOT NULL
            );
            """
        )
        self.db.commit()

    # -- version stamps (what the guest actually runs) -----------------

    def set_version(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO versions(key, value, ts) VALUES(?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value, ts=excluded.ts",
            (key, value, time.time()),
        )
        self.db.commit()

    def get_version(self, key: str) -> str | None:
        cur = self.db.execute("SELECT value FROM versions WHERE key=?", (key,))
        row = cur.fetchone()
        return row["value"] if row else None

    def get_versions(self) -> dict[str, str]:
        cur = self.db.execute("SELECT key, value FROM versions")
        return {r["key"]: r["value"] for r in cur.fetchall()}

    # -- rows ---------------------------------------------------------------

    @staticmethod
    def _row_to_session(r: sqlite3.Row) -> SessionRow:
        return SessionRow(
            id=r["id"],
            display_name=r["display_name"],
            slot=r["slot"],
            state=r["state"],
            stable=bool(r["stable"]),
            interrupted=bool(r["interrupted"]),
            session_file=r["session_file"],
            config_version=r["config_version"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
            last_activity=r["last_activity"],
            update_waiting=bool(r["update_waiting"]),
            loaded_extensions=tuple(json.loads(r["loaded_extensions"])),
            new_extensions=tuple(json.loads(r["new_extensions"])),
        )

    def upsert_session(
        self,
        session_id: str,
        *,
        display_name: str | None = None,
        slot: str,
        state: str = ALLOCATED,
        config_version: str | None = None,
    ) -> SessionRow:
        now = time.time()
        existing = self.get(session_id)
        if existing is None:
            self.db.execute(
                "INSERT INTO sessions (id, display_name, slot, state, created_at,"
                " updated_at, last_activity, config_version)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    session_id,
                    display_name or session_id[:8],
                    slot,
                    state,
                    now,
                    now,
                    now,
                    config_version,
                ),
            )
        else:
            # a released row is never re-allocated: session ids are uuids,
            # minted once
            if existing.state == FREE:
                raise RegistryError(f"session {session_id} is released; mint a new id")
            self.db.execute(
                "UPDATE sessions SET slot=?, state=?, updated_at=?, config_version=?"
                " WHERE id=?",
                (slot, state, now, config_version, session_id),
            )
        self.db.commit()
        row = self.get(session_id)
        assert row is not None
        return row

    def get(self, session_id: str) -> Optional[SessionRow]:
        r = self.db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return self._row_to_session(r) if r else None

    def list_sessions(self, *, include_released: bool = True) -> list[SessionRow]:
        q = "SELECT * FROM sessions"
        if not include_released:
            q += f" WHERE state != '{FREE}'"
        rows = self.db.execute(q + " ORDER BY created_at").fetchall()
        return [self._row_to_session(r) for r in rows]

    def session_for_slot(self, slot: str) -> Optional[SessionRow]:
        """The live-or-allocating row owning a slot (None if the slot is
        held by a released row or no one)."""
        for r in self.db.execute(
            "SELECT * FROM sessions WHERE slot=? AND state != ? ORDER BY updated_at DESC",
            (slot, FREE),
        ).fetchall():
            return self._row_to_session(r)
        return None

    # -- state machine --------------------------------------------------------

    def set_state(self, session_id: str, state: str, *, cause: str = "") -> SessionRow:
        row = self.get(session_id)
        if row is None:
            raise RegistryError(f"unknown session {session_id}")
        if state not in _STATES:
            raise RegistryError(f"unknown state {state}")
        if row.state == state:
            return row  # idempotent
        if (row.state, state) not in _ALLOWED:
            raise InvalidTransition(f"{session_id}: {row.state} -> {state} ({cause})")
        self.db.execute(
            "UPDATE sessions SET state=?, updated_at=? WHERE id=?",
            (state, time.time(), session_id),
        )
        self.db.commit()
        return self.get(session_id)  # type: ignore[return-value]

    def touch(self, session_id: str) -> None:
        """Activity bump."""
        self.db.execute(
            "UPDATE sessions SET last_activity=?, updated_at=? WHERE id=?",
            (time.time(), time.time(), session_id),
        )
        self.db.commit()

    # -- fields ---------------------------------------------------------------

    def set_session_file(self, session_id: str, path: Optional[str]) -> None:
        self.db.execute(
            "UPDATE sessions SET session_file=?, updated_at=? WHERE id=?",
            (path, time.time(), session_id),
        )
        self.db.commit()

    def set_stable(self, session_id: str, stable: bool) -> None:
        self.db.execute(
            "UPDATE sessions SET stable=?, updated_at=? WHERE id=?",
            (int(stable), time.time(), session_id),
        )
        self.db.commit()

    def mark_interrupted(self, session_id: str, interrupted: bool = True) -> None:
        self.db.execute(
            "UPDATE sessions SET interrupted=?, updated_at=? WHERE id=?",
            (int(interrupted), time.time(), session_id),
        )
        self.db.commit()

    def set_update_waiting(self, session_id: str, flag: bool) -> None:
        self.db.execute(
            "UPDATE sessions SET update_waiting=?, updated_at=? WHERE id=?",
            (int(flag), time.time(), session_id),
        )
        self.db.commit()

    def set_extensions(
        self, session_id: str, loaded: tuple[str, ...], new: tuple[str, ...]
    ) -> None:
        self.db.execute(
            "UPDATE sessions SET loaded_extensions=?, new_extensions=?, updated_at=?"
            " WHERE id=?",
            (json.dumps(list(loaded)), json.dumps(list(new)), time.time(), session_id),
        )
        self.db.commit()

    def delete(self, session_id: str) -> bool:
        """Remove a row entirely (a failed start that never became a
        session). Returns True if a row was removed."""
        cur = self.db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        self.db.commit()
        return cur.rowcount > 0

    def delete_released(self, older_than: float) -> int:
        """Registry GC: drop dead rows older than the cutoff."""
        cur = self.db.execute(
            "DELETE FROM sessions WHERE state=? AND updated_at<?", (FREE, older_than)
        )
        self.db.commit()
        return cur.rowcount

    def close(self) -> None:
        self.db.close()
