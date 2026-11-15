"""Per-session SSE event hub (PLAN §3.3).

One hub per session. Every session event — pi events (agent_*,
message_*, tool_*, queue_update) AND app events (spawned, respawned,
idle_killed, interrupted, new_extension, waiting, released) — is
published here, gets a monotonic integer id, and is kept in a bounded
ring buffer (the 50 MB per-session cap) for ``Last-Event-ID`` resume.

Backpressure (PLAN §3.3): when the unconsumed bytes (between the
slowest live consumer and the newest event) exceed the cap, the hub
pauses the pi process's event stream (the driver stops draining the
child's stdout — the pipe fills and pi blocks, i.e. the RUN pauses).
When the slowest consumer catches up (below the resume threshold), the
stream resumes. Memory is bounded (~ring + one chunk in flight); the
transcript on disk is never touched (it is pi's own file and is
complete regardless of delivery speed).

The cap is a memory policy knob (``SERVITOR_SSE_RING_BYTES``, default
50 MB — DECISIONS.md): it bounds the delivery buffer, it is never a
capability cap. A single event larger than the cap is allowed (the
ring holds at most the cap plus one event for a live consumer); the
run pauses, it is never cut off.

Control responses (get_state, prompt ack, ...) are NOT gated — only
session events are — so the control plane stays responsive.
"""
from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass
from typing import Any

# the per-session delivery cap (PLAN §3.3) — a memory bound, not a
# capability cap: the run pauses, it is never cut off or truncated.
# The cap is per-hub configurable (SseHub(cap_bytes=...), sourced from
# SERVITOR_SSE_RING_BYTES); this constant is the production default.
EVENT_BUFFER_CAP = 50 * 1024 * 1024


@dataclass
class SseEvent:
    id: int
    name: str
    data: str  # compact JSON, utf-8
    nbytes: int


