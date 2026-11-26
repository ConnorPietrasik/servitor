import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import * as api from "../api";
import type { KeepRow, LibRow } from "../types";
import Confirm from "../components/Confirm";
import { fmtBytes } from "../format";

function fmtTime(t: number): string {
  return new Date(t * 1000).toLocaleDateString();
}

// The shelf: bytes the human chose to keep — no session attached, no
// expiry, never overwritten. Deleting is the only lifecycle, and it takes
// two taps (the sheet names the file and the bytes it frees). One fetch per
// area on open, then the app-wide stream, viewed down to the kinds this
// screen uses (?kinds=): the shelf changes on keeps and deletes, a delete on
// another device arrives as `keep_deleted` carrying the path and drops the
// row without a refetch, a keep on another device revalidates (its row
// needs the size/mtime/mime the event does not carry), and a ring gap
// revalidates too — while a session's run-state chatter from the other
// fifteen sessions never crosses the wire for this screen. No polling — and
// a hidden tab closes the stream, like the list does.
//
// The library (lib/): the other durable area — and the one the agent READS:
// it is mounted read-only into every session's workdir. Human writes go in
// as saves from a session's files view (→ lib); the host's repo sync keeps
// the git-backed tree current, and a save is additive over it. Same stream
// discipline: a save on another device revalidates (the row's size/mtime
// are not in the event), a delete carries the path and drops its row.
export default function Kept() {
  const [rows, setRows] = useState<KeepRow[] | null>(null);
  const [lib, setLib] = useState<LibRow[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [ask, setAsk] = useState<{
    kind: "keep" | "lib";
    row: { path: string; size: number };
  } | null>(null);
  const [busy, setBusy] = useState(false);

  // the two durable areas revalidate independently: a keep only moves the
  // shelf, a save only moves the library — each event pays for its own
  // ETag'd fetch, never the other area's (a 304 costs ~200 bytes)
  const loadKept = useCallback(async () => {
    try {
      setRows((await api.listKept()).keep);
      setErr(null);
    } catch (e) {
      setErr(String(e));
    }
  }, []);
  const loadLib = useCallback(async () => {
    try {
      setLib((await api.listLib()).lib);
      setErr(null);
    } catch (e) {
      setErr(String(e));
    }
  }, []);
  const load = useCallback(() => {
    void loadKept();
    void loadLib();
  }, [loadKept, loadLib]);

  useEffect(() => {
    void load();
  }, [load]);

  // The app-wide stream, viewed to the four durable-area kinds (see the
  // header): a delete from another device drops its row here (the event
  // carries the path — zero extra bytes); a keep revalidates the SHELF,
  // a save revalidates the LIBRARY (the new row's size/mtime/mime are not
  // in the event, and each area pays only for itself); a ring gap
  // revalidates both. Hidden closes the stream; show re-attaches and
  // re-fetches.
  useEffect(() => {
    let unsub: (() => void) | null = null;
    const attach = () => {
      if (unsub) return;
      unsub = api.subscribeGlobal(
        (name, data) => {
          if (name === "keep_deleted" || name === "lib_deleted") {
            const p = (data as { detail?: string } | null)?.detail;
            if (!p) return;
            if (name === "keep_deleted")
              setRows((cur) => (cur ?? []).filter((r) => r.path !== p));
            else setLib((cur) => (cur ?? []).filter((r) => r.path !== p));
          } else if (name === "file_kept") {
            void loadKept();
          } else if (name === "lib_saved") {
            void loadLib();
          }
        },
        () => void load(),
        ["keep_deleted", "file_kept", "lib_saved", "lib_deleted"],
      );
    };
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
    // a tab that mounts HIDDEN holds no stream: the first show re-fetches
    // and then attaches — the UI pays for change, never for waiting
    if (!document.hidden) attach();
    return () => {
      unsub?.();
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [load]);

  async function doDelete() {
    if (!ask) return;
    try {
      const freed =
        ask.kind === "keep"
          ? await api.deleteKept(ask.row.path)
          : await api.deleteLibFile(ask.row.path);
      if (ask.kind === "keep")
        setRows((cur) => (cur ?? []).filter((r) => r.path !== ask.row.path));
      else setLib((cur) => (cur ?? []).filter((r) => r.path !== ask.row.path));
      setNote(`deleted ${ask.row.path} (${fmtBytes(freed)} freed)`);
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
                    setAsk({ kind: "keep", row: r });
                  }}
                >
                  delete
                </button>
              </div>
            </div>
          </li>
        ))}
      </ul>

      <h2 className="sect">Library — lib/, what the agent reads</h2>
      <div className="muted sm">
        Saved from a session's files (→ lib). Mounted read-only into every
        session's workdir: the agents read it, only you write it. The host's
        repo sync keeps its own files current; your saves are additive and
        survive syncs.
      </div>
      {lib === null && <div className="muted">loading…</div>}
      {lib && lib.length === 0 && (
        <div className="empty">
          The library is empty. Open a session's files and press → lib on
          anything the agents should be able to read — a save costs no extra
          bytes.
        </div>
      )}
      {lib && lib.length > 0 && (
        <ul className="list">
          {lib.map((r) => (
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
                  <a className="btn primary sm" href={api.libDownloadUrl(r.path)} download>
                    ↓ download
                  </a>
                  <button
                    className="btn ghost sm"
                    onClick={() => {
                      setBusy(false);
                      setAsk({ kind: "lib", row: r });
                    }}
                  >
                    delete
                  </button>
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}

      {ask && (
        <Confirm
          title={ask.kind === "keep" ? "Delete a kept file?" : "Delete a library file?"}
          detail={
            ask.kind === "keep"
              ? `${ask.row.path} · ${fmtBytes(ask.row.size)}\n\nThis is the last copy you chose to keep. A session that still has it keeps its own link to the same bytes — if it has no copy, deleting this frees the bytes for good.`
              : `${ask.row.path} · ${fmtBytes(ask.row.size)}\n\nEvery session's agent loses it at once. A file the host's repo pushes will re-land on the next sync; a file you saved from here is gone for good.`
          }
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
