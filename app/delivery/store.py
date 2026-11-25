"""Delivery store: uploads (staged part → verify → adopt into the blob store
→ link into the inbox), outbox, manifests.

Guarantees: no caps (uploads stream to a spooled part on disk — every
upload, one-shot or resumable, lands in the session's staging area, and the
sha256 is computed over the WHOLE part at complete, so the blob's name is
always the promise about the bytes actually stored); byte-identical (the
sha256 covers every byte that lands in the inbox); MIME by magic bytes,
never the extension; no half-files — an upload is visible only after it is
staged, verified, and linked into the session inbox: the part is adopted
into the blob store and the inbox entry is a hardlink to it, both
create-only (a link either makes the name or fails, never replaces), and
possible because staging, the store, and the inbox share the state
volume's filesystem. A crash before that leaves the part in staging,
swept by its TTL, and the store holds the bytes once: every session that
gets the file gets a link.

Root-reads are bounded by the slot uid. The app is root, so it CAN read any
inode on the volume; it must only read the ones the session owns. The
outbox is slot-writable, so a planted link is in play: a symlink is caught
by resolve-then-contain, but a HARDLINK is an ordinary file whose inode
someone else owns — listing it would advertise, hash (root reads the
target's bytes), and hand to the human a file the session never wrote. So
every list/read/delete path also requires ``st_uid == slot.uid``: a bound
on what root reads on the human's behalf, not a cap on the agent's reach
(invariant 2).

Per-version metadata (sha256, MIME) is computed at most once per file
version, keyed ``(sid, rel, size, mtime)`` — re-reading bytes per listing is
wasted SSD wear.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import stat
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from app.delivery.blobs import Blobs
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


# ---------------------------------------------------------------------- #
# outbox sha256: lazy background hash, cached per (size, mtime)
# ---------------------------------------------------------------------- #

_sha_lock = threading.Lock()
_SHA_CACHE: OrderedDict[tuple, str] = OrderedDict()
# Bounded: the key includes (size, mtime), so every file VERSION ever hashed
# gets its own entry and a superseded entry is never read again. Uncapped, a
# long-running process watching a growing artifact leaks ~300 B per version.
# FIFO evicts the oldest version first; if a live file is evicted, the next
# listing re-hashes it once (CPU, not correctness).
_SHA_CACHE_MAX = 4096  # ≈1.3 MB worst case
_SHA_PENDING: set[tuple] = set()

# MIME by magic bytes is a read too (8 KB head). Cached per file version for
# the same reason as the sha: a listing must not re-read every file every
# sweep (that is the whole outbox's worth of bytes, per poll).
_mime_lock = threading.Lock()
_MIME_CACHE: OrderedDict[tuple, str] = OrderedDict()
_MIME_CACHE_MAX = 4096


class _HashPool:
    """Bounded background hashers (daemon workers, unbounded queue).

    One full-file reader per new version, unbounded, would storm the disk on
    a run that materializes a big tree in the outbox (a clone is tens of
    thousands of versions in one sweep). The bound throttles the APP's own
    reads, never the agent's: queued versions still get hashed (the meter
    completes, slowly), and a hash interrupted at shutdown is simply re-
    offered by the next listing (the cache, not the queue, is the truth).
    """

    def __init__(self, max_workers: int = 4) -> None:
        self._max = max_workers
        self._queue: "queue.Queue[tuple[tuple, str]]" = queue.Queue()
        # workers are permanent daemons (a drained queue is a blocked
        # get, not an exit), so this counts live workers and never changes
        self._workers = 0
        self._gate = threading.Lock()

    def submit(self, key: tuple, path: str) -> None:
        self._queue.put((key, path))
        with self._gate:
            if self._workers >= self._max:
                return
            self._workers += 1
            threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self) -> None:
        while True:
            key, path = self._queue.get()
            _process_outbox_version(key, path)


_HASH_POOL = _HashPool()


def mime_for(key: tuple, path: str, size: int) -> str:
    """The file's MIME, sniffed at most once per version."""
    if not size:
        return "application/octet-stream"
    with _mime_lock:
        hit = _MIME_CACHE.get(key)
    if hit is not None:
        return hit
    mime = sniff_mime(path)
    with _mime_lock:
        _MIME_CACHE[key] = mime
        if len(_MIME_CACHE) > _MIME_CACHE_MAX:
            _MIME_CACHE.popitem(last=False)
    return mime


