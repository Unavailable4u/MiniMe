"use client";
import { useRef, useEffect, useState } from "react";
import { useSession } from "../context/SessionContext";
import { useWorkspaceDock } from "../context/WorkspaceDockContext";
import MessageBubble from "./MessageBubble";
import WorkingPanel from "./WorkingPanel";
import HireReviewScreen from "./HireReviewScreen";
import { Sparkles, Feather, Zap, Brain, Flame, ChevronDown, ClipboardCheck, PanelRightOpen, PanelRightClose, MessageSquare } from "lucide-react";

// NEW — §6: this component is the composition that used to live directly
// inside ChatTab.jsx (chat box + resizable/collapsible WorkingPanel dock).
// It's been pulled out so it can be embedded as a docked panel inside
// Notebooks/Research/etc (§6.2) as well as rendered standalone by the
// (now thin) ChatTab.jsx wrapper — same underlying chat, since both read
// the single active chat off SessionContext (messages/sessionId are
// global, not per-embed — a workspace only ever has one chat "in focus"
// at a time, matching §0's model).
//
// `collapsed`/`onToggleCollapse` are a NEW top-level pair, distinct from
// the WorkingPanel's own internal collapse state below — this is for the
// *whole panel* (chat + WorkingPanel together) when it's docked inside a
// domain tab and the user wants to fold it away entirely, same pattern as
// the left ChatSidebar's collapse. The standalone ChatTab wrapper doesn't
// pass these, so it always renders expanded, unchanged from today.
//
// Step 3d of the §2.6 build order: `workspaceId`/`chatId` are NEW,
// OPTIONAL props. Neither is passed by any of the 7 current call sites
// (Chat/Notebooks/Research/Plan/Build/Test/Growth tabs) — that rewiring is
// 3e, one tab at a time. Until a caller passes one, this component is
// byte-for-byte the same as before: it reads messages/sessionId/loading/
// etc. off useSession(), exactly like today.
//
// DUAL MODE: `useWorkspaceDock(workspaceId, chatId)` is called
// unconditionally (hooks can't be conditional) and resolves to a null key
// when neither prop is passed — safe, since the hook already returns inert
// no-op fields for a null key. `usingDock` below is the single switch that
// picks dock state/actions vs. SessionContext's, field by field. Once 3e
// starts passing a real workspaceId into a given call site, that one
// instance flips to the dock; every other still-unwired call site keeps
// behaving exactly as it does today. No cutover moment where every tab
// switches at once.
//
// `mode`/`reviewBeforeDispatch`: SessionContext's sendTask() reads its own
// `mode` state from closure (no param), while the dock's sendTask(key,
// text, {mode, reviewBeforeDispatch}) takes them as call-site args (see
// WorkspaceDockContext.jsx's note on this same question). Sharing one
// global mode toggle across what could be several simultaneously-open dock
// panels (the whole point of partial promotion) would be wrong, so in dock
// mode these are local state on THIS component instance — not read from
// SessionContext, not stored on the dock. In legacy mode they still come
// straight from SessionContext, unchanged.
const MODES = [
  { id: "auto", label: "Auto", icon: Sparkles, hint: "Let the Inspector decide" },
  { id: "simple", label: "Simple", icon: Feather, hint: "Cheapest capable tier only" },
  { id: "fast", label: "Fast", icon: Zap, hint: "Favor speed over headcount" },
  { id: "expert", label: "Expert", icon: Brain, hint: "Allow the full staffed ceiling" },
  { id: "beast", label: "Beast", icon: Flame, hint: "Force the full pipeline, skip SGA/cache" },
];

const WORKING_PANEL_KEY = "minime_working_panel_collapsed";
const WORKING_PANEL_WIDTH_KEY = "minime_working_panel_width";
const WORKING_PANEL_DEFAULT_WIDTH = 420;
const WORKING_PANEL_MIN_WIDTH = 280;
const WORKING_PANEL_MAX_WIDTH = 720;

