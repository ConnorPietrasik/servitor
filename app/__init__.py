"""Servitor — home assistant with coding ability.

Package layout:
- app.rpc       driver: LF-only framer, id-correlated futures, event
                dispatcher, dialog responder, shutdown ladders
- app.iso       slots, overlays, chown handoff, setuid spawn, sweep, release()
- app.registry  SQLite: the only source of session truth
- app.delivery  producers, per-subscriber queues, wire contract, meters
- app.api       HTTP/SSE routes + static UI serving
- app.doctor    servitorctl doctor

Env vars are read only in app/env.py (contract lint enforces it).
"""
