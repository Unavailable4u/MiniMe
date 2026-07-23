"use client";
import { useState, useEffect, useCallback } from "react";
import {
  Layers, BookMarked, CalendarDays, SearchCheck, BarChart3,
  Sparkles, Loader2, Copy, Check, AlertTriangle, ExternalLink, Plus,
} from "lucide-react";
import { useSession, authHeaders } from "../../context/SessionContext";
import { useWorkspaceDock } from "../../context/WorkspaceDockContext"; // NEW — step 3e follow-up: GrowthTab's chat dock
import { FactsView } from "./NotebooksTab";
import WorkspaceChatPanel from "../../components/WorkspaceChatPanel";
import WorkspaceDataBubble from "../../components/WorkspaceDataBubble";
import CreateWorkspaceModal from "../CreateWorkspaceModal"; // NEW — item #10 / B3: native "create project" for this tab, same as ResearchTab's B2
import Markdown from "../Markdown";

// RESOLVED (was TODO(confirm)): design doc §2.2 "voice" — this sub-tab
// directly reuses NotebooksTab.jsx's FactsView component instead of
// re-implementing fact-editing UI a second time, per the "near-zero new
// code" build-order line item. eo/workspace_facts.py is workspace-scoped,
// not domain-scoped, so a Growth-stage workspace calling the same
// fetchWorkspaceFacts/saveWorkspaceFacts/fetchFactCandidates/
// acceptFactCandidate/rejectFactCandidate functions NotebooksTab already
// uses gets brand voice (plus agent-suggested fact candidates) for free,
// zero new backend work. VoiceView below is now just FactsView + the one
// genuinely Growth-specific addition the design doc calls out: the
// "check this draft against brand voice" quick-action.

const SELECTED_GROWTH_WS_KEY = "minime_growth_selected_ws";
const CHAT_DOCK_KEY = "minime_growth_chatdock_collapsed";

const SUB_TABS = [
  { id: "content", label: "Content Fan-out", icon: Layers },
  { id: "voice", label: "Brand Voice", icon: BookMarked },
  { id: "calendar", label: "Calendar", icon: CalendarDays },
  { id: "audit", label: "Content Audit", icon: SearchCheck },
  { id: "analytics", label: "Analytics", icon: BarChart3 },
];

