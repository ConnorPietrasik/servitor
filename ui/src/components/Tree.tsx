import { useCallback, useEffect, useState } from "react";
import * as api from "../api";
import type { ForkPoint, TreeNode, TreeResp } from "../types";

type EditFn = (p: ForkPoint) => void;

// The branch view (last slice): read-only forest of everything the
// session has ever written (every session file), the current fork
// points, and the "edit that message" action (fork → prefill composer).
export default function Tree({
  sid,
  forkPoints,
  onEdit,
}: {
  sid: string;
  forkPoints: ForkPoint[];
  onEdit: EditFn;
}) {
  const [t, setT] = useState<TreeResp | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setT(await api.getTree(sid));
      setErr(null);
    } catch (e) {
      setErr(String(e));
    }
  }, [sid]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="subview">
      {err && <div className="banner err">{err}</div>}

      <h3 className="subhead">
        fork points <button className="btn ghost sm" onClick={() => void load()}>refresh</button>
      </h3>
      {forkPoints.length === 0 ? (
        <div className="muted">No user messages to fork from yet.</div>
      ) : (
        <ul className="list">
          {forkPoints.map((p) => (
            <li key={p.entryId} className="card fp">
              <div className="fp-text">{p.text.slice(0, 160)}</div>
              <button
                className="btn primary sm"
                disabled={busy !== null}
                onClick={() => {
                  setBusy(p.entryId);
                  onEdit(p);
                  window.setTimeout(() => setBusy(null), 3000);
                }}
              >
                {busy === p.entryId ? "forking…" : "edit this message"}
              </button>
            </li>
          ))}
        </ul>
      )}

      <h3 className="subhead">full tree (all branches, read-only)</h3>
      {t === null && <div className="muted">loading…</div>}
      {t && (
        <>
          {t.files.length > 1 && (
            <div className="muted sm">
              {t.files.length} session files (forks create new files):{" "}
              {t.files.map((f) => f.slice(0, 20) + "…").join(", ")}
            </div>
          )}
          <div className="tree">
            {t.roots.map((r) => (
              <Node
                key={String((r.entry as any).id)}
                n={r}
                leafId={t.leaf_id}
                depth={0}
              />
            ))}
          </div>
        </>
      )}
    </div>
  );
}

function Node({
  n,
  leafId,
  depth,
}: {
  n: TreeNode;
  leafId: string | null;
  depth: number;
}) {
  const e = n.entry as any;
  const id = String(e.id ?? "");
  const m = e.message;
  let label: string;
  if (e.type === "message" && m) {
    const role = m.role;
    if (role === "user") {
      const c = m.content;
      const txt =
        typeof c === "string"
          ? c
          : Array.isArray(c)
            ? c.filter((p: any) => p?.type === "text").map((p: any) => p.text).join("")
            : "";
      label = `user: ${txt.slice(0, 80)}`;
    } else if (role === "assistant") {
      label = "assistant";
    } else {
      label = String(role);
    }
  } else {
    label = String(e.type ?? "entry");
  }
  const isLeaf = leafId === id;
  return (
    <div className="tnode" style={{ marginLeft: depth * 14 }}>
      <span className={`tdot ${isLeaf ? "leaf" : ""}`} />
      <span className="tlabel">{label}</span>
      {isLeaf && <span className="chip live">leaf</span>}
      {n.children.map((c) => (
        <Node key={String((c.entry as any).id)} n={c} leafId={leafId} depth={depth + 1} />
      ))}
    </div>
  );
}
