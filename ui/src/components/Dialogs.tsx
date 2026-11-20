import { useEffect, useState } from "react";
import * as api from "../api";
import type { ExtensionDialog } from "../types";

// Extension dialogs (select/confirm/input/editor): the run is blocked
// until the human answers (or aborts). The SSE event is only the
// notification — the full record (options/prefill) comes from the API,
// and an attaching client re-fetches on load, so a returning phone is
// re-offered any still-pending dialog. Answering unblocks the child and
// the run resumes; the dialog disappears from the pending set.

export default function Dialogs({
  sid,
  dialogs,
  onChanged,
}: {
  sid: string;
  dialogs: ExtensionDialog[];
  onChanged: () => void;
}) {
  if (dialogs.length === 0) return null;
  return (
    <div className="dialogs">
      {dialogs.map((d) => (
        <DialogCard key={d.id} sid={sid} d={d} onChanged={onChanged} />
      ))}
    </div>
  );
}

function DialogCard({
  sid,
  d,
  onChanged,
}: {
  sid: string;
  d: ExtensionDialog;
  onChanged: () => void;
}) {
  const [value, setValue] = useState<string>(d.prefill ?? "");
  // a re-fetched record with a CHANGED prefill wins (same id = same
  // dialog, so this only fires on a genuine change, not every refresh
  // — in-flight typing survives a plain refresh)
  useEffect(() => {
    setValue(d.prefill ?? "");
  }, [d.prefill]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function answer(body: {
    value?: string;
    confirmed?: boolean;
    cancelled?: boolean;
  }) {
    if (!sid || busy) return;
    setBusy(true);
    setErr(null);
    try {
      await api.answerDialog(sid, d.id, body);
      onChanged(); // the dialog left the pending set: refresh
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card dialog-card">
      <div className="dialog-title">
        <span className="chip warn">dialog</span> {d.title || d.method}
      </div>
      {d.message && <div className="dialog-msg">{d.message}</div>}
      {d.method === "select" && (
        <div className="dialog-options">
          {(d.options ?? []).map((o) => (
            <button
              key={o}
              className="btn ghost"
              disabled={busy}
              onClick={() => void answer({ value: o })}
            >
              {o}
            </button>
          ))}
          {(!d.options || d.options.length === 0) && (
            <span className="mini-hint">no options given — cancel below</span>
          )}
        </div>
      )}
      {(d.method === "input" || d.method === "editor") && (
        <textarea
          className="input"
          rows={d.method === "editor" ? 5 : 1}
          placeholder={d.placeholder ?? ""}
          value={value}
          onChange={(e) => setValue(e.target.value)}
        />
      )}
      <div className="dialog-actions">
        {d.method === "confirm" && (
          <>
            <button
              className="btn primary"
              disabled={busy}
              onClick={() => void answer({ confirmed: true })}
            >
              confirm
            </button>
            <button
              className="btn ghost"
              disabled={busy}
              onClick={() => void answer({ confirmed: false })}
            >
              decline
            </button>
          </>
        )}
        {(d.method === "input" || d.method === "editor") && (
          <button
            className="btn primary"
            disabled={busy}
            onClick={() => void answer({ value })}
          >
            send
          </button>
        )}
        <button
          className="btn ghost"
          disabled={busy}
          onClick={() => void answer({ cancelled: true })}
          title="cancel this dialog (the run continues as if declined)"
        >
          cancel
        </button>
      </div>
      {err && <div className="banner err">{err}</div>}
    </div>
  );
}
