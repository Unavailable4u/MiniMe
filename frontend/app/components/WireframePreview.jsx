"use client";
import { useState } from "react";
import { RefreshCw, Send } from "lucide-react";

// New in Part 5 §5.5 -- the one genuinely new frontend component this
// part introduces (see Part 5 §5.7). Renders wireframe_sketcher's raw
// self-contained HTML block (a single ```html fenced code block, per
// that role's own brief) inside a sandboxed iframe, plus a small
// "request an edit" bar underneath.
//
// Edit round-trip: deliberately NOT wired to any dedicated backend
// endpoint or input_keys/stage_output mechanism -- there isn't one for
// cross-turn edits (see api/task_runner.py: a follow-up is just a
// normal POST /api/task reusing the same session_id, and generic_
// worker's ordinary conversation-memory prepend is what actually
// carries this component's own prior HTML forward to wireframe_
// sketcher's next hire -- see that role's brief). So `onRequestEdit`
// here is expected to be wired to the SAME chat-send function the main
// chat input already uses, just pre-seeded with an edit-shaped prompt
// -- not a new API call.
//
// sandbox="allow-scripts" only -- no allow-same-origin, no allow-forms,
// no allow-popups. wireframe_sketcher's own brief already forbids
// external scripts/stylesheets/CDN links (no network access in this
// sandbox anyway), so allow-scripts alone is enough for any inline
// interactivity a wireframe might sketch (e.g. a toggle) without
// granting the iframe access to this app's own origin, cookies, or
// parent DOM.
export default function WireframePreview({ html, screenLabel, onRequestEdit }) {
  const [editText, setEditText] = useState("");
  const [sending, setSending] = useState(false);
  const [iframeKey, setIframeKey] = useState(0);

  const hasContent = Boolean(html && html.trim());

  async function submitEdit() {
    const instruction = editText.trim();
    if (!instruction || !onRequestEdit) return;
    setSending(true);
    try {
      await onRequestEdit(
        screenLabel
          ? `For the "${screenLabel}" wireframe: ${instruction}`
          : instruction
      );
      setEditText("");
    } finally {
      setSending(false);
    }
  }

  function handleKeyDown(e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submitEdit();
    }
  }

  return (
    <div className="rounded-lg border border-[var(--neutral-800)] bg-[var(--neutral-950-a50)] overflow-hidden">
      <div className="flex items-center justify-between px-3 py-2 border-b border-[var(--neutral-800)]">
        <span className="text-xs font-medium text-[var(--neutral-400)]">
          {screenLabel || "Wireframe preview"}
        </span>
        {hasContent && (
          <button
            type="button"
            onClick={() => setIframeKey((k) => k + 1)}
            title="Reload preview"
            className="text-[var(--neutral-500)] hover:text-[var(--neutral-300)] p-1 rounded-md"
          >
            <RefreshCw size={12} />
          </button>
        )}
      </div>

      {hasContent ? (
        <iframe
          key={iframeKey}
          srcDoc={html}
          sandbox="allow-scripts"
          title={screenLabel || "Wireframe preview"}
          className="w-full bg-white"
          style={{ height: "420px", border: "none" }}
        />
      ) : (
        <div className="flex items-center justify-center h-[420px] text-xs text-[var(--neutral-600)]">
          No wireframe generated yet.
        </div>
      )}

      {onRequestEdit && (
        <div className="border-t border-[var(--neutral-800)] px-3 py-2 flex items-end gap-2">
          <textarea
            value={editText}
            onChange={(e) => setEditText(e.target.value)}
            onKeyDown={handleKeyDown}
            disabled={sending}
            placeholder="Describe an edit, e.g. 'make the primary button bigger'"
            rows={1}
            className="flex-1 resize-none bg-[var(--neutral-950)] border border-[var(--neutral-800)] rounded-md px-2.5 py-1.5 text-xs text-[var(--neutral-300)] outline-none focus:border-[var(--neutral-600)] leading-relaxed"
          />
          <button
            type="button"
            disabled={sending || !editText.trim()}
            onClick={submitEdit}
            className="flex items-center gap-1.5 bg-[var(--accent)] text-[var(--accent-text)] rounded-lg px-3 py-1.5 text-xs font-medium disabled:opacity-50"
          >
            <Send size={12} />
            {sending ? "Sending…" : "Send edit"}
          </button>
        </div>
      )}
    </div>
  );
}
