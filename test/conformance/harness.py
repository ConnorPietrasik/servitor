"""Session harness — wires iso + launch + driver + mock router for tests.

One instance per test: a scratch state tree (on a local fs, never overlay), a
staged config version, a toolkit dir, one slot, the mock router, and
session start/respawn/release. Mirrors what the app does, minus
the registry (the harness plays it).

Root is required (mounts, setuid, sweep). Tests that do not need root
(unit framing) do not use the harness.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from app.iso import configswap, mounts as M
from app.iso.layout import STATE_AREAS, SessionMountPaths, state_paths
from app.iso.release import release_session
from app.iso.slots import SlotPool
from app.iso.spawn import SlotIdentity, slot_identity
from app.rpc.launch import build_launch
from app.rpc.session import PiSession

from test.mock_router.server import MockRouter

REPO = Path(__file__).resolve().parent.parent.parent
AGENT_CONFIG = REPO / "agent-config"


class HarnessError(RuntimeError):
    pass


@dataclass
class LiveSession:
    session_id: str
    slot: "object"
    sp: SessionMountPaths
    driver: PiSession
    mount_ops: list[M.MountOp]
    config_lower: str
    events: list = field(default_factory=list)
    dialogs: list = field(default_factory=list)
    strict: bool = False  # the launch-line mode is create-time: respawn carries it


@dataclass
class Harness:
    root: Path  # scratch state tree (per-run, under the slot's test home)
    pi: object  # PiInstall
    slot_name: str
    config_key: str = "mock-key-0001112223"
    exa_key: str = "mock-exa-key"
    router: MockRouter = field(default_factory=MockRouter)
    pool: SlotPool = field(default=None)  # type: ignore[assignment]
    state: object = None  # type: ignore[assignment]
    sessions: dict = field(default_factory=dict)  # id -> LiveSession
    released: dict = field(default_factory=dict)  # id -> ReleaseOutcome
    mount_ops_by_session: dict = field(default_factory=dict)  # id -> ops (persist across idle-kill; only release unmounts)
    _toolkit: Path | None = None

    def __post_init__(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o755)  # slot uid must traverse (mkdtemp gives 0700)
        self.state = state_paths(str(self.root))
        for area in STATE_AREAS:
            (Path(self.state.root) / area).mkdir(parents=True, exist_ok=True)
        # the fake toolkit: what /opt/toolkit would be on the guest
        self._toolkit = self.root / "toolkit"
        self._toolkit.mkdir(exist_ok=True)
        (self._toolkit / "toolkit-marker.txt").write_text("baked toolkit\n")
        # a git repo would be an ANCESTOR here — it must not be (doctor
        # asserts the session tree root's ancestor chain is context-file
        # free). Assert the harness root itself is clean too.
        self.pool = SlotPool((self.slot_name,))
        self.router.start()

    # ------------------------------------------------------------------ #

    @property
    def toolkit_dir(self) -> str:
        assert self._toolkit is not None
        return str(self._toolkit)

    def stage_config(self, source: Path | None = None) -> str:
        """Stage a new config version from agent-config/ (or a custom dir);
        return the version name. Mirrors deploy/sync.sh's swap."""
        src = source or AGENT_CONFIG
        # the synced config carries the router URL; for tests the models.json
        # baseUrl is rewritten to the mock router (the literal-URL rule is
        # tested on the real file in the contract gate).
        stage = self.root / f"cfgsrc-{configswap.next_version(str(Path(self.state.config)))}"
        shutil.copytree(src, stage)
        import json

        models_path = stage / "models.json"
        models = json.loads(models_path.read_text())
        for prov in models.get("providers", {}).values():
            prov["baseUrl"] = self.router.base_url
        models_path.write_text(json.dumps(models, indent=2))
        version = configswap.stage_and_swap(str(self.state.config), str(stage))
        return version

    def launch_args(self, *, session_id: str, session_file: str | None = None, strict: bool = False):
        """The exact launch for a session. ``session_file`` set = respawn/
        probe (the recorded path, verbatim); else first spawn
        (``--session-id``). The session dir is IDENTICAL across
        spawn/respawn/probe: ``sessions/<s>/`` — with an explicit session
        dir, transcripts land flat inside it. ``strict`` pins the
        read-only launch line (no extension, the replacement prompt from
        the session's mounted config lower — the mount pins the version
        the session runs and outlives the process and the app — the active
        pointer only when unmounted, the app's resolution order)."""
        assert self.state is not None
        sp = SessionMountPaths.for_session(session_id, str(self.state.scratch))
        strict_system_path = None
        if strict:
            lower = (
                M.overlay_lower_dir(sp.agent_dir)
                or configswap.active_dir(str(self.state.config))
            )
            assert lower is not None, "no active config version"
            strict_system_path = f"{lower}/STRICT_SYSTEM.md"
        return build_launch(
            node_bin=self.pi.node_bin,
            cli_js=str(self.pi.cli_js),
            session_dir=f"{self.state.sessions}/{session_id}",
            session_id=None if session_file else session_id,
            session_file=session_file,
            extension_paths=() if strict else (str(self.pi.ext_path),),
            home=f"/home/{self.slot_name}",
            user=self.slot_name,
            agent_dir=sp.agent_dir,
            omnissiah_api_key=self.config_key,
            exa_api_key=None if strict else self.exa_key,
            web_cache_root=sp.web_cache,
            strict=strict,
            strict_system_path=strict_system_path,
        )

    async def start_session(
        self,
        session_id: str | None = None,
        *,
        respawn_file: str | None = None,
        key: str | None = None,  # None = the harness key; "" = unset (no key)
        extra_exts: tuple[str, ...] = (),
        expect_startup: bool = True,
        agent_dir_ro: bool = False,
        strict: bool = False,
    ) -> LiveSession:
        """Allocate the slot, mount, spawn, and (unless told otherwise)
        verify the protocol came up: get_state + model usable.

        ``expect_startup=False`` is for the failure tests (missing cwd,
        dead credential) — the SpawnFailure/ModelUnavailable propagates.
        """
        assert self.state is not None
        session_id = session_id or uuid.uuid4().hex
        slot = self.pool.allocate(session_id)
        slot_identity_obj = slot_identity(slot.name)
        sp = SessionMountPaths.for_session(session_id, str(self.state.scratch))
        lower = configswap.require_active_dir(str(self.state.config))
        # mount-before-spawn (always): mounts are up before ANY spawn.
        # On respawn the mounts SURVIVED the idle-kill (by design: pi runs a
        # session in its recorded cwd, so idle-kill never unmounts) — reuse.
        if sp.workdir in M.mounted_targets():
            ops = self.mount_ops_by_session[session_id]
        else:
            M.ensure_session_dirs(sp, self.state)
            ops = await asyncio.to_thread(
                M.mount_session, sp, lower, self.state, slot_identity_obj.uid, slot_identity_obj.gid, self.toolkit_dir, agent_dir_ro
            )
            self.mount_ops_by_session[session_id] = ops
        launch = self.launch_args(
            session_id=session_id, session_file=respawn_file, strict=strict
        )
        if key == "":
            # the credential is UNSET — removed from the env entirely
            # (never an empty string: an empty Bearer is a different, also
            # broken, case)
            launch = type(launch)(
                argv=launch.argv,
                env=tuple(e for e in launch.env if e[0] != "OMNISSIAH_API_KEY"),
            )
        elif key is not None:
            launch = type(launch)(
                argv=launch.argv,
                env=tuple(
                    (k, key if k == "OMNISSIAH_API_KEY" else v) for k, v in launch.env
                ),
            )
        if extra_exts:
            argv = list(launch.argv) + [e for ext in extra_exts for e in ("-e", ext)]
            launch = type(launch)(argv=tuple(argv), env=launch.env)

        events: list[dict] = []
        dialogs: list[dict] = []

        def on_event(rec: dict) -> None:
            events.append(rec)

        def on_dialog(drv: PiSession, rec: dict) -> None:
            dialogs.append(rec)

        driver = PiSession(
            launch,
            slot_identity_obj,
            cwd=sp.workdir,
            on_event=on_event,
            on_dialog=on_dialog,
        )
        try:
            await driver.start()
            if expect_startup:
                await driver.assert_model_usable()
        except BaseException:
            # never leak a mounted/spawned session on startup failure
            try:
                await driver.shutdown(eof_grace=2, term_grace=2)
                await driver.close()
            except Exception:
                pass
            if sp.workdir not in M.mounted_targets():
                await asyncio.to_thread(M.aunmount_all_safe, ops)
                self.mount_ops_by_session.pop(session_id, None)
            self.pool.mark_cleaning(session_id)
            self.pool.verify_free(session_id)
            raise
        live = LiveSession(
            session_id=session_id,
            slot=slot,
            sp=sp,
            driver=driver,
            mount_ops=ops,
            config_lower=lower,
            events=events,
            dialogs=dialogs,
            strict=strict,
        )
        self.sessions[session_id] = live
        return live

    async def respawn(self, live: LiveSession, *, expect_startup: bool = True) -> LiveSession:
        """Kill the live process (idle-kill semantics: never unmount) and
        respawn from the recorded sessionFile — identity asserts included."""
        recorded = await live.driver.get_state()
        session_file = recorded.get("sessionFile")
        session_id = recorded.get("sessionId")
        if not session_file or not session_id:
            raise HarnessError("get_state returned no sessionFile/sessionId")
        await live.driver.shutdown()
        await live.driver.close()
        return await self.start_session(
            session_id, respawn_file=session_file, expect_startup=expect_startup, strict=live.strict
        )

    async def prompt_and_settle(
        self, live: LiveSession, text: str, *, settle_timeout: float | None = 120.0
    ) -> str:
        """The race-free prompt pattern: settle waiter installed BEFORE the
        prompt (pi's own promptAndWait does this). Returns the disposition
        after settle (or immediately for 'handled')."""
        driver = live.driver
        disposition = "handled"
        if text.startswith("/"):
            fut = None
            disposition = await driver.prompt(text)
        else:
            fut = driver.settle_waiter()
            disposition = await driver.prompt(text)
            if disposition != "handled":
                await asyncio.wait_for(fut, settle_timeout)
        return disposition

    async def cleanup_failed(self, live: LiveSession) -> None:
        """Tear down a session whose driver died in a failure test (the
        release() path is for sessions; a dead-driver drill unmounts +
        frees the slot directly)."""
        await asyncio.to_thread(M.aunmount_all_safe, live.mount_ops)
        self.mount_ops_by_session.pop(live.session_id, None)
        if self.pool.by_session(live.session_id) is not None:
            self.pool.mark_cleaning(live.session_id)
            self.pool.verify_free(live.session_id)
        self.sessions.pop(live.session_id, None)

    async def release(self, live: LiveSession, *, reason: str = "test release"):

        hooks = _FakeHooks(self, live.session_id)
        outcome = await release_session(
            session_id=live.session_id,
            slot=live.slot,
            state=self.state,
            pool=self.pool,
            hooks=hooks,
            driver=live.driver,
            mount_ops=live.mount_ops,
            reason=reason,
        )
        try:
            await live.driver.close()
        except Exception:
            pass
        self.sessions.pop(live.session_id, None)
        self.mount_ops_by_session.pop(live.session_id, None)
        self.released[live.session_id] = outcome
        return outcome

    def stop(self) -> None:
        self.router.stop()
        # safety net: unmount ANYTHING this harness mounted — including
        # sessions a crashed test failed to release (a leaked overlay is
        # how the previous test's junk quarantines the next one's sweep)
        for live in list(self.sessions.values()):
            try:
                M.aunmount_all_safe(live.mount_ops)
            except Exception:
                pass
        for ops in list(self.mount_ops_by_session.values()):
            try:
                M.aunmount_all_safe(ops)
            except Exception:
                pass
        self.mount_ops_by_session.clear()
        self.sessions.clear()