function clampWorkingPanelWidth(w) {
  return Math.min(WORKING_PANEL_MAX_WIDTH, Math.max(WORKING_PANEL_MIN_WIDTH, w));
}

export default function WorkspaceChatPanel({ collapsed = false, onToggleCollapse = null, workspaceId = null, chatId = null }) {
  const legacy = useSession();
  const dock = useWorkspaceDock(workspaceId, chatId);
  const usingDock = dock.key != null;

  // Local to this component instance — only meaningful in dock mode. See
  // header comment for why these can't come from SessionContext OR the
  // dock store.
  const [dockMode, setDockMode] = useState("auto");
  const [dockReviewBeforeDispatch, setDockReviewBeforeDispatch] = useState(false);

  const messages = usingDock ? dock.state.messages : legacy.messages;
  const loading = usingDock ? dock.state.loading : legacy.loading;
  const mode = usingDock ? dockMode : legacy.mode;
  const setMode = usingDock ? setDockMode : legacy.setMode;
  const activeMessageIndex = usingDock ? dock.state.activeMessageIndex : legacy.activeMessageIndex;
  const setActiveMessageIndex = usingDock
    ? (i) => dock.setDockState({ activeMessageIndex: i })
    : legacy.setActiveMessageIndex;
  const reviewBeforeDispatch = usingDock ? dockReviewBeforeDispatch : legacy.reviewBeforeDispatch;   // Part 2 §2.5
  const setReviewBeforeDispatch = usingDock ? setDockReviewBeforeDispatch : legacy.setReviewBeforeDispatch;   // Part 2 §2.5
  const pendingHireReview = usingDock ? dock.state.pendingHireReview : legacy.pendingHireReview;   // Part 2 §2.5
  const confirmHireReview = usingDock ? dock.confirmHireReview : legacy.confirmHireReview;   // Part 2 §2.5
  const cancelHireReview = usingDock ? dock.cancelHireReview : legacy.cancelHireReview;   // Part 2 §2.5

  function sendTask(taskText) {
    if (usingDock) {
      return dock.sendTask(taskText, { mode, reviewBeforeDispatch });
    }
    return legacy.sendTask(taskText);
  }

  const bottomRef = useRef(null);
  const textareaRef = useRef(null);
  const chatContainerRef = useRef(null);
  const messageRefs = useRef([]);
  const isSyncingRef = useRef(false); // shared lock, passed to WorkingPanel's scroll handler too
  const [modeOpen, setModeOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const [workingPanelCollapsed, setWorkingPanelCollapsed] = useState(false);
  const [workingPanelWidth, setWorkingPanelWidth] = useState(WORKING_PANEL_DEFAULT_WIDTH);
  const resizeCleanupRef = useRef(null); // holds the active mousemove/mouseup remover, if a drag is in progress

  useEffect(() => {
    setWorkingPanelCollapsed(localStorage.getItem(WORKING_PANEL_KEY) === "1");
    const savedWidth = parseInt(localStorage.getItem(WORKING_PANEL_WIDTH_KEY), 10);
    if (!Number.isNaN(savedWidth)) setWorkingPanelWidth(clampWorkingPanelWidth(savedWidth));
    // If the panel unmounts mid-drag (e.g. clicking another top-level tab
    // without releasing the mouse), make sure the window listeners below
    // don't leak.
    return () => resizeCleanupRef.current?.();
  }, []);
  function toggleWorkingPanel() {
    setWorkingPanelCollapsed((prev) => {
      localStorage.setItem(WORKING_PANEL_KEY, !prev ? "1" : "0");
      return !prev;
    });
  }

  // Drag-to-resize — handle sits on the panel's left edge (it's docked
  // to the right), so dragging left grows it and dragging right shrinks
  // it. Width only hits localStorage once on mouseup, not on every
  // mousemove, to avoid hammering it during the drag.
  function startWorkingPanelResize(e) {
    e.preventDefault();
    const startX = e.clientX;
    const startWidth = workingPanelWidth;

    function onMouseMove(ev) {
      const deltaX = ev.clientX - startX;
      setWorkingPanelWidth(clampWorkingPanelWidth(startWidth - deltaX));
    }
    function onMouseUp() {
      cleanup();
      setWorkingPanelWidth((w) => {
        localStorage.setItem(WORKING_PANEL_WIDTH_KEY, String(w));
        return w;
      });
    }
    function cleanup() {
      window.removeEventListener("mousemove", onMouseMove);
      window.removeEventListener("mouseup", onMouseUp);
      resizeCleanupRef.current = null;
    }

    window.addEventListener("mousemove", onMouseMove);
    window.addEventListener("mouseup", onMouseUp);
    resizeCleanupRef.current = cleanup;
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

  // NEW — §6: whole-panel collapsed rail. Only reachable when a parent
  // passes collapsed=true (i.e. when docked inside a domain tab) — the
  // standalone ChatTab wrapper never does this, so nothing changes there.
  if (collapsed) {
    return (
      <div className="w-10 h-full flex flex-col items-center border-l border-[var(--neutral-800)] pt-2">
        <button
          onClick={onToggleCollapse}
          title="Show chat"
          className="text-[var(--neutral-500)] hover:text-[var(--neutral-300)] p-1.5 rounded-md hover:bg-[var(--neutral-900)] transition-colors"
        >
          <MessageSquare size={16} />
        </button>
      </div>
    );
  }

  return (
    <div className="flex h-full max-w-6xl mx-auto">
      {/* LEFT — Chat Box */}
      <div className="flex flex-col flex-1 min-w-0 border-r border-[var(--neutral-800)]">
        <div className="px-4 py-2 border-b border-[var(--neutral-800)] flex items-center justify-between">
          <span className="text-xs font-medium text-[var(--neutral-400)]">Chat Box</span>
          <div className="flex items-center gap-3">
            {/* Part 2 §2.5: per-session toggle, off by default — most
                tasks should stay one-click. When on, sendTask() calls
                /api/task/preview instead of /api/task and pauses on a
                real hires list (tier 2/3 only) before dispatching. */}
            <button
              type="button"
              onClick={() => setReviewBeforeDispatch((v) => !v)}
              title="Review staffed roles before a run starts"
              className={`flex items-center gap-1 text-xs px-2 py-1 rounded-md border transition-colors ${
                reviewBeforeDispatch
                  ? "border-[var(--neutral-500)] text-[var(--neutral-200)] bg-[var(--neutral-800-a70)]"
                  : "border-[var(--neutral-800)] text-[var(--neutral-500)] hover:text-[var(--neutral-300)]"
              }`}
            >
              <ClipboardCheck size={12} />
              Review hires
            </button>
            {/* NEW — §6: only rendered when embedded in a docked context
                (a parent passed onToggleCollapse). The standalone Chat
                tab has no fold-away affordance for itself, same as today. */}
            {onToggleCollapse && (
              <button
                type="button"
                onClick={onToggleCollapse}
                title="Collapse chat"
                className="text-[var(--neutral-500)] hover:text-[var(--neutral-300)]"
              >
                <PanelRightClose size={14} />
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
            <p className="text-[var(--neutral-500)] text-sm">
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
            <div className="text-[var(--neutral-500)] text-sm animate-pulse">Working…</div>
          )}
          <div ref={bottomRef} />
        </div>

        {/* Part 2 §2.5: the review screen renders in place of the
            compose bar while a preview is awaiting a decision — nothing
            has dispatched yet, so there's nothing for the compose bar to
            usefully do until Confirm/Cancel resolves it. */}
        {pendingHireReview ? (
          <div className="border-t border-[var(--neutral-800)] p-4">
            <HireReviewScreen
              hires={pendingHireReview.hires}
              onConfirm={confirmHireReview}
              onCancel={cancelHireReview}
            />
          </div>
        ) : (
        <form onSubmit={handleSubmit} className="border-t border-[var(--neutral-800)] p-4 flex gap-2 items-end">
          {/* Mode picker — custom dropdown (not a native <select>) so each
              option can carry its own icon. */}
          <div className="relative">
            <button
              type="button"
              disabled={loading}
              onClick={() => setModeOpen((o) => !o)}
              className="flex items-center gap-1.5 bg-[var(--neutral-900)] border border-[var(--neutral-800)] rounded-lg px-3 py-2 text-sm outline-none disabled:opacity-50 hover:border-[var(--neutral-600)] transition-colors"
            >
              <ActiveIcon size={14} />
              {activeMode.label}
              <ChevronDown size={13} className={`transition-transform ${modeOpen ? "rotate-180" : ""}`} />
            </button>
            {modeOpen && (
              <div className="absolute bottom-full mb-2 left-0 w-56 rounded-lg border border-[var(--neutral-800)] bg-[var(--neutral-900)] shadow-xl overflow-hidden z-10">
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
                      className={`w-full flex items-start gap-2 px-3 py-2 text-left text-sm hover:bg-[var(--neutral-800)] transition-colors ${
                        m.id === mode ? "bg-[var(--neutral-800-a70)]" : ""
                      }`}
                    >
                      <Icon size={15} className="mt-0.5 shrink-0" />
                      <span>
                        <span className="block text-[var(--neutral-200)]">{m.label}</span>
                        <span className="block text-[11px] text-[var(--neutral-500)]">{m.hint}</span>
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
            className="flex-1 resize-none bg-[var(--neutral-900)] border border-[var(--neutral-800)] rounded-lg px-3 py-2 text-sm outline-none focus:border-[var(--neutral-600)] disabled:opacity-50 leading-relaxed"
          />
          <button
            type="submit"
            disabled={loading || !draft.trim()}
            className="bg-[var(--accent)] text-[var(--accent-text)] rounded-lg px-4 py-2 text-sm font-medium disabled:opacity-50 self-end"
          >
            Send
          </button>
        </form>
        )}
      </div>

      {/* RIGHT — Working Panel: resizable when open, collapses to a slim
          icon rail (rather than vanishing entirely) so there's always a
          visible way back in. Stays hidden below lg same as before —
          there's no room for a rail either at that width. */}
      <div className="hidden lg:flex shrink-0">
        {workingPanelCollapsed ? (
          <div className="w-10 flex flex-col items-center border-l border-[var(--neutral-800)] pt-2">
            <button
              onClick={toggleWorkingPanel}
              title="Show Working Panel"
              className="text-[var(--neutral-500)] hover:text-[var(--neutral-300)] p-1.5 rounded-md hover:bg-[var(--neutral-900)] transition-colors"
            >
              <PanelRightOpen size={16} />
            </button>
          </div>
        ) : (
          <div className="flex" style={{ width: workingPanelWidth }}>
            <div
              onMouseDown={startWorkingPanelResize}
              title="Drag to resize"
              className="w-1.5 shrink-0 cursor-col-resize hover:bg-[var(--neutral-700)] active:bg-[var(--neutral-600)] transition-colors"
            />
            <div className="flex-1 min-w-0 flex flex-col border-l border-[var(--neutral-800)]">
              <div className="px-4 py-2 border-b border-[var(--neutral-800)] flex items-center justify-between">
                <span className="text-xs font-medium text-[var(--neutral-400)]">Working Panel</span>
                <button
                  onClick={toggleWorkingPanel}
                  title="Collapse"
                  className="text-[var(--neutral-500)] hover:text-[var(--neutral-300)]"
                >
                  <PanelRightClose size={14} />
                </button>
              </div>
              <div className="flex-1 min-h-0">
                <WorkingPanel isSyncingRef={isSyncingRef} workspaceId={workspaceId} chatId={chatId} />
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
