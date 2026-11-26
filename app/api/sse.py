"""Per-session SSE event hub.

One hub per session. Every session event — pi events AND app events — is
published here, gets a monotonic integer id, and is kept in a bounded
ring (the per-session cap) for ``Last-Event-ID`` resume.

The ring is a replay buffer for delivery; it NEVER feeds back into the
run. A consumer that falls behind the ring — slow connection, backgrounded
phone, closed app — is simply evicted: the stream emits a ``gap`` event
(at subscribe and mid-stream) and the client re-syncs via the API
(transcript fetch + re-subscribe). The producer never awaits a client — a
dropped or slow connection never cancels, slows, or stalls a run. Memory
is bounded (cap + one in-flight event); the transcript on disk is never
touched.

The cap is a memory policy knob (``SERVITOR_SSE_RING_BYTES``, default
50 MB) — it sizes the resume window, never a capability cap.
"""
from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass
from typing import Any

# the per-session replay cap (production default; per-hub configurable via
# SseHub(cap_bytes=...), from SERVITOR_SSE_RING_BYTES)
EVENT_BUFFER_CAP = 50 * 1024 * 1024

# the heartbeat sentinel name stream_events yields (the endpoint maps it to
# a bare ":hb\n\n" comment line — no id, no data)
HEARTBEAT = "hb"


@dataclass
class SseEvent:
    id: int
    name: str
    data: str  # compact JSON, utf-8
    nbytes: int


def ring_cap(cap_bytes: int | None) -> int:
    """The replay cap for a hub: the configured value, or the default.

    One helper so the cap an operator tunes (SERVITOR_SSE_RING_BYTES)
    lands on EVERY hub.
    """
    return int(cap_bytes) if cap_bytes else EVENT_BUFFER_CAP


class SseHub:
    """Bounded per-session event ring with resume + gap semantics.

    All methods are called from the app's event loop (publish is
    synchronous by design — it runs inside the driver's gated dispatch).
    """

    def __init__(self, session_id: str, cap_bytes: int | None = None) -> None:
        self.session_id = session_id
        self.cap = ring_cap(cap_bytes)
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
        # the app's stop path — see close()
        self._closed = False

    # ------------------------------------------------------------------ #
    # publish
    # ------------------------------------------------------------------ #

    def on_event(self, record: dict[str, Any]) -> None:
        """Driver on_event sink: one pi record in, one numbered SSE event
        out (the event name is the record's ``type``)."""
        self.publish(str(record.get("type", "event")), record)

    def publish(self, name: str, data: dict[str, Any] | str) -> int:
        """Append one event; return its id. Synchronous. Never blocks on a
        consumer — eviction is unconditional (the gap contract)."""
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
        self._evict()
        for fut in list(self._waiters):
            if not fut.done():
                fut.set_result(None)
        self._waiters.clear()
        return ev.id

    def _evict(self) -> None:
        """Free events every live consumer already has, then evict to the
        cap. Eviction may move PAST a slow consumer — that is the gap
        contract (the stream detects the skip via gap_before). The run is
        never blocked by delivery.
        """
        mc = self._min_consumed()
        if mc:
            while self._ring and self._ring[0].id <= mc:
                old = self._ring.popleft()
                self._bytes -= old.nbytes
        while self._bytes > self.cap and len(self._ring) > 1:
            old = self._ring.popleft()
            self._bytes -= old.nbytes

    # ------------------------------------------------------------------ #
    # subscribe / consume
    # ------------------------------------------------------------------ #

    def subscribe(self, after_id: int = 0) -> tuple[int, list[SseEvent], bool]:
        """Register a live consumer. Returns (sub_id, replay, gap).

        ``replay`` is a SNAPSHOT (list), not a lazy view of the ring: the
        generator yields lazily and the publisher mutates the ring between
        yields; the list comprehension is synchronous (no await), so the
        snapshot is atomic with respect to the event loop.

        ``gap`` is True when the requested position has already been
        evicted — the client must re-sync via the API, not guess.
        """
        sub = self._next_sub
        self._next_sub += 1
        start = max(0, after_id) + 1
        gap = bool(self._ring) and self._ring[0].id > start
        self._consumers[sub] = max(after_id, 0)
        return sub, [e for e in self._ring if e.id >= start], gap

    def gap_before(self, cursor: int) -> bool:
        """True when the ring has evicted past the consumer's next
        position (cursor + 1): the stream must emit a ``gap`` and the
        client re-syncs via the API. An empty ring has been evicted past
        everything too — the publisher always keeps the newest event, so a
        non-empty check suffices."""
        return bool(self._ring) and self._ring[0].id > cursor + 1

    def events_after(self, cursor: int) -> list[SseEvent]:
        return [e for e in self._ring if e.id > cursor]

    def consume(self, sub_id: int, up_to_id: int) -> None:
        """A consumer reports it has received events through up_to_id."""
        prev = self._consumers.get(sub_id)
        if prev is None:
            return
        if up_to_id > prev:
            self._consumers[sub_id] = up_to_id

    def unsubscribe(self, sub_id: int) -> None:
        self._consumers.pop(sub_id, None)

    def close(self) -> None:
        """Terminal (the app's stop path): end every stream. Idempotent.

        An SSE response never terminates on its own — the browser holds it
        open for the life of the tab, and uvicorn's graceful shutdown only
        disables keep-alive — so without this, live waiters hold uvicorn's
        'waiting for connections' phase open to the unit's TimeoutStopSec
        SIGKILL. It wakes the waiters so the response generators exit on
        their next iteration; a waiter registered after the close returns
        at once (the wait loop checks ``closed``).
        """
        self._closed = True
        for fut in list(self._waiters):
            if not fut.done():
                fut.set_result(None)
        self._waiters.clear()

    @property
    def closed(self) -> bool:
        return self._closed

    async def wait_new(self, cursor: int) -> None:
        """Wait until an event with id > cursor exists — or the hub is
        closed (returns at once, whatever the cursor)."""
        loop = asyncio.get_running_loop()
        while not self._closed:
            fut: asyncio.Future = loop.create_future()
            self._waiters.add(fut)
            # re-check BOTH after registering: a close() that lands in the
            # window between the loop-top check and this point (close runs
            # in the SIGTERM handler, on the loop thread) has already
            # woken+cleared the set — the _latest_id check alone would then
            # await a future nobody will ever resolve.
            if self._latest_id > cursor or self._closed:
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

    @property
    def consumer_count(self) -> int:
        """Live SSE consumers (open chat tabs). The idle-kill guard
        ("a human is watching: never idle-kill") reads this."""
        return len(self._consumers)

    # ------------------------------------------------------------------ #
    # diagnostics
    # ------------------------------------------------------------------ #

    def _min_consumed(self) -> int:
        return min(self._consumers.values()) if self._consumers else 0

    def ring_snapshot(self, limit: int = 200) -> list[dict]:
        """Diagnostic view (bounded)."""
        return [
            {"id": e.id, "name": e.name, "nbytes": e.nbytes}
            for e in list(self._ring)[-limit:]
        ]


