"""Mock LLM router — OpenAI-compatible, request-recording, scripted.

A small HTTP server that records every request (headers, body) and
returns scripted OpenAI-compatible completions — including thinking
blocks (on every run), parallel tool_calls, error responses, and long
gaps. The entire driver is testable without inference; gates assert
router-side (a request arrived, with the right Bearer/model/content) —
a gate that only checks `disposition: started` passes while inference
is dead.

Stdlib only. Control endpoints (same port, never on the LLM surface):
  POST /mock/script   — set the scripted scenario
  GET  /mock/requests — recorded requests (json list)
  POST /mock/reset    — clear the request log
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class MockRouter:
    """Threaded mock router. Bind to 127.0.0.1 on an ephemeral port in tests."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: list[dict[str, Any]] = []
        self._script: dict[str, Any] = {}  # empty = default scenario

    # --- control API -------------------------------------------------------

    @property
    def base_url(self) -> str:
        return f"http://{self._host}:{self._port}/v1"

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    def set_script(self, script: dict[str, Any] | None) -> None:
        with self._lock:
            self._script = dict(script or {})

    def requests(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._requests]

    def reset(self) -> None:
        with self._lock:
            self._requests.clear()
            self._script = {}

    def start(self, host: str = "127.0.0.1") -> None:
        router = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # silence
                pass

            def _send_json(self, code: int, obj: Any) -> None:
                body = json.dumps(obj).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_sse(self, chunks: list[dict[str, Any]], done: bool = True) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                for c in chunks:
                    self.wfile.write(f"data: {json.dumps(c)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                if done:
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()

            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/mock/requests":
                    self._send_json(200, router.requests())
                elif self.path == "/v1/models":
                    self._record("GET", self.path, None)
                    self._send_json(
                        200,
                        {
                            "object": "list",
                            "data": [
                                {
                                    "id": router._script.get("model_id", "default"),
                                    "object": "model",
                                    "capabilities": {
                                        "chat": True,
                                        "tools": True,
                                        "reasoning": True,
                                        "image": True,
                                    },
                                }
                            ],
                        },
                    )
                else:
                    self._send_json(404, {"error": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    body = {"__unparseable__": raw.decode("utf-8", "replace")}

                if self.path == "/mock/script":
                    router.set_script(body)
                    self._send_json(200, {"ok": True})
                    return
                if self.path == "/mock/reset":
                    router.reset()
                    self._send_json(200, {"ok": True})
                    return
                if self.path != "/v1/chat/completions":
                    self._send_json(404, {"error": "not found"})
                    return

                self._record("POST", self.path, body, headers=dict(self.headers))
                self._handle_chat(body)

            def _record(self, method: str, path: str, body: Any, headers: dict | None = None) -> None:
                with router._lock:
                    router._requests.append(
                        {
                            "ts": time.time(),
                            "method": method,
                            "path": path,
                            "headers": headers,
                            "body": body,
                        }
                    )

            def _handle_chat(self, body: dict[str, Any]) -> None:
                s = dict(router._script)
                script_model = s.get("model_id", "default")
                # an error scenario responds before streaming anything
                status = s.get("status", 200)
                if status >= 400:
                    self._send_json(status, {"error": s.get("error", {"message": "mock error"})})
                    return
                gap = s.get("gap_seconds", 0)
                if gap:
                    time.sleep(gap)  # long silent gap: no bytes at all

                messages = body.get("messages", [])
                tool_result_count = sum(
                    1 for m in messages if isinstance(m, dict) and m.get("role") == "tool"
                )
                has_tool_result = tool_result_count > 0
                # loop_count: keep emitting tool calls until N tool results
                # have accumulated (no-cap tests: a long multi-round run)
                loop_count = int(s.get("loop_count", 0) or 0)
                keep_looping = loop_count > 0 and tool_result_count < loop_count
                if has_tool_result and not s.get("loop_forever", False) and not keep_looping:
                    # final answer after the tool ran
                    if s.get("echo_final"):
                        # echo the last user message (per-session markers
                        # without shared script state — concurrency tests)
                        text = ""
                        for m in reversed(messages):
                            if isinstance(m, dict) and m.get("role") == "user":
                                c = m.get("content")
                                if isinstance(c, str):
                                    text = c
                                elif isinstance(c, list):
                                    text = "".join(
                                        p.get("text", "")
                                        for p in c
                                        if isinstance(p, dict) and p.get("type") == "text"
                                    )
                                break
                    else:
                        text = s.get("final_text", "DONE: tool output processed.")
                    chunks = _text_chunks(text, s, model=script_model)
                else:
                    chunks = _run_chunks(s, model=script_model)
                self._send_sse(chunks)

        # find a free port on the requested host (loopback by default; the
        # guest drills bind the host's bridge address so the guest can
        # reach the mock). The host firewall default-drops inbound from
        # the servitor edge subnet EXCEPT the test-mock range 48700:48799
        # (one-time host ufw setup) — prefer that range so a guest-bound
        # mock is actually reachable from the guest.
        import socket

        self._port = 0
        for candidate in range(48700, 48800):
            probe = socket.socket()
            try:
                probe.bind((host, candidate))
                self._port = candidate
                probe.close()
                break
            except OSError:
                probe.close()
        if not self._port:
            probe = socket.socket()
            probe.bind((host, 0))
            self._port = probe.getsockname()[1]
            probe.close()
        self._host = host
        self._server = ThreadingHTTPServer((host, self._port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _chunk(delta: dict[str, Any], model: str, finish: str | None = None) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def _usage_chunk(model: str, include_usage: bool) -> list[dict[str, Any]]:
    if not include_usage:
        return []
    return [
        {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [],
            "usage": {
                "prompt_tokens": 57,
                "completion_tokens": 27,
                "total_tokens": 84,
                "completion_tokens_details": {"reasoning_tokens": 20},
            },
        }
    ]


def _text_chunks(text: str, s: dict[str, Any], model: str) -> list[dict[str, Any]]:
    """Thinking block (every run, per verified behavior) + text + usage.

    `chunk_count` (default 7): the text is split into this many PIECES
    (not piece size): 32 KB with chunk_count 2048 streams 2048 small
    deltas → pi emits one small message_update per delta.
    """
    include_usage = bool(s.get("include_usage", True))
    size = int(s.get("chunk_count", 7))
    chunks: list[dict[str, Any]] = [_chunk({"role": "assistant", "content": ""}, model)]
    thinking = s.get("thinking", "Mock reasoning: inspect, then answer.")
    for piece in _split(thinking, 7):
        chunks.append(_chunk({"reasoning_content": piece}, model))
    for piece in _split(text, size):
        chunks.append(_chunk({"content": piece}, model))
    chunks.append(_chunk({}, model, finish="stop"))
    chunks.extend(_usage_chunk(model, include_usage))
    return chunks


def _run_chunks(s: dict[str, Any], model: str) -> list[dict[str, Any]]:
    """A run that ends in tool_calls (the model wants to do something).

    `tools` in the script: a list of {name, arguments(dict)}; by default a
    single bash call. The NEXT request (after the tool result is appended)
    produces the final text — see _handle_chat.
    """
    include_usage = bool(s.get("include_usage", True))
    size = int(s.get("chunk_count", 7))
    thinking = s.get("thinking", "Mock reasoning: I should run a command first.")
    tools = s.get("tools") or [
        {"name": "bash", "arguments": {"command": "echo servitor-mock-tool-ran"}}
    ]
    chunks: list[dict[str, Any]] = [_chunk({"role": "assistant", "content": ""}, model)]
    for piece in _split(thinking, 7):
        chunks.append(_chunk({"reasoning_content": piece}, model))
    if s.get("prefix_text"):
        for piece in _split(str(s["prefix_text"]), size):
            chunks.append(_chunk({"content": piece}, model))
    for i, t in enumerate(tools):
        name = t.get("name", "bash")
        args = json.dumps(t.get("arguments", {}))
        call_id = f"call_{uuid.uuid4().hex[:12]}"
        # announce the call with name+id, then stream the arguments
        chunks.append(
            _chunk(
                {
                    "tool_calls": [
                        {
                            "index": i,
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": ""},
                        }
                    ]
                },
                model,
            )
        )
        for piece in _split(args, 11):
            chunks.append(
                _chunk(
                    {"tool_calls": [{"index": i, "function": {"arguments": piece}}]},
                    model,
                )
            )
    chunks.append(_chunk({}, model, finish="tool_calls"))
    chunks.extend(_usage_chunk(model, include_usage))
    return chunks


def _split(text: str, n: int) -> list[str]:
    if not text:
        return []
    step = max(1, len(text) // n)
    return [text[i : i + step] for i in range(0, len(text), step)] or [text]
