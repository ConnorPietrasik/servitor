"""The mid-stream gap contract (root-free, in-process).

A consumer that stays CONNECTED but stalls (slow phone on a bad link,
backgrounded app) must not stop the run: the ring evicts past its
cursor, the stream emits a ``gap`` event, and the client re-syncs via
the API. The run never feeds back on delivery — backpressure pauses the
consumer, never the run, so a stalled phone cannot park a task forever
(the long-task contract: start a task, turn the phone off, it finishes).

These tests drive the extracted ``stream_events`` generator (the
endpoint's streaming body) against the real SseHub with a small cap —
deterministic, no sockets: the consumer is literally not reading while
the publisher floods past the cap, which is the exact stall the
production path sees (a suspended generator + a running publisher).
"""
from __future__ import annotations

import asyncio
import json

from app.api.sse import SseHub, stream_events


def test_stalled_consumer_gets_mid_stream_gap_and_the_stream_continues():
    async def go():
        hub = SseHub("s1", cap_bytes=1000)
        sub, replay, gap = hub.subscribe(after_id=0)
        assert gap is False
        for i in range(3):
            hub.publish("t", {"i": i})

        disconnected = False

        async def is_disconnected() -> bool:
            return disconnected

        gen = stream_events(hub, sub, 0, replay, False, is_disconnected, 15)

        # take three events (the consumer is current at cursor 3)
        for i in range(3):
            name, data, eid = await asyncio.wait_for(anext(gen), 1)
            assert name == "t" and int(eid) == i + 1

        # the consumer STALLS (it is not reading) while the run keeps
        # publishing past the cap — the ring evicts past cursor 3
        async def flood() -> None:
            for i in range(10, 200):
                hub.publish("t", {"i": i, "pad": "x" * 100})
                await asyncio.sleep(0)

        task = asyncio.ensure_future(flood())
        await asyncio.sleep(0.2)
        await asyncio.wait_for(task, 5)
        assert hub.gap_before(3) is True  # the ring moved past the cursor

        # resume: the next frame must be a gap (re-sync via the API)
        name, data, eid = await asyncio.wait_for(anext(gen), 1)
        assert name == "gap" and eid is None
        assert json.loads(data)["after"] == 3

        # and the stream continues from the newest — one more event
        # (the run went on while the consumer was stalled)
        hub.publish("t", {"i": 999})
        name, data, eid = await asyncio.wait_for(anext(gen), 1)
        assert name == "t" and int(eid) == hub.latest_id
        assert json.loads(data)["i"] == 999

        # disconnect ends the stream (the normal phone-off path)
        disconnected = True
        hub.publish("t", {"i": 1000})  # must not matter: the stream is done
        try:
            await asyncio.wait_for(anext(gen), 1)
        except StopAsyncIteration:
            pass
        else:
            raise AssertionError("the stream must end on disconnect")

    asyncio.run(go())


def test_subscribe_after_eviction_is_a_gap_not_partial():
    """The phone-came-back path: the client reconnects at a position
    the ring has evicted — it gets a gap and re-syncs via the API, never
    a partial tail (a half transcript is worse than none). The ring is
    NOT re-sent on the old stream either: it duplicates the transcript
    fetch and costs up to the cap — the stream resumes from the newest
    event, so events published while the client re-syncs still arrive.
    """
    async def go():
        hub = SseHub("s1", cap_bytes=1000)
        for i in range(200):
            hub.publish("t", {"i": i, "pad": "x" * 100})
        sub, replay, gap = hub.subscribe(after_id=0)  # evicted position
        assert gap is True
        # the stream never sees a disconnect in this test (the fake is
        # async — stream_events awaits it, as the real endpoint's
        # request.is_disconnected is a coroutine)
        async def is_disconnected() -> bool:
            return False

        gen = stream_events(hub, sub, 0, replay, gap, is_disconnected, 15)
        name, data, eid = await asyncio.wait_for(anext(gen), 1)
        assert name == "gap" and json.loads(data)["after"] == 0

        # the ring is NOT re-sent after a gap (it duplicates the
        # transcript fetch and costs up to the cap): the next event the
        # stream delivers is the one the run publishes NOW (resuming
        # from the newest at gap time), not the head of the ring. (A
        # silence probe via wait_for(anext) would CANCEL the generator
        # on timeout — that is the client-going-away path, not a probe.)
        hub.publish("t", {"i": 999})
        name, data, eid = await asyncio.wait_for(anext(gen), 1)
        assert name == "t" and int(eid) == hub.latest_id
        assert json.loads(data)["i"] == 999

        # a re-subscribe at the CURRENT position (the UI's re-attach)
        # gets replay, not a gap
        _, replay2, gap2 = hub.subscribe(after_id=hub.latest_id)
        assert gap2 is False
        assert replay2 == []

    asyncio.run(go())
