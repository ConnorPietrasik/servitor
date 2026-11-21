// Shared API types.

export interface SessionRow {
  id: string;
  display_name: string | null;
  slot: string; // the static slot this session pins
  state: string; // allocated | live | draining | cleaning | free
  busy: boolean; // a run is in flight (server: driver.run_active)
  stable: boolean;
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
  mime: string;
}

export interface UploadRow {
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