async def stream_events(
    hub: SseHub,
    sub_id: int,
    start: int,
    replay: list[SseEvent],
    gap: bool,
    is_disconnected,
    heartbeat_secs: float,
    kinds: frozenset[str] | None = None,
):
    """The live SSE tail for one subscription — the endpoint's streaming
    body. Yields ``(name, data, id_or_None)`` tuples; the endpoint formats
    them into frames. ``(HEARTBEAT, None, None)`` is the heartbeat sentinel.

    This loop only READS the ring — it never waits on the run for a
    client. A ``gap`` tuple is emitted whenever the consumer's position has
    been evicted — at subscribe or mid-stream — and the stream resumes from
    the newest event: the client re-syncs via the API, never a partial
    tail, and the ring is never re-sent after a gap. Events published while
    the client re-syncs still arrive (the cursor is snapshotted before the
    yield). Ends when the request disconnects or the hub is closed (the
    client's EventSource auto-reconnects, ?after= resumes).

    ``kinds`` (the app-wide stream only) is a per-subscription VIEW: this
    subscriber is delivered only the named kinds. The view never changes
    the transport — a filtered event still advances the cursor and is
    consumed, or a long-lived filtered stream pins the ring at its cap
    behind events it will never read, and position (hence gap detection and
    Last-Event-ID resume) counts every event, filtered or not. ``gap`` and
    the heartbeat are the stream's own frames, not ring events.
    """
    if gap:
        # the requested position was evicted: the client must re-sync from the API
        cursor = hub.latest_id
        yield ("gap", json.dumps({"after": start}), None)
        hub.consume(sub_id, cursor)
    else:
        cursor = start
        for e in replay:
            if await is_disconnected():
                return
            if kinds is None or e.name in kinds:
                yield (e.name, e.data, e.id)
            cursor = e.id
        hub.consume(sub_id, cursor)
    while True:
        if await is_disconnected():
            return
        try:
            await asyncio.wait_for(hub.wait_new(cursor), timeout=heartbeat_secs)
        except asyncio.TimeoutError:
            yield (HEARTBEAT, None, None)
            continue
        if hub.closed:
            # the stop path closed the hub: end the stream — the client's
            # EventSource auto-reconnects (?after=<cursor> resumes)
            return
        # the ring evicted past our cursor (slow consumer): gap — the client
        # re-syncs via the API; this stream continues from the newest event
        # at gap time (no partial tail, no ring re-send)
        if hub.gap_before(cursor):
            missed_from = cursor
            cursor = hub.latest_id
            yield ("gap", json.dumps({"after": missed_from}), None)
            hub.consume(sub_id, cursor)
            continue
        batch = hub.events_after(cursor)
        for e in batch:
            if await is_disconnected():
                return
            if kinds is None or e.name in kinds:
                yield (e.name, e.data, e.id)
        if batch:
            hub.consume(sub_id, batch[-1].id)
            cursor = batch[-1].id
