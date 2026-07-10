"use client";
import { useRef, useEffect, useState } from "react";
import { useSession } from "../../context/SessionContext";
import MessageBubble from "../MessageBubble";
import WorkingPanel from "../WorkingPanel";
import HireReviewScreen from "../HireReviewScreen";
import { Sparkles, Feather, Zap, Brain, Flame, ChevronDown, ClipboardCheck } from "lucide-react";

// Icon + label per mode, shared between the trigger button and the
// dropdown list. Swap these for any other lucide-react icon you like —
// full icon list: https://lucide.dev/icons
const MODES = [
  { id: "auto", label: "Auto", icon: Sparkles, hint: "Let the Inspector decide" },
  { id: "simple", label: "Simple", icon: Feather, hint: "Cheapest capable tier only" },
  { id: "fast", label: "Fast", icon: Zap, hint: "Favor speed over headcount" },
  { id: "expert", label: "Expert", icon: Brain, hint: "Allow the full staffed ceiling" },
  { id: "beast", label: "Beast", icon: Flame, hint: "Force the full pipeline, skip SGA/cache" },
];

const WORKING_PANEL_KEY = "minime_working_panel_collapsed";

export default function ChatTab() {
  const {
    messages, loading, sendTask, mode, setMode,
    activeMessageIndex, setActiveMessageIndex,
    reviewBeforeDispatch, setReviewBeforeDispatch,   // NEW — Part 2 §2.5
    pendingHireReview, confirmHireReview, cancelHireReview,   // NEW — Part 2 §2.5
  } = useSession();
  const bottomRef = useRef(null);
  const textareaRef = useRef(null);
  const chatContainerRef = useRef(null);
  const messageRefs = useRef([]);
  const isSyncingRef = useRef(false); // shared lock, passed to WorkingPanel's scroll handler too
  const [modeOpen, setModeOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const [workingPanelCollapsed, setWorkingPanelCollapsed] = useState(false);

  useEffect(() => {
    setWorkingPanelCollapsed(localStorage.getItem(WORKING_PANEL_KEY) === "1");
  }, []);
  function toggleWorkingPanel() {
    setWorkingPanelCollapsed((prev) => {
      localStorage.setItem(WORKING_PANEL_KEY, !prev ? "1" : "0");
      return !prev;
    });
  }

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, loading]);

  // Auto-grow the textarea as the person types multiple lines, capped so
  // it doesn't swallow the whole viewport on a very long paste.
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 240)}px`;
  }, [draft]);

  function handleSubmit(e) {
    e?.preventDefault();
    const text = draft.trim();
    if (!text || loading) return;
    setDraft("");
    sendTask(text);
  }

  // Enter sends; Shift+Enter (or Alt/Ctrl+Enter) inserts a real newline —
  // same convention as Slack/Discord/ChatGPT, so multiline/indented input
  // (e.g. pasted code, a numbered list) is actually usable here.
  function handleKeyDown(e) {
    if (e.key === "Enter" && !e.shiftKey && !e.altKey && !e.ctrlKey) {
      e.preventDefault();
      handleSubmit();
    }
  }

  // Scroll-sync: figure out which message is closest to the top of the
  // viewport and publish it as activeMessageIndex, so WorkingPanel can
  // scroll its own matching section into view. Guarded by isSyncingRef
  // so a programmatic sync-scroll (triggered by WorkingPanel's own
  // scroll) doesn't bounce right back and fight the other panel.
  function handleChatScroll() {
    if (isSyncingRef.current) return;
    let closestIndex = null;
    let closestDist = Infinity;
    messageRefs.current.forEach((el, i) => {
      if (!el) return;
      const dist = Math.abs(
        el.getBoundingClientRect().top - (chatContainerRef.current?.getBoundingClientRect().top ?? 0)
      );
      if (dist < closestDist) {
        closestDist = dist;
        closestIndex = i;
      }
    });
    if (closestIndex != null) setActiveMessageIndex(closestIndex);
  }

  const activeMode = MODES.find((m) => m.id === mode) || MODES[0];
  const ActiveIcon = activeMode.icon;

  return (
    <div className="flex h-full max-w-6xl mx-auto">
      {/* LEFT — Chat Box */}
      <div className="flex flex-col flex-1 min-w-0 border-r border-neutral-800">
        <div className="px-4 py-2 border-b border-neutral-800 flex items-center justify-between">
          <span className="text-xs font-medium text-neutral-400">Chat Box</span>
          <div className="flex items-center gap-3">
            {/* NEW — Part 2 §2.5: per-session toggle, off by default —
                most tasks should stay one-click. When on, sendTask()
                calls /api/task/preview instead of /api/task and pauses
                on a real hires list (tier 2/3 only) before dispatching. */}
            <button
              type="button"
              onClick={() => setReviewBeforeDispatch((v) => !v)}
              title="Review staffed roles before a run starts"
              className={`flex items-center gap-1 text-xs px-2 py-1 rounded-md border transition-colors ${
                reviewBeforeDispatch
                  ? "border-neutral-500 text-neutral-200 bg-neutral-800/70"
                  : "border-neutral-800 text-neutral-500 hover:text-neutral-300"
              }`}
            >
              <ClipboardCheck size={12} />
              Review hires
            </button>
            {workingPanelCollapsed && (
              <button
                onClick={toggleWorkingPanel}
                className="text-xs text-neutral-500 hover:text-neutral-300 flex items-center gap-1"
              >
                Show Working Panel
              </button>
            )}
          </div>
        </div>

        <div
          ref={chatContainerRef}
          onScroll={handleChatScroll}
          className="flex-1 overflow-y-auto px-4 py-6 space-y-4"
        >
          {messages.length === 0 && (
            <p className="text-neutral-500 text-sm">
              Send a task — the EO layer will classify it and route it through
              the appropriate tier.
            </p>
          )}
          {messages.map((m, i) => (
            <div
              key={i}
              ref={(el) => (messageRefs.current[i] = el)}
              onClick={() => setActiveMessageIndex(i)}
            >
              <MessageBubble message={m} />
            </div>
          ))}
          {loading && (
            <div className="text-neutral-500 text-sm animate-pulse">Working…</div>
          )}
          <div ref={bottomRef} />
        </div>

        {/* NEW — Part 2 §2.5: the review screen renders in place of the
            compose bar while a preview is awaiting a decision — nothing
            has dispatched yet, so there's nothing for the compose bar to
            usefully do until Confirm/Cancel resolves it. */}
        {pendingHireReview ? (
          <div className="border-t border-neutral-800 p-4">
            <HireReviewScreen
              hires={pendingHireReview.hires}
              onConfirm={confirmHireReview}
              onCancel={cancelHireReview}
            />
          </div>
        ) : (
        <form onSubmit={handleSubmit} className="border-t border-neutral-800 p-4 flex gap-2 items-end">
          {/* Mode picker — custom dropdown (not a native <select>) so each
              option can carry its own icon. */}
          <div className="relative">
            <button
              type="button"
              disabled={loading}
              onClick={() => setModeOpen((o) => !o)}
              className="flex items-center gap-1.5 bg-neutral-900 border border-neutral-800 rounded-lg px-3 py-2 text-sm outline-none disabled:opacity-50 hover:border-neutral-600 transition-colors"
            >
              <ActiveIcon size={14} />
              {activeMode.label}
              <ChevronDown size={13} className={`transition-transform ${modeOpen ? "rotate-180" : ""}`} />
            </button>
            {modeOpen && (
              <div className="absolute bottom-full mb-2 left-0 w-56 rounded-lg border border-neutral-800 bg-neutral-900 shadow-xl overflow-hidden z-10">
                {MODES.map((m) => {
                  const Icon = m.icon;
                  return (
                    <button
                      key={m.id}
                      type="button"
                      onClick={() => {
                        setMode(m.id);
                        setModeOpen(false);
                      }}
                      className={`w-full flex items-start gap-2 px-3 py-2 text-left text-sm hover:bg-neutral-800 transition-colors ${
                        m.id === mode ? "bg-neutral-800/70" : ""
                      }`}
                    >
                      <Icon size={15} className="mt-0.5 shrink-0" />
                      <span>
                        <span className="block text-neutral-200">{m.label}</span>
                        <span className="block text-[11px] text-neutral-500">{m.hint}</span>
                      </span>
                    </button>
                  );
                })}
              </div>
            )}
          </div>

          <textarea
            ref={textareaRef}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Describe a task... (Shift+Enter for a new line)"
            disabled={loading}
            rows={1}
            className="flex-1 resize-none bg-neutral-900 border border-neutral-800 rounded-lg px-3 py-2 text-sm outline-none focus:border-neutral-600 disabled:opacity-50 leading-relaxed"
          />
          <button
            type="submit"
            disabled={loading || !draft.trim()}
            className="bg-neutral-100 text-neutral-900 rounded-lg px-4 py-2 text-sm font-medium disabled:opacity-50 self-end"
          >
            Send
          </button>
        </form>
        )}
      </div>

      {/* RIGHT — Working Panel, foldable */}
      {!workingPanelCollapsed && (
        <div className="w-[420px] shrink-0 hidden lg:flex flex-col">
          <div className="px-4 py-2 border-b border-neutral-800 flex items-center justify-between">
            <span className="text-xs font-medium text-neutral-400">Working Panel</span>
            <button onClick={toggleWorkingPanel} className="text-xs text-neutral-500 hover:text-neutral-300">
              Hide
            </button>
          </div>
          <div className="flex-1 min-h-0">
            <WorkingPanel isSyncingRef={isSyncingRef} />
          </div>
        </div>
      )}
    </div>
  );
}