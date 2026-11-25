"""Per-session mounts.

Mounted at allocate, before any spawn/respawn; live for the SESSION
lifetime — pi runs a session in its recorded cwd, so idle-kill never
unmounts; only release does (reverse order).

The five real mounts:

1. **Workdir overlay** — lower = /opt/toolkit (baked, a plain directory,
   never a mount), upper/work = scratch/<s>/{upper,work}, merged at the
   stable per-session workdir path — the recorded cwd.
2. **Agent-dir overlay** — lower = config/v<N> (the version this session
   booted from; overlayfs won't show a nested mount inside a lowerdir),
   upper/work = scratch/<s>/{agent-upper,agent-work}.
   PI_CODING_AGENT_DIR = merged.
3. **inbox bind** — state/inbox/<s> into the merged workdir as ./inbox.
   The source is root-owned, in a root-owned directory: the agent reads its
   uploads and can hardlink them, but cannot write, rename, or delete them
   (the kernel, not the prompt, enforces "never lose what the human sent").
4. **outbox bind** — state/outbox/<s> into the merged workdir as ./outbox.
   Slot-owned: this is where the agent's handoffs live, and being a bind of
   a state-volume directory (not a directory inside the overlay upper) is
   what keeps artifacts visible while the session is unmounted and lets the
   app hardlink them into ``keep/`` across the same filesystem.
5. **models.json single-file RO bind** — over the merged agent dir (the
   agent could otherwise shadow it in the upper and steer the NEXT run's
   endpoint; the bearer key rides along).

Read-only to the agent = root-owned, slot-uid-unwritable.
"""
from __future__ import annotations

import asyncio
import os
import stat
import subprocess
from dataclasses import dataclass

from app.iso.layout import TOOLKIT_DIR, SessionMountPaths, StatePaths


class MountError(RuntimeError):
    pass


