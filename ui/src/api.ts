import type {
  DialogsResp,
  FilesChanged,
  FilesResp,
  KeepRow,
  KeptResp,
  LibResp,
  MessagesResp,
  OutboxFile,
  SessionRow,
  SseHandler,
  UsageResp,
} from "./types";

async function jget<T>(path: string): Promise<T> {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  return (await r.json()) as T;
}

async function jpost<T>(path: string, body?: unknown): Promise<T> {
  const r = await fetch(path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!r.ok) {
    let detail = `${r.status}`;
    try {
      detail = (await r.json()).detail ?? detail;
    } catch {
      /* non-JSON */
    }
    throw new Error(`${path} -> ${detail}`);
  }
  return (await r.json()) as T;
}

// ---- sessions ----
export const listSessions = () =>
  jget<{ sessions: SessionRow[] }>("/api/v1/sessions");
export const getSession = (sid: string) => jget<SessionRow>(`/api/v1/sessions/${sid}`);
export const createSession = (display_name?: string, strict = false) =>
  jpost<{ session_id: string; slot: string }>("/api/v1/sessions", {
    display_name,
    strict,
  });
export const releaseSession = (sid: string, reason = "user close") =>
  jpost<{ released: boolean; quarantined: boolean; errors: string[] }>(
    `/api/v1/sessions/${sid}/release`,
    { reason },
  );
export const setStable = (sid: string, stable: boolean) =>
  jpost<{ ok: boolean }>(`/api/v1/sessions/${sid}/stable`, { stable });

// ---- runs ----
export const prompt = (sid: string, text: string) =>
  jpost<{ disposition: string }>(`/api/v1/sessions/${sid}/prompt`, { text });
export const steer = (sid: string, text: string) =>
  jpost<{ disposition: string }>(`/api/v1/sessions/${sid}/steer`, { text });
export const followUp = (sid: string, text: string) =>
  jpost<{ disposition: string }>(`/api/v1/sessions/${sid}/follow-up`, { text });
export const abort = (sid: string) =>
  jpost<{ ok: boolean }>(`/api/v1/sessions/${sid}/abort`);

// ---- transcript fetch (reattach: fetch, never replay) ----
export const getMessages = (sid: string) =>
  jget<MessagesResp>(`/api/v1/sessions/${sid}/messages`);
export const fork = (sid: string, entryId: string) =>
  jpost<{ forked: boolean; cancelled?: boolean; text: string }>(
    `/api/v1/sessions/${sid}/fork`,
    {
      entry_id: entryId,
    },
  );

// ---- extension dialogs (the run waits on the human) ----
export const getDialogs = (sid: string) =>
  jget<DialogsResp>(`/api/v1/sessions/${sid}/dialogs`);
export const answerDialog = (
  sid: string,
  dialogId: string,
  body: { value?: string; confirmed?: boolean; cancelled?: boolean },
) =>
  jpost<{ ok: boolean }>(
    `/api/v1/sessions/${sid}/dialogs/${dialogId}/answer`,
    body,
  );

// ---- files ----
// Percent-encode each path segment: a name with a literal "%" (legal on disk
// — the server's gate allows it) must go out as %25. encodeURI leaves "%"
// untouched, so the server decodes the URL into a different name and 404s a
// file that exists.
function encodePath(path: string): string {
  return path.split("/").map(encodeURIComponent).join("/");
}
// A module-level cache of the last listing per session (data + ETag). The
// file lists are NEVER polled: a session fetches once, then applies the
// `files_changed` deltas that arrive on its own SSE stream. The cache makes
// a remount (chat → files → chat) free instead of a second full fetch, and a
// revalidation that comes back 304 costs ~200 bytes instead of a body.
const filesCache = new Map<string, { etag: string; data: FilesResp }>();

export interface FilesLoad {
  data: FilesResp;
  notModified: boolean; // the cached listing was still current (~200 bytes)
}

export async function listFiles(sid: string, force = false): Promise<FilesLoad> {
  const cached = filesCache.get(sid);
  const headers: Record<string, string> = {};
  if (cached && !force) headers["if-none-match"] = cached.etag;
  const r = await fetch(`/api/v1/sessions/${sid}/files`, { headers });
  if (r.status === 304 && cached) return { data: cached.data, notModified: true };
  if (!r.ok) throw new Error(`/files -> ${r.status}`);
  const data = (await r.json()) as FilesResp;
  const etag = r.headers.get("etag") ?? "";
  if (etag) filesCache.set(sid, { etag, data });
  return { data, notModified: false };
}