class _FakeHooks:
    """Test stand-in for the registry seam.

    The harness plays the registry (module docstring): the pin is the
    versions the OTHER live sessions booted from — the mounted lowers
    release() unions in itself (the mount is the session's config truth).
    """

    def __init__(self, harness: "Harness", session_id: str):
        self._harness = harness
        self._store = harness.released
        self._session_id = session_id

    def mark_draining(self, session_id: str, reason: str) -> None:
        self._store.setdefault(session_id, {})["draining_reason"] = reason

    def mark_released(self, session_id: str, *, quarantined: bool, detail: str) -> None:
        self._store.setdefault(session_id, {})["released"] = True
        self._store[session_id]["quarantined"] = quarantined
        self._store[session_id]["detail"] = detail

    def pinned_config_versions(self) -> set[str]:
        return {
            os.path.basename(l.config_lower)
            for sid, l in self._harness.sessions.items()
            if sid != self._session_id and l.config_lower
        }


def require_root() -> None:
    if os.geteuid() != 0:
        raise HarnessError(
            "this test needs root (mounts/setuid/sweep). Run under the root"
            " make targets as root (sudo make <target>) — never silently"
            " skipped."
        )


def require_root_or_skip(what: str) -> None:
    """Loud-skip seam for fixtures (the suite names what it could not run;
    the root make targets on the server are always run as root)."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return
    import pytest

    pytest.skip(f"LOUD-SKIP: {what} requires root (run: sudo make <target>)")
