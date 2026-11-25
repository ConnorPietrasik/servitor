import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import * as api from "../api";
import type { SessionRow, UsageResp } from "../types";
import { fmtBytes } from "../format";
import Confirm from "../components/Confirm";

function fmtAgo(ts: number | null | undefined): string {
  if (!ts) return "—";
  const s = Math.max(0, (Date.now() - ts * 1000) / 1000);
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

// "4.1 GB held · GC in 3d" — the two numbers that make pin-or-download a
// decision instead of a surprise. A release sweeps the session's files with
// everything else, so an unpinned session holding something you care about
// has to be pinned (or downloaded) BEFORE the idle GC reaches it.
function bytesLine(s: SessionRow, u: UsageResp | null): string | null {
  const mine = u?.by_session?.[s.id];
  if (!mine) return null;
  // every per-session area the server bills (the same set release deletes
  // and area_footprint counts) — sum them all except the file count, so a
  // new area on the server can never silently drop out of this number
  const bytes = Object.entries(mine)
    .filter(([k]) => k !== "files")
    .reduce((n, [, v]) => n + v, 0);
  const parts: string[] = [];
  if (bytes > 0) parts.push(`${fmtBytes(bytes)} held`);
  const idleDays = u?.gc_idle_days ?? null;
  if (!s.stable && idleDays && s.last_activity) {
    const left = idleDays * 86400 - (Date.now() / 1000 - s.last_activity);
    if (left > 0 && left < 4 * 86400) {
      parts.push(left > 86400 ? `GC in ${Math.ceil(left / 86400)}d` : `GC in ${Math.ceil(left / 3600)}h`);
    }
  }
  return parts.length ? parts.join(" · ") : null;
}

// The sheet's "exact consequence" line. bytesLine may be a bare "GC in …"
// countdown (zero bytes held) — only a byte count belongs in "swept (…)",
// so the countdown stays on the list row, not in the consequence.
function confirmDetail(s: SessionRow, u: UsageResp | null): string {
  const held = bytesLine(s, u);
  const swept = held?.includes("held") ? ` (${held})` : "";
  return `${s.display_name || s.id.slice(0, 8)}\n\nReleased now: any active run is aborted, and its files${swept} and transcript are deleted. Files you kept (the kept shelf) survive.`;
}

function stateChip(s: SessionRow): { label: string; cls: string } {
  if ((s.dialogs_pending ?? 0) > 0)
    return { label: `awaiting reply (${s.dialogs_pending})`, cls: "chip warn" };
  // busy ⇒ live_process, so "running" refines the "live" state below
  if (s.busy) return { label: "running", cls: "chip live" };
  if (s.live_process) return { label: "live", cls: "chip live" };
  if (s.interrupted) return { label: "interrupted", cls: "chip warn" };
  if (s.state === "draining") return { label: "draining", cls: "chip warn" };
  if (s.state === "cleaning") return { label: "cleaning", cls: "chip warn" };
  return { label: "idle", cls: "chip idle" };
}

export default function Sessions() {
  const [rows, setRows] = useState<SessionRow[] | null>(null);
  const [use, setUse] = useState<UsageResp | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [askDelete, setAskDelete] = useState<SessionRow | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteErr, setDeleteErr] = useState<string | null>(null);
  const nav = useNavigate();
  const unsub = useRef<(() => void) | null>(null);
  const pending = useRef<number | null>(null);

  const load = useCallback(async () => {
    try {
      const [d, u] = await Promise.all([
        api.listSessions(),
        api.usage().catch(() => null), // meters are the one thing allowed to fail
      ]);
      setRows(d.sessions);
      api.pruneGoneSessionStorage(d.sessions.map((s) => s.id));
      if (u) setUse(u);
      setErr(null);
    } catch (e) {
      setErr(String(e));
    }
  }, []);

  // The list follows reality on the app-wide stream — no polling. A burst of
  // events (an update touches every session) coalesces into ONE refetch, and
  // a hidden screen closes its stream instead of holding the radio awake.
  const refetchSoon = useCallback(() => {
    if (pending.current !== null) return;
    pending.current = window.setTimeout(() => {
      pending.current = null;
      void load();
    }, 600);
  }, [load]);

  useEffect(() => {
    void load();
    const attach = () => {
      if (unsub.current) return;
      unsub.current = api.subscribeGlobal(refetchSoon, () => void load());
    };
    attach();
    const onVis = () => {
      if (document.hidden) {
        unsub.current?.();
        unsub.current = null;
      } else {
        void load();
        attach();
      }
    };
    document.addEventListener("visibilitychange", onVis);
    return () => {
      unsub.current?.();
      document.removeEventListener("visibilitychange", onVis);
      if (pending.current !== null) window.clearTimeout(pending.current);
    };
  }, [load, refetchSoon]);

  async function create() {
    setCreating(true);
    try {
      const r = await api.createSession();
      nav(`/s/${r.session_id}`);
    } catch (e) {
      setErr(String(e));
    } finally {
      setCreating(false);
    }
  }

  async function toggleStable(s: SessionRow) {
    try {
      await api.setStable(s.id, !s.stable);
      await load();
    } catch (e) {
      setErr(String(e));
    }
  }

  // release() is the single cleanup path and kills any live run itself, so
  // a running session needs no separate abort before a manual delete
  async function doDelete(s: SessionRow) {
    setDeleting(true);
    setDeleteErr(null);
    try {
      const out = await api.releaseSession(s.id, "user delete");
      if (out.quarantined || !out.released) {
        // sweep could not verify the slot clean: the row stays draining and
        // the slot is quarantined — the release is retryable, not done; the
        // error goes IN the sheet (the page banner hides behind the scrim)
        setDeleteErr(
          out.errors.join("; ") ||
            "sweep could not verify the slot clean; the session is still draining",
        );
      } else {
        setAskDelete(null);
      }
      await load();
      // after load(): a clean load clears the banner, and a released-
      // with-errors release (a leftover the sweep could not delete) must still
      // say so
      if (out.released && !out.quarantined && out.errors.length)
        setErr(out.errors.join("; "));
    } catch (e) {
      setDeleteErr(String(e));
    } finally {
      setDeleting(false);
    }
  }

  return (
    <div className="page">
      <header className="topbar">
        <h1>Servitor</h1>
        <div className="actions">
          <Link className="btn ghost" to="/kept" title="files you kept past their session">
            kept
          </Link>
          <button className="btn primary" onClick={create} disabled={creating}>
            + New session
          </button>
        </div>
      </header>

      {err && <div className="banner err">{err}</div>}
      {rows === null && <div className="muted">loading…</div>}

      {rows && rows.length === 0 && (
        <div className="empty">
          No sessions yet. Start one — it gets its own isolated sandbox.
        </div>
      )}

      <ul className="list">
        {rows?.map((s) => {
          const chip = stateChip(s);
          const held = bytesLine(s, use);
          return (
            <li key={s.id} className="card">
              <Link className="card-main" to={`/s/${s.id}`}>
                <div className="card-title">
                  <span className="name">{s.display_name || s.id.slice(0, 8)}</span>
                  <span className={chip.cls}>{chip.label}</span>
                  {s.stable && <span className="chip stable" title="stable pin">📌</span>}
                </div>
                <div className="card-meters">
                  <span title="slot">slot {s.slot}</span>
                  <span title="config version">cfg {s.config_version ?? "—"}</span>
                  <span title="last activity">{fmtAgo(s.last_activity)}</span>
                </div>
                {held && (
                  <div className="card-notes">
                    <span title="bytes this session holds; a release deletes them">
                      {held}
                    </span>
                  </div>
                )}
                {(s.update_waiting || s.new_extensions.length > 0) && (
                  <div className="card-notes">
                    {s.update_waiting && <span>update waiting for idle</span>}
                    {s.new_extensions.map((e) => (
                      <span key={e}>new ext: {e}</span>
                    ))}
                  </div>
                )}
              </Link>
              <button
                className="btn ghost"
                onClick={() => toggleStable(s)}
                title={s.stable ? "unpin (release-eligible)" : "pin (never released by pressure/GC)"}
              >
                {s.stable ? "unpin" : "pin"}
              </button>
              <button
                className="btn ghost"
                onClick={() => setAskDelete(s)}
                disabled={deleting && askDelete?.id === s.id}
                title="release this session now"
              >
                delete
              </button>
            </li>
          );
        })}
      </ul>

      {askDelete && (
        <Confirm
          title="Delete this session?"
          detail={confirmDetail(askDelete, use)}
          confirmLabel="delete session"
          busy={deleting}
          error={deleteErr}
          onCancel={() => {
            setAskDelete(null);
            setDeleteErr(null);
          }}
          onConfirm={() => void doDelete(askDelete)}
        />
      )}
    </div>
  );
}
