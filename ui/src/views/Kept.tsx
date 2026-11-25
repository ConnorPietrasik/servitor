import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import * as api from "../api";
import type { KeepRow } from "../types";
import Confirm from "../components/Confirm";
import { fmtBytes } from "../format";

function fmtTime(t: number): string {
  return new Date(t * 1000).toLocaleDateString();
}

// The shelf: bytes the human chose to keep — no session attached, no
// expiry, never overwritten. Deleting is the only lifecycle, and it takes
// two taps (the sheet names the file and the bytes it frees). One fetch on
// open, then the app-wide stream, viewed down to the two kinds this screen
// uses (?kinds=): the shelf changes on keeps and deletes, a delete on
// another device arrives as `keep_deleted` carrying the path and drops the
// row without a refetch, a keep on another device revalidates (its row
// needs the size/mtime/mime the event does not carry), and a ring gap
// revalidates too — while a session's run-state chatter from the other
// fifteen sessions never crosses the wire for this screen. No polling — and
// a hidden tab closes the stream, like the list does.
export default function Kept() {
  const [rows, setRows] = useState<KeepRow[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [ask, setAsk] = useState<KeepRow | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setRows((await api.listKept()).keep);
      setErr(null);
    } catch (e) {
      setErr(String(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // The app-wide stream, viewed to the two shelf kinds (see the header):
  // a delete from another device drops its row here (the event carries the
  // path — zero extra bytes); a keep from another device revalidates (the
  // new row's size/mtime/mime are not in the event); a ring gap revalidates.
  // Hidden closes the stream; show re-attaches and re-fetches.
  useEffect(() => {
    let unsub: (() => void) | null = null;
    const attach = () => {
      if (unsub) return;
      unsub = api.subscribeGlobal(
        (name, data) => {
          if (name === "keep_deleted") {
            const p = (data as { detail?: string } | null)?.detail;
            if (!p) return;
            setRows((cur) => (cur ?? []).filter((r) => r.path !== p));
          } else if (name === "file_kept") {
            void load();
          }
        },
        () => void load(),
        ["keep_deleted", "file_kept"],
      );
    };
    attach();
    const onVis = () => {
      if (document.hidden) {
        unsub?.();
        unsub = null;
      } else {
        void load();
        attach();
      }
    };
    document.addEventListener("visibilitychange", onVis);
    return () => {
      unsub?.();
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [load]);

  async function doDelete() {
    if (!ask) return;
    try {
      const freed = await api.deleteKept(ask.path);
      setRows((cur) => (cur ?? []).filter((r) => r.path !== ask.path));
      setNote(`deleted ${ask.path} (${fmtBytes(freed)} freed)`);
      setAsk(null);
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }

  const total = (rows ?? []).reduce((n, r) => n + r.size, 0);

  return (
    <div className="page">
      <header className="topbar">
        <Link className="back" to="/">
          ← sessions
        </Link>
        <h1>Kept files</h1>
        <button className="btn ghost" onClick={() => void load()}>
          refresh
        </button>
      </header>

      {err && <div className="banner err">{err}</div>}
      {note && (
        <div className="banner" onClick={() => setNote(null)}>
          {note}
        </div>
      )}

      {rows === null && <div className="muted">loading…</div>}
      {rows && rows.length === 0 && (
        <div className="empty">
          Nothing kept yet. Open a session's files and press keep on anything
          you want to survive — a kept file costs no extra bytes and is deleted
          only by you.
        </div>
      )}
      {rows && rows.length > 0 && (
        <div className="muted sm">
          {rows.length} file{rows.length === 1 ? "" : "s"} · {fmtBytes(total)} —
          never swept, never overwritten, shared with the sessions that use
          them
        </div>
      )}

      <ul className="list">
        {rows?.map((r) => (
          <li key={r.path} className="card">
            <div className="card-main">
              <div className="card-title">
                <span className="name">{r.path}</span>
              </div>
              <div className="card-meters">
                <span>{fmtBytes(r.size)}</span>
                <span>{fmtTime(r.mtime)}</span>
                <span>{r.mime}</span>
              </div>
              <div className="row btn-row">
                <a className="btn primary sm" href={api.keptDownloadUrl(r.path)} download>
                  ↓ download
                </a>
                <button
                  className="btn ghost sm"
                  onClick={() => {
                    setBusy(false);
                    setAsk(r);
                  }}
                >
                  delete
                </button>
              </div>
            </div>
          </li>
        ))}
      </ul>

      {ask && (
        <Confirm
          title="Delete a kept file?"
          detail={`${ask.path} · ${fmtBytes(ask.size)}\n\nThis is the last copy you chose to keep. A session that still has it keeps its own link to the same bytes — if it has no copy, deleting this frees the bytes for good.`}
          confirmLabel="delete for good"
          busy={busy}
          onCancel={() => setAsk(null)}
          onConfirm={() => {
            setBusy(true);
            void doDelete();
          }}
        />
      )}
    </div>
  );
}
