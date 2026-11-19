"""Update drills — the destructive half.

Runs LAST (test_z_ prefix) against the same session-scoped VM:
  1. world-current update (no in-major release moved, image within the
     freshness floor, shape unchanged -> nothing touched, no restart)
  2. update-WAITING (sync stages a shape change WHILE A RUN IS ACTIVE,
     exits 0 immediately, the in-process tick applies restart+doctor at
     the first idle moment)
  3. fault-injected doctor (borked config artifact -> doctor red ->
     AUTOMATIC rollback -> doctor green)

Each drill re-certifies itself (doctor green) before finishing, so the
suite ends on a green guest.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from harness import REPO, GUEST_IP, HOST_BRIDGE_IP, GuestVM, GuestError, active_image
from conftest import require_root_or_skip

sys.path.insert(0, str(REPO / "deploy"))
# the update's float rule itself: the drill's skip decision must never
# drift from the update's move decision (test/unit pins the rule)
from resolve_target import is_exact, resolve as _resolve  # noqa: E402


def _free_port() -> int:
    s = socket.socket()
    s.bind(("10.42.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _moved(declared: str, versions: list[str]) -> bool:
    """True exactly when deploy/update.sh's float would move the line —
    the float rule itself (deploy/resolve_target.py, unit-tested) on the
    SAME registry input the update reads (npm's full versions list):
    target != declared exactly when an in-major stable release is newer
    (out-of-major releases stay put — the update only REPORTS them)."""
    target, _ = _resolve(declared, versions)
    return target != declared


def skip_if_update_would_move() -> None:
    """Shared guard for the update drills: the drill's contract is "the
    world is current". If the update's own float would move a version line
    (same rule, same registry input — resolve_target.py) or the image
    outgrew the freshness floor, a guest.update() run is a REAL update
    (gate + image rebuild + swap) — skip instead of paying for a
    40-minute rebuild in the suite (and, on a host without a 'servitor'
    libvirt domain, a guaranteed swap failure that would fail the drill
    for the wrong reason). The .env reading mirrors update.sh's:
    SERVITOR_PI_VERSION is required (no default), the extension line
    defaults to 0.48.0, IMAGE_MAX_AGE_DAYS to 7 (empty = default)."""
    import re as _re
    env = (REPO / ".env").read_text()

    def _env_line(key: str) -> str:
        m = _re.search(rf"^{key}=(.+)$", env, _re.M)
        return m.group(1).strip() if m else ""

    pi_decl = _env_line("SERVITOR_PI_VERSION")
    if not pi_decl:
        pytest.skip("no SERVITOR_PI_VERSION line in .env — the update would refuse the run (the line is required); cannot certify the world is current")
    ext_decl = _env_line("SERVITOR_PI_WEB_ACCESS_VERSION") or "0.48.0"
    for key, decl in (("SERVITOR_PI_VERSION", pi_decl),
                      ("SERVITOR_PI_WEB_ACCESS_VERSION", ext_decl)):
        if not is_exact(decl):
            pytest.skip(f"{key}={decl!r} is not an exact version (X.Y.Z) — the update's preflight would refuse the run; cannot certify the world is current")

    def _npm_versions(pkg: str) -> list[str]:
        # the SAME registry input the update's float reads. npm unreachable
        # is "cannot certify the world is current", not "the world moved" —
        # skip (this is the DRILL's skip decision; the update's own
        # no-movement fail-safe lives in resolve_target).
        r = subprocess.run(["npm", "view", pkg, "versions", "--json"],
                           capture_output=True, text=True)
        if r.returncode != 0 or not r.stdout.strip():
            pytest.skip(f"npm unreachable ({pkg}) — cannot certify the world is current")
        try:
            vers = json.loads(r.stdout)
        except ValueError:
            vers = None
        if not isinstance(vers, list):
            pytest.skip(f"npm returned an unexpected versions shape for {pkg} — cannot certify the world is current")
        return vers

    if _moved(pi_decl, _npm_versions("@earendil-works/pi-coding-agent")):
        pytest.skip("the world moved (an in-major pi release is newer) — this run would be a real update, not the shape-only path")
    if _moved(ext_decl, _npm_versions("pi-web-access")):
        pytest.skip("the world moved (an in-major extension release is newer) — this run would be a real update, not the shape-only path")
    floor_raw = _env_line("IMAGE_MAX_AGE_DAYS")
    try:
        floor = int(floor_raw) if floor_raw else 7
    except ValueError:
        pytest.skip(f"IMAGE_MAX_AGE_DAYS={floor_raw!r} is not an integer — cannot certify the world is current")
    _, entry = active_image()
    built = datetime.strptime(entry["built_at"], "%Y-%m-%dT%H:%M:%SZ")
    age_days = int((datetime.now(timezone.utc).replace(tzinfo=None) - built).total_seconds() // 86400)
    if age_days > floor:
        pytest.skip(f"the image outgrew the freshness floor ({age_days}d > {floor}d) — this run would rebuild it")


def test_update_noop_touches_nothing(guest: GuestVM):
    """World-current run: no in-major release newer than the deployed one,
    the image is within the freshness floor, and the shape is unchanged
    -> NOTHING is touched (no restart, no flag), exit 0."""
    require_root_or_skip()
    skip_if_update_would_move()
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


def test_update_fault_injected_rollback(guest: GuestVM):
    """Fault-injected doctor: the sync's config artifact is borked
    (models.json not parseable) -> the deploy lands -> doctor RED ->
    AUTOMATIC ROLLBACK restores the previous config -> doctor GREEN.
    The host is never left half-updated."""
    require_root_or_skip()
    # the drill exercises the SHAPE-only rollback path: if the float would
    # move a version line or the image outgrew the floor, guest.update() is
    # a real update (gate + rebuild + swap — swap_image would target the
    # host's 'servitor' domain) — skip, like the no-op drill above.
    skip_if_update_would_move()
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
