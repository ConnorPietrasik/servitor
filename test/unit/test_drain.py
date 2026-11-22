"""The app's stop-path drain (lifecycle.shutdown) — three phases, ONE
budget.

The contract under test:

- the per-session order stays load-bearing: the abort travels over
  stdin BEFORE that child's stdin closes, and a session with a run in
  flight is marked `interrupted` in the registry;
- the waits are SHARED: N children that each need their full share of
  the EOF window drain in ~grace, not N × grace;
- a child that does not exit on EOF gets SIGTERM after the shared
  window, then SIGKILL (never first) — without holding the other
  children's drain open;
- the budget: notify + close inside the EOF clock, TERM ≤ min(10,
  grace), KILL instant — worst case ≈ 100 s < TimeoutStopSec (120 s),
  independent of N.

Fake drivers stand in for the pi children (no subprocess, no root);
Lifecycle is built with __new__, the registry is real (sqlite).
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from app.lifecycle import Lifecycle
from app.registry import LIVE, Registry


# ---------------------------------------------------------------------- #
# fake pi child
# ---------------------------------------------------------------------- #


class FakeDriver:
    """A stand-in for a pi child: records the ladder steps it receives
    and exits (or not) per configuration, on the configured rung.

    ``exit_on_*``: False = never exits on that rung; float = delay in
    seconds before exiting (True = exit at once, code 0 for EOF)."""

    def __init__(
        self,
        *,
        run_active: bool = False,
        get_state: dict | Exception | None = None,
        exit_on_eof: bool | float = True,
        exit_on_term: bool | float = True,
        exit_on_kill: bool | float = True,
        die_on_get_state: bool = False,
        settle_on_abort: bool = False,
    ) -> None:
        self._exited = asyncio.Event()
        self._exit_code: int | None = None
        self._task: asyncio.Task | None = None
        self._run_active = run_active
        self._get_state = get_state
        self._exit_on_eof = exit_on_eof
        self._exit_on_term = exit_on_term
        self._exit_on_kill = exit_on_kill
        self._die_on_get_state = die_on_get_state
        self._settle_on_abort = settle_on_abort
        self.on_settled = None  # wired per test, as _make_driver does
        self.calls: list[str] = []

    @property
    def run_active(self) -> bool:
        return self._run_active

    @property
    def exited(self) -> bool:
        return self._exited.is_set()

    @property
    def exit_code(self) -> int | None:
        return self._exit_code

    async def get_state(self) -> dict:
        self.calls.append("get_state")
        if self._die_on_get_state:
            self._finish(0)  # the child dies while the probe is in flight
        if isinstance(self._get_state, Exception):
            raise self._get_state
        return self._get_state or {}

    async def abort(self) -> None:
        self.calls.append("abort")
        if self._settle_on_abort and self.on_settled is not None:
            # the real child: the abort RPC returns once the run has
            # settled ABORTED, and the child's agent_settled
            # notification then arrives on the stream — delivered on the
            # event loop a tick after the drain's mark_interrupted (the
            # ordering that raced the unconditional clear).
            async def _deliver() -> None:
                await asyncio.sleep(0)
                self.on_settled(self, True)

            asyncio.get_running_loop().create_task(_deliver())

    async def close_stdin(self) -> None:
        self.calls.append("close_stdin")
        self._exit_on(self._exit_on_eof, 0)

    def terminate(self) -> None:
        self.calls.append("terminate")
        self._exit_on(self._exit_on_term, -15)

    def kill(self) -> None:
        self.calls.append("kill")
        self._exit_on(self._exit_on_kill, -9)

    def wait_exited(self):
        return self._exited.wait()

    async def close(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    def _exit_on(self, cfg: bool | float, code: int) -> None:
        if cfg is False or self._exited.is_set():
            return
        # True = exit at once (guard the bool: True IS an int in Python)
        delay = (
            float(cfg)
            if isinstance(cfg, (int, float)) and not isinstance(cfg, bool)
            else 0.0
        )
        if delay:
            self._task = asyncio.get_running_loop().create_task(
                self._die_later(delay, code)
            )
        else:
            self._finish(code)

    async def _die_later(self, delay: float, code: int) -> None:
        await asyncio.sleep(delay)
        self._finish(code)

    def _finish(self, code: int) -> None:
        if self._exited.is_set():
            return
        self._exit_code = code
        self._exited.set()


# ---------------------------------------------------------------------- #
# lifecycle seam
# ---------------------------------------------------------------------- #


def _lc(tmp_path, grace: float, events: list | None = None) -> Lifecycle:
    """Lifecycle with __init__ skipped: real registry (sqlite); the
    drivers are the fakes, wired into lc.live per test. ``events``
    captures (session_id, kind, detail) tuples from the drain."""
    events = events if events is not None else []
    lc = Lifecycle.__new__(Lifecycle)
    lc.cfg = SimpleNamespace(
        shutdown_grace_sec=grace,
        registry=Registry(str(tmp_path / "registry.db")),
    )
    lc.shutting_down = False
    lc.events = SimpleNamespace(
        add=lambda *a, **k: events.append(
            (a[0], a[1], a[2] if len(a) > 2 else "")))
    lc.live = {}
    lc._hubs = {}
    lc._gc_task = None
    return lc


def _add(lc, session_id: str, driver: FakeDriver) -> None:
    lc.live[session_id] = SimpleNamespace(session_id=session_id, driver=driver)


def _drained(events: list, session_id: str) -> str:
    """The session's drained event detail — exactly one per session.
    """
    out = [d for s, k, d in events if s == session_id and k == "drained"]
    assert len(out) == 1, f"exactly one drained event per session, got {out}"
    return out[0]


def _row(tmp_path, session_id: str):
    """Re-open the registry (shutdown() closed its connection; the marks
    are committed, so a fresh connection sees them)."""
    reg = Registry(str(tmp_path / "registry.db"))
    try:
        return reg.get(session_id)
    finally:
        reg.close()


# ---------------------------------------------------------------------- #
# the shared budget
# ---------------------------------------------------------------------- #


def test_drain_is_max_not_sum(tmp_path):
    """N children that each take the same share of the EOF window drain
    in ~that share, not N × it (the sequential design's failure mode:
    N live sessions → N × ~101 s → past TimeoutStopSec)."""
    n, each = 4, 0.4
    events: list = []
    lc = _lc(tmp_path, grace=2.0, events=events)
    drivers = [FakeDriver(exit_on_eof=each) for _ in range(n)]
    for i, d in enumerate(drivers):
        _add(lc, f"s{i}", d)
    t0 = time.monotonic()
    asyncio.run(lc.shutdown(cause="test"))
    dt = time.monotonic() - t0
    assert all(d.exited and d.exit_code == 0 for d in drivers)
    assert all("close_stdin" in d.calls for d in drivers)
    for i in range(n):
        assert _drained(events, f"s{i}") == "exit=0"
    # sequential would be n × each (1.6 s); the shared budget is ~each.
    # Allow up to 3×each — far from either extreme.
    assert dt < each * 3, f"drain took {dt:.2f}s — the waits serialized"


def test_stuck_child_escalates_without_holding_the_others(tmp_path):
    """A child that ignores the EOF gets SIGTERM after the SHARED
    window (and exits there) while a healthy child drains in the same
    window — the stuck child's grace no longer serializes behind or in
    front of the others."""
    good = FakeDriver(exit_on_eof=0.1)
    stuck = FakeDriver(exit_on_eof=False, exit_on_term=0.1)
    events: list = []
    lc = _lc(tmp_path, grace=1.0, events=events)
    _add(lc, "s-good", good)
    _add(lc, "s-stuck", stuck)
    asyncio.run(lc.shutdown(cause="test"))
    assert good.exited and good.exit_code == 0
    assert stuck.exited and stuck.exit_code == -15
    assert "terminate" in stuck.calls, "stuck child must get the TERM rung"
    assert "kill" not in stuck.calls, "TERM was enough — KILL must not follow"
    assert _drained(events, "s-good") == "exit=0"
    assert _drained(events, "s-stuck") == "exit=-15 (SIGTERM)", \
        "the rung that ended the child must be visible in the event"


def test_wedged_child_is_killed_after_term(tmp_path):
    """SIGKILL is the rung that guarantees exit: a child that ignores
    both the EOF and the TERM gets killed, and the rungs arrive in
    order (never SIGKILL first)."""
    wedged = FakeDriver(exit_on_eof=False, exit_on_term=False, exit_on_kill=True)
    events: list = []
    lc = _lc(tmp_path, grace=0.4, events=events)
    _add(lc, "s1", wedged)
    asyncio.run(lc.shutdown(cause="test"))
    assert wedged.exited and wedged.exit_code == -9
    calls = wedged.calls
    assert calls.index("close_stdin") < calls.index("terminate") < calls.index("kill")
    assert _drained(events, "s1") == "exit=-9 (SIGKILL)"


def test_budget_holds_with_many_wedged_children(tmp_path):
    """The point of the redesign: K wedged children (each surviving EOF
    and TERM) cost ≈ the one-child worst case, not K × it — the stop
    stays inside its budget regardless of N."""
    grace = 0.3
    events: list = []
    wedged = [
        FakeDriver(exit_on_eof=False, exit_on_term=False, exit_on_kill=True)
        for _ in range(4)
    ]
    lc = _lc(tmp_path, grace=grace, events=events)
    for i, d in enumerate(wedged):
        _add(lc, f"s{i}", d)
    t0 = time.monotonic()
    asyncio.run(lc.shutdown(cause="test"))
    dt = time.monotonic() - t0
    assert all(d.exited and d.exit_code == -9 for d in wedged)
    for i in range(len(wedged)):
        assert _drained(events, f"s{i}") == "exit=-9 (SIGKILL)"
    # one-child worst case ≈ grace (EOF) + min(10, grace) (TERM) + ε;
    # the sequential design would be 4× that. Allow 2× the one-child
    # budget — 4 wedged children must not multiply it.
    one_child = grace + min(10.0, grace)
    assert dt < one_child * 2, f"drain took {dt:.2f}s — the budget multiplied"


# ---------------------------------------------------------------------- #
# the per-session order (still load-bearing)
# ---------------------------------------------------------------------- #


def test_abort_precedes_stdin_close_and_marks_interrupted(tmp_path):
    """A session with a run in flight gets its abort over stdin BEFORE
    its stdin closes (commands travel only over stdin), and the
    registry row is marked interrupted."""
    d = FakeDriver(run_active=True, get_state={"isStreaming": True})
    lc = _lc(tmp_path, grace=1.0)
    _add(lc, "s1", d)
    lc.cfg.registry.upsert_session("s1", slot="slot1", state=LIVE)
    asyncio.run(lc.shutdown(cause="test stop"))
    calls = d.calls
    assert calls.index("abort") < calls.index("close_stdin"), \
        "the abort must travel while stdin is still open"
    assert _row(tmp_path, "s1").interrupted is True, \
        "a run-active session must be marked interrupted"


def test_idle_session_is_not_aborted_or_marked(tmp_path):
    """An idle session is just closed (the graceful path) — no abort
    RPC, no interrupted mark."""
    d = FakeDriver(run_active=False, get_state={})
    lc = _lc(tmp_path, grace=1.0)
    _add(lc, "s1", d)
    lc.cfg.registry.upsert_session("s1", slot="slot1", state=LIVE)
    asyncio.run(lc.shutdown(cause="test stop"))
    assert "abort" not in d.calls, "an idle child has nothing to abort"
    assert d.exited and d.exit_code == 0
    assert _row(tmp_path, "s1").interrupted is False


def test_probe_failure_falls_back_to_the_local_flag(tmp_path):
    """A bounded get_state that fails (wedged child) must not silence
    the abort: the driver's own event-driven run_active flag is the
    fallback, and the session is still marked interrupted."""
    d = FakeDriver(run_active=True,
                   get_state=RuntimeError("rpc timed out"))
    lc = _lc(tmp_path, grace=1.0)
    _add(lc, "s1", d)
    lc.cfg.registry.upsert_session("s1", slot="slot1", state=LIVE)
    asyncio.run(lc.shutdown(cause="test stop"))
    assert "abort" in d.calls, "the local flag must carry the abort decision"
    assert _row(tmp_path, "s1").interrupted is True


def test_aborted_settle_cannot_unmark_the_drain(tmp_path):
    """The drain's own abort settles the child ABORTED: its
    agent_settled lands on the event loop a tick after the drain's
    mark_interrupted. The settle must not clear the mark — the run was
    interrupted on purpose, and the flag is the only meter a parked
    session has."""
    d = FakeDriver(run_active=True, get_state={"isStreaming": True},
                   settle_on_abort=True)
    lc = _lc(tmp_path, grace=1.0)
    _add(lc, "s1", d)
    # the wiring _make_driver does for every real driver
    d.on_settled = lambda _s, aborted: lc._on_settled("s1", aborted)
    lc.cfg.registry.upsert_session("s1", slot="slot1", state=LIVE)
    asyncio.run(lc.shutdown(cause="test stop"))
    assert "abort" in d.calls
    assert lc.live["s1"].last_settled is not None, \
        "the child's aborted settle must have been delivered"
    assert _row(tmp_path, "s1").interrupted is True, \
        "the drain's mark must survive the child's aborted settle"


def test_child_that_dies_during_notify_still_drains(tmp_path):
    """A child that exits while the notify phase probes it still gets
    its (single) drained event — no rung suffix: the graceful path
    ended it (or it ended itself), not the ladder."""
    d = FakeDriver(die_on_get_state=True)
    events: list = []
    lc = _lc(tmp_path, grace=1.0, events=events)
    _add(lc, "s1", d)
    asyncio.run(lc.shutdown(cause="test stop"))
    assert d.exited and d.exit_code == 0
    assert _drained(events, "s1") == "exit=0"
