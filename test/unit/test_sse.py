"""Unit tests for the SSE hub (app/api/sse.py).

The production default cap is pinned here: 50 MB per session (PLAN
§3.3). The backpressure GATE (test/api/test_backpressure.py) runs the
same code path at a scaled cap — pi's cumulative message_update
semantics (each update carries the whole accumulated message) bound a
single context window's SSE volume far below 50 MB, so exercising the
mechanism end-to-end at 50 MB in one run is impossible by construction
(DECISIONS.md).
"""
from __future__ import annotations

import asyncio

from app.api.sse import EVENT_BUFFER_CAP, SseHub


def test_default_cap_is_50mb():
    """The production default ring cap is 50 MB per session (PLAN §3.3)."""
    assert EVENT_BUFFER_CAP == 50 * 1024 * 1024
    hub = SseHub("s1")
    assert hub.cap == 50 * 1024 * 1024
    assert hub.resume_threshold == 25 * 1024 * 1024


def test_custom_cap_and_threshold():
    hub = SseHub("s1", cap_bytes=1024 * 1024)
    assert hub.cap == 1024 * 1024
    assert hub.resume_threshold == 512 * 1024


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


def test_never_evict_ahead_of_live_consumer():
    """With a live consumer, events it has not received are NEVER
    evicted — the run pauses instead (the gate exercises this
    end-to-end; here: the ring keeps everything the consumer lacks)."""
    hub = SseHub("s1", cap_bytes=1000)
    hub.publish("t", {"i": 0})
    sub, _, _ = hub.subscribe(after_id=0)
    hub.consume(sub, 1)  # the consumer has event 1
    for i in range(2, 60):
        hub.publish("t", {"i": i, "pad": "x" * 100})
    # the consumer is behind by far more than the cap: the hub pauses
    # (no driver attached: pause is recorded, not actioned) and the
    # ring still holds everything after the consumer's position
    assert hub.paused is True
    missing = hub.events_after(1)
    assert [e.id for e in missing] == list(range(2, 60))
    # catch-up: consume + ack resumes
    hub.consume(sub, 59)
    assert hub.paused is False
    assert hub.pause_count == 1
    # now the ring can evict what everyone has
    hub._evict_delivered()
    assert hub.buffered_bytes == 0


def test_no_consumer_evicts_to_cap():
    hub = SseHub("s1", cap_bytes=1000)
    for i in range(50):
        hub.publish("t", {"i": i, "pad": "x" * 100})
    # bounded without consumers
    assert hub.buffered_bytes <= 1000 + 200  # cap + the newest event
    assert hub.paused is False
    assert hub.pause_count == 0


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
