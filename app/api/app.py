"""The servitor HTTP/SSE API.

One published port (the container's loopback ingress, edge-only; the
Portcullis panel is the only public front). The front door over the same
Lifecycle the stdio protocol used — lifecycle semantics unchanged.

Contract surface (versioned in test/.scratch/api-contract.json):

    GET    /healthz
    GET    /api/v1/sessions
    POST   /api/v1/sessions
    GET    /api/v1/sessions/{id}
    POST   /api/v1/sessions/{id}/prompt
    POST   /api/v1/sessions/{id}/steer
    POST   /api/v1/sessions/{id}/follow-up
    POST   /api/v1/sessions/{id}/abort
    GET    /api/v1/sessions/{id}/probe
    GET    /api/v1/sessions/{id}/messages        (fetch, never replay)
    POST   /api/v1/sessions/{id}/fork
    POST   /api/v1/sessions/{id}/stable
    POST   /api/v1/sessions/{id}/release
    GET    /api/v1/sessions/{id}/events            (SSE; Last-Event-ID resume)
    POST   /api/v1/sessions/{id}/events/ack
    GET    /api/v1/sessions/{id}/files
    POST   /api/v1/sessions/{id}/files/upload      (raw body; no size cap)
    GET    /api/v1/sessions/{id}/files/{path}      (outbox/... | uploads/...)
    POST   /panel/sessions/{id}/action             (whitelist)

Rules: authz is per session (released/unknown ⇒ 404, other sessions
never leaked; file paths cannot escape the session dirs). The API key is
never in any response (browser-held, env-only). SSE: monotonic integer
ids per session; replay from the bounded ring via Last-Event-ID/?after=;
a position past the ring edge is a ``gap`` event (client re-syncs via
the API — never partial data); a slow consumer that lets the ring pass
the cap pauses the RUN. No caps: uploads/downloads stream to/from disk.
"""
from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from app.api.sse import SseHub
from app.delivery.service import Delivery
from app.delivery.store import DeliveryError, FileNotDelivered, sniff_mime
from app.iso.spawn import slot_identity
from app.lifecycle import PROBES, Lifecycle
from app.registry import Registry, SessionRow
from app.rpc.session import PromptRejected, SpawnFailure

log = logging.getLogger("servitor.api")

API_VERSION = "v1"
PANEL_ACTIONS = ("start", "steer", "prompt", "abort", "release")


class PromptBody(BaseModel):
    text: str


class SteerBody(BaseModel):
    text: str


class StableBody(BaseModel):
    stable: bool = True


class StartBody(BaseModel):
    display_name: str | None = None
    api_key: str | None = None  # browser-held; env-only, never stored/returned


class ReleaseBody(BaseModel):
    reason: str = "user close"


class ForkBody(BaseModel):
    entry_id: str


class AckBody(BaseModel):
    sub_id: int
    last_id: int


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
        "junk_bytes": r.junk_bytes,
        "waiting": r.waiting,
        "update_waiting": r.update_waiting,
        "new_extensions": list(r.new_extensions),
        "last_activity": r.last_activity,
        "live_process": live is not None and live.driver is not None and not live.driver.exited,
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

    Called from the ASGI lifespan startup — by then uvicorn's own
    handler (installed via ``signal.signal`` when ``serve()`` began)
    is in place, and ``signal.signal`` overrides it for the server's
    life (installing before ``serve()`` would not work: uvicorn saves
    the pre-existing handler as the "original" and masks it).

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


