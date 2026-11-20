"""Servitor — home assistant with coding ability.

- app.rpc       one `pi --mode rpc` subprocess per session (framer, driver, ladder)
- app.iso       slots, overlays, chown handoff, setuid spawn, sweep, release()
- app.registry  SQLite: the only source of session truth
- app.delivery  uploads (staged → verified → atomic rename into the inbox), outbox, manifests
- app.api       HTTP/SSE routes + static UI serving
- app.doctor    servitorctl doctor

Env vars are read only in app/env.py (contract lint enforces it).
"""