// A delta landed on the session's stream: fold it into the cache too, so a
// remount does not refetch bytes the phone already paid for.
export function applyFilesDelta(sid: string, delta: FilesChanged): FilesResp | null {
  const cached = filesCache.get(sid);
  if (!cached) return null;
  cached.data = { ...cached.data, outbox: foldOutbox(cached.data.outbox, delta) };
  return cached.data;
}

// The one fold, used for both the view and the cache: a removed path goes,
// added/changed rows replace (a rewrite never leaves a stale twin behind).
function foldOutbox(list: OutboxFile[], delta: FilesChanged): OutboxFile[] {
  const byPath = new Map(list.map((f) => [f.path, f]));
  for (const p of delta.removed) byPath.delete(p);
  for (const f of [...delta.added, ...delta.changed]) byPath.set(f.path, f);
  return [...byPath.values()].sort((a, b) => b.mtime - a.mtime);
}

function dropFilesCache(sid: string) {
  filesCache.delete(sid);
}

export async function deleteUpload(sid: string, name: string): Promise<number> {
  const r = await fetch(
    `/api/v1/sessions/${sid}/files/uploads/${encodePath(name)}`,
    { method: "DELETE" },
  );
  if (!r.ok) throw new Error(`delete upload ${name} -> ${r.status}`);
  const j = (await r.json()) as { size: number };
  dropFilesCache(sid);
  return j.size;
}

export async function deleteOutboxFile(sid: string, path: string): Promise<number> {
  const r = await fetch(`/api/v1/sessions/${sid}/files/outbox/${encodePath(path)}`, {
    method: "DELETE",
  });
  if (!r.ok) throw new Error(`delete ${path} -> ${r.status}`);
  const j = (await r.json()) as { size: number };
  dropFilesCache(sid);
  return j.size;
}

// ---- staged (resumable) upload ----
// One request per chunk, each chunk written AT the offset the client names. A
// lost response is not a lost upload: the retry asks the server where it
// actually is (a 409 carries the number) and continues — so a radio drop at
// 3.9 GB of 4 GB costs one chunk, not the file.
//
// Progress is counted in landed chunks, not bytes-out-of-the-socket: fetch has
// no upload progress, and "the server has it" is the number worth showing
// anyway (bytes the radio has actually paid for).
//
// A dropped connection is never a cancel. cancel() is an explicit DELETE, and
// an upload nobody finishes is swept by the server's TTL — not when this tab
// closes, and not when the app restarts.
const CHUNK = 1024 * 1024;
const CHUNK_RETRIES = 3;

export interface StagedRow {
  upload_id: string;
  name: string;
  size: number;
  created: number;
  updated: number;
}

export const listStaged = (sid: string) =>
  jget<{ staged: StagedRow[] }>(`/api/v1/sessions/${sid}/staged`);

/** Ask the server to drop a staged upload. The local resume record is
 *  forgotten only once the drop is confirmed — a 404 (already gone) counts
 *  — because a failed drop leaves the bytes on the server, and the record
 *  is what lets a later visit resume the same upload instead of re-sending
 *  the file. The TTL is the sweeper of last resort either way. */
export async function cancelStaged(sid: string, uploadId: string): Promise<boolean> {
  let ok = false;
  try {
    const r = await fetch(`/api/v1/sessions/${sid}/staged/${uploadId}`, {
      method: "DELETE",
    });
    ok = r.status === 200 || r.status === 404;
  } catch {
    ok = false;
  }
  if (ok) forgetStaged(sid, uploadId);
  return ok;
}

export class UploadCancelled extends Error {
  constructor(name: string) {
    super(`cancelled: ${name}`);
    this.name = "UploadCancelled";
  }
}

// The server's staged upload is gone (TTL-swept, or complete already landed
// and its response was lost). Resuming that id can never succeed — a retry
// must open a FRESH staged upload, and this is what tells the UI so.
export class StagedGone extends Error {
  constructor(name: string) {
    super(
      `${name}: the staged upload is gone (expired, or already delivered) — retry starts it over`,
    );
    this.name = "StagedGone";
  }
}

export interface UploadTask {
  readonly uploadId: string;
  readonly name: string;
  sent(): number;
  cancel(): void;
}

