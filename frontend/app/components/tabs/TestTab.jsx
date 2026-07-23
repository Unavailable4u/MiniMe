import { useState, useEffect } from "react";
import { useSession } from "../../context/SessionContext";
import Markdown from "../Markdown";
import WorkspaceChatPanel from "../WorkspaceChatPanel";
import { useWorkspaceDockActions, useWorkspaceDock, useLastActiveChatId } from "../../context/WorkspaceDockContext"; // NEW — step 3e (+ follow-up fix below); useLastActiveChatId added for item #11 / C2
import CreateWorkspaceModal from "../CreateWorkspaceModal"; // NEW — item #10 / B3: native "create project" for this tab, same as ResearchTab's B2
import ConfirmDialog from "../ConfirmDialog"; // NEW — issue #3: same delete-confirmation affordance as ChatSidebar's own per-chat delete
import WorkspaceStageIcons, { STAGE_THEME } from "../WorkspaceStageIcons"; // NEW — item #2: colored per-stage icon + per-project stage badges
import {
  FlaskConical, Users, ClipboardList, ShieldAlert, History,
  Loader2, RefreshCw, MessageSquare, ArrowUpRight, Sparkles,
  Pin, PinOff, Pencil, Check, X, Clock, AlertTriangle, Eye, Plus, Trash2,
} from "lucide-react";

// Test tab design spec §1 — "Simulate & Test", same shell shape as
// ResearchTab (Part 3 §3.9): a project (= workspace, stage: "test")
// picker on the left, sub-tabs on the right.
//
// Build order (per the design spec, §4): first pass shipped `run` and
// `reports` fully wired — the single highest-leverage slice, per the
// spec, because it needed no new backend module beyond one small read
// endpoint (see api/server.py's get_simulation_results() docstring for
// why that endpoint reads the memory bus instead of wrapping
// agents/review_aggregator.py, a deliberate deviation from the spec's
// original suggestion). This pass fills in the remaining three:
//   - `personas` (§4 step 2) — no new backend endpoint either; it's a
//     thin filtered view over the same Role Library store the Role
//     Library panel already reads/writes (GET/PUT/PATCH /api/roles),
//     scoped client-side to STRUCTURE_TEMPLATES["simulate"]'s own role
//     list (SIMULATE_DOMAIN_ROLES below) instead of a new "simulate
//     personas" backend concept.
//   - `redteam` — same fetchSimulationResults() read `reports` already
//     uses, just filtered to red_team's own entry. red_team's
//     ROLE_PROMPTS_SEED brief (eo/registry.py) produces free-form
//     prose, not a structured severity-tagged list — rendering it as
//     one would mean inventing new output-shape rules for that role
//     the brief doesn't actually specify, so this stays prose with an
//     explicit "not yet severity-tagged" note rather than a fabricated
//     structure. Flagged, not hidden.
//   - `history` — still no dedicated backend "past simulation runs"
//     store (unchanged from the design spec's own flagged
//     simplification), but a real one is buildable client-side:
//     recordDispatch() below now appends every dispatched run to a
//     per-workspace localStorage list, not just the single
//     most-recent pointer `reports` already used. Good enough for
//     "see and reopen past runs in this browser"; a server-side store
//     is the real follow-up if that's ever a hard requirement (e.g.
//     cross-device history).

const SUB_TABS = [
  { id: "run",      label: "Run Simulation",   icon: FlaskConical },
  { id: "personas", label: "Personas",         icon: Users },
  { id: "reports",  label: "Friction Reports", icon: ClipboardList },
  { id: "redteam",  label: "Red Team",         icon: ShieldAlert },
  { id: "history",  label: "History",          icon: History },
];
const PROMOTE_TARGETS = ["growth"];
const PROMOTE_LABELS = { growth: "Growth" };

// §1.2 `run` — a fixed, labeled simulation-type list, each mapped to a
// natural-language task lead that steers the Panel's own cold-start
// domain guess + role hire toward eo/structure.py's STRUCTURE_TEMPLATES
// ["simulate"] role list (see that file for the exact roles) — not a
// forced role list from the frontend, since there's no "hire exactly
// these roles" hook to call into; same live-dispatch-via-natural-
// language-task-text approach ResearchTab's SourcesPanel/DatasetPanel
// already use for academic_search/dataset_analyst.
//
// NOTE: the design spec's simulation-type list includes "A/B framing"
// as a 10th category — left out here since no persona role backs it yet
// (eo/registry.py's ROLE_PROMPTS_SEED has personas for the other 9).
// Flagging rather than inventing a new role brief mid-tab-build.
const SIMULATION_TYPES = [
  { id: "customer_reaction", label: "Customer Persona Reaction", taskLead: "Simulate how real customers would react — both an enthusiastic-but-realistic customer persona and a skeptical, hard-to-convince one — to" },
  { id: "reviewer_critic",   label: "Reviewer / Critic Pass",     taskLead: "Simulate an experienced, opinionated professional critic's published review of" },
  { id: "marketplace",       label: "Marketplace Reception",      taskLead: "Generate a realistic distribution of marketplace-style reviews (App Store / Amazon style, mixed sentiment) for" },
  { id: "focus_group",       label: "Focus Group",                taskLead: "Simulate a focus group — an enthusiastic customer, a skeptical customer, and a professional critic, each reacting independently — to" },
  { id: "usability",         label: "Usability Walkthrough",      taskLead: "Simulate a first-time user's usability walkthrough, narrating hesitation, misclicks, and friction points, for" },
  { id: "pricing",           label: "Pricing Sensitivity",        taskLead: "Simulate how a real prospective buyer would react to the pricing of" },
  { id: "support_tickets",   label: "Support-Ticket Prediction",  taskLead: "Predict the concrete support tickets and confused-user questions that would come in after launching" },
  { id: "red_team",          label: "Red-Team Pass",              taskLead: "Run a red-team pass looking for ways to break, misuse, or exploit" },
  { id: "competitive",       label: "Competitive Response",       taskLead: "Predict how a rational competitor would respond to" },
];

