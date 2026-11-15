import { useState } from "react";
import type { Msg, ToolCall } from "../types";

interface Props {
  m: Msg;
  showThinking: boolean;
  tools: { toolCallId: string; name: string; args: Record<string, unknown>; status: string; result?: string; durationMs?: number }[];
  streaming: boolean;
}

// one message bubble. thinking: client mode (hidden unless enabled,
// collapsed by default); tool status lines collapsed, tool results lazy
// (expanded on tap, first 4KB preview).
export default function Message({ m, showThinking, tools, streaming }: Props) {
  const [thinkingOpen, setThinkingOpen] = useState(false);
  return (
    <div className={`msg ${m.role}`}>
      {m.role === "assistant" && m.tool_calls.length > 0 && (
        <div className="tool-lines">
          {m.tool_calls.map((tc) => (
            <ToolLine
              key={tc.id || tc.name}
              tc={tc}
              live={tools.find((t) => t.toolCallId === tc.id)}
            />
          ))}
        </div>
      )}
      {m.role === "tool_result" && (
        <ToolResult
          name={m.tool_name ?? "tool"}
          text={m.result ?? ""}
          is_error={!!m.is_error}
        />
      )}
      {m.role === "assistant" && m.thinking && showThinking && (
        <div className="thinking">
          <button className="mini-btn" onClick={() => setThinkingOpen((o) => !o)}>
            {thinkingOpen ? "▾" : "▸"} thinking
          </button>
          {thinkingOpen && (
            <pre className="thinking-body">{m.thinking}</pre>
          )}
        </div>
      )}
      {(m.role === "user" || (m.role === "assistant" && m.text)) && (
        <div className={`bubble ${m.role}`}>
          <pre className="text">{m.text}</pre>
          {streaming && <span className="caret" />}
        </div>
      )}
      {m.role === "assistant" && m.model && (
        <div className="meta">
          {m.model}
          {m.usage && typeof m.usage.totalTokens === "number" ? ` · ${(m.usage as any).totalTokens} tok` : ""}
        </div>
      )}
    </div>
  );
}

function ToolLine({
  tc,
  live,
}: {
  tc: ToolCall;
  live?: { status: string; result?: string; durationMs?: number };
}) {
  const [open, setOpen] = useState(false);
  const args = Object.entries(tc.arguments ?? {})
    .filter(([k]) => k !== "_raw")
    .map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`)
    .join(" ");
  const raw = (tc.arguments as any)._raw;
  const status = live?.status ?? "done";
  return (
    <div className="tool-line">
      <button className="mini-btn" onClick={() => setOpen((o) => !o)}>
        {open ? "▾" : "▸"} ⚙ {tc.name}{" "}
        <span className={`tst ${status}`}>
          {status === "running" ? "…" : status === "error" ? "!" : "✓"}
          {live?.durationMs != null ? ` ${Math.round(live.durationMs)}ms` : ""}
        </span>
      </button>
      {open && (
        <pre className="tool-args">
          {args || (typeof raw === "string" ? raw : "") || "(no args)"}
        </pre>
      )}
      {live?.status === "running" && live.result ? (
        <pre className="tool-partial">{live.result}</pre>
      ) : null}
    </div>
  );
}

function ToolResult({ name, text, is_error }: { name: string; text: string; is_error: boolean }) {
  const [open, setOpen] = useState(false);
  const preview = text.slice(0, 4096);
  return (
    <div className={`tool-result ${is_error ? "err" : ""}`}>
      <button className="mini-btn" onClick={() => setOpen((o) => !o)}>
        {open ? "▾" : "▸"} {name}: {is_error ? "error" : `${text.length} chars`}
      </button>
      {(open || text.length <= 4096) && <pre className="tool-body">{open ? text : preview}</pre>}
      {text.length > 4096 && open && null}
      {text.length > 4096 && !open && (
        <div className="muted sm">… (tap to expand {text.length} chars)</div>
      )}
    </div>
  );
}
