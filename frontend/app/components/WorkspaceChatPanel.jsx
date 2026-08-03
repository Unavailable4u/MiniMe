"use client";
import { useRef, useEffect, useState, useCallback, useMemo } from "react";
import { useSession } from "../context/SessionContext";
import { useWorkspaceDock, useWorkspaceDockActions, useLastActiveChatId } from "../context/WorkspaceDockContext";
import MessageRow from "./MessageRow"; // Perf audit #3 step 3 — extracted from messages.map() below
import { List, useDynamicRowHeight } from "react-window"; // Perf audit #3 step 5d — react-window is v2 (see package.json), a
// ground-up rewrite: no VariableSizeList/FixedSizeList split, no itemSize/
// estimatedItemSize/outerRef/resetAfterIndex/scrollToItem. One `List`
// component instead, sized automatically off its own parent via an
// internal ResizeObserver — step 5c's manual containerSize tracking was
// dead from the moment this landed and has been removed (step 9 cleanup).
import GenerationNotificationRow from "./notebooks/GenerationNotificationRow";   // NEW — Phase 4 step 4.6
import WorkingPanel from "./WorkingPanel";
import HireReviewScreen from "./HireReviewScreen";
import { Sparkles, Feather, Zap, Brain, Flame, ChevronDown, ClipboardCheck, PanelRightOpen, PanelRightClose, MessageSquare, Paperclip, Loader2, CheckCircle2, XCircle, AlertTriangle } from "lucide-react";
import { ingestFileByExtension } from "../lib/ingestDispatch";
import { parseFreeText, TARGETS } from "./notebooks/NotebooksGeneratePicker";

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
//
// CHANGED — Item 2 remaining piece, live-run-state slice, step 4: the
// merged messages/loading/mode/etc. variables below no longer read from
// `legacy` (SessionContext) at all — audited every path that reaches
// them (the compose form, MessageBubble list, handleChatScroll,
// dispatchText/sendTask) and confirmed every one of those only renders
// or fires from inside the `if (!usingDock) return <placeholder>`
// branch below, so the `usingDock ? dock… : legacy…` reads were dead in
// the same way WorkingPanel.jsx's were (Item 2 step 3) — the
// `!usingDock` case (TestTab/BuildTab/ResearchTab/PlanTab with nothing
// selected, or ChatTab's brief bootstrap gap) shows an explicit
// placeholder instead, never the real chat UI, so there was nothing
// left for those particular legacy values to feed. This does NOT make
// `legacy` fully dead in this file: `ingestFile`/`ingestPdfFile`/
// `ingestVoiceFile`/`generateNotebooks`/`classifyIntent`/
// `markTopicDone` above are still read unconditionally off
// `useSession()` (no dock equivalent — see line ~136), and
// SessionContext.jsx itself still owns the real global Chat-tab
// conversation `standalone` mode ultimately resolves into a dock key
// for. Full `SessionContext.jsx` cleanup is Item 2 steps 5-6, once
// nothing reachable still depends on its dead state.
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

// NEW — stacked (top/bottom) layout: used instead of the width constants
// above when a caller passes `stacked` (every domain-tab dock — Build/
// Plan/Research/Test/Notebooks/Growth — see each tab's WorkspaceChatPanel
// call site). The standalone Chat tab never passes `stacked`, so it keeps
// the original side-by-side layout and these are unused there.
const WORKING_PANEL_HEIGHT_KEY = "minime_working_panel_height";
const WORKING_PANEL_DEFAULT_HEIGHT = 320;
const WORKING_PANEL_MIN_HEIGHT = 160;
const WORKING_PANEL_MAX_HEIGHT = 640;

// NEW — Data Layer §4b: every chat-tab surface embeds this one component
// (see the header comment above), so the attach affordance below —
// paperclip button + hidden file input + a small inline progress pill
// row, all pointed at agents/source_manager.py's process_upload() via
// the exact same ingestFile/ingestPdfFile/ingestVoiceFile helpers
// IngestionDropzone.jsx already uses (factored into lib/ingestDispatch.js
// so both share one dispatch table) — lands on all 7 tabs
// (Chat/Notebooks/Research/Plan/Build/Test/Growth) in this one patch.
// Deliberately file-only, no URL field here: pasting a web/YouTube link
// still belongs to Notebooks' own Sources panel (IngestionDropzone), which
// is the dedicated place for that and already has one. This is purely
// "I have a file open, let me drop it into the conversation" — the
// chat-composer equivalent of a Slack/ChatGPT attach button, not a second
// Sources UI. Ingestion here never turns into a chat turn/sendTask() call
// — it's the same "hires Source Manager, which hires Backlink Detector"
// path §4a describes, independent of whatever's typed in the textarea.
const ATTACH_DONE_AUTOCLEAR_MS = 3000;

// Perf audit #3 step 8 fix — hoisted to module scope, deliberately outside
// WorkspaceChatPanel. react-window v2 wraps whatever you pass as
// `rowComponent` in its own React.memo, keyed off that function's identity
// (see node_modules/react-window/dist/react-window.js — `useMemo(() =>
// memo(rowComponent, arePropsEqual), [rowComponent])`). Step 5b/7's `Row`
// was defined *inside* WorkspaceChatPanel's render body, so it was a brand
// new function on every render — meaning react-window's memo wrapper was
// also brand new every render, which React treats as an entirely
// different component type. Net effect: every currently-visible message
// row fully unmounted and remounted (not just re-rendered) on every
// keystroke in the composer, every mode-menu toggle, etc — exactly the
// "mounting/rendering every message on unrelated re-renders" step 8's own
// checklist says to rule out via the Profiler, and it would not have
// passed. Data that used to come from closing over the parent's render
// scope (messages, messageRefs, the two callbacks) now arrives via
// `rowProps` instead — react-window's built-in, documented way to feed a
// stable row component fresh data without recreating the component itself
// (see the <List rowProps={...}> call site below).
//
// Not wiring up a ResizeObserver/data-react-window-index here: List
// itself already does this automatically for every rendered row once
// `rowHeight` is a DynamicRowHeight object (same source file, the effect
// right after the row-rendering loop) — that was redundant, and has been
// removed from this component's Row-equivalent.
//
// Perf audit #3 step 9 cleanup: no longer threads `messageRefs` through.
// That was the old DOM-node-per-row wiring cross-panel scroll sync used
// to read (`messageRefs.current[i]`) before virtualization — step 7
// replaced it with `handleRowsRendered`/`onRowsRendered`, which reports
// the live visible-row range directly instead of scanning DOM refs. Once
// step 7 landed, nothing read `messageRefs.current` for its original
// purpose anymore, so it — and the ref-setting div wrapper in
// MessageRow.jsx — were dead weight kept alive only by being passed
// through. Removed here, and MessageRow's ref prop dropped to match.
function VirtualMessageRow({ index, style, messages, onSelect, onNavigateSubTab, onSendCommand }) {
  return (
    <div style={style}>
      <MessageRow
        message={messages[index]}
        index={index}
        onSelect={onSelect}
        onNavigateSubTab={onNavigateSubTab}
        onSendCommand={onSendCommand}
      />
    </div>
  );
}

function clampWorkingPanelWidth(w) {
  return Math.min(WORKING_PANEL_MAX_WIDTH, Math.max(WORKING_PANEL_MIN_WIDTH, w));
}

function clampWorkingPanelHeight(h) {
  return Math.min(WORKING_PANEL_MAX_HEIGHT, Math.max(WORKING_PANEL_MIN_HEIGHT, h));
}

