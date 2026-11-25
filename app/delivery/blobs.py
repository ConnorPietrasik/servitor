"""The blob store: one copy of every delivered file's bytes.

``<state>/blobs/<sha[0:2]>/<sha>`` — content-addressed, root-owned, and
**linked (never copied)** into whichever session wants it. Two independent
reasons, one mechanism:

- *bytes and wear*: uploading the same 5 GB dataset to two sessions costs one
  copy of bytes and zero extra writes, and a phone link is never crossed
  twice for content the volume already holds;
- *the read gate*: the inode is the app's (root), inside a root-owned
  directory. A slot can read a delivered upload, hardlink it, and
  hardlink-unlink it — but it cannot write it in place (write permission
  follows the inode owner) and cannot remove or replace it (no write
  permission on the directory). So dedup adds no cross-session mutation
  channel: session A mangling ``inbox/x`` cannot touch the bytes session B
  links to, and "never delete the uploads" is a kernel guarantee rather than
  a request in a prompt.

Because a delivered file is root-owned, the delivery read-gate cannot accept
root-owned inodes on sight (that is the hardlink-to-``/etc/passwd`` hole) —
so it accepts one only when the inode provably IS the blob for a sha the app
itself computed at upload time: :func:`Blobs.is_same_blob`. Content the app
stored, keyed by a hash the app computed from the bytes a human sent: a
planted link cannot satisfy it unless a human really uploaded those bytes.

Reclaim is the filesystem's own refcount: a blob with no other link
(``st_nlink == 1``) is referenced by no session and no kept file, and is
unlinked by the GC pass after a grace window. No index to drift.
"""
from __future__ import annotations

import hashlib
import logging
import os
import stat
import threading
import time
import uuid
from pathlib import Path

from app.iso.layout import StatePaths

log = logging.getLogger("servitor.delivery.blobs")


class BlobError(RuntimeError):
    pass


