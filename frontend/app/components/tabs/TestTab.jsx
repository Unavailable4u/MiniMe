import { useState, useEffect, memo } from "react";
import { useSession } from "../../context/SessionContext";
import { useWorkspaces } from "../../context/WorkspacesContext";   // FIX — Item 2 concern split, slice 3 follow-up: this file was missed when workspaces/fetchWorkspaces moved out of useSession()
import { useChatList } from "../../context/ChatListContext";   // NEW — Item 2 concern split, slice 4
import Markdown from "../Markdown";
import WorkspaceChatPanel from "../WorkspaceChatPanel";
import { useWorkspaceDockActions, useWorkspaceDock, useLastActiveChatId } from "../../context/WorkspaceDockContext"; // NEW — step 3e (+ follow-up fix below); useLastActiveChatId added for item #11 / C2
import CreateWorkspaceModal from "../CreateWorkspaceModal"; // NEW — item #10 / B3: native "create project" for this tab, same as ResearchTab's B2
import ConfirmDialog from "../ConfirmDialog"; // NEW — issue #3: same delete-confirmation affordance as ChatSidebar's own per-chat delete
import ManageWorkspaceModal from "../ManageWorkspaceModal"; // NEW — project management (rename/delete/members/export), parity with Notebooks/Plan — was already built, just never wired into this tab
import { ChatRowMenu } from "../RowMenu"; // NEW — shared per-chat "⋮" menu (Rename/Delete), same one Chat sidebar + every other stage tab uses
import WorkspaceStageIcons, { STAGE_THEME } from "../WorkspaceStageIcons"; // NEW — item #2: colored per-stage icon + per-project stage badges
import { useViewport } from "../../hooks/useViewport";   // NEW — mobile UI retrofit (MOBILE_PLAN.md Phase 5), same reference pattern as PlanTab/ResearchTab/NotebooksTab
import { OPEN_TAB_SIDEBAR_EVENT } from "../mobile/events"; // NEW — hamburger -> this tab's own project-picker drawer, same wiring as Notebooks/Research/Plan
import MobileTestTab from "../mobile/TestTab";             // NEW — real structural fork, see that file and MOBILE_PLAN.md
import {
  FlaskConical, Users, ClipboardList, ShieldAlert, History,
  Loader2, RefreshCw, MessageSquare, ArrowUpRight, Sparkles,
  Pin, PinOff, Pencil, Check, X, Clock, AlertTriangle, Eye, Plus, MoreVertical, ChevronRight, ChevronLeft,
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

// NEW — mobile retrofit: SUB_TABS/PROMOTE_TARGETS/PROMOTE_LABELS are exported
// (were module-local) so components/mobile/TestTab.jsx builds its icon rail
// and combo <select> off the same lists instead of a forked copy that can
// drift — same convention as PlanTab.jsx's/ResearchTab.jsx's own exports.
export const SUB_TABS = [
  { id: "run",      label: "Run Simulation",   icon: FlaskConical },
  { id: "personas", label: "Personas",         icon: Users },
  { id: "reports",  label: "Friction Reports", icon: ClipboardList },
  { id: "redteam",  label: "Red Team",         icon: ShieldAlert },
  { id: "history",  label: "History",          icon: History },
];
export const PROMOTE_TARGETS = ["growth"];
export const PROMOTE_LABELS = { growth: "Growth" };

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
// NEW — collapsible project-picker sidebar, same pattern as the chat
// dock's own collapse above.
const PROJECTS_KEY = "minime_test_projects_collapsed";

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

// Reads <html data-viewport> directly (applied at module load by
// useViewport.js) instead of the hook's own state, which deliberately
// starts at "desktop" on every first render for hydration safety and only
// corrects after mount. That's one paint too late for the one decision
// this feeds — whether the chat dock starts open — because on a phone an
// open dock is a full-screen overlay, so getting it wrong even for a
// frame flashes the whole tab body behind a chat panel. Safe to read
// during render here: this tab is only ever mounted client-side after a
// tab click (AppShell's visitedTabs starts as {"chat"}), never in the
// server-rendered HTML, so there's no hydration pass for it to disagree
// with.
function viewportIsMobileNow() {
  return typeof document !== "undefined" && document.documentElement.dataset.viewport === "mobile";
}

// CHANGED — mobile UI retrofit (MOBILE_PLAN.md Phase 5): this used to be
// the component itself (default-exported directly, its return JSX mixing
// desktop-only and shared markup with no viewport branch at all — Test
// hadn't been touched for mobile yet). Same retrofit as PlanTab.jsx/
// ResearchTab.jsx/NotebooksTab.jsx: it's now a controller hook — every
// bit of state, every effect, every handler, and the JSX that's
// genuinely IDENTICAL on every viewport (projectRows, subTabContent,
// dockAndModals) all still live here, unforked. Only the project-picker
// column (drawer on mobile), the sub-tab nav (icon-only rail on mobile),
// the promote control (one combo <select> on mobile instead of three
// separate widgets) and the no-project empty state ever differed by
// viewport, so those are the only things each shell builds for itself,
// handed back in via `renderRoot`'s slots — see TestTabDesktop below and
// components/mobile/TestTab.jsx for the two callers.
export function useTestTabController({ initialWorkspaceId, onConsumeInitialWorkspaceId, onPromoted, onActiveWorkspaceChange }) {
  const {
    promoteWorkspace,
    fetchSimulationResults,
    fetchRoles, updateRolePrompt, setRolePinned,
  } = useSession();
  const { workspaces, fetchWorkspaces } = useWorkspaces();   // FIX — was destructured off useSession(), which no longer serves it; this call site was missed at the time
  const { chats } = useChatList();   // CHANGED — Item 2 concern split, slice 4: was useSession()
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
  // CHANGED — starts collapsed on mobile (see viewportIsMobileNow above
  // for why it's read this way). Desktop is unchanged: starts open, then
  // the mount effect below restores whatever was saved.
  const [chatDockCollapsed, setChatDockCollapsed] = useState(viewportIsMobileNow);
  const [projectsCollapsed, setProjectsCollapsed] = useState(false); // NEW — collapsible project-picker sidebar
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
  // NEW — project management: which project's manage modal (rename /
  // delete / members / export) is open. Same shape as PlanTab's own
  // managingWorkspace — ManageWorkspaceModal already existed fully
  // built, this tab just never had an entry point into it.
  const [managingWorkspace, setManagingWorkspace] = useState(null);

  // NEW — mobile retrofit, same wiring as PlanTab.jsx/ResearchTab.jsx/
  // NotebooksTab.jsx: on mobile the project-picker column below never
  // renders inline (no width to spare next to sub-tab content) — it
  // becomes a hamburger-triggered MobileDrawer instead. AppShell.jsx
  // can't reach into this hook's state directly, so it dispatches
  // OPEN_TAB_SIDEBAR_EVENT on hamburger tap and this just listens and
  // flips its own drawer open (see AppShell.jsx's
  // TABS_WITH_OWN_MOBILE_SIDEBAR, now including "test").
  const [viewport] = useViewport();
  const isMobile = viewport === "mobile";
  const [mobileTestDrawerOpen, setMobileTestDrawerOpen] = useState(false);
  useEffect(() => {
    function onOpenTabSidebar(e) {
      if (e.detail?.tabId === "test") setMobileTestDrawerOpen(true);
    }
    window.addEventListener(OPEN_TAB_SIDEBAR_EVENT, onOpenTabSidebar);
    return () => window.removeEventListener(OPEN_TAB_SIDEBAR_EVENT, onOpenTabSidebar);
  }, []);

  useEffect(() => {
    // CHANGED — CHAT_DOCK_KEY is the DESKTOP preference. Test defaults
    // its dock to open (§1.3: the live persona branches ARE the tab), which
    // is right beside the sub-tabs on desktop but, on a phone, is a
    // full-screen overlay — so mobile never restores it (it always lands
    // on the tab itself, chat one tap away via the floating bubble) and,
    // see toggleChatDock below, never overwrites it either.
    if (!viewportIsMobileNow()) setChatDockCollapsed(localStorage.getItem(CHAT_DOCK_KEY) === "1");
    setProjectsCollapsed(localStorage.getItem(PROJECTS_KEY) === "1"); // NEW — collapsible project sidebar
  }, []);

  function toggleChatDock() {
    setChatDockCollapsed((prev) => {
      // CHANGED — mobile's open/closed state is per-visit, not a saved
      // preference: writing it here would let one phone session silently
      // flip what the desktop layout restores next time.
      if (!isMobile) localStorage.setItem(CHAT_DOCK_KEY, !prev ? "1" : "0");
      return !prev;
    });
  }

  // NEW — collapsible project-picker sidebar, same toggle pattern as
  // toggleChatDock above, its own localStorage key so the two collapse
  // independently.
  function toggleProjects() {
    setProjectsCollapsed((prev) => {
      localStorage.setItem(PROJECTS_KEY, !prev ? "1" : "0");
      return !prev;
    });
  }

  // For the project list's chat rows (browsing). FIX — mobile: picking a
  // chat from the project drawer used to force the dock open
  // unconditionally whenever it was collapsed, which on mobile means the
  // full-screen chat overlay (`lg:hidden fixed inset-0` in dockAndModals
  // below) slams over the whole screen the instant you tap a chat. On
  // mobile that's the person's call, not something selecting a chat
  // should decide for them — they can still open it via the floating
  // "Open chat" bubble. Desktop keeps the original auto-expand behavior.
  // Same fix as PlanTab.jsx's/ResearchTab.jsx's/NotebooksTab.jsx's own
  // openInDock.
  async function openInDock(chatId) {
    await switchChat(chatId);
    if (!isMobile && chatDockCollapsed) toggleChatDock();
  }

  // NEW — for the two places in this tab where opening the chat IS the
  // action the person just asked for: RunSimulationPanel's "Run
  // simulation" (the whole point of the tab is watching the run) and
  // HistoryPanel's "Open chat". openInDock's mobile guard above is right
  // for browsing a list but wrong here — without this, tapping "Run
  // simulation" on a phone would dispatch the run and then show nothing.
  async function revealChatInDock(chatId) {
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
      // NEW — the project drawer (z-50) sits above the chat overlay
      // (z-40), so without this the chat that was just created and opened
      // stays hidden behind the drawer that created it.
      if (isMobile) setMobileTestDrawerOpen(false);
    } catch (err) {
      // Bug fix: no catch here previously -- a failed create-chat
      // request (backend unreachable, CORS, network drop, etc.)
      // crashed with an unhandled promise rejection instead of
      // telling the user anything. Same inline alert() fallback
      // NotebooksTab's rename/progress handlers already use --
      // there's no toast system in these tab files.
      alert(`Couldn't create chat: ${err.message || err}`);
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

  // FIX — same stale-selection bug as ResearchTab: was `if (!activeWsId
  // && ...)`, which never re-checked an id that was already set. A
  // project deleted while this tab was kept alive in the background
  // (every visited tab stays mounted — see AppShell.jsx) left
  // activeWsId pointed at a dead id forever, 404ing on every visit.
  useEffect(() => {
    if (testProjects.length === 0) return;
    const stillExists = activeWsId && testProjects.some((w) => w.id === activeWsId);
    if (!stillExists) setActiveWsId(testProjects[0].id);
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

  const promoteTargets = (() => {
    // NEW — §2.2: exclude stages already active for this workspace —
    // same rule as Notebooks/Research/Plan/Build. Test only ever has
    // one target ("growth"), so this mainly just hides the row once
    // already partial-promoted into Growth. (Moved here from an inline
    // IIFE in the header JSX so both shells share one computation.)
    if (!activeWs) return null;
    const activeHere = activeWs.active_stages || [activeWs.stage];
    const availableTargets = PROMOTE_TARGETS.filter((s) => !activeHere.includes(s));
    if (!availableTargets.length) return null;
    const targetStage = availableTargets.includes(promoteTargetStage) ? promoteTargetStage : availableTargets[0];
    return { availableTargets, targetStage };
  })();

  // NEW — mobile retrofit: project-picker rows, shared between the
  // desktop sidebar and the mobile drawer (components/mobile/TestTab.jsx)
  // — identical markup either way, only the container around it differs
  // by viewport. Same "build once, share via controller" idea as
  // PlanTab.jsx's/ResearchTab.jsx's projectRows.
  //
  // The isMobile branches below are cosmetic, not structural (README's
  // rule), so they stay inline where they still apply (the rename field's
  // 16px text, the row padding). The hover-only controls and their hit
  // areas are now the shared `row-reveal`/`touch-target` classes
  // (globals.css) — keyed to touch input rather than to `isMobile`, so a
  // tablet wider than the mobile breakpoint gets them too.
  const projectRows = (
    <>
      {testProjects.length === 0 && (
        <p className="px-3 py-3 text-xs text-[var(--neutral-600)]">
          {/* FIX — said "Tasks tab"; that tab's label became "Build" (see the TABS comment in AppShell.jsx). */}
          No test projects yet — create one above, or promote a built feature from the Build tab.
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
              className={`group touch-row w-full flex items-center gap-1.5 min-w-0 text-left px-3 ${isMobile ? "py-3" : "py-2"} text-xs cursor-pointer ${
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
                  group in the Chat sidebar. CHANGED — `row-reveal`:
                  hidden until hover with a mouse, always visible on
                  touch (opacity-0-until-hover never reveals there);
                  `touch-target`: 40px hit area on touch. */}
              <button
                onClick={(e) => { e.stopPropagation(); handleCreateChatInProject(ws); }}
                title="New chat in this project"
                aria-label="New chat in this project"
                className="row-reveal touch-target shrink-0 text-[var(--neutral-500)] hover:text-[var(--neutral-200)]"
                disabled={creatingChatForWs === ws.id}
              >
                {creatingChatForWs === ws.id ? (
                  <Loader2 size={12} className="animate-spin" />
                ) : (
                  <Plus size={12} />
                )}
              </button>
              {/* NEW — project management, same "⋮" -> ManageWorkspaceModal
                  entry point Notebooks and Plan have. stopPropagation:
                  this row is itself clickable (select project). */}
              <button
                onClick={(e) => { e.stopPropagation(); setManagingWorkspace(ws); }}
                title="Rename or delete project"
                aria-label="Manage project"
                className="row-reveal touch-target shrink-0 text-[var(--neutral-600)] hover:text-[var(--neutral-200)]"
              >
                <MoreVertical size={13} />
              </button>
            </div>
            {memberChats.map((chat) => (
              <div
                key={chat.id}
                onClick={() => { if (editingChatId !== chat.id) { openInDock(chat.id); setMobileTestDrawerOpen(false); } }}
                className={`group touch-row flex items-center gap-1.5 text-left pl-7 pr-3 ${isMobile ? "py-2.5 text-xs" : "py-1.5 text-[11px]"} cursor-pointer ${
                  chat.id === activeChatId
                    ? "bg-[var(--neutral-800-a70)] text-[var(--neutral-100)]"
                    : "text-[var(--neutral-500)] hover:bg-[var(--neutral-900)] hover:text-[var(--neutral-300)]"
                }`}
              >
                {editingChatId === chat.id ? (
                  <div className="flex items-center gap-1 flex-1 min-w-0" onClick={(e) => e.stopPropagation()}>
                    {/* CHANGED — mobile: 16px, or iOS Safari zooms the
                        page the moment the rename field takes focus. */}
                    <input
                      autoFocus
                      id={`chat-title-${chat.id}`}
                      name="chatTitle"
                      aria-label="Chat title"
                      value={editChatTitle}
                      onChange={(e) => setEditChatTitle(e.target.value)}
                      onKeyDown={(e) => e.key === "Enter" && commitRenameChat(chat.id)}
                      className={`flex-1 min-w-0 bg-[var(--neutral-950)] border border-[var(--neutral-700)] rounded px-1.5 outline-none ${
                        isMobile ? "py-1 text-base" : "py-0.5 text-[11px]"
                      }`}
                    />
                    <button
                      onClick={() => commitRenameChat(chat.id)}
                      aria-label="Save chat title"
                      className={isMobile ? "flex items-center justify-center w-8 h-8 shrink-0" : ""}
                    >
                      <Check size={isMobile ? 16 : 12} className="text-green-400" />
                    </button>
                    <button
                      onClick={() => setEditingChatId(null)}
                      aria-label="Cancel rename"
                      className={isMobile ? "flex items-center justify-center w-8 h-8 shrink-0" : ""}
                    >
                      <X size={isMobile ? 16 : 12} className="text-[var(--neutral-500)]" />
                    </button>
                  </div>
                ) : (
                  <>
                    <MessageSquare size={10} className="shrink-0 text-[var(--neutral-600)]" />
                    <span className="truncate flex-1 min-w-0">{chat.title}</span>
                    {/* CHANGED — was a Pencil + Trash2 pair (hover-only on
                        desktop; two 32px buttons side by side on mobile,
                        where a slip on Delete meant losing a chat, guarded
                        only by the confirm dialog). Now the same single
                        "..." menu the Chat sidebar's rows use — Delete is
                        a deliberate second tap, not a neighbor of Rename. */}
                    <ChatRowMenu
                      onRename={() => startRenameChat(chat)}
                      onDelete={() => askDeleteChat(chat)}
                    />
                  </>
                )}
              </div>
            ))}
          </div>
        );
      })}
    </>
  );

  // Identical on every viewport — only the nav chrome around it
  // (subTabNav, built per-shell) ever differed. `activeWs &&` because the
  // no-project case is now owned by renderRoot (desktop: the old one-line
  // sentence; mobile: an actionable empty state).
  const subTabContent = activeWs && SUB_TABS.filter((t) => visitedSubTabs.has(t.id)).map((t) => (
    <div key={t.id} style={{ display: subTab === t.id ? "contents" : "none" }}>
      {t.id === "run" && (
        <RunSimulationPanel
          wsId={activeWs.id}
          openScopedSubChat={dock.openScopedSubChat}
          revealChat={revealChatInDock}
          onDispatched={recordDispatch}
          isMobile={isMobile}
        />
      )}
      {t.id === "personas" && (
        <PersonasPanel
          fetchRoles={fetchRoles}
          updateRolePrompt={updateRolePrompt}
          setRolePinned={setRolePinned}
          isMobile={isMobile}
        />
      )}
      {t.id === "reports" && (
        <ReportsPanel
          wsId={activeWs.id}
          sessionId={viewedSessionId}
          lastSessionId={lastSessionId}
          fetchSimulationResults={fetchSimulationResults}
          isMobile={isMobile}
        />
      )}
      {t.id === "redteam" && (
        <RedTeamPanel
          wsId={activeWs.id}
          sessionId={viewedSessionId}
          fetchSimulationResults={fetchSimulationResults}
          isMobile={isMobile}
        />
      )}
      {t.id === "history" && (
        <HistoryPanel
          runHistory={runHistory}
          viewedSessionId={viewedSessionId}
          lastSessionId={lastSessionId}
          onView={setViewedSessionId}
          revealChat={revealChatInDock}
          isMobile={isMobile}
        />
      )}
    </div>
  ));

  // NEW — embedded chat + WorkingPanel dock, scoped to this tab's own
  // activeWs. Identical on every viewport apart from the two mobile
  // details called out inline (the lg:hidden/hidden lg:flex pair is a
  // plain Tailwind breakpoint, same as PlanTab.jsx's/ResearchTab.jsx's
  // own dockAndModals, not the useViewport() data-viewport switch).
  const dockAndModals = (
    <>
      {/* CHANGED — closed dock renders nothing on desktop instead of a
          reserved rail; the floating bubble below is the way back in at
          every width now. Same change across all six docked tabs.
          CHANGED — not mounted at all on mobile: it's `hidden` below lg,
          so on a phone it was a second full WorkspaceChatPanel (message
          list, Pusher subscriptions, composer) mounted for nothing next
          to the overlay below. */}
      {!chatDockCollapsed && !isMobile && (
        <div className="hidden lg:flex shrink-0 border-l border-[var(--neutral-800)]" style={{ width: 560 }}>
          <WorkspaceChatPanel collapsed={false} onToggleCollapse={toggleChatDock} workspaceId={activeWs?.id} stacked />
        </div>
      )}
      {/* CHANGED — was `fixed inset-0`, which is sized to the layout
          viewport and so doesn't shrink for the on-screen keyboard on iOS
          — the chat composer at the bottom of this overlay ended up
          underneath it. `app-shell-viewport` (globals.css) is the
          shell's own answer to exactly that (100dvh, refined by
          AppShell's visualViewport effect into --app-vvh), so this
          layer now follows it instead of the raw layout viewport. */}
      {!chatDockCollapsed && (
        <div className="lg:hidden fixed inset-x-0 top-0 z-40 bg-[var(--neutral-950)] app-shell-viewport">
          <WorkspaceChatPanel collapsed={false} onToggleCollapse={toggleChatDock} workspaceId={activeWs?.id} stacked />
        </div>
      )}
      {/* CHANGED — safe-area offsets keep the bubble clear of an
          iPhone's home indicator (layout.js's viewportFit: "cover" is
          what makes it draw there); both insets are 0 elsewhere, so
          this is the same bottom-4/right-4 it always was. */}
      {chatDockCollapsed && (
        <button
          onClick={toggleChatDock}
          title="Open chat"
          aria-label="Open chat"
          className="fixed bottom-[calc(1rem+env(safe-area-inset-bottom))] right-[calc(1rem+env(safe-area-inset-right))] z-40 bg-[var(--accent)] text-[var(--accent-text)] rounded-full p-3 shadow-lg"
        >
          <MessageSquare size={18} />
        </button>
      )}

      {/* NEW — project management modal; see managingWorkspace above.
          Deleting the active project here is safe: the stillExists
          effect near the top re-points the selection at the next one. */}
      {managingWorkspace && (
        <ManageWorkspaceModal
          workspace={managingWorkspace}
          allChats={chats}
          onClose={() => setManagingWorkspace(null)}
        />
      )}

      {/* NEW — item #10 / B3: stage-aware create modal (B1). Auto-selects
          the created project so the user lands straight in it instead of
          having to find it in the list themselves — same as ResearchTab's B2. */}
      {showCreateModal && (
        <CreateWorkspaceModal
          stage="test"
          onClose={(created) => {
            setShowCreateModal(false);
            if (created) {
              setActiveWsId(created.id);
              setMobileTestDrawerOpen(false); // land in the new project, not behind the drawer that made it
            }
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
    </>
  );

  // The actual assembler. Both shells call this exactly once, supplying
  // only the fragments that differ by viewport (project picker, icon
  // rail, sub-tab nav, promote control, empty state) — everything else
  // (the outer flex row, the title/promote header row, the scroll pane,
  // subTabContent, dockAndModals) is built here so neither shell can
  // silently drift out of sync with the other on the parts that were
  // never supposed to differ in the first place. Same idea as
  // PlanTab.jsx's/ResearchTab.jsx's renderRoot.
  function renderRoot({ projectPicker, iconRail, subTabNav, promoteControl, emptyState }) {
    return (
      <div className="flex h-full">
        {projectPicker}
        {iconRail}
        {/* CHANGED — min-w-0 (Plan's renderRoot already has it): without
            it a flex item won't shrink below its content's width, so one
            long unbroken string in a report could push this column wider
            than the screen instead of wrapping. */}
        <div className="flex-1 min-h-0 min-w-0 flex flex-col">
          {activeWs && (
            <div className="h-10 shrink-0 flex items-center justify-between px-3 border-b border-[var(--neutral-800)]">
              <h2 className="text-sm font-medium text-[var(--neutral-100)] truncate">{activeWs.name}</h2>
              <div className="flex items-center gap-2 shrink-0">{promoteControl}</div>
            </div>
          )}
          {promoteError && (
            <p className="text-xs text-red-400 px-3 pt-2 break-words">{promoteError}</p>
          )}
          {subTabNav}
          {!activeWs && emptyState ? (
            emptyState
          ) : (
            // CHANGED — mobile: p-3 (the shared --viewport-content-padding
            // value) instead of p-4, and pb-20 so the last card can scroll
            // clear of the floating chat bubble instead of hiding under it.
            <div className={`flex-1 min-h-0 overflow-y-auto relative ${isMobile ? "p-3 pb-20" : "p-4"}`}>
              {!activeWs ? (
                <p className="text-xs text-[var(--neutral-600)]">Pick or create a project to get started.</p>
              ) : (
                subTabContent
              )}
            </div>
          )}
        </div>
        {dockAndModals}
      </div>
    );
  }

  return {
    viewport, isMobile,
    activeWs, testProjects, projectRows,
    projectsCollapsed, toggleProjects,
    mobileTestDrawerOpen, setMobileTestDrawerOpen,
    showCreateModal, setShowCreateModal,
    subTab, setSubTab,
    promoteTargets, promoteTargetStage, setPromoteTargetStage, promoteMode, setPromoteMode, promoting, promoteError, handlePromote,
    renderRoot,
  };
}

// Desktop shell. Thin on purpose (same idea as PlanTabDesktop/
// ResearchTabDesktop/NotebooksTabDesktop) — takes the already-built
// controller (see the router at the bottom of this file, which calls the
// hook above exactly once) and supplies only the project-picker column,
// the labeled pill-row sub-tab nav, and the full 3-widget promote control
// — the things that only ever render on desktop/tablet. See
// components/mobile/TestTab.jsx for the counterpart, which takes the same
// controller shape. Markup here is moved verbatim from the old inline JSX.
function TestTabDesktop({ controller: c }) {
  const projectPicker = c.projectsCollapsed ? (
    <div className="w-10 shrink-0 border-r border-[var(--neutral-800)] flex flex-col items-center py-3 gap-3">
      <button onClick={c.toggleProjects} className="touch-target text-[var(--neutral-500)] hover:text-[var(--neutral-300)]" title="Show projects">
        <ChevronRight size={16} />
      </button>
    </div>
  ) : (
    <div className="w-56 shrink-0 border-r border-[var(--neutral-800)] flex flex-col">
      <div className="h-10 px-3 border-b border-[var(--neutral-800)] flex items-center justify-between">
        <span className="text-xs font-medium text-[var(--neutral-400)] flex items-center gap-1.5">
          <STAGE_THEME.test.Icon size={13} className={STAGE_THEME.test.color} /> Test projects
        </span>
        {/* NEW — item #10 / B3: native create, same stage-aware modal
            ResearchTab's B2 wired up first. */}
        <div className="flex items-center gap-2">
          <button
            onClick={() => c.setShowCreateModal(true)}
            title="New test project"
            className="touch-target text-[var(--neutral-500)] hover:text-[var(--neutral-200)]"
          >
            <Plus size={14} />
          </button>
          {/* NEW — collapsible sidebar, same affordance as ChatSidebar's
              own ChevronLeft toggle. */}
          <button onClick={c.toggleProjects} title="Hide projects" className="touch-target text-[var(--neutral-500)] hover:text-[var(--neutral-300)]">
            <ChevronLeft size={14} />
          </button>
        </div>
      </div>
      <div className="flex-1 overflow-y-auto">{c.projectRows}</div>
    </div>
  );

  const promoteControl = c.promoteTargets && (
    <>
      <label className="sr-only" htmlFor="test-promote-target">Promote to</label>
      <select
        id="test-promote-target"
        value={c.promoteTargets.targetStage}
        onChange={(e) => c.setPromoteTargetStage(e.target.value)}
        disabled={c.promoting}
        className="bg-[var(--neutral-900)] border border-[var(--neutral-700)] text-[var(--neutral-200)] rounded-lg px-2 py-1.5 text-xs outline-none disabled:opacity-50"
      >
        {c.promoteTargets.availableTargets.map((stage) => (
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
          aria-checked={c.promoteMode === "complete"}
          onClick={() => c.setPromoteMode("complete")}
          disabled={c.promoting}
          title="Move the project fully into the target stage"
          className={`px-2 py-1.5 font-medium disabled:opacity-50 ${
            c.promoteMode === "complete"
              ? "bg-[var(--accent)] text-[var(--accent-text)]"
              : "bg-[var(--neutral-900)] text-[var(--neutral-400)]"
          }`}
        >
          Complete
        </button>
        <button
          type="button"
          role="radio"
          aria-checked={c.promoteMode === "partial"}
          onClick={() => c.setPromoteMode("partial")}
          disabled={c.promoting}
          title="Keep the project active here too"
          className={`px-2 py-1.5 font-medium disabled:opacity-50 ${
            c.promoteMode === "partial"
              ? "bg-[var(--accent)] text-[var(--accent-text)]"
              : "bg-[var(--neutral-900)] text-[var(--neutral-400)]"
          }`}
        >
          Partial
        </button>
      </div>
      <button
        onClick={() => c.handlePromote(c.activeWs.id, c.promoteTargets.targetStage)}
        disabled={c.promoting}
        className="flex items-center gap-1.5 text-xs border border-[var(--neutral-700)] text-[var(--neutral-200)] rounded-lg px-3 py-1.5 font-medium disabled:opacity-50 shrink-0"
      >
        {c.promoting ? <Loader2 size={13} className="animate-spin" /> : <ArrowUpRight size={13} />}
        {c.promoteMode === "partial" ? "Add to" : "Promote to"} {PROMOTE_LABELS[c.promoteTargets.targetStage]} →
      </button>
    </>
  );

  const subTabNav = (
    <div className="h-10 flex items-center justify-center gap-1 px-3 border-b border-[var(--neutral-800)] overflow-x-auto">
      {SUB_TABS.map((t) => {
        const Icon = t.icon;
        return (
          <button
            key={t.id}
            onClick={() => c.setSubTab(t.id)}
            className={`flex items-center gap-1.5 text-xs rounded-lg px-2.5 py-1.5 whitespace-nowrap ${
              c.subTab === t.id
                ? "bg-[var(--accent)] text-[var(--accent-text)] font-medium"
                : "text-[var(--neutral-500)] hover:text-[var(--neutral-300)]"
            }`}
          >
            <Icon size={13} />
            {t.label}
          </button>
        );
      })}
    </div>
  );

  return c.renderRoot({ projectPicker, subTabNav, promoteControl });
}

// NEW — mobile retrofit: the real router AppShell.jsx's dynamic import
// actually renders, same shape as PlanTab.jsx's/ResearchTab.jsx's/
// NotebooksTab.jsx's own router (and how mobile/AppShell.jsx /
// mobile/ChatSidebar.jsx are picked at their own call sites) rather than
// branching on isMobile inline. The controller hook is called exactly
// once here — never independently by either shell — since it owns
// effects (localStorage sync, the OPEN_TAB_SIDEBAR_EVENT listener) that
// must not run twice per render.
function TestTab(props) {
  const controller = useTestTabController(props);
  if (controller.viewport === "mobile") return <MobileTestTab controller={controller} />;
  return <TestTabDesktop controller={controller} />;
}

// --- Run Simulation (§1.2 `run`) — live dispatch via openScopedSubChat,
// same pattern as ResearchTab's SourcesPanel/DatasetPanel, not a paste
// box: this genuinely triggers a real multi-worker run.
// Shared by every panel below. A form control under 16px makes iOS Safari
// zoom the whole page on focus (and not reliably zoom back out), so on
// mobile every <select>/<textarea> is text-base; desktop keeps text-xs.
// Icon-only buttons get a real hit area on mobile instead of a bare
// 12px glyph with p-1 (~20px), which is fine under a mouse and not under
// a thumb.
const MOBILE_ICON_BTN = "flex items-center justify-center w-10 h-10 rounded-lg";
// Text+icon "Refresh" buttons: -mr-2 lets the enlarged hit area reach into
// the pane's own padding, so the row keeps its resting height/alignment.
const MOBILE_REFRESH_BTN = "min-h-[var(--viewport-touch-target)] px-2 -mr-2";

function RunSimulationPanel({ wsId, openScopedSubChat, revealChat, onDispatched, isMobile }) {
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
      await revealChat(chatId);
    } finally {
      setDispatching(false);
    }
  }

  return (
    <div className="space-y-4">
      <div className="border border-[var(--neutral-800)] rounded-lg p-3 space-y-3">
        <div>
          <label htmlFor="test-sim-type" className={`${isMobile ? "text-xs" : "text-[11px]"} text-[var(--neutral-500)]`}>Simulation type</label>
          <select
            id="test-sim-type"
            name="testSimType"
            value={simType}
            onChange={(e) => setSimType(e.target.value)}
            className={`w-full mt-1 bg-black/30 border border-[var(--neutral-800)] rounded px-2 outline-none focus:border-[var(--cyber-cyan)] ${
              isMobile ? "py-2.5 text-base" : "py-1.5 text-xs"
            }`}
          >
            {SIMULATION_TYPES.map((s) => (
              <option key={s.id} value={s.id}>{s.label}</option>
            ))}
          </select>
        </div>

        <div>
          <label htmlFor="test-target" className={`${isMobile ? "text-xs" : "text-[11px]"} text-[var(--neutral-500)]`}>
            What&apos;s being tested
          </label>
          {/* CHANGED — mobile: a shorter placeholder (the full one wraps to
              4+ lines at 16px and gets clipped by the 3-row box) and one
              more row, since typing a feature description into a 3-line
              window on a phone means scrolling inside it constantly. */}
          <textarea
            id="test-target"
            name="testTarget"
            value={target}
            onChange={(e) => setTarget(e.target.value)}
            placeholder={
              isMobile
                ? "Describe the feature, pricing, or app — e.g. 'the new $12/mo Pro tier'"
                : "Describe the feature, pricing, PRD excerpt, or app being tested — e.g. 'the new $12/mo Pro tier with unlimited exports'"
            }
            rows={isMobile ? 4 : 3}
            className={`w-full mt-1 bg-black/30 border border-[var(--neutral-800)] rounded px-3 py-2 outline-none focus:border-[var(--cyber-cyan)] ${
              isMobile ? "text-base" : "text-xs"
            }`}
          />
          <p className={`${isMobile ? "text-[11px]" : "text-[10px]"} text-[var(--neutral-600)] mt-1`}>
            No auto-fill from the Build cycle&apos;s handoff summary yet — paste or describe it manually.
          </p>
        </div>

        {/* CHANGED — mobile: the whole row is the tap target (a bare 13px
            native checkbox is easy to miss), with a larger box. */}
        <label
          className={`flex items-center text-[var(--neutral-500)] cursor-pointer ${
            isMobile ? "gap-2.5 text-xs min-h-[var(--viewport-touch-target)]" : "gap-1.5 text-[11px]"
          }`}
        >
          <input
            type="checkbox"
            id="test-thorough"
            name="testThorough"
            checked={thorough}
            onChange={(e) => setThorough(e.target.checked)}
            className={isMobile ? "w-5 h-5 shrink-0" : ""}
          />
          Use more personas/workers for a thorough pass
        </label>

        <button
          onClick={run}
          disabled={dispatching || !target.trim()}
          className={`bg-[var(--accent)] text-[var(--accent-text)] rounded font-medium disabled:opacity-50 flex items-center gap-1.5 ${
            isMobile ? "w-full justify-center text-sm min-h-[var(--viewport-touch-target)]" : "text-xs px-3 py-2"
          }`}
        >
          {dispatching ? <Loader2 size={13} className="animate-spin" /> : <Sparkles size={13} />}
          {dispatching ? "Dispatching…" : "Run simulation"}
        </button>
      </div>
      {/* CHANGED — mobile: there is no "dock on the right" on a phone; the
          chat opens full-screen the moment a run starts. */}
      <p className={`${isMobile ? "text-xs" : "text-[11px]"} text-[var(--neutral-600)]`}>
        {isMobile ? (
          <>
            Runs the chosen persona set in this project&apos;s own chat, which opens as soon as you start —
            watch the parallel branches in its Working Panel, then check Friction Reports once it finishes.
          </>
        ) : (
          <>
            Runs the chosen persona set in this project&apos;s own chat — watch the parallel branches live
            in the dock on the right, then check the Friction Reports tab once it finishes.
          </>
        )}
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

function ReportsPanel({ wsId, sessionId, lastSessionId, fetchSimulationResults, isMobile }) {
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

  // BUGFIX: the Refresh button used to call fetchSimulationResults()
  // directly in its onClick, bypassing loading/error entirely — same
  // fetch, but `loading` never flipped true (so the RefreshCw icon
  // never actually spun on a manual refresh, unlike RedTeamPanel's own
  // load(), which this now mirrors) and a stale error from a previous
  // failed fetch was never cleared before retrying, so it could keep
  // showing under freshly-loaded data after a successful refresh.
  function refresh() {
    if (!sessionId) return;
    setLoading(true);
    setError(null);
    fetchSimulationResults(wsId, sessionId)
      .then(setData)
      .catch((e) => setError(e.message || "Failed to load results."))
      .finally(() => setLoading(false));
  }

  if (!sessionId) {
    return (
      <p className="text-xs text-[var(--neutral-600)]">
        No simulation run yet for this project — dispatch one from the Run Simulation tab first.
      </p>
    );
  }

  return (
    <div className="space-y-4">
      {/* CHANGED — gap-3 + shrink-0 on the button (PersonasPanel's own
          refresh row already had shrink-0): without them a wrapping
          caption squeezes the button on a narrow pane. */}
      <div className="flex items-center justify-between gap-3">
        <p className="text-[11px] text-[var(--neutral-600)]">
          {sessionId === lastSessionId
            ? "Showing the most recently dispatched run."
            : "Showing results for the selected run."}
        </p>
        <button
          onClick={refresh}
          disabled={loading}
          className={`text-xs text-[var(--neutral-500)] hover:text-[var(--neutral-200)] flex items-center gap-1 shrink-0 disabled:opacity-50 ${
            isMobile ? MOBILE_REFRESH_BTN : ""
          }`}
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
          {/* CHANGED — min-w-0 break-words here and on the other rendered
              markdown below: a long URL or unbroken token in a model's
              output has nowhere to wrap otherwise and pushes the whole
              pane sideways on a phone. Tables/code blocks already scroll
              inside Markdown.jsx itself. */}
          <div className="min-w-0 break-words"><Markdown>{data.synthesis}</Markdown></div>
        </div>
      )}

      {data?.personas?.length > 0 && (
        <div className="space-y-3">
          <p className="text-[11px] text-[var(--neutral-500)] uppercase tracking-wide">Individual reactions</p>
          {data.personas.map((p) => (
            <div key={p.role} className="border border-[var(--neutral-800)] rounded-lg p-3">
              <p className="text-xs font-medium text-[var(--neutral-100)] mb-1.5 break-words">
                {PERSONA_LABELS[p.role] || p.role}
              </p>
              {p.text && <div className="min-w-0 break-words"><Markdown>{p.text}</Markdown></div>}
              {p.reviews && (
                <div className="space-y-2 mt-1">
                  {p.reviews.map((r, i) => (
                    <div key={i} className="border border-[var(--neutral-900)] rounded p-2">
                      <div className="flex items-center gap-2 text-[10px] text-[var(--neutral-500)] mb-1">
                        <span>{"★".repeat(Math.max(0, Math.min(5, r.rating || 0)))}</span>
                        <span className="uppercase tracking-wide">{r.sentiment}</span>
                      </div>
                      <p className="text-[11px] text-[var(--neutral-300)] break-words">{r.text}</p>
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
function PersonasPanel({ fetchRoles, updateRolePrompt, setRolePinned, isMobile }) {
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
      <div className="flex items-center justify-between gap-3">
        <p className="text-[11px] text-[var(--neutral-600)]">
          The role briefs this tab&apos;s simulations hire from — same store as the Role Library panel,
          filtered to the simulate domain&apos;s own roles.
        </p>
        <button
          onClick={load}
          disabled={loading}
          className={`text-xs text-[var(--neutral-500)] hover:text-[var(--neutral-200)] flex items-center gap-1 shrink-0 disabled:opacity-50 ${
            isMobile ? MOBILE_REFRESH_BTN : ""
          }`}
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
              {/* CHANGED — was one non-wrapping row of [label + mono role
                  name] and [source · hired N× · pin · edit]. At phone width
                  (or on a desktop with the chat dock open, where this pane
                  is ~240px) the right cluster took ~165px of a ~250px card,
                  crushing the label into a 4-line sliver and letting the
                  unbroken role name (e.g. support_ticket_predictor) run
                  straight into the buttons. flex-wrap drops the cluster to
                  its own line only when there isn't room, so a wide pane
                  looks exactly as before; on mobile it always takes its own
                  full-width line, meta left / buttons right. */}
              <div className="flex flex-wrap items-start justify-between gap-x-2 gap-y-1.5">
                <div className="min-w-0 flex-1 basis-40">
                  <p className="text-xs font-medium text-[var(--neutral-100)] break-words">{label}</p>
                  <p className="text-[10px] text-[var(--neutral-600)] font-mono break-all">{role}</p>
                </div>
                <div className={`flex items-center gap-2 text-[10px] text-[var(--neutral-500)] ${isMobile ? "w-full justify-between" : "shrink-0"}`}>
                  <span className="flex items-center gap-2">
                    {entry && (
                      <>
                        <span className="uppercase tracking-wide">{entry.source || "seed"}</span>
                        <span>· hired {entry.times_hired || 0}×</span>
                      </>
                    )}
                  </span>
                  <span className="flex items-center gap-2 shrink-0">
                    <button
                      onClick={() => togglePin(role, pinned)}
                      disabled={pinningRole === role}
                      title={pinned ? "Unpin" : "Pin"}
                      aria-label={pinned ? "Unpin role" : "Pin role"}
                      className={`hover:bg-[var(--neutral-900)] ${isMobile ? MOBILE_ICON_BTN : "p-1 rounded"} ${pinned ? "text-[var(--cyber-amber)]" : "text-[var(--neutral-500)]"}`}
                    >
                      {pinningRole === role ? <Loader2 size={isMobile ? 16 : 12} className="animate-spin" /> : pinned ? <Pin size={isMobile ? 16 : 12} /> : <PinOff size={isMobile ? 16 : 12} />}
                    </button>
                    {!isEditing && (
                      <button
                        onClick={() => startEdit(role, entry?.brief)}
                        title="Edit brief"
                        aria-label="Edit brief"
                        className={`hover:bg-[var(--neutral-900)] text-[var(--neutral-500)] ${isMobile ? MOBILE_ICON_BTN : "p-1 rounded"}`}
                      >
                        <Pencil size={isMobile ? 16 : 12} />
                      </button>
                    )}
                  </span>
                </div>
              </div>

              {!entry && !isEditing && (
                <p className="text-[11px] text-[var(--neutral-600)] mt-1.5">
                  Not yet briefed — a first-hire cold-start brief will be written and saved here the
                  first time a simulation run hires this role.
                </p>
              )}

              {!isEditing && entry?.brief && (
                <p className="text-[11px] text-[var(--neutral-400)] mt-1.5 leading-relaxed break-words">{entry.brief}</p>
              )}

              {isEditing && (
                <div className="mt-2 space-y-2">
                  <textarea
                    id={`test-draft-brief-${role}`}
                    name={`test-draft-brief-${role}`}
                    value={draftBrief}
                    onChange={(e) => setDraftBrief(e.target.value)}
                    rows={isMobile ? 6 : 4}
                    className={`w-full bg-black/30 border border-[var(--neutral-800)] rounded px-2 py-1.5 outline-none focus:border-[var(--cyber-cyan)] ${
                      isMobile ? "text-base" : "text-[11px]"
                    }`}
                  />
                  <div className="flex items-center gap-2">
                    <button
                      onClick={() => saveEdit(role)}
                      disabled={savingRole === role || !draftBrief.trim()}
                      className={`bg-[var(--accent)] text-[var(--accent-text)] font-medium disabled:opacity-50 flex items-center justify-center gap-1 ${
                        isMobile ? "text-xs rounded-lg px-4 min-h-[var(--viewport-touch-target)]" : "text-[11px] rounded px-2 py-1"
                      }`}
                    >
                      {savingRole === role ? <Loader2 size={11} className="animate-spin" /> : <Check size={11} />} Save
                    </button>
                    <button
                      onClick={() => setEditingRole(null)}
                      className={`text-[var(--neutral-500)] hover:text-[var(--neutral-200)] flex items-center justify-center gap-1 ${
                        isMobile ? "text-xs px-3 min-h-[var(--viewport-touch-target)]" : "text-[11px]"
                      }`}
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
function RedTeamPanel({ wsId, sessionId, fetchSimulationResults, isMobile }) {
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
      <div className="flex items-center justify-between gap-3">
        <p className="text-[11px] text-[var(--neutral-600)]">Showing red_team&apos;s pass for the selected run.</p>
        <button
          onClick={load}
          disabled={loading}
          className={`text-xs text-[var(--neutral-500)] hover:text-[var(--neutral-200)] flex items-center gap-1 shrink-0 disabled:opacity-50 ${
            isMobile ? MOBILE_REFRESH_BTN : ""
          }`}
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
          <div className="min-w-0 break-words"><Markdown>{redTeamEntry.text}</Markdown></div>
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
function HistoryPanel({ runHistory, viewedSessionId, lastSessionId, onView, revealChat, isMobile }) {
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
            {/* CHANGED — same flex-wrap fix as PersonasPanel's card header:
                two side-by-side action buttons (~190px) beside the title
                left ~65px of a phone-width card, so the run's name wrapped
                to three lines and its target text truncated to a few
                characters. The buttons now drop to their own line when
                there isn't room (always, on mobile, where they also split
                the row evenly). On mobile the target also gets two lines
                instead of one truncated one — it's the only thing that
                tells two runs of the same type apart. */}
            <div className="flex flex-wrap items-start justify-between gap-x-2 gap-y-2.5">
              <div className="min-w-0 flex-1 basis-48">
                <p className="text-xs font-medium text-[var(--neutral-100)] break-words">
                  {run.simTypeLabel || "Simulation run"}
                  {isLast && <span className="ml-1.5 text-[10px] text-[var(--neutral-500)] font-normal">· most recent</span>}
                </p>
                {run.target && (
                  <p className={`text-[11px] text-[var(--neutral-500)] mt-0.5 ${isMobile ? "line-clamp-2 break-words" : "truncate"}`}>{run.target}</p>
                )}
                <p className="text-[10px] text-[var(--neutral-600)] flex items-center gap-1 mt-1">
                  <Clock size={10} />
                  {new Date(run.ts).toLocaleString()}
                </p>
              </div>
              <div className={`flex items-center gap-2 ${isMobile ? "w-full" : "shrink-0"}`}>
                <button
                  onClick={() => onView(run.chatId)}
                  disabled={isViewed}
                  className={`flex items-center gap-1 ${
                    isMobile ? "flex-1 justify-center text-xs rounded-lg min-h-[var(--viewport-touch-target)]" : "text-[11px] rounded px-2 py-1"
                  } ${
                    isViewed
                      ? `text-[var(--cyber-violet)] cursor-default ${isMobile ? "border border-[var(--cyber-violet)]/40" : ""}`
                      : "text-[var(--neutral-400)] hover:text-[var(--neutral-100)] border border-[var(--neutral-700)]"
                  }`}
                >
                  <Eye size={isMobile ? 13 : 11} /> {isViewed ? "Viewing" : "View report"}
                </button>
                <button
                  onClick={() => revealChat(run.chatId)}
                  className={`text-[var(--neutral-400)] hover:text-[var(--neutral-100)] border border-[var(--neutral-700)] flex items-center gap-1 ${
                    isMobile ? "flex-1 justify-center text-xs rounded-lg min-h-[var(--viewport-touch-target)]" : "text-[11px] rounded px-2 py-1"
                  }`}
                >
                  <MessageSquare size={isMobile ? 13 : 11} /> Open chat
                </button>
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

// Item 6 (perf audit, tab-body pass): TestTab takes props from its parent
// (initialWorkspaceId, onConsumeInitialWorkspaceId, onPromoted,
// onActiveWorkspaceChange). Wrapped in memo() now that SessionContext's
// useCallback pass (item 2) means its stable-identity props/callbacks stay
// stable across unrelated parent re-renders -- prop objects/arrays it reads
// (workspaces, chats, etc.) are only ever replaced, never mutated in place, so
// a shallow prop comparison here is meaningful.
export default memo(TestTab);