// NEW — Notebooks Chat-First refinement, Phase 2 step 2.6a (scope
// resolution). `activeContext` is the caller's best guess at "what the
// person is currently looking at" -- { type: "topic", id, label } or
// { type: "source", id, label } -- fed from whichever sub-tab/view last
// had something clicked (see NotebooksTab.jsx). Optional and null by
// default: a standalone Chat tab, or a Notebooks session where nothing's
// been clicked yet, simply has no default to fall back to, same as
// today (see tryHandleClassifiedToolCall's scope resolution below).
export default function WorkspaceChatPanel({ collapsed = false, onToggleCollapse = null, workspaceId = null, chatId = null, onNavigateSubTab = null, stacked = false, hideAttach = false, activeContext = null, standalone = false }) {
  const legacy = useSession();
  const { ingestFile, ingestPdfFile, ingestVoiceFile, generateNotebooks, classifyIntent, markTopicDone } = legacy;   // NEW — Data Layer §4b; generateNotebooks NEW — chat audit bug #1; classifyIntent NEW — Phase 2 step 2.5; markTopicDone NEW — Phase 6 step 6.8
  const dock = useWorkspaceDock(workspaceId, chatId);
  const usingDock = dock.key != null;
  const { createWorkspaceChat, loadOlderMessages } = useWorkspaceDockActions();
  const globalActiveChatId = useLastActiveChatId();

  // CHANGED — chat-gating, take 2. Was `!dock.state.sessionId` — but a
  // dock's sessionId is whatever this workspace's chat last resolved to
  // in THIS in-memory store, which can go stale: switch to another
  // project's chat, then come back to this one by clicking the project
  // row alone (not one of its chat rows), and this dock's sessionId is
  // still sitting on the old value even though nothing about it is
  // "open" anymore — no visible cue anywhere would say which chat this
  // panel is about. `globalActiveChatId` is the single "last active
  // chat" NotebooksTab's own sidebar already highlights a chat row
  // against (`chat.id === activeChatId`) — comparing this dock's
  // sessionId to THAT, not just checking it's non-null, means the gate
  // agrees with the exact same "selected" the sidebar shows.
  const needsChatFirst = usingDock && !!workspaceId && (!dock.state.sessionId || dock.state.sessionId !== globalActiveChatId);
  // Distinguishes the two reasons needsChatFirst can be true, purely for
  // copy/button-label purposes below — both still gate the same way.
  // `!dock.state.sessionId` (this dock has never resolved to a chat at
  // all, in this store) is the "genuinely no chat yet" case; a present-
  // but-mismatched sessionId means a chat exists, it's just not the one
  // currently active — "Create first chat" would be misleading there.
  const needsBrandNewChat = needsChatFirst && !dock.state.sessionId;
  const [creatingChat, setCreatingChat] = useState(false);
  async function handleCreateFirstChat() {
    setCreatingChat(true);
    try {
      await createWorkspaceChat(workspaceId);
    } finally {
      setCreatingChat(false);
    }
  }

  // Local to this component instance — only meaningful in dock mode. See
  // header comment for why these can't come from SessionContext OR the
  // dock store.
  const [dockMode, setDockMode] = useState("auto");
  const [dockReviewBeforeDispatch, setDockReviewBeforeDispatch] = useState(false);

  const messages = dock.state.messages;
  const loading = dock.state.loading;
  const hasMoreOlder = dock.state.hasMoreOlder;   // NEW — perf audit item #3
  const loadingOlder = dock.state.loadingOlder;   // NEW — perf audit item #3
  const mode = dockMode;
  const setMode = setDockMode;
  const activeMessageIndex = dock.state.activeMessageIndex;
  // Fix (render-loop, take 2): the first fix's `[dock]` dependency was
  // itself the bug — useWorkspaceDock() returns a brand-new object literal
  // on every render (only its individual fields, like setDockState, are
  // internally memoized). Depending on the whole `dock` object meant this
  // callback was STILL recreated every render, which meant rowProps below
  // was still unstable, which is exactly what keeps react-window's
  // internal setIndices effect retriggering. Depending on the specific
  // stable field (dock.setDockState) and the specific primitive value
  // (dock.state.activeMessageIndex) actually breaks the cycle.
  const setActiveMessageIndex = useCallback(
    (i) => {
      if (i === dock.state.activeMessageIndex) return;
      dock.setDockState({ activeMessageIndex: i });
    },
    [dock.setDockState, dock.state.activeMessageIndex]
  );
  const reviewBeforeDispatch = dockReviewBeforeDispatch;   // Part 2 §2.5
  const setReviewBeforeDispatch = setDockReviewBeforeDispatch;   // Part 2 §2.5
  const pendingHireReview = dock.state.pendingHireReview;   // Part 2 §2.5
  const confirmHireReview = dock.confirmHireReview;   // Part 2 §2.5
  const cancelHireReview = dock.cancelHireReview;   // Part 2 §2.5
  // NEW — Phase 4 step 4.5: no legacy-mode equivalent (SessionContext.jsx
  // never grows this field — it's Notebooks Chat-First-only).
  const generationNotifications = dock.state.generationNotifications;

  // Fix (render-loop, take 2): same class of bug as setActiveMessageIndex
  // above — must depend on dock.sendTask (stable) specifically, never on
  // `dock` itself (a fresh object every render).
  const sendTask = useCallback(
    (taskText) => dock.sendTask(taskText, { mode, reviewBeforeDispatch }),
    [dock.sendTask, mode, reviewBeforeDispatch]
  );

  const textareaRef = useRef(null);
  const chatContainerRef = useRef(null);
  // Perf audit #3 step 5d — imperative handle for the List below. react-window
  // v2 exposes { element, scrollToRow } here instead of v1's scrollToItem/
  // resetAfterIndex. Step 6 wires this up for real (see the scrollToRow
  // effect below, which replaced the old bottomRef.scrollIntoView() call).
  const listRef = useRef(null);
  // Perf audit item #3: { prevLength, index } captured right before a
  // "load older messages" fetch fires, consumed by the two effects near
  // scrollToRow below once the prepend actually lands. null whenever no
  // load-older is in flight/pending — that's also the signal the normal
  // bottom-follow effect checks to decide whether it's safe to run.
  const prependAnchorRef = useRef(null);
  const isSyncingRef = useRef(false); // shared lock, passed to WorkingPanel's scroll handler too
  // Perf audit #3 step 8 fix — step 5a's plan (a hand-rolled heightCache
  // ref + a manual resetAfterIndex call) was written against react-window
  // v1 and never actually got finished for v2: nothing anywhere wrote
  // into heightCache, so getItemSize always fell through to
  // ESTIMATED_ROW_HEIGHT — every row was silently pinned at 88px forever,
  // and v2 doesn't even have a resetAfterIndex to call once a real
  // measurement came in. This would have failed step 8's stress test the
  // moment a Mermaid diagram or code block finished its async render and
  // grew taller than the estimate: react-window would still think the row
  // is 88px, so the row below it would start overlapping the bottom of
  // the diagram instead of being pushed down.
  //
  // v2 ships a built-in answer to exactly this — useDynamicRowHeight —
  // instead of hand-rolling the cache/invalidation dance ourselves.
  // `key: dock.state.sessionId` clears its measured-height map whenever
  // the active chat changes, so heights measured in one chat can't leak
  // into a different one when this same panel instance gets reused
  // across chats (see the Row component below for why keying by row
  // index rather than message id is fine here).
  const ESTIMATED_ROW_HEIGHT = 88; // rough single-paragraph-message guess; only used before a row's first real measurement
  const dynamicRowHeight = useDynamicRowHeight({
    defaultRowHeight: ESTIMATED_ROW_HEIGHT,
    key: dock.state.sessionId,
  });

  // Perf audit #3 step 9 cleanup: step 5c's containerSize state + the
  // ResizeObserver effect that fed it are gone. They were written when
  // VariableSizeList's explicit height/width props were still the plan;
  // once step 5d landed on react-window v2's `List` (which sizes itself
  // off its own parent via its own internal ResizeObserver — see the
  // import comment above), nothing in this file ever read containerSize
  // again. Confirmed via search before removing.

  const [modeOpen, setModeOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const [workingPanelCollapsed, setWorkingPanelCollapsed] = useState(false);
  const [workingPanelWidth, setWorkingPanelWidth] = useState(WORKING_PANEL_DEFAULT_WIDTH);
  const [workingPanelHeight, setWorkingPanelHeight] = useState(WORKING_PANEL_DEFAULT_HEIGHT); // NEW — stacked layout's counterpart to workingPanelWidth
  const resizeCleanupRef = useRef(null); // holds the active mousemove/mouseup remover, if a drag is in progress

  // NEW — Data Layer §4b: attach button state. Same "own pushItem/
  // settleItem, own mountedRef guard" shape as IngestionDropzone.jsx,
  // scaled down to what a composer needs (no drag target, no url field).
  const attachInputRef = useRef(null);
  const [attachItems, setAttachItems] = useState([]);
  const attachMountedRef = useRef(true);
  useEffect(() => {
    attachMountedRef.current = true;
    return () => { attachMountedRef.current = false; };
  }, []);

  function pushAttachItem(name) {
    const id = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    setAttachItems((prev) => [{ id, name, status: "pending", message: "" }, ...prev]);
    return id;
  }

  function settleAttachItem(id, status, message) {
    if (!attachMountedRef.current) return;
    setAttachItems((prev) => prev.map((it) => (it.id === id ? { ...it, status, message } : it)));
    if (status === "done") {
      setTimeout(() => {
        if (!attachMountedRef.current) return;
        setAttachItems((prev) => prev.filter((it) => it.id !== id));
      }, ATTACH_DONE_AUTOCLEAR_MS);
    }
  }

  // Same independent-per-file shape as IngestionDropzone.jsx's
  // handleFiles() (a slow/stuck file shouldn't block the others), pointed
  // at the identical process_upload()-backed endpoints via
  // ingestFileByExtension() (lib/ingestDispatch.js) — §4b's whole point is
  // "the same upload path, reachable from every chat tab now."
  async function handleAttachFiles(fileList) {
    if (!workspaceId) return;
    await Promise.allSettled(
      Array.from(fileList).map(async (file) => {
        const id = pushAttachItem(file.name);
        try {
          const result = await ingestFileByExtension(
            file, { ingestFile, ingestPdfFile, ingestVoiceFile }, workspaceId
          );
          settleAttachItem(id, "done", `${result.node_ids?.length || 0} node(s)`);
        } catch (err) {
          if (err?.isTimeout) {
            settleAttachItem(id, "timeout", "Still working — check Sources in a bit");
          } else {
            settleAttachItem(id, "error", String(err.message || err));
          }
        }
      })
    );
  }

  useEffect(() => {
    setWorkingPanelCollapsed(localStorage.getItem(WORKING_PANEL_KEY) === "1");
    const savedWidth = parseInt(localStorage.getItem(WORKING_PANEL_WIDTH_KEY), 10);
    if (!Number.isNaN(savedWidth)) setWorkingPanelWidth(clampWorkingPanelWidth(savedWidth));
    const savedHeight = parseInt(localStorage.getItem(WORKING_PANEL_HEIGHT_KEY), 10);
    if (!Number.isNaN(savedHeight)) setWorkingPanelHeight(clampWorkingPanelHeight(savedHeight));
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

  // NEW — stacked layout's counterpart to startWorkingPanelResize above.
  // Handle sits on the Working Panel's bottom edge (it's docked to the
  // top, Chat Box below it), so dragging down grows it and dragging up
  // shrinks it — same "only hit localStorage on mouseup" throttling.
  function startWorkingPanelResizeVertical(e) {
    e.preventDefault();
    const startY = e.clientY;
    const startHeight = workingPanelHeight;

    function onMouseMove(ev) {
      const deltaY = ev.clientY - startY;
      setWorkingPanelHeight(clampWorkingPanelHeight(startHeight + deltaY));
    }
    function onMouseUp() {
      cleanup();
      setWorkingPanelHeight((h) => {
        localStorage.setItem(WORKING_PANEL_HEIGHT_KEY, String(h));
        return h;
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

  // Perf audit #3 step 6 — bottomRef.scrollIntoView() replaced. That div
  // no longer sits inside a real scroll container (step 5d moved scrolling
  // onto List's own internal scrollport — see the comment above
  // chatContainerRef's div), so scrollIntoView had nothing meaningful left
  // to do. react-window v2's imperative API is scrollToRow({ index, align,
  // behavior }), not v1's scrollToItem — matches the note already left on
  // listRef's declaration above.
  //
  // Perf audit item #3, pagination follow-up: this used to key ONLY off
  // messages.length, which broke the moment "load older messages" started
  // prepending to the front of the array — a scroll-up load also changes
  // messages.length, and this effect can't tell "grew at the bottom" (a
  // new message arrived, scroll down) from "grew at the top" (older
  // messages loaded, scroll position should NOT move) using length alone.
  // prependAnchorRef (set by handleLoadOlder below, right before it calls
  // the store's loadOlderMessages) is the signal: when it's non-null this
  // render, a prepend is in flight/just landed, so this effect backs off
  // and lets the anchor-restoring effect below handle scroll position
  // instead. It's cleared there once consumed.
  useEffect(() => {
    if (messages.length === 0) return;
    if (prependAnchorRef.current) return;
    listRef.current?.scrollToRow({
      index: messages.length - 1,
      align: "end",
      behavior: "smooth",
    });
  }, [messages.length, loading]);

  // Perf audit item #3: after a "load older messages" prepend lands,
  // jump back to the same message the person was looking at rather than
  // letting the list silently re-anchor to whatever now sits at the old
  // scroll offset (which, since everything shifted down by the number of
  // newly-inserted rows, would visually yank the view further down the
  // list than where they were reading). prependAnchorRef carries the
  // pre-fetch { index, count } captured in handleLoadOlder; this effect
  // only fires once new rows have actually landed (messages.length grew
  // by that same count), then restores position and clears the anchor so
  // the effect above resumes its normal bottom-follow behavior on the
  // next genuinely-new message.
  useEffect(() => {
    const anchor = prependAnchorRef.current;
    if (!anchor) return;
    if (messages.length <= anchor.prevLength) return; // fetch still in flight
    const insertedCount = messages.length - anchor.prevLength;
    listRef.current?.scrollToRow({
      index: anchor.index + insertedCount,
      align: "start",
    });
    prependAnchorRef.current = null;
  }, [messages.length]);

  // Auto-grow the textarea as the person types multiple lines, capped so
  // it doesn't swallow the whole viewport on a very long paste.
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 240)}px`;
  }, [draft]);

  const TARGETS_BY_KEY = Object.fromEntries(TARGETS.map((t) => [t.key, t]));

  // NEW — Notebooks Chat-First refinement, Phase 2 step 2.6. Factored out
  // of tryHandleGenerateIntent() below (byte-for-byte the same body it
  // used to run inline) so both the legacy keyword-parser short-circuit
  // AND the new real-tool-calling branch (tryHandleClassifiedToolCall(),
  // further down) dispatch through the exact same generateNotebooks()
  // call, Working Panel branch bookkeeping, and "jump to the tab that
  // just got new content" behavior — one dispatch path, two different
  // ways of deciding to take it.
  //
  // CHANGED — step 2.10. `sourceText` is the raw text that triggered
  // this run (only ever passed by the two chat-intent callers below,
  // never by NotebooksGeneratePicker's manual Generate button, which
  // still only has the Working Panel above to watch). When present, this
  // now ALSO pushes into the message thread: the user's own text
  // (previously silently swallowed — see MessageBubble.jsx's own
  // step-2.10 comment for why) plus a live "generation" message this
  // function updates in place, by runId, as the run resolves. The
  // existing notebooksGenerateRun dock-state write is untouched — that's
  // what the Working Panel graph reads, and this is additive to it, not
  // a replacement for it.
  async function runGenerateTarget(key, scope, sourceText = null) {
    const label = TARGETS_BY_KEY[key]?.label || key;
    const runningBranch = { panel_key: key, status: "running", label, subTab: TARGETS_BY_KEY[key]?.subTab };
    // Unique per call, not per key — re-running the same capability
    // twice in one chat must update its OWN card, not the previous run's.
    const runId = `gen-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    if (usingDock) {
      dock.setDockState({ notebooksGenerateRun: { targets: [key], branches: [runningBranch] } });
      if (sourceText) {
        dock.setDockState((prev) => ({
          messages: [
            ...prev.messages,
            { role: "user", text: sourceText },
            { role: "generation", runId, branches: [runningBranch] },
          ],
        }));
      }
    }
    try {
      const { branches } = await generateNotebooks(workspaceId, [key], scope);
      const withMeta = branches.map((b) => ({ ...b, label: TARGETS_BY_KEY[b.panel_key]?.label, subTab: TARGETS_BY_KEY[b.panel_key]?.subTab }));
      if (usingDock) {
        dock.setDockState({ notebooksGenerateRun: { targets: [key], branches: withMeta } });
        if (sourceText) {
          dock.setDockState((prev) => ({
            messages: prev.messages.map((m) =>
              m.role === "generation" && m.runId === runId ? { ...m, branches: withMeta } : m
            ),
          }));
        }
      }
      // Jump straight to the tab that just got new content so the run
      // doesn't feel like it vanished into the Working Panel — same
      // "open the result" affordance BranchRow's own chevron gives in
      // the picker popover.
      const branch = branches.find((b) => b.panel_key === key);
      if (branch?.status === "done" && TARGETS_BY_KEY[key]?.subTab) {
        onNavigateSubTab?.(TARGETS_BY_KEY[key].subTab);
      }
    } catch (err) {
      const errorBranch = { panel_key: key, status: "error", error: String(err.message || err), label, subTab: TARGETS_BY_KEY[key]?.subTab };
      if (usingDock) {
        dock.setDockState({ notebooksGenerateRun: { targets: [key], branches: [errorBranch] } });
        if (sourceText) {
          dock.setDockState((prev) => ({
            messages: prev.messages.map((m) =>
              m.role === "generation" && m.runId === runId ? { ...m, branches: [errorBranch] } : m
            ),
          }));
        }
      }
    }
    return true;
  }

  // NEW — Notebooks Chat-First refinement, Phase 6 step 6.8. The
  // mark_topic_done counterpart to runGenerateTarget() above: same
  // "push the user's text, then a live status message, update it in
  // place by runId" shape, but there's no BranchRow/Working Panel
  // bookkeeping to do here -- this isn't a generation, just a one-field
  // progress-store write (see SessionContext.jsx's markTopicDone(),
  // which reuses step 6.5's manual-override PUT route).
  //
  // No confirmation step before calling markTopicDone() -- this mirrors
  // the guide's step 6.8 decision (low-stakes, reversible: a person can
  // always re-open the board and flip it back, same as any other manual
  // status edit). If that decision ever changes, this is the one place
  // to add a confirm-first branch.
  async function runMarkTopicDone(topicId, sourceText) {
    const runId = `done-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    if (usingDock && sourceText) {
      dock.setDockState((prev) => ({
        messages: [
          ...prev.messages,
          { role: "user", text: sourceText },
          { role: "progress_update", runId, status: "running" },
        ],
      }));
    }
    try {
      await markTopicDone(workspaceId, topicId);
      if (usingDock && sourceText) {
        dock.setDockState((prev) => ({
          messages: prev.messages.map((m) =>
            m.role === "progress_update" && m.runId === runId
              ? { ...m, status: "done", topicId }
              : m
          ),
        }));
      }
    } catch (err) {
      if (usingDock && sourceText) {
        dock.setDockState((prev) => ({
          messages: prev.messages.map((m) =>
            m.role === "progress_update" && m.runId === runId
              ? { ...m, status: "error", error: String(err.message || err) }
              : m
          ),
        }));
      }
    }
    return true;
  }

  // NEW — chat audit bug #1 fix. Per NotebooksGeneratePicker.jsx's own
  // long-standing SCOPE NOTE: "typing 'make flashcards' into
  // WorkspaceChatPanel should short-circuit the normal staffed-dispatcher
  // send and land here instead." This reuses that component's exact
  // free-text parser/keyword table (now exported) rather than a second
  // copy, and only auto-runs on the SAME single-unambiguous-target case
  // the picker's own free-text field already treats as safe to dispatch
  // without a human reviewing chips first (guide §4.1's "accepting the
  // misparse risk" is explicitly scoped to that one case, not to
  // multi-target or scope-language sentences — those still deserve a
  // chip row to look at, so they fall through to the picker/normal chat
  // instead of silently running here).
  //
  // Only wired when this panel is embedded with a real workspaceId (i.e.
  // docked inside Notebooks/Research/etc, per the constructor comment
  // above) — a standalone Chat tab with no workspace has nothing for
  // "generate a mind map" to mean, so it's left to fall through to the
  // ordinary staffed dispatcher there, same as free text that doesn't
  // match a target at all.
  //
  // CHANGED — step 2.6: this stays the FIRST thing checked, ahead of the
  // new real-tool-calling branch below. It's a cheap synchronous keyword
  // match (no network round trip) that's already shipped and proven —
  // there's no reason to wait on an LLM call to catch a case this
  // already catches for free, and keeping it first means step 2.6 can't
  // regress anything this already handles.
  async function tryHandleGenerateIntent(text) {
    if (!workspaceId || !generateNotebooks) return false;
    const { targetKeys, sourceNodeIds } = parseFreeText(text, []);
    if (targetKeys.length !== 1 || sourceNodeIds.length > 0) return false;

    // FIX — overlap-check finding (2026-08-02): this fast path predates
    // topic_notes (scopeAllowed: "topic") and always dispatched with
    // scope=null. That's correct for "whole"-scope targets, but for a
    // "topic"-scope target it means _generate_topic_notes() on the
    // server unconditionally raises "topic_notes requires scope.topic_id"
    // -- so typing exactly the phrasing topic_notes's own manifest
    // description recommends ("write a note on <topic>") always failed,
    // short-circuiting past the scope-aware classifier below (which
    // already knows how to resolve a topic from activeContext or the
    // model's own arguments -- see tryHandleClassifiedToolCall's
    // mark_topic_done/source_ids handling) before it ever got a turn.
    // Same activeContext fallback as that branch, not a new rule: if a
    // topic is in scope, dispatch with it; if not, don't guess -- fall
    // through so the classifier (or, failing that, sendTask()) can
    // still make sense of it.
    const target = TARGETS_BY_KEY[targetKeys[0]];
    if (target?.scopeAllowed === "topic") {
      const topicId = activeContext?.type === "topic" ? activeContext.id : null;
      if (!topicId) return false;
      return runGenerateTarget(targetKeys[0], { topic_id: topicId }, text);
    }

    return runGenerateTarget(targetKeys[0], null, text);
  }

  // NEW — Notebooks Chat-First refinement, Phase 2 step 2.5. Fires the
  // real tool-calling classification pass (POST .../notebooks/
  // classify-intent, see SessionContext.jsx's classifyIntent()) for
  // every outgoing message, purely so the result can be eyeballed in
  // the console against real usage before step 2.6 wires up an actual
  // branch. Deliberately:
  //   - NOT awaited before sendTask()/tryHandleGenerateIntent() below —
  //     classification is a side channel, not a gate, so it must add
  //     zero latency to the real send path and must never be able to
  //     block or fail it.
  //   - gated on workspaceId, same as tryHandleGenerateIntent() above —
  //     a standalone Chat tab with no workspace has nothing for the
  //     classifier to run against either.
  //   - console.log-only. No dispatch, no UI change, no state write.
  //     Whether this message also matched tryHandleGenerateIntent()'s
  //     keyword parser (and thus already short-circuited to
  //     generateNotebooks() above) is irrelevant here — this call runs
  //     unconditionally so the two approaches' outputs can be compared
  //     side by side in the logs.
  //
  // CHANGED — step 2.6: this is now only the FLAG-OFF path (see
  // handleSubmit below). When CHAT_TOOL_CALLING_ENABLED is on,
  // tryHandleClassifiedToolCall() below does its own (awaited, gating)
  // classifyIntent() call instead — calling it a second time here too
  // would double up the LLM request for no reason. Body is otherwise
  // unchanged from step 2.5.
  function logClassifiedIntent(text) {
    if (!workspaceId || !classifyIntent) return;
    classifyIntent(workspaceId, text)
      .then((result) => {
        console.log("[chat classify-intent] (2.5, log-only)", { message: text, result });
      })
      .catch((err) => {
        console.log("[chat classify-intent] (2.5, log-only) failed", { message: text, err });
      });
  }

  // NEW — Notebooks Chat-First refinement, Phase 2 step 2.6. The actual
  // "high-confidence tool call" branch: awaits the same classify-intent
  // call step 2.5 only logged, and — only on a single, unambiguous,
  // error-free tool call — dispatches it through runGenerateTarget(),
  // same as the legacy keyword path above. Anything less clean
  // (ambiguous, ".error" set, no tool call at all, or a tool name this
  // build doesn't recognize) returns false so handleSubmit's existing
  // fallback takes it to sendTask() — the ordinary staffed dispatcher —
  // same as today (step 2.7's requirement, satisfied by this same
  // "return false and let the caller fall through" shape).
  //
  // Tool names are "generate_" + the capability key (see
  // utils/capability_tools.py's manifest_to_tools()), and TARGETS_BY_KEY
  // is keyed by that same capability key — both frontend TARGETS and the
  // backend CAPABILITIES_MANIFEST are hand-kept in sync on this exact
  // key string (see notebookCapabilities.js's own header comment), so
  // stripping the prefix and looking it up here is safe.
  //
  // scope mapping: every capability actually enabled today is
  // scopeAllowed "whole" (no required args), so `arguments` normally
  // comes back empty. The source_ids -> source_node_ids translation
  // below is forward-looking for if/when a "sources"-scope capability
  // gets enabled — manifest_to_tools() names the arg "source_ids" for
  // the model, but NOTEBOOKS_GENERATE_TARGETS' scope dict reads
  // "source_node_ids" (see api/server.py's _generate_backlinks/
  // _generate_workflows), so this is not just a passthrough.
  async function tryHandleClassifiedToolCall(text) {
    if (!workspaceId || !generateNotebooks || !classifyIntent) return false;

    const result = await classifyIntent(workspaceId, text);
    console.log("[chat classify-intent] (2.6, dispatching)", { message: text, result });

    if (result?.error || result?.ambiguous || !result?.tool_calls?.length) {
      return false;
    }

    const call = result.tool_calls[0];

    // NEW — Phase 6 step 6.8. mark_topic_done is a hand-written,
    // non-generation tool (see utils/capability_tools.py's
    // study_progress_tools()) — it isn't in TARGETS_BY_KEY at all, so
    // it has to be checked ahead of the "generate_" lookup below rather
    // than folded into it. Requires a topic_id from either the model's
    // own arguments (it named the topic in the message text) or a
    // topic-typed activeContext, same fallback order the source_ids ->
    // activeContext resolution below uses. No topic to act on at all
    // (neither the model nor the surrounding UI knows which one) falls
    // through to sendTask() same as any other non-match — there's
    // nothing safe to guess here.
    if (call.name === "mark_topic_done") {
      const topicId = call.arguments?.topic_id
        || (activeContext?.type === "topic" ? activeContext.id : null);
      if (!topicId) return false;
      return runMarkTopicDone(topicId, text);
    }

    const key = call.name?.startsWith("generate_") ? call.name.slice("generate_".length) : null;
    if (!key || !TARGETS_BY_KEY[key]) {
      // Unknown/unrecognized tool name -- shouldn't happen since tools
      // are built from the same manifest TARGETS_BY_KEY comes from, but
      // never silently dispatch something we can't label or navigate
      // for. Fall through to sendTask() same as any other non-match.
      return false;
    }

    // NEW — step 2.6a: the model only returns source_ids when the
    // message itself named/implied a specific source ("summarize the
    // Chen paper"), so that branch is untouched and still wins outright.
    // When it comes back empty -- the message didn't name anything --
    // fall back to activeContext instead of going straight to null
    // (whole-workspace). Only a source-typed activeContext can resolve
    // to source_node_ids today, since NOTEBOOKS_GENERATE_TARGETS has no
    // topic-scoped capability live yet (see the comment above); a
    // topic-typed activeContext with nothing live to consume it still
    // falls through to null rather than sending an argument no backend
    // route reads.
    const scope = call.arguments?.source_ids?.length
      ? { source_node_ids: call.arguments.source_ids }
      : activeContext?.type === "source" && activeContext.id
      ? { source_node_ids: [activeContext.id] }
      : null;
    return runGenerateTarget(key, scope, text);
  }

  // NEW — step 2.6 feature flag. Default OFF: classifyIntent() keeps
  // running and logging (step 2.5, unchanged) either way, but nothing
  // acts on its result — sendTask() still fires on every non-keyword-
  // matched message — until this is explicitly turned on. Flip with
  // NEXT_PUBLIC_CHAT_TOOL_CALLING_ENABLED=1 in the frontend env once
  // step 2.4's findings hold up against real traffic, not just the
  // canned test-harness messages.
  const CHAT_TOOL_CALLING_ENABLED = process.env.NEXT_PUBLIC_CHAT_TOOL_CALLING_ENABLED === "1";

  // NEW — Phase 3 step 3.3. Pulled out of handleSubmit below (byte-for-
  // byte the same body it used to run inline on `draft`) so there are
  // two callers of the SAME dispatch path instead of a second one:
  // handleSubmit (typed input) and the affinity-suggestion card's
  // Generate button (MessageBubble.jsx's AffinitySuggestionCard, via
  // the onSendCommand prop below) both now funnel through here. This is
  // exactly the guide's "a button that just re-sends the equivalent
  // chat command — don't build a second execution path for this" —
  // clicking Generate quiz behaves identically to a person typing
  // "Generate quiz": same keyword short-circuit first
  // (tryHandleGenerateIntent), same tool-calling classification fallback
  // (tryHandleClassifiedToolCall), same sendTask() dispatcher as a last
  // resort. Nothing about accepting a suggestion needs its own
  // generateNotebooks() call.
  // Fix (render-loop): wrapped in useCallback so this has a stable identity
  // across renders — it's passed into rowProps below (onSendCommand), and an
  // unstable rowProps object was part of the same chain that caused the
  // "Maximum update depth exceeded" crash. See handleRowsRendered's comment
  // for the full chain.
  const dispatchText = useCallback(
    (text) => {
      tryHandleGenerateIntent(text).then((handled) => {
        if (handled) return;
        if (CHAT_TOOL_CALLING_ENABLED) {
          tryHandleClassifiedToolCall(text).then((toolHandled) => {
            if (!toolHandled) sendTask(text);
          });
        } else {
          logClassifiedIntent(text);
          sendTask(text);
        }
      });
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps -- tryHandleGenerateIntent/tryHandleClassifiedToolCall/logClassifiedIntent are recreated every render themselves (not yet stabilized); depending on the now-stable `sendTask` is what actually matters for rowProps below.
    [sendTask]
  );

  function handleSubmit(e) {
    e?.preventDefault();
    const text = draft.trim();
    if (!text || loading) return;
    setDraft("");
    dispatchText(text);
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

  // Perf audit #3 step 7 — cross-panel scroll sync, rewired for
  // react-window v2. The old handleChatScroll (DOM-distance loop over
  // messageRefs.current) is broken under virtualization: react-window
  // only mounts on-screen rows, so anything scrolled out of view is
  // `null` in messageRefs — the loop would silently skip most of a long
  // chat and pick the wrong index (or none).
  //
  // v2's ListImperativeAPI has no "row index at scroll offset" query
  // (just `element` + `scrollToRow`), but it doesn't need one:
  // `onRowsRendered` already reports the live visible-row range on every
  // scroll/resize with no DOM re-derivation required.
  // `visibleRows.startIndex` (topmost row with any pixel on-screen) is a
  // like-for-like swap for what handleChatScroll was approximating —
  // in a top-to-bottom stack, "row whose top is closest to the
  // container's top" and "topmost visible row" agree in effectively all
  // cases. Same isSyncingRef guard as before, unchanged semantics.
  // Perf audit item #3: fires the actual "load older messages on
  // scroll-up" fetch. Captures the pre-fetch anchor (current earliest
  // rendered row + current message count) BEFORE calling the store's
  // loadOlderMessages, since that call resolves asynchronously and this
  // component only sees the result once the dock state it's subscribed
  // to updates — the two effects near listRef.scrollToRow above consume
  // this anchor once messages.length actually grows.
  const handleLoadOlder = useCallback(() => {
    if (loadingOlder || !hasMoreOlder || !chatId) return;
    prependAnchorRef.current = { prevLength: messages.length, index: activeMessageIndex ?? 0 };
    loadOlderMessages(chatId);
  }, [loadingOlder, hasMoreOlder, chatId, messages.length, activeMessageIndex, loadOlderMessages]);

  // visibleRows.startIndex <= 3 (not just === 0) so the fetch starts a
  // little before the person hits the literal top row — same "prefetch
  // just ahead of the edge" idea as overscanCount below, just applied to
  // data-fetching instead of DOM mounting. loadingOlder/hasMoreOlder are
  // also checked inside handleLoadOlder itself, so this can fire on every
  // qualifying scroll event without needing its own separate guard here.
  // BUGFIX (Maximum update depth exceeded / react-window useVirtualizer
  // setIndices crash): this was a plain function, recreated on every render,
  // passed straight into <List onRowsRendered={...}>. react-window
  // re-invokes onRowsRendered whenever that reference changes (it's in the
  // dependency array of its own internal effect) — not only when the
  // visible range actually changes. It then called setActiveMessageIndex
  // unconditionally, which wrote to dock state unconditionally, which
  // re-rendered this component, which created a new handleRowsRendered
  // reference, which react-window called again — an infinite loop. Wrapping
  // this in useCallback (so its identity is stable across unrelated
  // renders) plus the equality guard below (so it doesn't fire a state
  // update when the index hasn't actually changed) closes the loop.
  const handleRowsRendered = useCallback(
    (visibleRows) => {
      if (isSyncingRef.current) return;
      if (visibleRows.startIndex !== activeMessageIndex) {
        setActiveMessageIndex(visibleRows.startIndex);
      }
      if (visibleRows.startIndex <= 3) handleLoadOlder();
    },
    [activeMessageIndex, setActiveMessageIndex, handleLoadOlder]
  );

  // Fix (render-loop): memoized so this object's identity only changes when
  // one of its actual contents changes, instead of on every render — it's
  // passed as <List rowProps={...}>, and an unstable rowProps object was
  // part of the same instability that fed the crash above.
  const rowProps = useMemo(
    () => ({
      messages,
      onSelect: setActiveMessageIndex,
      onNavigateSubTab,
      onSendCommand: dispatchText,
    }),
    [messages, setActiveMessageIndex, onNavigateSubTab, dispatchText]
  );

  // Perf audit #3 step 8 fix — Row now lives at module scope (see
  // VirtualMessageRow above this component) instead of being redefined
  // here on every render. Wiring its data through rowProps below, rather
  // than closing over messages/setActiveMessageIndex/etc from this
  // render's scope.

  const activeMode = MODES.find((m) => m.id === mode) || MODES[0];
  const ActiveIcon = activeMode.icon;

  // NEW — §6: whole-panel collapsed rail. Only reachable when a parent
  // passes collapsed=true (i.e. when docked inside a domain tab) — the
  // standalone ChatTab wrapper never does this, so nothing changes there.
  if (collapsed) {
    return stacked ? (
      <div className="w-full h-10 flex flex-row items-center border-b border-[var(--neutral-800)] px-2">
        <button
          onClick={onToggleCollapse}
          title="Show chat"
          className="text-[var(--neutral-500)] hover:text-[var(--neutral-300)] p-1.5 rounded-md hover:bg-[var(--neutral-900)] transition-colors"
        >
          <MessageSquare size={16} />
        </button>
      </div>
    ) : (
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

  // NEW — Item 2 remaining piece, live-run-state slice, step 2: nothing
  // resolved to a dock key (see this file's DUAL MODE comment up top for
  // why that's a real, common state for TestTab/BuildTab/ResearchTab/
  // PlanTab, not just a mount-time gap). `standalone` (only ChatTab.jsx
  // passes it) distinguishes its transient "bootstrap hasn't resolved
  // chatId yet" case, worth different copy than "nothing selected."
  if (!usingDock) {
    return (
      <div
        className={
          stacked
            ? "flex flex-col items-center justify-center h-full w-full text-center px-6 py-10"
            : "hidden lg:flex flex-col items-center justify-center h-full w-full text-center px-6 py-10"
        }
      >
        <MessageSquare size={28} className="text-[var(--neutral-700)] mb-3" />
        <p className="text-sm text-[var(--neutral-400)]">
          {standalone ? "Loading your chat…" : "Select a project to chat"}
        </p>
      </div>
    );
  }

  return (
    <div className={stacked ? "flex flex-col h-full" : "flex h-full max-w-6xl mx-auto"}>
      {/* LEFT (or BOTTOM, when stacked) — Chat Box. `order-2` only takes
          effect in stacked mode (flex-col), putting this below the
          Working Panel without needing to reorder the JSX itself. */}
      <div className={`flex flex-col flex-1 ${stacked ? "min-h-0 order-2 border-t" : "min-w-0 border-r"} border-[var(--neutral-800)]`}>
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

        {/* Perf audit #3 (message-list virtualization) — original pre-work
            checklist, kept as a record of what had to keep working across
            the whole migration. All four landed:
              1. Auto-scroll to bottom on new message — now
                 listRef.current?.scrollToRow(...) (step 6).
              2. Cross-panel scroll sync with WorkingPanel — now driven by
                 onRowsRendered/handleRowsRendered (step 7); the old
                 messageRefs DOM-ref loop this replaced has been removed
                 (step 9).
              3. Message heights are NOT uniform — markdown, code blocks,
                 and Mermaid diagrams (which render async and change
                 height after mount) — handled by react-window v2's
                 useDynamicRowHeight (step 8).
              4. Tabs stay mounted (display:none) rather than unmounting
                 (AppShell.jsx) — scroll position survives a tab switch
                 since it lives in List's own scrollport, not this
                 component's state. */}
        <div
          ref={chatContainerRef}
          className="flex-1 min-h-0 flex flex-col px-4 py-6"
        >
          {/* Perf audit #3 step 5d — this div is now just a sized wrapper;
              `overflow-y-auto`/`space-y-4` came off it because List owns
              its own scrolling and absolutely-positions each row (a CSS
              gap doesn't apply to that), and the scroll-sync callback
              moved onto List itself below (`onRowsRendered`, wired in
              step 7), since List's outer div is the real scroll container
              now, not this wrapper. chatContainerRef stays attached here
              rather than deleted — nothing in this file reads its
              getBoundingClientRect() anymore post-step-7, but WorkingPanel
              still gets `isSyncingRef` from this component and the two
              panels' layout code elsewhere may still expect the ref to
              exist; not worth chasing down as part of this substep. */}
          {needsChatFirst ? (
            <p className="text-[var(--neutral-500)] text-sm">
              {needsBrandNewChat
                ? "This project doesn't have a chat yet — create one below to start."
                : "No chat selected — pick one from the sidebar, or start a new one below."}
            </p>
          ) : messages.length === 0 && (
            <p className="text-[var(--neutral-500)] text-sm">
              Send a task — the EO layer will classify it and route it through
              the appropriate tier.
            </p>
          )}
          {/* Perf audit #3, step 4 (design-only — no VariableSizeList yet,
              that's step 5): row-height strategy for when MessageRow below
              moves out of this plain .map() and into react-window.

              Problem: messages aren't fixed-height (markdown, code blocks,
              Mermaid diagrams — see step 2's checklist above), so this has
              to be VariableSizeList, which needs a height *estimate* per
              row up front and a way to correct that estimate after the
              real content mounts.

              Planned shape:
                - heightCache = useRef({}) keyed by a stable message id
                  (index is NOT stable enough here — react-window indices
                  shift if messages are ever pruned/reordered, so this
                  should be whatever stable id MessageRow/message objects
                  already carry, falling back to index only if nothing
                  else exists).
                - MessageRow measures its own rendered height via
                  ResizeObserver after mount (and on every subsequent
                  resize — this is what catches a Mermaid diagram or
                  syntax-highlighted code block finishing its async render
                  and growing taller than the initial estimate).
                - On a measured height that differs from what's cached,
                  update heightCache.current[id] and call
                  listRef.current.resetAfterIndex(i) so react-window
                  re-lays-out from that row down instead of leaving stale
                  offsets for every row below it.
                - VariableSizeList's itemSize=(i) => heightCache.current[id
                  for row i] ?? someReasonableDefaultEstimate.

              Open questions to settle before step 5 writes any of this:
                - Where does listRef live — created here and passed down,
                  or does react-window's own ref suffice without a second
                  wrapper?
                - Does the ResizeObserver belong inside MessageRow itself
                  (one observer per row) or should this component own a
                  single observer and .observe() each row's node — the
                  former is simpler per-row but is one observer per
                  on-screen row at all times; the latter is more setup
                  but only one observer total.
                - Default/initial estimate: a flat guess (e.g. one line of
                  text) will undercount code/Mermaid messages on first
                  paint before ResizeObserver's first callback fires —
                  worth a slightly taller default for messages known up
                  front to contain a code block or diagram, if that's
                  cheap to detect from the message content.

              This comment is the pinned-down plan per the audit's own
              guidance to design the height strategy before touching code,
              since it's called out as the piece most likely to need
              rework. Nothing below this comment changes yet. */}
          {/* Perf audit #3 step 5d — the plain .map() is now react-window's
              List, wrapping the Row function from step 5b. rowHeight was
              step 5a's getItemSize (a hand-rolled, never-populated cache)
              until the step 8 fix swapped it for dynamicRowHeight, v2's
              own dynamic-height object — see that fix's comment above for
              why. No width/height props — List fills this wrapper div on
              its own. onScroll moves here since List's outer div (not the
              wrapper above) is the actual scrollport now.

              Smoke-test scope only (step 5e): a short chat should render
              virtualized and scroll with mouse/trackpad. At this substep,
              bottomRef auto-scroll and cross-panel sync were both expected
              to be broken — not a regression from this substep, just not
              fixed yet. Step 6 (listRef.scrollToRow, above) fixed
              auto-scroll; step 7 (handleRowsRendered, above) fixed
              cross-panel sync. */}
          {/* Perf audit item #3: loadingOlder is set by the store while a
              "load older messages" fetch (triggered from handleRowsRendered
              near the top of this file) is in flight. This sits above
              List's own scrollport rather than inside it, so it doesn't
              consume a virtualized row index or interfere with
              prependAnchorRef's position-restore effect above. */}
          {loadingOlder && (
            <div className="flex items-center justify-center gap-2 py-2 text-xs text-[var(--neutral-500)]">
              <Loader2 size={12} className="animate-spin" />
              Loading older messages…
            </div>
          )}
          <List
            listRef={listRef}
            rowComponent={VirtualMessageRow}
            rowCount={messages.length}
            rowHeight={dynamicRowHeight}
            rowProps={rowProps}
            overscanCount={6}
            onRowsRendered={handleRowsRendered}
          />
          {/* NOTE: the old `space-y-4` gap doesn't carry over — List rows
              are position:absolute, so a parent `space-y-*`/gap utility is
              a no-op on them. Gap has to move into Row's own wrapper div
              (e.g. paddingBottom) in step 5b's Row, which also means
              bumping ESTIMATED_ROW_HEIGHT and any measured heights to
              include it. Left as a known gap for 5e's smoke test rather
              than folded in here, since it touches an already-committed
              substep. */}
          {loading && (
            <div className="text-[var(--neutral-500)] text-sm animate-pulse">Working…</div>
          )}
          {/* NEW — Phase 4 step 4.6: live feed of generationNotifications
              (step 4.5's dock-state field, fed straight off the
              session-${session_id} Pusher channel — no polling, updates
              land here the moment eo/notify.py's _deliver() fires). Mounted
              here rather than as its own "generation" message-role entry
              like BranchRow's picker/chat-triggered runs (Phase 2 step
              2.10): these events aren't tied to any one chat turn (a
              proactive Phase-3 suggestion's generation has no preceding
              user message), so they get their own small standalone area
              instead of being spliced into the messages array.
              onNavigate — step 4.7 — reuses the exact same
              `onNavigateSubTab?.(subTab)` pass-through MessageBubble.jsx
              already gives BranchRow above, so a "done" notification's
              chevron opens the same sub-tab a BranchRow chevron would for
              the same panel_key; only rendered once a "done" row's target
              actually has a subTab (see GenerationNotificationRow's own
              guard). */}
          {generationNotifications.length > 0 && (
            <div className="space-y-1.5">
              {generationNotifications.map((n) => (
                <GenerationNotificationRow
                  key={`${n.workspaceId}:${n.panelKey}`}
                  notification={n}
                  onNavigate={(subTab) => onNavigateSubTab?.(subTab)}
                />
              ))}
            </div>
          )}
        </div>

        {/* Part 2 §2.5: the review screen renders in place of the
            compose bar while a preview is awaiting a decision — nothing
            has dispatched yet, so there's nothing for the compose bar to
            usefully do until Confirm/Cancel resolves it. */}
        {needsChatFirst ? (
          <div className="border-t border-[var(--neutral-800)] p-4 flex items-center justify-between gap-3">
            <p className="text-xs text-[var(--neutral-500)]">
              {needsBrandNewChat
                ? "Create a chat to start sending tasks, attaching files, or running Generate."
                : "Select a chat from the sidebar to continue — or start a new one."}
            </p>
            <button
              onClick={handleCreateFirstChat}
              disabled={creatingChat}
              className="flex items-center gap-1.5 text-xs bg-[var(--accent)] text-[var(--accent-text)] rounded-lg px-3 py-1.5 font-medium disabled:opacity-50 shrink-0"
            >
              {creatingChat ? <Loader2 size={13} className="animate-spin" /> : <MessageSquare size={13} />}
              {creatingChat ? "Creating…" : needsBrandNewChat ? "Create first chat" : "Start new chat"}
            </button>
          </div>
        ) : pendingHireReview ? (
          <div className="border-t border-[var(--neutral-800)] p-4">
            <HireReviewScreen
              hires={pendingHireReview.hires}
              onConfirm={confirmHireReview}
              onCancel={cancelHireReview}
            />
          </div>
        ) : (
        <>
        {/* NEW — Data Layer §4b: compact status pills for in-flight/just-
            finished attachments, distinct from IngestionDropzone.jsx's
            fuller progress list (this composer has no room for that, and
            doesn't need drag-and-drop or a url field) — same status
            vocabulary (pending/done/error/timeout) so it reads
            consistently with Notebooks' Sources tab. */}
        {attachItems.length > 0 && (
          <div className="border-t border-[var(--neutral-800)] px-4 pt-3 flex flex-wrap gap-2">
            {attachItems.map((it) => (
              <span
                key={it.id}
                className={`flex items-center gap-1.5 text-[11px] rounded-full border px-2.5 py-1 ${
                  it.status === "error"
                    ? "border-red-900 text-red-400"
                    : it.status === "timeout"
                    ? "border-amber-900 text-amber-400"
                    : it.status === "done"
                    ? "border-green-900 text-green-400"
                    : "border-[var(--neutral-800)] text-[var(--neutral-400)]"
                }`}
              >
                {it.status === "pending" && <Loader2 size={11} className="animate-spin shrink-0" />}
                {it.status === "done" && <CheckCircle2 size={11} className="shrink-0" />}
                {it.status === "error" && <XCircle size={11} className="shrink-0" />}
                {it.status === "timeout" && <AlertTriangle size={11} className="shrink-0" />}
                <span className="truncate max-w-[10rem]">{it.name}</span>
                <span className="text-[var(--neutral-600)]">
                  {it.status === "pending" ? "ingesting…" : it.message}
                </span>
              </span>
            ))}
          </div>
        )}
        <form onSubmit={handleSubmit} className="border-t border-[var(--neutral-800)] p-4 flex gap-2 items-end">
          {/* NEW — Data Layer §4b: attach a file straight into this
              workspace's sources — no separate Sources tab trip needed.
              Disabled without a resolved workspaceId (no active chat yet)
              since process_upload() has nothing to attach the source to. */}
          {/* NEW — §9.1: Notebooks passes hideAttach to drop this affordance
              there (IngestionDropzone.jsx already covers uploads for that
              tab). Every other tab leaves hideAttach unset and is unaffected. */}
          {!hideAttach && (
            <>
              <input
                ref={attachInputRef}
                type="file"
                multiple
                className="hidden"
                onChange={(e) => {
                  if (e.target.files?.length) handleAttachFiles(e.target.files);
                  e.target.value = "";   // allow re-selecting the same file twice in a row
                }}
              />
              <button
                type="button"
                disabled={!workspaceId}
                onClick={() => attachInputRef.current?.click()}
                title={workspaceId ? "Attach a file (PDF, docs, slides, sheets, audio)" : "Open or start a chat to attach files"}
                className="flex items-center justify-center bg-[var(--neutral-900)] border border-[var(--neutral-800)] rounded-lg p-2 text-sm outline-none disabled:opacity-40 hover:border-[var(--neutral-600)] transition-colors shrink-0"
              >
                <Paperclip size={15} className="text-[var(--neutral-400)]" />
              </button>
            </>
          )}

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
            id="chat-message-draft"
            name="chat-message-draft"
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
        </>
        )}
      </div>

      {/* RIGHT (or TOP, when stacked) — Working Panel: resizable when
          open, collapses to a slim icon rail (rather than vanishing
          entirely) so there's always a visible way back in.
          CHANGED — `stacked` (passed by every domain-tab dock) swaps this
          from a right-docked, fixed-WIDTH column (which needed extra
          horizontal space beyond whatever the dock container was given,
          forcing the page to scroll sideways once opened) to a
          top-docked, fixed-HEIGHT row that's always full-width and never
          asks for more width than its container already has. The
          standalone Chat tab doesn't pass `stacked`, so it keeps the
          original side-by-side layout, hidden below lg, unchanged. */}
      <div className={stacked ? "flex flex-col shrink-0 order-1 w-full" : "hidden lg:flex shrink-0"}>
        {workingPanelCollapsed ? (
          stacked ? (
            <div className="h-10 w-full flex flex-row items-center border-b border-[var(--neutral-800)] px-2">
              <button
                onClick={toggleWorkingPanel}
                title="Show Working Panel"
                className="text-[var(--neutral-500)] hover:text-[var(--neutral-300)] p-1.5 rounded-md hover:bg-[var(--neutral-900)] transition-colors"
              >
                <PanelRightOpen size={16} />
              </button>
            </div>
          ) : (
            <div className="w-10 flex flex-col items-center border-l border-[var(--neutral-800)] pt-2">
              <button
                onClick={toggleWorkingPanel}
                title="Show Working Panel"
                className="text-[var(--neutral-500)] hover:text-[var(--neutral-300)] p-1.5 rounded-md hover:bg-[var(--neutral-900)] transition-colors"
              >
                <PanelRightOpen size={16} />
              </button>
            </div>
          )
        ) : stacked ? (
          <div className="flex flex-col w-full" style={{ height: workingPanelHeight }}>
            <div className="flex-1 min-h-0 flex flex-col border-b border-[var(--neutral-800)]">
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
                <WorkingPanel isSyncingRef={isSyncingRef} workspaceId={workspaceId} chatId={chatId} onNavigateSubTab={onNavigateSubTab} />
              </div>
            </div>
            <div
              onMouseDown={startWorkingPanelResizeVertical}
              title="Drag to resize"
              className="h-1.5 w-full shrink-0 cursor-row-resize hover:bg-[var(--neutral-700)] active:bg-[var(--neutral-600)] transition-colors"
            />
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
                <WorkingPanel isSyncingRef={isSyncingRef} workspaceId={workspaceId} chatId={chatId} onNavigateSubTab={onNavigateSubTab} />
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