def _fsync_dir(path: str) -> None:
    """Best-effort durability for a rename/link (the state volume is a real
    disk, and an upload that survives its own fsync should not be able to
    vanish because its directory entry did not)."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


class Blobs:
    def __init__(self, state: StatePaths):
        self.state = state
        self._ino_index: dict[tuple[int, int], str] | None = None
        self._ino_lock = threading.Lock()
        Path(state.blobs).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    # ingest
    # ------------------------------------------------------------------ #

    def adopt(self, part: Path, sha256: str) -> tuple[str, bool]:
        """Put a verified staged part in the store. Returns
        ``(blob_path, existed)``.

        ``existed`` is the dedup hit: the staged part (bytes identical by
        construction — the sha was computed while streaming) is dropped and
        the store's copy is used.

        The insertion is create-only: ``os.link`` either makes the name or
        fails with FileExistsError, never replaces. A replace would swap the
        store's inode out from under a session that already linked its copy
        in the window — and the read gate's proof is that the inode IS the
        blob, so that session's upload would then fail its own gate and
        vanish from its listing. Same sha means same bytes, so the
        concurrent winner's inode is exactly what this part holds.

        A failed drop of the staging name (the second ``os.unlink``) propagates:
        the delivery answers as failed (the route reports the error and no inbox
        link is made), but the part stays resumable — a re-complete dedups the
        already-stored bytes and lands the link, so nothing is lost and nothing
        is silently left on the volume unreported.
        """
        blob = self.state.blob_for(sha256)
        os.makedirs(os.path.dirname(blob), exist_ok=True)
        if os.path.lexists(blob):
            try:
                os.unlink(part)
            except OSError:
                pass
            return blob, True
        try:
            # same filesystem by construction (both under the state root):
            # the staged part LINKS in (a directory entry, no copy) and its
            # staging name is dropped
            os.link(part, blob)
            os.unlink(part)
        except FileExistsError:
            # the pre-check raced a concurrent adopt of the same bytes that
            # won the create: the dedup hit
            try:
                os.unlink(part)
            except OSError:
                pass
            return blob, True
        try:
            os.chmod(blob, 0o644)  # world-READABLE (a slot must read its
            # uploads) and owner-writable only by the app
        except OSError:
            pass
        _fsync_dir(os.path.dirname(blob))
        self._ino_index_add(blob, sha256)
        return blob, False

    def adopt_hashed(self, src_path: str) -> tuple[str, str]:
        """Copy bytes INTO the store, returning ``(blob_path, sha256)`` — the
        hash of the bytes ACTUALLY stored.

        The hash is read in place first, and the copy happens only when the
        store LACKS the bytes: a re-keep or re-attach of content the store
        already holds is one read and a free link — no full-size temp, which
        is the first thing to fail on a near-full volume even though the
        bytes are already there. The pre-read is safe for a source that can
        move (the agent's outbox): its hash is trusted only when the store
        already proves the bytes under that name; on a miss the pre-read is
        discarded and the copy re-reads and re-hashes in one pass, so the
        name always describes the stored bytes, whatever the source did
        meanwhile.

        One copy, ever: every later attach links the stored inode for free,
        and an existing copy at the hashed name is never replaced (another
        keep or inbox may link it; same sha means same bytes). The copy goes
        to a temp name and is LINKED in (create-only, for the reason
        :meth:`adopt`'s docstring gives), so a crash never leaves a
        half-written blob under the content-addressed name.
        """
        h = hashlib.sha256()
        with open(src_path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        blob = self.state.blob_for(h.hexdigest())
        if os.path.lexists(blob):
            return blob, h.hexdigest()
        tmp = os.path.join(self.state.blobs, f".adopt-{uuid.uuid4().hex[:12]}")
        h = hashlib.sha256()
        try:
            with open(src_path, "rb") as src, open(tmp, "wb") as dst:
                while True:
                    b = src.read(1024 * 1024)
                    if not b:
                        break
                    h.update(b)
                    dst.write(b)
                dst.flush()
                os.fsync(dst.fileno())
            sha = h.hexdigest()
            blob = self.state.blob_for(sha)
            if os.path.lexists(blob):
                os.unlink(tmp)
                return blob, sha
            os.makedirs(os.path.dirname(blob), exist_ok=True)
            try:
                # create-only: a concurrent copy of the same bytes loses to
                # the store's existing inode instead of replacing it
                os.link(tmp, blob)
                os.unlink(tmp)
            except FileExistsError:
                os.unlink(tmp)
                return blob, sha
            try:
                os.chmod(blob, 0o644)  # world-readable (a slot must read its
                # uploads), owner-writable only by the app
            except OSError:
                pass
            _fsync_dir(os.path.dirname(blob))
            self._ino_index_add(blob, sha)
            return blob, sha
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------ #
    # linking
    # ------------------------------------------------------------------ #

    def link(self, blob_path: str, dest_dir: str, name: str) -> None:
        """Make ``dest_dir/name`` a hardlink to the blob, atomically.

        Create-only: ``os.link`` either makes the name or fails with
        FileExistsError — a name a concurrent delivery or keep just took is
        refused, never overwritten, and the caller re-chooses (its
        ``_land_name`` / ``_free_names`` is a prediction, the link is the
        fact). ``name`` may be nested (the caller made the parent dirs); the
        durable entry is fsynced in the dir that holds it. A link is a
        single directory entry, so there is no half-file state to sweep:
        a crash leaves nothing but the entry that was never made.
        """
        dest = os.path.join(dest_dir, name)
        os.link(blob_path, dest)
        _fsync_dir(os.path.dirname(dest))

    # ------------------------------------------------------------------ #
    # verification (the read gate's proof)
    # ------------------------------------------------------------------ #

    def is_same_blob(self, sha256: str, st: os.stat_result) -> bool:
        """An inode IS the blob for ``sha256`` (same inode, not same name).

        The only way a root-owned inode becomes readable to a session: it has
        to be the exact blob the app stored under the sha the app computed.
        Takes the caller's already-lstat'd result — the gate runs per file on
        every listing, so it must not re-stat.
        """
        if not sha256 or not stat.S_ISREG(st.st_mode):
            return False
        try:
            bs = os.lstat(self.state.blob_for(sha256))
        except OSError:
            return False
        return (bs.st_dev, bs.st_ino) == (st.st_dev, st.st_ino)

    # ------------------------------------------------------------------ #
    # the shelf's sha: the inode's address, not a computed value
    # ------------------------------------------------------------------ #

    def sha_for_inode(self, st: os.stat_result) -> str | None:
        """The sha of the stored blob whose inode this is — ``None`` if the
        inode is not a stored blob. Takes the caller's already-lstat'd
        result (the shelf walks it per file; a re-stat would double the
        syscall).

        The store's name IS the sha and every keep is a hardlink of a
        blob, so a shelf row's sha is recoverable from the inode without
        re-reading — let alone re-hashing — the bytes: a cold process
        lists a multi-GB shelf for one bounded store walk, not one full
        file read per kept file. The index is process-local memory, and
        this process's own adopt (in) and reclaim (out) keep it honest.
        """
        if not stat.S_ISREG(st.st_mode):
            return None
        with self._ino_lock:
            index = self._ino_index
        if index is None:
            index = self._build_ino_index()
        return index.get((st.st_dev, st.st_ino))

    def _build_ino_index(self) -> dict[tuple[int, int], str]:
        """One walk of the store: (dev, ino) -> sha for every blob.

        Blobs are content-addressed and inserted create-only, so nothing
        moves: a walk is a complete snapshot, and a blob created after it
        is indexed by the adopt that made it. Built outside the lock and
        swapped in, so a slow walk on a large store never holds off the
        reads it serves; a race with a concurrent builder just wastes a
        walk.
        """
        index: dict[tuple[int, int], str] = {}
        try:
            prefixes = os.listdir(self.state.blobs)
        except OSError:
            prefixes = []
        for prefix in prefixes:
            pdir = os.path.join(self.state.blobs, prefix)
            if not os.path.isdir(pdir) or len(prefix) != 2:
                continue
            try:
                names = os.listdir(pdir)
            except OSError:
                continue
            for name in names:
                try:
                    st = os.lstat(os.path.join(pdir, name))
                except OSError:
                    continue
                if stat.S_ISREG(st.st_mode):
                    index[(st.st_dev, st.st_ino)] = name
        with self._ino_lock:
            if self._ino_index is None:
                self._ino_index = index
        return self._ino_index

    def _ino_index_add(self, blob: str | Path, sha256: str) -> None:
        """Index a blob this process just created — if the index is warm
        already (a later walk would have seen it, but a warm index would
        miss it)."""
        with self._ino_lock:
            index = self._ino_index
        if index is None:
            return
        try:
            st = os.lstat(blob)
        except OSError:
            return
        with self._ino_lock:
            index[(st.st_dev, st.st_ino)] = sha256

    def _ino_index_drop(self, st: os.stat_result) -> None:
        """Drop a reclaimed blob's entry: its inode may be reused by an
        unrelated file, and a dead entry would answer for the reuser."""
        with self._ino_lock:
            if self._ino_index is not None:
                self._ino_index.pop((st.st_dev, st.st_ino), None)

    # ------------------------------------------------------------------ #
    # reclaim
    # ------------------------------------------------------------------ #

    def sweep_temps(self) -> list[str]:
        """Drop stale ``.adopt-*`` temp files (a crash mid-copy leaves one).

        A live adopt unlinks its own temp on every exit; only a DEAD process
        leaves one, and the app is single-process — so at boot everything
        that matches is dead weight. No grace: a temp is never a blob, and a
        blob never starts with this prefix.
        """
        removed: list[str] = []
        try:
            names = os.listdir(self.state.blobs)
        except OSError:
            return removed
        for name in names:
            if not name.startswith(".adopt-"):
                continue
            p = os.path.join(self.state.blobs, name)
            try:
                st = os.lstat(p)
                if stat.S_ISREG(st.st_mode):
                    os.unlink(p)
                    removed.append(name)
            except OSError:
                pass  # vanished mid-sweep
        return removed

    def gc(self, grace_sec: float = 3600.0) -> list[str]:
        """Unlink blobs nothing references any more.

        ``st_nlink == 1`` is the whole test: the store's own link is the only
        one left, so no session inbox, no keep folder, and no listing holds
        these bytes. The grace window keeps a blob that a concurrent delivery
        is about to link from being swept mid-flight (and keeps an
        interrupted upload's part from being billed as garbage before the
        boot/staging GC has had its turn).
        """
        removed: list[str] = []
        now = time.time()
        try:
            prefixes = sorted(os.listdir(self.state.blobs))
        except OSError:
            return removed
        for prefix in prefixes:
            pdir = os.path.join(self.state.blobs, prefix)
            if not os.path.isdir(pdir) or len(prefix) != 2:
                continue
            try:
                names = sorted(os.listdir(pdir))
            except OSError:
                continue
            for name in names:
                blob = os.path.join(pdir, name)
                try:
                    st = os.lstat(blob)
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue  # not a blob; a slot cannot write here anyway
                if st.st_nlink > 1:
                    continue  # someone still holds it
                if now - max(st.st_mtime, st.st_ctime) < grace_sec:
                    continue
                try:
                    os.unlink(blob)
                    removed.append(name)
                    self._ino_index_drop(st)
                except OSError:
                    log.warning("blob %s could not be reclaimed", blob, exc_info=True)
            try:
                os.rmdir(pdir)  # only ever removes an emptied fanout dir
            except OSError:
                pass
        if removed:
            log.info("blob gc: reclaimed %d unreferenced blob(s)", len(removed))
        return removed
