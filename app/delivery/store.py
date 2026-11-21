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
import stat
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


# ---------------------------------------------------------------------- #
# manifest write lock: one per manifest path, process-wide               #
# ---------------------------------------------------------------------- #

_manifest_lock_guard = threading.Lock()
_manifest_locks: dict[str, threading.Lock] = {}


def _manifest_lock(manifest_path: Path) -> threading.Lock:
    """The read-modify-write lock for one manifest file.

    Module-level, not on SessionFiles: every request builds a fresh
    SessionFiles, so an instance attribute would serialize nothing.
    One entry per manifest path ever seen — bounded by the session
    count for the process's life (the update path restarts the app).
    """
    key = str(manifest_path)
    with _manifest_lock_guard:
        lock = _manifest_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _manifest_locks[key] = lock
        return lock


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
            # the slot owns this dir: a symlinked manifest would let it forge
            # the upload list served to the human (and the root app would read
            # a root-readable target). Never follow it; register_upload's
            # atomic replace self-heals a tampered name on the next upload.
            if stat.S_ISLNK(os.lstat(self.manifest_path).st_mode):
                return {"uploads": []}
            return json.loads(self.manifest_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {"uploads": []}

    def register_upload(self, f: DeliveredFile) -> None:
        # The read-modify-write is serialized on the process-wide lock
        # (see _manifest_lock): every request builds a fresh SessionFiles,
        # so without it two concurrent uploads to the same session would
        # both read the manifest and the last write wins — the earlier
        # upload lands in the inbox but never in the manifest, so it is
        # invisible in the UI.
        with _manifest_lock(self.manifest_path):
            man = self.read_manifest()
            man["uploads"] = [u for u in man.get("uploads", []) if u["name"] != f.name]
            man["uploads"].append(
                {"name": f.name, "sha256": f.sha256, "size": f.size, "mime": f.mime, "ts": f.ts}
            )
            # The slot owns this directory and the app runs as root: the temp
            # name is FIXED, so a planted ``files.json.tmp`` symlink must never
            # be written through (that truncates/overwrites the target as root).
            # O_EXCL|O_NOFOLLOW fails closed on any pre-existing name, symlink
            # or otherwise; only a stale REGULAR file (a crash between open and
            # replace) is recovered — unlink it and retry once. os.replace below
            # swaps the name itself, never follows the manifest.
            tmp = self.manifest_path.with_suffix(".json.tmp")
            try:
                fd = os.open(
                    tmp,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                )
            except FileExistsError:
                # name already exists: only a stale REGULAR file (a crash
                # between open and replace) is recoverable; anything else
                # (a planted link) fails closed
                st = os.lstat(tmp)
                if not stat.S_ISREG(st.st_mode):
                    raise DeliveryError(
                        f"manifest temp {tmp} is not a regular file — "
                        "refusing to write (planted symlink?)"
                    ) from None
                os.unlink(tmp)
                try:
                    fd = os.open(
                        tmp,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                    )
                except OSError:
                    # the name re-appeared between unlink and open (race):
                    # refuse rather than write through whatever it is now
                    raise DeliveryError(
                        f"manifest temp {tmp} could not be (re)created — "
                        "refusing to write"
                    ) from None
            with os.fdopen(fd, "w") as fh:
                fh.write(json.dumps(man, indent=1))
            os.replace(tmp, self.manifest_path)
            self._chown(self.manifest_path)

    # --- delivery -------------------------------------------------------- #

    def deliver(self, part: Path, name: str) -> str:
        """Hand the verified staged file to the inbox: chown to the slot,
        then atomic rename. Staging and inbox share the state volume's
        filesystem, so the final name appears whole and slot-owned in one
        step — the agent never sees a root-owned or partially-written
        file, and a crash before the rename leaves the part in staging
        for the boot GC, never a half file under the real name.

        Returns the name the file actually landed under (``_safe_name``
        of the requested name — a leading-dot upload like ``.env``
        lands as ``env``): the caller must record THAT name in the
        manifest, or the entry can never match ``os.listdir(inbox)``
        and the upload is invisible in GET /files forever.
        """
        safe = _safe_name(name)
        dest = self.inbox / safe
        self._chown(part)
        os.replace(part, dest)  # atomic: same filesystem
        try:
            os.chmod(dest, 0o644)
        except OSError:
            pass
        self._chown(dest)
        return safe

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
                    st = p.lstat()
                except OSError:
                    continue
                # the outbox is slot-owned: a planted link must not be
                # root-read (the lazy hash and the mime sniff would open
                # the target) or advertised in the listing (a link to a
                # root file would expose its existence/size in the UI).
                # Symlinked artifacts are not a documented feature: skip.
                if stat.S_ISLNK(st.st_mode):
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

    def list_uploads(self) -> list[dict]:
        """Manifest entries that still resolve to a file in the inbox.

        Entries are recorded under the name the file LANDED under
        (_safe_name). Match through _safe_name so legacy manifests
        written before that fix (raw .env) still resolve, and a
        tampered entry (the manifest lives in a slot-owned dir) that
        does not resolve to an inbox file is dropped, not raised.
        """
        try:
            present = set(os.listdir(self.inbox))
        except OSError:
            return []

        def on_disk(name: str) -> bool:
            try:
                return _safe_name(name) in present
            except DeliveryError:
                return name in present

        return [u for u in self.read_manifest().get("uploads", []) if on_disk(u["name"])]

    def outbox_path(self, rel: str) -> Path:
        """Resolve a requested outbox path; escape => FileNotDelivered.

        ``is_relative_to`` (path-wise, not a string prefix) — a resolved
        target must stay under the outbox, sibling dirs included.
        """
        p = (self.outbox / rel).resolve()
        if not p.is_relative_to(self.outbox.resolve()) or not p.is_file():
            raise FileNotDelivered(rel)
        return p

    def upload_path(self, name: str) -> Path:
        try:
            safe = _safe_name(name)
        except DeliveryError:
            # a name _safe_name rejects (all dots, empty) is "no such
            # file" to the caller — FileNotDelivered (404), not an
            # unhandled DeliveryError (500)
            raise FileNotDelivered(name) from None
        p = (self.inbox / safe).resolve()
        if not p.is_relative_to(self.inbox.resolve()) or not p.is_file():
            raise FileNotDelivered(name)
        return p


def _safe_name(name: str) -> str:
    """A single path component, no traversal, no leading dot."""
    name = name.replace("\\", "/").split("/")[-1].strip()
    name = name.lstrip(".")
    if not name or name in ("", "."):
        raise DeliveryError(f"unsafe upload name: {name!r}")
    return name