// Client-side mirror of eo/structure.py's STRUCTURE_TEMPLATES["simulate"]
// — same order (simulation_synthesizer last, marketplace_review_batch
// just before it). No endpoint exposes this list directly, and it's
// small/stable enough (adding a new persona role is itself a code
// change on the backend) that mirroring it here beats adding a new
// "list this domain's roles" endpoint for one static array.
const SIMULATE_DOMAIN_ROLES = [
  "persona_customer", "persona_skeptic", "critic_reviewer",
  "usability_walkthrough", "red_team", "pricing_sensitivity",
  "support_ticket_predictor", "competitor_response",
  "marketplace_review_batch", "simulation_synthesizer",
];

// NEW — right-hand chat dock collapse key, own key like every other tab
// (§0's shared-shell table). §1.3: unlike Notebooks/Research (default
// collapsed), Test defaults to EXPANDED — a simulation run here IS a
// chat dispatch, and WorkingPanel's routing-trace graph visualizing the
// parallel persona branches live is the tab's main value, not a side
// panel. useState(false) below already means "not collapsed" on first
// mount before localStorage is checked, so no extra logic is needed —
// just noting the intent here since every other tab's dock defaults the
// opposite way.
const CHAT_DOCK_KEY = "minime_test_chatdock_collapsed";

// NEW — per-workspace "last dispatched run" pointer, localStorage-only.
// There's no dedicated "past simulation runs" store yet (§1.2 `history`
// flags this exact simplification in the design spec: "if there's no
// dedicated store for 'past simulation runs' yet... flag that as a
// known simplification"), so `reports` just remembers the most recent
// session_id it dispatched, per workspace, and re-fetches from that.
function lastRunKey(wsId) {
  return `minime_test_last_run_${wsId}`;
}

