"""`interrupted` must not outlive the run that was interrupted.

mark_interrupted is only ever called with the default True (boot
reconcile, shutdown drain) and nothing else writes the column — so the
ONLY thing that can clear it is a run that settled CLEAN. An aborted
settle does not count: the shutdown drain aborts a live run and marks
the session interrupted, and the child's agent_settled {aborted: true}
lands on _on_settled right after the mark — clearing on it would race
the drain's mark and lose the flag. The Sessions list uses the flag as
its state chip when no process is live (ui/src/views/Sessions.tsx),
the only meter a human has for a parked session: if a clean settle does
not clear it, one aborted restart labels the session 'interrupted' for
the rest of its life, past respawns and clean runs."""
from __future__ import annotations

from types import SimpleNamespace

from app.lifecycle import Lifecycle
from app.registry import LIVE, Registry


def _lc(tmp_path) -> Lifecycle:
    """Lifecycle with __init__ skipped: real registry (sqlite), no
    drivers (a settle is a registry concern; live entries are stubs)."""
    lc = Lifecycle.__new__(Lifecycle)
    lc.cfg = SimpleNamespace(registry=Registry(str(tmp_path / "registry.db")))
    lc.live = {}
    return lc


def _add(lc: Lifecycle, session_id: str) -> None:
    lc.live[session_id] = SimpleNamespace(
        session_id=session_id, driver=None, last_settled=None)


def test_settled_run_clears_interrupted(tmp_path):
    """A row marked interrupted (as boot reconcile / shutdown drain do)
    goes back to clean when its next run settles."""
    lc = _lc(tmp_path)
    lc.cfg.registry.upsert_session("s1", slot="slot1", state=LIVE)
    lc.cfg.registry.mark_interrupted("s1")  # the default-True call sites
    _add(lc, "s1")
    assert lc.cfg.registry.get("s1").interrupted is True

    lc._on_settled("s1")

    assert lc.cfg.registry.get("s1").interrupted is False, \
        "a settled run must clear the flag — it is the chip's meter"
    assert lc.live["s1"].last_settled is not None  # the settle is still recorded


def test_settle_never_sets_the_flag(tmp_path):
    """A clean settle on a clean session must not touch the flag (no
    spurious 'interrupted' from the clear path)."""
    lc = _lc(tmp_path)
    lc.cfg.registry.upsert_session("s1", slot="slot1", state=LIVE)
    _add(lc, "s1")

    lc._on_settled("s1")

    assert lc.cfg.registry.get("s1").interrupted is False


def test_aborted_settle_keeps_the_drain_mark(tmp_path):
    """The drain's own abort settles the child ABORTED, and its
    agent_settled lands on _on_settled AFTER the drain's
    mark_interrupted — the clear must not race the mark: the run was
    interrupted on purpose, and the flag is the parked session's only
    meter."""
    lc = _lc(tmp_path)
    lc.cfg.registry.upsert_session("s1", slot="slot1", state=LIVE)
    lc.cfg.registry.mark_interrupted("s1")  # the drain's mark
    _add(lc, "s1")

    lc._on_settled("s1", aborted=True)

    assert lc.cfg.registry.get("s1").interrupted is True, \
        "an aborted settle must not clear the drain's mark"
    assert lc.live["s1"].last_settled is not None, \
        "the settle is still recorded — only the clear is withheld"


def test_aborted_settle_on_clean_session_stays_clean(tmp_path):
    """The aborted gate must not be a SETTER either: an aborted settle
    on a session nobody marked stays clean."""
    lc = _lc(tmp_path)
    lc.cfg.registry.upsert_session("s1", slot="slot1", state=LIVE)
    _add(lc, "s1")

    lc._on_settled("s1", aborted=True)

    assert lc.cfg.registry.get("s1").interrupted is False
