"""Chown-to-slot handoff (PLAN §5, uploads).

Uploads are received by the privileged app and written into
``inbox/<s>/`` then **chowned to the session's slot uid before the agent
can see them**. The same primitive hands out any app-created file that
the agent must own (the outbox dir at mount time, save-script
provenance is the exception — that one STAYS root-owned in lib/, which is
read-only to the agent anyway).

Content-addressed naming and MIME-by-magic are the upload pipeline's job
(stage 3); this module owns the ownership transfer.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


class HandoffError(RuntimeError):
    pass


@dataclass(frozen=True)
class Handoff:
    path: str
    uid: int
    gid: int


def chown_to(path: str, uid: int, gid: int) -> Handoff:
    try:
        st = os.lstat(path)
    except OSError as exc:
        raise HandoffError(f"cannot stat {path}: {exc}") from exc
    try:
        os.chown(path, uid, gid)
    except OSError as exc:
        raise HandoffError(f"chown {path}: {exc}") from exc
    return Handoff(path=path, uid=uid, gid=gid)


def handoff_upload(tmp_path: str, dest_dir: str, final_name: str, uid: int, gid: int) -> str:
    """Atomic rename into the inbox, chowned BEFORE the final name exists.

    Order matters: the file is chowned while it is still under a private
    temp name in the SAME directory (same filesystem => rename is
    atomic), so the agent never sees a root-owned or partially-written
    file — and no root-owned partial survives an aborted upload (the temp
    file is chown'd too, or deleted on abort).
    """
    if os.path.dirname(tmp_path) != dest_dir:
        raise HandoffError("temp file must live in the destination directory (atomic rename)")
    try:
        os.chown(tmp_path, uid, gid)
        final_path = os.path.join(dest_dir, final_name)
        os.replace(tmp_path, final_path)  # atomic; final name appears whole
        return final_path
    except OSError as exc:
        try:
            os.unlink(tmp_path)  # no root-owned partial survives
        except OSError:
            pass
        raise HandoffError(f"handoff failed: {exc}") from exc
