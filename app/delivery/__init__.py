"""Delivery (PLAN §7): one asyncio producer per session drains pi events
into per-subscriber bounded queues that drop and coalesce on overflow and
never block; the wire contract (show_thinking none/final/all, coalesce,
flush-before-tool-ordering, heartbeat); reattach is a fetch, not a replay;
meters are computed by the pump/OS, never client-reported.
"""
