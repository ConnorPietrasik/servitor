"""API/delivery test fixtures.

The app runs as a real subprocess (``python -m app.main`` in HTTP mode —
uvicorn on an ephemeral loopback port), exactly as it will run under
systemd in the guest: root, real setuid spawns, real mounts, real pi.
The mock router plays the model (scripted completions, request log).
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conformance.harness import AGENT_CONFIG, require_root_or_skip  # noqa: E402
from conformance.install import resolve_pi  # noqa: E402
from mock_router.server import MockRouter  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
VENV_PY = REPO / ".venv" / "bin" / "python"


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _umount_all(run_tag: str) -> None:
    out = subprocess.run(["mount"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if run_tag in line:
            target = line.split()[2]
            for flag in ("-l", ""):
                subprocess.run(["umount", *(["-l"] if flag else [])] + [target],
                               capture_output=True)
            break


@pytest.fixture()
def sse_cap() -> int | None:
    """Per-test SSE ring cap in bytes (None = the production 50 MB
    default). The backpressure gate overrides this (in its own module)
    with a scaled-down cap: the pause/resume/bounded mechanism is
    cap-independent, and pi's cumulative message_update semantics bound
    a single context window's SSE volume well below 50 MB (the
    production default is pinned by a unit test)."""
    return None


@pytest.fixture()
def api_env(sse_cap, tmp_path_factory):
    """One full app (HTTP) + mock router + isolated state, torn down."""
    require_root_or_skip("API/delivery tests")
    tag = f"api-{uuid.uuid4().hex[:6]}"
    run_root = Path(f"/srv/servitor-test/run-{tag}")
    run_root.mkdir(parents=True, exist_ok=True)
    run_root.chmod(0o755)
    # the state root lives on the SHARED 0755 tree, NOT under a slot
    # home: every slot's pi process must traverse state — a 0700 slot
    # home makes the state invisible to every other slot (EACCES: pi
    # exits 1 at spawn). Mirrors production, where state is a dedicated
    # volume, never inside a slot home.
    state_root = run_root / "state"
    state_root.mkdir(parents=True, exist_ok=True)
    state_root.chmod(0o755)
    # per-test home dir state (the slot's home is shared; keep test dirs
    # namespaced so sweep boundaries stay clean)

    pi = resolve_pi(run_root / "pi-install", "1.5.2", "0.48.0", node_bin="/usr/bin/node")
    toolkit = run_root / "toolkit"
    toolkit.mkdir(exist_ok=True)
    (toolkit / "f.txt").write_text("toolkit\n")

    router = MockRouter()
    router.start()
    port = free_port()
    origin = f"http://127.0.0.1:{port + 1}"  # a different loopback port = a different origin
    env = {
        **os.environ,
        "OMNISSIAH_API_KEY": "test-super-secret-key",
        "SERVITOR_BIND": "127.0.0.1",
        "SERVITOR_PORT": str(port),
        "SERVITOR_BROWSER_ORIGIN": origin,
        "PYTHONPATH": str(REPO),
        "SERVITOR_SSE_RING_BYTES": str(sse_cap) if sse_cap else "",
    }
    proc = subprocess.Popen(
        [
            str(VENV_PY), "-m", "app.main",
            "--state", str(state_root),
            "--config-src", str(AGENT_CONFIG),
            "--router-url", router.base_url,
            "--pi-node", pi.node_bin,
            "--pi-cli", str(pi.cli_js),
            "--pi-ext", str(pi.ext_path) if pi.ext_path else "",
            "--toolkit", str(toolkit),
            "--slots", "slot1,slot2,slot3",
            "--idle-kill-sec", "3600",
            "--shutdown-grace-sec", "5",
            "--gc-interval-sec", "30",
        ],
        cwd=str(REPO), env=env,
        stdout=subprocess.DEVNULL,
        stderr=open(f"/tmp/api_app_{tag}.err","wb"),
    )
    base = f"http://127.0.0.1:{port}"
    try:
        # wait for the front door
        deadline = time.time() + 30
        up = False
        while time.time() < deadline:
            try:
                if httpx.get(base + "/healthz", timeout=2.0).status_code == 200:
                    up = True
                    break
            except httpx.HTTPError:
                pass
            if proc.poll() is not None:
                break
            time.sleep(0.2)
        err_text = ""
        try:
            err_text = Path(f"/tmp/api_app_{tag}.err").read_text(errors="replace")[-3000:]
        except OSError:
            pass
        assert up, f"app never came up; exit={proc.poll()}\n{err_text}"

        yield {
            "base": base,
            "proc": proc,
            "router": router,
            "run_root": run_root,
            "state_root": state_root,
            "tag": tag,
            "origin": origin,
            "other_origin": "http://evil.example",
            "pi": pi,
        }
    finally:
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        router.stop()
        _umount_all(tag)
        # state cleanup (home + shared root); mounts are gone by now
        for p in (Path(f"/home/slot1/servitor-test/run-{tag}"), run_root):
            subprocess.run(["rm", "-rf", str(p)], capture_output=True)


class Api:
    """A tiny blocking HTTP client around the running app."""

    def __init__(self, base: str):
        self.base = base

    def _req(self, method: str, path: str, *, json_body=None, content=None,
             headers=None, timeout=60.0) -> httpx.Response:
        return httpx.request(
            method, self.base + path,
            json=json_body, content=content, headers=headers or {},
            timeout=timeout,
        )

    def get(self, path, **kw):
        return self._req("GET", path, **kw)

    def post(self, path, **kw):
        return self._req("POST", path, **kw)

    # --- domain helpers ------------------------------------------------- #

    def create_session(self, display_name=None, api_key=None, slot_wait=True) -> dict:
        body = {}
        if display_name:
            body["display_name"] = display_name
        if api_key:
            body["api_key"] = api_key
        r = self.post("/api/v1/sessions", json_body=body)
        assert r.status_code == 200, r.text
        d = r.json()
        if slot_wait and d.get("waiting"):
            # poll until a slot frees (the app releases pressure)
            for _ in range(60):
                time.sleep(1)
                rr = self.get(f"/api/v1/sessions/{d['session_id']}")
                if rr.json().get("slot") not in (None, "waiting"):
                    return rr.json()
        return d

    def prompt(self, sid: str, text: str) -> dict:
        r = self.post(f"/api/v1/sessions/{sid}/prompt", json_body={"text": text})
        assert r.status_code == 200, r.text
        return r.json()

    def release(self, sid: str, reason="test end") -> dict:
        r = self.post(f"/api/v1/sessions/{sid}/release", json_body={"reason": reason},
                      timeout=120)
        assert r.status_code == 200, r.text
        return r.json()

    def settle(self, sid: str, timeout=120.0) -> dict:
        """Wait for the run to settle via the SSE stream (agent_settled
        event) — the HTTP-mode analogue of the stdio settle command.
        Returns {"aborted": bool, "seen": [event names]}."""
        frames, _ = self.sse_read(sid, after=0, until_settled=True, timeout=timeout)
        names = [n for (_i, n, _d) in frames if _i is not None]
        aborted = False
        for _i, n, d in frames:
            if n == "agent_settled":
                import json as _json

                aborted = bool(_json.loads(d).get("aborted", False))
        return {"aborted": aborted, "seen": names}

    def sse_read(self, sid: str, after: int = 0, max_frames: int = 100000,
                 timeout: float = 60.0, until_settled: bool = False):
        """Read SSE frames from the stream. Returns (frames, last_id) where
        frames is a list of (id|None, name, data_str). Stops after
        max_frames, after timeout, or (until_settled) at agent_settled."""
        import json as _json

        frames = []
        last_id = after
        with httpx.stream(
            "GET", f"{self.base}/api/v1/sessions/{sid}/events?after={after}",
            timeout=httpx.Timeout(timeout, read=timeout),
        ) as r:
            assert r.status_code == 200
            name = None
            eid = None
            data_lines = []
            for line in r.iter_lines():
                if line.startswith("id: "):
                    eid = int(line[4:])
                elif line.startswith("event: "):
                    name = line[7:]
                elif line.startswith("data: "):
                    data_lines.append(line[6:])
                elif line == "":
                    if name is not None:
                        frames.append((eid, name, "\n".join(data_lines)))
                        if eid is not None:
                            last_id = eid
                        done_name = name
                        name, eid, data_lines = None, None, []
                        if len(frames) >= max_frames:
                            return frames, last_id
                        if until_settled and done_name == "agent_settled":
                            return frames, last_id
        return frames, last_id


@pytest.fixture()
def api(api_env) -> Api:
    return Api(api_env["base"])


def relaunch_app(api_env: dict, port: int | None = None,
                 extra_env: dict[str, str] | None = None) -> tuple[subprocess.Popen, str]:
    """Relaunch the app over the SAME state root (crash drills).

    Returns (proc, base_url). The caller owns the process lifetime.
    ``extra_env`` overrides env for the new process (e.g. a scaled SSE
    ring cap for the backpressure gate)."""
    env = api_env
    port = port or free_port()
    argv = [
        str(VENV_PY), "-m", "app.main",
        "--state", str(env["state_root"]),
        "--config-src", str(AGENT_CONFIG),
        "--router-url", env["router"].base_url,
        "--pi-node", env["pi"].node_bin,
        "--pi-cli", str(env["pi"].cli_js),
        "--pi-ext", str(env["pi"].ext_path) if env["pi"].ext_path else "",
        "--toolkit", str(env["run_root"] / "toolkit"),
        "--slots", "slot1,slot2,slot3",
        "--idle-kill-sec", "3600",
        "--shutdown-grace-sec", "5",
        "--gc-interval-sec", "30",
    ]
    env2 = {
        **os.environ,
        "OMNISSIAH_API_KEY": "test-super-secret-key",
        "SERVITOR_BIND": "127.0.0.1",
        "SERVITOR_PORT": str(port),
        "PYTHONPATH": str(REPO),
        **(extra_env or {}),
    }
    proc = subprocess.Popen(
        argv, cwd=str(REPO), env=env2, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            if httpx.get(base + "/healthz", timeout=2).status_code == 200:
                return proc, base
        except httpx.HTTPError:
            pass
        if proc.poll() is not None:
            raise RuntimeError(f"app exited at boot: {proc.stderr.read()[-2000:]}")
        time.sleep(0.2)
    proc.kill()
    raise RuntimeError("app never came up on relaunch")