interface PumpOpts {
  onProgress: (sent: number, total: number) => void;
  onTask?: (task: UploadTask) => void;
}

/** The pump both paths share: push `file`'s remaining bytes into an existing
 * staged upload, from `offset` on, one chunk at a time. */
async function pump(
  sid: string,
  uploadId: string,
  name: string,
  file: File,
  offset: number,
  { onProgress, onTask }: PumpOpts,
): Promise<{ name: string; sha256: string; size: number }> {
  let cancelled = false;
  let inflight: AbortController | null = null;
  const at = () => offset;
  const task: UploadTask = {
    uploadId,
    name,
    sent: at,
    cancel: () => {
      cancelled = true;
      inflight?.abort();
      void cancelStaged(sid, uploadId);
    },
  };
  onTask?.(task);

  while (offset < file.size) {
    if (cancelled) throw new UploadCancelled(name);
    inflight = new AbortController();
    const slice = file.slice(offset, Math.min(offset + CHUNK, file.size));
    let sent: number | null = null;
    let failure = "";
    for (let attempt = 0; attempt < CHUNK_RETRIES && sent === null; attempt++) {
      try {
        const r = await fetch(
          `/api/v1/sessions/${sid}/staged/${uploadId}/chunk?offset=${offset}`,
          {
            method: "PUT",
            headers: { "content-type": "application/octet-stream" },
            body: slice,
            signal: inflight.signal,
          },
        );
        // 200 and 409 are the ONLY answers, and both carry where the server
        // actually is: a 409 is the server correcting us (a duplicate retry
        // after a lost response, most often), and its number wins — that is
        // what makes this resumable. Any other status (a TTL-swept 404, a disk
        // 500) carries NO position: accepting "no number" as an answer would
        // let the pump fall through and complete a PARTIAL part as if it were
        // the whole file, so a non-answer is a failure to retry, never a
        // correction
        const answered = r.ok || r.status === 409;
        let pos: unknown = null;
        if (answered) pos = ((await r.json()) as { offset?: unknown }).offset;
        if (!answered || typeof pos !== "number")
          throw new Error(`chunk ${offset} -> ${r.status}`);
        sent = pos;
      } catch (e) {
        if (cancelled) throw new UploadCancelled(name);
        failure = e instanceof Error ? e.message : String(e);
        await new Promise((res) => setTimeout(res, 400 * (attempt + 1)));
      }
    }
    if (sent === null) throw new Error(`${name}: ${failure || "upload failed"}`);
    // a 409's number is the server correcting OUR bookkeeping: its part only
    // ever grows, so a number below our offset means our state is wrong (a
    // stale resume record, a lost ack) — re-send from where the server
    // actually is instead of failing an upload whose bytes are half there
    offset = sent;
    onProgress(offset, file.size);
  }
  if (cancelled) throw new UploadCancelled(name);

  const r = await fetch(`/api/v1/sessions/${sid}/staged/${uploadId}/complete`, {
    method: "POST",
  });
  if (r.status === 404) throw new StagedGone(name);
  if (!r.ok) throw new Error(`complete ${name} -> ${r.status}`);
  const done = (await r.json()) as { name: string; sha256: string; size: number };
  forgetStaged(sid, uploadId);
  dropFilesCache(sid);
  return done;
}

// A folder pick (webkitdirectory) carries the tree's shape — the relative
// path — while a single-file pick has only a name. The upload name is this,
// so a folder arrives in the inbox as the folder the human organized it into
// (same basenames in different folders coexist instead of suffixing).
export function relName(f: File): string {
  return (f as File & { webkitRelativePath?: string }).webkitRelativePath || f.name;
}

export function uploadResumable(
  sid: string,
  file: File,
  onProgress: (sent: number, total: number) => void,
  onTask?: (task: UploadTask) => void,
  name?: string,
): Promise<{ name: string; sha256: string; size: number }> {
  const n = name ?? file.name;
  return jpost<{ upload_id: string; offset: number }>(
    `/api/v1/sessions/${sid}/staged?name=${encodeURIComponent(n)}`,
  ).then((started) => {
    rememberStaged(sid, { upload_id: started.upload_id, name: n, total: file.size });
    return pump(sid, started.upload_id, n, file, started.offset, { onProgress, onTask });
  });
}

