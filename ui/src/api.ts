import type {
  DialogsResp,
  FilesResp,
  MessagesResp,
  SessionRow,
  SseHandler,
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
export const createSession = (display_name?: string) =>
  jpost<{ session_id: string; slot: string }>("/api/v1/sessions", {
    display_name,
  });
export const releaseSession = (sid: string, reason = "user close") =>
  jpost<{ released: boolean }>(`/api/v1/sessions/${sid}/release`, { reason });
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
export const listFiles = (sid: string) => jget<FilesResp>(`/api/v1/sessions/${sid}/files`);

export async function uploadFile(
  sid: string,
  name: string,
  blob: Blob,
): Promise<{ sha256: string; size: number }> {
  // raw body upload (the API has no size cap; no 413, ever). Per-file
  // status is surfaced by the composer around this call.
  const r = await fetch(`/api/v1/sessions/${sid}/files/upload?name=${encodeURIComponent(name)}`, {
    method: "POST",
    headers: { "content-type": "application/octet-stream" },
    body: blob,
  });
  if (!r.ok) throw new Error(`upload ${name} -> ${r.status}`);
  return (await r.json()) as { sha256: string; size: number };
}

export const downloadUrl = (sid: string, path: string) =>
  `/api/v1/sessions/${sid}/files/${encodeURI(path)}`;

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
    "gap",
  ];
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
