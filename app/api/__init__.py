"""HTTP/SSE API + static UI serving (PLAN §11).

Edge-gated, no auth of its own (invariant 3): anything that can reach the
socket can drive the agent; reachability is the control.
"""
