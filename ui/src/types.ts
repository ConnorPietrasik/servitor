// Shared API types.

export interface SessionRow {
  id: string;
  display_name: string | null;
  slot: string; // the static slot this session pins
  state: string; // allocated | live | draining | cleaning | free
  busy: boolean; // a run is in flight (server: driver.run_active)
  stable: boolean;
  strict: boolean; // strict session: read-only tools, no web (create-time)
  interrupted: boolean;
  session_file: string | null;
  config_version: string | null;
  update_waiting: boolean;
  new_extensions: string[];
  last_activity: number | null;
  live_process: boolean;
  dialogs_pending: number; // extension dialogs waiting on the human
}

export interface ToolCall {
  id: string;
  name: string;
  arguments: Record<string, unknown>;
}

export interface Msg {
  id: string;
  parent_id: string | null;
  timestamp: string | null;
  role: "user" | "assistant" | "tool_result";
  text: string;
  thinking: string;
  thinking_signature: string;
  tool_calls: ToolCall[];
  tool_call_id?: string;
  tool_name?: string;
  is_error?: boolean;
  result?: string;
  model?: string;
  usage?: Record<string, unknown>;
}

export interface MessagesResp {
  leaf_id: string | null;
  cursor: number; // hub event id at snapshot time (SSE ?after=cursor)
  messages: Msg[];
}

export interface OutboxFile {
  path: string;
  size: number;
  mtime: number;
  sha256: string | null; // null while the background hash runs
  mime: string | null; // null until the same background pass lands (the UI does not render outbox mime; downloads get the blocking value)
  // false while the agent is probably still writing it (a METER: an unstable
  // file downloads the same; the badge only tells you what you are about to
  // catch mid-write)
  stable?: boolean;
}

// A file-change delta from the server's sweep — the only thing the file
// lists consume after their first fetch (the UI never polls them).
export interface FilesChanged {
  added: OutboxFile[];
  changed: OutboxFile[];
  removed: string[];
  at: number;
}

export interface UploadRow {
  // the path the file LANDED under: a folder upload keeps its tree
  // ("photos/a.jpg"), a single file is its name ("a.jpg")
  name: string;
  sha256: string;
  size: number;
  mime: string;
  ts: number;
}

export interface FilesResp {
  outbox: OutboxFile[];
  uploads: UploadRow[];
}

// --- the kept shelf (bytes a human chose to keep past a session) ------
export interface KeepRow {
  path: string; // relpath under the shelf, e.g. "s1/report.md"
  size: number;
  mtime: number;
  sha256?: string | null; // the store's name for the bytes; null while the background hash runs
  mime: string;
}

export interface KeptResp {
  keep: KeepRow[];
}

// --- the shared library (lib/): human-curated, mounted read-only into
// every session's workdir; the agent reads it, humans save into it ------
export interface LibRow {
  path: string; // relpath under lib, e.g. "scripts/foo.sh"
  size: number;
  mtime: number;
  sha256?: string | null; // the store's name for the bytes (a save is a blob link); null for a repo-pushed file — the listing never hashes it
  mime: string;
}

export interface LibResp {
  lib: LibRow[];
}

// --- state-volume byte meters (a report, never a limit) ---------------
export interface UsageArea {
  bytes: number;
  files: number;
}

export interface UsageResp {
  areas: Record<string, UsageArea>;
  by_session: Record<string, Record<string, number>>;
  shared_bytes: number;
  loose_bytes: number;
  fs: { total?: number; free?: number; used?: number };
  at: number;
  // the idle-GC threshold the server is running: shown next to a session's
  // bytes so "pin it or lose it" is visible before it happens
  gc_idle_days?: number | null;
}

// --- extension dialogs (the run blocks until the human answers or
// aborts; the dialog dies with the process) ---

export interface ExtensionDialog {
  id: string;
  method: "select" | "confirm" | "input" | "editor";
  title?: string;
  message?: string;
  options?: string[];
  placeholder?: string;
  prefill?: string;
  timeout?: number; // ms; pi auto-resolves with undefined when it elapses
}

export interface DialogsResp {
  dialogs: ExtensionDialog[];
}

// --- SSE (the live tail; the UI NEVER replays — it fetches, then
// follows events from the snapshot cursor; the browser's EventSource
// re-sends Last-Event-ID on reconnect, so a dropped phone loses
// nothing) ---

export type SseHandler = (name: string, data: unknown, id: number | null) => void;
