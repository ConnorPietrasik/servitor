"""Delivery store: uploads (staged → verified → atomic rename into the
inbox), outbox, manifests.

Guarantees: no truncation, no caps (uploads stream to a spooled temp
file, hashed incrementally); byte-identical (the sha256 is computed
while streaming and the VERIFIED part is what lands in the inbox); MIME
by magic bytes, never the extension; no half-files (crash drill) — an
upload is visible only after it is staged, verified, and renamed into
the session inbox (atomic: staging and inbox share the state volume's
filesystem). A crash before that leaves the part in staging, swept by
the boot GC — nothing half-visible, and no second copy is kept around.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.iso.spawn import SlotIdentity

log = logging.getLogger("servitor.delivery")

class DeliveryError(RuntimeError):
    pass


class FileNotDelivered(DeliveryError):
    pass


def sniff_mime(path: str, head_bytes: int = 8192) -> str:
    """MIME by magic bytes (never the extension)."""
    try:
        import magic  # python-magic (deps)

        with open(path, "rb") as f:
            head = f.read(head_bytes)
        return magic.from_buffer(head, mime=True)
    except Exception:
        return "application/octet-stream"


@dataclass
class DeliveredFile:
    name: str
    sha256: str
    size: int
    mime: str
    ts: float


class Staging:
    """Spooled upload staging: one temp file per upload, on the state
    volume (disk, never RAM). Unregistered dirs are swept at boot."""

    def __init__(self, root: Path):
        self.root = Path(root) / "staging"
        self.root.mkdir(parents=True, exist_ok=True)

    def new(self) -> tuple[str, Path, hashlib.sha256]:
        upload_id = uuid.uuid4().hex
        d = self.root / upload_id
        d.mkdir(parents=True)
        return upload_id, d / "part", hashlib.sha256()

    def remove(self, upload_id: str) -> None:
        shutil.rmtree(self.root / upload_id, ignore_errors=True)

    def gc_unregistered(self, registered: set[str]) -> list[str]:
        """Boot/gc sweep: staging dirs with no registered upload."""
        swept: list[str] = []
        try:
            entries = os.listdir(self.root)
        except OSError:
            return swept
        for name in entries:
            if name not in registered:
                shutil.rmtree(self.root / name, ignore_errors=True)
                swept.append(name)
        return swept


# ---------------------------------------------------------------------- #
# outbox sha256: lazy background hash, cached per (size, mtime)
# ---------------------------------------------------------------------- #

_sha_lock = threading.Lock()
_SHA_CACHE: dict[tuple, str] = {}
_SHA_PENDING: set[tuple] = set()


def _hash_outbox_file(key: tuple, path: str) -> None:
    """Background: sha256 a single outbox file (1 MB chunks)."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        with _sha_lock:
            _SHA_CACHE[key] = h.hexdigest()
    except OSError:
        pass  # file gone mid-hash: the next listing re-checks
    finally:
        with _sha_lock:
            _SHA_PENDING.discard(key)


class SessionFiles:
    """One session's delivery state.

    Layout (the mount contract):
    - inbox:  ``state/inbox/<s>`` (the bind source; uploads land here)
    - outbox: ``scratch/<s>/workdir/outbox`` (agent artifacts; inside the
      merged workdir — only meaningful while the session is mounted)
    - manifest: ``state/sessions/<s>/files.json`` (the delivered-uploads
      registry; the only thing that makes an upload API-visible)
    """

    def __init__(
        self,
        *,
        session_dir: str | Path,
        inbox: str | Path,
        outbox: str | Path,
        slot: SlotIdentity | None,
    ):
        self.session_dir = Path(session_dir)
        self.inbox = Path(inbox)
        self.outbox = Path(outbox)
        self.manifest_path = self.session_dir / "files.json"
        self.slot = slot
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.inbox.mkdir(parents=True, exist_ok=True)
        self._chown(self.inbox)
        # outbox is created lazily: it lives in the merged workdir and is
        # (re)created by mount_session; mkdir here only matters unmounted
        # (harmless: it lands in the overlay upper)

    def _chown(self, p: Path) -> None:
        if self.slot is not None:
            try:
                os.chown(p, self.slot.uid, self.slot.gid)
            except OSError:
                pass

    # --- manifest ------------------------------------------------------- #

    def read_manifest(self) -> dict:
        try:
            return json.loads(self.manifest_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {"uploads": []}

    def register_upload(self, f: DeliveredFile) -> None:
        man = self.read_manifest()
        man["uploads"] = [u for u in man.get("uploads", []) if u["name"] != f.name]
        man["uploads"].append(
            {"name": f.name, "sha256": f.sha256, "size": f.size, "mime": f.mime, "ts": f.ts}
        )
        tmp = self.manifest_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(man, indent=1))
        os.replace(tmp, self.manifest_path)
        self._chown(self.manifest_path)

    # --- delivery -------------------------------------------------------- #

    def deliver(self, part: Path, name: str) -> None:
        """Hand the verified staged file to the inbox: chown to the slot,
        then atomic rename. Staging and inbox share the state volume's
        filesystem, so the final name appears whole and slot-owned in one
        step — the agent never sees a root-owned or partially-written
        file, and a crash before the rename leaves the part in staging
        for the boot GC, never a half file under the real name."""
        safe = _safe_name(name)
        dest = self.inbox / safe
        self._chown(part)
        os.replace(part, dest)  # atomic: same filesystem
        try:
            os.chmod(dest, 0o644)
        except OSError:
            pass
        self._chown(dest)

    # --- listing / reads -------------------------------------------------- #

    def list_outbox(self) -> list[dict]:
        # sha256 is computed LAZILY in a background thread, cached per
        # (session, path, size, mtime): a multi-GB artifact must not be
        # re-hashed on every listing. The UI shows whatever is cached
        # (null while the first hash is still running).
        sid = self.session_dir.name
        out = []
        if not self.outbox.is_dir():
            return out
        for dirpath, _dirnames, filenames in os.walk(self.outbox):
            for name in sorted(filenames):
                p = Path(dirpath) / name
                rel = p.relative_to(self.outbox).as_posix()
                try:
                    st = p.stat()
                except OSError:
                    continue
                key = (sid, rel, st.st_size, st.st_mtime)
                sha = _SHA_CACHE.get(key)
                if sha is None:
                    with _sha_lock:
                        if key not in _SHA_PENDING:
                            _SHA_PENDING.add(key)
                            threading.Thread(
                                target=_hash_outbox_file,
                                args=(key, str(p)),
                                daemon=True,
                            ).start()
                out.append(
                    {
                        "path": rel,
                        "size": st.st_size,
                        "mtime": st.st_mtime,
                        "sha256": sha,
                        "mime": sniff_mime(str(p)) if st.st_size else "application/octet-stream",
                    }
                )
        return out

    def outbox_path(self, rel: str) -> Path:
        """Resolve a requested outbox path; escape => FileNotDelivered."""
        p = (self.outbox / rel).resolve()
        if not str(p).startswith(str(self.outbox.resolve())) or not p.is_file():
            raise FileNotDelivered(rel)
        return p

    def upload_path(self, name: str) -> Path:
        p = (self.inbox / _safe_name(name)).resolve()
        if not str(p).startswith(str(self.inbox.resolve())) or not p.is_file():
            raise FileNotDelivered(name)
        return p


def _safe_name(name: str) -> str:
    """A single path component, no traversal, no leading dot."""
    name = name.replace("\\", "/").split("/")[-1].strip()
    name = name.lstrip(".")
    if not name or name in ("", "."):
        raise DeliveryError(f"unsafe upload name: {name!r}")
    return name
