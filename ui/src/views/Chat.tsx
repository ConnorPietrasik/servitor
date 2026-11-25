import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import * as api from "../api";
import type {
  ExtensionDialog,
  FilesChanged,
  Msg,
  SessionRow,
  ToolCall,
} from "../types";
import Message from "../components/Message";
import Files, { type FileEvent } from "../components/Files";
import Dialogs from "../components/Dialogs";
import { fmtBytes } from "../format";

// ---- client settings (persisted locally; rendering-only) -------------
interface Settings {
  show_thinking: boolean;
  coalesce: number; // ms between render flushes (slow phones)
}
function loadSettings(): Settings {
  try {
    const raw = localStorage.getItem("servitor.settings");
    if (raw) return { show_thinking: true, coalesce: 150, ...JSON.parse(raw) };
  } catch {
    /* first run */
  }
  return { show_thinking: true, coalesce: 150 };
}

interface ToolLine {
  toolCallId: string;
  name: string;
  args: Record<string, unknown>;
  status: "running" | "done" | "error";
  result?: string;
  durationMs?: number;
}

// fingerprint for snapshot/live dedup (events carry no entry ids):
// role + text + thinking. A completed message already in the fetched
// snapshot is never appended twice.
function fp(m: { role: string; text: string; thinking: string }): string {
  return `${m.role}|${m.text}|${m.thinking}`;
}

let seq = 0;
const nextSeq = () => ++seq;

