"""PiSession — one `pi --mode rpc` subprocess per conversation (PLAN §4/§5/§7).

Protocol rules encoded here (all verified against pi 1.5.2, docs/rpc.md,
docs/rpc-commands.md, docs/json.md):

- JSONL over stdio; LF-only framing (framer.py); correlate by ``id``.
- Subscribe (start the stdout dispatcher) BEFORE prompting — structural:
  the reader task starts in :meth:`PiSession.start`, before any command
  can be sent.
- stdout is protocol-only; stderr is captured (logged, NEVER parsed).
- ``prompt`` response ``disposition``: ``started`` | ``queued`` |
  ``handled``. ``handled`` = consumed, no run started — do NOT wait for
  ``agent_settled``. Otherwise the run ends at ``agent_settled`` (not
  ``agent_end``).
- A failed prompt response (``success: false``) is TERMINAL. Some failure
  modes emit zero events (no agent_start, no turn_start, no
  agent_settled; get_state still success:true and the process idles) — the
  driver ends the run with the sanitized reason; no automatic retry (a
  dead credential is a config fault; retrying burns the slot).
- ``model.id == "unknown"`` is the single no-usable-model signal: the
  spawn path fails closed on it before any prompt.
- Orderly shutdown on stdin EOF waits for pending work before exiting —
  the first rung of the ladder. A second signal exits at once — the
  SIGKILL rung is what guarantees exit.
- Extension dialogs (select/confirm/input/editor) block the child until
  answered: the driver parks them for the human (indefinitely — D4);
  fire-and-forget records (notify/setStatus/setWidget/setTitle/
  set_editor_text) are forwarded as events and NEVER answered (a stray
  reply desynchronizes the sub-protocol).
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.iso.spawn import SlotIdentity, spawn_setuid
from app.rpc.framer import LFFramer, encode_command
from app.rpc.launch import Launch

log = logging.getLogger("servitor.rpc")

DIALOG_METHODS = ("select", "confirm", "input", "editor")
FIRE_AND_FORGET_METHODS = ("notify", "setStatus", "setWidget", "setTitle", "set_editor_text")

SettledCallback = Callable[["PiSession", bool], Awaitable[None] | None]


class PromptRejected(RuntimeError):
    """A prompt/steer/follow_up command returned success:false — terminal.

    The run ends with this reason; the process keeps idling (healthy-
    looking); no retry (PLAN §4).
    """

    def __init__(self, command: str, reason: str):
        super().__init__(f"{command} rejected: {reason}")
        self.command = command
        self.reason = reason


class SpawnFailure(RuntimeError):
    """The process died before/without the protocol coming up, or an
    identity assertion failed. Fatal for the session — route to release,
    never a retry loop (a missing recorded workdir exits pi with code 1
    and NO response)."""

    def __init__(self, reason: str, *, exit_code: int | None = None, stderr_tail: str = ""):
        super().__init__(reason)
        self.exit_code = exit_code
        self.stderr_tail = stderr_tail


class ModelUnavailable(RuntimeError):
    """get_state.model.id == "unknown" — no usable model (models.json
    absent or credential unresolvable). Config error: fail closed before
    any prompt (PLAN §4)."""


@dataclass(frozen=True)
class CommandResult:
    command: str
    success: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class PiSession:
    """One pi subprocess: spawn, commands, events, dialogs, ladder.

    Event flow: every session event is delivered to ``on_event`` (the
    delivery layer's pump in stage 3; a listender in tests). Dialogs are
    delivered to ``on_dialog`` and answered via :meth:`answer_dialog`.
    """

    def __init__(
        self,
        launch: Launch,
        slot: SlotIdentity,
        *,
        cwd: str,
        startup_timeout: float = 15.0,
        request_timeout: float = 30.0,
        on_event: Callable[[dict[str, Any]], Any] | None = None,
        on_dialog: Callable[["PiSession", dict[str, Any]], Any] | None = None,
        on_exited: Callable[["PiSession"], Any] | None = None,
        on_settled: Callable[["PiSession", bool], Any] | None = None,
    ) -> None:
        self.launch = launch
        self.slot = slot
        self.cwd = cwd
        self.startup_timeout = startup_timeout
        self.request_timeout = request_timeout
        self.on_event = on_event
        self.on_dialog = on_dialog
        self.on_exited = on_exited
        self.on_settled = on_settled

        self._proc: asyncio.subprocess.Process | None = None
        self._launched_argv: tuple[str, ...] | None = None
        self._pending: dict[str, asyncio.Future[CommandResult]] = {}
        self._pending_order: list[str] = []
        self._id_gen = itertools.count(1)
        self._settled_waiters: list[asyncio.Future[bool]] = []
        self._last_settled_aborted: bool | None = None
        self._exited = asyncio.Event()
        self._exit_code: int | None = None
        self._run_active = False  # a run is in flight (prompt/steer/follow-up accepted, not yet settled)
        self._stderr_tail = deque[str](maxlen=4096)
        self._stderr_bytes = 0
        self._stdin_open = False
        self._pending_dialogs: dict[str, dict[str, Any]] = {}
        self._tasks: list[asyncio.Task] = []
        self._parse_errors: list[str] = []
        self._commands_sent: int = 0
        # backpressure gate (PLAN §3.3): cleared = pause the event stream.
        # The reader stops draining the child's stdout, the pipe fills, and
        # pi blocks on write — the RUN pauses (control responses are never
        # gated, so the control plane stays responsive). Set at all times
        # unless the delivery layer paused it.
        self._events_allowed = asyncio.Event()
        self._events_allowed.set()
        self.events_paused = False

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    @property
    def exited(self) -> bool:
        return self._exited.is_set()

    @property
    def exit_code(self) -> int | None:
        return self._exit_code

    @property
    def stderr_tail(self) -> str:
        return "".join(self._stderr_tail)

    @property
    def last_settled_aborted(self) -> bool | None:
        return self._last_settled_aborted

    @property
    def run_active(self) -> bool:
        """True while a run is in flight: set when a prompt/steer/
        follow-up is ACCEPTED (disposition != "handled"), cleared at
        ``agent_settled`` (abort settles too). The update tick uses
        this: a restart must never land mid-run (D8)."""
        return self._run_active

    def pause_events(self) -> None:
        """Backpressure: stop draining session events (the run pauses).

        In-flight control responses still flow (they are not gated). Safe
        to call repeatedly; :meth:`resume_events` re-opens the gate."""
        if not self.events_paused:
            self.events_paused = True
            self._events_allowed.clear()

    def resume_events(self) -> None:
        if self.events_paused:
            self.events_paused = False
            self._events_allowed.set()

    @property
    def pending_dialogs(self) -> dict[str, dict[str, Any]]:
        return dict(self._pending_dialogs)

    @property
    def launched_argv(self) -> tuple[str, ...] | None:
        """The argv the kernel actually executed, snapshotted at spawn
        (None if start() has not run or the child died before the
        snapshot). See _snapshot_launched_argv for why this must be
        spawn-time evidence."""
        return self._launched_argv

    async def _snapshot_launched_argv(self, proc) -> tuple[str, ...]:
        """Read /proc/<pid>/cmdline immediately after spawn.

        Node rewrites its own process title early in startup (pi's
        process.title becomes "pi"), so /proc/<pid>/cmdline is NOT valid
        launch-line evidence after the child has booted — the launch line
        must be captured at spawn. The first non-empty read is the
        original argv: the setuid preexec ran before the exec, and no JS
        has executed yet, so nothing could have rewritten the title.
        """
        for _ in range(150):  # up to ~300 ms for a slow exec to populate
            try:
                with open(f"/proc/{proc.pid}/cmdline", "rb") as f:
                    raw = f.read()
                parts = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
                if parts:
                    return tuple(parts)
            except OSError:
                pass
            await asyncio.sleep(0.002)
        raise SpawnFailure(
            f"could not snapshot the launched argv for pid {proc.pid} "
            "(child exited before the first read — a fatal spawn error)"
        )

    async def start(self) -> None:
        """Spawn setuid, start the dispatcher BEFORE any prompt can go out,
        and verify the child's uid (done by spawn_setuid).

        A spawn that cannot even exec (stored cwd gone, bad env, exec 127)
        surfaces as SpawnFailure — the fatal spawn error the caller must
        NOT retry (T14: a respawn into a missing cwd is a dead session,
        never a retry loop)."""
        try:
            proc = await spawn_setuid(
                list(self.launch.argv),
                slot=self.slot,
                cwd=self.cwd,
                env=self.launch.env_dict,
            )
        except OSError as e:
            raise SpawnFailure(
                f"spawn failed before exec: {e} (cwd={self.cwd})",
                exit_code=127,
                stderr_tail="",
            ) from e
        self._proc = proc
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
        self._stdin_open = True
        # launch-line evidence: what the kernel ran (before node rewrites
        # the process title). A child that dies before this read is a
        # fatal spawn error (missing cwd, bad env) — surfaced here.
        self._launched_argv = await self._snapshot_launched_argv(proc)

        # event dispatcher: subscribed before prompting (structural)
        self._tasks.append(asyncio.create_task(self._stdout_task(), name="pi-stdout"))
        self._tasks.append(asyncio.create_task(self._stderr_task(), name="pi-stderr"))
        self._tasks.append(asyncio.create_task(self._exit_task(), name="pi-exit"))

    async def _stdout_task(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        framer = LFFramer(self._on_record, self._on_parse_error)
        while True:
            chunk = await self._proc.stdout.read(65536)
            if not chunk:
                break
            for record in framer.split_records(chunk):
                rtype = record.get("type")
                if rtype != "response" and rtype != "extension_ui_request":
                    # session event: gated by the backpressure valve
                    # (PLAN §3.3). While paused this await blocks, the pipe
                    # stops draining, and pi's in-flight run pauses.
                    await self._events_allowed.wait()
                self._on_record(record)

    async def _stderr_task(self) -> None:
        """Capture stderr: logged, NEVER parsed as protocol data."""
        assert self._proc is not None and self._proc.stderr is not None
        while True:
            chunk = await self._proc.stderr.read(65536)
            if not chunk:
                break
            text = chunk.decode("utf-8", "replace")
            for line in text.splitlines():
                self._stderr_tail.append(line + "\n")
            self._stderr_bytes += len(chunk)
            if self._stderr_bytes > 512 * 1024:
                # keep the ring bounded; diagnostics only
                pass

    async def _exit_task(self) -> None:
        assert self._proc is not None
        code = await self._proc.wait()
        self._exit_code = code
        self._exited.set()
        # fail every outstanding command: a dead process resolves nothing
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(
                    SpawnFailure(
                        f"pi process exited (code {code}) while a command was in flight",
                        exit_code=code,
                        stderr_tail=self.stderr_tail[-2000:],
                    )
                )
        self._pending.clear()
        self._pending_order.clear()
        # a run can never settle after exit: fail the waiters
        for fut in self._settled_waiters:
            if not fut.done():
                fut.set_exception(
                    SpawnFailure(
                        f"pi process exited (code {code}) before agent_settled",
                        exit_code=code,
                        stderr_tail=self.stderr_tail[-2000:],
                    )
                )
        self._settled_waiters.clear()
        if self.on_exited:
            try:
                result = self.on_exited(self)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                log.exception("on_exited callback failed")

    def _on_parse_error(self, record_no: int, detail: str) -> None:
        self._parse_errors.append(f"record {record_no}: {detail}")
        log.error("pi stdout parse error (record %d): %s", record_no, detail)

    def _on_record(self, record: dict[str, Any]) -> None:
        rtype = record.get("type")
        if rtype == "response":
            self._on_response(record)
        elif rtype == "extension_ui_request":
            self._on_extension_ui(record)
        else:
            # session event (agent_*, message_*, tool_*, queue_update, ...)
            if rtype == "agent_settled":
                self._on_agent_settled(record)
            if self.on_event:
                try:
                    result = self.on_event(record)
                    if asyncio.iscoroutine(result):
                        self._tasks.append(asyncio.ensure_future(result))
                except Exception:
                    log.exception("on_event callback failed")

    def _on_response(self, record: dict[str, Any]) -> None:
        rid = record.get("id")
        if rid is None:
            # e.g. a parse-failure response (command "parse"): no correlation
            log.warning("uncorrelated pi response: %s", record)
            return
        fut = self._pending.pop(rid, None)
        if fut is None or fut.done():
            return
        fut.set_result(
            CommandResult(
                command=str(record.get("command", "")),
                success=bool(record.get("success", False)),
                data=record.get("data") or {},
                error=record.get("error"),
            )
        )

    def _on_agent_settled(self, record: dict[str, Any]) -> None:
        self._last_settled_aborted = bool(record.get("aborted", False))
        self._run_active = False
        for fut in self._settled_waiters:
            if not fut.done():
                fut.set_result(self._last_settled_aborted)
        self._settled_waiters.clear()
        if self.on_settled:
            try:
                self.on_settled(self, self._last_settled_aborted)
            except Exception:
                log.exception("on_settled callback failed")

    def _on_extension_ui(self, record: dict[str, Any]) -> None:
        method = str(record.get("method", ""))
        rid = str(record.get("id", ""))
        if method in DIALOG_METHODS:
            # blocks the child until answered — park for the human (D4:
            # wait indefinitely; never auto-allow, never auto-decline)
            self._pending_dialogs[rid] = record
            if self.on_dialog:
                try:
                    result = self.on_dialog(self, record)
                    if asyncio.iscoroutine(result):
                        self._tasks.append(asyncio.ensure_future(result))
                except Exception:
                    log.exception("on_dialog callback failed")
        elif method in FIRE_AND_FORGET_METHODS:
            # forward as an event; NEVER send a reply (a stray reply
            # desynchronizes the sub-protocol)
            if self.on_event:
                try:
                    self.on_event({"type": "extension_ui", "fire_and_forget": True, **record})
                except Exception:
                    log.exception("on_event callback failed (fire-and-forget)")
        else:
            log.warning("unknown extension_ui_request method %r (id=%s)", method, rid)

    # ------------------------------------------------------------------ #
    # commands
    # ------------------------------------------------------------------ #

    def _next_id(self) -> str:
        self._id_gen = self._id_gen  # keep the linter honest
        n = next(self._id_gen)
        return f"sv-{uuid.uuid4().hex[:8]}-{n}"

    async def request(
        self, cmd: dict[str, Any], *, timeout: float | None = None
    ) -> CommandResult:
        """Send one command, await the correlated response.

        Raises SpawnFailure if the process is gone / dies in flight,
        asyncio.TimeoutError on timeout.
        """
        if self._exited.is_set():
            raise SpawnFailure(
                "pi process already exited",
                exit_code=self._exit_code,
                stderr_tail=self.stderr_tail[-2000:],
            )
        rid = self._next_id()
        cmd = {"id": rid, **cmd}
        fut: asyncio.Future[CommandResult] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        self._pending_order.append(rid)
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write(encode_command(cmd))
        await self._proc.stdin.drain()
        self._commands_sent += 1
        try:
            return await asyncio.wait_for(fut, timeout or self.request_timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            raise

    # --- typed wrappers -------------------------------------------------- #

    async def get_state(self) -> dict[str, Any]:
        res = await self.request({"type": "get_state"})
        if not res.success:
            raise SpawnFailure(f"get_state failed: {res.error}")
        return res.data

    async def assert_model_usable(self) -> dict[str, Any]:
        """The spawn path's fail-closed model check (PLAN §4).

        ``model.id == "unknown"`` is the single no-usable-model signal.
        Returns the model object.
        """
        data = await self.get_state()
        model = data.get("model")
        if not model or model.get("id") == "unknown":
            raise ModelUnavailable(
                "get_state.model.id == 'unknown' — no usable model (models.json "
                "absent or credential unresolvable); failing closed before any prompt"
            )
        return model

    async def get_available_models(self) -> list[dict[str, Any]]:
        res = await self.request({"type": "get_available_models"})
        if not res.success:
            raise SpawnFailure(f"get_available_models failed: {res.error}")
        return list(res.data.get("models", []))

    async def get_messages(self) -> list[dict[str, Any]]:
        res = await self.request({"type": "get_messages"})
        if not res.success:
            raise SpawnFailure(f"get_messages failed: {res.error}")
        return list(res.data.get("messages", []))

    async def get_entries(self, since: str | None = None) -> dict[str, Any]:
        cmd: dict[str, Any] = {"type": "get_entries"}
        if since is not None:
            cmd["since"] = since
        res = await self.request(cmd)
        if not res.success:
            raise SpawnFailure(f"get_entries failed: {res.error}")
        return res.data

    async def get_tree(self) -> dict[str, Any]:
        res = await self.request({"type": "get_tree"})
        if not res.success:
            raise SpawnFailure(f"get_tree failed: {res.error}")
        return res.data

    async def get_session_stats(self) -> dict[str, Any]:
        res = await self.request({"type": "get_session_stats"})
        if not res.success:
            raise SpawnFailure(f"get_session_stats failed: {res.error}")
        return res.data

    async def get_commands(self) -> list[dict[str, Any]]:
        res = await self.request({"type": "get_commands"})
        if not res.success:
            raise SpawnFailure(f"get_commands failed: {res.error}")
        return list(res.data.get("commands", []))

    async def get_fork_messages(self) -> list[dict[str, Any]]:
        res = await self.request({"type": "get_fork_messages"})
        if not res.success:
            raise SpawnFailure(f"get_fork_messages failed: {res.error}")
        return list(res.data.get("messages", []))

    async def fork(self, entry_id: str) -> dict[str, Any]:
        res = await self.request({"type": "fork", "entryId": entry_id})
        if not res.success:
            raise SpawnFailure(f"fork failed: {res.error}")
        return res.data

    async def switch_session(self, session_path: str) -> bool:
        res = await self.request({"type": "switch_session", "sessionPath": session_path})
        if not res.success:
            raise SpawnFailure(f"switch_session failed: {res.error}")
        return not res.data.get("cancelled", False)

    # --- prompting & control --------------------------------------------- #

    async def prompt(
        self,
        text: str,
        *,
        images: list[dict[str, Any]] | None = None,
        streaming_behavior: str | None = None,
    ) -> str:
        """Send a prompt; return the disposition.

        ``disposition == "handled"`` => consumed, no run started: do NOT
        wait for agent_settled. ``success: false`` => terminal
        PromptRejected (zero-event failures included).
        """
        cmd: dict[str, Any] = {"type": "prompt", "message": text}
        if images:
            cmd["images"] = images
        if streaming_behavior:
            cmd["streamingBehavior"] = streaming_behavior
        res = await self.request(cmd)
        if not res.success:
            raise PromptRejected("prompt", res.error or "unknown error")
        disposition = str(res.data.get("disposition", "started"))
        if disposition != "handled":
            self._run_active = True
        return disposition

    async def steer(self, text: str) -> str:
        res = await self.request({"type": "steer", "message": text})
        if not res.success:
            raise PromptRejected("steer", res.error or "unknown error")
        disposition = str(res.data.get("disposition", "queued"))
        if disposition != "handled":
            self._run_active = True
        return disposition

    async def follow_up(self, text: str) -> str:
        res = await self.request({"type": "follow_up", "message": text})
        if not res.success:
            raise PromptRejected("follow_up", res.error or "unknown error")
        disposition = str(res.data.get("disposition", "queued"))
        if disposition != "handled":
            self._run_active = True
        return disposition

    async def abort(self) -> None:
        """RPC abort: waits for the session to become idle before responding
        (rpc-commands.md) — so a successful return means settled-aborted."""
        res = await self.request({"type": "abort"}, timeout=max(self.request_timeout, 120.0))
        if not res.success:
            raise PromptRejected("abort", res.error or "unknown error")

    async def wait_settled(self, *, timeout: float | None = None) -> bool:
        """Wait for agent_settled; return the ``aborted`` flag.

        Raises SpawnFailure if the process dies first (a dead process
        never settles). ``timeout=None`` waits forever (no caps — a
        20-minute run is normal; the human controls the run).

        Race note: for a prompt whose run may finish before this call is
        made, install the waiter with :meth:`settle_waiter` BEFORE sending
        the prompt (the pattern pi's own promptAndWait uses).
        """
        fut = self.settle_waiter()
        try:
            if timeout is None:
                return await fut
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._settled_waiters.remove(fut)
            raise

    def settle_waiter(self) -> asyncio.Future[bool]:
        """Install an agent_settled waiter (before prompting, to be
        race-free). Resolves with the aborted flag; raises SpawnFailure
        if the process exits first."""
        if self._exited.is_set():
            fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
            fut.set_exception(
                SpawnFailure(
                    "pi process already exited",
                    exit_code=self._exit_code,
                    stderr_tail=self.stderr_tail[-2000:],
                )
            )
            return fut
        fut = asyncio.get_running_loop().create_future()
        self._settled_waiters.append(fut)
        return fut

    # --- dialogs ----------------------------------------------------------- #

    async def answer_dialog(
        self,
        dialog_id: str,
        *,
        value: Any = None,
        confirmed: bool | None = None,
        cancelled: bool = False,
    ) -> None:
        """Answer a parked dialog with a matching id. Only dialog methods
        are ever answered; only with the id pi supplied."""
        rec = self._pending_dialogs.get(dialog_id)
        if rec is None:
            raise KeyError(f"no pending dialog with id {dialog_id!r}")
        resp: dict[str, Any] = {"id": dialog_id, "type": "extension_ui_response"}
        if cancelled:
            resp["cancelled"] = True
        else:
            if confirmed is not None:
                resp["confirmed"] = confirmed
            if value is not None:
                resp["value"] = value
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write(encode_command(resp))
        await self._proc.stdin.drain()
        # the child unblocks; the record stays parked until it does (the
        # response to a dialog produces no command response of its own)
        # -> remove once the child consumed it: we cannot know when, so
        # drop from the park now; re-asking an answered id raises KeyError.
        self._pending_dialogs.pop(dialog_id, None)

    # ------------------------------------------------------------------ #
    # shutdown ladder: stdin EOF -> SIGTERM -> SIGKILL
    # ------------------------------------------------------------------ #

    async def shutdown(
        self, *, eof_grace: float = 10.0, term_grace: float = 10.0
    ) -> int | None:
        """The kill ladder (PLAN §5). Returns the exit code.

        Rung 1: close stdin — orderly shutdown; pi waits for pending work.
        Rung 2: SIGTERM (a single signal does not interrupt an in-progress
        orderly shutdown — give it its grace).
        Rung 3: SIGKILL — a second signal exits at once; the rung that
        guarantees exit.
        """
        proc = self._proc
        if proc is None:
            return None
        if proc.returncode is None and self._stdin_open:
            try:
                proc.stdin.close()
                await proc.stdin.wait_closed()
            except (ConnectionResetError, BrokenPipeError):
                pass
            self._stdin_open = False
            try:
                await asyncio.wait_for(self._exited.wait(), eof_grace)
                return self._exit_code
            except asyncio.TimeoutError:
                pass
        if proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self._exited.wait(), term_grace)
                return self._exit_code
            except asyncio.TimeoutError:
                pass
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await self._exited.wait()
        return self._exit_code

    async def close(self) -> None:
        """Best-effort teardown after shutdown: reap tasks."""
        for t in self._tasks:
            if not t.done():
                t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()

    def classify_exit(self) -> str:
        """Exit classification for the meters (PLAN §7): a child
        OOM-killed by MemoryMax shows as 'killed by OOM', not hung or
        successful. Signal 9 = killed; 0 = clean; else crash."""
        if self._exit_code is None:
            return "running"
        if self._exit_code == 0:
            return "clean"
        if self._exit_code < 0:
            sig = -self._exit_code
            if sig == 9:
                return "killed (signal 9 — OOM under MemoryMax unless the ladder did it)"
            return f"killed (signal {sig})"
        return f"crash (exit {self._exit_code})"
