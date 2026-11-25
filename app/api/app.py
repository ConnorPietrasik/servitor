"""The servitor HTTP/SSE API.

One published port (the container's loopback ingress, edge-only; the
Portcullis panel is the only public front). The front door over the same
Lifecycle the stdio protocol used — lifecycle semantics unchanged.

Routes:

    GET    /healthz
    GET    /api/v1/sessions
    POST   /api/v1/sessions
    GET    /api/v1/sessions/{id}
    POST   /api/v1/sessions/{id}/prompt
    POST   /api/v1/sessions/{id}/steer
    POST   /api/v1/sessions/{id}/follow-up
    POST   /api/v1/sessions/{id}/abort
    GET    /api/v1/sessions/{id}/probe
    GET    /api/v1/sessions/{id}/messages
    POST   /api/v1/sessions/{id}/fork
    GET    /api/v1/sessions/{id}/dialogs
    POST   /api/v1/sessions/{id}/dialogs/{dialog_id}/answer
    POST   /api/v1/sessions/{id}/stable
    POST   /api/v1/sessions/{id}/release
    GET    /api/v1/sessions/{id}/events            (SSE; Last-Event-ID resume)
    GET    /api/v1/events                          (app-wide SSE: session list, no poll;
                                                     ?kinds=a,b is a per-subscription
                                                     view of it — only those kinds
                                                     are delivered)
    GET    /api/v1/sessions/{id}/files             (ETag/304; outbox + uploads)
    POST   /api/v1/sessions/{id}/files/upload      (one-shot; no size cap;
                                                     a mid-body drop answers 499
                                                     and leaves the part resumable)
    POST   /api/v1/sessions/{id}/files/keep        (a hardlink into keep/<sid>/)
    POST   /api/v1/sessions/{id}/files/attach      (another session's file, or "keep")
    GET    /api/v1/sessions/{id}/files/archive     (streamed zip; repeated path=)
    GET    /api/v1/sessions/{id}/files/{path}      (outbox/... | uploads/...)
    PATCH  /api/v1/sessions/{id}/files/uploads/{path}    (rename, within the inbox)
    PATCH  /api/v1/sessions/{id}/files/outbox/{path}     (rename/move inside outbox)
    DELETE /api/v1/sessions/{id}/files/uploads/{path}
    DELETE /api/v1/sessions/{id}/files/outbox/{path}
    POST   /api/v1/sessions/{id}/staged            (open a resumable upload)
    GET    /api/v1/sessions/{id}/staged            (what is staged — the resume list)
    PUT    /api/v1/sessions/{id}/staged/{uid}/chunk        (append AT offset; 409 carries it)
    POST   /api/v1/sessions/{id}/staged/{uid}/complete     (hash whole, deliver)
    DELETE /api/v1/sessions/{id}/staged/{uid}      (explicit cancel)
    GET    /api/v1/files                           (the kept shelf, across sessions)
    GET    /api/v1/files/keep/{path}               (download a kept file)
    DELETE /api/v1/files/keep/{path}               (un-keep; the only keep deletion)
    GET    /api/v1/usage                           (state-volume byte meters)
    POST   /panel/sessions/{id}/action             (whitelist)

Rules: authz is per session (released/unknown ⇒ 404, other sessions never
leaked; file paths cannot escape the session dirs, and root reads only the
inodes the session's own uid owns). The static UI fallback
only serves files that RESOLVE inside ui/dist (the app runs as root; a
percent-decoded ``..`` or an escaping symlink must not walk out). The LLM
key is env-only — never in any request or response. SSE: monotonic integer
ids per session; replay from the bounded ring via Last-Event-ID/?after=;
a position past the ring edge is a ``gap`` event (client re-syncs via the
API — never partial data). The run NEVER feeds back on delivery: a slow
consumer that falls behind the ring simply gets a gap. No caps: uploads/
downloads stream to/from disk.

Admission: the edge credential gate is active when ``SERVITOR_EDGE_KEY``
is set — always in production (baked into the image at build, re-injected
by the sync's key slice, certified by the doctor's keyed/keyless probes);
unset in dev/standalone, where the app authenticates nobody and the bind is
the control. When active, EVERY request must carry
``X-Servitor-Edge-Key`` — the value the Traefik ``servitor-edge``
middleware injects after the auth chain, overwriting anything a client
sends. :class:`EdgeKeyMiddleware` (pure-ASGI; wraps the whole app, the
/assets StaticFiles mount included — route-level dependencies never reach
a mount; websocket upgrades are checked the same way) constant-time-
verifies it before any route runs and answers one uniform 403 for missing
vs wrong. Why the credential: the guest firewall admits the port from the
WHOLE edge subnet (the proxy's source IP is indistinguishable from a
neighbor's), and that subnet carries the public gpu fleet — a compromised
neighbor must not drive the agent or read its files directly. It stops no
internet attacker (Authelia 2FA + the admins-only rule stays the human
gate) — its population is unauthenticated in-subnet callers.

Extension dialogs (select/confirm/input/editor) park in the session's
process until the human answers (or aborts): the pending set is listed via
GET .../dialogs and answered via POST .../dialogs/{id}/answer. pi may
resolve one on its own (its timeout, or an abort); the run's settle sweeps
the park and publishes `extension_dialog_resolved`. A dialog dies with its
process; nothing survives a respawn.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import signal
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel
from starlette.datastructures import Headers
from starlette.requests import ClientDisconnect
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.sse import HEARTBEAT, stream_events
from app.delivery.archive import ArchiveError, stream_zip
from app.delivery.store import DeliveryError, FileNotDelivered, mime_for, sniff_mime
from app.delivery.upload import OffsetMismatch, StagedError
from app.iso.spawn import slot_identity
from app.lifecycle import GLOBAL_EVENTS, PROBES, Lifecycle, SessionNotResumable
from app.iso.slots import SlotOutsidePool
from app.registry import SessionRow
from app.rpc.session import PromptRejected, SpawnFailure

log = logging.getLogger("servitor.api")

API_VERSION = "v1"
PANEL_ACTIONS = ("start", "steer", "prompt", "abort", "release")

# The edge credential header. The value is injected by Traefik's
# `servitor-edge` headers middleware AFTER the auth chain — customRequestHeaders
# overwrites any client-supplied value, so a neighbor on the edge subnet
# cannot forge it without the secret.
EDGE_KEY_HEADER = "X-Servitor-Edge-Key"


class EdgeKeyMiddleware:
    """The edge credential gate — active only when the key is set.

    Configured: every request must carry the value the edge injects;
    the app constant-time-verifies it before any route runs, one uniform
    403 for missing vs wrong (no oracle). Unset or empty: no gate at all
    (dev/standalone — the bind is the control); production is ALWAYS
    keyed, so a keyless production boot is caught, not merely
    discouraged.

    Pure-ASGI on purpose, in both directions: it must wrap the whole app
    (the /assets mount is a separate ASGI app that FastAPI route
    dependencies never see), and it must not buffer or intercept
    ``receive`` (BaseHTTPMiddleware breaks the SSE streaming +
    disconnect contract). ``lifespan`` passes through untouched;
    a websocket upgrade is checked the same way — the app is only ever
    called with a valid key, the consumed ``connect`` is re-delivered —
    and a missing/wrong key closes the upgrade without accepting, which
    the server turns into a 403.
    """

    def __init__(self, app: ASGIApp, key: bytes) -> None:
        self.app = app
        self.key = key

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            if self.key:
                provided = Headers(scope=scope).get(EDGE_KEY_HEADER, "").encode("latin-1")
                if not secrets.compare_digest(provided, self.key):
                    await JSONResponse({"detail": "forbidden"}, status_code=403)(
                        scope, receive, send
                    )
                    return
            await self.app(scope, receive, send)
            return
        if scope["type"] == "lifespan":
            # middleware stacks sit above the router: lifespan must reach
            # the app untouched
            await self.app(scope, receive, send)
            return
        # Websocket upgrade: the gate's "EVERY request" claim must not
        # depend on the transport staying SSE. The upgrade carries the same
        # headers, so the same check applies; the connect message is
        # consumed here and re-delivered once, because the app expects to
        # receive it first.
        if self.key:
            connect = await receive()
            provided = Headers(scope=scope).get(EDGE_KEY_HEADER, "").encode("latin-1")
            if not secrets.compare_digest(provided, self.key):
                # close WITHOUT accepting: the app is never called, and the
                # server rejects the upgrade (uvicorn answers 403)
                await send({"type": "websocket.close"})
                return
            delivered = False

            async def gated_receive() -> Message:
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return connect
                return await receive()

            receive = gated_receive
        await self.app(scope, receive, send)


class PromptBody(BaseModel):
    text: str


class SteerBody(BaseModel):
    text: str


class StableBody(BaseModel):
    stable: bool = True


class StartBody(BaseModel):
    display_name: str | None = None


class ReleaseBody(BaseModel):
    reason: str = "user close"


class DialogAnswerBody(BaseModel):
    value: str | None = None
    confirmed: bool | None = None
    cancelled: bool = False


class ForkBody(BaseModel):
    entry_id: str


class RenameBody(BaseModel):
    # The target name. A bare name or path (no area prefix, no leading slash)
    # stays in the source's own area — the request URL already says which.
    # A leading slash is the explicit form and must name the source's area.
    # The OTHER area's prefix crosses areas and is refused: a move across
    # areas is a copy (a keep / an upload), not a rename.
    to: str


class KeepBody(BaseModel):
    # "outbox/report.md" or "uploads/photos/dataset.csv" — the same
    # addressing the download route uses, so a keep can only ever name a
    # file the human can already see and download.
    path: str
    as_name: str | None = None  # keep it under a different path in the shelf


class AttachBody(BaseModel):
    source: str  # another session id, or "keep" for the kept shelf
    path: str  # "outbox/<path>" / "uploads/<path>" (session), relpath (keep)
    as_name: str | None = None


class PanelActionBody(BaseModel):
    action: str
    text: str | None = None


# ---------------------------------------------------------------------- #
# helpers
# ---------------------------------------------------------------------- #


def _row_dict(r: SessionRow, lc: Lifecycle) -> dict:
    live = lc.live.get(r.id)
    busy = (
        live is not None
        and live.driver is not None
        and not live.driver.exited
        and live.driver.run_active
    )
    dialogs_pending = (
        len(live.driver.pending_dialogs)
        if live is not None and live.driver is not None and not live.driver.exited
        else 0
    )
    return {
        "id": r.id,
        "display_name": r.display_name,
        "slot": r.slot,
        "state": r.state,
        "busy": busy,
        "stable": r.stable,
        "interrupted": r.interrupted,
        "session_file": r.session_file,
        "config_version": r.config_version,
        "update_waiting": r.update_waiting,
        "new_extensions": list(r.new_extensions),
        "last_activity": r.last_activity,
        "live_process": live is not None and live.driver is not None and not live.driver.exited,
        "dialogs_pending": dialogs_pending,
    }


def _get_row(app_state, sid: str) -> SessionRow:
    row = app_state.registry.get(sid)
    if row is None or row.state == "free":
        raise HTTPException(status_code=404, detail="no such session")
    return row


def _live_driver(app_state, sid: str):
    live = app_state.lc.live.get(sid)
    if live is None or live.driver is None or live.driver.exited:
        raise HTTPException(status_code=409, detail="no live process for this session")
    return live.driver


def _start_pos(hub, after: int | None, last_event_id: str | None, head: bool) -> int:
    """Where a stream begins.

    A position past the ring edge is a ``gap`` (the client re-syncs via the
    API). ``head`` starts at the newest event with NO replay — the app-wide
    stream is a "something changed, refetch" notifier, and replaying its ring
    to a phone that just opened the app is the exact waste this exists to
    remove. A reconnect carrying Last-Event-ID still resumes exactly where it
    stopped: the caller's position always beats ``head``.
    """
    if after is not None:
        return after
    if last_event_id is not None:
        try:
            return int(last_event_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="bad Last-Event-ID")
    if head:
        return hub.latest_id
    return 0


def _sse_frame(name: str, data: str, eid: int | None = None) -> str:
    out = []
    if eid is not None:
        out.append(f"id: {eid}")
    out.append(f"event: {name}")
    for line in data.split("\n"):
        out.append(f"data: {line}")
    out.append("")
    out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------- #
# app factory
# ---------------------------------------------------------------------- #


def _install_http_stop_handler(app_state) -> None:
    """Replace uvicorn's SIGTERM with the app's stop path.

    Called from the ASGI lifespan startup — by then uvicorn's own handler
    is in place, and ``signal.signal`` overrides it for the server's life
    (installing before ``serve()`` would not work: uvicorn saves the
    pre-existing handler as the "original" and masks it).

    uvicorn's handler only sets ``should_exit``: in-flight SSE streams
    keep the 'waiting for connections' phase open (uvicorn does not
    cancel an in-flight streaming response), so the stop would run to
    the unit's TimeoutStopSec SIGKILL with the abort-then-drain never
    running and ``shutting_down`` never set. The app's handler:
    ``shutting_down`` now, close every stream, only then let uvicorn
    exit — the connection wait ends at once and the lifespan shutdown
    runs the drain (bounded < TimeoutStopSec). No-op outside the HTTP
    front door (no ``http_server``).
    """
    server = getattr(app_state, "http_server", None)
    if server is None:
        return

    def _on_term(_sig, _frame):
        app_state.lc.shutting_down = True
        app_state.lc.close_streams()
        server.should_exit = True

    signal.signal(signal.SIGTERM, _on_term)


def _ui_dist() -> Path:
    """The built static UI (ui/dist) at the repo root. Indirected so a
    test can point the SPA fallback at a scratch dist."""
    return Path(__file__).resolve().parents[2] / "ui" / "dist"


def build_app(app_state, cfg) -> FastAPI:
    """Build the FastAPI app over the App core (registry + lifecycle +
    delivery). ``cfg`` is the validated env Config."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        app_state.lc.start_gc()
        # the outbox sweep (file deltas for the UI). Only the HTTP front door
        # starts it: a delta with no SSE consumer anywhere is waste, and the
        # stdio front door has none.
        app_state.lc.start_files()
        _install_http_stop_handler(app_state)
        yield
        await app_state.lc.shutdown(cause="app stop")

    app = FastAPI(title="servitor", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception):
        # log + surface the reason (a bare 500 body is not debuggable over the one published port)
        log.exception("unhandled error in %s", _request.url.path)
        return JSONResponse(
            status_code=500,
            content={"error": f"{type(exc).__name__}: {exc}"},
        )

    @app.exception_handler(SessionNotResumable)
    async def _not_resumable(_request: Request, exc: SessionNotResumable):
        # a respawn-on-pickup route (prompt, panel start, fork) met a
        # terminal row: a conflict with the session's state — the client
        # stops, it does not retry into a session that is gone
        log.info("respawn refused for %s (state %s)", exc.session_id, exc.state)
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(SlotOutsidePool)
    async def _slot_outside_pool(_request: Request, exc: SlotOutsidePool):
        # the row's slot name is not in the pool: a config-vs-registry
        # conflict — 409 with the remedy, not a server fault
        log.info("release refused: %s", exc)
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    # Edge credential gate — registered BEFORE the CORS middleware below:
    # last-added is outermost, and a CORS preflight (OPTIONS, no key) must
    # reach CORSMiddleware first. The credential is injected server-side by
    # the proxy, so the browser never sees or sends it — same-origin UI,
    # SSE and file downloads need no client change.
    # (getattr, not attribute: a cfg built outside load_http_config that
    # lacks the key gets an EMPTY key — gate off (dev/standalone semantics)
    # instead of build_app crashing mid-startup; `or ""` because
    # load_http_config hands out None for an unset key.)
    app.add_middleware(
        EdgeKeyMiddleware,
        key=(getattr(cfg, "edge_key", "") or "").encode("latin-1"),
    )

    if cfg.browser_origin:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[cfg.browser_origin],  # exact match; never *
            allow_methods=["GET", "POST", "DELETE", "PATCH", "OPTIONS"],
            allow_headers=["*"],
            allow_credentials=False,
        )

    # ------------------------------------------------------------------ #
    # health / list
    # ------------------------------------------------------------------ #

    @app.get("/healthz")
    async def healthz():
        return {
            "ok": True,
            "version": "0.1.0",
            "api": API_VERSION,
            "boot_reconciled": app_state.boot_report,
        }

    @app.get(f"/api/{API_VERSION}/usage")
    async def usage():
        """State-volume meters: bytes per area, per session, and the
        filesystem totals. A report, never a limit.

        ``gc_idle_days`` rides along because the list screen pairs a
        session's held bytes with its idle-GC countdown ("held · GC in Nd").
        """
        report = dict(await asyncio.to_thread(app_state.lc.usage.report))
        report["gc_idle_days"] = app_state.lc.cfg.gc_idle_days
        return report

    @app.get(f"/api/{API_VERSION}/sessions")
    async def list_sessions():
        # released sessions are GONE (404 everywhere); the list shows live
        # sessions only, most recent first: last_activity descending, with
        # created_at as the tiebreak so two rows touched in the same instant
        # fall back to mint order (newest first)
        rows = app_state.registry.list_sessions(include_released=False)
        rows.sort(key=lambda r: (r.last_activity, r.created_at), reverse=True)
        return {"sessions": [_row_dict(r, app_state.lc) for r in rows]}

    @app.post(f"/api/{API_VERSION}/sessions")
    async def create_session(body: StartBody):
        try:
            res = await app_state.lc.start_session(body.display_name)
        except RuntimeError as e:
            raise HTTPException(status_code=503, detail=str(e))
        return res

    # ------------------------------------------------------------------ #
    # per-session
    # ------------------------------------------------------------------ #

    @app.get(f"/api/{API_VERSION}/sessions/{{sid}}")
    async def get_session(sid: str):
        return _row_dict(_get_row(app_state, sid), app_state.lc)

    @app.post(f"/api/{API_VERSION}/sessions/{{sid}}/prompt")
    async def prompt(sid: str, body: PromptBody):
        _get_row(app_state, sid)
        try:
            disposition = await app_state.lc.prompt(sid, body.text)
        except PromptRejected as e:
            raise HTTPException(status_code=502, detail=e.reason)
        except SpawnFailure as e:
            raise HTTPException(status_code=502, detail=f"spawn: {e.reason}")
        return {"disposition": disposition}

    @app.post(f"/api/{API_VERSION}/sessions/{{sid}}/steer")
    async def steer(sid: str, body: SteerBody):
        _get_row(app_state, sid)
        driver = _live_driver(app_state, sid)
        try:
            disposition = await driver.steer(body.text)
        except PromptRejected as e:
            raise HTTPException(status_code=502, detail=e.reason)
        if disposition != "handled":
            await app_state.lc.retire_after_write(sid, driver)
        return {"disposition": disposition}

    @app.post(f"/api/{API_VERSION}/sessions/{{sid}}/follow-up")
    async def follow_up(sid: str, body: SteerBody):
        _get_row(app_state, sid)
        driver = _live_driver(app_state, sid)
        try:
            disposition = await driver.follow_up(body.text)
        except PromptRejected as e:
            raise HTTPException(status_code=502, detail=e.reason)
        if disposition != "handled":
            await app_state.lc.retire_after_write(sid, driver)
        return {"disposition": disposition}

    @app.post(f"/api/{API_VERSION}/sessions/{{sid}}/abort")
    async def abort(sid: str):
        _get_row(app_state, sid)
        driver = _live_driver(app_state, sid)
        try:
            await driver.abort()
        except PromptRejected as e:
            raise HTTPException(status_code=502, detail=e.reason)
        return {"ok": True}

    @app.get(f"/api/{API_VERSION}/sessions/{{sid}}/probe")
    async def probe(sid: str, which: str = "messages"):
        _get_row(app_state, sid)
        if which not in PROBES:
            raise HTTPException(status_code=400, detail=f"unknown probe: {which!r}")
        live = app_state.lc.live.get(sid)
        busy = (
            live is not None
            and live.driver is not None
            and not live.driver.exited
            and live.driver.run_active
        )
        try:
            result = await app_state.lc.probe(sid, which)
        except ValueError:
            # fresh session, no transcript yet: the probe's short-lived spawn
            # is refused by the first-touch rule; the busy state is everything
            # a waiter polling this endpoint needs
            return {"result": None, "busy": busy}
        return {"result": result, "busy": busy}

    # ------------------------------------------------------------------ #
    # transcript fetch + fork/branch
    # ------------------------------------------------------------------ #

    @app.get(f"/api/{API_VERSION}/sessions/{{sid}}/messages")
    async def messages(sid: str):
        """Authoritative active-branch messages: the UI's reattach base.
        The SSE stream is the LIVE TAIL only — on load the client fetches
        here and then follows events from the leaf; it never replays."""
        try:
            return await app_state.lc.fetch_messages(sid)
        except KeyError:
            raise HTTPException(status_code=404, detail="no such session")

    @app.post(f"/api/{API_VERSION}/sessions/{{sid}}/fork")
    async def fork(sid: str, body: ForkBody):
        """Fork from a previous user message ('edit that message').
        Dead process ⇒ respawn first. An extension can cancel the fork:
        then `forked` is false, `cancelled` is true, and nothing changed."""
        _get_row(app_state, sid)
        try:
            out = await app_state.lc.fork(sid, body.entry_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="no such session")
        except SessionNotResumable:
            raise  # terminal row: the app-level handler maps it to 409,
            # NOT the 503 below (a retryable upstream failure) — without
            # this the blanket RuntimeError catch would shadow it
        except RuntimeError as e:
            raise HTTPException(status_code=503, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"fork: {e}")
        return {"forked": not out.get("cancelled", False), **out}

    @app.post(f"/api/{API_VERSION}/sessions/{{sid}}/stable")
    async def set_stable(sid: str, body: StableBody):
        _get_row(app_state, sid)
        app_state.registry.set_stable(sid, body.stable)
        return {"ok": True}

    @app.post(f"/api/{API_VERSION}/sessions/{{sid}}/release")
    async def release(sid: str, body: ReleaseBody):
        _get_row(app_state, sid)
        out = await app_state.lc.release(sid, reason=body.reason)
        return {
            "released": out.released,
            "quarantined": out.quarantined,
            "errors": out.errors,
        }

    # ------------------------------------------------------------------ #
    # SSE events
    # ------------------------------------------------------------------ #

    def _stream(
        hub, request: Request, after: int | None, head: bool = False,
        kinds: frozenset[str] | None = None,
    ) -> StreamingResponse:
        """One SSE subscription: replay from the ring, then the live tail.

        Shared by the per-session stream and the app-wide one — the resume
        contract (Last-Event-ID / ?after=, gap on eviction, 503 while the
        stop path drains) must be identical on both, or a client learns to
        trust one and silently miss events on the other.
        """
        if hub is None:
            # no hub: the row is released, or stuck mid-release (quarantine).
            # Terminal either way — every further request 404s too, so the
            # client should stop (EventSource closes permanently on non-200)
            raise HTTPException(status_code=404, detail="no such session")
        if hub.closed:
            # per-hub terminal (defense-in-depth: shutting_down always
            # precedes close_streams)
            raise HTTPException(status_code=503, detail="shutting down")
        last_event_id = request.headers.get("last-event-id")
        start = _start_pos(hub, after, last_event_id, head)
        sub, replay, gap = hub.subscribe(start)

        async def gen():
            try:
                yield ":servitor\n\n"
                async for name, data, eid in stream_events(
                    hub, sub, start, replay, gap,
                    request.is_disconnected, cfg.heartbeat_secs, kinds,
                ):
                    if name == HEARTBEAT:
                        yield ":hb\n\n"
                    else:
                        yield _sse_frame(name, data, eid)
            finally:
                hub.unsubscribe(sub)

        headers = {
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
            "X-Sub-Id": str(sub),
        }
        return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)

    @app.get(f"/api/{API_VERSION}/sessions/{{sid}}/events")
    async def events(
        sid: str,
        request: Request,
        after: int | None = Query(default=None, ge=0),
    ):
        _get_row(app_state, sid)
        if app_state.lc.shutting_down:
            # the stop path is draining: gate BEFORE hub() — hub() is lazy,
            # so a session with no hub yet would get a fresh OPEN hub that
            # the stop path would never close. The client's EventSource
            # backs off and retries against the new instance once the
            # restart lands.
            raise HTTPException(status_code=503, detail="shutting down")
        return _stream(app_state.lc.hub(sid), request, after)

    @app.get(f"/api/{API_VERSION}/events")
    async def global_events(
        request: Request,
        after: int | None = Query(default=None, ge=0),
        head: bool = Query(default=False),
        kinds: str | None = Query(default=None),
    ):
        """App-wide stream: lifecycle transitions and run state for every
        session. This is what lets the sessions list follow reality on its
        own — a phone on a metered plan must not poll the whole list every
        few seconds to learn that a run started.

        ``?head=1`` (what the list screen uses) takes no replay: it wants
        "did anything change?", not history. A reconnect with
        Last-Event-ID resumes exactly where it stopped either way.

        ``?kinds=a,b`` is a per-subscription view of the stream: this
        subscriber is delivered only the named kinds (``gap`` rides whatever
        the view is — it is the stream's own resync frame, not a kind), so a
        screen that needs two kinds does not pay the wire bytes for the
        rest. The stream still carries exactly GLOBAL_EVENTS — the view is
        a subset of the contract, never a rename, and an unknown kind is a
        400 — ``gap`` excepted: it is the one name a view may name that the
        stream never carries as a ring event (a view of just it is a stream
        of nothing but resync signals).
        """
        if app_state.lc.shutting_down:
            raise HTTPException(status_code=503, detail="shutting down")
        kindset: frozenset[str] | None = None
        if kinds:
            asked = {k.strip() for k in kinds.split(",") if k.strip()}
            # gap is the stream's own resync frame, not a GLOBAL_EVENTS kind:
            # it rides every view, and a view may name it (a stream of
            # nothing but resync signals) without a 400
            unknown = asked - set(GLOBAL_EVENTS) - {"gap"}
            if unknown:
                raise HTTPException(
                    status_code=400,
                    detail=f"unknown kind(s): {', '.join(sorted(unknown))}",
                )
            if asked:
                kindset = frozenset(asked)
        return _stream(
            app_state.lc.global_hub, request, after, head=head, kinds=kindset
        )

    # ------------------------------------------------------------------ #
    # files (delivery)
    # ------------------------------------------------------------------ #

    def _session_files(sid: str):
        row = _get_row(app_state, sid)
        try:
            slot = slot_identity(row.slot)
        except Exception:
            slot = None
        return row, app_state.delivery.files(app_state.state, sid, slot)

    @app.get(f"/api/{API_VERSION}/sessions/{{sid}}/files")
    async def list_files(sid: str, request: Request):
        """The session's files: outbox artifacts + the human's uploads.

        ETag/304 on the whole payload: the UI takes it once and then lives on
        ``files_changed`` deltas, but a re-attach after a gap or a tab reopen
        hits it, and an unchanged listing must not cost a phone the whole
        body again. The sweep also stamps `stable` and seeds the
        server-side snapshot the deltas diff against.
        """
        row, sfiles = _session_files(sid)
        uploads, (outbox, delta) = await asyncio.to_thread(
            lambda: (sfiles.list_uploads(), app_state.lc.files.sweep(sid, sfiles))
        )
        if delta is not None and not app_state.lc.shutting_down:
            # the fetch is a sweep driver like the timer: its delta lands on
            # the session's hub while the hub has a watcher, or the snapshot
            # it just wrote would make the timer diff past a change the
            # session's other watchers (a second open tab) never get. The
            # lookup must not MINT, and the publish must not go to a hub
            # with no watcher: a later subscriber starts from a full
            # listing, not a replay, so a delta nobody can receive is bytes
            # a metered phone pays once and uses never (the timer's "swept
            # only while someone can receive it" rule)
            hub = app_state.lc.hub_if_present(sid)
            if hub is not None and not hub.closed and hub.consumer_count > 0:
                hub.publish("files_changed", delta)
        body = json.dumps(
            {"outbox": outbox, "uploads": uploads},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        etag = '"' + hashlib.blake2b(body, digest_size=16).hexdigest() + '"'
        if request.headers.get("if-none-match") == etag:
            # ~200 bytes instead of the whole listing, sha strings included
            return Response(status_code=304, headers={"ETag": etag})
        return Response(
            content=body,
            media_type="application/json",
            headers={"ETag": etag, "Cache-Control": "no-cache"},
        )

    @app.post(f"/api/{API_VERSION}/sessions/{{sid}}/files/upload")
    async def upload(sid: str, request: Request, name: str = Query(...)):
        """The one-shot: one stream, one part, nobody to answer for the next
        offset. Driven here, in the route, so the
        upload_id is at hand when it matters: a mid-body drop is not an
        abort — what landed is staged until its TTL. The 499 and its resume
        handle are BEST-EFFORT: the client that disconnected is usually
        already gone (the body then lands only in the log), so the resume
        path that never depends on this response is the staged list —
        GET /staged shows the part and its size.
        """
        row, sfiles = _session_files(sid)
        staged = app_state.delivery.staged(sid)
        try:
            uid, _offset = await asyncio.to_thread(staged.create, name)
        except StagedError as e:
            # a name the store will refuse to land (a NUL, an all-dot name):
            # refused at create, before any byte is on disk — a bad request,
            # not a server fault, and no residue for the TTL to find
            raise HTTPException(status_code=400, detail=str(e))
        try:
            await staged.append_stream(uid, 0, request.stream())
        except FileNotFoundError:
            # the part vanished under a concurrent cancel or TTL sweep
            raise HTTPException(status_code=404, detail="no such staged upload")
        except ClientDisconnect:
            # the radio gave up mid-body: what landed is on disk (the part
            # is resumable until its TTL), nothing was delivered — an
            # expected interruption, not a server fault. The 499 rides back
            # only if the client is within earshot and always lands in the
            # log; the resume handle in the body is best-effort, the staged
            # list is the resume path that does not depend on it
            try:
                size = await asyncio.to_thread(staged.size, uid)
            except (StagedError, OSError):
                size = 0  # the part was swept under us; the handle is stale either way
            log.info("upload %s aborted by client disconnect at offset %d", name, size)
            return JSONResponse(
                status_code=499,
                content={
                    "detail": "client closed the request",
                    "upload_id": uid,
                    "offset": size,
                    "resume": f"PUT /api/v1/sessions/{sid}/staged/{uid}/chunk?offset={size}",
                },
            )
        except StagedError as e:
            # the id resolved, then the upload directory vanished (a
            # concurrent cancel or TTL sweep): the resolved path's own 404
            raise HTTPException(status_code=404, detail=str(e) or "no such staged upload")
        try:
            f = await app_state.delivery.complete_staged(staged, uid, sfiles)
        except StagedError:
            raise HTTPException(status_code=404, detail="no such staged upload")
        except DeliveryError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except FileNotFoundError:
            # the part vanished under a concurrent cancel or TTL sweep
            raise HTTPException(status_code=404, detail="no such staged upload")
        app_state.lc.events.add(sid, "file_uploaded", f"{f.name} {f.size}B {f.mime}")
        app_state.lc.usage.invalidate()
        return JSONResponse(
            status_code=201,
            content={"name": f.name, "sha256": f.sha256, "size": f.size, "mime": f.mime},
        )

    @app.delete(f"/api/{API_VERSION}/sessions/{{sid}}/files/uploads/{{name:path}}")
    async def delete_upload(sid: str, name: str):
        """Delete one upload (the human's own file, their call).

        Resolution is the download's — escape-gated, symlink-gated, and
        bound to inodes the session's uid owns — so a delete can never reach
        a byte a download could not. The manifest entry goes with it.
        """
        _row, sfiles = _session_files(sid)
        try:
            gone = await asyncio.to_thread(sfiles.delete_upload, name)
        except (FileNotDelivered, DeliveryError):
            raise HTTPException(status_code=404, detail="no such upload")
        app_state.lc.events.add(sid, "file_deleted", f"uploads/{gone['name']}")
        app_state.lc.usage.invalidate()
        return {"deleted": f"uploads/{gone['name']}", "size": gone["size"]}

    @app.delete(f"/api/{API_VERSION}/sessions/{{sid}}/files/outbox/{{path:path}}")
    async def delete_outbox(sid: str, path: str):
        """Delete one artifact. Same resolution rules as its download."""
        _row, sfiles = _session_files(sid)
        try:
            gone = await asyncio.to_thread(sfiles.delete_outbox, path)
        except (FileNotDelivered, DeliveryError):
            raise HTTPException(status_code=404, detail="no such file")
        app_state.lc.events.add(sid, "file_deleted", f"outbox/{gone['path']}")
        app_state.lc.usage.invalidate()
        return {"deleted": f"outbox/{gone['path']}", "size": gone["size"]}

    @app.patch(f"/api/{API_VERSION}/sessions/{{sid}}/files/uploads/{{name:path}}")
    async def rename_upload_route(sid: str, name: str, body: RenameBody):
        """Rename or move an upload, within the inbox (contained both ways,
        no symlink on either path, destination must not exist). ``to`` is a
        bare name or path, or ``uploads/<path>``; a leading slash or the
        outbox prefix is refused. The manifest entry moves with the file —
        it is what proves the inode is the app's blob, so the two cannot
        diverge."""
        _row, sfiles = _session_files(sid)
        try:
            moved = await asyncio.to_thread(
                app_state.delivery.rename, sfiles, f"uploads/{name}", body.to
            )
        except (FileNotDelivered, DeliveryError) as e:
            raise HTTPException(status_code=404, detail=str(e) or "no such upload")
        app_state.lc.events.add(sid, "file_renamed", f"uploads/{name} -> {moved['to']}")
        return moved

    @app.patch(f"/api/{API_VERSION}/sessions/{{sid}}/files/outbox/{{path:path}}")
    async def rename_outbox_route(sid: str, path: str, body: RenameBody):
        """Rename or move an artifact inside the outbox (contained both
        ways, no symlink on either path, destination must not exist).
        ``to`` is a bare name or path (stays in the outbox) or
        ``outbox/<path>``; a leading slash or the uploads prefix is refused."""
        _row, sfiles = _session_files(sid)
        try:
            moved = await asyncio.to_thread(
                app_state.delivery.rename, sfiles, f"outbox/{path}", body.to
            )
        except (FileNotDelivered, DeliveryError) as e:
            raise HTTPException(status_code=404, detail=str(e) or "no such file")
        app_state.lc.events.add(sid, "file_renamed", f"outbox/{path} -> {moved['to']}")
        # no usage.invalidate(): a rename moves no bytes — the meter's cache
        # is still exactly right, and invalidating would bill a full
        # state-volume re-walk for a number that did not change
        return moved

    @app.get(f"/api/{API_VERSION}/sessions/{{sid}}/files/archive")
    async def archive_files(sid: str, path: list[str] = Query(...)):
        """One file, a folder, a whole area, or an explicit list of any of
        those (repeated ``path=`` — a multi-select download) as a ZIP,
        streamed.

        Built from the gated listing and re-gated per entry, so it is exactly
        what the human can see — and nothing is written to disk first: an
        archive of a tree on a nearly full volume must not need room for the
        archive.
        """
        _row, sfiles = _session_files(sid)
        try:
            name, entries = await asyncio.to_thread(
                app_state.delivery.archive_entries, sfiles, path
            )
        except DeliveryError as e:
            raise HTTPException(status_code=404, detail=str(e))
        safe = name.replace('"', "").replace("/", "_")

        def gen():
            # a sync generator: Starlette iterates it in a thread, so hashing
            # and deflating a big tree never blocks the loop that serves the
            # runs. An entry that grows past the 32-bit fields its header
            # promised aborts the stream mid-way (the headers are already on
            # the wire) — the log says why, instead of an opaque truncation.
            try:
                yield from stream_zip(entries)
            except ArchiveError as e:
                log.warning("archive %s aborted mid-stream: %s", name, e)
                raise

        return StreamingResponse(
            gen(),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{safe}"'},
        )

    # ---- staged (resumable) uploads: a phone on a bad link finishes -------- #

    @app.post(f"/api/{API_VERSION}/sessions/{{sid}}/staged", status_code=201)
    async def staged_create(sid: str, name: str = Query(...)):
        """Open a staged upload. The bytes may arrive over several requests —
        or none of them, in which case the TTL sweep is the only thing that
        removes it (a dropped socket is never an abort)."""
        _session_files(sid)
        staged = app_state.delivery.staged(sid)
        try:
            upload_id, offset = await asyncio.to_thread(staged.create, name)
        except StagedError as e:
            # a name the store will refuse to land (a NUL, an all-dot name):
            # the refusal is a bad request, and it happens before a part or
            # a meta exists to be swept
            raise HTTPException(status_code=400, detail=str(e))
        return {"upload_id": upload_id, "offset": offset}

    @app.get(f"/api/{API_VERSION}/sessions/{{sid}}/staged")
    async def staged_list(sid: str):
        """What is staged right now — the resume list after a reload or a
        restart, so a half-finished upload is finished, not repeated."""
        _session_files(sid)
        staged = app_state.delivery.staged(sid)
        return {"staged": await asyncio.to_thread(staged.list)}

    @app.put(f"/api/{API_VERSION}/sessions/{{sid}}/staged/{{upload_id}}/chunk")
    async def staged_chunk(sid: str, upload_id: str, request: Request, offset: int = Query(..., ge=0)):
        """Append the body AT ``offset``.

        A 409 carries the server's size: the client resumes from there. It is
        never a re-upload, and it is never a silent overwrite of bytes that
        already landed.
        """
        _session_files(sid)
        staged = app_state.delivery.staged(sid)
        try:
            size = await staged.append_stream(upload_id, offset, request.stream())
        except OffsetMismatch as e:
            return JSONResponse(status_code=409, content={"offset": e.size})
        except FileNotFoundError:
            # the part went away between the id resolving and the offset
            # check (a concurrent complete or cancel): the same 404 the
            # resolved path gives, not a 500 from the catch-all
            raise HTTPException(status_code=404, detail="no such staged upload")
        except ClientDisconnect:
            # what landed is fsynced and stays staged: the client resumes
            # from the server's size (its own offset bookkeeping or the
            # staged list) — the designed path, not a fault. The log is the
            # part of this answer that always lands; the 499 with its resume
            # handle is best-effort, the same shape as the one-shot upload's
            try:
                size = await asyncio.to_thread(staged.size, upload_id)
            except (StagedError, OSError):
                size = 0
            log.info("chunk for %s aborted by client disconnect at offset %d", upload_id, size)
            return JSONResponse(
                status_code=499,
                content={
                    "detail": "client closed the request",
                    "upload_id": upload_id,
                    "offset": size,
                    "resume": f"PUT /api/v1/sessions/{sid}/staged/{upload_id}/chunk?offset={size}",
                },
            )
        except StagedError:
            raise HTTPException(status_code=404, detail="no such staged upload")
        return {"offset": size}

    @app.delete(f"/api/{API_VERSION}/sessions/{{sid}}/staged/{{upload_id}}")
    async def staged_cancel(sid: str, upload_id: str):
        """Cancel — explicit, the only way a staged upload dies before its TTL."""
        _session_files(sid)
        staged = app_state.delivery.staged(sid)
        try:
            await asyncio.to_thread(staged.cancel, upload_id)
        except StagedError:
            raise HTTPException(status_code=404, detail="no such staged upload")
        # the human just moved a number the meter shows
        app_state.lc.usage.invalidate()
        return {"cancelled": upload_id}

    @app.post(f"/api/{API_VERSION}/sessions/{{sid}}/staged/{{upload_id}}/complete")
    async def staged_complete(sid: str, upload_id: str):
        """Finish: hash what actually landed (over the whole file, never per
        chunk — see :mod:`app.delivery.upload`), then deliver it like any
        upload.

        The whole verify → deliver → drop runs under the part's append lock
        (the shared :meth:`app.delivery.service.Delivery.complete_staged`):
        a chunk that landed between the hash and the move would break the
        store's content address, and the UI's one-ack-before-complete order
        is a property of a well-behaved client, not a guarantee.
        """
        _row, sfiles = _session_files(sid)
        staged = app_state.delivery.staged(sid)
        try:
            f = await app_state.delivery.complete_staged(staged, upload_id, sfiles)
        except StagedError:
            raise HTTPException(status_code=404, detail="no such staged upload")
        except DeliveryError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except FileNotFoundError:
            # the part vanished under a concurrent cancel or TTL sweep
            raise HTTPException(status_code=404, detail="no such staged upload")
        app_state.lc.events.add(sid, "file_uploaded", f"{f.name} {f.size}B {f.mime}")
        app_state.lc.usage.invalidate()
        return JSONResponse(
            status_code=201,
            content={"name": f.name, "sha256": f.sha256, "size": f.size, "mime": f.mime},
        )

    @app.get(f"/api/{API_VERSION}/sessions/{{sid}}/files/{{path:path}}")
    async def download(sid: str, path: str):
        row, sfiles = _session_files(sid)
        try:
            if path.startswith("outbox/"):
                rel = path[len("outbox/") :]
                p = sfiles.outbox_path(rel)
                filename = p.name
                try:
                    st = os.lstat(p)
                except FileNotFoundError:
                    # the gate passed and the artifact is gone (the agent
                    # owns the outbox): the gate's 404 shape, never a 500
                    raise FileNotDelivered(path) from None
                # the listing's per-version cache (same key): a file the human
                # just saw listed is not sniffed a second time
                mime = await asyncio.to_thread(
                    mime_for, (sid, rel, st.st_size, st.st_mtime), str(p), st.st_size
                )
            elif path.startswith("uploads/"):
                name = path[len("uploads/") :]
                p = sfiles.upload_path(name)
                filename = p.name
                # sniffed once, at upload time, and recorded in the manifest;
                # the inbox file is immutable to the session, so that is its MIME
                mime = sfiles.manifest_mime(name)
                if not mime:
                    mime = await asyncio.to_thread(sniff_mime, str(p))
            else:
                raise FileNotDelivered(path)
        except FileNotDelivered:
            raise HTTPException(status_code=404, detail="no such file")
        return FileResponse(str(p), media_type=mime, filename=filename)

    @app.post(f"/api/{API_VERSION}/sessions/{{sid}}/files/keep")
    async def keep_file(sid: str, body: KeepBody):
        """Keep a file beyond the session (the store-hop rationale is in
        :meth:`app.delivery.service.Delivery.keep`). ``body.path`` is addressed
        exactly like a download — a keep can only ever name a file the human
        can already see and download — and the sha rides the file's own
        inode, looked up, never taken from the body."""
        _row, sfiles = _session_files(sid)
        try:
            if body.path.startswith("outbox/"):
                rel = body.path[len("outbox/") :]
                src = sfiles.outbox_path(rel)
                # the sha is the keep's address in the store; an upload's is
                # already recorded, an artifact's may already be hashed in the
                # background — otherwise the keep pass computes it once
                sha = sfiles.sha_for_outbox(rel)
                keep_name = body.as_name or rel
            elif body.path.startswith("uploads/"):
                name = body.path[len("uploads/") :]
                src = sfiles.upload_path(name)
                sha = sfiles.manifest_sha(name)
                # the path the file LANDED under (a raw-dot ask resolves to
                # its safe form): keeping keeps the folder it arrived in
                keep_name = (
                    body.as_name or src.relative_to(sfiles.inbox.resolve()).as_posix()
                )
            else:
                raise FileNotDelivered(body.path)
            row = await asyncio.to_thread(
                app_state.delivery.keep, sfiles, str(src), keep_name, sha
            )
        except (FileNotDelivered, DeliveryError) as e:
            raise HTTPException(status_code=404, detail=f"no such file: {e}")
        app_state.lc.events.add(sid, "file_kept", row["path"])
        app_state.lc.usage.invalidate()
        return JSONResponse(status_code=201, content=row)

    @app.post(f"/api/{API_VERSION}/sessions/{{sid}}/files/attach")
    async def attach_file(sid: str, body: AttachBody):
        """Give this session a file another session already has, or one the
        human kept (``from: "keep"``).

        The target's inbox gets a link to the stored bytes and a manifest
        entry, so the agent finds it in ``./inbox`` exactly like an upload
        (see :meth:`app.delivery.service.Delivery.attach` for the store-hop
        accounting). The source is resolved through ITS OWN download gates.
        """
        _row, target = _session_files(sid)
        att_mime: str | None = None
        att_key: tuple | None = None
        att_name: str | None = None
        try:
            if body.source == "keep":
                src = app_state.delivery.keep_path(body.path)
                # the shelf's listing hashed this row in the background: with
                # its sha the attach is a free link (the keep IS the store's
                # inode), without, a full re-hash of the same bytes. The
                # MIME falls to the shelf's own key (its absolute path — the
                # key list_keep warms).
                sha = await asyncio.to_thread(
                    app_state.delivery.keep_sha, body.path
                )
                # the shelf's path is <source session>/<path>: the bucket is
                # the source session's prefix, not the file's shape — land
                # it under the path it was kept under
                att_name = (
                    body.as_name
                    or (body.path.split("/", 1)[1] if "/" in body.path else body.path)
                )
            else:
                src_row, src_files = _session_files(body.source)
                if body.path.startswith("outbox/"):
                    rel = body.path[len("outbox/") :]
                    src = src_files.outbox_path(rel)
                    sha = src_files.sha_for_outbox(rel)
                    # the source's OWN listing key: the row the human saw
                    # listed is not sniffed a second time
                    att_key = (body.source, rel)
                    # an attach keeps the folder its source lived in
                    att_name = body.as_name or rel
                elif body.path.startswith("uploads/"):
                    name = body.path[len("uploads/") :]
                    src = src_files.upload_path(name)
                    sha = src_files.manifest_sha(name)
                    # sniffed once, at upload time, and recorded in the
                    # source's manifest: the inbox file is immutable, so
                    # that record is the MIME — no sniff of any kind
                    att_mime = src_files.manifest_mime(name) or None
                    # the path the file LANDED under (a raw-dot ask resolves
                    # to its safe form): an attach keeps its folder
                    att_name = body.as_name or src.relative_to(
                        src_files.inbox.resolve()
                    ).as_posix()
                else:
                    raise FileNotDelivered(body.path)
            f = await asyncio.to_thread(
                app_state.delivery.attach,
                target,
                str(src),
                sha256=sha,
                name=att_name,
                mime=att_mime,
                mime_key=att_key,
            )
        except (FileNotDelivered, DeliveryError) as e:
            raise HTTPException(status_code=404, detail=f"cannot attach: {e}")
        app_state.lc.events.add(sid, "file_uploaded", f"{f.name} {f.size}B (attached)")
        app_state.lc.usage.invalidate()
        return JSONResponse(
            status_code=201,
            content={"name": f.name, "sha256": f.sha256, "size": f.size, "mime": f.mime},
        )

    @app.get(f"/api/{API_VERSION}/files")
    async def list_kept(request: Request):
        """Everything the human has kept, newest first — the durable shelf,
        across sessions (this is what ``make pull`` mirrors host-side).

        ETag/304 like the session's file listing: a revalidation that comes
        back unchanged costs ~200 bytes, not the whole shelf."""
        rows = await asyncio.to_thread(app_state.delivery.list_keep)
        body = (
            json.dumps({"keep": rows}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        etag = '"' + hashlib.blake2b(body, digest_size=16).hexdigest() + '"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        return Response(
            content=body,
            media_type="application/json",
            headers={"ETag": etag, "Cache-Control": "no-cache"},
        )

    @app.get(f"/api/{API_VERSION}/files/keep/{{path:path}}")
    async def download_kept(path: str):
        try:
            p = app_state.delivery.keep_path(path)
            st = os.lstat(p)
        except (FileNotFoundError, FileNotDelivered, DeliveryError):
            # the lstat runs after the gate: an un-keep landing in the
            # window is the gate's own 404 shape, never a 500 off the
            # vanished path
            raise HTTPException(status_code=404, detail="no such kept file")
        # the shelf listing's per-version cache (same key): a file the human
        # just saw listed is not sniffed a second time
        mime = await asyncio.to_thread(
            mime_for, ("keep", p, st.st_size, st.st_mtime), p, st.st_size
        )
        return FileResponse(p, media_type=mime, filename=os.path.basename(p))

    @app.delete(f"/api/{API_VERSION}/files/keep/{{path:path}}")
    async def delete_kept(path: str):
        """Un-keep. The only way kept bytes are ever removed (nothing here
        expires on its own), so it is an explicit call with an explicit
        answer: how many bytes the human just gave back."""
        try:
            size = await asyncio.to_thread(app_state.delivery.delete_keep, path)
        except (FileNotDelivered, DeliveryError):
            raise HTTPException(status_code=404, detail="no such kept file")
        app_state.lc.events.add(None, "keep_deleted", path)
        app_state.lc.usage.invalidate()
        return {"deleted": f"keep/{path}", "size": size}

    # ------------------------------------------------------------------ #
    # extension dialogs (the run waits on the human; the dialog dies with
    # the process)
    # ------------------------------------------------------------------ #

    @app.get(f"/api/{API_VERSION}/sessions/{{sid}}/dialogs")
    async def list_dialogs(sid: str):
        """Pending extension dialogs (select/confirm/input/editor). The full
        record (title/options/prefill) is here; the SSE event is only the
        notification. 409 when the process is dead — a dialog cannot
        outlive its child, so an empty list is the only answer."""
        _get_row(app_state, sid)
        driver = _live_driver(app_state, sid)
        return {"dialogs": list(driver.pending_dialogs.values())}

    @app.post(f"/api/{API_VERSION}/sessions/{{sid}}/dialogs/{{dialog_id}}/answer")
    async def answer_dialog(sid: str, dialog_id: str, body: DialogAnswerBody):
        """Answer a parked dialog: value (select/input/editor) or confirmed
        (confirm) or cancelled — the child unblocks and the run resumes.
        Unknown id ⇒ 404 (already answered, or resolved by the pi-side
        timeout/abort and swept at the run's settle)."""
        _get_row(app_state, sid)
        driver = _live_driver(app_state, sid)
        try:
            await driver.answer_dialog(
                dialog_id, value=body.value, confirmed=body.confirmed,
                cancelled=body.cancelled,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="no pending dialog with that id")
        app_state.lc.events.add(sid, "extension_dialog_answered", dialog_id)
        return {"ok": True}

    # ------------------------------------------------------------------ #
    # panel (Portcullis) — the exact whitelist, nothing else
    # ------------------------------------------------------------------ #

    @app.post("/panel/sessions/{sid}/action")
    async def panel_action(sid: str, body: PanelActionBody):
        _get_row(app_state, sid)
        if body.action not in PANEL_ACTIONS:
            raise HTTPException(
                status_code=400,
                detail=f"unknown action (whitelist: {', '.join(PANEL_ACTIONS)})",
            )
        lc = app_state.lc
        if body.action == "start":
            return await lc.ensure_alive(sid)
        if body.action == "steer":
            if not body.text:
                raise HTTPException(status_code=400, detail="steer needs text")
            driver = _live_driver(app_state, sid)
            try:
                disposition = await driver.steer(body.text)
            except PromptRejected as e:
                raise HTTPException(status_code=502, detail=e.reason)
            if disposition != "handled":
                await lc.retire_after_write(sid, driver)
            return {"disposition": disposition}
        if body.action == "prompt":
            if not body.text:
                raise HTTPException(status_code=400, detail="prompt needs text")
            try:
                return {"disposition": await lc.prompt(sid, body.text)}
            except PromptRejected as e:
                raise HTTPException(status_code=502, detail=e.reason)
        if body.action == "abort":
            driver = _live_driver(app_state, sid)
            try:
                await driver.abort()
            except PromptRejected as e:
                raise HTTPException(status_code=502, detail=e.reason)
            return {"ok": True}
        if body.action == "release":
            out = await lc.release(sid, reason="panel release")
            return {
                "released": out.released,
                "quarantined": out.quarantined,
                "errors": out.errors,
            }
        raise HTTPException(status_code=400, detail="unreachable")

    # ------------------------------------------------------------------ #
    # static UI (served by the app, same port). Built to ui/dist; absent
    # in dev checkouts — the API alone keeps working.
    # ------------------------------------------------------------------ #
    ui_dist = _ui_dist()
    if ui_dist.is_dir() and (ui_dist / "index.html").is_file():
        ui_root = ui_dist.resolve()
        assets = ui_dist / "assets"
        if assets.is_dir():
            from fastapi.staticfiles import StaticFiles

            app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str):
            # API/panel/healthz routes are registered above and win; this is
            # the SPA fallback (client-side routing: /s/<id> is a UI route,
            # not a server one). Unknown API-ish prefixes 404.
            if path.startswith(("api/", "panel/", "healthz", "openapi", "docs")):
                raise HTTPException(status_code=404, detail="not found")
            candidate = (ui_dist / path) if path else ui_dist / "index.html"
            try:
                # the path arrives percent-decoded (%2f == /), so a name that
                # is lexically inside ui/dist can still walk out via ..; the
                # app runs as root, so only serve files that RESOLVE inside
                # ui/dist (an escaping symlink fails the check too). Anything
                # else falls back to the SPA index.
                if (
                    path
                    and candidate.is_file()
                    and candidate.resolve().is_relative_to(ui_root)
                ):
                    return FileResponse(str(candidate))
            except OSError:
                pass
            return FileResponse(str(ui_dist / "index.html"))

    return app