export default function Chat() {
  const { sid } = useParams<{ sid: string }>();
  const nav = useNavigate();
  const idRef = useRef(sid);
  idRef.current = sid;

  const [row, setRow] = useState<SessionRow | null>(null);
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [tools, setTools] = useState<ToolLine[]>([]);
  const [running, setRunning] = useState(false);
  const [subview, setSubview] = useState<"chat" | "files">("chat");
  // the last file-change from this session's stream, handed to the Files
  // view: a delta to fold in, or null meaning "revalidate" (the phone itself
  // deleted something, or the ring gapped). The Files view never polls.
  const [fileEv, setFileEv] = useState<FileEvent | null>(null);
  const [settings, setSettings] = useState<Settings>(loadSettings);
  const [composerPrefill, setComposerPrefill] = useState<string | null>(null);
  const [banner, setBanner] = useState<string | null>(null);
  const [closing, setClosing] = useState(false);
  const [dialogs, setDialogs] = useState<ExtensionDialog[]>([]);
  // one row per upload in this tab: progress is in LANDED bytes (what the
  // radio paid for), cancel is explicit, and a failed row keeps its File so
  // "retry" continues from the server's offset instead of starting over
  const [dropping, setDropping] = useState(false);
  const [jobs, setJobs] = useState<
    {
      id: string;
      name: string;
      total: number;
      sent: number;
      status: "uploading" | "done" | "failed" | "cancelled";
      error?: string;
    }[]
  >([]);
  const jobFiles = useRef<Record<string, File>>({});
  const jobTasks = useRef<Record<string, api.UploadTask>>({});
  const [pending, setPending] = useState<api.StagedRecord[]>([]);

  const msgsRef = useRef<Msg[]>([]);
  msgsRef.current = msgs;
  const settingsRef = useRef(settings);
  settingsRef.current = settings;
  const queue = useRef<{ name: string; data: unknown }[]>([]);
  const flushTimer = useRef<number | null>(null);
  const unsubRef = useRef<(() => void) | null>(null);
  const runningRef = useRef(false);
  runningRef.current = running;

  // ---- live event application -----------------------------------------
  const applyEvent = useCallback((name: string, data: unknown) => {
    const d = (data ?? {}) as Record<string, any>;
    switch (name) {
      case "files_changed":
        setFileEv((c) => ({ seq: (c?.seq ?? 0) + 1, delta: d as unknown as FilesChanged }));
        break;
      case "file_kept":
        // the listing is untouched by a keep; what may go stale is a
        // kept-badge (a keep from another tab). Named, so the Files view
        // refreshes the shelf it is showing instead of revalidating rows
        setFileEv((c) => ({ seq: (c?.seq ?? 0) + 1, delta: null, kind: "file_kept" }));
        break;
      case "file_deleted":
      case "file_uploaded":
      case "file_renamed":
        // what changed is not in these events (they carry a name, not a row):
        // revalidate — an unchanged listing answers 304 for ~200 bytes
        setFileEv((c) => ({ seq: (c?.seq ?? 0) + 1, delta: null }));
        break;
      case "agent_start":
        setRunning(true);
        break;
      case "agent_settled":
        setRunning(false);
        break;
      case "message_start": {
        const m = d.message ?? {};
        if (m.role === "assistant") {
          setMsgs((cur) => [
            ...cur,
            {
              id: `live-a-${nextSeq()}`,
              parent_id: null,
              timestamp: null,
              role: "assistant",
              text: "",
              thinking: "",
              thinking_signature: "",
              tool_calls: [],
            },
          ]);
        } else if (m.role === "user") {
          setMsgs((cur) => {
            const text =
              typeof m.content === "string"
                ? m.content
                : Array.isArray(m.content)
                  ? m.content
                      .filter((p: any) => p?.type === "text")
                      .map((p: any) => p.text)
                      .join("")
                  : "";
            const nm: Msg = {
              id: `live-u-${nextSeq()}`,
              parent_id: null,
              timestamp: null,
              role: "user",
              text,
              thinking: "",
              thinking_signature: "",
              tool_calls: [],
            };
            if (cur.some((x) => fp(x) === fp(nm))) return cur; // dedup
            return [...cur, nm];
          });
        }
        break;
      }
      case "message_update": {
        const ev = d.assistantMessageEvent ?? {};
        const usage = d.usage;
        setMsgs((cur) => {
          if (!cur.length) return cur;
          const i = cur.length - 1;
          const last = { ...cur[i] };
          if (last.role !== "assistant") return cur;
          const t = ev.type;
          if (t === "text_delta") last.text += ev.delta ?? "";
          else if (t === "thinking_delta") last.thinking += ev.delta ?? "";
          else if (t === "toolcall_start")
            last.tool_calls = [
              ...last.tool_calls,
              { id: ev.id ?? "", name: ev.toolName ?? "", arguments: {} },
            ];
          else if (t === "toolcall_delta" && last.tool_calls.length) {
            const tc = [...last.tool_calls];
            const j = tc.length - 1;
            tc[j] = {
              ...tc[j],
              arguments: { ...(tc[j].arguments as object), _raw: ((tc[j].arguments as any)._raw ?? "") + (ev.delta ?? "") },
            };
            last.tool_calls = tc;
          } else if (t === "toolcall_end" && last.tool_calls.length) {
            const tc = [...last.tool_calls];
            const j = tc.length - 1;
            tc[j] = {
              id: ev.toolCall?.id ?? tc[j].id,
              name: ev.toolCall?.name ?? tc[j].name,
              arguments: ev.toolCall?.arguments ?? tc[j].arguments,
            };
            last.tool_calls = tc;
          }
          if (usage) last.usage = usage;
          const next = [...cur];
          next[i] = last;
          return next;
        });
        break;
      }
      case "message_end": {
        const m = d.message ?? {};
        if (m.role === "assistant") {
          setMsgs((cur) => {
            const nm = mapMessage({ id: `live-end-${nextSeq()}`, message: m });
            if (!nm) return cur;
            // replace the trailing partial assistant slot (if any), else
            // append; dedup against the snapshot
            let arr = [...cur];
            if (arr.length && arr[arr.length - 1].role === "assistant" && arr[arr.length - 1].id.startsWith("live-a-")) {
              arr = arr.slice(0, -1);
            }
            if (arr.some((x) => fp(x) === fp(nm))) return cur;
            return [...arr, nm];
          });
        }
        break;
      }
      case "tool_execution_start":
        setTools((cur) => [
          ...cur,
          {
            toolCallId: d.toolCallId,
            name: d.toolName,
            args: d.args ?? {},
            status: "running",
          },
        ]);
        break;
      case "tool_execution_update":
        setTools((cur) =>
          cur.map((t) =>
            t.toolCallId === d.toolCallId && t.status === "running"
              ? { ...t, result: partialText(d.partialResult), status: "running" }
              : t,
          ),
        );
        break;
      case "tool_execution_end":
        setTools((cur) =>
          cur.map((t) =>
            t.toolCallId === d.toolCallId
              ? {
                  ...t,
                  status: d.isError ? "error" : "done",
                  result: resultText(d.result),
                  durationMs: d.durationMs,
                }
              : t,
          ),
        );
        break;
      case "extension_dialog":
      case "extension_dialog_answered":
      case "extension_dialog_resolved":
        // a dialog parked, was answered, or was resolved by pi on its
        // own (timeout/abort — the settle swept it): re-fetch the
        // pending set
        void refreshDialogs();
        break;
      default:
        // started/respawned/idle_killed/...: refresh the row
        if (sid) void refreshRow(sid);
    }
  }, [sid]);

  const refreshRow = useCallback(async (s: string) => {
    try {
      const r = await api.getSession(s);
      if (idRef.current !== s) return; // navigated away during fetch
      setRow(r);
    } catch {
      /* session gone */
    }
  }, []);

  // the pending extension dialogs (the run blocks on these until a human
  // answers). The SSE event is only the notification — the full record
  // (options/prefill) comes from the API. 409 (no live process) means
  // nothing pending: a dialog dies with its child.
  const refreshDialogs = useCallback(async () => {
    const s = idRef.current;
    if (!s) return;
    try {
      const r = await api.getDialogs(s);
      if (idRef.current === s) setDialogs(r.dialogs);
    } catch {
      if (idRef.current === s) setDialogs([]);
    }
  }, []);

  // coalesced flush: batch event application to at most one render per
  // `coalesce` ms (slow phones); the queue drains fully each flush
  const scheduleFlush = useCallback(() => {
    if (flushTimer.current !== null) return;
    flushTimer.current = window.setTimeout(() => {
      flushTimer.current = null;
      const evs = queue.current;
      queue.current = [];
      for (const e of evs) applyEvent(e.name, e.data);
    }, Math.max(0, settingsRef.current.coalesce));
  }, [applyEvent]);

  // ---- attach: fetch (authoritative base) then follow the live tail ----
  const attach = useCallback(async () => {
    if (!idRef.current) return;
    const s = idRef.current;
    unsubRef.current?.();
    // a re-attach (gap re-sync, fork) replaces the transcript wholesale:
    // drop the old stream's queued events, else a pending flush applies
    // them on top of the fresh snapshot
    queue.current = [];
    try {
      const m = await api.getMessages(s);
      if (idRef.current !== s) return; // navigated away
      setMsgs(m.messages);
      const r = await api.getSession(s);
      if (idRef.current !== s) return; // navigated away during fetch
      setRow(r);
      // seed run state from the row, not the stream tail: agent_start/
      // agent_settled for a run already in flight fired before our cursor
      // and will not be redelivered
      setRunning(r.busy);
      // a returning client is re-offered any still-pending dialog
      void refreshDialogs();
      unsubRef.current = api.subscribeEvents(
        s,
        m.cursor,
        (name, data) => {
          queue.current.push({ name, data });
          scheduleFlush();
        },
        () => {
          // the ring evicted past our position: re-fetch + re-attach
          // (never partial data) — and revalidate the file listing: its
          // deltas were in the evicted window and will not be re-published
          void attach();
          setFileEv((c) => ({ seq: (c?.seq ?? 0) + 1, delta: null }));
        },
      );
    } catch (e) {
      setBanner(String(e));
    }
  }, [scheduleFlush]);

  useEffect(() => {
    void attach();
    return () => {
      unsubRef.current?.();
      if (flushTimer.current !== null) window.clearTimeout(flushTimer.current);
    };
  }, [attach, sid]);

  // A hidden tab holds its socket open (and the radio with it) — the cost
  // that matters. Close the stream when hidden; on show, re-attach from the
  // snapshot (fetch + cursor) and revalidate the file listing — the same
  // path a reconnect takes.
  useEffect(() => {
    const onVis = () => {
      if (document.hidden) {
        unsubRef.current?.();
        unsubRef.current = null;
      } else {
        void attach();
        setFileEv((c) => ({ seq: (c?.seq ?? 0) + 1, delta: null }));
      }
    };
    document.addEventListener("visibilitychange", onVis);
    return () => document.removeEventListener("visibilitychange", onVis);
  }, [attach]);

  // ---- composer ---------------------------------------------------------
  const [text, setText] = useState("");
  useEffect(() => {
    if (composerPrefill !== null) {
      setText(composerPrefill);
      setComposerPrefill(null);
    }
  }, [composerPrefill]);

  // navigated to a different session: every state here is session-scoped
  // and must not survive the transition. `running` in particular gates
  // send() (steer vs prompt) — a stale true would steer a session with no
  // run in flight; the row/msgs reset avoids showing the previous
  // session's data during the fetch window
  useEffect(() => {
    setRow(null);
    setMsgs([]);
    setTools([]);
    setRunning(false);
    setDialogs([]);
    // an upload belongs to the session it was started in; the rows go with it
    setJobs([]);
    setPending([]);
    // the refs key job state by id and would otherwise pin the last
    // session's File handles (and their tasks) for the tab's life: an
    // in-flight pump keeps its own file closure, and a half-finished
    // upload is re-offered from its staged record on the next visit
    jobFiles.current = {};
    jobTasks.current = {};
    setBanner(null);
    setText("");
    setClosing(false);
    setComposerPrefill(null);
    setFileEv(null);
  }, [sid]);

  async function send(mode: "auto" | "followup") {
    if (!sid || !text.trim()) return;
    const t = text.trim();
    setText("");
    try {
      if (mode === "followup") {
        const r = await api.followUp(sid, t);
        if (r.disposition !== "handled") setRunning(true);
      } else if (runningRef.current) {
        // send while a run is active = steer
        const r = await api.steer(sid, t);
        if (r.disposition !== "handled") setRunning(true);
      } else {
        const r = await api.prompt(sid, t);
        if (r.disposition !== "handled") setRunning(true);
      }
      setBanner(null);
    } catch (e) {
      setBanner(String(e));
    }
  }

  async function onAbort() {
    if (!sid) return;
    try {
      await api.abort(sid);
    } catch (e) {
      setBanner(String(e));
    }
  }

  // ---- close (with confirm; abort first when a run is active) ----------
  async function closeSession() {
    if (!sid) return;
    setClosing(true);
    try {
      if (runningRef.current) {
        await api.abort(sid);
        // wait for the settle (bounded) before releasing
        for (let i = 0; i < 40 && runningRef.current; i++) {
          await new Promise((r) => setTimeout(r, 500));
        }
      }
      await api.releaseSession(sid);
      nav("/");
    } catch (e) {
      setBanner(String(e));
      setClosing(false);
    }
  }

  function runJob(id: string, file: File, resume?: api.StagedRecord) {
    const patch = (p: Partial<(typeof jobs)[number]>) =>
      setJobs((cur) => cur.map((j) => (j.id === id ? { ...j, ...p } : j)));
    const go = resume
      ? api.resumeStaged(sid!, resume, file, progress, task)
      : api.uploadResumable(sid!, file, progress, task, api.relName(file));
    function progress(sent: number, total: number) {
      patch({ sent, total });
    }
    function task(task: api.UploadTask) {
      // the registration is async (the POST /staged round-trip): a session
      // switch in the window has already cleared the table this id belonged
      // to, and writing into the new one would register a dead job
      if (idRef.current !== sid) return;
      jobTasks.current[id] = task;
    }
    go.then(
      () => patch({ status: "done", sent: file.size }),
      (e) => {
        const cancelled = e instanceof api.UploadCancelled;
        if (e instanceof api.StagedGone) {
          // the server's upload id is dead: the next retry must open a fresh
          // staged upload (retryJob reads this table), not resume the ghost
          delete jobTasks.current[id];
        }
        patch({
          status: cancelled ? "cancelled" : "failed",
          error: cancelled ? undefined : String((e as Error).message ?? e),
        });
        // a cancelled upload's record follows the server's drop (cancelStaged
        // forgets it only once the DELETE is confirmed): if the drop failed,
        // the bytes are still on the server and the next visit re-offers the
        // resume instead of re-sending the file
      },
    );
  }

  function sendFiles(files: File[]) {
    if (!sid || files.length === 0) return;
    for (const f of files) {
      // the upload name is the file's path within its pick: a folder pick
      // (webkitdirectory) arrives as the tree the human organized, a single
      // file as its name
      const name = api.relName(f);
      // two files with the same name are two rows, so the key cannot be a name
      const id = `${name}:${f.size}:${Math.random().toString(36).slice(2, 8)}`;
      jobFiles.current[id] = f;
      setJobs((cur) => [
        ...cur,
        { id, name, total: f.size, sent: 0, status: "uploading" },
      ]);
      runJob(id, f);
    }
  }

  function onFiles(files: FileList | null) {
    if (!files) return;
    sendFiles(Array.from(files));
  }

  function retryJob(id: string) {
    const f = jobFiles.current[id];
    if (!f) return;
    // the pump registered the server's upload id for this job: a retry must
    // resume from where the server actually is, not open a fresh upload and
    // orphan the bytes that already landed. A StagedGone failure clears the
    // registration (above), so a dead id falls through to a fresh start.
    const t = jobTasks.current[id];
    const resume = t
      ? { upload_id: t.uploadId, name: api.relName(f), total: f.size }
      : undefined;
    setJobs((cur) => cur.map((j) => (j.id === id ? { ...j, status: "uploading", error: undefined } : j)));
    runJob(id, f, resume);
  }

  // A half-finished upload from before this page load. The bytes the server
  // holds are not wasted — re-pick the same file (same name, same size) and
  // the rest goes; a different file is refused rather than spliced on.
  useEffect(() => {
    if (!sid) return;
    void (async () => {
      const local = api.localStaged(sid);
      if (local.length === 0) {
        setPending([]);
        return;
      }
      try {
        const held = new Set((await api.listStaged(sid)).staged.map((r) => r.upload_id));
        setPending(local.filter((r) => held.has(r.upload_id)));
      } catch {
        /* offline or gone: nothing to offer */
      }
    })();
  }, [sid]);

  function resumePending(rec: api.StagedRecord, file: File) {
    const id = `resume:${rec.upload_id}`;
    jobFiles.current[id] = file;
    setJobs((cur) => [
      ...cur,
      { id, name: rec.name, total: rec.total, sent: 0, status: "uploading" },
    ]);
    setPending((cur) => cur.filter((r) => r.upload_id !== rec.upload_id));
    runJob(id, file, rec);
  }

  async function discardPending(rec: api.StagedRecord) {
    const ok = await api.cancelStaged(sid!, rec.upload_id);
    if (ok) {
      setPending((cur) => cur.filter((r) => r.upload_id !== rec.upload_id));
    } else {
      // the drop did not land: the bytes are still on the server — say so
      // and keep the row, where the resume offer stays honest
      setBanner("could not drop the upload — it is still on the server and will expire");
    }
  }

  // paste a screenshot or a copied file straight into the session (the
  // composer keeps focus; a paste with no files is left alone)
  useEffect(() => {
    const onPaste = (e: ClipboardEvent) => {
      const files = e.clipboardData?.files;
      if (!files || files.length === 0) return;
      e.preventDefault();
      sendFiles(Array.from(files));
    };
    window.addEventListener("paste", onPaste);
    return () => window.removeEventListener("paste", onPaste);
    // sendFiles is re-created per render; re-binding it every render would
    // re-attach the listener for no reason (sid is the only real dependency)
  }, [sid]); // eslint-disable-line react-hooks/exhaustive-deps

  // dragend fires wherever the drop lands (or the drag is cancelled) — the
  // composer's own onDragLeave does not fire when a drag dies outside the
  // window, so without this the "drop to upload" hint lingers
  useEffect(() => {
    const onDragEnd = () => setDropping(false);
    window.addEventListener("dragend", onDragEnd);
    return () => window.removeEventListener("dragend", onDragEnd);
  }, []);

  // "edit + go from here" (user message context menu): fork the
  // conversation back to this user message, prefill the composer, and
  // re-attach for the new branch.
  async function editAt(m: Msg) {
    if (!sid) return;
    try {
      const r = await api.fork(sid, m.id);
      if (r.cancelled) {
        // the extension canceled the fork: the process is still on the
        // original branch — nothing to re-attach to
        setBanner("Fork was canceled — the conversation is unchanged");
        return;
      }
      setComposerPrefill(m.text);
      setBanner(null);
      // the fork switches the process to a new session file: re-attach
      // for the fresh transcript (only if still on this session — a nav
      // away already switched targets)
      window.setTimeout(() => {
        if (idRef.current === sid) void attach();
      }, 500);
    } catch (e) {
      setBanner(String(e));
    }
  }

  if (!sid) return null;
  const showThinking = settings.show_thinking;
  // the last user message is the current position — forking from it is a
  // no-op, so its context menu offers no "edit + go from here"
  const lastUserId = [...msgs].reverse().find((m) => m.role === "user")?.id;
  const lastAssistant = [...msgs].reverse().find((m) => m.role === "assistant");
  void lastAssistant;

  return (
    <div className="page chat">
      <header className="topbar">
        <Link to="/" className="back" aria-label="back">
          ←
        </Link>
        <div className="title">
          <span className="name">{row?.display_name || sid.slice(0, 8)}</span>
          <span className={`chip ${running ? "live" : "idle"}`}>
            {running ? "running" : row?.live_process ? "idle" : "offline"}
          </span>
        </div>
        <div className="actions">
          <button
            className={`btn ${row?.stable ? "primary" : "ghost"}`}
            onClick={async () => {
              try {
                await api.setStable(sid, !row?.stable);
                await refreshRow(sid);
              } catch (e) {
                setBanner(String(e));
              }
            }}
          >
            {row?.stable ? "📌 pinned" : "pin"}
          </button>
          <button
            className="btn ghost"
            onClick={() => {
              const s = window.confirm(
                running
                  ? "Close this session? A run is active — it will be aborted first, then the session released."
                  : "Close this session? It will be released: its files and transcript are deleted.",
              );
              if (s) void closeSession();
            }}
            disabled={closing}
          >
            {closing ? "closing…" : "close"}
          </button>
        </div>
      </header>

      <nav className="tabs">
        {(["chat", "files"] as const).map((t) => (
          <button
            key={t}
            className={`tab ${subview === t ? "on" : ""}`}
            onClick={() => setSubview(t)}
          >
            {t}
          </button>
        ))}
      </nav>

      {banner && <div className="banner err">{banner}</div>}

      {subview === "chat" && (
        <>
          <div className="messages">
            {msgs.map((m, i) => {
              const tool =
                m.role === "assistant"
                  ? tools.filter((t) =>
                      m.tool_calls.some((tc) => tc.id === t.toolCallId),
                    )
                  : [];
              return (
                <Message
                  key={m.id}
                  m={m}
                  showThinking={showThinking}
                  tools={tool}
                  streaming={
                    i === msgs.length - 1 && m.role === "assistant" && running
                  }
                  onEdit={
                    m.role === "user" && m.text !== "" && m.id !== lastUserId
                      ? editAt
                      : undefined
                  }
                />
              );
            })}
            {msgs.length === 0 && !running && (
              <div className="empty">No messages yet — start below.</div>
            )}
          </div>

          {/* pending extension dialogs: the run waits on the human here */}
          <Dialogs
            sid={sid}
            dialogs={dialogs}
            onChanged={() => void refreshDialogs()}
          />

          <div
            className={`composer${dropping ? " dropping" : ""}`}
            onDragOver={(e) => {
              e.preventDefault();
              setDropping(true);
            }}
            onDragLeave={() => setDropping(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDropping(false);
              sendFiles(Array.from(e.dataTransfer?.files ?? []));
            }}
          >
            {dropping && <div className="drop-hint">drop to upload</div>}
            {jobs.length > 0 && (
              <div className="uploads">
                {jobs.map((j) => (
                  <div key={j.id} className={`up ${j.status}`}>
                    <span className="up-name" title={j.name}>
                      {j.name}
                    </span>
                    {j.status === "uploading" && (
                      <>
                        <span className="bar" aria-label="uploaded so far">
                          <span
                            className="fill"
                            style={{
                              width: `${Math.round((j.sent / Math.max(j.total, 1)) * 100)}%`,
                            }}
                          />
                        </span>
                        <span className="muted sm">
                          {fmtBytes(j.sent)} / {fmtBytes(j.total)}
                        </span>
                        <button
                          className="btn ghost xs"
                          onClick={() => jobTasks.current[j.id]?.cancel()}
                        >
                          cancel
                        </button>
                      </>
                    )}
                    {j.status === "done" && <span className="muted sm">✓ uploaded</span>}
                    {j.status === "cancelled" && (
                      <span className="muted sm">cancelled — nothing was delivered</span>
                    )}
                    {j.status === "failed" && (
                      <>
                        <span className="muted sm" title={j.error}>
                          stopped at {fmtBytes(j.sent)} — {j.error}
                        </span>
                        <button className="btn xs" onClick={() => retryJob(j.id)}>
                          resume
                        </button>
                      </>
                    )}
                  </div>
                ))}
              </div>
            )}
            {pending.length > 0 && (
              <div className="uploads pending">
                {pending.map((r) => (
                  <div key={r.upload_id} className="up pending">
                    <span className="up-name" title={r.name}>
                      unfinished: {r.name} ({fmtBytes(r.total)})
                    </span>
                    <label className="mini">
                      <input
                        type="file"
                        style={{ display: "none" }}
                        onChange={(e) => {
                          const f = e.target.files?.[0];
                          if (f) resumePending(r, f);
                          e.target.value = "";
                        }}
                      />
                      <span className="btn xs">pick the same file to finish</span>
                    </label>
                    <button className="btn ghost xs" onClick={() => void discardPending(r)}>
                      discard
                    </button>
                  </div>
                ))}
              </div>
            )}
            <div className="row settings-row">
              <label className="mini">
                <input
                  type="checkbox"
                  checked={settings.show_thinking}
                  onChange={(e) => {
                    const n = { ...settings, show_thinking: e.target.checked };
                    try {
                      localStorage.setItem("servitor.settings", JSON.stringify(n));
                    } catch { /* private mode */ }
                    setSettings(n);
                  }}
                />
                thinking
              </label>
              <label className="mini">
                coalesce{" "}
                <select
                  value={settings.coalesce}
                  onChange={(e) => {
                    const n = { ...settings, coalesce: Number(e.target.value) };
                    try {
                      localStorage.setItem("servitor.settings", JSON.stringify(n));
                    } catch { /* private mode */ }
                    setSettings(n);
                  }}
                >
                  {[0, 50, 150, 300].map((v) => (
                    <option key={v} value={v}>
                      {v === 0 ? "off" : `${v}ms`}
                    </option>
                  ))}
                </select>
              </label>
              <label className="mini grow">
                <input
                  type="file"
                  multiple
                  style={{ display: "none" }}
                  onChange={(e) => {
                    onFiles(e.target.files);
                    e.target.value = "";
                  }}
                />
                <span
                  className="btn ghost sm"
                  onClick={() => {
                    const el = document.querySelector<HTMLInputElement>(
                      '.settings-row input[type="file"]:not([webkitdirectory])',
                    );
                    el?.click();
                  }}
                >
                  attach
                </span>
              </label>
              {/* a whole folder, the tree shape included: the pick's relative
                  paths are the upload names, so the inbox keeps the folders
                  (each file uploads independently and resumes independently) */}
              <label className="mini">
                <input
                  type="file"
                  style={{ display: "none" }}
                  {...({ webkitdirectory: "", directory: "" } as Record<string, string>)}
                  onChange={(e) => {
                    onFiles(e.target.files);
                    e.target.value = "";
                  }}
                />
                <span
                  className="btn ghost sm"
                  onClick={() => {
                    const el = document.querySelector<HTMLInputElement>(
                      '.settings-row input[webkitdirectory]',
                    );
                    el?.click();
                  }}
                >
                  folder
                </span>
              </label>
            </div>
            <div className="row send-row">
              <textarea
                className="input"
                rows={2}
                placeholder={
                  running ? "steer the run…" : "message… (send = steer while running)"
                }
                value={text}
                onChange={(e) => setText(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    void send("auto");
                  }
                }}
              />
              <div className="send-btns">
                <button
                  className="btn ghost"
                  onClick={() => void send("followup")}
                  disabled={!running || !text.trim()}
                  title="queue a follow-up turn in the current run"
                >
                  follow-up
                </button>
                <button
                  className="btn primary"
                  onClick={() => void send("auto")}
                  disabled={!text.trim()}
                >
                  {running ? "steer" : "send"}
                </button>
              </div>
            </div>
            {/* always-visible abort while a run is active */}
            {running && (
              <button className="btn danger wide" onClick={() => void onAbort()}>
                ■ abort
              </button>
            )}
          </div>
        </>
      )}

      {subview === "files" && (
        <Files
          sid={sid}
          ev={fileEv}
          onAttached={(name) => {
            setText((cur) =>
              cur.trim().length === 0
                ? `I put ${name} in inbox/ — start from that.`
                : `${cur}\n\nI also put ${name} in inbox/.`,
            );
            setSubview("chat");
          }}
        />
      )}
    </div>
  );
}

