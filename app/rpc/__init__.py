"""Pi RPC driver: one `pi --mode rpc` subprocess per session.

LF-only binary framing, id-correlated request/response futures, an
always-on event dispatcher, the extension-UI responder, the shutdown
ladder (stdin EOF -> SIGTERM -> SIGKILL), `success:false` terminal
handling, and `model.id == "unknown"` fail-closed.
"""