export default function GrowthTab({ initialWorkspaceId, onConsumeInitialWorkspaceId, onPromoted }) {
  const { workspaces, fetchWorkspaces } = useSession();
  const [selectedWsId, setSelectedWsId] = useState(null);
  const [activeSubTab, setActiveSubTab] = useState("voice"); // voice is the only fully-built sub-tab today
  const [dockCollapsed, setDockCollapsed] = useState(true); // §2.3: default collapsed, unlike Test
  // NEW — item #10 / B3: native "create project" trigger, same pattern
  // as ResearchTab's B2. This tab can now create its own growth-stage
  // workspace directly, instead of requiring a promotion from Test or
  // the chat sidebar's folder button — those remain valid paths in,
  // this is just no longer the only one.
  const [showCreateModal, setShowCreateModal] = useState(false);

  const growthWorkspaces = (workspaces || []).filter((w) => (w.active_stages || [w.stage]).includes("growth"));

  useEffect(() => {
    fetchWorkspaces();
    const saved = localStorage.getItem(SELECTED_GROWTH_WS_KEY);
    if (saved) setSelectedWsId(saved);
    setDockCollapsed(localStorage.getItem(CHAT_DOCK_KEY) !== "0");
  }, []);

  // §8 hand-off: a promote into "growth" from TestTab lands here via
  // AppShell's pendingWorkspaceSelection -> initialWorkspaceId prop.
  useEffect(() => {
    if (initialWorkspaceId) {
      selectWorkspace(initialWorkspaceId);
      onConsumeInitialWorkspaceId?.();
    }
  }, [initialWorkspaceId]);

  function selectWorkspace(wsId) {
    setSelectedWsId(wsId);
    localStorage.setItem(SELECTED_GROWTH_WS_KEY, wsId);
  }

  function toggleDock() {
    setDockCollapsed((prev) => {
      // Read side treats "0" as expanded, anything else as collapsed
      // (see the mount effect above), so the stored value must match
      // the *new* state, not `prev`: going collapsed(true)->expanded
      // means the new state is false, so store "0"; that only happens
      // when `prev` was true, i.e. store `prev ? "0" : "1"`.
      localStorage.setItem(CHAT_DOCK_KEY, prev ? "0" : "1");
      return !prev;
    });
  }

  return (
    <div className="flex h-full min-h-0">
      {/* Left-hand project picker -- same contract as every other tab
          (§0 of the design doc), just filtered to stage === "growth". */}
      <aside className="w-56 border-r border-[var(--neutral-800)] flex flex-col shrink-0">
        <div className="px-3 py-2 flex items-center justify-between text-xs font-medium text-[var(--neutral-500)] uppercase tracking-wide">
          <span>Growth Workspaces</span>
          {/* NEW — item #10 / B3: native create, same stage-aware modal
              ResearchTab's B2 wired up first. */}
          <button
            onClick={() => setShowCreateModal(true)}
            title="New growth workspace"
            className="normal-case text-[var(--neutral-500)] hover:text-[var(--neutral-200)]"
          >
            <Plus size={14} />
          </button>
        </div>
        <div className="flex-1 overflow-y-auto">
          {growthWorkspaces.length === 0 && (
            <div className="px-3 py-4 text-xs text-[var(--neutral-500)]">
              No workspaces at the Growth stage yet. Create one above, or
              promote one from Test to see it here.
            </div>
          )}
          {growthWorkspaces.map((w) => (
            <button
              key={w.id}
              onClick={() => selectWorkspace(w.id)}
              className={`w-full text-left px-3 py-2 text-sm truncate transition-colors ${
                selectedWsId === w.id
                  ? "bg-[var(--accent)] text-[var(--accent-text)]"
                  : "text-[var(--neutral-300)] hover:bg-[var(--neutral-800)]"
              }`}
            >
              {w.name}
            </button>
          ))}
        </div>
      </aside>

      {/* Right-hand content pane */}
      <div className="flex-1 min-w-0 flex flex-col">
        <nav className="flex gap-1 px-3 py-2 border-b border-[var(--neutral-800)]">
          {SUB_TABS.map((t) => {
            const Icon = t.icon;
            const built = t.id === "voice" || t.id === "content" || t.id === "calendar" || t.id === "audit"; // implemented so far
            return (
              <button
                key={t.id}
                onClick={() => setActiveSubTab(t.id)}
                className={`flex items-center gap-1.5 text-xs rounded-lg px-3 py-1.5 transition-colors ${
                  activeSubTab === t.id
                    ? "bg-[var(--accent)] text-[var(--accent-text)] font-medium"
                    : "text-[var(--neutral-500)] hover:text-[var(--neutral-300)]"
                } ${!built ? "opacity-60" : ""}`}
              >
                <Icon size={13} />
                {t.label}
                {!built && <span className="ml-1 text-[10px]">(soon)</span>}
              </button>
            );
          })}
        </nav>

        <div className="flex-1 min-h-0 overflow-y-auto p-4 relative">
          <WorkspaceDataBubble
            workspaceId={selectedWsId}
            workspaceName={(growthWorkspaces.find((w) => w.id === selectedWsId) || {}).name}
            storageKey="minime_growth_data_bubble_collapsed"
          />
          {!selectedWsId && (
            <div className="text-sm text-[var(--neutral-500)]">
              Select a Growth workspace on the left to get started.
            </div>
          )}
          {selectedWsId && activeSubTab === "voice" && (
            <VoiceView wsId={selectedWsId} />
          )}
          {selectedWsId && activeSubTab === "content" && (
            <ContentView wsId={selectedWsId} onDispatched={() => setDockCollapsed(false)} />
          )}
          {selectedWsId && activeSubTab === "calendar" && <CalendarView />}
          {selectedWsId && activeSubTab === "audit" && <ContentAuditView wsId={selectedWsId} />}
          {selectedWsId && activeSubTab !== "voice" && activeSubTab !== "content" && activeSubTab !== "calendar" && activeSubTab !== "audit" && (
            <ComingSoonPanel subTabId={activeSubTab} />
          )}
        </div>

      </div>

      {/* Workspace chat dock -- real WorkspaceChatPanel.jsx, same
          component NotebooksTab/ResearchTab embed (design doc §2.3:
          "Yes, same pattern, default collapsed"). It manages its own
          collapsed rendering internally based on the `collapsed` prop --
          a narrow icon rail when true, chat box + WorkingPanel
          side-by-side when false -- and renders its own toggle button
          in both states (a MessageSquare rail button collapsed,
          PanelRightClose expanded), so this wrapper only needs to size
          the container; no chevron/toggle of our own like the old
          placeholder had. */}
      {selectedWsId && (
        <div
          className={`shrink-0 border-l border-[var(--neutral-800)] ${
            dockCollapsed ? "w-10" : "w-[480px]"
          }`}
        >
          <WorkspaceChatPanel collapsed={dockCollapsed} onToggleCollapse={toggleDock} workspaceId={selectedWsId} />
        </div>
      )}

      {/* NEW — item #10 / B3: stage-aware create modal (B1). Auto-selects
          the created project so the user lands straight in it instead of
          having to find it in the list themselves — same as ResearchTab's
          B2. Uses selectWorkspace() (not a bare setSelectedWsId) so the
          new workspace also persists as this tab's restored selection on
          reload, same as picking it from the sidebar would. */}
      {showCreateModal && (
        <CreateWorkspaceModal
          stage="growth"
          onClose={(created) => {
            setShowCreateModal(false);
            if (created) selectWorkspace(created.id);
          }}
        />
      )}
    </div>
  );
}

// --- voice: Brand Voice ------------------------------------------------
// §2.2 "voice": reuses eo/workspace_facts.py's fetch/save pair directly.
// Facts are workspace-scoped, not domain-scoped, so a growth-stage
// workspace gets brand voice for free -- zero new backend work, exactly
// as the design doc claims.