@dataclass(frozen=True)
class MountOp:
    """One mount, with its exact teardown. Order in the plan matters."""

    kind: str  # "overlay" | "bind" | "ro-bind-file"
    target: str
    options: str  # the -o string ("" for plain bind)

    def _run(self, args: list[str]) -> None:
        try:
            subprocess.run(args, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            raise MountError(
                f"mount {self.kind} {self.target} failed: "
                f"{exc.stderr.strip() or exc.stdout.strip()}"
            ) from exc

    def mount(self) -> None:
        if self.kind == "overlay":
            self._run(
                ["mount", "-t", "overlay", "overlay", "-o", self.options, self.target]
            )
        elif self.kind == "bind":
            self._run(["mount", "--bind", self.options, self.target])
        elif self.kind == "ro-bind-file":
            # options = "source:target" split by the caller via a marker
            source, target = self.options.split("::")
            self._run(["mount", "--bind", source, target])
            self._run(["mount", "-o", "remount,ro,bind", target])
        else:
            raise MountError(f"unknown mount kind {self.kind!r}")

    def unmount(self) -> None:
        self._run(["umount", self.target])


def ensure_session_dirs(sp: SessionMountPaths, state: StatePaths) -> None:
    """Create the scratch layout (as root) before mounting.

    Note: the inbox and outbox BIND TARGETS (``workdir/inbox``,
    ``workdir/outbox``) are created only AFTER the workdir overlay is mounted
    (the target must exist in the merged view; a bind onto a missing target
    fails) — see mount_session.
    """
    for d in (
        sp.scratch,
        sp.upper,
        sp.work,
        sp.workdir,
        sp.agent_dir,
        sp.agent_upper,
        sp.agent_work,
        sp.web_cache,
        state.inbox_for(sp.session_id),
        state.outbox_for(sp.session_id),
        state.sessions_for(sp.session_id),
    ):
        os.makedirs(d, exist_ok=True)


def _assert_real_dir(path: str) -> None:
    """Refuse to chown or mount a path that is not a REAL directory.

    ``os.makedirs(exist_ok=True)`` succeeds silently when the final path is
    a symlink to an existing directory, and both mount(2) and chown(2) follow
    a final-component symlink: a slot-planted link (the scratch tree is
    slot-owned and survives a reboot) would otherwise make the app chown the
    TARGET — or overlay-mount it. A symlink at a canonical name is tampering:
    fail closed (MountError), never absorb it.
    """
    try:
        st = os.lstat(path)
    except OSError:
        raise MountError(f"{path} vanished before mount (tampered?)") from None
    if not stat.S_ISDIR(st.st_mode):
        raise MountError(
            f"{path} is not a real directory (tampered?); refusing to mount"
        )


def _chown_tree(path: str, uid: int, gid: int) -> None:
    """Hand the tree to the slot uid.

    Entries are chown'd with ``lchown`` (never follow a link): these trees
    are slot-writable, so a planted link to a root-owned file must not make
    the app chown the target. Symlink entries become slot-owned links —
    harmless, they are the slot's own. The caller guarantees ``path`` itself
    is a real directory (``_assert_real_dir``).

    Entries with more than one link are LEFT alone, and that is a security
    rule, not an optimisation: a delivered upload's inode is shared with the
    blob store (and with other sessions' inboxes), so chown'ing it here would
    hand every holder write access to bytes they are only meant to read.
    Same for an outbox artifact the human kept — the keep link shares it.
    """
    os.chown(path, uid, gid)  # real dir, guaranteed by the caller
    for dirpath, dirnames, filenames in os.walk(path):
        for name in list(dirnames) + list(filenames):
            p = os.path.join(dirpath, name)
            try:
                st = os.lstat(p)
                # directories carry nlink > 1 by their own nature (each
                # subdirectory adds one); only file inodes can be shared by
                # hardlink
                if not stat.S_ISDIR(st.st_mode) and st.st_nlink > 1:
                    continue  # a shared inode: never re-handed to one holder
                os.lchown(p, uid, gid)
            except OSError:
                pass


def _make_agent_readonly(path: str) -> None:
    """Root-owned, world-readable, slot-unwritable — the inbox's shape.

    Only the DIRECTORY is set here; existing entries keep their ownership
    (nothing gains access it lacked). New entries are created by the app
    (root): no write permission on the directory means no create, rename,
    or unlink, and no write permission on the inode means no in-place edit.
    """
    os.chown(path, 0, 0)
    os.chmod(path, 0o755)


def mount_session(
    sp: SessionMountPaths,
    config_lower: str,
    state: StatePaths,
    slot_uid: int,
    slot_gid: int,
    toolkit_dir: str = TOOLKIT_DIR,
    agent_dir_ro: bool = False,
) -> list[MountOp]:
    """The full ordered mount sequence. Returns the ops (for reverse-order
    teardown at release).

    ``scratch/<s>/`` is slot-uid-owned: the app creates the dirs as root,
    then hands the whole per-session tree to the slot BEFORE any mount — a
    root-owned 0755 upper would be unwritable by the very session it exists
    for (the agent runs as the slot uid).
    """
    # scratch/<s>/ is slot-owned and survives a reboot: on a re-mount the agent
    # may have replaced a canonical path (upper, work, ...) with a symlink.
    # A plain chown or mount(2) would follow it: gate EVERY canonical name
    # before anything touches it.
    for d in (
        sp.scratch,
        sp.upper,
        sp.work,
        sp.workdir,
        sp.agent_dir,
        sp.agent_upper,
        sp.agent_work,
        sp.web_cache,
        state.inbox_for(sp.session_id),
        state.outbox_for(sp.session_id),
        state.sessions_for(sp.session_id),
    ):
        _assert_real_dir(d)
    # Hand the trees to the slot: one walk per TOP-LEVEL root — every
    # per-session scratch child (upper, work, workdir, agent_*, web_cache)
    # already sits under sp.scratch, so per-child chowns would re-walk the
    # same entries. The inbox is NOT one of them: it is the agent's read-only
    # area, and chown'ing it would make uploads deletable by the session that
    # received them.
    for root in (
        sp.scratch,
        state.outbox_for(sp.session_id),
        state.sessions_for(sp.session_id),
    ):
        _chown_tree(root, slot_uid, slot_gid)
    _make_agent_readonly(state.inbox_for(sp.session_id))
    ops = plan_session_mounts(sp, config_lower, state, toolkit_dir, agent_dir_ro)
    # 1: workdir overlay
    ops[0].mount()
    try:
        # The inbox/outbox BIND TARGETS, created post-overlay. A planted
        # upper/inbox or upper/outbox link would surface here in the merged
        # view, so both are gated before anything is mounted on them.
        inbox_target = f"{sp.workdir}/inbox"
        os.makedirs(inbox_target, exist_ok=True)
        _assert_real_dir(inbox_target)
        os.chown(inbox_target, slot_uid, slot_gid)
        # The outbox target is covered by the bind a few lines below, so its
        # own ownership is moot; a pre-upgrade run that left files in the
        # upper's outbox/ has them shadowed by the bind — invisible to the
        # agent AND the UI, and gone with the scratch at release.
        outbox_target = f"{sp.workdir}/outbox"
        os.makedirs(outbox_target, exist_ok=True)
        _assert_real_dir(outbox_target)
        # 2-5: agent-dir overlay, inbox bind, outbox bind, models.json RO bind
        for op in ops[1:]:
            op.mount()
    except Exception:
        # tamper gate or a later mount failure: never leave a partial mount
        # behind (a surviving workdir overlay would mask the next remount's
        # gates). Roll back in reverse, best effort; the original error
        # propagates (the caller aborts/quarantines).
        for op in reversed(ops):
            try:
                op.unmount()
            except MountError:
                pass
        raise
    return ops


def plan_session_mounts(
    sp: SessionMountPaths,
    config_lower: str,
    state: StatePaths,
    toolkit_dir: str = TOOLKIT_DIR,
    agent_dir_ro: bool = False,
) -> list[MountOp]:
    """The five mounts, in mount order (teardown is the reverse).

    ``toolkit_dir`` defaults to the image-baked constant; tests inject a
    scratch toolkit (the production path always uses the constant).

    ``agent_dir_ro``: mount the agent-dir overlay READ-ONLY at mount time
    (overlayfs refuses a later `remount,ro` — "No changes allowed in
    reconfigure" — so the negative config gate must use this flag, which
    reproduces the simplified/broken layout the gate exists to catch).
    """
    agent_opts = f"lowerdir={config_lower},upperdir={sp.agent_upper},workdir={sp.agent_work}"
    if agent_dir_ro:
        agent_opts += ",ro"
    return [
        MountOp(
            kind="overlay",
            target=sp.workdir,
            options=f"lowerdir={toolkit_dir},upperdir={sp.upper},workdir={sp.work}",
        ),
        MountOp(
            kind="overlay",
            target=sp.agent_dir,
            options=agent_opts,
        ),
        MountOp(
            kind="bind",
            target=f"{sp.workdir}/inbox",
            options=state.inbox_for(sp.session_id),
        ),
        MountOp(
            kind="bind",
            target=f"{sp.workdir}/outbox",
            options=state.outbox_for(sp.session_id),
        ),
        MountOp(
            kind="ro-bind-file",
            target=f"{sp.agent_dir}/models.json",
            options=f"{config_lower}/models.json::{sp.agent_dir}/models.json",
        ),
    ]


def execute_mounts(ops: list[MountOp]) -> None:
    for op in ops:
        op.mount()


def unmount_all(ops: list[MountOp]) -> list[str]:
    """Reverse mount order. Returns the list of targets actually unmounted."""
    unmounted: list[str] = []
    for op in reversed(ops):
        try:
            op.unmount()
            unmounted.append(op.target)
        except MountError as exc:
            # a stuck mount is a release failure: it gets reported (the sweep
            # will see the mountpoint), never silently skipped
            raise
    return unmounted


def mounted_targets() -> set[str]:
    """Targets currently mounted (from /proc/mounts) — sweep guard."""
    out: set[str] = set()
    with open("/proc/mounts", "r", encoding="ascii") as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 2:
                out.add(parts[1])
    return out


async def aexecute_mounts(ops: list[MountOp]) -> None:
    await asyncio.to_thread(execute_mounts, ops)


async def aunmount_all(ops: list[MountOp]) -> list[str]:
    return await asyncio.to_thread(unmount_all, ops)


def aunmount_all_safe(ops: list[MountOp]) -> list[str]:
    """Best-effort unmount (test/cleanup path): never raises; on a busy
    target (a live child holding the cwd) falls back to a lazy unmount;
    returns the targets still mounted after the attempt."""
    still: list[str] = []
    for op in reversed(ops):
        try:
            op.unmount()
        except MountError:
            try:
                subprocess.run(["umount", "-l", op.target], check=True, capture_output=True)
            except subprocess.CalledProcessError:
                still.append(op.target)
    return still
