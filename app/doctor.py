"""servitorctl doctor — the deploy gate.

Certifies the guest, read-only with respect to the deployed artifacts
(config, image, env file), with a pass/fail report. Runs as root on the
guest, invoked from the host by ``deploy/doctor.sh`` and in-process by
the app's update tick:

    .venv/bin/python -m app.doctor --state ROOT [--config-src DIR]
        [--pi-node BIN --pi-cli JS --pi-ext DIR] [--toolkit DIR]
        [--slots slot1,...] [--skip-spawn]

Env-derived values (keys, subnets) come from the 0600 env file; keys
ride env only and are never written to the report. The stateful checks
(no-prompt spawn, then one live run) create a real probe session and
release it: the live run certifies a REAL model call leaving the VM
(VM -> edge -> router -> model) and, with EXA_API_KEY, public egress +
the search extension (one prompt, both jobs). LLM-key-gated; the
search leg is EXA-key-gated.

Exit: 0 = green (warnings allowed), 1 = at least one FAIL.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from contextlib import asynccontextmanager

from app.env import deployment_env, optional_env
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"

CONTEXT_FILE_NAMES = ("AGENTS.md", "SYSTEM.md", "APPEND_SYSTEM.md")


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str = ""


@dataclass
class Report:
    results: list[CheckResult] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.results.append(CheckResult(name, status, detail))
        mark = {"PASS": "PASS", "FAIL": "FAIL", "WARN": "WARN", "SKIP": "SKIP"}[status]
        print(f"{mark:4}  {name}" + (f"  — {detail}" if detail else ""))

    @property
    def fails(self) -> int:
        return sum(1 for r in self.results if r.status == FAIL)

    @property
    def warns(self) -> int:
        return sum(1 for r in self.results if r.status == WARN)


def _sh(cmd: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def _msg_text(m: dict[str, Any]) -> str:
    """Flatten a raw pi message (driver.get_messages() shape) to its text."""
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(
            p.get("text", "")
            for p in c
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return ""


# ---------------------------------------------------------------------- #
# static checks (no session)                                             #
# ---------------------------------------------------------------------- #


def check_env(rep: Report, args: argparse.Namespace) -> None:
    key = os.environ.get(args.api_key_env, "")
    if key:
        rep.add("env.llm_key", PASS, f"{args.api_key_env} present (value never reported)")
    else:
        rep.add("env.llm_key", FAIL, f"{args.api_key_env} is empty — no live spawn possible")
    exa = os.environ.get(args.exa_key_env, "")
    if exa:
        rep.add("env.exa_key", PASS, "EXA_API_KEY present")
    else:
        rep.add("env.exa_key", WARN, "EXA_API_KEY absent — live search disabled")


def check_state_fs(rep: Report, root: str) -> None:
    code, out = _sh(["stat", "-f", "-c", "%T", root])
    fstype = out.strip().splitlines()[0] if code == 0 and out.strip() else "?"
    if fstype in ("ext2/ext3", "ext4"):
        rep.add("state.fs", PASS, f"{root} is {fstype} (real ext4, not overlay)")
    else:
        rep.add("state.fs", FAIL, f"{root} is {fstype} — the state volume must be real ext4")


def check_state_mount(rep: Report, root: str) -> None:
    # The state volume contract is a separate ext4 DISK. The fstype check
    # alone cannot catch a MISSING disk: the unmounted fallback dir is on
    # the 24G root disk, which is also ext4. findmnt --mountpoint (exact
    # match), NOT -T. WARN, not FAIL: laptop dev uses a plain dir by
    # design; on the guest it signals INSTALL.md step 5 was skipped.
    code, out = _sh(["findmnt", "--mountpoint", root, "-n", "-o", "SOURCE"])
    src = out.strip().splitlines()[0] if code == 0 and out.strip() else ""
    if src:
        rep.add("state.mount", PASS, f"state root is a separate mount ({src})")
    else:
        rep.add(
            "state.mount",
            WARN,
            "state root is NOT a separate mount point — laptop dev is fine with "
            "this; on the guest it means the state disk is missing and all state "
            "silently lives on the 24G root disk (create + attach it, reboot)",
        )


def check_ancestors(rep: Report, root: str, toolkit: str) -> None:
    bad: list[str] = []
    checked = 0
    for base in (root, toolkit):
        p = Path(base).resolve()
        while True:
            checked += 1
            for name in CONTEXT_FILE_NAMES:
                if (p / name).exists():
                    bad.append(str(p / name))
            if (p / ".pi").is_dir():
                bad.append(str(p / ".pi"))
            if p.parent == p:
                break
            p = p.parent
    if bad:
        rep.add("ancestors.context_free", FAIL, f"context files found: {bad[:5]}")
    else:
        rep.add("ancestors.context_free", PASS, f"{checked} ancestor levels clean (state + toolkit)")


def check_config_version(rep: Report, config_root: str) -> None:
    from app.iso.configswap import active_dir, active_version

    v = active_version(config_root)
    if v is None:
        rep.add("config.active", FAIL, f"no active pointer at {config_root}/active")
        return
    d = active_dir(config_root)
    if d and Path(d).is_dir():
        rep.add("config.active", PASS, f"active pointer -> {v} (dir exists)")
    else:
        rep.add("config.active", FAIL, f"active pointer -> {v} but {d} missing")
        return  # the dir is gone; the per-file checks would be noise
    for name in ("SYSTEM.md", "models.json", "settings.json"):
        if not (Path(d) / name).exists():
            rep.add(f"config.file.{name}", FAIL, f"{d}/{name} missing from the active config")
            return
    rep.add("config.files", PASS, "SYSTEM.md + models.json + settings.json present")
    # JSON validity: a truncated artifact must fail HERE with a named
    # check, not inside the spawn check's models parse
    for name in ("models.json", "settings.json"):
        try:
            json.loads((Path(d) / name).read_text())
        except (ValueError, OSError) as e:
            rep.add(f"config.json.{name}", FAIL, f"{d}/{name} is not valid JSON: {e}")
    else:
        rep.add("config.json", PASS, "models.json + settings.json parse")


def check_slots(rep: Report, slot_names: list[str]) -> None:
    if not slot_names:
        rep.add("slots.pool", SKIP, "no slots given")
        return
    import pwd

    missing = []
    for s in slot_names:
        try:
            pwd.getpwnam(s)
        except KeyError:
            missing.append(s)
    if missing:
        rep.add("slots.users", FAIL, f"missing slot users: {missing}")
        return
    rep.add("slots.users", PASS, f"{len(slot_names)} slot users present")
    # no root-owned files inside slot-owned trees (the sweep invariant)
    code, out = _sh([
        "find", "/home",
        "-maxdepth", "1", "-type", "d", "-name", "slot*", "-user", "root",
        "-exec", "find", "{}", "-user", "root", "-print", ";",
    ])
    root_owned = [l for l in out.strip().splitlines() if l.strip()]
    if root_owned:
        rep.add("slots.no_root_files", FAIL, f"root-owned under slot homes: {root_owned[:5]}")
    else:
        rep.add("slots.no_root_files", PASS, "no root-owned files under slot homes")
    # homes are 0700
    bad_mode = []
    for s in slot_names:
        try:
            mode = Path(f"/home/{s}").stat().st_mode & 0o777
            if mode != 0o700:
                bad_mode.append(f"{s}:{oct(mode)}")
        except OSError:
            bad_mode.append(f"{s}:unreadable")
    if bad_mode:
        rep.add("slots.home_mode", FAIL, f"not 0700: {bad_mode[:5]}")
    else:
        rep.add("slots.home_mode", PASS, "all slot homes 0700")


def check_versions(rep: Report, args: argparse.Namespace, registry) -> None:
    # installed pi version == the env selection
    env_pi = optional_env("SERVITOR_PI_VERSION") or ""
    cli = Path(args.pi_cli or "")
    pkg = None
    p = cli
    while p and not (p / "package.json").exists():
        p = p.parent if p != p.parent else None
    if p:
        try:
            pkg = json.loads((p / "package.json").read_text()).get("version")
        except (OSError, ValueError):
            pkg = None
    if pkg:
        if env_pi and pkg != env_pi:
            rep.add("versions.pi", FAIL, f"installed pi {pkg} != .env selection {env_pi}")
        else:
            rep.add("versions.pi", PASS, f"installed pi {pkg}")
    else:
        rep.add("versions.pi", FAIL, "could not read the installed pi package.json")
    # installed extension == registry stamp == (exact) env selection
    env_ext = optional_env("SERVITOR_PI_WEB_ACCESS_VERSION") or ""
    ext_dir = Path(args.pi_ext or "")
    ext_ver = None
    if ext_dir.is_dir():
        try:
            ext_ver = json.loads((ext_dir / "package.json").read_text()).get("version")
        except (OSError, ValueError):
            ext_ver = None
    stamp = registry.get_version("pi_web_access") if registry else None
    if ext_ver is None:
        rep.add("versions.extension", FAIL, "no installed pi-web-access (the sync owns /opt/pi-ext)")
    elif stamp != ext_ver:
        rep.add("versions.extension", FAIL, f"registry stamp {stamp!r} != installed {ext_ver}")
    else:
        detail = f"installed pi-web-access {ext_ver} (stamp matches)"
        if env_ext and env_ext.count(".") > 0 and env_ext != ext_ver:
            rep.add("versions.extension", FAIL, detail + f" != .env selection {env_ext}")
        elif env_ext:
            rep.add("versions.extension", PASS, detail + f" == .env {env_ext}")
        else:
            rep.add("versions.extension", PASS, detail)


def check_firewall_rules(rep: Report) -> None:
    port = optional_env("SERVITOR_PORT") or ""
    edge = optional_env("SERVITOR_EDGE_SUBNET") or ""
    code, out = _sh(["nft", "list", "ruleset"])
    if code != 0:
        rep.add("firewall.ruleset", FAIL, f"nft list ruleset failed: {out[:200]}")
        return
    if "table inet servitor" not in out:
        rep.add("firewall.ruleset", FAIL, "the servitor nftables table is not loaded")
        return
    rep.add("firewall.ruleset", PASS, "servitor table loaded (live probes run host-side)")
    if port and edge:
        if f"tcp dport {port}" in out and edge in out and "policy drop" in out:
            rep.add("firewall.port", PASS, f"port {port} admitted from {edge} only, policy drop")
        else:
            rep.add("firewall.port", FAIL, f"ruleset lacks port {port}/{edge}/policy-drop")


def check_ui(rep: Report) -> None:
    """The chat UI is served by the app itself (static, same port): GET /
    on the app's bind address must answer 200. If ui/dist is missing
    from the deployed tree, / is a bare FastAPI 404 while the API stays
    healthy — only this check sees it."""
    bind = optional_env("SERVITOR_BIND") or ""
    port = optional_env("SERVITOR_PORT") or ""
    if not bind or not port:
        rep.add("ui.index", FAIL,
                "SERVITOR_BIND/SERVITOR_PORT unset — cannot probe the UI")
        return
    import urllib.request

    url = f"http://{bind}:{port}/"
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            status, ctype = r.status, r.headers.get("Content-Type", "")
    except Exception as e:  # transport failure — report, don't crash the doctor
        rep.add("ui.index", FAIL, f"GET {url} failed ({type(e).__name__}: {e})")
        return
    if status == 200 and "text/html" in ctype:
        rep.add("ui.index", PASS, "GET / -> 200 text/html (SPA served by the app)")
    else:
        rep.add(
            "ui.index",
            FAIL,
            f"GET / -> {status} {ctype or '(no content-type)'} — want 200 text/html "
            "(ui/dist missing from the deployed tree?)",
        )


def check_disk(rep: Report, root: str) -> None:
    sizes = {}
    for sub in ("scratch", "inbox", "sessions"):
        code, out = _sh(["du", "-sk", f"{root}/{sub}"])
        if code == 0 and out.strip():
            sizes[sub] = int(out.strip().split()[0]) * 1024
    detail = ", ".join(f"{k}={v // (1024 * 1024)}MB" for k, v in sizes.items()) or "empty"
    code, out = _sh(["df", "-B1", "--output=avail", root])
    avail = int(out.strip().splitlines()[1]) if code == 0 and len(out.strip().splitlines()) > 1 else 0
    if avail < 1 * 1024 * 1024 * 1024:
        rep.add("disk.headroom", FAIL, f"only {avail // (1024 * 1024)}MB free on the state volume")
    else:
        rep.add("disk.headroom", PASS, f"{avail // (1024 * 1024)}MB free; {detail}")


def check_keys_absent(rep: Report, args: argparse.Namespace, root: str) -> None:
    needles: list[str] = []
    for var in (args.api_key_env, args.exa_key_env):
        v = os.environ.get(var, "")
        if v:
            needles.append(v)
    if not needles:
        rep.add("keys.absent", WARN, "no key values in env — sweep skipped")
        return
    # sweep roots: the synced tree, session files, the app log dir.
    # Key values must be nowhere.
    roots = [f"{root}/config", f"{root}/lib", f"{root}/sessions"]
    log_dir = "/var/log/servitor"
    if os.path.isdir(log_dir):
        roots.append(log_dir)
    found: list[str] = []
    for r in roots:
        if not os.path.isdir(r):
            continue
        for needle in needles:
            # one needle PER grep: multiple would make grep take the second
            # as a FILE argument, and the error line echoes the key value
            # into the report
            code, out = _sh(["grep", "-rIl", "-e", needle, "--", r])
            if code != 0:  # 1 = no match, 2 = error: never a hit
                continue
            for line in out.strip().splitlines():
                if line and needle not in line:
                    found.append(line)
    if found:
        rep.add("keys.absent", FAIL, f"key material found in: {found[:5]}")
    else:
        rep.add("keys.absent", PASS, "key values absent from config/lib/sessions/logs")
    # auth.json / models-store.json: only per-session scratch uppers
    code, out = _sh(["find", root, "-name", "auth.json", "-o", "-name", "models-store.json", "-print"])
    bad = []
    for line in out.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        if f"{root}/scratch/" not in line:
            bad.append(line)
    if bad:
        rep.add("keys.no_stores", FAIL, f"auth/model stores outside scratch uppers: {bad[:5]}")
    else:
        rep.add("keys.no_stores", PASS, "auth.json/models-store.json only in per-session scratch")
    # web-search.json: nowhere
    code, out = _sh(["find", root, "/opt", "/etc", "/home", "-name", "web-search.json", "-print"])
    hits = [l for l in out.strip().splitlines() if l.strip()]
    if hits:
        rep.add("keys.no_web_search", FAIL, f"web-search.json exists: {hits[:5]}")
    else:
        rep.add("keys.no_web_search", PASS, "no web-search.json anywhere")


# ---------------------------------------------------------------------- #
# the no-prompt spawn check (stateful: creates + releases one session)  #
# ---------------------------------------------------------------------- #


class ProbeLockTimeout(Exception):
    """Another doctor held the probe lock past the deadline."""


@asynccontextmanager
async def _probe_lock(state_root: str, timeout: float = 240.0):
    """Serialize the stateful probe section across concurrent doctors
    (host doctor.sh and the app tick's inline doctor). Both probes use
    the probe uid, and a release's sweep deletes EVERYTHING that uid
    owns. The 240 s deadline fails the spawn check with a re-run hint
    before the capped doctor budget (600 s) can kill the waiter
    silently — waiting out the full concurrent probe would leave no
    time for its own live run."""
    import fcntl as _fcntl

    lock_path = f"{state_root.rstrip('/')}/.doctor-probe.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    deadline = time.monotonic() + timeout
    while True:
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() > deadline:
                os.close(fd)
                raise ProbeLockTimeout
            await asyncio.sleep(1.0)
    try:
        yield
    finally:
        try:
            _fcntl.flock(fd, _fcntl.LOCK_UN)
        finally:
            os.close(fd)


async def check_no_prompt_spawn(rep: Report, args: argparse.Namespace) -> None:
    from app.lifecycle import Lifecycle, LifecycleConfig, PiInstall
    from app.iso.layout import state_paths
    from app.registry import (
        ALLOCATED,
        CLEANING,
        DRAINING,
        FREE,
        LIVE,
        Registry,
    )
    from app.iso.slots import SlotPool

    import pwd as _pwd

    # the probe slot is outside the app's pool by contract: a probe on an
    # app slot would race the service's uid-gated kills (boot reconciliation
    # after a tick restart SIGTERMs every process of a slot uid that owns a
    # live row — the probe's own row)
    if args.probe_slot in args.slots:
        rep.add(
            "spawn.probe_slot",
            FAIL,
            f"probe slot {args.probe_slot} is in the app slot list — the probe "
            "must use a slot the app never allocates",
        )
        return
    try:
        _pw = _pwd.getpwnam(args.probe_slot)
    except KeyError:
        rep.add(
            "spawn.probe_slot",
            FAIL,
            f"probe slot user {args.probe_slot!r} missing from passwd "
            "(the image build must create it — uid 9000)",
        )
        return

    state = state_paths(args.state)
    registry = Registry(args.registry or f"{args.state}/registry.db")
    # pool of the probe slot ONLY: the probe is the sole user of it
    pool = SlotPool((args.probe_slot,))
    api_key = os.environ.get(args.api_key_env, "")
    lc = Lifecycle(
        LifecycleConfig(
            state=state,
            pool=pool,
            registry=registry,
            pi=PiInstall(node_bin=args.pi_node, cli_js=args.pi_cli, ext_path=args.pi_ext),
            api_key=api_key,
            exa_key=os.environ.get(args.exa_key_env) or None,
            toolkit_dir=args.toolkit,
            baseline_extensions=tuple(
                args.baseline_ext or ([Path(args.pi_ext).name] if args.pi_ext else [])
            ),
            update_pending_path="",
        )
    )
    # stale probe rows from a previous doctor that died mid-probe: ONLY the
    # doctor can clean them up (the app's pool does not include the probe
    # slot). Rebuild the pool view, then the normal release path: ladder
    # (kills any stray pi child), sweep, free. A row already CLEANING
    # cannot re-enter draining: clean it physically and close the state
    # machine by idempotent transition.
    for row in registry.list_sessions(include_released=False):
        if row.slot != args.probe_slot or row.state == FREE:
            continue
        s = pool.get(args.probe_slot)
        s.state = "allocated"
        s.session_id = row.id
        try:
            if row.state in (ALLOCATED, LIVE, DRAINING):
                await lc.release(row.id, reason="stale doctor probe row (previous doctor died)")
            else:  # CLEANING: the previous doctor died mid-release
                from app.iso.layout import SessionMountPaths
                from app.iso.sweep import _mounted_targets, kill_strays_by_uid, sweep_by_owner

                uid = _pw.pw_uid
                await asyncio.to_thread(kill_strays_by_uid, uid)
                sp = SessionMountPaths.for_session(row.id, state.scratch)
                mounted = _mounted_targets()
                for target in (sp.workdir, sp.agent_dir):
                    if target in mounted:
                        subprocess.run(["umount", "-l", target],
                                       capture_output=True, timeout=10)
                excludes = (state.inbox_for(row.id), state.scratch_for(row.id),
                            state.sessions_for(row.id))
                sweep = await asyncio.to_thread(
                    sweep_by_owner, uid, excludes, keep_dir=_pw.pw_dir)
                for area in excludes:
                    if os.path.lexists(area):
                        shutil.rmtree(area, ignore_errors=True)
                registry.set_state(row.id, CLEANING, cause="stale probe cleanup")
                registry.set_state(row.id, FREE, cause="stale probe cleanup verified")
                if sweep.ok:
                    rep.add("spawn.stale_probe", PASS,
                            f"released stale probe row {row.id[:8]} (was cleaning)")
                else:
                    rep.add("spawn.stale_probe", WARN,
                            f"stale probe row {row.id[:8]}: sweep not clean: {sweep.errors[:3]}")
            if row.state != CLEANING:
                rep.add("spawn.stale_probe", PASS,
                        f"released stale probe row {row.id[:8]} (was {row.state})")
        except Exception as e:
            rep.add("spawn.stale_probe", WARN, f"stale probe row {row.id[:8]} not released: {e}")
    # the spawn uses the ACTIVE config pointer (require_active_dir);
    # check_config_version already certifies it exists + parses
    t0 = time.monotonic()
    sid: str | None = None
    try:
        out = await lc.start_session("doctor-probe")
        if out.get("waiting"):
            rep.add("spawn.no_prompt", FAIL, "session went to wait state (slot pressure)")
            return
        sid = out["session_id"]
        spawn_sec = time.monotonic() - t0
        live = lc.live.get(sid)
        driver = live.driver if live else None
        if driver is None:
            rep.add("spawn.no_prompt", FAIL, "session created but no driver")
            return
        # launch line: executed argv == the golden constant; key off argv
        argv = list(driver.launched_argv)
        if argv != list(driver.launch.argv):
            rep.add("spawn.launch_line", FAIL, "executed argv drifted from the constant")
        else:
            rep.add(
                "spawn.launch_line",
                PASS,
                f"argv == constant ({len(argv)} parts; --no-approve, no --api-key, "
                f"key absent from argv)",
            )
        if api_key and any(api_key in a for a in argv):
            rep.add("spawn.key_off_argv", FAIL, "the key appears on argv")
        elif api_key:
            rep.add("spawn.key_off_argv", PASS, "key rode env only")
        # no model call, ever: get_available_models is a control command
        try:
            models = await driver.get_available_models()
        except Exception as e:
            rep.add("spawn.models", FAIL, f"get_available_models failed: {e}")
            return
        blob = json.dumps(models)
        # the active models.json's provider + baseUrl must be listed
        mj = json.loads(Path(args.config_src, "models.json").read_text())
        providers = mj.get("providers", {})
        prov = next(iter(providers.values()), {})
        pname = providers and next(iter(providers.keys()), "?")
        pbase = prov.get("baseUrl", "")
        if pname in blob and pbase in blob:
            rep.add("spawn.models", PASS, f"provider {pname} + baseUrl {pbase} listed")
        else:
            rep.add("spawn.models", FAIL, f"provider/baseUrl not listed: {blob[:300]}")
        # the no-prompt spawn must resolve a USABLE model (the app's own
        # fail-closed signal: model.id == "unknown"). No id or context
        # window pin: the live prompt below proves the router serves it.
        try:
            model = await driver.assert_model_usable()
        except Exception as e:
            rep.add("spawn.model_usable", FAIL, f"no usable model: {e}")
        else:
            rep.add(
                "spawn.model_usable",
                PASS,
                f"model {model.get('id')} resolved, ctx={model.get('contextWindow', '?')}"
                " (usability only — no id/context pin)",
            )
        # spawn timing (informational: ~1.5 s expected; slow = WARN)
        if spawn_sec <= 5.0:
            rep.add("spawn.timing", PASS, f"no-prompt spawn in {spawn_sec:.2f}s (no model call)")
        else:
            rep.add("spawn.timing", WARN, f"no-prompt spawn slow: {spawn_sec:.2f}s")
        # RO bind: the agent-dir models.json is a read-only bind
        sp = live.sp
        agent_models = None
        for cand in (
            Path(sp.agent_dir) / "models.json" if getattr(sp, "agent_dir", None) else None,
        ):
            if cand and cand.exists():
                agent_models = cand
        if agent_models is None:
            rep.add("spawn.ro_bind", WARN, "could not locate the bound models.json — check skipped")
        else:
            try:
                with open(agent_models, "ab"):
                    pass
                rep.add("spawn.ro_bind", FAIL, "WRITE to the RO-bound models.json succeeded")
            except OSError as e:
                if e.errno == 30:  # EROFS
                    rep.add("spawn.ro_bind", PASS, "write to RO bind -> EROFS")
                else:
                    rep.add("spawn.ro_bind", FAIL, f"write to RO bind -> {e}")
            try:
                os.unlink(agent_models)
                rep.add("spawn.ro_bind.unlink", FAIL, "UNLINK on the RO bind succeeded")
            except OSError as e:
                if e.errno in (16, 30):  # EBUSY / EROFS
                    rep.add("spawn.ro_bind.unlink", PASS, "unlink on RO bind -> EBUSY")
                else:
                    rep.add("spawn.ro_bind.unlink", FAIL, f"unlink on RO bind -> {e}")
        # extension baseline (drift = warning)
        row2 = registry.get(sid)
        loaded = tuple(row2.loaded_extensions) if row2 else ()
        baseline = lc.cfg.baseline_extensions
        extra = tuple(sorted(set(loaded) - set(baseline)))
        if extra:
            rep.add("spawn.extensions", WARN, f"loaded beyond image baseline: {extra} (drift)")
        else:
            rep.add("spawn.extensions", PASS, f"loaded set == baseline ({list(loaded) or 'none'})")
        # ---- live run (THE integration gate) -----------------------------
        # Everything above makes NO network call: classic deploy failures
        # (hairpin NAT on the router's public name, a dead key, a broken
        # router path, a dead PUBLIC egress path) only surface when a REAL
        # model call leaves the VM. ONE prompt does both jobs: it makes the
        # agent search the web, so the same run certifies public egress +
        # the installed pi-web-access (the model router is reached
        # edge-internally — a dead public path looks green to the
        # no-prompt gate). Two report lines, one settle wait.
        # Key-gated: without EXA_API_KEY the run degrades to the echo-only
        # prompt and spawn.live_search is not reported.
        exa_key = os.environ.get(args.exa_key_env, "")
        if api_key:
            prompt_text = (
                "Use the web search tool to search for 'pi coding agent'. "
                "Reply with the first result's title, and make your final "
                "line exactly: SERVITOR-LIVE-OK"
                if exa_key else "Reply with exactly: SERVITOR-LIVE-OK")
            t1 = time.monotonic()
            try:
                # prompt_and_wait_settled installs the settle waiter BEFORE
                # the prompt (a fast run can settle between the prompt's
                # reply and a post-hoc waiter). 300 s: two model turns +
                # one Exa round trip.
                disposition, aborted = await driver.prompt_and_wait_settled(
                    prompt_text, timeout=300)
                if disposition == "handled":
                    # pi consumed the prompt WITHOUT starting a run —
                    # a precise failure, not a 300 s settle timeout that
                    # would blame key/router/egress.
                    rep.add(
                        "spawn.live_prompt", FAIL,
                        f"prompt disposition {disposition!r} — consumed "
                        "without a run; no model call to certify")
                    if exa_key:
                        rep.add(
                            "spawn.live_search", FAIL,
                            "no run started — no search evidence (same "
                            "cause as spawn.live_prompt)")
                elif aborted:
                    rep.add(
                        "spawn.live_prompt", FAIL,
                        "run aborted before settling — key? router? (VM journal: "
                        "sudo virsh console servitor)")
                    if exa_key:
                        rep.add(
                            "spawn.live_search", FAIL,
                            "run aborted before settling — no search evidence "
                            "(same cause as spawn.live_prompt)")
                else:
                    msgs = await driver.get_messages()
                    answers = [
                        _msg_text(m)
                        for m in msgs
                        if m.get("role") == "assistant" and _msg_text(m)
                    ]
                    echoed = any("SERVITOR-LIVE-OK" in a for a in answers)
                    rep.add(
                        "spawn.live_prompt",
                        PASS if echoed else FAIL,
                        f"live run settled in {time.monotonic() - t1:.1f}s "
                        "(VM -> edge -> router -> model end to end)"
                        if echoed else
                        "no SERVITOR-LIVE-OK echo "
                        f"(last answer: {(answers[-1][:120] if answers else 'none')}) — "
                        "hairpin NAT / router path / key")
                    if exa_key:
                        # Match the SEARCH tool by name, not just any
                        # successful toolResult: a file/bash success would
                        # otherwise certify egress. `web_search` is the
                        # extension's contract tool name — a rename shows
                        # up here as FAIL, which is the point.
                        ok = [
                            m for m in msgs
                            if m.get("role") == "toolResult"
                            and m.get("toolName") == "web_search"
                            and not m.get("isError") and _msg_text(m).strip()
                        ]
                        rep.add(
                            "spawn.live_search",
                            PASS if ok else FAIL,
                            "live search succeeded (public egress + extension, "
                            "same run as spawn.live_prompt)"
                            if ok else
                            "no successful web_search tool result — "
                            "public egress from the guest (host ufw route rule?), "
                            "the extension, or the tool name drifted off the "
                            "contract (web_search) — see the transcript")
            except asyncio.TimeoutError:
                rep.add(
                    "spawn.live_prompt", FAIL,
                    "live run did not settle within 300 s — the model call "
                    "is hanging (key? router?; VM journal: "
                    "sudo virsh console servitor)")
                if exa_key:
                    rep.add(
                        "spawn.live_search", FAIL,
                        "live run did not settle within 300 s — no search "
                        "evidence (same cause as spawn.live_prompt)")
            except Exception as e:
                rep.add(
                    "spawn.live_prompt", FAIL,
                    f"live run failed: {type(e).__name__}: {e}")
                if exa_key:
                    rep.add(
                        "spawn.live_search", FAIL,
                        f"live run failed: {type(e).__name__}: {e}")
    except Exception as e:
        rep.add("spawn.no_prompt", FAIL, f"no-prompt spawn failed: {type(e).__name__}: {e}")
    finally:
        if sid is not None:
            try:
                await lc.release(sid, reason="doctor probe")
            except Exception:
                pass
        registry.close()


def check_update_waiting(rep: Report, args: argparse.Namespace) -> None:
    from app.update_pending import load

    flag = load(args.update_pending_path) if args.update_pending_path else None
    if flag is None:
        return
    detail = f"staged target pending: restart={flag.restart} doctor={flag.doctor} " \
        f"reason={flag.reason}"
    rep.add("update.waiting", WARN, detail + " (certifies the RUNNING state; full green after the deferred restart)")


# ---------------------------------------------------------------------- #


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="servitorctl doctor")
    p.add_argument("--state", required=True)
    p.add_argument("--config-src", default=None,
                   help="the active config dir (default: the active pointer)")
    p.add_argument("--registry", default=None)
    _dep = deployment_env()
    p.add_argument("--pi-node", default=_dep["SERVITOR_PI_NODE_BIN"] or "/usr/sbin/node")
    p.add_argument("--pi-cli", default=_dep["SERVITOR_PI_CLI_JS"])
    p.add_argument("--pi-ext", default=_dep["SERVITOR_PI_EXT_DIR"])
    p.add_argument("--toolkit", default=_dep["SERVITOR_TOOLKIT_DIR"] or "/opt/toolkit")
    p.add_argument("--slots", default=",".join(f"slot{i}" for i in range(1, 17)))
    p.add_argument("--probe-slot", default=None,
                   help="slot for the no-prompt spawn probe (default: the image's "
                        "dedicated slot0 — outside --slots, which the app's pool owns)")
    p.add_argument("--api-key-env", default="OMNISSIAH_API_KEY")
    p.add_argument("--exa-key-env", default="EXA_API_KEY")
    p.add_argument("--baseline-ext", action="append", default=[])
    p.add_argument("--update-pending-path", default=None,
                   help="default: <state>/update-pending.json")
    p.add_argument("--skip-spawn", action="store_true",
                   help="skip the no-prompt spawn check (degraded modes)")
    args = p.parse_args(argv)
    args.slots = [s.strip() for s in args.slots.split(",") if s.strip()]
    from app.iso.layout import PROBE_SLOT

    if not args.probe_slot:
        args.probe_slot = PROBE_SLOT
    # default update-pending path: derived from state (env-only paths)
    if not args.update_pending_path:
        args.update_pending_path = str(Path(args.state) / "update-pending.json")
    # default config-src: the active pointer under <state>/config
    if not args.config_src:
        from app.iso.configswap import active_dir

        args.config_src = active_dir(f"{args.state.rstrip('/')}/config")
    return args


async def main(argv: list[str]) -> int:
    args = parse_args(argv)
    rep = Report()
    print(f"DOCTOR servitor — {socket.gethostname()} {time.strftime('%Y-%m-%d %H:%M:%S')}")
    check_env(rep, args)
    check_state_fs(rep, args.state)
    check_state_mount(rep, args.state)
    check_ancestors(rep, args.state, args.toolkit)
    check_config_version(rep, f"{args.state.rstrip('/')}/config")
    check_slots(rep, args.slots + [args.probe_slot])
    from app.registry import Registry

    registry = Registry(args.registry or f"{args.state}/registry.db")
    check_versions(rep, args, registry)
    check_firewall_rules(rep)
    check_ui(rep)
    check_disk(rep, args.state)
    check_keys_absent(rep, args, args.state)
    check_update_waiting(rep, args)
    if args.skip_spawn:
        rep.add("spawn.no_prompt", SKIP, "--skip-spawn")
    else:
        try:
            async with _probe_lock(args.state):
                await check_no_prompt_spawn(rep, args)
        except ProbeLockTimeout:
            rep.add(
                "spawn.no_prompt",
                FAIL,
                "probe lock held by another doctor > 240 s — the spawn check "
                "did not run (doctors serialize on the probe uid; the "
                "concurrent doctor's live run is probably still in progress — "
                "re-run the doctor once it finishes)",
            )
    print(f"RESULT: {'FAIL' if rep.fails else 'GREEN'} ({rep.fails} fail, {rep.warns} warn)")
    return 1 if rep.fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