class SseHub:
    """Bounded per-session event buffer with resume + backpressure.

    All methods are called from the app's event loop (publish is
    synchronous by design — it runs inside the driver's gated dispatch).
    """

    def __init__(self, session_id: str, cap_bytes: int | None = None) -> None:
        self.session_id = session_id
        self.cap = int(cap_bytes) if cap_bytes else EVENT_BUFFER_CAP
        # hysteresis: resume once unconsumed falls below half the cap
        self.resume_threshold = self.cap // 2
        self._next_id = 1
        self._ring: deque[SseEvent] = deque()
        self._bytes = 0
        self._latest_id = 0
        # live consumers: sub_id -> last delivered event id
        self._consumers: dict[int, int] = {}
        self._next_sub = 1
        # publish -> waiters wake-up (lost-wakeup-free: a waiter registers
        # its future BEFORE re-checking the latest id)
        self._waiters: set[asyncio.Future] = set()
        # the driver this hub pauses/resumes (wired at spawn)
        self._session: Any | None = None
        self.paused = False
        self.pause_count = 0  # meter: how many pause episodes this session had

    # ------------------------------------------------------------------ #
    # publish
    # ------------------------------------------------------------------ #

    def on_event(self, record: dict[str, Any]) -> None:
        """Driver on_event sink: one pi record in, one numbered SSE event
        out (the event name is the record's ``type``)."""
        self.publish(str(record.get("type", "event")), record)

    def publish(self, name: str, data: dict[str, Any] | str) -> int:
        """Append one event; return its id. Synchronous."""
        if isinstance(data, str):
            data_str = data
        else:
            data_str = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        ev = SseEvent(
            id=self._next_id,
            name=name,
            data=data_str,
            nbytes=len(data_str.encode("utf-8")) + 48,  # framing overhead
        )
        self._next_id += 1
        self._latest_id = ev.id
        self._ring.append(ev)
        self._bytes += ev.nbytes
        if self._consumers:
            # NEVER evict ahead of a live consumer — that is silent data
            # loss. Free only what every consumer already has, and if the
            # slowest is still more than the cap behind, pause the run
            # (the pipe fills, pi blocks) instead of dropping data.
            mc = self._min_consumed()
            while self._ring and self._ring[0].id <= mc:
                old = self._ring.popleft()
                self._bytes -= old.nbytes
            if self._unconsumed_bytes() > self.cap:
                self._do_pause()
        else:
            # No live consumers: the ring is a bounded buffer — evict to
            # the cap (a consumer older than the ring gets a gap and
            # re-syncs from the API, never partial data).
            while self._bytes > self.cap and len(self._ring) > 1:
                old = self._ring.popleft()
                self._bytes -= old.nbytes
        for fut in list(self._waiters):
            if not fut.done():
                fut.set_result(None)
        self._waiters.clear()
        return ev.id

    # ------------------------------------------------------------------ #
    # subscribe / consume
    # ------------------------------------------------------------------ #

    def subscribe(self, after_id: int = 0) -> tuple[int, list[SseEvent], bool]:
        """Register a live consumer. Returns (sub_id, replay, gap).

        ``replay`` is a SNAPSHOT (list) of the ring's events with
        id > after_id (possibly empty). It must not be a lazy view of
        the ring: the generator yields lazily, and the publisher mutates
        the ring between yields (deque mutated during iteration). The
        list comprehension is synchronous (no await), so the snapshot is
        atomic with respect to the event loop.

        ``gap`` is True when the requested position has already been
        evicted — the client must re-sync via the API, not guess.
        """
        sub = self._next_sub
        self._next_sub += 1
        start = max(0, after_id) + 1
        gap = bool(self._ring) and self._ring[0].id > start
        self._consumers[sub] = max(after_id, 0)
        return sub, [e for e in self._ring if e.id >= start], gap

    def events_after(self, cursor: int) -> list[SseEvent]:
        return [e for e in self._ring if e.id > cursor]

    def consume(self, sub_id: int, up_to_id: int) -> None:
        """A consumer reports it has received events through up_to_id."""
        prev = self._consumers.get(sub_id)
        if prev is None:
            return
        if up_to_id > prev:
            self._consumers[sub_id] = up_to_id
            self._maybe_resume()

    def ack(self, sub_id: int, up_to_id: int) -> None:
        """Explicit position ack (POST .../events/ack) — same as consume."""
        self.consume(sub_id, up_to_id)

    def unsubscribe(self, sub_id: int) -> None:
        self._consumers.pop(sub_id, None)
        self._maybe_resume()

    async def wait_new(self, cursor: int) -> None:
        """Wait until an event with id > cursor exists (or forever)."""
        loop = asyncio.get_running_loop()
        while True:
            fut: asyncio.Future = loop.create_future()
            self._waiters.add(fut)
            if self._latest_id > cursor:
                self._waiters.discard(fut)
                return
            try:
                await fut
            finally:
                self._waiters.discard(fut)

    @property
    def latest_id(self) -> int:
        return self._latest_id

    @property
    def oldest_id(self) -> int:
        return self._ring[0].id if self._ring else 0

    @property
    def buffered_bytes(self) -> int:
        return self._bytes

    # ------------------------------------------------------------------ #
    # backpressure
    # ------------------------------------------------------------------ #

    def attach(self, session_driver) -> None:
        """Wire the driver this hub pauses/resumes (at spawn/respawn)."""
        self._session = session_driver

    def _min_consumed(self) -> int:
        return min(self._consumers.values()) if self._consumers else 0

    def _evict_delivered(self) -> None:
        """Free events every live consumer already has (id <= min
        consumed). With no live consumers, additionally evict to the cap
        (the ring is a bounded buffer either way)."""
        mc = self._min_consumed()
        if mc:
            while self._ring and self._ring[0].id <= mc:
                old = self._ring.popleft()
                self._bytes -= old.nbytes
        if not self._consumers:
            while self._bytes > self.cap and len(self._ring) > 1:
                old = self._ring.popleft()
                self._bytes -= old.nbytes

    def _unconsumed_bytes(self) -> int:
        if not self._consumers:
            return 0
        mc = self._min_consumed()
        return sum(e.nbytes for e in self._ring if e.id > mc)

    def _do_pause(self) -> None:
        if self.paused:
            return
        self.paused = True
        self.pause_count += 1
        if self._session is not None:
            try:
                self._session.pause_events()
            except Exception:
                pass

    def _maybe_resume(self) -> None:
        self._evict_delivered()
        if not self.paused:
            return
        if not self._consumers or self._unconsumed_bytes() <= self.resume_threshold:
            if self._session is not None:
                try:
                    self._session.resume_events()
                except Exception:
                    pass
            self.paused = False

    def ring_snapshot(self, limit: int = 200) -> list[dict]:
        """Diagnostic view (bounded)."""
        return [
            {"id": e.id, "name": e.name, "nbytes": e.nbytes}
            for e in list(self._ring)[-limit:]
        ]
