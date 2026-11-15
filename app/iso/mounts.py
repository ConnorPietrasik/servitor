"""Per-session mounts (PLAN §5).

Mounted at allocate, before **any** spawn/respawn; live for the SESSION
lifetime — pi runs a session in its recorded cwd, so idle-kill never
unmounts; only release does (reverse order).

The four real mounts:

1. **Workdir overlay** — lower = /opt/toolkit (baked toolkit, a plain
   directory, never a mount), upper/work = scratch/<s>/{upper,work},
   merged at the stable per-session workdir path — the recorded cwd.
2. **Agent-dir overlay** — lower = config/v<N> (the version this session
   booted from — a plain subdirectory, never a mount; overlayfs won't show
   a nested mount inside a lowerdir), upper/work =
   scratch/<s>/{agent-upper,agent-work}. PI_CODING_AGENT_DIR = merged.
3. **inbox bind** — state/inbox/<s> into the merged workdir as ./inbox.
4. **models.json single-file RO bind** — over the merged agent dir (the
   agent could otherwise shadow it in the upper and steer the NEXT run's
   endpoint; the bearer key rides along — T34 verified EROFS on write,
   EBUSY on unlink, even as root).

Read-only to the agent = root-owned, slot-uid-unwritable.
"""
from __future__ import annotations

import asyncio
import os
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

    Note: the inbox BIND TARGET (``workdir/inbox``) is created only AFTER
    the workdir overlay is mounted (the target must exist in the merged
    view; a bind onto a missing target fails), and is chowned to the slot
    uid by the caller before the agent can see it — see mount_session.
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
        state.sessions_for(sp.session_id),
    ):
        os.makedirs(d, exist_ok=True)


def _chown_tree(path: str, uid: int, gid: int) -> None:
    os.chown(path, uid, gid)
    for dirpath, dirnames, filenames in os.walk(path):
        for d in dirnames:
            p = os.path.join(dirpath, d)
            try:
                os.chown(p, uid, gid)
            except OSError:
                pass
        for f in filenames:
            p = os.path.join(dirpath, f)
            try:
                os.chown(p, uid, gid)
            except OSError:
                pass


def mount_session(
    sp: SessionMountPaths,
    config_lower: str,
    state: StatePaths,
    slot_uid: int,
    slot_gid: int,
    toolkit_dir: str = TOOLKIT_DIR,
    agent_dir_ro: bool = False,
) -> list[MountOp]:
    """The full ordered mount sequence (PLAN §5). Returns the ops (for
    reverse-order teardown at release).

    ``scratch/<s>/`` is SLOt-uid-owned (PLAN §5 areas table): the app
    creates the dirs as root, then hands the whole per-session tree to
    the slot BEFORE any mount — a root-owned 0755 upper would be
    unwritable by the very session it exists for (the agent runs as the
    slot uid). The outbox dir (the workdir convention, SYSTEM.md) is
    likewise chowned once it exists in the merged view.
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
        state.sessions_for(sp.session_id),
    ):
        _chown_tree(d, slot_uid, slot_gid)
    ops = plan_session_mounts(sp, config_lower, state, toolkit_dir, agent_dir_ro)
    # 1: workdir overlay
    ops[0].mount()
    # the inbox bind target, created post-overlay and handed to the slot
    inbox_target = f"{sp.workdir}/inbox"
    os.makedirs(inbox_target, exist_ok=True)
    os.chown(inbox_target, slot_uid, slot_gid)
    # 2-4: agent-dir overlay, inbox bind, models.json RO bind
    outbox = f"{sp.workdir}/outbox"  # the workdir convention (SYSTEM.md)
    os.makedirs(outbox, exist_ok=True)
    os.chown(outbox, slot_uid, slot_gid)
    for op in ops[1:]:
        op.mount()
    return ops


def plan_session_mounts(
    sp: SessionMountPaths,
    config_lower: str,
    state: StatePaths,
    toolkit_dir: str = TOOLKIT_DIR,
    agent_dir_ro: bool = False,
) -> list[MountOp]:
    """The four mounts, in mount order (teardown is the reverse).

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
            # a stuck mount is a release failure: it gets reported (the
            # sweep will see the mountpoint), never silently skipped
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