// What the browser remembers about an unfinished upload. The server knows how
// many bytes it HOLDS; only the client knew the total. Without the total a
// "resume" cannot tell a re-picked file from a smaller one that happens to
// share its name — and resuming onto that would deliver a file that is part
// one document and part another. So the total is recorded here, and a resume
// is only offered when name AND total match.
export interface StagedRecord {
  upload_id: string;
  name: string;
  total: number;
}

const STAGED_KEY = (sid: string) => `servitor-staged:${sid}`;

export function localStaged(sid: string): StagedRecord[] {
  try {
    const raw = localStorage.getItem(STAGED_KEY(sid));
    const parsed = raw ? (JSON.parse(raw) as StagedRecord[]) : [];
    return Array.isArray(parsed) ? parsed.filter((r) => r && r.upload_id && r.total > 0) : [];
  } catch {
    return [];
  }
}

function writeStaged(sid: string, rows: StagedRecord[]) {
  try {
    localStorage.setItem(STAGED_KEY(sid), JSON.stringify(rows));
  } catch {
    /* private mode / quota: resume is then simply not offered across a reload */
  }
}

export function rememberStaged(sid: string, rec: StagedRecord): void {
  writeStaged(sid, [...localStaged(sid).filter((r) => r.upload_id !== rec.upload_id), rec]);
}

export function forgetStaged(sid: string, uploadId: string): void {
  writeStaged(sid, localStaged(sid).filter((r) => r.upload_id !== uploadId));
}

/** Per-session localStorage (the kept-badge map, the staged-upload records)
 * is keyed by session id. When a session leaves the list — release, idle GC,
 * the FREE row deleted — its keys are residue: nothing will ever read them,
 * and a badge that survives a release points at a shelf path for a session
 * that is gone. The list is the authority on which sessions are alive, so a
 * successful fetch drops the keys for ids that are not. A FAILED fetch must
 * not drop anything: an error would otherwise read as "every session dead".
 */
export function pruneGoneSessionStorage(liveIds: string[]): void {
  try {
    const live = new Set(liveIds);
    for (const key of Object.keys(localStorage)) {
      const m = /^servitor-(?:kept|staged|lib):(.+)$/.exec(key);
      if (m && !live.has(m[1])) localStorage.removeItem(key);
    }
  } catch {
    /* private mode: the residue is then simply per-mount */
  }
}

/** Finish an upload the server still holds (the tab closed, the app restarted,
 * the radio gave up mid-chunk). The bytes already there are kept; the rest come
 * from the file the human re-picks — which must be the same file: same name,
 * same total size. A smaller or renamed file is refused rather than spliced
 * onto the end of someone else's upload. */
export function resumeStaged(
  sid: string,
  rec: StagedRecord,
  file: File,
  onProgress: (sent: number, total: number) => void,
  onTask?: (task: UploadTask) => void,
): Promise<{ name: string; sha256: string; size: number }> {
  if (relName(file) !== rec.name || file.size !== rec.total) {
    return Promise.reject(
      new Error(`pick ${rec.name} (${rec.total} bytes) again — not ${file.name}`),
    );
  }
  return listStaged(sid).then((live) => {
    const held = live.staged.find((r) => r.upload_id === rec.upload_id);
    if (!held) throw new StagedGone(rec.name);
    // size is the server's on-disk count; 0 when no chunk has landed yet.
    // Resuming from 0 re-sends from the start and the 409-with-server-size
    // dance corrects it — an unknown offset would deliver a 0-byte file.
    const from = held.size ?? 0;
    if (from > rec.total) throw new Error(`${rec.name}: the staged bytes do not fit`);
    return pump(sid, held.upload_id, rec.name, file, from, { onProgress, onTask });
  });
}

export async function renameFile(
  sid: string,
  from: string, // "outbox/<path>" or "uploads/<path>"
  to: string, // same area, e.g. "outbox/v2/report.md"
): Promise<{ from: string; to: string; size: number }> {
  const url = from.startsWith("uploads/")
    ? `/api/v1/sessions/${sid}/files/uploads/${encodePath(from.slice("uploads/".length))}`
    : `/api/v1/sessions/${sid}/files/outbox/${encodePath(from.slice("outbox/".length))}`;
  const r = await fetch(url, {
    method: "PATCH",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ to }),
  });
  if (!r.ok) {
    let detail = `${r.status}`;
    try {
      detail = (await r.json()).detail ?? detail;
    } catch {
      /* non-JSON */
    }
    throw new Error(detail);
  }
  dropFilesCache(sid);
  return (await r.json()) as { from: string; to: string; size: number };
}

