import { useCallback, useEffect, useMemo, useState } from "react";
import * as api from "../api";
import { fmtBytes } from "../format";
import type { FilesChanged, KeepRow, OutboxFile, SessionRow, UploadRow } from "../types";
import Confirm from "./Confirm";

const SOFT_SIZE_BYTES = 100 * 1024 * 1024; // soft warning threshold (no cap)

// The event the parent's SSE stream hands down: a delta (fold it in) or,
// for `null`, "something changed and I can't say what — revalidate" (a
// delete the phone itself did not make, a rename, or a ring gap). The
// revalidate is an ETag fetch: a changed listing comes back whole, an
// unchanged one costs ~200 bytes. `kind` names the one revalidate that
// touches nothing the listing carries: a keep (possibly from another tab)
// changes no row but can stale the kept-badges.
export interface FileEvent {
  seq: number;
  delta: FilesChanged | null;
  kind?: string;
}

interface Dir {
  path: string; // "" for the outbox root
  files: OutboxFile[];
}

// One flat listing into folders, root first, each folder's files newest first.
// A phone screen showing `v2/data/rows.csv` next to `loose.txt` is a list of
// strings; showing folders is a thing a person can aim at.
function groupByDir(files: OutboxFile[]): Dir[] {
  const dirs = new Map<string, OutboxFile[]>();
  for (const f of files) {
    const i = f.path.lastIndexOf("/");
    const dir = i === -1 ? "" : f.path.slice(0, i);
    const list = dirs.get(dir) ?? [];
    list.push(f);
    dirs.set(dir, list);
  }
  return [...dirs.entries()]
    .map(([path, list]) => ({
      path,
      files: list.sort((a, b) => b.mtime - a.mtime),
    }))
    .sort((a, b) => (a.path === "" ? -1 : b.path === "" ? 1 : a.path.localeCompare(b.path)));
}

// The uploads' tree: a folder upload lands in the folders it arrived in, so
// the inbox shows those folders like the outbox does (root first, newest
// first within a folder).
function groupUploads(rows: UploadRow[]): Dir[] {
  const dirs = new Map<string, UploadRow[]>();
  for (const u of rows) {
    const i = u.name.lastIndexOf("/");
    const dir = i === -1 ? "" : u.name.slice(0, i);
    const list = dirs.get(dir) ?? [];
    list.push(u);
    dirs.set(dir, list);
  }
  return [...dirs.entries()]
    .map(([path, list]) => ({
      path,
      files: list.map((u) => ({
        path: u.name,
        size: u.size,
        mtime: u.ts,
        sha256: u.sha256,
        mime: u.mime,
      })),
    }))
    .sort((a, b) => (a.path === "" ? -1 : b.path === "" ? 1 : a.path.localeCompare(b.path)));
}

function fmtTime(t: number): string {
  return new Date(t * 1000).toLocaleString();
}

// Mirrors the server's archive naming for a selection: a single entry is
// named for itself, a multi-select for the deepest directory ALL selected
// (outbox-relative) paths share, else "files". Labels the download; the
// server's Content-Disposition is the truth.
function selectionZipName(paths: string[]): string {
  if (paths.length === 1) {
    return paths[0].split("/").pop() || "files";
  }
  let parts: string[] = (paths
    .map((p) => p.split("/"))
    .sort((a, b) => a.length - b.length)[0] ?? [])
    .slice(0, -1);
  for (const p of paths) {
    const d = p.split("/").slice(0, -1);
    let i = 0;
    while (i < parts.length && i < d.length && parts[i] === d[i]) i++;
    parts = parts.slice(0, i);
    if (parts.length === 0) return "files";
  }
  return parts[parts.length - 1] || "files";
}

