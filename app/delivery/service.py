"""Delivery service — the app-level wiring over the store.

One Delivery per app instance (state-volume root). Upload:

    stream body → staging/<id>/part (disk, 1 MB chunks, sha256 as we go)
    → verify → chown to the slot → atomic rename into inbox/<name>
      (staging and inbox share the state volume's fs — one write of the
      bytes, no second copy, the final name appears whole and slot-owned)
    → session manifest (files.json) → API-visible

A crash at any step leaves nothing half-visible: boot GC sweeps every
staging dir; the manifest is the only registry of delivered uploads.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

from app.delivery.store import (
    DeliveredFile,
    DeliveryError,
    SessionFiles,
    Staging,
    sniff_mime,
)
from app.iso.layout import StatePaths
from app.iso.spawn import SlotIdentity

log = logging.getLogger("servitor.delivery")


class Delivery:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.staging = Staging(self.root)

    def files(self, state: StatePaths, session_id: str, slot: SlotIdentity | None) -> SessionFiles:
        """The session's delivery views (inbox bind source, outbox in the
        merged workdir, manifest in the session dir)."""
        return SessionFiles(
            session_dir=state.sessions_for(session_id),
            inbox=state.inbox_for(session_id),
            outbox=f"{state.scratch_for(session_id)}/workdir/outbox",
            slot=slot,
        )

    # ------------------------------------------------------------------ #
    # upload
    # ------------------------------------------------------------------ #

    async def upload(
        self,
        stream,
        name: str,
        session_files: SessionFiles,
    ) -> DeliveredFile:
        """Consume an async byte stream (request body) into a delivered
        file. No size limit (no 413, ever). A failure at any step never
        registers a partial file: the staging dir is unregistered until
        the manifest write succeeds, and boot GC sweeps it if we die
        mid-flight."""
        upload_id, part, sha = self.staging.new()
        try:
            size = 0
            with open(part, "ab") as f:
                async for chunk in stream:
                    if not isinstance(chunk, (bytes, bytearray)):
                        chunk = bytes(chunk)
                    if not chunk:
                        continue
                    f.write(chunk)
                    sha.update(chunk)
                    size += len(chunk)
                f.flush()
                os.fsync(f.fileno())
            digest = sha.hexdigest()
            mime = sniff_mime(str(part))
            await asyncio.to_thread(session_files.deliver, part, name)
            delivered = DeliveredFile(name=name, sha256=digest, size=size, mime=mime, ts=time.time())
            session_files.register_upload(delivered)
            return delivered
        finally:
            self.staging.remove(upload_id)

    # ------------------------------------------------------------------ #
    # GC
    # ------------------------------------------------------------------ #

    def gc_boot(self) -> dict:
        """At boot: sweep every staging dir (any survivor is an
        interrupted upload)."""
        swept = self.staging.gc_unregistered(set())
        log.info("delivery boot gc: swept %d staging dirs", len(swept))
        return {"staging_swept": swept}