// One file, a folder, an area — or a selection (repeated `path=`; the
// server archives exactly the entries it resolves, each through the same
// download gate, and names the zip for their deepest shared directory).
export const archiveUrl = (sid: string, path: string | string[]) =>
  `/api/v1/sessions/${sid}/files/archive?` +
  (Array.isArray(path) ? path : [path]).map((p) => `path=${encodePath(p)}`).join("&");


export const downloadUrl = (sid: string, path: string) =>
  `/api/v1/sessions/${sid}/files/${encodePath(path)}`;

// ---- the kept shelf ----
// Kept bytes live outside every session area: they survive release and are
// never swept, expired, or overwritten. The shelf is one screen and one
// fetch — no polling (it changes only when the human acts on it).
// The shelf is one listing across all sessions, so it gets the same
// ETag/304 treatment as the per-session file lists: a revalidation that
// comes back unchanged costs ~200 bytes, not the whole shelf. A stale etag
// is harmless — the server only answers 304 for a byte-identical body — so
// nothing has to invalidate the cache on a keep or an unkeep.
let keptEtag = "";
let keptRows: KeepRow[] = [];

export async function listKept(): Promise<KeptResp> {
  const headers: Record<string, string> = {};
  if (keptEtag) headers["if-none-match"] = keptEtag;
  const r = await fetch(`/api/v1/files`, { headers });
  if (r.status === 304) return { keep: keptRows };
  if (!r.ok) throw new Error(`/files -> ${r.status}`);
  const data = (await r.json()) as KeptResp;
  keptEtag = r.headers.get("etag") ?? "";
  keptRows = data.keep;
  return data;
}

export async function keepFile(
  sid: string,
  path: string,
  asName?: string,
): Promise<KeepRow & { sha256: string; ts: number }> {
  const r = await fetch(`/api/v1/sessions/${sid}/files/keep`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ path, as_name: asName ?? null }),
  });
  if (!r.ok) throw new Error(`keep ${path} -> ${r.status}`);
  return await r.json();
}

export async function attachFile(
  sid: string,
  source: string,
  path: string,
  asName?: string,
): Promise<{ name: string; sha256: string; size: number; mime: string }> {
  const r = await fetch(`/api/v1/sessions/${sid}/files/attach`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ source, path, as_name: asName ?? null }),
  });
  if (!r.ok) throw new Error(`attach ${path} -> ${r.status}`);
  return await r.json();
}

export async function deleteKept(path: string): Promise<number> {
  const r = await fetch(`/api/v1/files/keep/${encodePath(path)}`, { method: "DELETE" });
  if (!r.ok) throw new Error(`unkeep ${path} -> ${r.status}`);
  return ((await r.json()) as { size: number }).size;
}

export const keptDownloadUrl = (path: string) => `/api/v1/files/keep/${encodePath(path)}`;

// ---- the shared library (lib/) ----
// The library is global (one per app, mounted read-only into every
// session's workdir), so it is one listing with the same ETag/304
// treatment as the kept shelf: a revalidation that comes back unchanged
// costs ~200 bytes, not the whole tree.
let libEtag = "";
let libRows: LibResp["lib"] = [];

export async function listLib(): Promise<LibResp> {
  const headers: Record<string, string> = {};
  if (libEtag) headers["if-none-match"] = libEtag;
  const r = await fetch(`/api/v1/files/lib`, { headers });
  if (r.status === 304) return { lib: libRows };
  if (!r.ok) throw new Error(`/files/lib -> ${r.status}`);
  const data = (await r.json()) as LibResp;
  libEtag = r.headers.get("etag") ?? "";
  libRows = data.lib;
  return data;
}

export async function saveToLib(
  sid: string,
  path: string,
  asName?: string,
): Promise<{ path: string; sha256: string; size: number; ts: number }> {
  const r = await fetch(`/api/v1/sessions/${sid}/files/lib`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ path, as_name: asName ?? null }),
  });
  if (!r.ok) throw new Error(`save-to-lib ${path} -> ${r.status}`);
  return await r.json();
}

export async function deleteLibFile(path: string): Promise<number> {
  const r = await fetch(`/api/v1/files/lib/${encodePath(path)}`, { method: "DELETE" });
  if (!r.ok) throw new Error(`delete lib ${path} -> ${r.status}`);
  return ((await r.json()) as { size: number }).size;
}

