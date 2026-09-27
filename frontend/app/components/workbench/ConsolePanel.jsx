"use client";
// frontend/app/components/workbench/ConsolePanel.jsx — W6.2 (Build
// Workbench plan). The bottom panel's Console tab: a live feed of
// console.*/window.onerror/unhandledrejection events relayed out of the
// live preview by lib/preview/consoleBridge.js's injected runtime and
// PreviewPane.jsx's verified message listener, both landing in the
// editor store's `console` slice (editorStore.js) this component just
// reads and renders — see that file's own header for why the state
// lives there rather than in PreviewPane.jsx or here directly (both the
// preview, which OWNS the feed, and this panel, which DISPLAYS it, are
// siblings under the same EditorStoreProvider, not parent/child).
//
// "Fix with AI" (plan §5, step W6.2's own "Build" line: "adds an error
// chip... and focuses the chat"): shown on any row this component
// considers an error (level === "error" — covers a thrown/uncaught
// exception, an unhandled promise rejection, AND a plain
// console.error(...) the page's own code called deliberately; there's
// no reason to treat those three differently here). Clicking it is
// this component's only real behavior beyond rendering — see `onFixWithAI`'s
// own doc comment on what it actually does and why it can't just default
// straight to Edit mode the way the plan's own wording literally reads.
import { AlertTriangle, Bug, Info, Sparkles, Trash2, XCircle } from "lucide-react";

// Icon + text color per row, keyed by `type` first (an "error"/
// "unhandledrejection" row is always styled as an error regardless of
// what `level` the bridge happened to set — consoleBridge.js's own
// buildBridgeScript() always sets level:"error" for both anyway, so
// this is really just documenting that coupling rather than adding a
// new one), falling back to `level` for an ordinary console.* call.
const TYPE_STYLE = {
  error: { Icon: XCircle, color: "text-red-400" },
  unhandledrejection: { Icon: XCircle, color: "text-red-400" },
};
const LEVEL_STYLE = {
  error: { Icon: XCircle, color: "text-red-400" },
  warn: { Icon: AlertTriangle, color: "text-amber-300" },
  info: { Icon: Info, color: "text-[var(--neutral-400)]" },
  debug: { Icon: Bug, color: "text-[var(--neutral-500)]" },
  log: { Icon: Info, color: "text-[var(--neutral-300)]" },
};

function rowStyle(entry) {
  return TYPE_STYLE[entry.type] || LEVEL_STYLE[entry.level] || LEVEL_STYLE.log;
}

function formatTime(ts) {
  try {
    return new Date(ts).toLocaleTimeString([], { hour12: false });
  } catch {
    return "";
  }
}

/**
 * @param {object} props
 * @param {object[]} props.messages - editorStore.js's `console` slice, oldest first
 * @param {() => void} props.onClear - the tab's own "Clear" button (editorStore.js's consoleClear())
 * @param {(entry: object) => void} [props.onFixWithAI] - omitted entirely (no button rendered) when the caller has nothing to wire it to
 */
export default function ConsolePanel({ messages, onClear, onFixWithAI }) {
  if (!messages || messages.length === 0) {
    return (
      <div className="h-full flex flex-col items-center justify-center gap-1 text-center">
        <p className="text-xs text-[var(--neutral-400)]">Console is empty</p>
        <p className="max-w-sm text-[11px] leading-relaxed text-[var(--neutral-600)]">
          Output and errors from the live preview will appear here.
        </p>
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col gap-1.5">
      <div className="flex shrink-0 items-center justify-end">
        <button
          type="button"
          onClick={onClear}
          title="Clear the console"
          className="flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] text-[var(--neutral-500)] hover:text-[var(--neutral-300)] hover:bg-[var(--neutral-900)]"
        >
          <Trash2 size={11} /> Clear
        </button>
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto flex flex-col gap-1 font-mono">
        {messages.map((entry) => {
          const { Icon, color } = rowStyle(entry);
          const isError = entry.level === "error";
          return (
            <div key={entry.id} className="flex items-start gap-1.5 rounded px-1.5 py-1 hover:bg-[var(--neutral-900)]">
              <Icon size={12} className={`mt-0.5 shrink-0 ${color}`} />
              <div className="min-w-0 flex-1">
                <div className="flex items-baseline gap-2">
                  <span className="shrink-0 text-[10px] text-[var(--neutral-600)]">{formatTime(entry.timestamp)}</span>
                  <p className={`min-w-0 whitespace-pre-wrap break-words text-[11px] ${color}`}>{entry.text}</p>
                </div>
                {entry.stack && (
                  <pre className="mt-0.5 max-h-24 overflow-y-auto whitespace-pre-wrap break-words text-[10px] text-[var(--neutral-600)]">
                    {entry.stack}
                  </pre>
                )}
              </div>
              {isError && onFixWithAI && (
                <button
                  type="button"
                  onClick={() => onFixWithAI(entry)}
                  title="Add this error to the chat and ask for a fix"
                  className="flex shrink-0 items-center gap-1 self-start rounded bg-[var(--accent)] px-1.5 py-0.5 text-[10px] font-medium text-[var(--accent-text)] hover:opacity-90"
                >
                  <Sparkles size={10} /> Fix with AI
                </button>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
