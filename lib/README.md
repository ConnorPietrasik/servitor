# lib/ — the durable, git-backed library

Shared, human-curated material the agent may reference. Pushed by
`deploy/sync.sh` to `/var/lib/servitor/lib` on the guest; mounted RO as
`./lib` in every session's workdir (one source for all sessions — no copy
per session).

Rules:
- Never written by the agent (the session gets an RO bind — the remount,ro,
  not the mode bits, is the control: lib's modes arrive from a host tar).
- Never GC'd (excluded from the state-volume sweep; the owner walk only
  touches slot-owned inodes).
- Changes ride the config/artifact sync: the push is ADDITIVE over the
  guest's tree — repo files always land and win, a repo deletion propagates
  through the previous push's manifest (`/var/lib/servitor/.lib-manifest`),
  and a file a human saved from the UI survives syncs.
- The UI's "save to lib" action (a session file's → lib button) links the
  file's stored bytes into `lib/` on the guest, through the blob store —
  the same inode the session's `./lib` mount shows every agent at once.
