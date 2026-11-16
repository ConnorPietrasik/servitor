"""Stage-5 live gates (PLAN §9) — the fresh-guest certification suite.

These run against the ACTIVE manifest image under QEMU on the laptop
bridge, with the real router (key-gated) and the mock router (drills).
They are the gate: green here means the image + sync + update pipeline
is deployable; red means the version is not verified.

Ordering: pytest runs files alphabetically; the no-op/fault drills must
see a clean, healthy guest, so the destructive drills (fault injection,
extension bump) live in a separate file that runs LAST (test_z_*), and
each re-certifies (doctor green) after itself.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time

import pytest

from harness import REPO, IMAGES, GUEST_IP, active_image, GuestError
from conftest import require_root_or_skip


def has_key(name: str) -> bool:
    # the host's .env is the source of truth for the key slice (sync rides
    # it to the guest) — os.environ is NOT (the suite runs under sudo,
    # whose env_reset strips it), and a shell-exported key the sync would
    # never deploy would make the gate lie
    env_path = REPO / ".env"
    if not env_path.exists():
        return False
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line.startswith(f"{name}="):
            return bool(line.split("=", 1)[1])
    return False


# ---- artifacts -----------------------------------------------------------

def test_image_artifacts_are_versioned():
    """The image is the versioned artifact: manifest + sha + boot files."""
    require_root_or_skip()
    name, entry = active_image()
    p = IMAGES / name
    assert p.exists(), f"active image {name} missing"
    assert len(entry["sha256"]) == 64
    assert entry["pi"]["version"], "manifest must record the pi version"
    assert entry["pi"]["node"], "manifest must record the node version"
    assert entry["base"]["sha256"], "manifest must pin the base"
    assert len(entry["packages"]) > 10, "manifest must record the package set"
    stem = name[:-len(".qcow2")]
    assert (IMAGES / f"{stem}.vmlinuz").exists(), "boot kernel not shipped"
    assert (IMAGES / f"{stem}.initramfs.img").exists(), "boot initramfs not shipped"


# ---- boot + doctor ---------------------------------------------------------

def test_boot_doctor_green(guest: GuestVM):
    """The fresh-guest certification: doctor GREEN (incl. firewall probes,
    no-prompt spawn, model assert, state ext4, context-file freedom)."""
    require_root_or_skip()
    r = guest.doctor()
    assert r.returncode == 0, f"doctor red:\n{r.stdout[-4000:]}\n{r.stderr[-2000:]}"
    assert "RESULT: GREEN" in r.stdout


def test_no_prompt_spawn_is_fast_and_promptless(guest: GuestVM):
    """~1.5s to ready, no model call before the first prompt: create a
    session, assert it is idle (not busy) immediately, and that no
    router request happened (the mock counts; here: real router, so
    assert the session spent no time busy and the transcript is empty)."""
    require_root_or_skip()
    t0 = time.time()
    s = guest.create_session()
    sid = s["session_id"]
    try:
        p = guest.api("GET", f"/api/v1/sessions/{sid}/probe")[1]
        assert not p.get("busy"), "fresh session must be idle (no model call on create)"
        msgs = guest.tail_transcript(sid)
        users = [m for m in msgs if m["role"] == "user"]
        assert users == [], "no user turn exists before the first prompt"
        elapsed = time.time() - t0
        assert elapsed < 5, f"session create+probe took {elapsed:.1f}s (target ~1.5s)"
    finally:
        guest.release(sid)


def test_model_assert_matches_intended(guest: GuestVM):
    """model.id is the runtime-discovered id (never hardcoded) and matches
    the intended value; the context window matches too. Independent of the
    doctor's own check: the guest runs the app's own PiSession against the
    real router and reads get_available_models back."""
    require_root_or_skip()
    probe = r'''
import asyncio, os, tempfile
env = {}
for line in open("/etc/servitor/servitor.env"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        env[k] = v
from app.rpc.session import PiSession
from app.rpc.launch import build_launch
from app.iso.spawn import slot_identity
from app.iso.layout import PROBE_SLOT

sid = "sess-model-probe"
sdir = f"/var/lib/servitor/sessions/{sid}"
os.makedirs(sdir, exist_ok=True)
work = tempfile.mkdtemp(prefix="probe-", dir="/var/lib/servitor")
os.makedirs(f"{work}/agent", exist_ok=True)
# stage the active config into the agent dir (mimic the Lifecycle's overlay
# lowerdir) so the pi CLI can read models.json (provider + baseUrl); a raw
# PiSession does not stage the config itself
import shutil as _shutil
_active = os.readlink("/var/lib/servitor/config/active")
_cdir = f"/var/lib/servitor/config/{_active}"
for _f in os.listdir(_cdir):
    _src = f"{_cdir}/{_f}"
    if os.path.isfile(_src):
        _shutil.copy(_src, f"{work}/agent/{_f}")
# the probe slot (slot0), not an app slot: a raw spawn on an app slot uid
# could be SIGTERMed by the service's uid-gated kills (boot reconciliation
# after a tick restart) — the probe slot is outside the app's pool
ident = slot_identity(PROBE_SLOT)
# uid handoff: the Lifecycle's mount_session chowns the whole per-session
# tree to the slot BEFORE spawn — pi must be able to WRITE the session dir
# (transcript), the agent dir (models store / auth cache), and cwd (temps).
# A root-owned 0700/0755 tree leaves model.id == "unknown" (no usable model)
# even though models.json is present and readable. Reproduce that handoff.
def _chown_tree(path, uid, gid):
    os.chown(path, uid, gid)
    for dp, dn, fn in os.walk(path):
        for d in dn:
            try: os.chown(os.path.join(dp, d), uid, gid)
            except OSError: pass
        for f in fn:
            try: os.chown(os.path.join(dp, f), uid, gid)
            except OSError: pass
_chown_tree(work, ident.uid, ident.gid)
_chown_tree(sdir, ident.uid, ident.gid)
launch = build_launch(
    node_bin=env["SERVITOR_PI_NODE_BIN"],
    cli_js=env["SERVITOR_PI_CLI_JS"],
    session_dir=sdir,
    session_id=sid,
    extension_paths=(env["SERVITOR_PI_EXT_DIR"],),
    home=ident.home, user=PROBE_SLOT,
    agent_dir=f"{work}/agent",
    omnissiah_api_key=env["OMNISSIAH_API_KEY"],
    exa_api_key=env.get("EXA_API_KEY") or None,
    web_cache_root=f"{work}/webcache")

async def main():
    s = PiSession(launch, ident, cwd=work)
    try:
        await s.start()
        try:
            raw = await s.get_available_models()
        finally:
            await s.shutdown(eof_grace=2, term_grace=2)
            await s.close()
    finally:
        # leave no probe residue on the shared guest (the session-scoped VM
        # is shared by the whole suite; the probe dirs must not accumulate)
        _shutil.rmtree(work, ignore_errors=True)
        _shutil.rmtree(sdir, ignore_errors=True)
    flat = []
    def walk(x):
        if isinstance(x, dict):
            if "id" in x and ("contextWindow" in x or "context_window" in x):
                flat.append(x)
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(raw)
    intended = env["SERVITOR_INTENDED_MODEL_ID"]
    cw = int(env["SERVITOR_INTENDED_CONTEXT_WINDOW"])
    m = next((x for x in flat if x["id"] == intended), None)
    assert m is not None, f"intended model {intended!r} not in get_available_models: " \
                          f"{[x.get('id') for x in flat][:10]}"
    assert m["id"] != "unknown"
    got_cw = m.get("contextWindow", m.get("context_window"))
    assert got_cw == cw, f"context window {got_cw} != intended {cw}"
    print("MODEL-OK", m["id"], got_cw)

# serialize with the doctor's probe (same lock on the state volume): a
# concurrent doctor's release sweep deletes every file the probe uid owns —
# including this probe's live session file
import fcntl as _fcntl
import time as _time
_lock_fh = open("/var/lib/servitor/.doctor-probe.lock", "a+")
_deadline = _time.monotonic() + 300.0
while True:
    try:
        _fcntl.flock(_lock_fh.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        break
    except BlockingIOError:
        if _time.monotonic() > _deadline:
            print("LOCK-FAIL: probe lock held > 300 s")
            raise SystemExit(1)
        _time.sleep(1.0)
try:
    asyncio.run(main())
finally:
    _fcntl.flock(_lock_fh.fileno(), _fcntl.LOCK_UN)
    _lock_fh.close()
'''
    import base64
    b64 = base64.b64encode(probe.encode()).decode()
    out = guest.run(f"echo {b64} | base64 -d | /opt/servitor/app/.venv/bin/python 2>&1",
                    timeout=300, check=False)
    assert "MODEL-OK" in out, f"model assert failed:\n{out[-1500:]}"


# ---- live prompt (real router, key-gated) ---------------------------------

def test_live_prompt_end_to_end(guest: GuestVM):
    """One live prompt through the REAL router, end to end: the assistant
    answer is non-empty and the session settles. Key-gated: the router
    needs OMNISSIAH_API_KEY in the guest env (sync injects it)."""
    require_root_or_skip()
    if not has_key("OMNISSIAH_API_KEY"):
        pytest.skip("OMNISSIAH_API_KEY not set (host); the live prompt gate is key-gated")
    s = guest.create_session()
    sid = s["session_id"]
    try:
        guest.prompt(sid, "Reply with exactly: SERVITOR-LIVE-OK")
        p = guest.settle(sid, timeout=180)
        assert not p.get("busy")
        msgs = guest.tail_transcript(sid)
        assistants = [m for m in msgs if m["role"] == "assistant" and m.get("text")]
        assert assistants, "no non-empty assistant answer"
        assert any("SERVITOR-LIVE-OK" in (a.get("text") or "") for a in assistants), (
            f"expected the echo in the answer, got: {[a.get('text')[:80] for a in assistants]}")
    finally:
        guest.release(sid)


def test_live_search_key_gated(guest: GuestVM):
    """The search extension works end to end when EXA_API_KEY is present
    (env-first in pi-web-access, so it never needs a key file). SKIPPED
    when the key is absent — the gate is key-gated, not skipped-away."""
    require_root_or_skip()
    if not has_key("EXA_API_KEY"):
        pytest.skip("EXA_API_KEY not set (host); live-search gate is key-gated")
    s = guest.create_session()
    sid = s["session_id"]
    try:
        guest.prompt(sid, "Use the web search tool to search for 'pi coding agent'. "
                          "Reply with the first result's title.")
        p = guest.settle(sid, timeout=240)
        assert not p.get("busy")
        msgs = guest.tail_transcript(sid)
        tools = [m for m in msgs if m.get("kind") == "tool" or m.get("tool")]
        assert any(("web" in json.dumps(m).lower() or "search" in json.dumps(m).lower())
                   for m in msgs), "no web-search activity in the transcript"
        # the search must actually SUCCEED: a successful, non-empty tool
        # result. (A weaker "tool activity" check alone would still pass
        # when the guest has no egress — the exa call dies in the network
        # and the transcript still shows the attempted tool call.)
        tool_results = [m for m in msgs if m.get("role") == "tool_result"]
        ok = [m for m in tool_results
              if not m.get("is_error") and (m.get("result") or "").strip()]
        assert ok, ("web search produced no successful tool result (network "
                    "egress from the guest?) — "
                    f"{tool_results[-1] if tool_results else 'no tool results at all'}")
    finally:
        guest.release(sid)


# ---- sync ------------------------------------------------------------------

def test_sync_clean_then_noop(guest: GuestVM):
    """A clean sync (idempotent) is a NO-OP: nothing touched, no restart,
    exit 0. Then confirm the active config version is untouched."""
    require_root_or_skip()
    before = guest.run("readlink /var/lib/servitor/config/active")
    up_before = guest.service_uptime()
    r = guest.sync()
    assert r.returncode == 0, f"sync failed:\n{r.stdout[-3000:]}\n{r.stderr[-1000:]}"
    # the image was built from the current repo, so a first sync is a
    # NO-OP (nothing changed) OR a benign re-stage of identical content —
    # either way: no flag file left behind and the service kept running
    flag = guest.run("test -f /var/lib/servitor/update-pending.json && echo yes || echo no")
    assert flag.strip() == "no", "a NO-OP/unchanged sync must not leave the flag"
    after = guest.run("readlink /var/lib/servitor/config/active")
    up_after = guest.service_uptime()
    # uptime is monotonic-ns; a restart would RESET it (much smaller)
    assert up_after >= up_before * 0.5, "an unchanged sync must not restart the service"
    if "NO-OP" in r.stdout:
        assert after == before, "a NO-OP sync must not move the active pointer"


def test_sync_shape_change_applies_via_tick(guest: GuestVM):
    """A real shape change (config content) is staged, flagged, and the
    app's tick applies the restart — no blocking, doctor green after."""
    require_root_or_skip()
    # bump the config content deterministically
    out = guest.run("readlink /var/lib/servitor/config/active")
    vdir = "/var/lib/servitor/config/" + out.strip()
    guest.run(f"echo '{{\"servitor_test_stamp\": {int(time.time())}}}' > {vdir}/test-stamp.json")
    # that change is GUEST-side; the host sync compares host content, so
    # instead drive it host-side: sync with a test config dir that differs
    import tempfile, pathlib
    src = REPO / "agent-config"
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        shutil.copytree(src, td / "cfg")
        # a harmless, content-only difference
        (td / "cfg" / "settings.json").write_text(
            (src / "settings.json").read_text().replace("{", '{ "_servitor_test": 1, ', 1))
        r = guest.sync(SERVITOR_TEST_CONFIG_DIR=str(td / "cfg"))
        assert r.returncode == 0, f"sync (shape change) failed:\n{r.stdout[-3000:]}"
        assert "update-pending flag written" in r.stdout or "OK" in r.stdout
        # the tick applied it: the active pointer moved and no flag lingers
        deadline = time.time() + 90
        moved = False
        while time.time() < deadline:
            now = guest.run("readlink /var/lib/servitor/config/active").strip()
            flag = guest.run("test -f /var/lib/servitor/update-pending.json && echo yes || echo no").strip()
            if now != out.strip() and flag == "no":
                moved = True
                break
            time.sleep(3)
        assert moved, f"tick did not apply the config swap (active={out!r} -> " \
                      f"{guest.run('readlink /var/lib/servitor/config/active')!r})"
        # doctor green on the new state
        d = guest.doctor()
        assert d.returncode == 0, f"doctor red after shape change:\n{d.stdout[-3000:]}"
        # restore the repo config (the stamped temp dir above is gone):
        # the destructive drills (test_z_drills) run LAST and expect a
        # true no-op update, which requires the guest to match the repo
        r2 = guest.sync()
        assert r2.returncode == 0, f"restore sync failed:\n{r2.stdout[-2000:]}"