function VoiceView({ wsId }) {
  const {
    fetchWorkspaceFacts, saveWorkspaceFacts, fetchFactCandidates,
    acceptFactCandidate, rejectFactCandidate,
  } = useSession();
  // NEW — step 3e follow-up: was destructured off useSession() (global
  // sessionId/messages), which would silently stop matching what the
  // dock-mode WorkspaceChatPanel above shows once it's keyed to wsId.
  // Reading/writing the same ws:${wsId} dock slot instead.
  const dock = useWorkspaceDock(wsId);
  const { messages, loading, sessionId } = dock.state;
  const [draft, setDraft] = useState("");
  const [checking, setChecking] = useState(false);
  // Same settled-result tracking ContentView uses below: only trust
  // `messages` for this dispatch once the session has actually landed
  // back on the chat we just created and finished loading.
  const [checkedChatId, setCheckedChatId] = useState(null);
  const [copied, setCopied] = useState(false);

  // §2.2's "check this draft against brand voice" quick-action. Opens a
  // scoped sub-chat and sends the draft with an explicit instruction,
  // landing on brand_voice_checker via the Panel's normal domain routing
  // (growth's reference structure in eo/structure.py lists
  // brand_voice_checker as a hireable role).
  //
  // UPDATE (inline result, was the flagged dock-only gap): same fix as
  // ContentView -- openScopedSubChat() blocks on sendTask() until the
  // run finishes, so the assistant turn is already the last message in
  // `messages` by the time it returns. No separate reader needed here
  // either. The chat dock (now the real WorkspaceChatPanel) is still
  // there for watching the live trace, just no longer required to see
  // the result.
  async function handleCheckDraft() {
    if (!draft.trim()) return;
    setChecking(true);
    setCheckedChatId(null);
    try {
      const chatId = await dock.openScopedSubChat(
        `Check this draft against our stored brand voice and flag any drift:\n\n${draft}`
      );
      setCheckedChatId(chatId);
    } finally {
      setChecking(false);
    }
  }

  const resultMessage =
    checkedChatId && sessionId === checkedChatId && !loading
      ? messages[messages.length - 1]
      : null;
  const result =
    resultMessage?.role === "assistant" ? extractCheckResult(resultMessage.data) : null;
  const stillChecking = checkedChatId && sessionId === checkedChatId && loading;

  async function copyResult(text) {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard permissions can be denied by the browser -- not fatal,
      // the text is still visible to select/copy manually.
    }
  }

  return (
    <div className="max-w-2xl space-y-6">
      <FactsView
        workspaceId={wsId}
        fetchWorkspaceFacts={fetchWorkspaceFacts}
        saveWorkspaceFacts={saveWorkspaceFacts}
        fetchFactCandidates={fetchFactCandidates}
        acceptFactCandidate={acceptFactCandidate}
        rejectFactCandidate={rejectFactCandidate}
      />

      <div className="pt-4 border-t border-[var(--neutral-800)]">
        <label className="block text-xs font-medium text-[var(--neutral-500)] mb-1">
          Check a draft against brand voice
        </label>
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          rows={4}
          className="w-full bg-[var(--neutral-900)] border border-[var(--neutral-800)] rounded-lg px-3 py-2 text-sm text-[var(--neutral-200)]"
          placeholder="Paste a piece of content to check…"
        />
        <button
          onClick={handleCheckDraft}
          disabled={checking || !draft.trim()}
          className="mt-2 text-xs font-medium border border-[var(--neutral-700)] text-[var(--neutral-300)] rounded-lg px-3 py-1.5 disabled:opacity-50"
        >
          {checking ? "Sending…" : "Check against brand voice"}
        </button>

        {stillChecking && (
          <div className="mt-2 flex items-center gap-1.5 text-xs text-[var(--neutral-500)]">
            <Loader2 size={12} className="animate-spin" />
            Checking against brand voice…
          </div>
        )}

        {result?.kind === "error" && (
          <div className="mt-2 flex items-start gap-2 text-xs text-red-400 border border-red-900/50 bg-red-950/20 rounded-lg px-3 py-2">
            <AlertTriangle size={13} className="mt-0.5 shrink-0" />
            <span>The check failed: {result.message}</span>
          </div>
        )}

        {result?.kind === "text" && (
          <ResultCard
            platform="Brand voice check"
            text={result.text}
            copied={copied}
            onCopy={() => copyResult(result.text)}
          />
        )}
      </div>
    </div>
  );
}

// Best-effort extraction for a single-result task (as opposed to
// extractPlatformResults() below, which splits N platform variants out
// of one payload). Same "don't invent a field name" caution as that
// function -- see the comment above ContentView.
function extractCheckResult(data) {
  if (!data) return null;
  if (data.status === "error") {
    return { kind: "error", message: data.message || "Unknown error." };
  }
  const result = data.result ?? data;
  return {
    kind: "text",
    text: typeof result === "string" ? result : JSON.stringify(result, null, 2),
  };
}

// --- content: Content Fan-out ------------------------------------------
// §2.1 "content" — agents/content_adapter_pool.py already does the real
// work server-side (ThreadPoolExecutor, one worker per platform, same
// _select_workers(role_tag=...) fairness pool code_writers.py uses).
//
// UPDATE (inline results, was the flagged gap): now that SessionContext.jsx
// is available, the fix turned out not to be a new fetchPlatformContent()
// reader — there isn't one, and none is needed. `messages`/`sessionId`/
// `loading` are already global session state (§0: "a workspace only ever
// has one chat in focus at a time"), and openScopedSubChat() (a) creates a
// brand-new, empty chat, then (b) calls sendTask(), which pushes the user
// turn and — since post_task() blocks until the run finishes or hits an
// approval checkpoint — the assistant turn too, before it resolves. So by
// the time openScopedSubChat() returns, the fan-out result is already the
// last message in that fresh chat. ContentView below just watches for it
// and renders it as cards instead of only leaving it in the dock.
//
// Also dropped the `openInDock` call from the previous version — it was
// destructured off useSession() but SessionContext.jsx doesn't export a
// function by that name, so it would have thrown as soon as a dispatch
// finished. The dock is still available (toggled via `onDispatched`,
// same as before) for watching the live WorkingPanel trace; it's just no
// longer required to see the result.
//
// One real unknown, flagged rather than guessed at: content_adapter_pool.py's
// and api/server.py's exact response shape for a finished task isn't in
// this bundle, so `extractPlatformResults` below tries a few plausible
// shapes (object keyed by platform id, {platforms:[...]}, or a single
// blob with per-platform headings) and falls back to one "full output"
// card rather than silently dropping data it can't parse. Once you can
// show me content_adapter_pool.py's return value (or just the real
// console.log'd payload from a live run), this can drop the guessing and
// just do the right one directly.

const CONTENT_PLATFORMS = [
  { id: "twitter", label: "X / Twitter" },
  { id: "linkedin", label: "LinkedIn" },
  { id: "instagram_caption", label: "Instagram caption" },
  { id: "press_release", label: "Press release" },
  { id: "facebook", label: "Facebook" },
  { id: "blog_intro", label: "Blog intro" },
];