// NEW — `history` sub-tab: a real (client-only) list of past dispatched
// runs, not just the single "most recent" pointer lastRunKey tracks.
// Capped at 20 entries per workspace, newest first — plenty for
// "reopen a run from earlier today/this week" without unbounded
// localStorage growth.
const RUN_HISTORY_LIMIT = 20;
function runHistoryKey(wsId) {
  return `minime_test_run_history_${wsId}`;
}
function readRunHistory(wsId) {
  try {
    const raw = localStorage.getItem(runHistoryKey(wsId));
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}
function pushRunHistory(wsId, entry) {
  const next = [entry, ...readRunHistory(wsId)].slice(0, RUN_HISTORY_LIMIT);
  localStorage.setItem(runHistoryKey(wsId), JSON.stringify(next));
  return next;
}

export default function TestTab({ initialWorkspaceId, onConsumeInitialWorkspaceId, onPromoted, onActiveWorkspaceChange }) {
  const {
    workspaces, fetchWorkspaces, promoteWorkspace,
    fetchSimulationResults,
    fetchRoles, updateRolePrompt, setRolePinned,
    chats,
  } = useSession();
  // NEW — step 3e follow-up fix: the embedded WorkspaceChatPanel below was
  // NOT actually dock-driven despite this comment previously claiming so —
  // it had no workspaceId prop, so it read messages/sessionId off
  // useSession() (legacy/global) while `switchChat` here (dock-based)
  // wrote into a ws:${activeWsId} slot nothing read. That meant the
  // History panel's "Open chat" button (openInDock -> switchChat with no
  // accompanying legacy write) silently did nothing visible. Fixed by
  // passing workspaceId={activeWs?.id} to the panel below (now the same
  // key switchChat already resolves to) and switching RunSimulationPanel's
  // dispatch to the dock's own openScopedSubChat, so both the "dispatch a
  // new run" and "reopen a past run" paths write into the same slot the
  // panel reads.
  const { switchChat, renameChat, deleteChat, createWorkspaceChat } = useWorkspaceDockActions();
  // NEW — item #11 / C2: same row-highlight source ChatSidebar's nested
  // chat rows use, same as ResearchTab/PlanTab's C1.
  const activeChatId = useLastActiveChatId();
  const [activeWsId, setActiveWsId] = useState(null);
  const [subTab, setSubTab] = useState("run");
  const [promoting, setPromoting] = useState(false);
  const [promoteError, setPromoteError] = useState(null);
  const [promoteTargetStage, setPromoteTargetStage] = useState("growth");
  // NEW — §2.6 step 4: "complete" (existing behavior, leaves this tab)
  // vs "partial" (stays active here too, per §2.1/§2.2). Same toggle as
  // Notebooks/Research/Plan/Build.
  const [promoteMode, setPromoteMode] = useState("complete");
  const [chatDockCollapsed, setChatDockCollapsed] = useState(false);
  // NEW — lifted here (not into RunSimulationPanel/ReportsPanel
  // directly) so a run dispatched from `run` is immediately visible to
  // `reports` without needing a page reload, same "shared state at the
  // tab level" reasoning ResearchTab uses for activeWsId/visitedSubTabs.
  const [lastSessionId, setLastSessionId] = useState(null);
  // NEW — `history`: the full list of past dispatched runs for the
  // active workspace, and which one `reports`/`redteam` are currently
  // showing. viewedSessionId defaults to lastSessionId (the normal
  // "just ran it, look at the report" flow) but `history` can point it
  // at an older run without disturbing lastSessionId itself.
  const [runHistory, setRunHistory] = useState([]);
  const [viewedSessionId, setViewedSessionId] = useState(null);
  // NEW — item #10 / B3: native "create project" trigger, same pattern
  // as ResearchTab's B2. This tab can now create its own test-stage
  // workspace directly, instead of requiring a promotion from Build or
  // the chat sidebar's folder button — those remain valid paths in,
  // this is just no longer the only one.
  const [showCreateModal, setShowCreateModal] = useState(false);
  // NEW — issue #3: nested-chat create/rename/delete state, same shape as
  // ResearchTab's/NotebooksTab's/PlanTab's/BuildTab's own.
  const [creatingChatForWs, setCreatingChatForWs] = useState(null);
  const [editingChatId, setEditingChatId] = useState(null);
  const [editChatTitle, setEditChatTitle] = useState("");
  const [pendingDeleteChat, setPendingDeleteChat] = useState(null);

  useEffect(() => {
    setChatDockCollapsed(localStorage.getItem(CHAT_DOCK_KEY) === "1");
  }, []);

  function toggleChatDock() {
    setChatDockCollapsed((prev) => {
      localStorage.setItem(CHAT_DOCK_KEY, !prev ? "1" : "0");
      return !prev;
    });
  }

  async function openInDock(chatId) {
    await switchChat(chatId);
    if (chatDockCollapsed) toggleChatDock();
  }

  // NEW — issue #3: "+" beside a project name. Creates a chat nested
  // directly inside that project and opens it, same mechanic the Chat
  // sidebar uses for "new chat in this group".
  async function handleCreateChatInProject(ws) {
    setCreatingChatForWs(ws.id);
    try {
      if (activeWsId !== ws.id) setActiveWsId(ws.id);
      await createWorkspaceChat(ws.id);
      if (chatDockCollapsed) toggleChatDock();
    } finally {
      setCreatingChatForWs(null);
    }
  }

  function startRenameChat(chat) {
    setEditingChatId(chat.id);
    setEditChatTitle(chat.title);
  }

  async function commitRenameChat(chatId) {
    if (editChatTitle.trim()) await renameChat(chatId, editChatTitle.trim());
    setEditingChatId(null);
  }

  function askDeleteChat(chat) {
    setPendingDeleteChat(chat);
  }

  async function confirmDeleteChat() {
    await deleteChat(pendingDeleteChat.id);
    setPendingDeleteChat(null);
  }

  const testProjects = workspaces.filter((w) => (w.active_stages || [w.stage]).includes("test"));

  useEffect(() => {
    if (initialWorkspaceId) {
      setActiveWsId(initialWorkspaceId);
      onConsumeInitialWorkspaceId?.();
    }
  }, [initialWorkspaceId, onConsumeInitialWorkspaceId]);

  useEffect(() => {
    if (!activeWsId && testProjects.length > 0) setActiveWsId(testProjects[0].id);
  }, [testProjects, activeWsId]);

  const activeWs = testProjects.find((w) => w.id === activeWsId) || null;

  // NEW — item #1: the Data bubble now lives in AppShell's top nav, not
  // floating over this tab's own content, so this just reports which
  // project (if any) is active instead of rendering the bubble itself.
  useEffect(() => {
    onActiveWorkspaceChange?.(activeWs?.id || null, activeWs?.name);
  }, [activeWs?.id, activeWs?.name, onActiveWorkspaceChange]);
  // NEW — step 3e follow-up: dock-aware openScopedSubChat, keyed to
  // whichever project is selected. See comment above switchChat's
  // destructure for why this is needed now that the panel gets a real
  // workspaceId.
  const dock = useWorkspaceDock(activeWs?.id);

  // Restore this workspace's last-dispatched session_id whenever the
  // active project changes — same "fetch fresh on workspaceId change"
  // reasoning ResearchTab's ContradictionsPanel fix uses, applied to
  // localStorage instead of a backend fetch since there's no backend
  // store for this yet (see lastRunKey's own comment).
  useEffect(() => {
    if (!activeWsId) {
      setLastSessionId(null);
      setRunHistory([]);
      setViewedSessionId(null);
      return;
    }
    const last = localStorage.getItem(lastRunKey(activeWsId)) || null;
    setLastSessionId(last);
    setViewedSessionId(last);
    setRunHistory(readRunHistory(activeWsId));
  }, [activeWsId]);

  // meta: { simTypeLabel, target } — from RunSimulationPanel, for the
  // history list's display. Every fresh dispatch also becomes the
  // viewed run, same as before this pass added history.
  function recordDispatch(chatId, meta = {}) {
    if (!activeWsId) return;
    localStorage.setItem(lastRunKey(activeWsId), chatId);
    setLastSessionId(chatId);
    setViewedSessionId(chatId);
    setRunHistory(
      pushRunHistory(activeWsId, {
        chatId,
        ts: new Date().toISOString(),
        simTypeLabel: meta.simTypeLabel || null,
        target: meta.target || null,
      })
    );
  }

  const [visitedSubTabs, setVisitedSubTabs] = useState(() => new Set([subTab]));
  useEffect(() => {
    setVisitedSubTabs((prev) => (prev.has(subTab) ? prev : new Set(prev).add(subTab)));
  }, [subTab]);

  async function handlePromote(wsId, toStage = promoteTargetStage, mode = promoteMode) {
    setPromoting(true);
    setPromoteError(null);
    try {
      await promoteWorkspace(wsId, toStage, mode);
      await fetchWorkspaces();
      onPromoted?.(toStage, wsId);
      setPromoteMode("complete");
    } catch (err) {
      setPromoteError(err.message);
    } finally {
      setPromoting(false);
    }
  }

  return (
    <div className="flex h-full">
      <div className="w-56 shrink-0 border-r border-[var(--neutral-800)] flex flex-col">
        <div className="px-3 py-3 border-b border-[var(--neutral-800)] flex items-center justify-between">
          <span className="text-xs font-medium text-[var(--neutral-400)] flex items-center gap-1.5">
            <STAGE_THEME.test.Icon size={13} className={STAGE_THEME.test.color} /> Test projects
          </span>
          {/* NEW — item #10 / B3: native create, same stage-aware modal
              ResearchTab's B2 wired up first. */}
          <button
            onClick={() => setShowCreateModal(true)}
            title="New test project"
            className="text-[var(--neutral-500)] hover:text-[var(--neutral-200)]"
          >
            <Plus size={14} />
          </button>
        </div>
        <div className="flex-1 overflow-y-auto">
          {testProjects.length === 0 && (
            <p className="px-3 py-3 text-xs text-[var(--neutral-600)]">
              No test projects yet — create one above, or promote a built feature from the Tasks tab.
            </p>
          )}
          {testProjects.map((ws) => {
            // NEW — item #11 / C2: nested chat list, same pattern as
            // ResearchTab/PlanTab's C1 — "expand" just means "is the
            // active project", no separate toggle state needed since
            // this tab already has a single-selection model.
            const isActive = ws.id === activeWsId;
            const memberChats = isActive ? chats.filter((c) => ws.chat_ids.includes(c.id)) : [];
            return (
              <div key={ws.id} className="border-b border-[var(--neutral-900)]">
                <div
                  onClick={() => setActiveWsId(ws.id)}
                  className={`group w-full flex items-center gap-1.5 min-w-0 text-left px-3 py-2 text-xs cursor-pointer ${
                    isActive
                      ? "bg-[var(--neutral-800-a70)] text-[var(--neutral-100)]"
                      : "text-[var(--neutral-300)] hover:bg-[var(--neutral-900)]"
                  }`}
                >
                  <WorkspaceStageIcons workspace={ws} />
                  <span className="truncate flex-1 min-w-0">
                    {ws.name}
                    <span className="text-[var(--neutral-600)]"> · {ws.chat_ids.length}</span>
                  </span>
                  {/* NEW — issue #3: "+" creates a chat nested in this
                      project, same idea as starting a new chat under a
                      group in the Chat sidebar. */}
                  <button
                    onClick={(e) => { e.stopPropagation(); handleCreateChatInProject(ws); }}
                    title="New chat in this project"
                    className="shrink-0 opacity-0 group-hover:opacity-100 text-[var(--neutral-500)] hover:text-[var(--neutral-200)]"
                    disabled={creatingChatForWs === ws.id}
                  >
                    {creatingChatForWs === ws.id ? (
                      <Loader2 size={12} className="animate-spin" />
                    ) : (
                      <Plus size={12} />
                    )}
                  </button>
                </div>
                {memberChats.map((chat) => (
                  <div
                    key={chat.id}
                    onClick={() => editingChatId !== chat.id && openInDock(chat.id)}
                    className={`group flex items-center gap-1.5 text-left pl-7 pr-3 py-1.5 text-[11px] cursor-pointer ${
                      chat.id === activeChatId
                        ? "bg-[var(--neutral-800-a70)] text-[var(--neutral-100)]"
                        : "text-[var(--neutral-500)] hover:bg-[var(--neutral-900)] hover:text-[var(--neutral-300)]"
                    }`}
                  >
                    {editingChatId === chat.id ? (
                      <div className="flex items-center gap-1 flex-1 min-w-0" onClick={(e) => e.stopPropagation()}>
                        <input
                          autoFocus
                          value={editChatTitle}
                          onChange={(e) => setEditChatTitle(e.target.value)}
                          onKeyDown={(e) => e.key === "Enter" && commitRenameChat(chat.id)}
                          className="flex-1 min-w-0 bg-[var(--neutral-950)] border border-[var(--neutral-700)] rounded px-1.5 py-0.5 text-[11px] outline-none"
                        />
                        <button onClick={() => commitRenameChat(chat.id)}><Check size={12} className="text-green-400" /></button>
                        <button onClick={() => setEditingChatId(null)}><X size={12} className="text-[var(--neutral-500)]" /></button>
                      </div>
                    ) : (
                      <>
                        <MessageSquare size={10} className="shrink-0 text-[var(--neutral-600)]" />
                        <span className="truncate flex-1 min-w-0">{chat.title}</span>
                        {/* NEW — issue #3: rename/delete, same controls
                            ChatSidebar's own chat rows already offer. */}
                        <div className="hidden group-hover:flex items-center gap-1.5 shrink-0">
                          <button onClick={(e) => { e.stopPropagation(); startRenameChat(chat); }} title="Rename chat">
                            <Pencil size={10} className="text-[var(--neutral-500)] hover:text-[var(--neutral-200)]" />
                          </button>
                          <button onClick={(e) => { e.stopPropagation(); askDeleteChat(chat); }} title="Delete chat">
                            <Trash2 size={10} className="text-[var(--neutral-500)] hover:text-red-400" />
                          </button>
                        </div>
                      </>
                    )}
                  </div>
                ))}
              </div>
            );
          })}
        </div>
      </div>

      <div className="flex-1 min-h-0 flex flex-col">
        {activeWs && (
          <div className="flex items-center justify-between px-3 py-2 border-b border-[var(--neutral-800)]">
            <h2 className="text-sm font-medium text-[var(--neutral-100)] truncate">{activeWs.name}</h2>
            <div className="flex items-center gap-2 shrink-0">
              {(() => {
                // NEW — §2.2: exclude stages already active for this
                // workspace — same rule as Notebooks/Research/Plan/Build.
                // Test only ever has one target ("growth"), so this
                // mainly just hides the row once already
                // partial-promoted into Growth.
                const activeHere = activeWs.active_stages || [activeWs.stage];
                const availableTargets = PROMOTE_TARGETS.filter((s) => !activeHere.includes(s));
                const targetStage = availableTargets.includes(promoteTargetStage)
                  ? promoteTargetStage
                  : availableTargets[0];
                if (!availableTargets.length) return null;
                return (
                  <>
                    <label className="sr-only" htmlFor="test-promote-target">Promote to</label>
                    <select
                      id="test-promote-target"
                      value={targetStage}
                      onChange={(e) => setPromoteTargetStage(e.target.value)}
                      disabled={promoting}
                      className="bg-[var(--neutral-900)] border border-[var(--neutral-700)] text-[var(--neutral-200)] rounded-lg px-2 py-1.5 text-xs outline-none disabled:opacity-50"
                    >
                      {availableTargets.map((stage) => (
                        <option key={stage} value={stage}>{PROMOTE_LABELS[stage]}</option>
                      ))}
                    </select>
                    {/* NEW — §2.6 step 4: complete/partial toggle. */}
                    <div
                      role="radiogroup"
                      aria-label="Promote mode"
                      className="flex items-center rounded-lg border border-[var(--neutral-700)] overflow-hidden text-xs shrink-0"
                    >
                      <button
                        type="button"
                        role="radio"
                        aria-checked={promoteMode === "complete"}
                        onClick={() => setPromoteMode("complete")}
                        disabled={promoting}
                        title="Move the project fully into the target stage"
                        className={`px-2 py-1.5 font-medium disabled:opacity-50 ${
                          promoteMode === "complete"
                            ? "bg-[var(--accent)] text-[var(--accent-text)]"
                            : "bg-[var(--neutral-900)] text-[var(--neutral-400)]"
                        }`}
                      >
                        Complete
                      </button>
                      <button
                        type="button"
                        role="radio"
                        aria-checked={promoteMode === "partial"}
                        onClick={() => setPromoteMode("partial")}
                        disabled={promoting}
                        title="Keep the project active here too"
                        className={`px-2 py-1.5 font-medium disabled:opacity-50 ${
                          promoteMode === "partial"
                            ? "bg-[var(--accent)] text-[var(--accent-text)]"
                            : "bg-[var(--neutral-900)] text-[var(--neutral-400)]"
                        }`}
                      >
                        Partial
                      </button>
                    </div>
                    <button
                      onClick={() => handlePromote(activeWs.id, targetStage)}
                      disabled={promoting}
                      className="flex items-center gap-1.5 text-xs border border-[var(--neutral-700)] text-[var(--neutral-200)] rounded-lg px-3 py-1.5 font-medium disabled:opacity-50 shrink-0"
                    >
                      {promoting ? <Loader2 size={13} className="animate-spin" /> : <ArrowUpRight size={13} />}
                      {promoteMode === "partial" ? "Add to" : "Promote to"} {PROMOTE_LABELS[targetStage]} →
                    </button>
                  </>
                );
              })()}
            </div>
          </div>
        )}
        {promoteError && (
          <p className="text-xs text-red-400 px-3 pt-2">{promoteError}</p>
        )}
        <div className="flex items-center gap-1 px-3 py-2 border-b border-[var(--neutral-800)] overflow-x-auto">
          {SUB_TABS.map((t) => {
            const Icon = t.icon;
            return (
              <button
                key={t.id}
                onClick={() => setSubTab(t.id)}
                className={`flex items-center gap-1.5 text-xs rounded-lg px-2.5 py-1.5 whitespace-nowrap ${
                  subTab === t.id
                    ? "bg-[var(--cyber-violet)] text-black font-medium"
                    : "text-[var(--neutral-500)] hover:text-[var(--neutral-300)]"
                }`}
              >
                <Icon size={13} />
                {t.label}
              </button>
            );
          })}
        </div>

        <div className="flex-1 min-h-0 overflow-y-auto p-4 relative">
          {!activeWs ? (
            <p className="text-xs text-[var(--neutral-600)]">Pick or create a project to get started.</p>
          ) : (
            SUB_TABS.filter((t) => visitedSubTabs.has(t.id)).map((t) => (
              <div key={t.id} style={{ display: subTab === t.id ? "contents" : "none" }}>
                {t.id === "run" && (
                  <RunSimulationPanel
                    wsId={activeWs.id}
                    openScopedSubChat={dock.openScopedSubChat}
                    openInDock={openInDock}
                    onDispatched={recordDispatch}
                  />
                )}
                {t.id === "personas" && (
                  <PersonasPanel
                    fetchRoles={fetchRoles}
                    updateRolePrompt={updateRolePrompt}
                    setRolePinned={setRolePinned}
                  />
                )}
                {t.id === "reports" && (
                  <ReportsPanel
                    wsId={activeWs.id}
                    sessionId={viewedSessionId}
                    fetchSimulationResults={fetchSimulationResults}
                  />
                )}
                {t.id === "redteam" && (
                  <RedTeamPanel
                    wsId={activeWs.id}
                    sessionId={viewedSessionId}
                    fetchSimulationResults={fetchSimulationResults}
                  />
                )}
                {t.id === "history" && (
                  <HistoryPanel
                    runHistory={runHistory}
                    viewedSessionId={viewedSessionId}
                    lastSessionId={lastSessionId}
                    onView={setViewedSessionId}
                    openInDock={openInDock}
                  />
                )}
              </div>
            ))
          )}
        </div>
      </div>

      <div className="hidden lg:flex shrink-0 border-l border-[var(--neutral-800)]" style={{ width: chatDockCollapsed ? undefined : 560 }}>
        <WorkspaceChatPanel collapsed={chatDockCollapsed} onToggleCollapse={toggleChatDock} workspaceId={activeWs?.id} />
      </div>
      {!chatDockCollapsed && (
        <div className="lg:hidden fixed inset-0 z-40 bg-[var(--neutral-950)]">
          <WorkspaceChatPanel collapsed={false} onToggleCollapse={toggleChatDock} workspaceId={activeWs?.id} />
        </div>
      )}
      {chatDockCollapsed && (
        <button
          onClick={toggleChatDock}
          title="Open chat"
          className="lg:hidden fixed bottom-4 right-4 z-40 bg-[var(--cyber-violet)] text-black rounded-full p-3 shadow-lg"
        >
          <MessageSquare size={18} />
        </button>
      )}

      {/* NEW — item #10 / B3: stage-aware create modal (B1). Auto-selects
          the created project so the user lands straight in it instead of
          having to find it in the list themselves — same as ResearchTab's B2. */}
      {showCreateModal && (
        <CreateWorkspaceModal
          stage="test"
          onClose={(created) => {
            setShowCreateModal(false);
            if (created) setActiveWsId(created.id);
          }}
        />
      )}

      {/* NEW — issue #3: same delete-confirmation affordance as
          ChatSidebar's own per-chat delete, just scoped to a nested
          project chat here. */}
      <ConfirmDialog
        open={!!pendingDeleteChat}
        title="Delete chat"
        message={`Delete "${pendingDeleteChat?.title}"? Its messages and memory can't be recovered.`}
        confirmLabel="Delete"
        tone="danger"
        onConfirm={confirmDeleteChat}
        onCancel={() => setPendingDeleteChat(null)}
      />
    </div>
  );
}

// --- Run Simulation (§1.2 `run`) — live dispatch via openScopedSubChat,
// same pattern as ResearchTab's SourcesPanel/DatasetPanel, not a paste
// box: this genuinely triggers a real multi-worker run.
function RunSimulationPanel({ wsId, openScopedSubChat, openInDock, onDispatched }) {
  const [simType, setSimType] = useState(SIMULATION_TYPES[0].id);
  const [target, setTarget] = useState("");
  const [thorough, setThorough] = useState(false);
  const [dispatching, setDispatching] = useState(false);

  async function run() {
    if (!target.trim() || !wsId) return;
    setDispatching(true);
    try {
      const chosen = SIMULATION_TYPES.find((s) => s.id === simType);
      const task = `${chosen.taskLead}: ${target.trim()}.${
        thorough ? " Use additional personas/workers for a more thorough pass." : ""
      }`;
      const chatId = await openScopedSubChat(task);
      onDispatched(chatId, { simTypeLabel: chosen.label, target: target.trim() });
      await openInDock(chatId);
    } finally {
      setDispatching(false);
    }
  }

  return (
    <div className="space-y-4">
      <div className="border border-[var(--neutral-800)] rounded-lg p-3 space-y-3">
        <div>
          <label className="text-[11px] text-[var(--neutral-500)]">Simulation type</label>
          <select
            value={simType}
            onChange={(e) => setSimType(e.target.value)}
            className="w-full mt-1 bg-black/30 border border-[var(--neutral-800)] rounded px-2 py-1.5 text-xs outline-none focus:border-[var(--cyber-violet)]"
          >
            {SIMULATION_TYPES.map((s) => (
              <option key={s.id} value={s.id}>{s.label}</option>
            ))}
          </select>
        </div>

        <div>
          <label className="text-[11px] text-[var(--neutral-500)]">
            What's being tested
          </label>
          <textarea
            value={target}
            onChange={(e) => setTarget(e.target.value)}
            placeholder="Describe the feature, pricing, PRD excerpt, or app being tested — e.g. 'the new $12/mo Pro tier with unlimited exports'"
            rows={3}
            className="w-full mt-1 bg-black/30 border border-[var(--neutral-800)] rounded px-3 py-2 text-xs outline-none focus:border-[var(--cyber-violet)]"
          />
          <p className="text-[10px] text-[var(--neutral-600)] mt-1">
            No auto-fill from the Build cycle's handoff summary yet — paste or describe it manually.
          </p>
        </div>

        <label className="flex items-center gap-1.5 text-[11px] text-[var(--neutral-500)] cursor-pointer">
          <input type="checkbox" checked={thorough} onChange={(e) => setThorough(e.target.checked)} />
          N workers, parallel (more personas, slower)
        </label>

        <button
          onClick={run}
          disabled={dispatching || !target.trim()}
          className="text-xs bg-[var(--cyber-violet)] text-black rounded px-3 py-2 font-medium disabled:opacity-50 flex items-center gap-1.5"
        >
          {dispatching ? <Loader2 size={13} className="animate-spin" /> : <Sparkles size={13} />}
          {dispatching ? "Dispatching…" : "Run simulation"}
        </button>
      </div>
      <p className="text-[11px] text-[var(--neutral-600)]">
        Runs the chosen persona set in this project's own chat — watch the parallel branches live
        in the dock on the right, then check the Friction Reports tab once it finishes.
      </p>
    </div>
  );
}

// --- Friction Reports (§1.2 `reports`) — reads back simulation_synthesizer's
// synthesis plus each persona's own reaction off the memory bus for the
// most recently dispatched run in this workspace (see lastRunKey's
// comment for why that's localStorage, not a backend store). Every
// report carries the "AI-estimated — not verified" amber banner, same
// component instance ResearchTab's ContradictionsPanel uses for
// consensus_meter — these are simulated reactions, not real user data.
const PERSONA_LABELS = {
  persona_customer: "Customer (enthusiastic-but-realistic)",
  persona_skeptic: "Customer (skeptical)",
  critic_reviewer: "Critic / Reviewer",
  usability_walkthrough: "Usability Walkthrough",
  red_team: "Red Team",
  pricing_sensitivity: "Pricing Sensitivity",
  support_ticket_predictor: "Support-Ticket Prediction",
  competitor_response: "Competitive Response",
  marketplace_review_batch: "Marketplace Reviews",
  simulation_synthesizer: "Synthesis (cross-persona summary)",
};

function ReportsPanel({ wsId, sessionId, fetchSimulationResults }) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [data, setData] = useState(null);

  useEffect(() => {
    if (!sessionId) {
      setData(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetchSimulationResults(wsId, sessionId)
      .then((res) => { if (!cancelled) setData(res); })
      .catch((e) => { if (!cancelled) setError(e.message || "Failed to load results."); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [wsId, sessionId, fetchSimulationResults]);

  if (!sessionId) {
    return (
      <p className="text-xs text-[var(--neutral-600)]">
        No simulation run yet for this project — dispatch one from the Run Simulation tab first.
      </p>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-[11px] text-[var(--neutral-600)]">Showing the most recently dispatched run.</p>
        <button
          onClick={() => fetchSimulationResults(wsId, sessionId).then(setData).catch((e) => setError(e.message))}
          className="text-xs text-[var(--neutral-500)] hover:text-[var(--neutral-200)] flex items-center gap-1"
        >
          <RefreshCw size={12} className={loading ? "animate-spin" : ""} /> Refresh
        </button>
      </div>

      {loading && !data && (
        <div className="text-xs text-[var(--neutral-600)] flex items-center gap-1.5"><Loader2 size={12} className="animate-spin" /> Loading…</div>
      )}
      {error && <p className="text-[11px] text-red-400">{error}</p>}

      {data && !data.synthesis && data.personas.length === 0 && (
        <p className="text-xs text-[var(--neutral-600)]">
          Nothing has landed on the bus yet for this run — it may still be in progress. Check the
          chat dock, or hit Refresh once it finishes.
        </p>
      )}

      {data?.synthesis && (
        <div className="border border-[var(--cyber-amber)]/40 bg-[var(--cyber-amber)]/5 rounded-lg p-3">
          <p className="text-[10px] uppercase tracking-wide text-[var(--cyber-amber)] mb-2">
            AI-estimated — not verified · simulated reactions, not real user data
          </p>
          <Markdown>{data.synthesis}</Markdown>
        </div>
      )}

      {data?.personas?.length > 0 && (
        <div className="space-y-3">
          <p className="text-[11px] text-[var(--neutral-500)] uppercase tracking-wide">Individual reactions</p>
          {data.personas.map((p) => (
            <div key={p.role} className="border border-[var(--neutral-800)] rounded-lg p-3">
              <p className="text-xs font-medium text-[var(--neutral-100)] mb-1.5">
                {PERSONA_LABELS[p.role] || p.role}
              </p>
              {p.text && <Markdown>{p.text}</Markdown>}
              {p.reviews && (
                <div className="space-y-2 mt-1">
                  {p.reviews.map((r, i) => (
                    <div key={i} className="border border-[var(--neutral-900)] rounded p-2">
                      <div className="flex items-center gap-2 text-[10px] text-[var(--neutral-500)] mb-1">
                        <span>{"★".repeat(Math.max(0, Math.min(5, r.rating || 0)))}</span>
                        <span className="uppercase tracking-wide">{r.sentiment}</span>
                      </div>
                      <p className="text-[11px] text-[var(--neutral-300)]">{r.text}</p>
                    </div>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// --- Personas (§1.2 `personas`) — a filtered view over the Role
// Library store (GET/PUT/PATCH /api/roles), not a new backend concept:
// scoped client-side to SIMULATE_DOMAIN_ROLES so this reads as "the
// personas this tab hires" rather than the full, unrelated role list
// the standalone Role Library panel shows. A role that's never been
// hired yet has no entry in the store (list_role_metadata only returns
// roles someone has actually briefed) — shown as an honest "not yet
// briefed" row rather than a fabricated default, same discipline as
// every other flagged gap in this codebase.
function PersonasPanel({ fetchRoles, updateRolePrompt, setRolePinned }) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [rolesByName, setRolesByName] = useState({});
  const [editingRole, setEditingRole] = useState(null);
  const [draftBrief, setDraftBrief] = useState("");
  const [savingRole, setSavingRole] = useState(null);
  const [pinningRole, setPinningRole] = useState(null);

  function load() {
    setLoading(true);
    setError(null);
    fetchRoles()
      .then((list) => {
        const map = {};
        for (const entry of list) map[entry.role] = entry;
        setRolesByName(map);
      })
      .catch((e) => setError(e.message || "Failed to load roles."))
      .finally(() => setLoading(false));
  }

  useEffect(() => { load(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  function startEdit(role, currentBrief) {
    setEditingRole(role);
    setDraftBrief(currentBrief || "");
  }

  async function saveEdit(role) {
    setSavingRole(role);
    try {
      await updateRolePrompt(role, draftBrief);
      setEditingRole(null);
      load();
    } catch (e) {
      setError(e.message || "Failed to save brief.");
    } finally {
      setSavingRole(null);
    }
  }

  async function togglePin(role, currentlyPinned) {
    setPinningRole(role);
    try {
      await setRolePinned(role, !currentlyPinned);
      load();
    } catch (e) {
      setError(e.message || "Failed to update pin.");
    } finally {
      setPinningRole(null);
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <p className="text-[11px] text-[var(--neutral-600)]">
          The role briefs this tab's simulations hire from — same store as the Role Library panel,
          filtered to the simulate domain's own roles.
        </p>
        <button
          onClick={load}
          className="text-xs text-[var(--neutral-500)] hover:text-[var(--neutral-200)] flex items-center gap-1 shrink-0"
        >
          <RefreshCw size={12} className={loading ? "animate-spin" : ""} /> Refresh
        </button>
      </div>
      {error && <p className="text-[11px] text-red-400">{error}</p>}
      {loading && Object.keys(rolesByName).length === 0 && (
        <div className="text-xs text-[var(--neutral-600)] flex items-center gap-1.5"><Loader2 size={12} className="animate-spin" /> Loading…</div>
      )}

      <div className="space-y-2">
        {SIMULATE_DOMAIN_ROLES.map((role) => {
          const entry = rolesByName[role];
          const label = PERSONA_LABELS[role] || role;
          const isEditing = editingRole === role;
          const pinned = !!entry?.pinned;

          return (
            <div key={role} className="border border-[var(--neutral-800)] rounded-lg p-3">
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <p className="text-xs font-medium text-[var(--neutral-100)]">{label}</p>
                  <p className="text-[10px] text-[var(--neutral-600)] font-mono">{role}</p>
                </div>
                <div className="flex items-center gap-2 shrink-0 text-[10px] text-[var(--neutral-500)]">
                  {entry && (
                    <>
                      <span className="uppercase tracking-wide">{entry.source || "seed"}</span>
                      <span>· hired {entry.times_hired || 0}×</span>
                    </>
                  )}
                  <button
                    onClick={() => togglePin(role, pinned)}
                    disabled={pinningRole === role}
                    title={pinned ? "Unpin" : "Pin"}
                    className={`p-1 rounded hover:bg-[var(--neutral-900)] ${pinned ? "text-[var(--cyber-amber)]" : "text-[var(--neutral-500)]"}`}
                  >
                    {pinningRole === role ? <Loader2 size={12} className="animate-spin" /> : pinned ? <Pin size={12} /> : <PinOff size={12} />}
                  </button>
                  {!isEditing && (
                    <button
                      onClick={() => startEdit(role, entry?.brief)}
                      title="Edit brief"
                      className="p-1 rounded hover:bg-[var(--neutral-900)] text-[var(--neutral-500)]"
                    >
                      <Pencil size={12} />
                    </button>
                  )}
                </div>
              </div>

              {!entry && !isEditing && (
                <p className="text-[11px] text-[var(--neutral-600)] mt-1.5">
                  Not yet briefed — a first-hire cold-start brief will be written and saved here the
                  first time a simulation run hires this role.
                </p>
              )}

              {!isEditing && entry?.brief && (
                <p className="text-[11px] text-[var(--neutral-400)] mt-1.5 leading-relaxed">{entry.brief}</p>
              )}

              {isEditing && (
                <div className="mt-2 space-y-2">
                  <textarea
                    value={draftBrief}
                    onChange={(e) => setDraftBrief(e.target.value)}
                    rows={4}
                    className="w-full bg-black/30 border border-[var(--neutral-800)] rounded px-2 py-1.5 text-[11px] outline-none focus:border-[var(--cyber-violet)]"
                  />
                  <div className="flex items-center gap-2">
                    <button
                      onClick={() => saveEdit(role)}
                      disabled={savingRole === role || !draftBrief.trim()}
                      className="text-[11px] bg-[var(--cyber-violet)] text-black rounded px-2 py-1 font-medium disabled:opacity-50 flex items-center gap-1"
                    >
                      {savingRole === role ? <Loader2 size={11} className="animate-spin" /> : <Check size={11} />} Save
                    </button>
                    <button
                      onClick={() => setEditingRole(null)}
                      className="text-[11px] text-[var(--neutral-500)] hover:text-[var(--neutral-200)] flex items-center gap-1"
                    >
                      <X size={11} /> Cancel
                    </button>
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// --- Red Team (§1.2 `redteam`) — same fetchSimulationResults() read
// `reports` uses, filtered down to just red_team's own entry. Rendered
// as prose with an explicit "not yet severity-tagged" note rather than
// a fabricated severity list: red_team's own ROLE_PROMPTS_SEED brief
// (eo/registry.py) asks it to "be specific about the failure mode and
// how it would actually happen," but never specifies a structured
// output shape the way marketplace_review_batch's brief does — treating
// its prose as if it had labeled severities would be inventing a
// contract the role was never actually briefed to fill.
function RedTeamPanel({ wsId, sessionId, fetchSimulationResults }) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [data, setData] = useState(null);

  function load() {
    if (!sessionId) return;
    setLoading(true);
    setError(null);
    fetchSimulationResults(wsId, sessionId)
      .then(setData)
      .catch((e) => setError(e.message || "Failed to load results."))
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    if (!sessionId) { setData(null); return; }
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wsId, sessionId]);

  if (!sessionId) {
    return (
      <p className="text-xs text-[var(--neutral-600)]">
        No simulation run selected — dispatch one from the Run Simulation tab, or pick a past run
        from History.
      </p>
    );
  }

  const redTeamEntry = data?.personas?.find((p) => p.role === "red_team");

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-[11px] text-[var(--neutral-600)]">Showing red_team's pass for the selected run.</p>
        <button
          onClick={load}
          className="text-xs text-[var(--neutral-500)] hover:text-[var(--neutral-200)] flex items-center gap-1"
        >
          <RefreshCw size={12} className={loading ? "animate-spin" : ""} /> Refresh
        </button>
      </div>

      {loading && !data && (
        <div className="text-xs text-[var(--neutral-600)] flex items-center gap-1.5"><Loader2 size={12} className="animate-spin" /> Loading…</div>
      )}
      {error && <p className="text-[11px] text-red-400">{error}</p>}

      {data && !redTeamEntry && (
        <p className="text-xs text-[var(--neutral-600)]">
          No red_team pass on the bus yet for this run — it may not have been hired for this
          simulation type, or the run is still in progress. Check the chat dock, or hit Refresh.
        </p>
      )}

      {redTeamEntry?.text && (
        <div className="border border-red-500/40 bg-red-500/5 rounded-lg p-3">
          <p className="text-[10px] uppercase tracking-wide text-red-400 mb-2 flex items-center gap-1.5">
            <AlertTriangle size={11} /> AI-estimated — not verified · prose findings, not yet severity-tagged
          </p>
          <Markdown>{redTeamEntry.text}</Markdown>
        </div>
      )}
    </div>
  );
}

// --- History (§1.2 `history`) — a real client-side list of past
// dispatched runs (see runHistoryKey's own comment for why this is
// localStorage rather than a new backend store). Picking an entry
// re-points `reports`/`redteam` at that run without disturbing
// lastSessionId, so the "most recent run" pointer those tabs default to
// stays accurate even after browsing older history.
function HistoryPanel({ runHistory, viewedSessionId, lastSessionId, onView, openInDock }) {
  if (runHistory.length === 0) {
    return (
      <p className="text-xs text-[var(--neutral-600)]">
        No runs dispatched yet for this project — dispatch one from the Run Simulation tab.
      </p>
    );
  }

  return (
    <div className="space-y-2">
      <p className="text-[11px] text-[var(--neutral-600)]">
        Last {runHistory.length} run{runHistory.length === 1 ? "" : "s"} dispatched from this project,
        this browser only.
      </p>
      {runHistory.map((run) => {
        const isViewed = run.chatId === viewedSessionId;
        const isLast = run.chatId === lastSessionId;
        return (
          <div
            key={`${run.chatId}-${run.ts}`}
            className={`border rounded-lg p-3 ${isViewed ? "border-[var(--cyber-violet)]" : "border-[var(--neutral-800)]"}`}
          >
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <p className="text-xs font-medium text-[var(--neutral-100)]">
                  {run.simTypeLabel || "Simulation run"}
                  {isLast && <span className="ml-1.5 text-[10px] text-[var(--neutral-500)] font-normal">· most recent</span>}
                </p>
                {run.target && (
                  <p className="text-[11px] text-[var(--neutral-500)] truncate mt-0.5">{run.target}</p>
                )}
                <p className="text-[10px] text-[var(--neutral-600)] flex items-center gap-1 mt-1">
                  <Clock size={10} />
                  {new Date(run.ts).toLocaleString()}
                </p>
              </div>
              <div className="flex items-center gap-2 shrink-0">
                <button
                  onClick={() => onView(run.chatId)}
                  disabled={isViewed}
                  className={`text-[11px] rounded px-2 py-1 flex items-center gap-1 ${
                    isViewed
                      ? "text-[var(--cyber-violet)] cursor-default"
                      : "text-[var(--neutral-400)] hover:text-[var(--neutral-100)] border border-[var(--neutral-700)]"
                  }`}
                >
                  <Eye size={11} /> {isViewed ? "Viewing" : "View report"}
                </button>
                <button
                  onClick={() => openInDock(run.chatId)}
                  className="text-[11px] text-[var(--neutral-400)] hover:text-[var(--neutral-100)] border border-[var(--neutral-700)] rounded px-2 py-1 flex items-center gap-1"
                >
                  <MessageSquare size={11} /> Open chat
                </button>
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}