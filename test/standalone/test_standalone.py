"""Standalone/loopback mode behind a local reverse proxy.

The app binds loopback only (the contract: bind comes from env; in
standalone that's 127.0.0.1); the proxy accepts on the LAN interface and
forwards — so the app's bind never widens, and the UI/API are reachable
from another device without any firewall hole beyond the proxy itself.

No root, no VM.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
VENV_PY = REPO / ".venv" / "bin" / "python"
PI_INSTALL = REPO / "test" / ".scratch" / "pi"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture()
def standalone():
    """App on loopback + the stdlib reverse proxy on an ephemeral port."""
    if not VENV_PY.exists():
        pytest.skip("venv missing (make venv)")
    work = Path(tempfile.mkdtemp(prefix="servitor-standalone-"))
    state = work / "state"
    state.mkdir()
    app_port = _free_port()
    proxy_port = _free_port()
    env = dict(os.environ)
    env.update({
        "SERVITOR_BIND": "127.0.0.1",
        "SERVITOR_PORT": str(app_port),
        "OMNISSIAH_API_KEY": "sk-test-standalone",
    })
    app = subprocess.Popen(
        [str(VENV_PY), "-m", "app.main",
         "--state", str(state),
         "--config-src", str(REPO / "agent-config"),
         "--pi-node", "node",
         "--pi-cli", str(PI_INSTALL / "node_modules" / "@earendil-works" /
                        "pi-coding-agent" / "dist" / "cli.js"),
         "--pi-ext", str(REPO / "test" / ".scratch" / "ext")],
        cwd=REPO, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    proxy = subprocess.Popen(
        [sys.executable, str(REPO / "deploy" / "standalone-proxy.py"),
         "--bind", "127.0.0.1",
         "--listen", str(proxy_port), "--target", f"127.0.0.1:{app_port}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def up(port: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            return False

    deadline = time.time() + 20
    while time.time() < deadline and not (up(app_port) and up(proxy_port)):
        if app.poll() is not None:
            err = app.stderr.read().decode(errors="replace")[-2000:]
            app.stderr.close()
            proxy.kill()
            shutil.rmtree(work, ignore_errors=True)
            pytest.fail(f"app died at startup:\n{err}")
        time.sleep(0.2)
    yield {
        "app_base": f"http://127.0.0.1:{app_port}",
        "proxy_base": f"http://127.0.0.1:{proxy_port}",
        "work": work,
    }
    for p in (proxy, app):
        p.send_signal(signal.SIGTERM)
    try:
        proxy.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proxy.kill()
    try:
        app.wait(timeout=10)
    except subprocess.TimeoutExpired:
        app.kill()
        app.wait(timeout=5)
    if app.stderr:
        app.stderr.close()
    shutil.rmtree(work, ignore_errors=True)


def test_loopback_reachable_behind_proxy(standalone):
    """healthz + the session list + the UI all work through the proxy,
    and the app itself never listens off loopback."""
    with urllib.request.urlopen(
            standalone["proxy_base"] + "/healthz", timeout=5) as r:
        assert r.status == 200
    with urllib.request.urlopen(
            standalone["proxy_base"] + "/api/v1/sessions", timeout=5) as r:
        sessions = json.loads(r.read())
    assert isinstance(sessions.get("sessions"), list)
    # the SPA shell comes through too
    with urllib.request.urlopen(standalone["proxy_base"] + "/", timeout=5) as r:
        assert r.status == 200
    # the app socket is loopback-only (bind discipline, not proxy magic)
    # find the listener on the app port via /proc/net/tcp
    app_port = int(standalone["app_base"].rsplit(":", 1)[1])
    hexport = format(app_port, "04X")
    listeners = []
    for line in open("/proc/net/tcp"):
        parts = line.split()
        if len(parts) > 3 and parts[1].endswith(":" + hexport) and parts[3] == "0A":
            listeners.append(parts[1].split(":")[0])
    assert listeners, f"no listener found on {app_port}"
    for local in listeners:
        ip_hex = local
        ip = socket.inet_ntop(socket.AF_INET, bytes.fromhex(ip_hex)[::-1])
        assert ip in ("127.0.0.1", "0.0.0.0") and ip == "127.0.0.1", (
            f"app listens on {ip} — standalone must bind loopback only")