def _process_outbox_version(key: tuple, path: str) -> None:
    """Background: the one pass a listing triggers per outbox file version —
    its sha256 (1 MB chunks) and its MIME (8 KB head) land together, so a
    listing never blocks on either read and the delta diff sees both
    fill-ins at once.

    The sniff runs only for the outbox namespace: the keep shelf's rows
    render their MIME and keep it synchronous (a small, human-curated
    area), and the shelf keys its mime by the link's absolute path while
    its sha key — the one this pass receives — is a RELPATH, so a sniff
    here would land under a key nobody reads (a double read on every
    shelf listing).
    """
    try:
        lst = os.lstat(path)
        if not stat.S_ISREG(lst.st_mode):
            # a FIFO/socket/device is not a version to read: a plain open
            # of a FIFO BLOCKS (the reader waits for a writer that never
            # comes) and would hold one of the pool's permanent workers
            # forever. The listing is what must not have offered it; the
            # pool must not die on it anyway.
            return
        with _sha_lock:
            need_sha = key not in _SHA_CACHE
        with _mime_lock:
            need_mime = key[0] != "keep" and bool(key[2]) and key not in _MIME_CACHE
        if need_sha:
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(chunk)
            with _sha_lock:
                _SHA_CACHE[key] = h.hexdigest()
                if len(_SHA_CACHE) > _SHA_CACHE_MAX:
                    _SHA_CACHE.popitem(last=False)  # FIFO: oldest version first
        if need_mime:
            with _mime_lock:
                _MIME_CACHE[key] = sniff_mime(path)
                if len(_MIME_CACHE) > _MIME_CACHE_MAX:
                    _MIME_CACHE.popitem(last=False)
    except OSError:
        pass  # file gone mid-pass: the next listing re-checks
    finally:
        with _sha_lock:
            _SHA_PENDING.discard(key)


def ensure_hash(key: tuple, path: str) -> str | None:
    """The per-version sha: the cached value, or ``None`` with the file
    queued for the background hash pool.

    A listing is a lookup that costs at most ONE hash per file version,
    never a blocking read of the file — the row carries null until the
    background pass lands (and the delta machinery publishes the fill-in).
    """
    sha = _SHA_CACHE.get(key)
    if sha is not None:
        return sha
    with _sha_lock:
        if key not in _SHA_PENDING:
            _SHA_PENDING.add(key)
            _HASH_POOL.submit(key, path)
    return None


def sha_cached(key: tuple) -> str | None:
    """The per-version sha cache, read-only: a lookup that never queues a
    hash. A download or an attach must not start reading a multi-GB file to
    answer "do we already know this version" — the listing's background
    pass is the one that computes it."""
    return _SHA_CACHE.get(key)


def record_sha(key: tuple, sha: str) -> None:
    """Record a sha the caller already knows — the store's own name for the
    bytes, read off the inode's address, not computed from them: the next
    lookup is a hit and nothing gets queued. A pass the key already queued
    still drains, but finds the cache warm and skips the read."""
    with _sha_lock:
        _SHA_CACHE[key] = sha
        if len(_SHA_CACHE) > _SHA_CACHE_MAX:
            _SHA_CACHE.popitem(last=False)
        _SHA_PENDING.discard(key)


def mime_cached(key: tuple) -> str | None:
    """The per-version MIME cache, read-only: a lookup that never sniffs and
    never blocks. The listing's row carries null until the background pass
    lands (the delta machinery publishes the fill-in — the same contract as
    the sha). A download and the kept shelf still take the blocking
    :func:`mime_for`: the human asked for the bytes; one sniff at most."""
    return _MIME_CACHE.get(key)


# ---------------------------------------------------------------------- #
# manifest write lock: one per manifest path, process-wide               #
# ---------------------------------------------------------------------- #

_manifest_lock_guard = threading.Lock()
_manifest_locks: dict[str, threading.Lock] = {}


