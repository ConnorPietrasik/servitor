"""Delivery store: uploads (staged → blob → inbox), outbox, manifests
(PLAN §3.4).

Guarantees (stage-3 gates):

- **No truncation, no caps**: uploads are streamed to a spooled temp
  file on the state volume (never buffered in memory), hashed
  incrementally; a 100 MB upload is served exactly like a 100 B one.
- **Byte-identical**: stored blobs are verified (sha256) before they are
  registered or delivered; downloads stream the stored bytes whole.
- **MIME by magic**: the content type comes from magic bytes, never the
  file extension (python-magic).
- **No half-blobs** (crash drill): an upload is visible to the API only
  after it is (a) fully staged, (b) verified, (c) moved to the content-
  addressed blob store (atomic rename on the same fs), (d) delivered to
  the session inbox (temp-write + rename), and (e) registered in the
  session manifest. A crash at any step leaves either nothing visible
  or the complete file; boot GC sweeps unregistered staging dirs and
  orphan inbox temp files.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.iso.spawn import SlotIdentity

log = logging.getLogger("servitor.delivery")

CHUNK = 1024 * 1024  # 1 MB stream chunks (memory stays flat)


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


class BlobStore:
    """Content-addressed blob store on the state volume (shared)."""

    def __init__(self, root: Path):
        self.root = Path(root) / "blobs"
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, sha: str) -> Path:
        return self.root / sha[:2] / sha[2:]

    def has(self, sha: str) -> bool:
        return self.path_for(sha).is_file()

    def register(self, src: Path, sha: str) -> Path:
        """Atomically move a verified staged file into the store.

        Same-filesystem rename: no partial blob can ever appear at the
        final path. Idempotent (an existing blob is kept)."""
        dest = self.path_for(sha)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            try:
                os.unlink(src)
            except OSError:
                pass
            return dest
        os.replace(src, dest)  # atomic on the same fs
        try:
            os.chmod(dest, 0o444)
        except OSError:
            pass
        return dest

    def gc_orphans(self, referenced: set[str]) -> list[str]:
        """Remove blobs no manifest references (best-effort, bounded)."""
        removed: list[str] = []
        if not self.root.is_dir():
            return removed  # no uploads yet (the dir is created on first register)
        for prefix in sorted(os.listdir(self.root)):
            pdir = self.root / prefix
            if not pdir.is_dir() or len(prefix) != 2:
                continue
            for name in sorted(os.listdir(pdir)):
                sha = prefix + name
                if sha not in referenced:
                    try:
                        os.chmod(pdir / name, 0o600)
                        os.unlink(pdir / name)
                        removed.append(sha)
                    except OSError:
                        pass
        return removed


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

    def finalize_path(self, upload_id: str) -> Path:
        return self.root / upload_id / "part"

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


class SessionFiles:
    """One session's delivery state.

    Layout (the stage-1 mount contract):
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

    def deliver(self, blob_path: Path, name: str) -> None:
        """Copy a verified blob into the inbox: temp-write + atomic
        rename (a crash mid-copy leaves an .incoming-* temp, never a
        half file under the real name)."""
        safe = _safe_name(name)
        dest = self.inbox / safe
        tmp = self.inbox / f".incoming-{uuid.uuid4().hex}"
        shutil.copyfile(blob_path, tmp)
        os.chmod(tmp, 0o600)
        self._chown(tmp)
        os.replace(tmp, dest)
        try:
            os.chmod(dest, 0o644)
        except OSError:
            pass
        self._chown(dest)

    def gc_inbox_temps(self) -> list[str]:
        swept = []
        try:
            for name in os.listdir(self.inbox):
                if name.startswith(".incoming-"):
                    os.unlink(self.inbox / name)
                    swept.append(name)
        except OSError:
            pass
        return swept

    # --- listing / reads -------------------------------------------------- #

    def list_outbox(self) -> list[dict]:
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
                out.append(
                    {
                        "path": rel,
                        "size": st.st_size,
                        "mtime": st.st_mtime,
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
