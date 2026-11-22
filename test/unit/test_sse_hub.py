"""Unit tests for the SSE hub (app/api/sse.py).

The production default cap is pinned here: 50 MB per session. pi's
cumulative message_update semantics (each update carries the whole
accumulated message) bound a single context window's SSE volume far
below 50 MB, so exercising the mechanism end-to-end at 50 MB in one
run is impossible by construction.

The ring is a replay buffer only: the run never feeds back on delivery.
A slow consumer falls behind the ring, gets a gap, and re-syncs via
the API — the hub never blocks it or the run.
"""
from __future__ import annotations

import asyncio

from app.api.sse import EVENT_BUFFER_CAP, SseHub


def test_default_cap_is_50mb():
    """The production default ring cap is 50 MB per session."""
    assert EVENT_BUFFER_CAP == 50 * 1024 * 1024
    hub = SseHub("s1")
    assert hub.cap == 50 * 1024 * 1024


def test_custom_cap():
    hub = SseHub("s1", cap_bytes=1024 * 1024)
    assert hub.cap == 1024 * 1024


def test_ids_are_monotonic_and_gap_detected():
    hub = SseHub("s1")
    for i in range(5):
        hub.publish("t", {"i": i})
    sub, replay, gap = hub.subscribe(after_id=0)
    assert gap is False
    ids = [e.id for e in replay]
    assert ids == [1, 2, 3, 4, 5]
    # a position past the ring edge is a gap (re-sync, never partial)
    hub2 = SseHub("s2", cap_bytes=100)
    for i in range(50):
        hub2.publish("t", {"i": i, "pad": "x" * 200})
    sub2, replay2, gap2 = hub2.subscribe(after_id=0)
    assert gap2 is True, "a position evicted from the ring must be a gap"


def test_consumer_count_tracks_live_consumers():
    """The idle-kill guard reads this ("a human is watching") — it
    counts only live subscriptions and returns to zero as they drop."""
    hub = SseHub("s1")
    assert hub.consumer_count == 0
    sub, _, _ = hub.subscribe(after_id=0)
    assert hub.consumer_count == 1
    sub2, _, _ = hub.subscribe(after_id=0)
    assert hub.consumer_count == 2
    hub.unsubscribe(sub)
    assert hub.consumer_count == 1
    hub.unsubscribe(sub2)
    assert hub.consumer_count == 0


def test_slow_consumer_is_evicted_and_gets_a_gap():
    """A live consumer that falls behind by more than the cap is evicted
    (the ring stays bounded) — never does the hub (or the run) wait for
    it. gap_before() is what the stream uses to emit the ``gap`` event;
    a fresh subscribe at the evicted position also reports the gap.
    This is the owner's long-task contract: start a run, power the phone
    off, the run finishes unattended."""
    hub = SseHub("s1", cap_bytes=1000)
    hub.publish("t", {"i": 0})
    sub, _, _ = hub.subscribe(after_id=0)
    hub.consume(sub, 1)  # the consumer has event 1
    for i in range(2, 60):
        hub.publish("t", {"i": i, "pad": "x" * 100})
    # bounded despite the behind consumer:
    assert hub.buffered_bytes <= 1000 + 200  # cap + the newest event
    # the stream detects the skip:
    assert hub.gap_before(1) is True
    # and a re-subscribe at the evicted position is a gap (re-sync):
    _, _, gap = hub.subscribe(after_id=1)
    assert gap is True


def test_gap_before_false_while_consumer_is_current():
    hub = SseHub("s1", cap_bytes=1000)
    for i in range(5):
        hub.publish("t", {"i": i})
    assert hub.gap_before(0) is False  # event 1 is still in the ring
    assert hub.gap_before(4) is False  # current
    assert hub.gap_before(5) is False  # ahead (nothing missing)
    # consume everything, then evict the delivered prefix:
    sub, _, _ = hub.subscribe(after_id=0)
    hub.consume(sub, 5)
    hub._evict()
    assert hub.buffered_bytes == 0
    assert hub.gap_before(5) is False  # empty ring: nothing to gap on


def test_consumed_events_are_evicted():
    """Events every live consumer already received are freed immediately
    (the ring holds the undelivered tail + the resume window)."""
    hub = SseHub("s1", cap_bytes=10_000)
    for i in range(10):
        hub.publish("t", {"i": i})
    sub, _, _ = hub.subscribe(after_id=0)
    hub.consume(sub, 10)
    hub._evict()
    assert hub.buffered_bytes == 0
    assert hub.latest_id == 10


def test_no_consumer_evicts_to_cap():
    hub = SseHub("s1", cap_bytes=1000)
    for i in range(50):
        hub.publish("t", {"i": i, "pad": "x" * 100})
    # bounded without consumers
    assert hub.buffered_bytes <= 1000 + 200  # cap + the newest event
    # the newest event always survives (the publisher keeps it):
    assert hub.latest_id == 50
    assert hub.ring_snapshot()[-1]["id"] == 50


def test_wait_new_wakes_on_publish():
    async def go():
        hub = SseHub("s1")
        hub.publish("t", {"i": 0})
        got = asyncio.ensure_future(hub.wait_new(1))
        await asyncio.sleep(0)
        hub.publish("t", {"i": 1})
        await asyncio.wait_for(got, 1)

    asyncio.run(go())


def test_sse_event_framing_overhead_accounted():
    hub = SseHub("s1", cap_bytes=10_000)
    e1 = hub.publish("n", "data-" + "x" * 100)
    ev = hub._ring[0]
    assert ev.id == e1
    # nbytes includes the data + framing overhead (id/event/blank lines)
    assert ev.nbytes >= len(ev.data)


def test_close_wakes_blocked_waiters():
    """close() ends the wait: the response generator's wait_new returns
    on the next iteration, the stream ends, and uvicorn's 'waiting for
    connections' phase can no longer be held open by an SSE stream
    (the 2026-11-19 120 s stop: a browser-held SSE stream rode the stop
    out to TimeoutStopSec)."""

    async def go():
        hub = SseHub("s1")
        hub.publish("t", {"i": 0})
        w = asyncio.ensure_future(hub.wait_new(1))  # nothing past id 1 yet
        await asyncio.sleep(0)
        assert not w.done()
        hub.close()
        await asyncio.wait_for(w, timeout=1)

    asyncio.run(go())


def test_wait_new_returns_at_once_after_close():
    """A waiter arriving after the close must not block."""

    async def go():
        hub = SseHub("s1")
        hub.close()
        await asyncio.wait_for(hub.wait_new(0), timeout=1)
        assert hub.closed is True

    asyncio.run(go())


def test_close_is_idempotent_and_final():
    """Double close is a no-op; once closed, wait_new never blocks again
    (even with a cursor far past the ring edge)."""

    async def go():
        hub = SseHub("s1")
        hub.publish("t", {"i": 0})
        hub.close()
        hub.close()
        await asyncio.wait_for(hub.wait_new(99), timeout=1)

    asyncio.run(go())