function ContentView({ wsId, onDispatched }) {
  // NEW — step 3e follow-up: same reasoning as VoiceView above — reads/
  // writes the ws:${wsId} dock slot instead of global SessionContext state.
  const dock = useWorkspaceDock(wsId);
  const { messages, loading, sessionId } = dock.state;
  const [coreMessage, setCoreMessage] = useState("");
  const [selectedPlatforms, setSelectedPlatforms] = useState(["twitter", "linkedin"]);
  const [customPlatform, setCustomPlatform] = useState("");
  const [dispatching, setDispatching] = useState(false);
  // The chat id + platform list a dispatch was sent to, so a later
  // dispatch (or switching away and back) doesn't accidentally render a
  // stale result against the wrong run.
  const [dispatchedChatId, setDispatchedChatId] = useState(null);
  const [dispatchedPlatforms, setDispatchedPlatforms] = useState([]);
  const [copiedId, setCopiedId] = useState(null);

  function togglePlatform(id) {
    setSelectedPlatforms((prev) =>
      prev.includes(id) ? prev.filter((p) => p !== id) : [...prev, id]
    );
  }

  function addCustomPlatform() {
    const p = customPlatform.trim().toLowerCase().replace(/\s+/g, "_");
    if (p && !selectedPlatforms.includes(p)) {
      setSelectedPlatforms((prev) => [...prev, p]);
    }
    setCustomPlatform("");
  }

  async function handleDispatch() {
    if (!coreMessage.trim() || selectedPlatforms.length === 0) return;
    setDispatching(true);
    setDispatchedChatId(null);
    try {
      const task =
        `Adapt this core message for the following platforms: ` +
        `${selectedPlatforms.join(", ")}.\n\nCore message:\n${coreMessage.trim()}`;
      const chatId = await dock.openScopedSubChat(task);
      // openScopedSubChat() awaits sendTask() internally, so `messages`
      // (this dock's state) already has this chat's [user, assistant]
      // pair by the time we get here — no extra fetch needed.
      setDispatchedChatId(chatId);
      setDispatchedPlatforms(selectedPlatforms);
      onDispatched?.(); // still expands the dock, for the live trace view
    } finally {
      setDispatching(false);
    }
  }

  // Only trust `messages` as this dispatch's result once the session has
  // actually settled back onto the chat we just created and it's done
  // loading — otherwise we'd render whatever chat happens to be active.
  const resultMessage =
    dispatchedChatId && sessionId === dispatchedChatId && !loading
      ? messages[messages.length - 1]
      : null;
  const result =
    resultMessage?.role === "assistant"
      ? extractPlatformResults(resultMessage.data, dispatchedPlatforms)
      : null;
  const stillRunning = dispatchedChatId && sessionId === dispatchedChatId && loading;

  async function copyText(id, text) {
    try {
      await navigator.clipboard.writeText(text);
      setCopiedId(id);
      setTimeout(() => setCopiedId((cur) => (cur === id ? null : cur)), 1500);
    } catch {
      // Clipboard permissions can be denied by the browser — not fatal,
      // the text is still visible to select/copy manually.
    }
  }

  return (
    <div className="max-w-2xl space-y-4">
      <p className="text-xs text-[var(--neutral-500)]">
        Write the core message once, pick the platforms to adapt it for, and
        this dispatches to the platform-fan-out pool -- N platforms run as
        genuinely parallel workers. Results land right here as copyable
        cards once the run finishes; open the chat dock below if you want
        to watch it happen live first.
      </p>

      <div>
        <label className="block text-xs font-medium text-[var(--neutral-500)] mb-1">
          Core message
        </label>
        <textarea
          value={coreMessage}
          onChange={(e) => setCoreMessage(e.target.value)}
          rows={4}
          className="w-full bg-[var(--neutral-900)] border border-[var(--neutral-800)] rounded-lg px-3 py-2 text-sm text-[var(--neutral-200)]"
          placeholder="e.g. We're launching a redesigned onboarding flow next Tuesday..."
        />
      </div>

      <div>
        <label className="block text-xs font-medium text-[var(--neutral-500)] mb-2">
          Platforms
        </label>
        <div className="flex flex-wrap gap-2">
          {CONTENT_PLATFORMS.map((p) => (
            <button
              key={p.id}
              onClick={() => togglePlatform(p.id)}
              className={`text-xs rounded-full px-3 py-1 border transition-colors ${
                selectedPlatforms.includes(p.id)
                  ? "bg-[var(--accent)] text-[var(--accent-text)] border-[var(--accent)]"
                  : "border-[var(--neutral-700)] text-[var(--neutral-400)] hover:text-[var(--neutral-200)]"
              }`}
            >
              {p.label}
            </button>
          ))}
          {selectedPlatforms
            .filter((id) => !CONTENT_PLATFORMS.some((p) => p.id === id))
            .map((id) => (
              <button
                key={id}
                onClick={() => togglePlatform(id)}
                className="text-xs rounded-full px-3 py-1 border bg-[var(--accent)] text-[var(--accent-text)] border-[var(--accent)]"
              >
                {id}
              </button>
            ))}
        </div>
        <div className="flex items-center gap-2 mt-2">
          <input
            value={customPlatform}
            onChange={(e) => setCustomPlatform(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && addCustomPlatform()}
            placeholder="custom platform (e.g. reddit_post)"
            className="flex-1 bg-[var(--neutral-900)] border border-[var(--neutral-800)] rounded-lg px-2 py-1 text-xs text-[var(--neutral-200)]"
          />
          <button
            onClick={addCustomPlatform}
            disabled={!customPlatform.trim()}
            className="text-xs border border-[var(--neutral-700)] text-[var(--neutral-400)] rounded-lg px-2 py-1 disabled:opacity-50"
          >
            Add
          </button>
        </div>
        <p className="text-[10px] text-[var(--neutral-600)] mt-1">
          Unrecognized platforms still work -- the pool falls back to a
          generic format if it doesn't have specific rules on file for one.
        </p>
      </div>

      <button
        onClick={handleDispatch}
        disabled={dispatching || !coreMessage.trim() || selectedPlatforms.length === 0}
        className="text-xs bg-[var(--cyber-violet)] text-black rounded-lg px-3 py-2 font-medium disabled:opacity-50 flex items-center gap-1.5"
      >
        {dispatching ? <Loader2 size={13} className="animate-spin" /> : <Sparkles size={13} />}
        {dispatching ? "Dispatching…" : `Adapt for ${selectedPlatforms.length} platform${selectedPlatforms.length === 1 ? "" : "s"}`}
      </button>

      {stillRunning && (
        <div className="flex items-center gap-1.5 text-xs text-[var(--neutral-500)]">
          <Loader2 size={12} className="animate-spin" />
          Running the fan-out across {dispatchedPlatforms.length} platform
          {dispatchedPlatforms.length === 1 ? "" : "s"}… open the chat dock
          below to watch it live.
        </div>
      )}

      {result?.kind === "error" && (
        <div className="flex items-start gap-2 text-xs text-red-400 border border-red-900/50 bg-red-950/20 rounded-lg px-3 py-2">
          <AlertTriangle size={13} className="mt-0.5 shrink-0" />
          <span>The run failed: {result.message}</span>
        </div>
      )}

      {result?.kind === "cards" && (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          {result.cards.map((card, i) => (
            <ResultCard
              key={`${card.platform}-${i}`}
              platform={card.platform}
              text={card.text}
              copied={copiedId === `${card.platform}-${i}`}
              onCopy={() => copyText(`${card.platform}-${i}`, card.text)}
            />
          ))}
        </div>
      )}

      {result?.kind === "raw" && (
        <ResultCard
          platform="Full output"
          text={result.text}
          copied={copiedId === "raw"}
          onCopy={() => copyText("raw", result.text)}
          note="Couldn't confidently split this into per-platform cards — showing the whole response. See the code comment above ContentView for why."
        />
      )}
    </div>
  );
}

// One result card per platform variant. Kept dumb/presentational on
// purpose so extractPlatformResults() (below) is the only place that
// needs to change once the real content_adapter_pool.py response shape
// is confirmed.
function ResultCard({ platform, text, copied, onCopy, note }) {
  return (
    <div className="border border-[var(--neutral-800)] rounded-lg p-3 bg-[var(--neutral-900)]">
      <div className="flex items-center justify-between mb-1.5">
        <span className="text-xs font-medium text-[var(--neutral-300)]">
          {platform}
        </span>
        <button
          onClick={onCopy}
          className="flex items-center gap-1 text-[10px] text-[var(--neutral-500)] hover:text-[var(--neutral-300)]"
        >
          {copied ? <Check size={12} /> : <Copy size={12} />}
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <p className="text-xs text-[var(--neutral-300)] whitespace-pre-wrap">
        {text || "(empty)"}
      </p>
      {note && (
        <p className="text-[10px] text-[var(--neutral-600)] mt-2">{note}</p>
      )}
    </div>
  );
}

// Best-effort extraction of per-platform text from a finished task's
// response payload — see the comment above ContentView for why this
// guesses at a few shapes instead of reading one known field.
function extractPlatformResults(data, platformIds) {
  if (!data) return null;
  if (data.status === "error") {
    return { kind: "error", message: data.message || "Unknown error." };
  }
  const result = data.result ?? data;

  // Shape A: { platforms: [{ platform, content }] } / [{id, text}] etc.
  if (Array.isArray(result?.platforms)) {
    return {
      kind: "cards",
      cards: result.platforms.map((p) => ({
        platform: platformLabel(p.platform || p.id || p.name || "platform"),
        text: p.content ?? p.text ?? "",
      })),
    };
  }

  // Shape B: an object keyed directly by the platform ids we dispatched
  // with (mirrors code_writers.py's {file_path: code} worker-pool shape).
  if (result && typeof result === "object" && !Array.isArray(result)) {
    const keys = Object.keys(result).filter((k) => platformIds.includes(k));
    if (keys.length > 0) {
      return {
        kind: "cards",
        cards: keys.map((k) => ({
          platform: platformLabel(k),
          text: typeof result[k] === "string" ? result[k] : JSON.stringify(result[k], null, 2),
        })),
      };
    }
  }

  // Shape C: a single text blob with a heading per platform (e.g.
  // "## LinkedIn\n...\n## X / Twitter\n...").
  if (typeof result === "string") {
    const cards = splitByPlatformHeadings(result, platformIds);
    if (cards.length > 1) return { kind: "cards", cards };
    return { kind: "raw", text: result };
  }

  // Nothing recognized -- surface it rather than lose it.
  return { kind: "raw", text: JSON.stringify(result, null, 2) };
}

function platformLabel(id) {
  const known = CONTENT_PLATFORMS.find((p) => p.id === id);
  if (known) return known.label;
  return id.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function escapeRegExp(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function splitByPlatformHeadings(text, platformIds) {
  const positions = [];
  for (const id of platformIds) {
    const label = platformLabel(id);
    // Matches a line that's just the label, optionally wrapped in
    // markdown heading hashes, bold asterisks, or a trailing colon.
    const re = new RegExp(
      `^[ \\t]*#{0,3}[ \\t]*\\*{0,2}${escapeRegExp(label)}\\*{0,2}[ \\t]*:?[ \\t]*$`,
      "im"
    );
    const m = re.exec(text);
    if (m) positions.push({ label, index: m.index, end: m.index + m[0].length });
  }
  if (positions.length < 2) return [];
  positions.sort((a, b) => a.index - b.index);
  return positions.map((p, i) => ({
    platform: p.label,
    text: text.slice(p.end, i + 1 < positions.length ? positions[i + 1].index : text.length).trim(),
  }));
}

// --- calendar: Calendar -------------------------------------------------
// §2.2 "calendar" — RESOLVED differently than the design doc's original
// guess: not a paste-parsed date-grouped list off Plan's handoff summary
// sentence. agents/calendar_agent.py + api/server.py's
// /api/integrations/google_calendar/* routes (Part 8.5) are already a
// complete, live Google Calendar connector. This view reads the same
// GET .../events endpoint CalendarEventsPanel.jsx already calls, and
// groups the result by date -- that grouping is the one genuinely new
// piece per the design doc's own §2.4 table ("Calendar formatting role +
// date-grouped list UI"), not a new fetch mechanism.
//
// Deliberately NOT reusing CalendarEventsPanel.jsx directly: that
// component is a flat, collapsed-by-default Settings accordion for
// personal event create/delete; this is a full-pane, always-open,
// date-grouped view sized for a Growth sub-tab. Both call the same three
// endpoints, same authHeaders() pattern -- toRFC3339/the fetch shape are
// intentionally mirrored from there rather than imported, since that
// file doesn't export them and duplicating three small pure functions
// beats reaching into another component's internals.
//
// NOTE: Google Calendar is a per-USER connection (eo/integrations.py,
// user_integrations keyed by user_id+provider), not per-workspace -- so
// unlike VoiceView/ContentView above, this view's data doesn't change
// with the selected Growth workspace. No wsId prop needed for fetching;
// the workspace-picker gate above it stays only for UI consistency with
// the other sub-tabs.

function toRFC3339(localDatetimeValue) {
  if (!localDatetimeValue) return null;
  return new Date(localDatetimeValue).toISOString();
}

function formatEventTime(value) {
  if (!value) return "";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value; // all-day events: plain "YYYY-MM-DD"
  return d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

function formatDateHeading(dateKey) {
  const d = new Date(`${dateKey}T00:00:00`);
  if (Number.isNaN(d.getTime())) return dateKey;
  return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}

// Groups a flat events[] (same shape calendar_agent.list_events returns)
// into date-ordered buckets. All-day events' start is already
// "YYYY-MM-DD" (calendar_agent.py falls back to the date field when
// there's no dateTime); timed events' start is an ISO datetime, sliced
// to its date portion.
function groupByDate(events) {
  const groups = {};
  for (const ev of events) {
    const dateKey = (ev.start || "").slice(0, 10) || "unknown";
    (groups[dateKey] ||= []).push(ev);
  }
  const sortedKeys = Object.keys(groups).sort();
  for (const key of sortedKeys) {
    groups[key].sort((a, b) => (a.start || "").localeCompare(b.start || ""));
  }
  return sortedKeys.map((key) => ({ date: key, events: groups[key] }));
}

// Default window: now through 30 days out -- a content-calendar-shaped
// range rather than CalendarEventsPanel's 7-day "what's coming up"
// default, since Growth's use case is planning ahead, not a quick glance.
function defaultRange() {
  const now = new Date();
  const monthOut = new Date(now.getTime() + 30 * 24 * 60 * 60 * 1000);
  const toLocalInputValue = (d) => {
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  };
  return { timeMin: toLocalInputValue(now), timeMax: toLocalInputValue(monthOut) };
}

function CalendarView() {
  const { API_URL } = useSession();
  const [range, setRange] = useState(defaultRange);
  const [events, setEvents] = useState(null); // null = not loaded yet
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [notConnected, setNotConnected] = useState(false);
  const [connecting, setConnecting] = useState(false);

  const loadEvents = useCallback(async () => {
    const time_min = toRFC3339(range.timeMin);
    const time_max = toRFC3339(range.timeMax);
    if (!time_min || !time_max) return;
    setLoading(true);
    setError(null);
    setNotConnected(false);
    try {
      const params = new URLSearchParams({ time_min, time_max });
      const res = await fetch(`${API_URL}/api/integrations/google_calendar/events?${params}`, {
        headers: await authHeaders(),
      });
      if (res.status === 409) {
        setNotConnected(true);
        setEvents([]);
        return;
      }
      if (!res.ok) throw new Error(`Failed to load events (${res.status})`);
      const data = await res.json();
      setEvents(data.events || []);
    } catch (e) {
      setError(String(e.message || e));
      setEvents([]);
    } finally {
      setLoading(false);
    }
  }, [API_URL, range]);

  useEffect(() => { loadEvents(); }, []); // load once on mount with the default 30-day window

  // Same connect flow as IntegrationsPanel.handleConnect -- duplicated
  // rather than imported since IntegrationsPanel isn't exported as a
  // reusable hook, just a full settings-page component; this is the
  // handful of lines of it actually needed here.
  async function handleConnect() {
    setConnecting(true);
    setError(null);
    try {
      const res = await fetch(`${API_URL}/api/integrations/google_calendar/connect`, {
        headers: await authHeaders(),
      });
      if (!res.ok) throw new Error(`Failed to start connection (${res.status})`);
      const { auth_url } = await res.json();
      window.location.href = auth_url;
    } catch (e) {
      setError(String(e.message || e));
      setConnecting(false);
    }
  }

  if (notConnected) {
    return (
      <div className="max-w-md text-sm text-[var(--neutral-400)] space-y-3">
        <p>Google Calendar isn't connected yet.</p>
        {error && <p className="text-xs text-red-400">{error}</p>}
        <button
          onClick={handleConnect}
          disabled={connecting}
          className="flex items-center gap-1.5 text-xs bg-[var(--accent)] text-[var(--accent-text)] rounded-lg px-3 py-1.5 font-medium disabled:opacity-50"
        >
          {connecting ? <Loader2 size={13} className="animate-spin" /> : <CalendarDays size={13} />}
          {connecting ? "Redirecting…" : "Connect Google Calendar"}
        </button>
        <p className="text-[11px] text-[var(--neutral-600)]">
          You can also connect from Settings → Integrations, where you can manage
          or disconnect it later.
        </p>
      </div>
    );
  }

  const grouped = events ? groupByDate(events) : [];

  return (
    <div className="max-w-2xl space-y-4">
      <p className="text-xs text-[var(--neutral-500)]">
        Reads live from your connected Google Calendar -- same connection
        Settings → Integrations manages, grouped here by date for a
        content-calendar view.
      </p>

      <div className="flex items-center gap-2">
        <input
          type="datetime-local"
          value={range.timeMin}
          onChange={(e) => setRange((r) => ({ ...r, timeMin: e.target.value }))}
          className="flex-1 text-xs bg-[var(--neutral-900)] border border-[var(--neutral-800)] rounded-lg px-2 py-1.5 text-[var(--neutral-300)]"
        />
        <span className="text-[10px] text-[var(--neutral-600)]">to</span>
        <input
          type="datetime-local"
          value={range.timeMax}
          onChange={(e) => setRange((r) => ({ ...r, timeMax: e.target.value }))}
          className="flex-1 text-xs bg-[var(--neutral-900)] border border-[var(--neutral-800)] rounded-lg px-2 py-1.5 text-[var(--neutral-300)]"
        />
        <button
          onClick={loadEvents}
          disabled={loading}
          className="shrink-0 text-xs rounded-lg px-3 py-1.5 border border-[var(--neutral-700)] text-[var(--neutral-400)] hover:text-[var(--neutral-200)] disabled:opacity-50"
        >
          {loading ? <Loader2 size={12} className="animate-spin" /> : "Refresh"}
        </button>
      </div>

      {error && (
        <div className="flex items-start gap-2 text-xs text-red-400 border border-red-900/50 bg-red-950/20 rounded-lg px-3 py-2">
          <AlertTriangle size={13} className="mt-0.5 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {loading && events === null && (
        <div className="flex items-center gap-1.5 text-xs text-[var(--neutral-500)]">
          <Loader2 size={12} className="animate-spin" /> Loading events…
        </div>
      )}

      {events !== null && !loading && grouped.length === 0 && !error && (
        <p className="text-xs text-[var(--neutral-600)]">No events in this range.</p>
      )}

      <div className="space-y-4">
        {grouped.map(({ date, events: dayEvents }) => (
          <div key={date}>
            <h3 className="text-[11px] font-medium uppercase tracking-wide text-[var(--neutral-500)] mb-1.5">
              {formatDateHeading(date)}
            </h3>
            <div className="space-y-1.5">
              {dayEvents.map((ev) => (
                <div
                  key={ev.id}
                  className="flex items-center justify-between gap-2 border border-[var(--neutral-800)] rounded-lg px-3 py-2 bg-[var(--neutral-900)]"
                >
                  <div className="min-w-0">
                    <p className="text-xs text-[var(--neutral-300)] truncate">{ev.summary}</p>
                    <p className="text-[10px] text-[var(--neutral-600)] truncate">
                      {formatEventTime(ev.start)}
                      {ev.end ? ` – ${formatEventTime(ev.end)}` : ""}
                      {ev.location ? ` · ${ev.location}` : ""}
                    </p>
                  </div>
                  {ev.html_link && (
                    <a
                      href={ev.html_link}
                      target="_blank"
                      rel="noreferrer"
                      className="p-1.5 text-[var(--neutral-600)] hover:text-[var(--neutral-300)] shrink-0"
                      title="Open in Google Calendar"
                    >
                      <ExternalLink size={12} />
                    </a>
                  )}
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// --- audit: Content Audit ------------------------------------------------
// §2.2 "audit" -- two halves, deliberately never blended into one number
// (design doc's honesty discipline, same reasoning as ContradictionsPanel's
// amber banner / Feasibility's estimateBanner):
//
//   1. LLM-estimated: seo_structure_auditor's output, pasted in the same
//      MarkdownPastePanel shape PlanTab.jsx uses for PRD/API Contract/
//      Devil's Advocate/Feasibility -- panelKey "audit" (already in
//      eo/panel_content.py's VALID_PANEL_KEYS), persisted via the same
//      fetchPanelContent/savePanelContent pair off useSession() every
//      other paste panel in this codebase uses.
//   2. Real, measured: a live GET to
//      /api/workspaces/{ws_id}/audit/pagespeed (agents/pagespeed_agent.py,
//      already wired end-to-end server-side) -- fetched fresh on demand,
//      not persisted, same "live, not a backing store" pattern
//      CalendarView above already uses for Google Calendar events.
//
// MarkdownPastePanel itself isn't exported from PlanTab.jsx, so this is a
// near-identical duplicate (ContentAuditPastePanel below) rather than a
// new cross-tab import -- per the design doc §2.4's own "near-identical to
// PlanTab.MarkdownPastePanel, just relabeled" line item.

function ContentAuditView({ wsId }) {
  const { API_URL, fetchPanelContent, savePanelContent } = useSession();

  const [url, setUrl] = useState("");
  const [strategy, setStrategy] = useState("mobile");
  const [psLoading, setPsLoading] = useState(false);
  const [psError, setPsError] = useState(null);
  const [psResult, setPsResult] = useState(null);

  async function runPagespeed() {
    if (!url.trim()) return;
    setPsLoading(true);
    setPsError(null);
    try {
      const params = new URLSearchParams({ url: url.trim(), strategy });
      const res = await fetch(`${API_URL}/api/workspaces/${wsId}/audit/pagespeed?${params}`, {
        headers: await authHeaders(),
      });
      if (!res.ok) {
        const detail = await res.json().catch(() => null);
        throw new Error(detail?.detail || `PageSpeed check failed (${res.status})`);
      }
      setPsResult(await res.json());
    } catch (e) {
      setPsError(String(e.message || e));
      setPsResult(null);
    } finally {
      setPsLoading(false);
    }
  }

  return (
    <div className="max-w-2xl space-y-8">
      <section className="space-y-3">
        <h3 className="text-xs font-medium uppercase tracking-wide text-[var(--neutral-500)]">
          Content Quality Audit
        </h3>
        <ContentAuditPastePanel
          workspaceId={wsId}
          fetchPanelContent={fetchPanelContent}
          savePanelContent={savePanelContent}
        />
      </section>

      <section className="space-y-3 border-t border-[var(--neutral-800)] pt-6">
        <h3 className="text-xs font-medium uppercase tracking-wide text-[var(--neutral-500)]">
          PageSpeed Insights
        </h3>
        <p className="text-[11px] text-[var(--neutral-600)]">
          A real Lighthouse run against a live URL -- kept separate from the
          AI-estimated audit above, never blended into one score.
        </p>
        <div className="flex items-center gap-2">
          <input
            type="url"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://your-launch-page.com"
            className="flex-1 text-xs bg-[var(--neutral-900)] border border-[var(--neutral-800)] rounded-lg px-2 py-1.5 text-[var(--neutral-300)]"
          />
          <select
            value={strategy}
            onChange={(e) => setStrategy(e.target.value)}
            className="text-xs bg-[var(--neutral-900)] border border-[var(--neutral-800)] rounded-lg px-2 py-1.5 text-[var(--neutral-300)]"
          >
            <option value="mobile">Mobile</option>
            <option value="desktop">Desktop</option>
          </select>
          <button
            onClick={runPagespeed}
            disabled={psLoading || !url.trim()}
            className="shrink-0 flex items-center gap-1.5 text-xs bg-[var(--cyber-amber)] text-black rounded-lg px-3 py-1.5 font-medium disabled:opacity-50"
          >
            {psLoading ? <Loader2 size={12} className="animate-spin" /> : "Run check"}
          </button>
        </div>

        {psError && (
          <div className="flex items-start gap-2 text-xs text-red-400 border border-red-900/50 bg-red-950/20 rounded-lg px-3 py-2">
            <AlertTriangle size={13} className="mt-0.5 shrink-0" />
            <span>{psError}</span>
          </div>
        )}

        {psResult && (
          <div className="space-y-3">
            <div className="grid grid-cols-4 gap-2">
              {["performance", "accessibility", "best_practices", "seo"].map((key) => (
                <div key={key} className="border border-[var(--neutral-800)] rounded-lg px-3 py-2 text-center">
                  <p className="text-[10px] uppercase tracking-wide text-[var(--neutral-600)]">
                    {key.replace("_", " ")}
                  </p>
                  <p className={`text-xl font-semibold ${scoreColor(psResult.scores?.[key])}`}>
                    {psResult.scores?.[key] ?? "—"}
                  </p>
                </div>
              ))}
            </div>

            {psResult.issues?.length > 0 && (
              <div className="space-y-1.5">
                <p className="text-[11px] text-[var(--neutral-500)]">
                  {psResult.issues.length} issue{psResult.issues.length === 1 ? "" : "s"} flagged below the passing threshold:
                </p>
                {psResult.issues.map((issue) => (
                  <div
                    key={issue.id}
                    className="flex items-center justify-between gap-2 border border-[var(--neutral-800)] rounded-lg px-3 py-2 bg-[var(--neutral-900)]"
                  >
                    <span className="text-xs text-[var(--neutral-300)]">{issue.title}</span>
                    <span className={`text-xs font-medium ${scoreColor(issue.score)}`}>{issue.score}</span>
                  </div>
                ))}
              </div>
            )}

            {psResult.fetched_at && (
              <p className="text-[10px] text-[var(--neutral-600)]">
                Fetched {new Date(psResult.fetched_at).toLocaleString()}
              </p>
            )}
          </div>
        )}
      </section>
    </div>
  );
}

// Same 90/50 red-amber-green banding Lighthouse's own report UI uses, so a
// score here reads consistently with what a person may have already seen
// running Lighthouse themselves.
function scoreColor(score) {
  if (score == null) return "text-[var(--neutral-500)]";
  if (score >= 90) return "text-green-400";
  if (score >= 50) return "text-[var(--cyber-amber)]";
  return "text-red-400";
}

// Near-identical to PlanTab.jsx's MarkdownPastePanel (not exported from
// there, hence the duplication -- see the comment above ContentAuditView).
// panelKey is fixed to "audit" rather than a prop since this component has
// exactly one caller and one purpose, unlike PlanTab's shared version.
function ContentAuditPastePanel({ workspaceId, fetchPanelContent, savePanelContent }) {
  const panelKey = "audit";
  const [raw, setRaw] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setSavedAt(null);
    fetchPanelContent(workspaceId, panelKey).then((saved) => {
      if (cancelled) return;
      setRaw(saved?.content || "");
      setLoading(false);
    });
    return () => { cancelled = true; };
  }, [workspaceId, panelKey, fetchPanelContent]);

  async function handleSave() {
    setSaving(true);
    try {
      await savePanelContent(workspaceId, panelKey, raw);
      setSavedAt(Date.now());
    } finally {
      setSaving(false);
    }
  }

  if (loading) {
    return <div className="text-xs text-[var(--neutral-600)] flex items-center gap-1.5"><Loader2 size={12} className="animate-spin" /> Loading…</div>;
  }

  return (
    <div className="space-y-3">
      <p className="text-[11px] text-[var(--neutral-600)]">
        Paste seo_structure_auditor's output below. Saved per project --
        pasting again overwrites the previous paste, same as Plan's
        PRD/Feasibility tabs.
      </p>
      <textarea
        value={raw}
        onChange={(e) => setRaw(e.target.value)}
        placeholder="Paste the role's markdown output here…"
        rows={8}
        className="w-full bg-black/30 border border-[var(--neutral-800)] rounded px-3 py-2 text-xs outline-none focus:border-[var(--cyber-amber)] font-mono"
      />
      <div className="flex items-center gap-2">
        <button
          onClick={handleSave}
          disabled={saving}
          className="text-xs bg-[var(--cyber-amber)] text-black rounded px-3 py-1.5 font-medium disabled:opacity-50"
        >
          {saving ? "Saving…" : "Save"}
        </button>
        {savedAt && !saving && <span className="text-[11px] text-[var(--neutral-600)]">Saved</span>}
      </div>
      {raw.trim() && (
        <div className="border border-[var(--cyber-amber)]/40 bg-[var(--cyber-amber)]/5 rounded-lg p-3">
          <p className="text-[10px] uppercase tracking-wide text-[var(--cyber-amber)] mb-2">
            AI-estimated -- not verified against real ranking/search data
          </p>
          <Markdown>{raw}</Markdown>
        </div>
      )}
    </div>
  );
}

function ComingSoonPanel({ subTabId }) {
  const label = SUB_TABS.find((t) => t.id === subTabId)?.label || subTabId;
  return (
    <div className="text-sm text-[var(--neutral-500)]">
      {label} isn't built yet — see build order §4 (design spec). Brand
      Voice, Content Fan-out, Calendar, and Content Audit are done; next up
      is Analytics (step 6).
    </div>
  );
}