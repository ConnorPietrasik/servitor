"""Setuid spawn helper (root app, slot-uid agent).

The app runs privileged (root); the agent never is. Each session's pi
runs as a slot uid. This helper: asserts the caller is root (fail closed),
spawns with ``preexec_fn`` setgid/setuid, and asserts the child's uid from
/proc after spawn (the helper asserts; the caller trusts nothing).

asyncio note: ``create_subprocess_exec`` runs ``preexec_fn`` in the forked
child before exec; it must be called from the event loop's thread.
"""
from __future__ import annotations

import asyncio
import os
import pwd
from dataclasses import dataclass


class SpawnError(RuntimeError):
    """The child did not come up as the requested uid, or could not spawn."""


@dataclass(frozen=True)
class SlotIdentity:
    name: str
    uid: int
    gid: int
    home: str


def slot_identity(name: str) -> SlotIdentity:
    """Resolve a slot user from the passwd database (uids discovered, never
    hardcoded)."""
    try:
        pw = pwd.getpwnam(name)
    except KeyError as exc:
        raise SpawnError(f"slot user {name!r} does not exist (image build must create it)") from exc
    return SlotIdentity(name=name, uid=pw.pw_uid, gid=pw.pw_gid, home=pw.pw_dir)


def _child_uid(pid: int) -> int | None:
    """Read the child's real uid from /proc; None if it already exited."""
    try:
        with open(f"/proc/{pid}/status", "rb") as f:
            for line in f:
                if line.startswith(b"Uid:"):
                    return int(line.split()[1])
    except (FileNotFoundError, ProcessLookupError):
        return None
    return None


async def spawn_setuid(
    argv: tuple[str, ...] | list[str],
    *,
    slot: SlotIdentity,
    cwd: str,
    env: dict[str, str],
) -> asyncio.subprocess.Process:
    """Spawn ``argv`` setuid to ``slot``; assert the child's uid.

    Raises SpawnError if the caller is not root or the child's uid does not
    match. A child that exits before the uid can be read is still reported
    (the caller sees the exit via the process — classified as spawn failure
    upstream, never retried silently).
    """
    if os.geteuid() != 0:
        raise SpawnError("setuid spawn requires root (the app is privileged; the agent never is)")

    def preexec() -> None:  # runs in the child, between fork and exec
        os.setgid(slot.gid)
        os.setuid(slot.uid)

    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=env,  # explicit allowlist — never the app's environment
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        preexec_fn=preexec,
    )
    assert proc.pid is not None
    # the child is now exec-ing node (slow first step): read /proc while it
    # still exists; the setuid happened in the child before exec, so the uid
    # is already final.
    for _ in range(50):  # up to ~50 ms of grace
        uid = _child_uid(proc.pid)
        if uid is not None:
            break
        await asyncio.sleep(0.001)
    if uid is None and proc.returncode is None:
        uid = _child_uid(proc.pid)
    if uid is not None and uid != slot.uid:
        # never trust: kill what we cannot verify
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise SpawnError(f"child uid {uid} != slot uid {slot.uid} — refusing to continue")
    return proc
