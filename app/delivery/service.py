"""Delivery service — the app-level wiring over the store.

One Delivery per app instance (state-volume root). Upload (one-shot):

    stream body → the session's staging part (disk, writer thread)
    → verify the WHOLE part → adopt into the blob store → hardlink into
      inbox/<name>
    → session manifest (files.json) → API-visible

The one-shot path IS the staged path without the client: one stream, one
part, nobody to answer for the next offset. It reuses the pump (a slow disk
slows this upload, never the loop), the complete transaction, and the TTL
sweeper — a stream that dies mid-body leaves its part on disk (a dropped
connection is not an abort), and the sha is computed over what actually
landed, never per chunk (see :mod:`app.delivery.upload`). A failure at any
step never registers a partial file: the manifest is the only registry of
delivered uploads.

Everything else a human can *do* with bytes lives here too, and every one of
those operations is a hardlink, never a copy:

- **keep** — a session's upload or artifact gains a second link under
  ``keep/<session>/``, outside every session area, so ``release()`` cannot
  reach it and the owner sweep cannot find it (it is excluded there).
- **save to lib** — a session's upload or artifact gains a link under the
  shared library (``lib/``, mounted RO in every session's workdir), through
  the same store hop: the link is the app's inode, never the agent's.
- **attach** — a file one session already has becomes an inbox entry of
  another, from the same inode (or, for an artifact not yet in the store,
  one copy into the store and then zero per extra session).
- **reclaim** — the blob store's own refcount: a blob no session or kept
  file links to is deleted. No index to drift.

No caps anywhere on this path: the controls are meters plus the human's own
delete/keep/download.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import stat
import time
from pathlib import Path

from app.delivery.blobs import Blobs
from app.delivery.upload import STAGED_TTL_SEC, Staged, StagedError
from app.delivery.store import (
    DeliveredFile,
    DeliveryError,
    FileNotDelivered,
    SessionFiles,
    _safe_relpath_safe,
    ensure_hash,
    mime_for,
    record_sha,
    sha_cached,
    sniff_mime,
)
from app.iso.layout import StatePaths, state_paths
from app.iso.spawn import SlotIdentity

log = logging.getLogger("servitor.delivery")


class Delivery:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.state = state_paths(str(self.root))
        self.blobs = Blobs(self.state)
        Path(self.state.keep).mkdir(parents=True, exist_ok=True)
        # the shared library: the agent's RO mount source, and the landing
        # area of a save-to-lib. Created like the keep area, on first use.
        Path(self.state.lib).mkdir(parents=True, exist_ok=True)

    def files(self, state: StatePaths, session_id: str, slot: SlotIdentity | None) -> SessionFiles:
        """The session's delivery views: the two bind sources (inbox is
        root-owned read-only to the agent, outbox is its own), and the
        manifest in the session dir."""
        return SessionFiles(
            session_dir=state.sessions_for(session_id),
            inbox=state.inbox_for(session_id),
            outbox=state.outbox_for(session_id),
            slot=slot,
            blobs=self.blobs,
            keep_dir=state.keep_for(session_id),
        )

    # ------------------------------------------------------------------ #
    # upload
    # ------------------------------------------------------------------ #

    async def complete_staged(
        self, staged: Staged, upload_id: str, session_files: SessionFiles
    ) -> DeliveredFile:
        """The one finish every staged upload goes through — the complete
        route and the one-shot share it, so verify → deliver → drop stays a
        single transaction: under the part's append lock, a chunk cannot
        land between the hash and the move (the blob's name is its promise
        about its bytes).
        """
        with staged.part_lock(upload_id) as lock:
            async with lock:
                part, sha, _size = await asyncio.to_thread(
                    staged.verify, upload_id
                )
                name = staged.name_of(upload_id)
                f = await asyncio.to_thread(
                    self.deliver_staged, part, name, sha, session_files
                )
                await asyncio.to_thread(staged.drop, upload_id)
        return f

    def deliver_staged(self, part: Path, name: str, sha256: str,
                       session_files: SessionFiles) -> DeliveredFile:
        """Deliver bytes that are already complete on disk (a staged upload at
        ``complete``): hash-verified by the caller over the whole file, adopted
        into the store, hardlinked into the inbox, recorded.

        Same tail as :meth:`upload` — the staged path differs in how the bytes
        arrived, never in how they land.
        """
        size = os.stat(part).st_size
        mime = sniff_mime(str(part))
        landed = session_files.deliver(part, name, sha256)
        delivered = DeliveredFile(name=landed, sha256=sha256, size=size, mime=mime, ts=time.time())
        session_files.register_upload(delivered)
        return delivered

    # ------------------------------------------------------------------ #
    # keep / attach (human actions; both are hardlinks)
    # ------------------------------------------------------------------ #

    def keep(
        self,
        sf: SessionFiles,
        src_path: str,
        name: str | None = None,
        sha256: str | None = None,
    ) -> dict:
        """Keep a session file beyond the session: a link under
        ``keep/<session>/`` to the STORE's copy of its bytes.

        Through the store, never straight at the session's own file, and
        never a move of it: the kept inode must be the app's (root-owned) —
        a moved or directly linked artifact keeps the agent's owner, and a
        still-running agent rewrites what it owns.

        The link lives outside every session area: ``release()`` cannot reach
        it and the owner sweep excludes it. Nothing here expires on its own;
        only an explicit delete removes a keep.

        ``name`` is caller-vetted (never re-sanitized here): the source's own
        gated area-relative path (safe as-is, a dot file keeps its dots) or
        the safe form of a typed ``as_name``.
        """
        try:
            st = os.stat(src_path)
        except FileNotFoundError:
            # a file unlinked between the gate and here is the gate's 404
            # shape, never a 500
            raise FileNotDelivered(src_path) from None
        if not stat.S_ISREG(st.st_mode):
            raise DeliveryError(f"{src_path} is not a regular file")
        if sha256 and self.blobs.is_same_blob(sha256, st):
            # the file IS the store's inode (a delivery, or an earlier
            # keep/attach): the link is free, and the sha is proven by the
            # inode, not claimed by a caller
            blob = self.blobs.state.blob_for(sha256)
        else:
            # a claimed sha is a hint (a cache hit for the file's current
            # size+mtime), never a proof: the bytes that are stored must be
            # the ones that get the name
            try:
                blob, sha256 = self.blobs.adopt_hashed(src_path)
            except FileNotFoundError:
                # unlinked between the stat and the copy (the source may be
                # multi-GB: the window is real)
                raise FileNotDelivered(src_path) from None
        bucket = sf.keep_dir
        if bucket is None:
            raise DeliveryError("no keep area for this session")
        os.makedirs(bucket, exist_ok=True)
        # the name is caller-vetted: keeping keeps the folder the file
        # arrived in, and a keep is never silently overwritten
        dest = self._free_link(blob, bucket, name or os.path.basename(src_path))
        # The row and the MIME cache key off the LINK, not the source: the
        # shelf lists the link's inode. The source's mtime names a different
        # version and would cost a re-sniff per keep.
        kst = os.lstat(dest)
        return {
            "path": os.path.relpath(dest, os.path.realpath(self.state.keep)),
            "sha256": sha256,
            "size": kst.st_size,
            "mtime": kst.st_mtime,
            "mime": _mime_cached(dest, kst),
            "ts": time.time(),
        }

    def _free_link(self, blob: str, bucket: str, relname: str) -> str:
        """Link a blob into a durable area (the keep shelf or the library)
        under the first free path, atomically.

        ``relname`` is a caller-vetted path within the bucket (a keep or a
        save preserves the folder its source lived in); ``_free_names``
        PREDICTS the name, ``Blobs.link`` DECIDES — a name a concurrent keep
        or save takes in the window fails the link (FileExistsError) and the
        next suffix wins. Nothing is ever silently overwritten.
        """
        parent, _, base = relname.rpartition("/")
        target_dir = os.path.join(bucket, parent) if parent else bucket
        os.makedirs(target_dir, exist_ok=True)
        for cand in _free_names(target_dir, base):
            try:
                self.blobs.link(blob, target_dir, cand)
                # realpath'd: the shelf's listing walks the realpath'd root,
                # so the row's path and the MIME cache key agree with it
                return os.path.realpath(os.path.join(target_dir, cand))
            except FileExistsError:
                continue  # taken in the race: next suffix
            except FileNotFoundError:
                # the store's copy vanished (a reclaim race): the 404 shape,
                # never a 500
                raise FileNotDelivered(f"kept bytes for {relname!r} are gone") from None
        raise DeliveryError(f"no free name for {relname!r} in {bucket}")

    def list_keep(self) -> list[dict]:
        """Every kept file, newest first.

        The sha needs no read of the file at all: a keep is a hardlink of a
        blob, and the blob's name IS its sha — :meth:`Blobs.sha_for_inode`
        answers from the store's index. The background pass is the fallback
        for a file that is not a stored blob — the area is root-only, so
        anything odd there is noise, not data.
        """
        rows: list[dict] = []
        root = os.path.realpath(self.state.keep)
        for dirpath, _dirs, names in os.walk(root):
            for name in names:
                p = os.path.join(dirpath, name)
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue  # a slot cannot write here; anything odd is noise
                rel = os.path.relpath(p, root)
                key = ("keep", rel, st.st_size, st.st_mtime)
                sha = self.blobs.sha_for_inode(st)
                if sha is not None:
                    record_sha(key, sha)
                else:
                    sha = ensure_hash(key, p)
                rows.append(
                    {
                        "path": rel,
                        "size": st.st_size,
                        "mtime": st.st_mtime,
                        "sha256": sha,
                        "mime": _mime_cached(p, st),
                    }
                )
        rows.sort(key=lambda r: -r["mtime"])
        return rows

    def keep_sha(self, rel: str) -> str | None:
        """The sha of a kept file — a lookup, never a hash: the cache if
        this process already knows it, else the inode's address in the
        store (a keep is a hardlink of a blob; the blob's name is the
        sha)."""
        p = self.keep_path(rel)  # containment-gated, realpath'd
        try:
            st = os.lstat(p)
        except FileNotFoundError:
            # an un-keep can take the file in the window between the gate
            # and this lookup: the 404 shape, never a 500
            raise FileNotDelivered(rel) from None
        relpath = os.path.relpath(p, os.path.realpath(self.state.keep))
        key = ("keep", relpath, st.st_size, st.st_mtime)
        sha = sha_cached(key)
        if sha is None:
            sha = self.blobs.sha_for_inode(st)
            if sha is not None:
                record_sha(key, sha)
        return sha

    def keep_path(self, rel: str) -> str:
        """A kept file, containment-gated (404 as FileNotDelivered so the
        route maps it, never a 500). ``p`` is realpath'd, so a link pointing
        out of the shelf fails the prefix test — and a link that stays in
        the shelf resolves to the kept inode itself, which is the one the
        human chose."""
        root = os.path.realpath(self.state.keep)
        if "\x00" in rel:
            raise FileNotDelivered(rel)
        p = os.path.realpath(os.path.join(root, rel))
        if p == root or not p.startswith(root + os.sep) or not os.path.isfile(p):
            raise FileNotDelivered(rel)
        return p

    # ------------------------------------------------------------------ #
    # save to lib (human action; the store hop, then a link into lib/)
    # ------------------------------------------------------------------ #

    def save_to_lib(
        self,
        src_path: str,
        name: str | None = None,
        sha256: str | None = None,
    ) -> dict:
        """Save a session file into the shared library (``lib/``).

        The destination is keep's opposite: not a private shelf outside the
        agent's world, but the one directory the agent's world shares —
        mounted RO in every session's workdir, read by all sessions, written
        by humans only. The store hop is keep's, for keep's reason: the link
        must be the app's (root-owned) inode. The suffix scheme is keep's
        too: a save never silently overwrites a name the library already
        holds.

        ``name`` is caller-vetted, as on :meth:`keep`: the source's own
        gated area-relative path (a dot file keeps its dots) or the safe
        form of a typed ``as_name`` — never re-sanitized here.
        """
        try:
            st = os.stat(src_path)
        except FileNotFoundError:
            raise FileNotDelivered(src_path) from None
        if not stat.S_ISREG(st.st_mode):
            raise DeliveryError(f"{src_path} is not a regular file")
        if sha256 and self.blobs.is_same_blob(sha256, st):
            blob = self.blobs.state.blob_for(sha256)
        else:
            try:
                blob, sha256 = self.blobs.adopt_hashed(src_path)
            except FileNotFoundError:
                raise FileNotDelivered(src_path) from None
        dest = self._free_link(blob, self.state.lib, name or os.path.basename(src_path))
        lst = os.lstat(dest)
        real = os.path.realpath(dest)
        rel = os.path.relpath(real, os.path.realpath(self.state.lib))
        return {
            "path": rel,
            "sha256": sha256,
            "size": lst.st_size,
            "mtime": lst.st_mtime,
            # the listing's per-version cache (the key list_lib uses): the
            # row is not sniffed a second time
            "mime": _mime_cached(real, lst, "lib"),
            "ts": time.time(),
        }

    def list_lib(self) -> list[dict]:
        """The shared library, newest first — the listing of ``lib/`` that
        the UI shows and the download/delete routes gate against.

        The sha is answered from the store's index only — the file is
        NEVER read for it. A save is a hardlink of a blob and the blob's
        name IS its sha. A file the repo pushed is not a stored blob, and
        it is the library's normal content, not noise — so it earns a null,
        never a background hash (the keep shelf's read-per-file fallback
        does not transfer here, where the non-blob file is the rule).
        """
        rows: list[dict] = []
        root = os.path.realpath(self.state.lib)
        for dirpath, _dirs, names in os.walk(root):
            for name in names:
                p = os.path.join(dirpath, name)
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue
                rows.append(
                    {
                        "path": os.path.relpath(p, root),
                        "size": st.st_size,
                        "mtime": st.st_mtime,
                        # a save addresses its bytes by the store's name;
                        # a repo file has none — null, never a read
                        "sha256": self.blobs.sha_for_inode(st),
                        "mime": _mime_cached(p, st, "lib"),
                    }
                )
        rows.sort(key=lambda r: -r["mtime"])
        return rows

    def lib_path(self, rel: str) -> str:
        """A library file, containment-gated (404 as FileNotDelivered so the
        route maps it, never a 500). ``p`` is realpath'd, so a link pointing
        out of the library fails the prefix test."""
        root = os.path.realpath(self.state.lib)
        if "\x00" in rel:
            raise FileNotDelivered(rel)
        p = os.path.realpath(os.path.join(root, rel))
        if p == root or not p.startswith(root + os.sep) or not os.path.isfile(p):
            raise FileNotDelivered(rel)
        return p

    def delete_lib(self, rel: str) -> int:
        """Delete one library file (a human curation action). Repo-pushed
        files re-land on the next sync; a file saved from the UI is gone.
        Empty dirs the delete leaves are pruned — never the lib root itself,
        which is a mounted bind source."""
        p = self.lib_path(rel)
        try:
            size = os.stat(p).st_size
            os.unlink(p)
        except FileNotFoundError:
            raise FileNotDelivered(rel) from None
        d = os.path.dirname(p)
        while d and d != os.path.realpath(self.state.lib):
            try:
                os.rmdir(d)
            except OSError:
                break
            d = os.path.dirname(d)
        return size

    # ------------------------------------------------------------------ #
    # rename / move — inside one area, never across the durability line
    # ------------------------------------------------------------------ #

    def rename(self, session_files: SessionFiles, src: str, dst: str) -> dict:
        """Rename or move one file inside its own area.

        ``uploads/x`` and ``outbox/sub/x.md`` are the two namespaces, and both
        sides must be in the same one. A ``to`` without an area prefix is the
        source's own area — the request URL already says which, and the
        prefix is a repeat, not information. A leading slash is the explicit
        form and must name the source's own area. A ``to`` with the OTHER
        area's prefix crosses areas, which is deliberately not a rename:
        uploads arrive through delivery and artifacts enter the store through
        a keep, and an in-place path change is neither (the agent can copy:
        ``cp inbox/x outbox/x`` — it should cost a copy).

        The response's ``from``/``to`` are paths WITHIN the area (no area
        prefix) on both sides — the request URL already names the area, so a
        response that re-prefixed it would be the shape callers special-case.
        """
        s_kind, _, s_rel = (src or "").replace("\\", "/").strip("/").partition("/")
        raw = (dst or "").replace("\\", "/")
        dst = raw.strip("/")
        first = dst.partition("/")[0]
        if raw.startswith("/") and first != s_kind:
            raise DeliveryError(
                f"an absolute 'to' must be in the source's own area ({s_kind}): {dst!r}"
            )
        if first not in ("uploads", "outbox"):
            dst = f"{s_kind}/{dst}"  # a bare name or path: the source's area
        d_kind, _, d_rel = dst.partition("/")
        if s_kind != d_kind or s_kind not in ("uploads", "outbox") or not s_rel or not d_rel:
            raise DeliveryError(
                "rename takes a bare name or path (the source's own area), or "
                "'uploads/<path>' / 'outbox/<path>', both sides in the same area"
            )
        return (
            session_files.rename_upload(s_rel, d_rel)
            if s_kind == "uploads"
            else session_files.rename_outbox(s_rel, d_rel)
        )

    # ------------------------------------------------------------------ #
    # archive — what the human can see is what the archive holds
    # ------------------------------------------------------------------ #

    def archive_entries(
        self,
        session_files: SessionFiles,
        path: str | list[str],
    ) -> tuple[str, list[tuple[str, str]]]:
        """``(suggested_name, [(arcname, abspath)])`` — one file, a folder,
        a whole area, or an explicit list of any of those (a multi-select
        download).

        The candidate set is the GATED LISTING and every entry is re-resolved
        through the download's gate as it is selected — so an archive holds
        exactly the files the human could click; the archive never walks an
        area on its own. A name the listing does not hold is a 404, not an
        empty zip. A selection that earns the same arcname twice (a root
        outbox file and an upload of the same name) is suffixed, not
        collided: readers keep only the LAST same-named zip entry, so a
        silent collision would drop the first file from the archive.
        """
        paths = [path] if isinstance(path, str) else list(path)
        sf = session_files
        # one listing per area per call: a multi-select is N selections
        # over ONE consistent view
        listings: dict[str, list[dict]] = {}

        def _list(kind: str) -> list[dict]:
            if kind not in listings:
                listings[kind] = (
                    sf.list_outbox() if kind == "outbox" else sf.list_uploads()
                )
            return listings[kind]

        # (kind, path in the area) → first arcname (a selection may name a
        # file and the folder that holds it; the file goes in once)
        first_win: dict[tuple[str, str], str] = {}
        for one in paths:
            kind, _, rel = (one or "").replace("\\", "/").strip("/").partition("/")
            prefix = rel.rstrip("/")
            if kind == "outbox":
                for row in _list("outbox"):
                    rp = row["path"]
                    if not prefix:
                        first_win.setdefault((kind, rp), rp)
                    elif rp == prefix:
                        first_win.setdefault((kind, rp), Path(rp).name)
                    elif rp.startswith(prefix + "/"):
                        first_win.setdefault((kind, rp), rp[len(prefix) + 1 :])
            elif kind == "uploads":
                uploads = _list("uploads")
                if prefix:
                    # matched through _safe_relpath exactly as the download's
                    # gate does, so a raw-dot ask finds its landed rows. A
                    # file selection carries the basename into the zip; a
                    # folder selection, its files relative to the folder
                    safe_prefix = _safe_relpath_safe(prefix)
                    matched: list[tuple[str, str]] = []
                    for u in uploads:
                        n = _safe_relpath_safe(str(u["name"]))
                        if n == safe_prefix:
                            matched.append((n, Path(n).name))
                        elif n.startswith(safe_prefix + "/"):
                            matched.append((n, n[len(safe_prefix) + 1 :]))
                    if not matched:
                        raise FileNotDelivered(f"no such upload: {prefix!r}")
                    for n, arc in matched:
                        first_win.setdefault((kind, n), arc)
                else:
                    for row in uploads:
                        first_win.setdefault((kind, row["name"]), row["name"])
            else:
                raise DeliveryError(f"unknown archive path: {one!r}")

        single = len(paths) == 1
        if single:
            kind0, _, rel0 = (
                (paths[0] or "").replace("\\", "/").strip("/").partition("/")
            )
            prefix = rel0.rstrip("/")
            name = Path(prefix).name if prefix else kind0
        else:
            # the multi-select is named for the deepest directory ALL its
            # entries share — within one area; a mix of areas (or root
            # files) earns the neutral name — and its entries are relative
            # to it
            kinds = {k for (k, _rp) in first_win}
            common = ""
            if len(kinds) == 1:
                common = self._common_dir([rp for _k, rp in first_win])
            name = common.split("/")[-1] if common else "files"

        out: list[tuple[str, str]] = []
        used_arcs: set[str] = set()

        def _free_arc(arc: str) -> str:
            # the keep shelf's suffix scheme, on the LAST component
            # (a/b.csv -> a/b.1.csv): a name a sibling entry already took is
            # refused, never replaced — a collision here is a lost file in
            # the zip
            if arc not in used_arcs:
                used_arcs.add(arc)
                return arc
            head, slash, base = arc.rpartition("/")
            stem, dot, ext = base.partition(".")
            prefix = f"{head}/{stem}" if slash else stem
            for i in range(1, 100_000):
                cand = f"{prefix}.{i}{dot}{ext}" if dot else f"{arc}.{i}"
                if cand not in used_arcs:
                    used_arcs.add(cand)
                    return cand
            raise DeliveryError(f"no free name for {arc!r} in the archive")

        for (kind, rp), arc in first_win.items():
            try:
                p = sf.outbox_path(rp) if kind == "outbox" else sf.upload_path(rp)
            except FileNotDelivered:
                continue  # vanished or no longer readable: not in the archive
            if not single:
                if common and rp.startswith(common + "/"):
                    arc = rp[len(common) + 1 :]
                else:
                    arc = rp  # no shared dir: the full path keeps the shape
            out.append((_free_arc(arc), str(p)))
        if not out:
            # the selection named nothing that resolves — the 404 shape
            raise FileNotDelivered(f"nothing to archive at {path!r}")
        return f"{name}.zip", out

    @staticmethod
    def _common_dir(rps: list[str]) -> str:
        """The deepest directory every path sits in (empty for root or empty)."""
        parts: tuple[str, ...] | None = None
        for rp in rps:
            d = tuple(rp.split("/")[:-1])
            if parts is None:
                parts = d
                continue
            n = 0
            while n < len(parts) and n < len(d) and parts[n] == d[n]:
                n += 1
            parts = parts[:n]
        return "/".join(parts or ())

    def delete_keep(self, rel: str) -> int:
        p = self.keep_path(rel)
        try:
            size = os.stat(p).st_size
            os.unlink(p)
        except FileNotFoundError:
            # a concurrent un-keep took it: 404, not a 500
            raise FileNotDelivered(rel) from None
        d = os.path.dirname(p)
        while d and d != os.path.realpath(self.state.keep):
            try:
                os.rmdir(d)  # prune the emptied bucket, never the keep root
            except OSError:
                break
            d = os.path.dirname(d)
        return size

    def attach(
        self,
        target: SessionFiles,
        src_path: str,
        *,
        sha256: str | None = None,
        name: str | None = None,
        mime: str | None = None,
        mime_key: tuple | None = None,
    ) -> DeliveredFile:
        """Give another session this file — the same inode where possible.

        The bytes go through the store, so the target's inbox entry is
        app-owned and provably a blob — which is what makes it readable under
        the delivery gate and un-modifiable by the target's agent. Free when
        the source is already the store's inode or the store already holds
        the bytes; otherwise one copy into the store, and the name is always
        the hash of the bytes stored. Every session after that is free.

        ``mime`` (a pre-resolved value, e.g. the source manifest's record) or
        ``mime_key`` (the source's OWN listing key) take the row's MIME from
        a cache the listing already warmed. Without either, a keep-shelf
        source falls back to the shelf's key (its absolute path — the key
        :meth:`list_keep` uses).
        """
        try:
            st = os.stat(src_path)
        except FileNotFoundError:
            # a file unlinked between the gate and here is the gate's 404
            # shape, never a 500
            raise FileNotDelivered(src_path) from None
        if not stat.S_ISREG(st.st_mode):
            raise DeliveryError(f"{src_path} is not a regular file")
        sp = os.path.realpath(src_path)  # the per-version cache key
        if sha256 and self.blobs.is_same_blob(sha256, st):
            blob = self.blobs.state.blob_for(sha256)
        else:
            # a claimed sha is a hint (the shelf's sha is answered from the
            # inode's address in the store), never a proof: the bytes that
            # are stored must be the ones that get the name
            try:
                blob, sha256 = self.blobs.adopt_hashed(src_path)
            except FileNotFoundError:
                # unlinked between the stat and the read (the source may be
                # multi-GB: the window is real)
                raise FileNotDelivered(src_path) from None
        if mime is None:
            if mime_key is not None:
                mime = mime_for(mime_key + (st.st_size, st.st_mtime), sp, st.st_size)
            else:
                mime = _mime_cached(sp, st)
        landed = target.link_blob(sha256, name or os.path.basename(src_path))
        delivered = DeliveredFile(
            name=landed,
            sha256=sha256,
            size=os.lstat(blob).st_size,
            mime=mime,
            ts=time.time(),
        )
        target.register_upload(delivered)
        return delivered

    # ------------------------------------------------------------------ #
    # GC
    # ------------------------------------------------------------------ #

    def gc_boot(self) -> dict:
        """At boot. A stale ``.adopt-*`` temp is a copy the process died
        mid-flight, and it goes.

        Every staging part — one-shot or resumable — SURVIVES boot: it is
        resumable by design, and wiping it would make a service restart cost
        the human a re-upload of exactly the large file that made staging
        worth having. Those are swept by their TTL, on the GC tick.
        """
        temps = self.blobs.sweep_temps()
        swept = self.gc_staged()
        log.info(
            "delivery boot gc: swept %d staging dirs, %d adopt temp(s)",
            len(swept), len(temps),
        )
        return {
            "staging_swept": swept,
            "adopt_temps_swept": temps,
        }

    def gc_staged(self, ttl_sec: float = STAGED_TTL_SEC) -> list[str]:
        """Sweep staged uploads nobody has touched in ``ttl_sec``, per session
        area. Recent ones stay: an upload in progress must survive an app
        restart, and one nobody resumed for `ttl_sec` has no client left."""
        gone: list[str] = []
        try:
            sids = os.listdir(self.state.staging)
        except OSError:
            return gone
        for sid in sids:
            try:
                gone += Staged(self.root, sid).gc(ttl_sec)
            except StagedError:
                # not a session-shaped name: the app did not put it here and
                # nothing can own it — junk goes. Removal by shape, as in
                # Staged.gc: a real directory by rmtree, a non-directory
                # entry by unlink. Report only what was still there AND is
                # gone now: listdir and the removal are not atomic.
                d = Path(self.state.staging) / sid
                was_there = d.is_symlink() or d.exists()
                if d.is_dir() and not d.is_symlink():
                    shutil.rmtree(d, ignore_errors=True)
                else:
                    d.unlink(missing_ok=True)
                if was_there and not (d.is_symlink() or d.exists()):
                    gone.append(sid)
        return gone

    def staged(self, session_id: str) -> Staged:
        """The staged-upload view of one session."""
        return Staged(self.root, session_id)

    def gc_blobs(self, grace_sec: float = 3600.0) -> int:
        """Reclaim blobs nothing links to any more (see Blobs.gc). Runs in
        the idle-GC tick: release drops a session's links, and this is what
        turns that into free space without a second bookkeeping layer."""
        return len(self.blobs.gc(grace_sec=grace_sec))


def _free_names(directory: str, name: str):
    """The names a keep may land under, in order: ``x.csv``, then
    ``x.1.csv``, ``x.2.csv``, … A PREDICTION — the link that consumes a name
    (``_free_link``) is the authority on what is actually free."""
    if not os.path.exists(os.path.join(directory, name)):
        yield name
    stem, dot, ext = name.partition(".")
    for i in range(1, 100_000):
        cand = f"{stem}.{i}{dot}{ext}"
        if not os.path.exists(os.path.join(directory, cand)):
            yield cand
    return


def _mime_cached(path: str, st: os.stat_result, area: str = "keep") -> str:
    """MIME for a known file version, from the store's cache (a shelf or
    library walk that re-sniffed the tree it just listed would read bytes
    nobody asked for). ``area`` namespaces the key: keep and lib are
    different listings over different paths, and a download reuses the
    listing's key, never re-sniffs."""
    return mime_for((area, path, st.st_size, st.st_mtime), path, st.st_size)
