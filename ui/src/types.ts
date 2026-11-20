// Shared API types.

export interface SessionRow {
  id: string;
  display_name: string | null;
  slot: string | null; // "waiting" while queued for a slot
  state: string; // allocated | live | draining | cleaning | free
  stable: boolean;
  interrupted: boolean;
  session_file: string | null;
  config_version: string | null;
  junk_bytes: number | null;
  waiting: boolean;
  update_waiting: boolean;
  new_extensions: string[];
  last_activity: number | null;
  live_process: boolean;
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

// --- SSE (the live tail; the UI NEVER replays — it fetches, then
// follows events from the snapshot cursor; the browser's EventSource
// re-sends Last-Event-ID on reconnect, so a dropped phone loses
// nothing) ---

export type SseHandler = (name: string, data: unknown, id: number | null) => void;
