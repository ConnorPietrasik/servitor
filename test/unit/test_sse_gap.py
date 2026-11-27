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


def test_kinds_view_delivers_only_the_asked_kinds_and_consumes_the_rest():
    """The app-wide stream can be viewed down to a subset of kinds (a
    screen that needs two kinds must not pay the wire bytes for the rest).
    The view must not change the TRANSPORT: a filtered event still advances
    the cursor and is consumed — pinning it here, because an unconsumed
    event pins the ring at its cap behind events this subscriber will never
    read, and position (hence gap detection and Last-Event-ID resume)
    counts every event, filtered or not."""

    async def go():
        hub = SseHub("*")
        hub.publish("agent_start", {"session_id": "s1"})  # id 1, replay side
        hub.publish("keep_deleted", "s1/a.md")            # id 2, replay side
        sub, replay, gap = hub.subscribe(0)
        assert gap is False and len(replay) == 2

        async def is_disconnected() -> bool:
            return False

        gen = stream_events(
            hub, sub, 0, replay, gap, is_disconnected, 15,
            kinds=frozenset({"keep_deleted"}),
        )
        # only the asked kind is delivered (from the replay), with its real id
        name, data, eid = await asyncio.wait_for(anext(gen), 1)
        assert name == "keep_deleted" and eid == 2
        assert data == "s1/a.md"

        # resume the generator: the replay loop finishes and consumes through
        # it, then the view waits live. A filtered event now arrives — it
        # must be CONSUMED even though it is never yielded. (The pending
        # anext stays blocked on the next, never-arriving event; asyncio.run
        # cancels it at shutdown.)
        asyncio.ensure_future(anext(gen))
        await asyncio.sleep(0.2)
        assert hub._consumers[sub] == 2
        hub.publish("agent_end", {"session_id": "s1"})  # id 3, filtered
        await asyncio.sleep(0.2)
        assert hub._consumers[sub] == 3, (
            "a filtered event is still consumed — unconsumed, it pins the "
            "ring at its cap behind events this subscriber will never read"
        )

    asyncio.run(go())


def test_a_kinds_view_still_gets_its_gap():
    """``gap`` is the stream's own frame, not a ring event: it is not a kind
    and rides whatever view the subscription asked for — or a client that
    filtered the feed kinds would never learn to re-sync."""

    async def go():
        hub = SseHub("*", cap_bytes=300)
        for i in range(50):
            hub.publish("agent_start", {"i": i, "pad": "x" * 60})
        sub, replay, gap = hub.subscribe(0)  # evicted position
        assert gap is True and replay

        async def is_disconnected() -> bool:
            return False

        gen = stream_events(
            hub, sub, 0, replay, gap, is_disconnected, 15,
            kinds=frozenset({"keep_deleted"}),
        )
        name, data, eid = await asyncio.wait_for(anext(gen), 1)
        assert name == "gap" and eid is None
        assert json.loads(data)["after"] == 0

    asyncio.run(go())
