import { useEffect } from "react";

// A confirm sheet for anything destructive. Nothing here fires on a single
// tap: the sheet names the exact object and the exact consequence, cancel is
// the default focus, and the destructive action is the last thing (a thumb
// has to aim for it). Scrim tap and Escape are cancel, never confirm.
interface Props {
  title: string;
  detail: string;
  confirmLabel: string;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

export default function Confirm({
  title,
  detail,
  confirmLabel,
  busy,
  onConfirm,
  onCancel,
}: Props) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel]);

  return (
    <div className="sheet-scrim" onClick={onCancel}>
      <div
        className="sheet"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="sheet-title">{title}</div>
        <div className="sheet-detail">{detail}</div>
        <button className="btn wide" onClick={onCancel} autoFocus disabled={busy}>
          cancel
        </button>
        <button
          className="btn danger wide"
          onClick={onConfirm}
          disabled={busy}
          aria-label={confirmLabel}
        >
          {busy ? "working…" : confirmLabel}
        </button>
      </div>
    </div>
  );
}