def build_app(app_state, cfg) -> FastAPI:
    """Build the FastAPI app over the App core (registry + lifecycle +
    delivery). ``cfg`` is the validated env Config."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        app_state.lc.start_gc()
        _install_http_stop_handler(app_state)
        yield
        await app_state.lc.shutdown(cause="app stop")

    app = FastAPI(title="servitor", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception):
        # log + surface the reason (a bare 500 body is not debuggable
        # over the one published port)
        log.exception("unhandled error in %s", _request.url.path)
        return JSONResponse(
            status_code=500,
            content={"error": f"{type(exc).__name__}: {exc}"},
        )

    if cfg.browser_origin:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[cfg.browser_origin],  # exact match; never *
            allow_methods=["GET", "POST", "OPTIONS"],
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

    @app.get(f"/api/{API_VERSION}/sessions")
    async def list_sessions():
        # released sessions are GONE (404 everywhere); the list shows
        # live sessions only
        return {
            "sessions": [_row_dict(r, app_state.lc) for r in
                         app_state.registry.list_sessions(include_released=False)]
        }

    @app.post(f"/api/{API_VERSION}/sessions")
    async def create_session(body: StartBody):
        try:
            res = await app_state.lc.start_session(body.display_name, api_key=body.api_key)
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
            # fresh session, no transcript yet: the probe's short-lived
            # spawn is refused by the first-touch rule; the busy state is
            # everything a waiter polling this endpoint needs
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

    @app.get(f"/api/{API_VERSION}/sessions/{{sid}}/events")
    async def events(
        sid: str,
        request: Request,
        after: int | None = Query(default=None, ge=0),
    ):
        row = _get_row(app_state, sid)
        if app_state.lc.shutting_down:
            # the stop path is draining: gate BEFORE hub() — hub() is
            # lazy, so a session with no hub yet would get a fresh OPEN
            # hub that the stop path would never close (close_streams
            # already ran at SIGTERM). The client's EventSource backs off
            # and retries against the new instance once the restart lands.
            raise HTTPException(status_code=503, detail="shutting down")
        hub: SseHub = app_state.lc.hub(sid)
        if hub.closed:
            # per-hub terminal (defense-in-depth: shutting_down always
            # precedes close_streams, so this only fires if a hub is
            # closed while the global flag is somehow unset)
            raise HTTPException(status_code=503, detail="shutting down")
        last_event_id = request.headers.get("last-event-id")
        if after is not None:
            start = after
        elif last_event_id is not None:
            try:
                start = int(last_event_id)
            except ValueError:
                raise HTTPException(status_code=400, detail="bad Last-Event-ID")
        else:
            start = 0
        sub, replay, gap = hub.subscribe(start)

        async def gen():
            try:
                yield ":servitor\n\n"
                if gap:
                    # the requested position was evicted from the ring:
                    # the client must re-sync from the API, not guess
                    yield _sse_frame("gap", json.dumps({"after": start}), None)
                    cursor = hub.latest_id
                    hub.consume(sub, cursor)
                else:
                    cursor = start
                for e in replay:
                    if await request.is_disconnected():
                        return
                    yield _sse_frame(e.name, e.data, e.id)
                    cursor = e.id
                hub.consume(sub, cursor)
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        await asyncio.wait_for(
                            hub.wait_new(cursor), timeout=cfg.heartbeat_secs
                        )
                    except asyncio.TimeoutError:
                        yield ":hb\n\n"
                        continue
                    if hub.closed:
                        # the stop path closed the hub (SIGTERM drain):
                        # end the stream — the client's EventSource
                        # auto-reconnects (?after=<cursor> resumes)
                        return
                    batch = hub.events_after(cursor)
                    for e in batch:
                        if await request.is_disconnected():
                            return
                        yield _sse_frame(e.name, e.data, e.id)
                    if batch:
                        hub.consume(sub, batch[-1].id)
                        cursor = batch[-1].id
            finally:
                hub.unsubscribe(sub)

        headers = {
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
            "X-Sub-Id": str(sub),
        }
        return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)

    @app.post(f"/api/{API_VERSION}/sessions/{{sid}}/events/ack")
    async def events_ack(sid: str, body: AckBody):
        _get_row(app_state, sid)
        hub: SseHub = app_state.lc.hub(sid)
        hub.ack(body.sub_id, body.last_id)
        return {
            "ok": True,
            "latest_id": hub.latest_id,
            "paused": hub.paused,
            "pause_count": hub.pause_count,
            "buffered_bytes": hub.buffered_bytes,
            "unconsumed_bytes": hub._unconsumed_bytes(),
        }

    # ------------------------------------------------------------------ #
    # files (delivery)
    # ------------------------------------------------------------------ #

    def _session_files(sid: str):
        row = _get_row(app_state, sid)
        slot = None
        if row.slot != "waiting":
            try:
                slot = slot_identity(row.slot)
            except Exception:
                slot = None
        return row, app_state.delivery.files(app_state.state, sid, slot)

    @app.get(f"/api/{API_VERSION}/sessions/{{sid}}/files")
    async def list_files(sid: str):
        row, sfiles = _session_files(sid)
        uploads = sfiles.read_manifest().get("uploads", [])
        present = set()
        try:
            import os

            present = set(os.listdir(sfiles.inbox))
        except OSError:
            pass
        uploads = [u for u in uploads if u["name"] in present]
        return {
            "outbox": await asyncio.to_thread(sfiles.list_outbox),
            "uploads": uploads,
        }

    @app.post(f"/api/{API_VERSION}/sessions/{{sid}}/files/upload")
    async def upload(sid: str, request: Request, name: str = Query(...)):
        row, sfiles = _session_files(sid)
        try:
            f = await app_state.delivery.upload(request.stream(), name, sfiles)
        except DeliveryError as e:
            raise HTTPException(status_code=400, detail=str(e))
        app_state.lc.events.add(sid, "file_uploaded", f"{f.name} {f.size}B {f.mime}")
        return JSONResponse(
            status_code=201,
            content={"name": f.name, "sha256": f.sha256, "size": f.size, "mime": f.mime},
        )

    @app.get(f"/api/{API_VERSION}/sessions/{{sid}}/files/{{path:path}}")
    async def download(sid: str, path: str):
        row, sfiles = _session_files(sid)
        try:
            if path.startswith("outbox/"):
                p = sfiles.outbox_path(path[len("outbox/") :])
                filename = p.name
            elif path.startswith("uploads/"):
                p = sfiles.upload_path(path[len("uploads/") :])
                filename = p.name
            else:
                raise FileNotDelivered(path)
        except FileNotDelivered:
            raise HTTPException(status_code=404, detail="no such file")
        mime = await asyncio.to_thread(sniff_mime, str(p))
        return FileResponse(str(p), media_type=mime, filename=filename)

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
    ui_dist = Path(__file__).resolve().parents[2] / "ui" / "dist"
    if ui_dist.is_dir() and (ui_dist / "index.html").is_file():
        assets = ui_dist / "assets"
        if assets.is_dir():
            from fastapi.staticfiles import StaticFiles

            app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str):
            # API/panel/healthz routes are registered above and win; this
            # is the SPA fallback (client-side routing: /s/<id> is a UI
            # route, not a server one). Unknown API-ish prefixes 404.
            if path.startswith(("api/", "panel/", "healthz", "openapi", "docs")):
                raise HTTPException(status_code=404, detail="not found")
            candidate = (ui_dist / path) if path else ui_dist / "index.html"
            try:
                if path and candidate.is_file():
                    return FileResponse(str(candidate))
            except OSError:
                pass
            return FileResponse(str(ui_dist / "index.html"))

    return app
