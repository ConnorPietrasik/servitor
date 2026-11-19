import { useEffect, useState } from "react";
import type { MouseEvent as RMouseEvent } from "react";
import type { Msg, ToolCall } from "../types";

interface Props {
  m: Msg;
  showThinking: boolean;
  tools: { toolCallId: string; name: string; args: Record<string, unknown>; status: string; result?: string; durationMs?: number }[];
  streaming: boolean;
  // "edit + go from here" — set only for older user messages (the last
  // one is the current position; forking from it is a no-op)
  onEdit?: (m: Msg) => void;
}

// one message bubble. thinking: client mode (hidden unless enabled,
// collapsed by default); tool status lines collapsed, tool results lazy
// (expanded on tap, first 4KB preview). User bubbles carry a context
// menu (right-click / long-press): copy, and fork from here.
export default function Message({ m, showThinking, tools, streaming, onEdit }: Props) {
  const [thinkingOpen, setThinkingOpen] = useState(false);
  const [menu, setMenu] = useState<{ x: number; y: number } | null>(null);

  // Escape closes the context menu
  useEffect(() => {
    if (!menu) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setMenu(null);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [menu]);

  function openMenu(e: RMouseEvent<HTMLDivElement>) {
    e.preventDefault(); // suppress the browser's own (text-select) menu
    setMenu({
      x: Math.max(4, Math.min(e.clientX, window.innerWidth - 200)),
      y: Math.max(4, Math.min(e.clientY, window.innerHeight - 110)),
    });
  }

  async function copyText() {
    setMenu(null);
    try {
      await navigator.clipboard.writeText(m.text);
    } catch {
      /* clipboard unavailable or permission denied */
    }
  }

  const isUser = m.role === "user";
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
        <div
          className={`bubble ${m.role}`}
          onContextMenu={isUser ? openMenu : undefined}
        >
          <pre className="text">{m.text}</pre>
          {streaming && <span className="caret" />}
        </div>
      )}
      {menu && (
        <>
          <div className="ctx-overlay" onClick={() => setMenu(null)} />
          <div
            className="ctxmenu"
            style={{ left: menu.x, top: menu.y }}
            role="menu"
          >
            <button role="menuitem" onClick={() => void copyText()}>
              copy
            </button>
            {onEdit && (
              <button
                role="menuitem"
                onClick={() => {
                  setMenu(null);
                  onEdit(m);
                }}
              >
                edit + go from here
              </button>
            )}
          </div>
        </>
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
