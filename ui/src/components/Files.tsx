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
// unchanged one costs ~200 bytes. A keep or a save (possibly from another
// tab) changes no row; the event carries the source→dest mapping, so the
// badge is set without a fetch — only an open shelf revalidates (its new
// row's size/mtime are not in the event).
export interface FileEvent {
  seq: number;
  delta: FilesChanged | null;
  kind?: string;
  // `lib_saved` / `file_kept`: the JSON mapping the event carries
  // (source → lib path / shelf path) — a save or keep from another tab
  // sets the badge directly, no fetch
  detail?: string;
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

// The badge record in localStorage is shared across tabs of the same
// browser, but a tab's in-memory copy carries only what its own events
// told it: a whole-map write would wipe entries a sibling tab recorded
// since (a keep or save it made), and they would die on the next remount.
// A write therefore applies the update's delta to the record as stored:
// added or changed keys take this tab's values, keys the update dropped
// — verified stale here, the row/shelf/lib no longer backs them — are
// deleted, and entries this tab has no opinion on survive.
function readBadgeRecord(key: string): Record<string, string> {
  try {
    const parsed = JSON.parse(localStorage.getItem(key) ?? "{}");
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

function writeBadgeRecord(
  key: string,
  cur: Record<string, string>,
  next: Record<string, string>,
): void {
  try {
    const merged: Record<string, string> = { ...readBadgeRecord(key) };
    for (const [src, dest] of Object.entries(next)) merged[src] = dest;
    for (const src of Object.keys(merged))
      if (!(src in next) && src in cur) delete merged[src];
    const out = JSON.stringify(merged);
    // a sibling tab may have applied the same change first — the record
    // already is `out`, and the write is a no-op
    if (out !== (localStorage.getItem(key) ?? "{}")) localStorage.setItem(key, out);
  } catch {
    /* private mode: the badge is simply per-mount */
  }
}

// Prune the stored record against a fresh listing: a badge a now-closed
// tab recorded lives in the store, in no tab's memory, so the in-memory
// prune cannot reach it — only a prune that reads the store can. The
// loaded rows verify whatever tab recorded the entry: the server keeps
// and saves only a source it has, so a fresh sibling badge always names
// a row the listing carries. Writes back only when something goes.
function pruneStoredBadges(key: string, sources: Set<string>): void {
  try {
    const stored = readBadgeRecord(key);
    let changed = false;
    const next: Record<string, string> = {};
    for (const [src, dest] of Object.entries(stored))
      if (sources.has(src)) next[src] = dest;
      else changed = true; // row gone — the badge goes, whoever recorded it
    if (changed) localStorage.setItem(key, JSON.stringify(next));
  } catch {
    /* private mode: the badge is simply per-mount */
  }
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
  // a second copy under a suffix. Two truths: the shelf — when it is
  // loaded, a badge whose recorded shelf path is gone is unkept and goes —
  // and the row: a badge whose source row is gone goes with it (the row
  // effect below, as for in-lib).
  const keptKey = `servitor-kept:${sid}`;
  const [kept, setKept] = useState<Record<string, string>>(() => readBadgeRecord(keptKey));
  const updateKept = (updater: (cur: Record<string, string>) => Record<string, string>) =>
    setKept((cur) => {
      const next = updater(cur);
      // no-op updaters return `cur` itself: the UI pays for change, so a
      // listing change that pruned nothing writes nothing (and stringifies
      // nothing)
      if (next !== cur) writeBadgeRecord(keptKey, cur, next);
      return next;
    });
  // source paths already saved to the shared library ("outbox/<rel>" |
  // "uploads/<name>" → the lib path the server landed it under). Same
  // persistence and truth rules as the kept-badge: the library listing is
  // the authority, and a badge whose lib path is gone is unsaved and goes.
  const libKey = `servitor-lib:${sid}`;
  const [inLib, setInLib] = useState<Record<string, string>>(() => readBadgeRecord(libKey));
  const updateInLib = (updater: (cur: Record<string, string>) => Record<string, string>) =>
    setInLib((cur) => {
      const next = updater(cur);
      // same no-op contract as updateKept: `next === cur` means nothing
      // changed, so no write
      if (next !== cur) writeBadgeRecord(libKey, cur, next);
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
    // badges are per-mount residue with the library as their truth: if any
    // survived (a delete on the Library screen, a sync from the host), one
    // ETag'd fetch settles them — a 304 costs ~200 bytes, no badges no fetch
    if (Object.keys(inLib).length > 0) {
      void api
        .listLib()
        .then(({ lib }) => reconcileInLib(lib))
        .catch(() => {});
    }
  }, [load]); // eslint-disable-line react-hooks/exhaustive-deps

  // deltas from the session's stream (the parent owns the socket)
  useEffect(() => {
    if (!ev) return;
    if (ev.delta) {
      const folded = api.applyFilesDelta(sid, ev.delta);
      if (folded) setOutbox(folded.outbox);
      else void load(true); // no cache to fold into (first load raced it)
    } else if (ev.kind === "file_kept") {
      // a keep — possibly from another tab — changes no listing row; what
      // goes stale is a kept-badge. The event carries the source→shelf
      // mapping (JSON detail), so the badge is set directly with no fetch
      // (the shelf listing cannot map a shelf path back to its source);
      // the open shelf still revalidates — the new row's size/mtime are
      // not in the event, and the reconcile drops any badge the shelf no
      // longer backs
      let parsed: { source?: unknown; path?: unknown } | null = null;
      try {
        parsed = ev.detail ? JSON.parse(ev.detail) : null;
      } catch {
        parsed = null;
      }
      const source = parsed?.source;
      const shelfPath = parsed?.path;
      if (typeof source === "string" && typeof shelfPath === "string" && source && shelfPath) {
        updateKept((cur) =>
          cur[source] === shelfPath ? cur : { ...cur, [source]: shelfPath },
        );
      }
      if (shelf !== null) {
        void api
          .listKept()
          .then(({ keep }) => {
            setShelf(keep);
            reconcileKept(keep);
          })
          .catch((e) => setErr(String(e)));
      }
    } else if (ev.kind === "lib_saved") {
      // a save — possibly from another tab. The event carries the
      // source→lib mapping (JSON detail), so the badge is set directly with
      // no fetch; a DELETE still arrives as the global lib_deleted event,
      // which names the lib path and drops its badge
      let parsed: { source?: unknown; path?: unknown } | null = null;
      try {
        parsed = ev.detail ? JSON.parse(ev.detail) : null;
      } catch {
        parsed = null;
      }
      const source = parsed?.source;
      const libPath = parsed?.path;
      if (typeof source === "string" && typeof libPath === "string" && source && libPath) {
        updateInLib((cur) =>
          cur[source] === libPath ? cur : { ...cur, [source]: libPath },
        );
      }
    } else {
      // revalidate through the ETag (not force): the event names a change
      // the listing will have to carry, but a stale duplicate event must
      // cost ~200 bytes, not the whole body
      void load();
    }
    // ev.seq is the trigger; the handlers below are stable per session
  }, [ev?.seq]); // eslint-disable-line react-hooks/exhaustive-deps

  // a library DELETE arrives as a GLOBAL event (the session stream cannot
  // carry it), so a badge would sit stale while this tab stays open. While
  // any badge exists, hold one narrow global stream (viewed to `lib_deleted`):
  // the event names the deleted path, so the badge is dropped with no fetch
  // (the same contract the Kept screen has for its rows). No badges, no
  // stream; a hidden tab closes it (the UI pays for change, never for
  // waiting), and show reconciles through the ETag — a delete that landed
  // while hidden settles there, and an unchanged answer is a 304
  const hasLibBadges = Object.keys(inLib).length > 0;
  useEffect(() => {
    if (!hasLibBadges) return;
    let unsub: (() => void) | null = null;
    const attach = () => {
      if (unsub) return;
      unsub = api.subscribeGlobal(
        (name, data) => {
          if (name !== "lib_deleted") return;
          const p = (data as { detail?: string } | null)?.detail;
          if (!p) return;
          updateInLib((cur) => {
            if (!Object.values(cur).includes(p)) return cur;
            const next: Record<string, string> = {};
            for (const [src, libPath] of Object.entries(cur))
              if (libPath !== p) next[src] = libPath;
            return next;
          });
        },
        () => {
          // a gap evicted the delete we were waiting for: settle against
          // the listing (ETag'd)
          void api.listLib().then(({ lib }) => reconcileInLib(lib)).catch(() => {});
        },
        ["lib_deleted"],
      );
    };
    const onVis = () => {
      if (document.hidden) {
        unsub?.();
        unsub = null;
      } else {
        void api.listLib().then(({ lib }) => reconcileInLib(lib)).catch(() => {});
        attach();
      }
    };
    document.addEventListener("visibilitychange", onVis);
    // a tab that mounts HIDDEN holds no stream: the first show reconciles
    // (a lib_deleted that landed in the dark settles against the listing)
    // and then attaches — the UI pays for change, never for waiting
    if (!document.hidden) attach();
    return () => {
      unsub?.();
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [hasLibBadges]); // eslint-disable-line react-hooks/exhaustive-deps

  // a badge is only as good as its row — kept and in-lib alike: when a
  // source leaves the listing — a delete that landed from another tab or
  // device (its delta folds in, or its revalidate reloads the rows) — the
  // badge goes with it, the same rule the local deletes apply. An orphaned
  // badge would mislabel a NEW file later written to the same path (a
  // different artifact the keep/save does not cover) and, for in-lib, hold
  // the lib_deleted stream open for a row that is gone. The rows the view
  // just loaded are the truth; a first load in flight (outbox null) prunes
  // nothing
  useEffect(() => {
    if (outbox === null) return;
    const sources = new Set<string>([
      ...outbox.map((f) => `outbox/${f.path}`),
      ...uploads.map((u) => `uploads/${u.name}`),
    ]);
    const pruneToRows = (cur: Record<string, string>) => {
      const next: Record<string, string> = {};
      for (const [src, dest] of Object.entries(cur))
        if (sources.has(src)) next[src] = dest;
      return Object.keys(next).length === Object.keys(cur).length ? cur : next;
    };
    updateInLib(pruneToRows);
    updateKept(pruneToRows);
    // and the store itself: a badge a now-closed tab recorded is in no
    // tab's memory, so the in-memory prune above cannot reach it — the
    // stored record is pruned against these same fresh rows
    pruneStoredBadges(keptKey, sources);
    pruneStoredBadges(libKey, sources);
  }, [outbox, uploads]); // eslint-disable-line react-hooks/exhaustive-deps

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
      updateKept((cur) =>
        cur[source] === row.path ? cur : { ...cur, [source]: row.path },
      );
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

  // drop in-lib badges the library no longer backs (a delete from the
  // Library screen, a host sync that re-pushed the tree)
  function reconcileInLib(rows: { path: string }[]) {
    const alive = new Set(rows.map((r) => r.path));
    updateInLib((cur) => {
      const next: Record<string, string> = {};
      for (const [src, libPath] of Object.entries(cur))
        if (alive.has(libPath)) next[src] = libPath;
      return Object.keys(next).length === Object.keys(cur).length ? cur : next;
    });
  }

  async function doSaveToLib(source: string, label: string) {
    try {
      const row = await api.saveToLib(sid, source);
      updateInLib((cur) =>
        cur[source] === row.path ? cur : { ...cur, [source]: row.path },
      );
      setNote(
        `saved ${label} to lib/ as ${row.path} — every session's agent can read it (read-only)`,
      );
    } catch (e) {
      setErr(String(e));
    }
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
      // the in-lib badge for the source goes with the row: the library copy
      // survives (the Library screen still shows it), but a badge with no
      // row to render on would hold the lib_deleted stream open forever —
      // and a NEW file later written to the same path is a different
      // artifact the old save does not cover
      const srcKey = `${target.kind === "outbox" ? "outbox" : "uploads"}/${target.path}`;
      updateInLib((cur) => {
        if (!(srcKey in cur)) return cur;
        const next = { ...cur };
        delete next[srcKey];
        return next;
      });
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
    const gone: string[] = [];
    for (const p of selectedPaths) {
      try {
        freed += await api.deleteOutboxFile(sid, p);
        gone.push(p);
      } catch {
        failed++;
      }
    }
    // the in-lib badges for the deleted sources go with the rows (the
    // doDeleteOne rule); only the paths that actually went are stale
    if (gone.length > 0) {
      updateInLib((cur) => {
        const stale = new Set(gone.map((p) => `outbox/${p}`));
        const next: Record<string, string> = {};
        for (const [src, libPath] of Object.entries(cur))
          if (!stale.has(src)) next[src] = libPath;
        return Object.keys(next).length === Object.keys(cur).length ? cur : next;
      });
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
      // the kept and in-lib badges are keyed by the source path: a renamed
      // file keeps both under the NEW path (the shelf and library entries
      // they point at are untouched), never a stale key the truth can no
      // longer match against. A stale in-lib key is worse than cosmetic — it
      // would hold the lib_deleted stream open for a badge no row can show.
      // Both areas report a path within the area; the area goes back on as
      // the key
      const newFrom = `${area}/${moved.to}`;
      if (newFrom !== rename.from) {
        const oldKey = rename.from;
        updateKept((cur) => {
          if (!(oldKey in cur)) return cur;
          const next = { ...cur, [newFrom]: cur[oldKey] };
          delete next[oldKey];
          return next;
        });
        updateInLib((cur) => {
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
                    {inLib[`outbox/${f.path}`] ? (
                      <span className="chip stable sm-chip" title={`in lib as ${inLib[`outbox/${f.path}`]} (read-only to the agent)`}>
                        in lib
                      </span>
                    ) : (
                      <button
                        className="btn sm"
                        title="save into the shared library — every session's agent can read it (read-only to them; no extra bytes)"
                        onClick={() => void doSaveToLib(`outbox/${f.path}`, f.path)}
                      >
                        → lib
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
                    {inLib[`uploads/${f.path}`] ? (
                      <span className="chip stable sm-chip" title={`in lib as ${inLib[`uploads/${f.path}`]} (read-only to the agent)`}>
                        in lib
                      </span>
                    ) : (
                      <button
                        className="btn sm"
                        title="save into the shared library — every session's agent can read it (read-only to them; no extra bytes)"
                        onClick={() => void doSaveToLib(`uploads/${f.path}`, f.path.split("/").pop() ?? f.path)}
                      >
                        → lib
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
