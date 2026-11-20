import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import * as api from "../api";
import type { SessionRow } from "../types";

function fmtAgo(ts: number | null | undefined): string {
  if (!ts) return "—";
  const s = Math.max(0, (Date.now() - ts * 1000) / 1000);
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function stateChip(s: SessionRow): { label: string; cls: string } {
  if (s.live_process) return { label: "live", cls: "chip live" };
  if (s.interrupted) return { label: "interrupted", cls: "chip warn" };
  if (s.state === "draining") return { label: "draining", cls: "chip warn" };
  if (s.state === "cleaning") return { label: "cleaning", cls: "chip warn" };
  return { label: "idle", cls: "chip idle" };
}

export default function Sessions() {
  const [rows, setRows] = useState<SessionRow[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const nav = useNavigate();
  const timer = useRef<number | null>(null);

  const load = useCallback(async () => {
    try {
      const d = await api.listSessions();
      setRows(d.sessions);
      setErr(null);
    } catch (e) {
      setErr(String(e));
    }
  }, []);

  // meters update without a live socket: poll the list (O(1) registry
  // read server-side) — no SSE needed on the list screen
  useEffect(() => {
    load();
    timer.current = window.setInterval(load, 5000);
    return () => {
      if (timer.current) window.clearInterval(timer.current);
    };
  }, [load]);

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

  return (
    <div className="page">
      <header className="topbar">
        <h1>Servitor</h1>
        <button className="btn primary" onClick={create} disabled={creating}>
          + New session
        </button>
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
            </li>
          );
        })}
      </ul>
    </div>
  );
}
