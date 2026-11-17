"""Pi RPC driver: one `pi --mode rpc` subprocess per session.

LF-only binary framing (never Node readline; never text-mode universal
newlines), id-correlated request/response futures, an always-on event
dispatcher (subscribed before any prompt), typed command wrappers, the
extension-UI responder (dialogs wait for the human; fire-and-forget records
are never answered), stderr capture (logged, never parsed), the shutdown
ladder (stdin EOF -> SIGTERM -> SIGKILL), `success:false` terminal handling,
and `model.id == "unknown"` fail-closed.
"""
