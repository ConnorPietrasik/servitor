"""The HTTP stop path (root-free, loopback-only).

uvicorn's graceful shutdown 'waiting for connections' phase is held
open by any SSE stream (a browser holds it for the life of the tab,
and uvicorn does not cancel an in-flight streaming response — it only
disables keep-alive), and the abort-then-drain (lifespan shutdown)
only starts AFTER that phase. Without intervention, a deploy restart
with a connected browser rides the stop out to the unit's
TimeoutStopSec SIGKILL.

The fix under test: the lifespan installs a SIGTERM handler that sets
shutting_down, closes every SSE hub (the response generators end),
only then sets uvicorn's should_exit — so the connection wait ends at
once and the process exits cleanly in well under the stop budget.

A minimal uvicorn app is spawned as a subprocess (signals + the event
loop need their own process), wired with the REAL SseHub and the REAL
_install_http_stop_handler; a client holds an SSE stream open while
SIGTERM lands.
"""
from __future__ import annotations

import http.client
import os
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

APP = textwrap.dedent(
    """
    import asyncio
    import os

    import uvicorn
    from fastapi import FastAPI
    from fastapi.responses import StreamingResponse
    from contextlib import asynccontextmanager

    from app.api.sse import SseHub
    from app.api.app import _install_http_stop_handler

    hub = SseHub("s1")

    class _LC:
        shutting_down = False

        def close_streams(self):
            hub.close()

    class _State:
        http_server = None
        lc = _LC()

    @asynccontextmanager
    async def lifespan(_app):
        _install_http_stop_handler(_State)
        yield

    app = FastAPI(lifespan=lifespan)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/events")
    async def events():
        async def gen():
            yield "x: 1\\n\\n"
            while True:
                try:
                    await asyncio.wait_for(hub.wait_new(0), timeout=15)
                except asyncio.TimeoutError:
                    yield ":hb\\n\\n"
                    continue
                if hub.closed:
                    return
        return StreamingResponse(gen(), media_type="text/event-stream")

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=int(os.environ["PORT"]),
            log_level="warning",
            lifespan="on",
            timeout_keep_alive=75,
            # the production backstop (see run_http)
            timeout_graceful_shutdown=20.0,
        )
    )
    _State.http_server = server
    asyncio.run(server.serve())
    """
)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _wait_up(port: int, proc: subprocess.Popen, deadline_sec: float) -> None:
    deadline = time.time() + deadline_sec
    while time.time() < deadline:
        if proc.poll() is not None:
            err = proc.stderr.read().decode(errors="replace")[-1500:]
            proc.stderr.close()
            raise AssertionError(f"app died at startup:\n{err}")
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            c.request("GET", "/healthz")
            c.getresponse().read()
            c.close()
            return
        except OSError:
            time.sleep(0.2)
    raise AssertionError("app did not come up")


def test_sigterm_stops_cleanly_with_a_held_sse_stream(tmp_path):
    """A held-open SSE stream must not hold the stop open: SIGTERM ->
    clean exit in seconds (the unit's stop budget is 120 s)."""
    port = _free_port()
    app_file = tmp_path / "stop_app.py"
    app_file.write_text(APP)
    proc = subprocess.Popen(
        [sys.executable, str(app_file)],
        env={**os.environ, "PORT": str(port),
             "PYTHONPATH": str(REPO)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        cwd=str(REPO),
    )
    stream: http.client.HTTPConnection | None = None
    try:
        _wait_up(port, proc, 20)
        # the browser equivalent: hold the stream open
        stream = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        stream.request("GET", "/events")
        assert stream.getresponse().read(5) == b"x: 1\n", \
            "SSE stream not established"
        t0 = time.time()
        proc.terminate()
        try:
            rc = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            err = proc.stderr.read().decode(errors="replace")[-1500:]
            raise AssertionError(
                f"process still alive 10 s after SIGTERM with an SSE "
                f"stream held open (the 2026-11-19 failure mode)\n{err}"
            )
        dt = time.time() - t0
        assert rc in (0, -15, 143), f"unexpected exit {rc}"
        assert dt < 10, f"stop took {dt:.1f}s"
    finally:
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        proc.stderr.close()