// The session's files, both directions: browse, download, rename, keep,
// attach, delete.
//
// Data budget: this view NEVER polls. It fetches once (ETag'd), then folds the
// `files_changed` deltas that arrive on the chat's existing stream, so an open
// Files tab on a phone costs bytes only when a file actually changed. The shelf
// and the other sessions' listings are fetched when — and only when — the
// human opens them.
export default function Files({
  sid,
  ev,
  onAttached,
}: {
  sid: string;
  ev: FileEvent | null;
  // "attach & tell": the composer gets a line about the file, and the human
  // sends it (nothing is sent behind their back)
  onAttached?: (name: string) => void;
}) {
  const [outbox, setOutbox] = useState<OutboxFile[] | null>(null);
  const [uploads, setUploads] = useState<UploadRow[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [ask, setAsk] = useState<
    { title: string; detail: string; confirmLabel: string; run: () => void } | null
  >(null);
  const [busy, setBusy] = useState(false);
  // source paths already kept ("outbox/<rel>" | "uploads/<name>" → the shelf
  // path the server landed it under). Persisted per session like the
  // staged-upload records: this view remounts on every chat↔files switch,
  // and a badge that dies with the mount would let the button silently make
  // a second copy under a suffix. The shelf is the truth — when it is
  // loaded, a badge whose recorded shelf path is gone is unkept and goes.
  const keptKey = `servitor-kept:${sid}`;
  const [kept, setKept] = useState<Record<string, string>>(() => {
    try {
      const parsed = JSON.parse(localStorage.getItem(keptKey) ?? "{}");
      return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
    } catch {
      return {};
    }
  });
  const updateKept = (updater: (cur: Record<string, string>) => Record<string, string>) =>
    setKept((cur) => {
      const next = updater(cur);
      try {
        localStorage.setItem(keptKey, JSON.stringify(next));
      } catch {
        /* private mode: the badge is simply per-mount */
      }
      return next;
    });
  const [shelf, setShelf] = useState<KeepRow[] | null>(null);
  const [showShelf, setShowShelf] = useState(false);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const [selected, setSelected] = useState<Record<string, boolean>>({});
  const [rename, setRename] = useState<{ from: string; value: string } | null>(null);
  const [others, setOthers] = useState<SessionRow[] | null>(null);
  const [othersFiles, setOthersFiles] = useState<{
    sid: string;
    outbox: OutboxFile[];
    uploads: { path: string; size: number }[];
  } | null>(null);

  const load = useCallback(
    async (force = false) => {
      try {
        const { data } = await api.listFiles(sid, force);
        setOutbox(data.outbox);
        setUploads(data.uploads);
        setErr(null);
      } catch (e) {
        setErr(String(e));
      }
    },
    [sid],
  );

  useEffect(() => {
    void load();
  }, [load]);

  // deltas from the session's stream (the parent owns the socket)
  useEffect(() => {
    if (!ev) return;
    if (ev.delta) {
      const folded = api.applyFilesDelta(sid, ev.delta);
      if (folded) setOutbox(folded.outbox);
      else void load(true); // no cache to fold into (first load raced it)
    } else if (ev.kind === "file_kept") {
      // a keep — possibly from another tab — changes no listing row; what
      // goes stale is a kept-badge. Only the open shelf renders one, so only
      // it pays: the shelf is the badge's truth, and the reconcile drops any
      // badge whose shelf path is gone
      if (shelf !== null) {
        void api
          .listKept()
          .then(({ keep }) => {
            setShelf(keep);
            reconcileKept(keep);
          })
          .catch((e) => setErr(String(e)));
      }
    } else {
      // revalidate through the ETag (not force): the event names a change
      // the listing will have to carry, but a stale duplicate event must
      // cost ~200 bytes, not the whole body
      void load();
    }
    // ev.seq is the trigger; the handlers below are stable per session
  }, [ev?.seq]); // eslint-disable-line react-hooks/exhaustive-deps

  const dirs = useMemo(() => groupByDir(outbox ?? []), [outbox]);
  const uploadDirs = useMemo(() => groupUploads(uploads), [uploads]);
  const selectedPaths = useMemo(() => Object.keys(selected).filter((p) => selected[p]), [selected]);
  const selectedBytes = useMemo(
    () => (outbox ?? []).filter((f) => selected[f.path]).reduce((n, f) => n + f.size, 0),
    [outbox, selected],
  );

  async function doKeep(source: string, label: string) {
    try {
      const row = await api.keepFile(sid, source);
      updateKept((cur) => ({ ...cur, [source]: row.path }));
      setNote(`kept ${label} as ${row.path} — it now survives the session`);
      if (shelf !== null) {
        const rows = (await api.listKept()).keep;
        setShelf(rows);
        reconcileKept(rows);
      }
    } catch (e) {
      setErr(String(e));
    }
  }

  // drop badges the shelf no longer backs (an unkeep from the Kept screen)
  function reconcileKept(rows: KeepRow[]) {
    const alive = new Set(rows.map((r) => r.path));
    updateKept((cur) => {
      const next: Record<string, string> = {};
      for (const [src, shelfPath] of Object.entries(cur))
        if (alive.has(shelfPath)) next[src] = shelfPath;
      return Object.keys(next).length === Object.keys(cur).length ? cur : next;
    });
  }

  // the shelf is fetched when the human opens the section, never on mount:
  // a phone that does not care about old kept files does not pay for them
  async function toggleShelf() {
    setShowShelf((v) => !v);
    if (shelf === null) {
      try {
        const rows = (await api.listKept()).keep;
        setShelf(rows);
        reconcileKept(rows);
      } catch (e) {
        setErr(String(e));
      }
    }
  }

  function tell(name: string) {
    onAttached?.(name);
    setNote(`${name} is in the inbox — the composer has a line about it to send`);
  }

  async function doAttach(source: string, path: string, label: string) {
    try {
      const f = await api.attachFile(sid, source, path);
      // the landed path is the source's area-relative path — an attach
      // keeps the folder its source lived in (a keep's bucket prefix is the
      // source session, not the file's shape, so it drops) — and a collision
      // in the same folder lands it beside a suffix, which the note must say
      // rather than pretend
      const expected =
        source === "keep"
          ? (label.includes("/") ? label.slice(label.indexOf("/") + 1) : label)
          : label;
      setNote(
        `${f.name} is in this session's inbox — same bytes, no copy` +
          (f.name === expected ? "" : ` (a file already held that path — landed as ${f.name})`),
      );
      setShowShelf(false);
      setOthersFiles(null);
      void load(true);
    } catch (e) {
      setErr(String(e));
    }
  }

  async function openPicker() {
    if (others === null) {
      try {
        setOthers((await api.listSessions()).sessions.filter((r) => r.id !== sid));
      } catch (e) {
        setErr(String(e));
      }
    } else {
      setOthers(null);
      setOthersFiles(null);
    }
  }

  async function pickSession(id: string) {
    try {
      // both of the other session's areas are attachable (the API takes
      // `uploads/...` as well as `outbox/...`): its artifacts, and what its
      // own human sent it
      const { data } = await api.listFiles(id);
      setOthersFiles({
        sid: id,
        outbox: data.outbox,
        uploads: data.uploads.map((u) => ({ path: u.name, size: u.size })),
      });
    } catch (e) {
      setErr(String(e));
    }
  }

  async function doDeleteOne(target: { kind: "outbox" | "upload"; path: string }) {
    try {
      const freed =
        target.kind === "outbox"
          ? await api.deleteOutboxFile(sid, target.path)
          : await api.deleteUpload(sid, target.path);
      if (target.kind === "outbox") {
        setOutbox((cur) => (cur ?? []).filter((f) => f.path !== target.path));
      } else {
        setUploads((cur) => cur.filter((u) => u.name !== target.path));
      }
      setNote(`deleted ${target.path} (${fmtBytes(freed)} freed)`);
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
      setAsk(null);
    }
  }

  async function doDeleteMany() {
    let freed = 0;
    let failed = 0;
    for (const p of selectedPaths) {
      try {
        freed += await api.deleteOutboxFile(sid, p);
      } catch {
        failed++;
      }
    }
    setSelected({});
    void load(true);
    setBusy(false);
    setAsk(null);
    setNote(
      `deleted ${selectedPaths.length - failed} file(s), ${fmtBytes(freed)} freed` +
        (failed ? ` — ${failed} refused (gone already, or not yours)` : ""),
    );
  }

  async function doRename() {
    if (!rename) return;
    const area = rename.from.startsWith("uploads/") ? "uploads" : "outbox";
    const to = rename.value.trim();
    if (!to) {
      setRename(null);
      return;
    }
    // the area is implied: "v2/note.md" moves it into v2/, "outbox/v2/note.md"
    // says the same thing twice, and a leading slash is decoration
    const clean = to.replace(/^\/+/, "");
    const target = clean.startsWith(area + "/") ? clean : `${area}/${clean}`;
    try {
      const moved = await api.renameFile(sid, rename.from, target);
      // the kept-badge is keyed by the source path: a renamed file keeps its
      // badge under the NEW path (the shelf entry it points at is untouched),
      // never a stale key the shelf can no longer match against. Both areas
      // report a path within the area; the area goes back on as the key
      const newFrom = `${area}/${moved.to}`;
      if (newFrom !== rename.from) {
        const oldKey = rename.from;
        updateKept((cur) => {
          if (!(oldKey in cur)) return cur;
          const next = { ...cur, [newFrom]: cur[oldKey] };
          delete next[oldKey];
          return next;
        });
      }
      setNote(`renamed to ${moved.to}`);
      setRename(null);
      void load(true);
    } catch (e) {
      setErr(String(e));
      setRename(null);
    }
  }

  function askDelete(what: string, detail: string, run: () => void) {
    setBusy(false);
    setAsk({ title: `Delete ${what}?`, detail, confirmLabel: "delete for good", run });
  }

  return (
    <div className="subview">
      {err && <div className="banner err">{err}</div>}
      {note && (
        <div className="banner" onClick={() => setNote(null)}>
          {note}
        </div>
      )}

      <div className="sect-row">
        <h2 className="sect">Outbox — the agent's handoffs</h2>
        <a
          className="btn ghost xs"
          href={api.archiveUrl(sid, "outbox")}
          download="outbox.zip"
          title="download the whole outbox as one archive (streamed; nothing is staged on the server)"
        >
          ↓ zip all
        </a>
      </div>
      {outbox === null && <div className="muted">loading…</div>}
      {outbox && outbox.length === 0 && (
        <div className="empty">
          Nothing in the outbox yet. Files the agent writes to outbox/ appear
          here the moment they change — no refresh.
        </div>
      )}

      {selectedPaths.length > 0 && (
        <div className="bulkbar">
          <span>
            {selectedPaths.length} selected · {fmtBytes(selectedBytes)}
          </span>
          <a
            className="btn sm"
            href={api.archiveUrl(sid, selectedPaths.map((p) => `outbox/${p}`))}
            download={`${selectionZipName(selectedPaths)}.zip`}
            title="download exactly the selected files as one archive (streamed; nothing is staged on the server)"
          >
            ↓ archive
          </a>
          <button
            className="btn ghost sm"
            onClick={() => setSelected({})}
          >
            clear
          </button>
          <button
            className="btn danger sm"
            onClick={() =>
              askDelete(
                `${selectedPaths.length} file(s)`,
                `${selectedPaths.map((p) => `• ${p}`).slice(0, 12).join("\n")}${
                  selectedPaths.length > 12 ? `\n• … ${selectedPaths.length - 12} more` : ""
                }\n\n${fmtBytes(
                  selectedBytes,
                )} freed. These artifacts are gone for good when the session is released — keep anything you want first.`,
                () => void doDeleteMany(),
              )
            }
          >
            delete
          </button>
        </div>
      )}

      {dirs.map((d) => (
        <div key={d.path || "."} className="dir">
          {d.path !== "" && (
            <div className="dir-head" onClick={() => setCollapsed((c) => ({ ...c, [d.path]: !c[d.path] }))}>
              <span className="dir-name">
                {collapsed[d.path] ? "▸" : "▾"} {d.path}/
              </span>
              <span className="muted sm">{d.files.length} file(s)</span>
              <a
                className="btn ghost xs"
                href={api.archiveUrl(sid, `outbox/${d.path}`)}
                download={`${d.path.split("/").pop()}.zip`}
                onClick={(e) => e.stopPropagation()}
              >
                ↓ zip
              </a>
            </div>
          )}
          {!collapsed[d.path] &&
            d.files.map((f) => (
              <div key={f.path} className="card file-row">
                <input
                  type="checkbox"
                  className="sel"
                  checked={!!selected[f.path]}
                  onChange={(e) => setSelected((s) => ({ ...s, [f.path]: e.target.checked }))}
                  aria-label={`select ${f.path}`}
                />
                <div className="file">
                  <div className="file-name">
                    {f.path.lastIndexOf("/") === -1 ? f.path : f.path.slice(f.path.lastIndexOf("/") + 1)}
                    {f.stable === false && (
                      <span className="chip warn sm-chip" title="size or mtime moved on the last sweep">
                        writing…
                      </span>
                    )}
                  </div>
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
                  {rename?.from === `outbox/${f.path}` && (
                    <div className="row">
                      <input
                        className="grow"
                        autoFocus
                        value={rename.value}
                        onChange={(e) => setRename({ from: rename.from, value: e.target.value })}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") void doRename();
                          if (e.key === "Escape") setRename(null);
                        }}
                        placeholder="new name, or v2/new-name.md"
                      />
                    </div>
                  )}
                  <div className="row btn-row">
                    <a
                      className="btn primary sm"
                      href={api.downloadUrl(sid, `outbox/${f.path}`)}
                      download={f.path.split("/").pop()}
                    >
                      ↓ download
                    </a>
                    {kept[`outbox/${f.path}`] ? (
                      <span className="chip stable sm-chip" title={`kept as ${kept[`outbox/${f.path}`]}`}>
                        kept
                      </span>
                    ) : (
                      <button
                        className="btn sm"
                        title="keep this past the session (no extra bytes; it is deleted only by you)"
                        onClick={() => void doKeep(`outbox/${f.path}`, f.path)}
                      >
                        keep
                      </button>
                    )}
                    <button
                      className="btn ghost sm"
                      title="rename or move inside the outbox"
                      onClick={() => setRename({ from: `outbox/${f.path}`, value: f.path.split("/").pop() ?? f.path })}
                    >
                      rename
                    </button>
                    <button
                      className="btn ghost sm"
                      onClick={() =>
                        askDelete(
                          f.path,
                          `${f.path} · ${fmtBytes(f.size)}\n\nThis artifact is deleted from the session. It is gone for good when the session is released — anything you want to keep, download first.`,
                          () => void doDeleteOne({ kind: "outbox", path: f.path }),
                        )
                      }
                    >
                      delete
                    </button>
                  </div>
                </div>
              </div>
            ))}
        </div>
      ))}

      <div className="row">
        <h2 className="sect">Inbox — what you sent</h2>
        {uploads.length > 0 && (
          <a
            className="btn ghost sm"
            href={api.archiveUrl(sid, "uploads")}
            download="uploads.zip"
            title="download every upload as one archive (streamed; nothing is staged on the server)"
          >
            ↓ zip all
          </a>
        )}
      </div>
      {uploads.length === 0 && <div className="empty">No uploads in this session yet.</div>}
      {uploadDirs.map((d) => (
        <div key={d.path || "."} className="dir">
          {d.path !== "" && (
            <div className="dir-head" onClick={() => setCollapsed((c) => ({ ...c, [`u:${d.path}`]: !c[`u:${d.path}`] }))}>
              <span className="dir-name">
                {collapsed[`u:${d.path}`] ? "▸" : "▾"} {d.path}/
              </span>
              <span className="muted sm">{d.files.length} file(s)</span>
              <a
                className="btn ghost xs"
                href={api.archiveUrl(sid, `uploads/${d.path}`)}
                download={`${d.path.split("/").pop()}.zip`}
                onClick={(e) => e.stopPropagation()}
              >
                ↓ zip
              </a>
            </div>
          )}
          {!collapsed[`u:${d.path}`] &&
            d.files.map((f) => (
              <div key={f.path} className="card file-row">
                <div className="file">
                  <div className="file-name">{f.path.slice(f.path.lastIndexOf("/") + 1)}</div>
                  <div className="card-meters">
                    <span>{fmtBytes(f.size)}</span>
                    <span>{fmtTime(f.mtime)}</span>
                    <span>{f.mime}</span>
                  </div>
                  <div className="sha256" title="sha256 (computed on upload)">
                    {f.sha256}
                  </div>
                  {rename?.from === `uploads/${f.path}` && (
                    <div className="row">
                      <input
                        className="grow"
                        autoFocus
                        value={rename.value}
                        onChange={(e) => setRename({ from: rename.from, value: e.target.value })}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") void doRename();
                          if (e.key === "Escape") setRename(null);
                        }}
                        placeholder="new name, or v2/new-name.md"
                      />
                    </div>
                  )}
                  <div className="row btn-row">
                    <a
                      className="btn primary sm"
                      href={api.downloadUrl(sid, `uploads/${f.path}`)}
                      download={f.path.split("/").pop()}
                    >
                      ↓ download
                    </a>
                    {kept[`uploads/${f.path}`] ? (
                      <span className="chip stable sm-chip" title={`kept as ${kept[`uploads/${f.path}`]}`}>
                        kept
                      </span>
                    ) : (
                      <button
                        className="btn sm"
                        title="keep this past the session (no extra bytes; it is deleted only by you)"
                        onClick={() => void doKeep(`uploads/${f.path}`, f.path.split("/").pop() ?? f.path)}
                      >
                        keep
                      </button>
                    )}
                    <button
                      className="btn ghost sm"
                      title="rename or move inside the inbox"
                      onClick={() => setRename({ from: `uploads/${f.path}`, value: f.path.split("/").pop() ?? f.path })}
                    >
                      rename
                    </button>
                    <button
                      className="btn ghost sm"
                      onClick={() =>
                        askDelete(
                          f.path,
                          `${f.path} · ${fmtBytes(f.size)}\n\nThis upload is deleted from the session's inbox. If the agent is still working on it, its next read will fail.`,
                          () => void doDeleteOne({ kind: "upload", path: f.path }),
                        )
                      }
                    >
                      delete
                    </button>
                  </div>
                </div>
              </div>
            ))}
        </div>
      ))}

      <h2 className="sect">Start from something you already have</h2>
      <div className="muted sm">
        Attaching links the same bytes into this session's inbox — the file is
        not uploaded again and not copied.
      </div>
      <div className="row btn-row">
        <button className="btn ghost sm" onClick={() => void toggleShelf()}>
          {showShelf ? "hide kept files" : "attach from kept"}
        </button>
        <button className="btn ghost sm" onClick={() => void openPicker()}>
          {others === null ? "attach from another session" : "hide sessions"}
        </button>
      </div>
      {showShelf && shelf === null && <div className="muted">loading…</div>}
      {showShelf && shelf && shelf.length === 0 && (
        <div className="empty">Nothing kept yet. Keep a file above and it shows up here.</div>
      )}
      {showShelf &&
        shelf &&
        shelf.length > 0 &&
        shelf.map((r) => (
          <div key={r.path} className="card">
            <div className="card-main">
              <div className="card-title">
                <span className="name">{r.path}</span>
              </div>
              <div className="card-meters">
                <span>{fmtBytes(r.size)}</span>
                <span>{fmtTime(r.mtime)}</span>
              </div>
            </div>
            <div className="row btn-row">
              <button className="btn primary sm" onClick={() => void doAttach("keep", r.path, r.path)}>
                → inbox
              </button>
              <button
                className="btn sm"
                onClick={() => {
                  void doAttach("keep", r.path, r.path).then(() => tell(r.path.split("/").pop() ?? r.path));
                }}
              >
                → inbox & tell
              </button>
            </div>
          </div>
        ))}

      {others !== null && (
        <ul className="list">
          {others.length === 0 && <div className="empty">No other sessions.</div>}
          {others.map((r) => (
            <li key={r.id} className="card">
              <div className="card-main">
                <div className="card-title">
                  <span className="name">{r.display_name ?? r.id}</span>
                </div>
                <div className="card-meters">
                  <span>{r.state}</span>
                </div>
              </div>
              <button className="btn sm" onClick={() => void pickSession(r.id)}>
                browse
              </button>
            </li>
          ))}
        </ul>
      )}
      {othersFiles && (
        <ul className="list">
          {othersFiles.outbox.length === 0 && othersFiles.uploads.length === 0 && (
            <div className="empty">That session has nothing to attach.</div>
          )}
          {othersFiles.outbox.length > 0 && (
            <div className="muted sm">its outbox — what it handed you</div>
          )}
          {othersFiles.outbox.map((f) => (
            <li key={`${othersFiles.sid}:${f.path}`} className="card">
              <div className="card-main">
                <div className="card-title">
                  <span className="name">{f.path}</span>
                </div>
                <div className="card-meters">
                  <span>{fmtBytes(f.size)}</span>
                </div>
              </div>
              <div className="row btn-row">
                <button
                  className="btn primary sm"
                  onClick={() => void doAttach(othersFiles.sid, `outbox/${f.path}`, f.path)}
                >
                  → inbox
                </button>
                <button
                  className="btn sm"
                  onClick={() => {
                    const n = f.path.split("/").pop() ?? f.path;
                    void doAttach(othersFiles.sid, `outbox/${f.path}`, f.path).then(() => tell(n));
                  }}
                >
                  → inbox & tell
                </button>
              </div>
            </li>
          ))}
          {othersFiles.uploads.length > 0 && (
            <div className="muted sm">its inbox — what its human sent it</div>
          )}
          {othersFiles.uploads.map((u) => (
            <li key={`${othersFiles.sid}:u:${u.path}`} className="card">
              <div className="card-main">
                <div className="card-title">
                  <span className="name">{u.path}</span>
                </div>
                <div className="card-meters">
                  <span>{fmtBytes(u.size)}</span>
                </div>
              </div>
              <div className="row btn-row">
                <button
                  className="btn primary sm"
                  onClick={() => void doAttach(othersFiles.sid, `uploads/${u.path}`, u.path)}
                >
                  → inbox
                </button>
                <button
                  className="btn sm"
                  onClick={() => {
                    const n = u.path.split("/").pop() ?? u.path;
                    void doAttach(othersFiles.sid, `uploads/${u.path}`, u.path).then(() => tell(n));
                  }}
                >
                  → inbox & tell
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}

      {ask && (
        <Confirm
          title={ask.title}
          detail={ask.detail}
          confirmLabel={ask.confirmLabel}
          busy={busy}
          onCancel={() => setAsk(null)}
          onConfirm={() => {
            setBusy(true);
            ask.run();
          }}
        />
      )}
    </div>
  );
}
