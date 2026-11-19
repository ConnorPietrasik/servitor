import { useCallback, useEffect, useState } from "react";
import * as api from "../api";
import type { OutboxFile } from "../types";
import { fmtBytes } from "../format";

const SOFT_SIZE_BYTES = 100 * 1024 * 1024; // soft warning threshold (no cap)

function fmtTime(t: number): string {
  return new Date(t * 1000).toLocaleString();
}

// The outbox is a first-class view: name, size, mtime, sha256, download
// (multi-GB friendly: plain link — the browser resumes interrupted
// downloads; the server serves the full file, Range-capable).
export default function Outbox({ sid }: { sid: string }) {
  const [files, setFiles] = useState<OutboxFile[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setFiles((await api.listFiles(sid)).outbox);
      setErr(null);
    } catch (e) {
      setErr(String(e));
    }
  }, [sid]);

  useEffect(() => {
    void load();
    const t = window.setInterval(load, 4000); // sha256 fills in as background hashes finish
    return () => window.clearInterval(t);
  }, [load]);

  return (
    <div className="subview">
      {err && <div className="banner err">{err}</div>}
      {files === null && <div className="muted">loading…</div>}
      {files && files.length === 0 && (
        <div className="empty">
          Nothing in the outbox yet. Files the agent writes to outbox/
          appear here.
        </div>
      )}
      <ul className="list">
        {files?.map((f) => (
          <li key={f.path} className="card">
            <div className="file">
              <div className="file-name">{f.path}</div>
              <div className="card-meters">
                <span>{fmtBytes(f.size)}</span>
                <span>{fmtTime(f.mtime)}</span>
              </div>
              <div className="sha256" title="sha256 (computed in the background)">
                {f.sha256 ?? "sha256 computing…"}
              </div>
              {f.size > SOFT_SIZE_BYTES && (
                <div className="soft-warn">
                  large file — the download may take a while (no size cap;
                  interrupted downloads resume)
                </div>
              )}
              <a
                className="btn primary sm"
                href={api.downloadUrl(sid, `outbox/${f.path}`)}
                download={f.path.split("/").pop()}
              >
                ↓ download
              </a>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
