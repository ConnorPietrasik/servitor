"""Update drills — the destructive half.

Runs LAST (test_z_ prefix) against the same session-scoped VM:
  1. no-op update (nothing changed -> nothing touched, no restart)
  2. update-WAITING (sync stages a shape change WHILE A RUN IS ACTIVE,
     exits 0 immediately, the in-process tick applies restart+doctor at
     the first idle moment)
  3. extension bump (full conformance gate, deploy, doctor green,
     installed version stamped)
  4. fault-injected doctor (borked config artifact -> doctor red ->
     AUTOMATIC rollback -> doctor green)

Each drill re-certifies itself (doctor green) before finishing, so the
suite ends on a green guest.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from harness import REPO, GUEST_IP, HOST_BRIDGE_IP, GuestVM, GuestError
from conftest import require_root_or_skip


def _free_port() -> int:
    s = socket.socket()
    s.bind(("10.42.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_update_noop_touches_nothing(guest: GuestVM):
    """No-op run: resolved selections match what's deployed -> NOTHING is
    touched (no restart, no flag), exit 0."""
    require_root_or_skip()
    up_before = guest.service_uptime()
    r = guest.update()
    assert r.returncode == 0, f"update (no-op) failed:\n{r.stdout[-3000:]}"
    assert "NO-OP" in r.stdout, f"expected the sync NO-OP path, got:\n{r.stdout[-2000:]}"
    flag = guest.run("test -f /var/lib/servitor/update-pending.json && echo yes || echo no").strip()
    assert flag == "no", "a no-op update must not write the flag"
    up_after = guest.service_uptime()
    assert up_after >= up_before * 0.5, "a no-op update must not restart the service"


def test_update_waiting_drill(guest: GuestVM):
    """THE update-waiting drill: a long run is active; a sync with a changed config
    must stage non-disruptively, set the flag, and EXIT 0 without
    touching the run. The app's tick then applies restart+doctor at the
    first idle moment. Nothing is ever blocked or interrupted."""
    require_root_or_skip()
    # a mock router on the host, reachable from the guest over the bridge
    from test.mock_router.server import MockRouter
    router = MockRouter()
    router.start(host=HOST_BRIDGE_IP)  # reachable from the guest over the bridge
    port = router.port
    try:
        # test config: the repo config pointed at the mock router
        src = REPO / "agent-config"
        cfgdir = Path("/tmp/servitor-drill-config")
        if cfgdir.exists():
            shutil.rmtree(cfgdir)
        shutil.copytree(src, cfgdir)
        models = json.loads((cfgdir / "models.json").read_text())
        for prov in models["providers"].values():
            prov["baseUrl"] = f"http://{HOST_BRIDGE_IP}:{port}/v1"
        (cfgdir / "models.json").write_text(json.dumps(models, indent=2))

        # point the guest at the mock: sync the test config (initial deploy)
        r = guest.sync(SERVITOR_TEST_CONFIG_DIR=str(cfgdir))
        assert r.returncode == 0, f"initial mock-config sync failed:\n{r.stdout[-3000:]}"
        deadline = time.time() + 120
        while time.time() < deadline:
            try:
                guest.wait_healthz(timeout=5)
                break
            except GuestError:
                time.sleep(2)
        else:
            pytest.fail("service not healthy after the mock-config sync")

        # a LONG run: the mock holds the stream open for 150s before the
        # first piece — the run is active well past the sync below
        router.set_script(
            {"gap_seconds": 150, "final_text": "drill done", "chunk_count": 2})
        s = guest.create_session()
        sid = s["session_id"]

        # make the config content differ for the second sync
        stamp = cfgdir / "settings.json"
        stamp.write_text(stamp.read_text().replace("{", '{ "_drill": 2, ', 1))

        # the second sync WHILE THE RUN IS ACTIVE
        guest.prompt(sid, "start the long drill")
        time.sleep(8)  # let the run become active (pi spawn + first request)
        t0 = time.time()
        r = guest.sync(SERVITOR_TEST_CONFIG_DIR=str(cfgdir))
        sync_elapsed = time.time() - t0
        assert r.returncode == 0, f"update-waiting sync failed:\n{r.stdout[-3000:]}"
        assert "update-WAITING" in r.stdout or "flag written" in r.stdout, (
            f"expected the waiting path, got:\n{r.stdout[-2000:]}")
        assert sync_elapsed < 60, (
            f"sync blocked {sync_elapsed:.0f}s — it must not wait on a run")
        # the run was NOT interrupted: still busy immediately after the sync
        busy = guest.api("GET", f"/api/v1/sessions/{sid}/probe")[1].get("busy")
        assert busy, "the active run must survive the sync (never interrupted)"

        # the flag is set and survives
        flag = guest.run("test -f /var/lib/servitor/update-pending.json && echo yes || echo no").strip()
        assert flag == "yes", "the flag must be set while waiting"

        # wait for the run to settle, then the tick applies restart+doctor
        guest.settle(sid, timeout=300)
        deadline = time.time() + 240
        applied = False
        while time.time() < deadline:
            flag = guest.run("test -f /var/lib/servitor/update-pending.json && echo yes || echo no").strip()
            if flag == "no":
                applied = True
                break
            time.sleep(5)
        assert applied, "the tick did not consume the flag after the run settled"
        # the service restarted (uptime reset) and is healthy again
        time.sleep(3)
        guest.wait_healthz(timeout=60)
        up = guest.service_uptime()
        assert up < 120, f"service uptime {up:.0f}s — the restart did not happen"
        d = guest.doctor()
        assert d.returncode == 0, f"doctor red after the deferred apply:\n{d.stdout[-3000:]}"
        guest.release(sid)
    finally:
        router.stop()


def test_update_extension_bump(guest: GuestVM):
    """Extension bump: resolve a DIFFERENT existing version, the full
    conformance gate runs host-side, the deploy swaps the extension,
    stamps the registry, and the doctor is green on the new version."""
    require_root_or_skip()
    # pick the newest pi-web-access other than what's deployed
    out = subprocess.run(
        ["npm", "view", "pi-web-access", "versions", "--json"],
        capture_output=True, text=True, check=True).stdout
    versions = [v for v in json.loads(out) if not any(c in v for c in ".-rc")]
    deployed = guest.run(
        "node -e \"try{console.log(require('/opt/pi-ext/pi-web-access/package.json').version)}catch(e){}\"").strip()
    candidates = [v for v in versions if v != deployed]
    if not candidates:
        pytest.skip("no alternative pi-web-access version available")
    target = candidates[-1]
    # the target must actually be resolvable/installable
    r = subprocess.run(["npm", "view", f"pi-web-access@{target}", "version"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip(f"pi-web-access@{target} not installable: {r.stderr[:200]}")

    # run the update with the bumped selection (temp env file)
    env_text = (REPO / ".env").read_text()
    import re as _re
    env_bumped = _re.sub(r"^SERVITOR_PI_WEB_ACCESS_VERSION=.*$",
                         f"SERVITOR_PI_WEB_ACCESS_VERSION={target}",
                         env_text, flags=_re.M)
    env_path = Path("/tmp/servitor-drill-env")
    env_path.write_text(env_bumped)
    try:
        r = guest.update(env_file=str(env_path))
        assert r.returncode == 0, f"update (extension bump) failed:\n{r.stdout[-4000:]}"
        installed = guest.run(
            "node -e \"console.log(require('/opt/pi-ext/pi-web-access/package.json').version)\"").strip()
        assert installed == target, f"installed {installed} != target {target}"
        stamp = guest.run(
            "cd /opt/servitor/app && .venv/bin/python -c \""
            "from app.registry import Registry; r = Registry('/var/lib/servitor/registry.db'); "
            f"print(r.get_version('pi_web_access')); r.close()\"").strip()
        assert stamp == target, f"registry stamp {stamp!r} != {target!r}"
        d = guest.doctor()
        assert d.returncode == 0, f"doctor red on the bumped extension:\n{d.stdout[-3000:]}"
    finally:
        env_path.unlink(missing_ok=True)


def test_update_fault_injected_rollback(guest: GuestVM):
    """Fault-injected doctor: the sync's config artifact is borked
    (models.json not parseable) -> the deploy lands -> doctor RED ->
    AUTOMATIC ROLLBACK restores the previous config -> doctor GREEN.
    The host is never left half-updated."""
    require_root_or_skip()
    active_before = guest.run("readlink /var/lib/servitor/config/active").strip()
    r = guest.update(SERVITOR_TEST_CONFIG_FAULT="bork")
    # the update's own verify step must have caught the red doctor and
    # rolled back: final exit 0 with the rollback reported. The success
    # marker on stdout is "ROLLED BACK" (the "AUTOMATIC ROLLBACK" trigger
    # line goes to stderr, so it cannot be the success signal)
    assert r.returncode == 0, (
        f"update (fault) did not recover:\n{r.stdout[-4000:]}")
    assert "ROLLED BACK" in r.stdout, f"expected the rollback path, got:\n{r.stdout[-3000:]}"
    # back to the previous config version
    active_after = guest.run("readlink /var/lib/servitor/config/active").strip()
    assert active_after == active_before, (
        f"rollback did not restore the config (was {active_before}, now {active_after})")
    # healthy again
    d = guest.doctor()
    assert d.returncode == 0, f"doctor red after rollback:\n{d.stdout[-3000:]}"