def _manifest_lock(manifest_path: Path) -> threading.Lock:
    """The read-modify-write lock for one manifest file.

    Module-level, not on SessionFiles: every request builds a fresh
    SessionFiles, so an instance attribute would serialize nothing.
    Bounded by the session count for the process's life (the update path
    restarts the app).
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
    - inbox:  ``state/inbox/<s>`` (the bind source; uploads land here, in
      the folders they arrived in — the human's tree, root-owned)
    - outbox: ``state/outbox/<s>`` (agent artifacts; the bind the agent
      writes through, so its handoffs live on the state volume and can be
      linked into ``keep/`` for free)
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
        blobs: "Blobs | None" = None,
        keep_dir: str | Path | None = None,
    ):
        self.session_dir = Path(session_dir)
        self.inbox = Path(inbox)
        self.outbox = Path(outbox)
        self.manifest_path = self.session_dir / "files.json"
        self.blobs = blobs
        # where a KEEP of this session's files lands (None: keeping is not
        # available — a caller with no state layout)
        self.keep_dir = str(keep_dir) if keep_dir is not None else None
        self.slot = slot
        # the ownership bound for every root-read below (None = no slot
        # identity known: nothing to bind reads to — the gate is inert)
        self.uid = None if slot is None else slot.uid
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.inbox.mkdir(parents=True, exist_ok=True)
        # The inbox directory is created root-owned and STAYS that way: no
        # delivery ever chowns it. Handing it to the slot would hand back the
        # one thing the read-only inbox is for — the power to create, rename,
        # or unlink what a human sent.
        # The outbox is created by mount_session (it is the bind source on the
        # state volume); nothing here needs to make it.

    def link_blob(self, sha256: str, name: str) -> str:
        """Deliver a blob the store already holds into this inbox (attach).

        No bytes move: the inbox entry is another link to the same inode, and
        the manifest entry written for it carries the sha that proves the link
        is the app's own blob (see :meth:`_readable`). The name is a path
        within the inbox (an attach keeps the folder its source lived in) and
        is disambiguated exactly like a delivery (:meth:`_land_name`), and the
        link is create-only (:meth:`_link_into_inbox`): an attach never
        silently replaces a different file the human already sees.
        """
        if self.blobs is None:
            raise DeliveryError("no blob store: cannot attach")
        return self._link_into_inbox(sha256, self.blobs.state.blob_for(sha256), name)

    def _ensure_inbox_parents(self, safe: str) -> None:
        """Create the parent dirs a landed path needs.

        Root-owned (like the inbox itself: its directory bits are what make
        uploads un-removable and un-creatable to the agent), mode fixed past
        the umask, and a component that already exists as anything but a
        dir is a conflict, not a mkdir: landing ``a/b.csv`` through a file
        named ``a`` would silently hide the file under the name ``a``.
        """
        d = self.inbox
        for part in safe.split("/")[:-1]:
            d = d / part
            if d.is_dir() and not d.is_symlink():
                continue
            if os.path.lexists(d):
                raise DeliveryError(
                    f"upload path blocked: {part!r} is not a directory"
                )
            try:
                d.mkdir(mode=0o755)
            except FileExistsError:
                continue  # a concurrent delivery made it in the window
            try:
                os.chmod(d, 0o755)  # mkdir's mode arg loses to the umask
            except OSError:
                pass

    def _link_into_inbox(self, sha256: str, blob: str, name: str) -> str:
        """Link a store blob into the inbox under the first path that is
        still free, atomically.

        ``_land_name`` PREDICTS the path (the requested one, or a
        disambiguated suffix); the LINK decides — a concurrent delivery that
        takes it in the window fails the link, and the choice is made again.
        A silent replace of a file the human already sees is the outcome this
        exists to prevent. A path that already holds the SAME blob is a
        no-op: the bytes are already where they should be.
        """
        while True:
            safe = self._land_name(_safe_relpath(name), sha256)
            try:
                st = os.lstat(self.inbox / safe)
            except OSError:
                st = None
            if st is not None and self.blobs.is_same_blob(sha256, st):
                return safe  # the store's inode is already under the path
            self._ensure_inbox_parents(safe)
            try:
                self.blobs.link(blob, str(self.inbox), safe)
                return safe
            except FileExistsError:
                continue  # taken in the race: re-choose
            except FileNotFoundError:
                # the link's ENOENT names a vanished path: the store's copy
                # (a reclaim race — the 404 shape, never a 500 off the
                # missing blob), or a parent dir a concurrent prune took in
                # the window (the name gets re-chosen and the dir re-made)
                if not os.path.isdir(self.inbox / os.path.dirname(safe)):
                    continue
                raise FileNotDelivered(f"blob for {name!r} is gone") from None

    def _land_name(self, safe: str, sha256: str | None) -> str:
        """The path a delivery lands under.

        The requested path, unless a file the app is NOT about to replace
        already occupies it: re-landing the SAME bytes on their own inode is
        the dedup (free, path kept), but a DIFFERENT file under a path that
        is taken must land beside it — the inbox is where the human's files
        live, and a silent replace is a lost file the phone paid for twice
        (a folder upload makes same-basename pairs routine). The suffix
        scheme is the keep shelf's (``x.csv`` → ``x.1.csv``), applied to the
        LAST component so the folder the human organized into stays.
        """
        dest = self.inbox / safe
        try:
            st = os.lstat(dest)
        except OSError:
            return safe
        if (
            sha256
            and self.blobs is not None
            and stat.S_ISREG(st.st_mode)
            and self.blobs.is_same_blob(sha256, st)
        ):
            return safe  # the same bytes: the replace is a no-op on content
        parent, _, base = safe.rpartition("/")
        stem, dot, ext = base.partition(".")
        for i in range(1, 100_000):
            cand_base = f"{stem}.{i}{dot}{ext}" if dot else f"{base}.{i}"
            cand = f"{parent}/{cand_base}" if parent else cand_base
            if not (self.inbox / cand).exists():
                return cand
        raise DeliveryError(f"no free name for {safe!r} in the inbox")

    def _chown(self, p: Path) -> None:
        if self.slot is not None:
            try:
                os.chown(p, self.slot.uid, self.slot.gid)
            except OSError:
                pass

    def _serves_uid(self, uid: int) -> bool:
        """May root read an inode owned by ``uid`` for this session?

        Only the session's own uid. A hardlink into the outbox has some
        other owner's uid (root for a system file, another slot for its
        uploads), and that is exactly what this rejects.
        """
        return self.uid is None or uid == self.uid

    def _readable(self, name: str, st: os.stat_result, manifest_sha: str | None = None) -> bool:
        """The inbox gate: the session's own files, plus app-delivered blobs.

        Delivered uploads are root-owned (that is what makes them
        un-writable and un-removable to the agent), so "owned by the slot"
        cannot be the only rule. The second is a PROOF, not a permission:
        the inode must be the very blob the app stored under the sha the app
        computed at upload time — a planted hardlink to a root-owned system
        file can satisfy it only if a human uploaded those exact bytes.

        ``manifest_sha``: the entry's recorded sha, pre-read by a caller that
        gates many entries at once (the listing); None = look it up.
        """
        if self._serves_uid(st.st_uid):
            return True
        if self.blobs is None or not stat.S_ISREG(st.st_mode):
            return False
        if manifest_sha is None:
            manifest_sha = self.manifest_sha(name)
        return self.blobs.is_same_blob(manifest_sha, st)

    def manifest_sha(self, name: str) -> str:
        """The recorded sha for a landed path ("" when there is none)."""
        want = _safe_relpath_safe(name)
        for u in self.read_manifest().get("uploads", []):
            if _safe_relpath_safe(u.get("name", "")) == want:
                return str(u.get("sha256", ""))
        return ""

    def manifest_mime(self, name: str) -> str:
        """The MIME recorded at upload time for a landed path ("" when there
        is none). The inbox file is immutable to the session, so a download
        must not re-read the bytes for a value already computed."""
        want = _safe_relpath_safe(name)
        for u in self.read_manifest().get("uploads", []):
            if _safe_relpath_safe(u.get("name", "")) == want:
                return str(u.get("mime", ""))
        return ""

    # --- manifest ------------------------------------------------------- #

    def read_manifest(self) -> dict:
        """The manifest, always shaped ``{"uploads": [dict, …]}`` with every
        entry a dict carrying a string ``name``.

        The slot owns this directory, so a tampered file must not be a 500
        on the routes that read it — a structurally broken entry is DROPPED,
        not raised, and register_upload's atomic replace self-heals the file
        on the next real upload. A symlinked manifest would let the slot
        forge the upload list served to the human (and the root app would
        read a root-readable target); never follow it.
        """
        try:
            if stat.S_ISLNK(os.lstat(self.manifest_path).st_mode):
                return {"uploads": []}
            man = json.loads(self.manifest_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {"uploads": []}
        return _clean_manifest(man)

    def register_upload(self, f: DeliveredFile) -> None:
        # The read-modify-write is serialized on the process-wide lock:
        # every request builds a fresh SessionFiles, so without it two
        # concurrent uploads to the same session would both read the
        # manifest and the last write wins — the earlier upload lands in
        # the inbox but never in the manifest.
        with _manifest_lock(self.manifest_path):
            man = self.read_manifest()
            # match through _safe_relpath, as drop_upload does: a legacy raw
            # entry (".env" for the file that landed as "env") is the same
            # row a re-landing of "env" replaces — a raw compare would keep
            # both, and the UI would render one file twice
            man["uploads"] = [
                u for u in man.get("uploads", [])
                if _safe_relpath_safe(u["name"]) != f.name
            ]
            man["uploads"].append(
                {"name": f.name, "sha256": f.sha256, "size": f.size, "mime": f.mime, "ts": f.ts}
            )
            self._write_manifest(man)

    def drop_upload(self, name: str) -> bool:
        """Drop one manifest entry (the human deleted the upload). True if
        an entry went away — the name is matched through ``_safe_relpath``
        so a legacy raw entry (".env" for the file that landed as "env") is
        removed too."""
        with _manifest_lock(self.manifest_path):
            man = self.read_manifest()
            try:
                landed = _safe_relpath(name)
            except DeliveryError:
                landed = name
            keep = [
                u for u in man.get("uploads", [])
                if _safe_relpath_safe(u["name"]) != landed
            ]
            if len(keep) == len(man.get("uploads", [])):
                return False
            man["uploads"] = keep
            self._write_manifest(man)
            return True

    def _write_manifest(self, man: dict) -> None:
        """The one atomic, symlink-proof manifest write (caller holds the
        manifest lock). The slot owns this directory and the app runs as
        root: the temp name is FIXED, so a planted ``files.json.tmp`` symlink
        must never be written through (that overwrites the target as root).
        O_EXCL|O_NOFOLLOW fails closed on any pre-existing name; only a stale
        REGULAR file (a crash between open and replace) is recovered —
        unlink it and retry once."""
        tmp = self.manifest_path.with_suffix(".json.tmp")
        try:
            fd = os.open(
                tmp,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
        except FileExistsError:
            # a planted link fails closed; only a stale regular file recovers
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

    def deliver(self, part: Path, name: str, sha256: str | None = None) -> str:
        """Hand the verified staged file to the inbox.

        With a store and its sha: the bytes are adopted into the blob store
        and HARDLINKED in (create-only, :meth:`_link_into_inbox`) — one copy
        however many sessions get the file, and the inode stays the app's:
        the agent can read it but neither write nor remove it (the inbox
        directory is root-owned too, so it cannot create or rename there
        either).

        Without them the part is LINKED in (create-only) and the part
        dropped — an atomic move with the same no-replace guarantee.

        Returns the path the file actually landed under (``_safe_relpath``
        of the requested path, disambiguated by ``_land_name`` when a
        different file already holds it — a leading-dot upload like ``.env``
        lands as ``env``): the caller must record THAT name in the manifest,
        or the entry can never match the files on disk.
        """
        if self.blobs is not None and sha256:
            blob, _existed = self.blobs.adopt(part, sha256)
            return self._link_into_inbox(sha256, blob, name)
        safe = self._land_name(_safe_relpath(name), sha256)
        dest = self.inbox / safe
        self._chown(part)
        self._ensure_inbox_parents(safe)
        try:
            os.link(part, dest)  # create-only: taken in the race => refuse
        except FileExistsError:
            raise DeliveryError(
                f"upload path {safe!r} was taken concurrently — re-upload"
            ) from None
        os.unlink(part)
        try:
            os.chmod(dest, 0o644)
        except OSError:
            pass
        return safe

    # --- listing / reads -------------------------------------------------- #

    def list_outbox(self) -> list[dict]:
        # sha256 and mime are computed LAZILY in one background pass, cached
        # per file version (module docstring): the row carries null until the
        # pass lands.
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
                # root-read (the lazy hash and the mime sniff would open the
                # target) or advertised in the listing (a link to a root file
                # would expose its existence/size in the UI). A NON-REGULAR
                # file (a FIFO, a socket, a device) is not an artifact either:
                # the background hash that opens it can BLOCK forever (a
                # reader on a FIFO waits for a writer that never comes), and
                # a worker hung like that is lost from the pool for the
                # process's life. Skip.
                if (
                    stat.S_ISLNK(st.st_mode)
                    or not stat.S_ISREG(st.st_mode)
                    or not self._serves_uid(st.st_uid)
                ):
                    continue
                key = (sid, rel, st.st_size, st.st_mtime)
                sha = ensure_hash(key, str(p))
                # the mime rides the same background pass as the sha: a walk
                # of a fresh tree must not sniff serially (one 8 KB read per
                # file, in the walk's critical section) — the row carries
                # null until the pass lands. Empty files need no sniff at
                # all: they are the one MIME with no bytes to read.
                mime = mime_cached(key) if st.st_size else "application/octet-stream"
                out.append(
                    {
                        "path": rel,
                        "size": st.st_size,
                        "mtime": st.st_mtime,
                        "sha256": sha,
                        "mime": mime,
                    }
                )
        return out

    def sha_for_outbox(self, rel: str) -> str | None:
        """The cached sha for an artifact, if it has been hashed yet.

        A lookup, never a hash: attaching a file whose hash is not computed
        must not read the whole artifact here (the caller hashes it once in
        the store instead).
        """
        try:
            p = self.outbox_path(rel)  # the download's gate, unchanged
        except FileNotDelivered:
            return None
        try:
            st = p.lstat()
        except OSError:
            return None
        return _SHA_CACHE.get((self.session_dir.name, rel, st.st_size, st.st_mtime))

    def list_uploads(self) -> list[dict]:
        """Manifest entries that still resolve to a file in the inbox.

        Entries are recorded under the path the file LANDED under
        (_safe_relpath); match through it, so a raw-dot entry still resolves,
        and a tampered entry (the manifest lives in a slot-owned dir) that
        does not resolve to an inbox file is dropped, not raised.

        The read gate applies here too (see :meth:`_readable`): an entry is
        listed only when its inode is the session's own or a blob the app
        delivered to it — a row the app will not serve is worse than no row.

        The manifest is read ONCE for the whole listing: the gate asks each
        root-owned entry for its recorded sha, and re-reading the file per
        entry turns a listing of N uploads into N manifest reads.
        """
        present: set[str] = set()
        try:
            for dirpath, _dirs, names in os.walk(self.inbox):
                for n in names:
                    p = Path(dirpath) / n
                    try:
                        if stat.S_ISREG(p.lstat().st_mode):
                            present.add(p.relative_to(self.inbox).as_posix())
                    except OSError:
                        continue
        except OSError:
            return []
        entries = self.read_manifest().get("uploads", [])
        shas: dict[str, str] = {}
        for u in entries:
            # first entry wins, as manifest_sha's scan does
            shas.setdefault(
                _safe_relpath_safe(str(u.get("name", ""))), str(u.get("sha256", ""))
            )

        def on_disk(name: str) -> bool:
            safe = _safe_relpath_safe(name)
            if safe not in present:
                return False
            try:
                st = os.lstat(self.inbox / safe)
            except OSError:
                return False
            return stat.S_ISREG(st.st_mode) and self._readable(safe, st, shas.get(safe, ""))

        return [u for u in entries if on_disk(u["name"])]

    def _assert_linkfree(self, root: Path, rel: str) -> None:
        """Refuse a name whose literal path crosses a symlink.

        A resolve would FOLLOW such a link and answer for its target —
        a delete through an in-area link would unlink the target's name
        and report the link's. The listing never shows links, so a link
        name only arrives hand-crafted: it is not a file.
        """
        cur = root
        for part in rel.split("/"):
            if part in ("", "."):
                continue
            cur = cur / part
            if cur.is_symlink():
                raise FileNotDelivered(rel)

    def outbox_path(self, rel: str) -> Path:
        """Resolve a requested outbox path; escape or link => FileNotDelivered.

        The link gate runs on the LITERAL name before the resolve (a link
        into the area is not the file its target has). ``is_relative_to``
        (path-wise, not a string prefix) — a resolved target must stay
        under the outbox, sibling dirs included. The uid gate closes what
        resolve cannot: a hardlink is not a link and resolves to itself,
        but its inode belongs to someone else; the post-resolve S_ISLNK
        is the guard for a link planted between the walk and the resolve.
        """
        if "\x00" in rel:
            raise FileNotDelivered(rel)
        self._assert_linkfree(self.outbox, rel)
        p = (self.outbox / rel).resolve()
        if not p.is_relative_to(self.outbox.resolve()) or not p.is_file():
            raise FileNotDelivered(rel)
        try:
            st = os.lstat(p)
        except OSError:
            raise FileNotDelivered(rel) from None
        if stat.S_ISLNK(st.st_mode) or not self._serves_uid(st.st_uid):
            raise FileNotDelivered(rel)
        return p

    def upload_path(self, name: str) -> Path:
        """Resolve a requested upload path; escape, link, or gate =>
        FileNotDelivered.

        ``name`` is a path within the inbox (a folder upload lives under its
        relative path). The rules are the outbox's: no traversal component,
        no symlink anywhere on the way in, and the file must pass the read
        gate (the uid or the blob proof) — a download, delete, rename, keep,
        or attach can never reach a byte the listing would not show.
        """
        try:
            safe = _safe_relpath(name)
        except DeliveryError:
            # a name _safe_relpath rejects (all dots, empty) is "no such
            # file" to the caller — FileNotDelivered (404), not an
            # unhandled DeliveryError (500)
            raise FileNotDelivered(name) from None
        self._assert_linkfree(self.inbox, safe)
        p = (self.inbox / safe).resolve()
        if not p.is_relative_to(self.inbox.resolve()) or not p.is_file():
            raise FileNotDelivered(name)
        try:
            st = os.lstat(p)
        except OSError:
            raise FileNotDelivered(name) from None
        if stat.S_ISLNK(st.st_mode) or not self._readable(safe, st):
            raise FileNotDelivered(name)
        return p

    # --- deletion (a human action; the agent has no route here) --------- #

    def delete_upload(self, name: str) -> dict:
        """Unlink one upload and drop its manifest entry.

        Path resolution is the download's (escape/symlink/uid-gated), so a
        delete can never reach a byte a download could not. The manifest
        entry goes after the unlink: a crash between the two leaves an
        entry that ``list_uploads`` filters out (it must resolve to a file),
        never a file with no record of where it came from. Empty parent
        dirs the delete leaves are pruned (never the inbox root: the bind
        source — the outbox's rule).
        """
        p = self.upload_path(name)
        try:
            st = os.lstat(p)
            os.unlink(p)
        except FileNotFoundError:
            # the gate passed and the file is gone (a delete from another
            # tab): the gate's own 404 shape, never a 500 off the path
            raise FileNotDelivered(name) from None
        rel = p.relative_to(self.inbox.resolve()).as_posix()
        self.drop_upload(rel)
        self._prune_empty(p.parent, self.inbox.resolve())
        return {"name": rel, "size": st.st_size}

    def delete_outbox(self, rel: str) -> dict:
        """Unlink one artifact and prune the empty dirs it leaves (never the
        outbox root: the bind target / workdir convention)."""
        p = self.outbox_path(rel)
        try:
            st = os.lstat(p)
            os.unlink(p)
        except FileNotFoundError:
            # the gate passed and the artifact is gone (the agent owns the
            # outbox and may have unlinked it in the window): 404, not a 500
            raise FileNotDelivered(rel) from None
        self._prune_empty(p.parent, self.outbox.resolve())
        return {"path": rel, "size": st.st_size}

    # ------------------------------------------------------------------ #
    # rename / move (inside one area — see Delivery.rename)
    # ------------------------------------------------------------------ #

    def rename_outbox(self, rel: str, to: str) -> dict:
        """Move/rename one artifact inside the outbox.

        Both sides run the download's gate, so a rename can neither read nor
        write outside the outbox; the destination must not exist (replacing
        would destroy a file the human can still see, to fix a typo); parents
        are created — mkdir is not an escape once the target path is
        contained — and every parent THIS call created is handed to the
        slot: a root-owned folder a human made would be an EACCES wall the
        agent's next write into it runs into, and the outbox is the agent's
        handoff, not a museum.
        """
        src = self.outbox_path(rel)
        dst = self._outbox_target(to)
        if dst == src:
            # a rename to itself is a no-op, not a 404: the destination
            # exists (it IS the source), and an unchanged name in the UI's
            # rename box must not read as an error
            try:
                size = src.stat().st_size
            except FileNotFoundError:
                raise FileNotDelivered(rel) from None
            return {
                "from": rel,
                "to": dst.relative_to(self.outbox.resolve()).as_posix(),
                "size": size,
            }
        if dst.exists() or dst.is_symlink():
            raise FileNotDelivered(f"outbox/{to} already exists")
        created = []
        p = dst.parent
        while not p.is_dir():
            created.append(p)
            p = p.parent
        dst.parent.mkdir(parents=True, exist_ok=True)
        for d in created:
            self._chown(d)
        # link-then-unlink, not rename: the link is the atomic check-and-
        # create — a destination a concurrent rename just took is refused
        # (404 to the caller), never replaced. A crash between the two leaves
        # two names for the same file, never a lost one.
        try:
            os.link(src, dst)
        except FileExistsError:
            raise FileNotDelivered(f"outbox/{to} already exists") from None
        except FileNotFoundError:
            # the source vanished between the gate and the link (the agent
            # owns the outbox): the gate's 404 shape, never a 500
            raise FileNotDelivered(rel) from None
        try:
            os.unlink(src)
        except FileNotFoundError:
            # a concurrent delete took the old name after our link: the link
            # at dst is the rename, and the manifest moves on either way
            pass
        self._prune_empty(src.parent, self.outbox.resolve())
        return {
            "from": rel,
            "to": dst.relative_to(self.outbox.resolve()).as_posix(),
            "size": dst.stat().st_size,
        }

    def rename_upload(self, name: str, to: str) -> dict:
        """Rename one upload, within the inbox: the file and its manifest
        entry move together.

        Both sides land through ``_safe_relpath`` (no traversal component,
        leading dots stripped per component, no symlink on the way in), so
        "move to v2/x" is a move into a folder the app creates root-owned,
        and "rename to ../x" is refused outright — not a silent rename to
        ``x``. The link moves first, then the entry: the row stays listed
        (under its old name) through the window, and a crash between the two
        leaves the file agent-readable under a name the UI will not show —
        never a listed row pointing at nothing.

        ``from``/``to`` are paths WITHIN the inbox (no area prefix) — the
        same response shape as :meth:`rename_outbox`, which answers within
        the outbox: the caller's URL already names the area, and a response
        that re-prefixed it would be the shape the UI has to special-case.
        """
        src = self.upload_path(name)
        src_rel = src.relative_to(self.inbox.resolve()).as_posix()
        safe = _safe_relpath(to)
        dst = self.inbox / safe
        if safe == src_rel:
            # a rename to itself is a no-op, not a 404: the destination
            # exists (it IS the source), and an unchanged name in the UI's
            # rename box must not read as an error
            try:
                size = src.stat().st_size
            except FileNotFoundError:
                raise FileNotDelivered(name) from None
            return {"from": src_rel, "to": safe, "size": size}
        if dst.exists() or dst.is_symlink():
            raise FileNotDelivered(f"uploads/{safe} already exists")
        self._ensure_inbox_parents(safe)
        # link-then-unlink: create-only, the rename's atomicity contract
        try:
            st = os.lstat(src)
            os.link(src, dst)
        except FileNotFoundError:
            # a delete from another tab won the race between the gate
            # and the move: the gate's 404 shape, never a 500
            raise FileNotDelivered(name) from None
        except FileExistsError:
            raise FileNotDelivered(f"uploads/{safe} already exists") from None
        try:
            os.unlink(src)
        except FileNotFoundError:
            # the delete took the old name after our link: the link at
            # dst is the rename, and the manifest moves on either way
            pass
        self._prune_empty(src.parent, self.inbox.resolve())
        with _manifest_lock(self.manifest_path):
            m = self.read_manifest()
            for u in m.get("uploads", []):
                if _safe_relpath_safe(str(u.get("name", ""))) == src_rel:
                    u["name"] = safe
            self._write_manifest(m)
        # `from` is the path the file actually lived under, not the one the
        # URL asked for (a legacy ``.env`` row moves the file ``env``)
        return {"from": src_rel, "to": safe, "size": st.st_size}

    def _outbox_target(self, to: str) -> Path:
        """A rename destination inside the outbox.

        No traversal component (so the path is under the outbox by
        construction) and no symlink anywhere on the way in — the session owns
        its outbox and could otherwise plant ``outbox/tmp -> /etc`` and have
        the app, running as root, move a file through it.
        """
        raw = (to or "").replace("\\", "/").strip("/")
        if "\x00" in to:
            raise FileNotDelivered(f"unsafe destination: {to!r}")
        parts = raw.split("/")
        if not raw or any(p in ("", ".", "..") for p in parts):
            raise FileNotDelivered(f"unsafe destination: {to!r}")
        root = self.outbox.resolve()
        dst = root.joinpath(*parts)
        if not dst.is_relative_to(root):
            raise FileNotDelivered(f"destination escapes the outbox: {to!r}")
        cursor = dst
        while cursor != root:
            if cursor.is_symlink():
                raise FileNotDelivered(f"destination passes through a symlink: {to!r}")
            cursor = cursor.parent
        return dst

    def _prune_empty(self, d: Path, root: Path) -> None:
        """Remove genuinely empty dirs up to ``root`` (never ``root``
        itself: the bind source / area root)."""
        cur = d
        while True:
            try:
                if not cur.is_relative_to(root) or cur.resolve() == root:
                    return
                os.rmdir(cur)  # only ever removes a genuinely empty dir
            except OSError:
                return  # non-empty, vanished, or a link: stop, leave it
            cur = cur.parent


def _clean_manifest(man) -> dict:
    """Coerce parsed manifest JSON to the shape every reader assumes (a dict
    whose ``uploads`` is a list of dicts with a string ``name``). Anything
    else is slot tampering (the file lives in a slot-owned dir), and a
    structurally broken entry is the one that would turn a listing or an
    upload into a 500 — so it is dropped, and the next real write replaces
    the file with a clean one."""
    uploads = man.get("uploads") if isinstance(man, dict) else None
    if not isinstance(uploads, list):
        return {"uploads": []}
    return {
        "uploads": [
            u for u in uploads if isinstance(u, dict) and isinstance(u.get("name"), str)
        ]
    }


def _safe_relpath_safe(name: str) -> str:
    """``_safe_relpath`` without the raise — for matching a possibly-forged
    manifest entry against a real path."""
    try:
        return _safe_relpath(name)
    except DeliveryError:
        return name


def _safe_relpath(name: str) -> str:
    """A safe relative path into the inbox: ``_safe_name``'s rules per
    component.

    ``/`` and ``\\`` are separators, bare separators collapse, and each
    component is stripped of leading dots (``.env`` lands as ``env``) — so
    ``photos/./.IMG_001.jpg`` lands as ``photos/IMG_001.jpg``. A component
    that is only dots (``.``, ``..``, ``...``) is traversal, not a name, and
    a NUL anywhere is refused: both raise, so every route already answers
    them 400/404.
    """
    parts = []
    for raw in (name or "").replace("\\", "/").split("/"):
        if raw == "":
            continue  # a bare separator: // and a trailing /
        try:
            parts.append(_safe_name(raw))
        except DeliveryError:
            raise DeliveryError(f"unsafe upload path: {name!r}") from None
    if not parts:
        raise DeliveryError(f"unsafe upload path: {name!r}")
    return "/".join(parts)


def _safe_name(name: str) -> str:
    """A single path component, no traversal, no leading dot."""
    name = name.replace("\\", "/").split("/")[-1].strip()
    name = name.lstrip(".")
    if not name or name in ("", ".") or "\x00" in name:
        # a NUL in the component would make the first path syscall raise an
        # unhandled ValueError (a 500) — refuse it here, where every route
        # already answers 400/404
        raise DeliveryError(f"unsafe upload name: {name!r}")
    return name
