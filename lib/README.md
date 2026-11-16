# lib/ — the durable, git-backed library

Shared, human-curated material the agent may reference. Pushed by
`deploy/sync.sh` to `/var/lib/servitor/lib` on the guest; mounted RO into
every session at spawn (D: the same path for all — no copy per session).

Rules:
- Never written by the agent (the session gets an RO bind).
- Never GC'd (excluded from the state-volume sweep).
- Changes ride the config/artifact sync (versioned, doctor-gated).
