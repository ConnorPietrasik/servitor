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
(no-prompt spawn, then one live run) spawn a standalone probe (its own
scratch tree, no registry row, no lifecycle) and tear it down by path:
the live run certifies a REAL model call leaving the VM (VM -> edge ->
router -> model) and, with EXA_API_KEY, public egress + the search
extension (one prompt, both jobs). LLM-key-gated; the search leg is
EXA-key-gated.

Exit: 0 = green (warnings allowed), 1 = at least one FAIL.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid

from app.env import deployment_env, optional_env
from app.iso import mounts as M
from app.iso.layout import STATE_AREAS, SessionMountPaths, StatePaths, state_paths
from app.registry import LIVE
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


_HEX2 = re.compile(r"^[0-9a-f]{2}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def check_state_fs(rep: Report, root: str) -> None:
    """The state volume must not be overlayfs: the per-session scratch
    overlays use it for upperdir/workdir, and overlayfs refuses an
    upper on overlayfs. The fs TYPE otherwise does not matter (ext4 is
    what the install creates, but xfs/btrfs are equally fine) — so this
    check FAILs on overlay, WARNs on volatile tmpfs (state lost on
    reboot) and on an untested type, and passes nothing else."""
    code, out = _sh(["stat", "-f", "-c", "%T", root])
    fstype = out.strip().splitlines()[0] if code == 0 and out.strip() else "?"
    # stat(1) names the SUPERBLOCK MAGIC, and ext2/ext3/ext4 share one: an
    # ext4 state volume reports "ext2/ext3". That is not a wrong filesystem
    # and not a wrong check (the magic is what decides overlay-safety); the
    # name is only what the operator reads, so when the root IS a mount ask
    # findmnt for the real type. The unmounted dev fallback has no findmnt row
    # and keeps stat's name.
    label = fstype
    if fstype == "ext2/ext3":
        code, out = _sh(["findmnt", "-no", "FSTYPE", "--mountpoint", root])
        label = out.strip().splitlines()[0] if code == 0 and out.strip() else fstype
    if fstype in ("overlay", "overlayfs"):
        rep.add(
            "state.fs", FAIL,
            f"{root} is overlayfs — the per-session scratch overlays cannot "
            "use it as upperdir/workdir (attach the state disk)",
        )
    elif fstype in ("ext2/ext3", "ext4", "xfs", "btrfs"):
        rep.add("state.fs", PASS, f"{root} is {label} (local, overlay-safe)")
    elif fstype == "tmpfs":
        rep.add(
            "state.fs", WARN,
            f"{root} is tmpfs (RAM-backed, volatile) — all state is lost on "
            "reboot; fine for throwaway dev, never for the real state volume",
        )
    else:
        rep.add(
            "state.fs", WARN,
            f"{root} is {fstype} — untested backing fs for the session "
            "overlays (ext4/xfs/btrfs are the known-good set)",
        )


def check_state_mount(rep: Report, root: str) -> None:
    # The state volume contract is a separate LOCAL DISK. The fstype check
    # alone cannot catch a MISSING disk: the unmounted fallback dir is on
    # the 24G root disk, which is also ext4. findmnt --mountpoint (exact
    # match), NOT -T. WARN, not FAIL: unprivileged dev uses a plain dir by
    # design; on the guest it signals INSTALL.md step 5 was skipped.
    code, out = _sh(["findmnt", "--mountpoint", root, "-n", "-o", "SOURCE"])
    src = out.strip().splitlines()[0] if code == 0 and out.strip() else ""
    if src:
        rep.add("state.mount", PASS, f"state root is a separate mount ({src})")
    else:
        rep.add(
            "state.mount",
            WARN,
            "state root is NOT a separate mount point — unprivileged dev is fine with "
            "this; on the guest it means the state disk is missing and all state "
            "silently lives on the 24G root disk (create + attach it, reboot)",
        )


def check_machine_id(rep: Report, path: str = "/etc/machine-id") -> None:
    # logind refuses to start without a valid id, and systemd-firstboot
    # prompts (hanging a boot nobody watches) on an empty one. The image
    # build writes the id directly — the deployed system is certified the
    # same way the build gate certifies the image: 32 lowercase hex chars,
    # nothing else. No dev-mode shape is tolerated: every booted Linux box
    # carries a valid machine-id, so a red here is a broken guest, full stop.
    try:
        v = Path(path).read_text().strip()
    except OSError as e:
        rep.add("os.machine_id", FAIL, f"machine-id unreadable ({e})")
        return
    if re.fullmatch(r"[0-9a-f]{32}", v):
        rep.add("os.machine_id", PASS, "valid 128-bit id")
    else:
        rep.add(
            "os.machine_id", FAIL,
            f"machine-id is not a valid id ({v!r}) — logind cannot start and "
            "firstboot would prompt; repair: write 32 random hex chars, reboot",
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
    # no root-owned files inside slot-owned trees
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


def check_quarantined_slots(rep: Report, args: argparse.Namespace, registry) -> None:
    """Quarantine is fail-closed by design: a slot whose release sweep
    failed is never handed out again until a human clears the blocker and
    re-runs release — each one shrinks the pool's capacity. This check is
    the GATE-level view: a stuck release is a DRAINING/CLEANING row on a
    pool slot (a successful release flips to FREE within seconds; a
    release interrupted by an app crash keeps its row across restarts,
    so the shrink persists until the retry). WARN, not FAIL: the system
    works with fewer slots and the deploy is not at fault — but it must
    be visible in the gate report, not only in the panel. (A release in
    flight during the doctor's read looks identical for a few seconds; a
    spurious WARN then clears on the next run.)"""
    from app.registry import CLEANING, DRAINING

    stuck = []
    for name in args.slots:
        row = registry.session_for_slot(name)
        if row is not None and row.state in (DRAINING, CLEANING):
            stuck.append(f"{name} (row {row.id[:8]}, state {row.state})")
    if stuck:
        rep.add(
            "slots.quarantined",
            WARN,
            f"stuck release(s) — quarantined slot(s), pool shrunk: {stuck} — "
            "clear the blocker and re-run release for the session",
        )
    else:
        rep.add("slots.quarantined", PASS, "no quarantined slots")


def check_off_pool_rows(rep: Report, args: argparse.Namespace, registry) -> None:
    """The pool's keyset is the --slots flag; the registry's rows are what
    survived. Shrink or rename --slots and an old ACTIVE row can point at
    a slot name the pool no longer holds: every release path resolves the
    row's slot through the pool (lifecycle._release), so the row is
    UNRELEASEABLE — idle GC skips it, and the operator's POST /release /
    panel release 409s. This check is the registry-side view of
    check_quarantined_slots (which walks pool -> rows; this one walks
    rows -> pool). WARN, not FAIL: the app runs fine without the row
    (nothing respawns it, the process is gone) — but it is stuck until an
    operator restores the slot name or edits the registry, and it must be
    visible in the gate report, not only in the release 409."""
    pool_names = set(args.slots)
    stuck = [
        f"{row.slot} (row {row.id[:8]}, state {row.state})"
        for row in registry.list_sessions()
        if row.is_running_or_awaiting_release() and row.slot not in pool_names
    ]
    if stuck:
        rep.add(
            "slots.off_pool_rows",
            WARN,
            f"row(s) on slot(s) outside --slots: {stuck} — unreleaseable "
            "via API/panel (release is refused); restore the slot name(s) "
            "in --slots or hand-edit the registry",
        )
    else:
        rep.add("slots.off_pool_rows", PASS, "no rows on slots outside --slots")


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
    healthy — only this check sees it. The app key-gates every route
    (EdgeKeyMiddleware in app/api/app.py) while SERVITOR_EDGE_KEY is set
    — production always: this doctor runs with the guest env file
    sourced, so the probe presents the same value the app verifies;
    keyless (dev/standalone) the gate is off and a bare GET is right. A
    403 here means the guest env's key drifted from what the app holds —
    deploy/sync.sh re-injects it."""
    bind = optional_env("SERVITOR_BIND") or ""
    port = optional_env("SERVITOR_PORT") or ""
    if not bind or not port:
        rep.add("ui.index", FAIL,
                "SERVITOR_BIND/SERVITOR_PORT unset — cannot probe the UI")
        return
    import urllib.request

    url = f"http://{bind}:{port}/"
    req = urllib.request.Request(url)
    edge_key = optional_env("SERVITOR_EDGE_KEY")
    if edge_key:
        req.add_header("X-Servitor-Edge-Key", edge_key)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
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
    # every state area, so an operator sees WHERE the volume went; du counts a
    # hardlinked blob once per tree it appears in, so blobs (where the bytes
    # live) plus near-zero links elsewhere is the expected shape, not a bug
    sizes = {}
    for sub in STATE_AREAS:
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


def check_delivery(rep: Report, root: str) -> None:
    """The blob store, the kept shelf, and the durability of each.

    Three facts only the filesystem can answer:

    - a blob's NAME is its content address: a blob sitting in the wrong bucket
      or named for something other than its own sha256 can never be found
      again (an upload that "vanished" while its bytes sit unreclaimable);
    - a blob nothing links to is reclaimable, and a pile of them that keeps
      growing past the GC's grace window means the reclaim tick is not running;
    - ``keep/`` is the one area with no expiry and no session attached, so it
      must live on the state volume itself — a keep on the overlay or on a
      tmpfs is a keep that disappears on the next boot.
    """
    state = state_paths(root)
    misfiled: list[str] = []
    blob_bytes = files = orphan_bytes = 0
    stale_orphans = 0
    now = time.time()
    from app.lifecycle import BLOB_GRACE_SEC  # the same grace the reclaim uses

    try:
        prefixes = sorted(os.listdir(state.blobs))
    except OSError:
        prefixes = []
    for prefix in prefixes:
        # the in-flight/crash temp shape of ``adopt_hashed`` (``.adopt-*``
        # under the blobs root, not in a bucket): a live keep is copying a
        # multi-GB artifact right now, or the last process died mid-copy —
        # the boot sweep takes it. It is not a blob, so it is not a misfile.
        if prefix.startswith(".adopt-"):
            continue
        if not _HEX2.fullmatch(prefix):
            misfiled.append(f"blobs/{prefix} (bucket is not 2 hex chars)")
            continue
        pdir = os.path.join(state.blobs, prefix)
        if not os.path.isdir(pdir):
            misfiled.append(f"blobs/{prefix} (not a directory)")
            continue
        for name in os.listdir(pdir):
            files += 1
            if not _HEX64.fullmatch(name) or not name.startswith(prefix):
                misfiled.append(f"blobs/{prefix}/{name}")
                continue
            try:
                st = os.lstat(os.path.join(pdir, name))
            except OSError:
                continue
            blob_bytes += st.st_size
            if st.st_nlink <= 1:
                orphan_bytes += st.st_size
                # young and unlinked is normal (just adopted, or just released:
                # the grace window is what keeps a race from deleting bytes a
                # keep is about to link). Old and unlinked means the reclaim
                # tick has not run — the service is down, or it is broken.
                # The clock is the GC's own (max of mtime and ctime): link
                # churn bumps ctime without touching mtime, and the GC holds
                # such a blob — blaming the tick on mtime alone would.
                if now - max(st.st_mtime, st.st_ctime) > BLOB_GRACE_SEC * 4:
                    stale_orphans += 1
    if misfiled:
        rep.add(
            "delivery.blobs", FAIL,
            "blob store entries that are not their own address (unfindable, "
            f"unreclaimable): {', '.join(misfiled[:5])}"
            + (f" (+{len(misfiled) - 5} more)" if len(misfiled) > 5 else ""),
        )
    else:
        detail = f"{files} blob(s), {blob_bytes // (1024 * 1024)}MB; "
        if orphan_bytes:
            detail += f"{orphan_bytes // (1024 * 1024)}MB unlinked"
            if stale_orphans:
                detail += (
                    f", {stale_orphans} past the grace window — the reclaim tick is not "
                    "running (is the service up?)"
                )
            else:
                detail += " inside the grace window, reclaimable at the next GC tick"
        else:
            detail += "every blob linked by a session or a keep"
        rep.add("delivery.blobs", PASS, detail)

    # the kept shelf: durable, and on the same device as the state root
    try:
        keep_dev = os.stat(state.keep).st_dev
        root_dev = os.stat(root).st_dev
    except FileNotFoundError:
        rep.add(
            "delivery.keep", WARN,
            f"{state.keep} does not exist yet — the service creates it at boot; on a "
            "volume that has run, this means the state areas are not where they should be",
        )
        return
    except OSError as e:
        rep.add("delivery.keep", FAIL, f"{state.keep} unreadable: {e}")
        return
    if not os.path.isdir(state.keep):
        rep.add(
            "delivery.keep", FAIL,
            f"{state.keep} is not a directory — the shelf cannot hold anything "
            "(restore the area; kept files are the one thing with no other copy here)",
        )
        return
    if keep_dev != root_dev:
        rep.add(
            "delivery.keep", FAIL,
            f"{state.keep} is on a different device than {root} — a kept file is the "
            "one thing that must outlive everything, and this one lives on whatever "
            "is mounted there instead (fix the mount, do not move the files by hand)",
        )
        return
    kept = kbytes = 0
    for dirpath, _dirs, names in os.walk(state.keep):
        for name in names:
            try:
                kept += 1
                kbytes += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
    rep.add(
        "delivery.keep", PASS,
        f"{kept} kept file(s), {kbytes // (1024 * 1024)}MB on the state volume"
        " (no expiry; deleted only by an explicit delete)",
    )


def check_outbox_mounts(rep: Report, root: str, registry) -> None:
    """Every LIVE session's workdir must have its outbox bind up.

    The outbox is the agent's handoff directory, and it is a bind of
    ``<state>/outbox/<sid>`` into the workdir overlay. If the overlay is
    mounted and the bind is not, the agent is writing into the overlay upper:
    the app never sees the artifacts (the listing reads the bind source), and
    they die with the scratch instead of waiting for a keep. That is a broken
    mount plan, not a preference — so it FAILs when a session is mounted
    without it, and says nothing when the session is simply not mounted
    (unprivileged dev, or a session killed and released).
    """
    missing = []
    for row in registry.list_sessions():
        if row.state != LIVE or not row.slot:
            continue
        m = SessionMountPaths.for_session(row.id, f"{root.rstrip('/')}/scratch")
        if not os.path.isdir(m.workdir) or not os.path.ismount(m.workdir):
            continue  # not mounted at all: nothing to say
        if not os.path.ismount(f"{m.workdir}/outbox"):
            missing.append(row.id)
    if missing:
        rep.add(
            "mounts.outbox", FAIL,
            "live session(s) mounted WITHOUT the outbox bind — artifacts would "
            f"land in the overlay and be lost at release: {', '.join(missing[:5])}"
            + (f" (+{len(missing) - 5} more)" if len(missing) > 5 else ""),
        )
    else:
        rep.add("mounts.outbox", PASS, "every mounted session has its outbox bind")


def check_usage_meters(rep: Report, root: str) -> None:
    """What the app's own byte meters say (the numbers the UI shows).

    A second walk after ``disk.headroom``'s ``du`` is worth it: ``du`` counts a
    hardlinked blob once per tree it appears in, and these meters count it once
    in the store. The difference IS the dedup, and an operator comparing the
    two lines is exactly the person who should see both.
    """
    from app.delivery.usage import UsageMeter

    snap = UsageMeter(root).report()
    if snap.get("error"):
        rep.add("delivery.meters", FAIL, f"state volume unreadable: {snap['error']}")
        return
    areas = snap["areas"]
    total = sum(a["bytes"] for a in areas.values())
    files = sum(a["files"] for a in areas.values())
    busiest = ", ".join(
        f"{name}={areas[name]['bytes'] // (1024 * 1024)}MB/{areas[name]['files']}f"
        for name in sorted(areas, key=lambda n: -areas[n]["bytes"])
        if areas[name]["files"]
    ) or "empty"
    rep.add(
        "delivery.meters", PASS,
        f"{files} file(s), {total // (1024 * 1024)}MB counted once, "
        f"{snap['shared_bytes'] // (1024 * 1024)}MB saved by hardlink; {busiest}",
    )


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
    # the ( ) grouping is load-bearing: unparenthesized, find parses the
    # -print as part of the models-store branch only, and a stored
    # auth.json outside the scratch uppers passes silently
    code, out = _sh(["find", root, "(", "-name", "auth.json", "-o", "-name", "models-store.json", ")", "-print"])
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
# the no-prompt spawn check (stateful: spawns + cleans one probe)        #
# ---------------------------------------------------------------------- #

#: the probe's per-run session-id prefix; its scratch/inbox/sessions
#: trees are reaped by this prefix (the probe keeps no registry row)
PROBE_SID_PREFIX = "doctor-probe-"

#: before a probe child/tree is an orphan of a DEAD doctor. A live probe
#: finishes far inside this (spawn ~2 s + a 300 s settle cap; the tick
#: doctor's whole subprocess budget is 600 s) — a concurrent live probe
#: can never be reaped.
PROBE_ORPHAN_GRACE_SEC = 600.0


def _uptime_sec() -> float:
    with open("/proc/uptime", "r", encoding="ascii") as f:
        return float(f.read().split()[0])


def _proc_age_sec(pid: int) -> float | None:
    """A process's age in seconds (None if unreadable — treat as young)."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            raw = f.read().decode("ascii", "replace")
        # field 22 (starttime, clock ticks since boot); the comm field
        # (field 2) is parenthesized and may contain spaces/parens
        starttime = int(raw.rsplit(")", 1)[1].split()[19])
        hz = os.sysconf("SC_CLK_TCK")
        return (_uptime_sec() * hz - starttime) / hz
    except (OSError, ValueError, IndexError, OverflowError):
        return None


def _proc_cwd(pid: int) -> str | None:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


def _live_pids() -> list[int]:
    return [int(e) for e in os.listdir("/proc") if e.isdigit()]


async def _kill_pid(pid: int, *, term_grace: float = 2.0) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + term_grace
    while time.monotonic() < deadline and os.path.exists(f"/proc/{pid}"):
        await asyncio.sleep(0.05)
    if os.path.exists(f"/proc/{pid}"):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


async def reap_stale_probes(state: StatePaths, probe_uid: int) -> list[tuple[str, str]]:
    """Reap the debris of a previous doctor that died mid-probe.

    PATH-SCOPED (the probe keeps no registry row): (1) kill probe-uid
    children older than the grace — a dead doctor's child normally exits
    on stdin EOF on its own; this catches the wedged case, and a live
    concurrent probe's child is always younger than the grace; (2)
    unmount + delete stale per-probe trees — older than the grace AND
    holding no live process (belt: a concurrent probe's tree is never
    touched). Returns (kind, detail) report items (kind: "tree" | "kill").
    """
    from app.iso.sweep import _pids_of_uid

    items: list[tuple[str, str]] = []
    killed = [
        pid
        for pid in _pids_of_uid(probe_uid)
        if (age := _proc_age_sec(pid)) is not None and age > PROBE_ORPHAN_GRACE_SEC
    ]
    for pid in killed:
        await _kill_pid(pid)
    if killed:
        items.append(("kill", f"wedged probe orphan(s) killed (older than grace): {killed}"))
    scratch_root = Path(state.scratch)
    now = time.time()  # wall clock: st_mtime is an epoch timestamp
    for d in sorted(scratch_root.glob(f"{PROBE_SID_PREFIX}*")):
        if not d.is_dir():
            continue
        try:
            age = now - d.stat().st_mtime
        except OSError:
            continue
        if age < PROBE_ORPHAN_GRACE_SEC:
            continue
        # never touch a tree a live process sits in (a concurrent probe;
        # cwd == the tree or anything under it)
        dstr = f"{d}"
        if any(
            (cwd := _proc_cwd(p)) is not None
            and (cwd == dstr or cwd.startswith(dstr + os.sep))
            for p in _live_pids()
        ):
            continue
        sp = SessionMountPaths.for_session(d.name, str(scratch_root))
        mounted = M.mounted_targets()
        for target in (sp.workdir, sp.agent_dir):
            if target in mounted:
                subprocess.run(["umount", "-l", target], capture_output=True, timeout=10)
        for area in (
            str(d),
            state.inbox_for(d.name),
            state.outbox_for(d.name),
            state.sessions_for(d.name),
        ):
            if os.path.lexists(area):
                shutil.rmtree(area, ignore_errors=True)
        items.append(("tree", f"stale probe tree {d.name} removed (previous doctor died mid-probe)"))
    return items


async def _probe_cleanup(
    driver: "PiSession | None",
    sp: SessionMountPaths,
    ops: list[M.MountOp],
    state: StatePaths,
) -> list[str]:
    """PID/path-scoped probe teardown: ladder the probe's OWN child,
    unmount its own overlays (lazy fallback), delete its own trees. No
    uid sweep, no registry — every path is this run's (unique per run),
    so concurrent probes can never tear each other down."""
    errors: list[str] = []
    if driver is not None:
        try:
            await driver.shutdown()
        except Exception as e:
            errors.append(f"probe child shutdown: {e}")
        try:
            await driver.close()
        except Exception:
            pass
    if ops:
        unmounted: list[str] = []
        try:
            unmounted = await M.aunmount_all(ops)
        except M.MountError as e:
            errors.append(f"probe unmount: {e}")
        for op in reversed(ops):
            if op.target not in unmounted:
                try:
                    subprocess.run(["umount", "-l", op.target], capture_output=True, timeout=10)
                    unmounted.append(op.target)
                except Exception:
                    pass
        still = [op.target for op in ops if op.target in M.mounted_targets()]
        if still:
            errors.append(f"probe mounts still up: {still}")
    for area in (
        sp.scratch,
        state.inbox_for(sp.session_id),
        state.outbox_for(sp.session_id),
        state.sessions_for(sp.session_id),
    ):
        if os.path.lexists(area):
            try:
                shutil.rmtree(area)
            except OSError as e:
                errors.append(f"probe rmtree {area}: {e}")
    return errors


async def check_no_prompt_spawn(rep: Report, args: argparse.Namespace) -> None:
    """The no-prompt spawn probe — STANDALONE by design: it builds the
    launch line, mounts the session and spawns setuid directly, with no
    Lifecycle and no registry row. Consequences:

    - teardown is PID/path-scoped (ladder the probe's own child, unmount
      its own overlays, rmtree its own trees) — no uid sweep, so
      concurrent doctors cannot sweep each other's files: no lock;
    - the probe uid must still be OUTSIDE the app's pool (an app session
      on the same uid would hit the probe through the app's uid-gated
      kills and owner sweep);
    - a previous doctor that died mid-probe leaves only paths (no row):
      reap_stale_probes reaps them, age- and cwd-gated, no state machine.
    """
    from app.iso.configswap import require_active_dir
    from app.iso.spawn import slot_identity
    from app.lifecycle import extension_names
    from app.rpc.launch import build_launch
    from app.rpc.session import PiSession

    import pwd as _pwd

    # the probe slot is outside the app's pool by contract: a probe on an
    # app slot would race the service's uid-gated kills (boot reconciliation
    # after a tick restart SIGTERMs every process of a slot uid that owns a
    # live row — the probe's own row) and its owner sweep would delete the
    # probe's files
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
    # stale probe debris from a previous doctor that died mid-probe: ONLY
    # the doctor can clean it (the app's pool does not include the probe
    # slot). Path-scoped: no registry row to find, no sweep to re-run.
    try:
        for kind, detail in await reap_stale_probes(state, _pw.pw_uid):
            rep.add("spawn.stale_probe", PASS if kind == "tree" else WARN, detail)
    except Exception as e:
        rep.add("spawn.stale_probe", WARN, f"stale probe reap failed: {e}")

    ident = slot_identity(args.probe_slot)
    api_key = os.environ.get(args.api_key_env, "")
    exa_key = os.environ.get(args.exa_key_env) or None
    baseline = tuple(args.baseline_ext or ([Path(args.pi_ext).name] if args.pi_ext else []))

    # unique per run: the tree is reaped by prefix, and concurrent probes
    # never share a path (or a HOME — the per-probe home below)
    probe_sid = f"{PROBE_SID_PREFIX}{uuid.uuid4().hex[:12]}"
    sp = SessionMountPaths.for_session(probe_sid, state.scratch)
    ops: list[M.MountOp] = []
    driver: PiSession | None = None
    t0 = time.monotonic()
    try:
        # the spawn uses the ACTIVE config pointer (require_active_dir);
        # check_config_version already certifies it exists + parses
        lower = require_active_dir(state.config)
        await asyncio.to_thread(M.ensure_session_dirs, sp, state)
        # per-probe HOME inside the probe's own scratch tree: teardown is
        # then purely path-scoped and concurrent probes cannot interleave
        # in a shared home. The passwd home stays on disk (0700 —
        # check_slots); pi's state lives in the agent dir, not HOME.
        home = f"{sp.scratch}/home"
        os.makedirs(home, exist_ok=True)
        os.chown(home, ident.uid, ident.gid)
        ops = await asyncio.to_thread(
            M.mount_session, sp, lower, state, ident.uid, ident.gid, args.toolkit
        )
        launch = build_launch(
            node_bin=args.pi_node,
            cli_js=args.pi_cli,
            session_dir=state.sessions_for(probe_sid),
            session_id=probe_sid,
            extension_paths=(args.pi_ext,) if args.pi_ext else (),
            home=home,
            user=args.probe_slot,
            agent_dir=sp.agent_dir,
            omnissiah_api_key=api_key,
            exa_api_key=exa_key,
            web_cache_root=sp.web_cache,
        )
        driver = PiSession(launch, ident, cwd=sp.workdir)
        await driver.start()
        await driver.get_state()  # the protocol is up (and bounds the timing)
        spawn_sec = time.monotonic() - t0
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
        cand = Path(sp.agent_dir) / "models.json"
        agent_models = cand if cand.exists() else None
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
        loaded = extension_names(sp, args.pi_ext)
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
        errors = await _probe_cleanup(driver, sp, ops, state)
        if errors:
            rep.add("spawn.cleanup", WARN, "; ".join(errors))


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
    check_machine_id(rep)
    check_ancestors(rep, args.state, args.toolkit)
    check_config_version(rep, f"{args.state.rstrip('/')}/config")
    check_slots(rep, args.slots + [args.probe_slot])
    from app.registry import Registry

    registry = Registry(args.registry or f"{args.state}/registry.db")
    check_versions(rep, args, registry)
    check_quarantined_slots(rep, args, registry)
    check_off_pool_rows(rep, args, registry)
    check_firewall_rules(rep)
    check_ui(rep)
    check_disk(rep, args.state)
    check_delivery(rep, args.state)
    check_outbox_mounts(rep, args.state, registry)
    check_usage_meters(rep, args.state)
    check_keys_absent(rep, args, args.state)
    check_update_waiting(rep, args)
    if args.skip_spawn:
        rep.add("spawn.no_prompt", SKIP, "--skip-spawn")
    else:
        await check_no_prompt_spawn(rep, args)
    print(f"RESULT: {'FAIL' if rep.fails else 'GREEN'} ({rep.fails} fail, {rep.warns} warn)")
    return 1 if rep.fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