export const libDownloadUrl = (path: string) => `/api/v1/files/lib/${encodePath(path)}`;

// ---- byte meters (a report, never a limit) ----
export const usage = () => jget<UsageResp>("/api/v1/usage");

// ---- the app-wide stream (so the sessions list never polls) ----
// Lifecycle transitions and run state for every session on one stream.
// Without it the list screen refetches every few seconds to learn that a run
// started — the single biggest steady-state cost on a metered phone.
// The names list is the UI half of the GLOBAL_EVENTS contract in
// app/lifecycle.py — make check asserts the two agree (gap is the
// transport's resync signal, the one allowed extra).
export function subscribeGlobal(
  onEvent: SseHandler,
  onGap: () => void,
  kinds?: string[],
): () => void {
  // head=1: no replay. The list wants "did anything change?", and a reconnect
  // still resumes by Last-Event-ID (the browser replays that header).
  //
  // `kinds` is a per-subscription VIEW of the stream: the server delivers
  // only the named kinds (plus `gap` — the stream's own resync frame, which
  // rides whatever the view is), so a screen that needs two kinds does not
  // pay the wire bytes for the rest. The names list below stays the FULL
  // contract set — make check pairs it with GLOBAL_EVENTS; a view is a
  // subset, never a rename.
  const view = kinds && kinds.length > 0 ? kinds : undefined;
  const names = [
    "started",
    "release_start",
    "released",
    "respawned",
    "idle_killed",
    "interrupted",
    "drained",
    "spawn_fatal",
    "slot_quarantined",
    "new_extension",
    "extension_dialog",
    "extension_dialog_resolved",
    "agent_start",
    "agent_end",
    "agent_settled",
    "file_uploaded",
    "file_kept",
    "lib_saved",
    "lib_deleted",
    "file_deleted",
    "file_renamed",
    "keep_deleted",
    "update_applying",
    "update_restarted",
    "gap",
  ];
  const wanted = view
    ? names.filter((n) => n === "gap" || view.includes(n))
    : names;
  const es = new EventSource(
    `/api/v1/events?head=1${view ? `&kinds=${view.join(",")}` : ""}`,
  );
  return _bind(es, wanted, onEvent, onGap);
}

function _bind(
  es: EventSource,
  names: string[],
  onEvent: SseHandler,
  onGap: () => void,
): () => void {
  const handlers: [string, EventListener][] = names.map((n) => [
    n,
    (ev) => {
      const m = ev as MessageEvent;
      const id = m.lastEventId ? parseInt(m.lastEventId, 10) : null;
      let data: unknown = {};
      try {
        data = JSON.parse(m.data);
      } catch {
        /* keep raw */
      }
      if (n === "gap") onGap();
      else onEvent(n, data, id);
    },
  ]);
  for (const [n, h] of handlers) es.addEventListener(n, h);
  es.addEventListener("error", () => {
    /* EventSource auto-reconnects (with Last-Event-ID); the gap
       event is the resync signal if the ring moved on */
  });
  return () => es.close();
}

// ---- SSE live tail ----
// One EventSource per open chat. after = the snapshot cursor (the fetch
// and the cursor are atomic server-side); the browser re-sends
// Last-Event-ID on every (re)connect, so a dropped connection resumes
// exactly where it stopped. A `gap` event means the ring evicted past
// our position: the caller re-fetches /messages and re-subscribes.
export function subscribeEvents(
  sid: string,
  after: number,
  onEvent: SseHandler,
  onGap: () => void,
): () => void {
  const es = new EventSource(`/api/v1/sessions/${sid}/events?after=${after}`);
  const names: (string)[] = [
    "agent_start",
    "agent_end",
    "agent_settled",
    "turn_start",
    "turn_end",
    "message_start",
    "message_update",
    "message_end",
    "tool_execution_start",
    "tool_execution_update",
    "tool_execution_end",
    "queue_update",
    "started",
    "respawned",
    "idle_killed",
    "interrupted",
    "released",
    "new_extension",
    "extension_dialog",
    "extension_dialog_answered",
    "extension_dialog_resolved",
    "file_uploaded",
    "file_kept",
    "lib_saved",
    "files_changed",
    "file_deleted",
    "file_renamed",
    "gap",
  ];
  return _bind(es, names, onEvent, onGap);
}