function partialText(p: any): string | undefined {
  if (!p) return undefined;
  const c = p.content;
  if (Array.isArray(c))
    return c
      .filter((x: any) => x?.type === "text")
      .map((x: any) => x.text)
      .join("");
  return undefined;
}

function resultText(r: any): string {
  if (!r) return "";
  const c = r.content;
  if (typeof c === "string") return c;
  if (Array.isArray(c))
    return c
      .filter((x: any) => x?.type === "text")
      .map((x: any) => x.text)
      .join("");
  return "";
}

function mapMessage(entry: { id: string; message: any }): Msg | null {
  const m = entry.message ?? {};
  const role = m.role;
  if (role !== "assistant" && role !== "user" && role !== "toolResult") return null;
  const content = m.content;
  let text = "";
  let thinking = "";
  const tool_calls: ToolCall[] = [];
  if (typeof content === "string") text = content;
  else if (Array.isArray(content)) {
    for (const p of content) {
      if (!p) continue;
      if (p.type === "text") text += p.text ?? "";
      else if (p.type === "thinking") thinking += p.thinking ?? "";
      else if (p.type === "toolCall")
        tool_calls.push({ id: p.id ?? "", name: p.name ?? "", arguments: p.arguments ?? {} });
    }
  }
  return {
    id: entry.id,
    parent_id: null,
    timestamp: null,
    role: role === "toolResult" ? "tool_result" : role,
    text,
    thinking,
    thinking_signature: "",
    tool_calls,
  };
}
