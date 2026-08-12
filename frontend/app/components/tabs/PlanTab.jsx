"use client";
import { useState, useEffect, memo } from "react";
import { useSession } from "../../context/SessionContext";
import { useWorkspaces } from "../../context/WorkspacesContext";   // FIX — Item 2 concern split, slice 3 follow-up: this file was missed when workspaces/fetchWorkspaces moved out of useSession()
import { useChatList } from "../../context/ChatListContext";   // NEW — Item 2 concern split, slice 4
import MermaidDiagram from "../MermaidDiagram";
import WireframePreview from "../WireframePreview";
import Markdown from "../Markdown";
import ManageWorkspaceModal from "../ManageWorkspaceModal"; // NEW — parity fix: rename/delete kebab, same as NotebooksTab
import ConfirmDialog from "../ConfirmDialog"; // NEW — issue #3: same delete-confirmation affordance as ChatSidebar's own per-chat delete
import WorkspaceChatPanel from "../WorkspaceChatPanel";      // NEW — parity fix: embedded chat + WorkingPanel dock, same as Notebooks/Research
import { useWorkspaceDock, useWorkspaceDockActions, useLastActiveChatId } from "../../context/WorkspaceDockContext"; // NEW — step 3e; useLastActiveChatId added for C1 nested-chat row highlight
import CreateWorkspaceModal from "../CreateWorkspaceModal"; // NEW — item #10 / B3: native "create project" for this tab, same as ResearchTab's B2
import WorkspaceStageIcons, { STAGE_THEME } from "../WorkspaceStageIcons"; // NEW — item #2: colored per-stage icon + per-project stage badges
import { getPusherClient } from "../../lib/pusherClient"; // NEW — live-refetch fix (patch 3 follow-up): workspace-${id} panel-update subscription
import PartsTable from "../PartsTable";                       // NEW — Blueprint sub-tab
import WiringGraph from "../WiringGraph";                     // NEW — Blueprint sub-tab
import MechView from "../MechView";                           // NEW — Blueprint sub-tab
// InstructionChecklist import removed — patch 7 (T2/T3 Plan/Build split):
// the checklist relocated to BuildTab.jsx. See BuildTab.jsx for the
// equivalent import and render block.
import {
  FileText, GitBranch, Database, Webhook, Skull, Calculator,
  LayoutTemplate, Rocket, FolderOpen, MoreVertical, ArrowUpRight,
  Loader2, ChevronRight, ChevronLeft, MessageSquare, Cpu, Plus, Pencil, Check, X,
  Trash2, Sparkles,
} from "lucide-react";

// Part 5 — Plan as a dedicated top-level section, same shape as Notebooks
// (§4.7) and Research (§3.9): a project (= workspace, exactly like
// "notebook"/"research project" == workspace_id there) picker on the
// left, sub-tabs on the right.
//
// Unlike Notebooks/Research, NOTHING in this domain writes a Part 0
// knowledge-graph node — confirmed straight from agents/handoff_packager.py
// (§5.6): prd_writer/api_contract_writer/devils_advocate/
// feasibility_estimator are plain generic_worker roles living at
// stage_output:{session_id}:{role}, and architecture_diagrammer/
// schema_diagrammer write to their own bare bus keys
// (ARCHITECTURE_DIAGRAM_KEY/SCHEMA_DIAGRAM_KEY), never eo.knowledge_graph.
// write_node(). So there's no "browse past PRDs for this project" store —
// every artifact sub-tab below takes a paste of a completed chat run's
// output, same known-simplification-flagged-not-hidden pattern
// ResearchTab's ExtractionPanel/ContradictionsPanel already established.
//
// PARITY FIX (this pass): PlanTab previously showed every workspace
// regardless of stage, had no promote button (so nothing could ever
// reach Build), and had no chat dock or rename/delete kebab — the only
// stage tab out of step with Notebooks/Research/Tasks. Also removed a
// duplicate `StartBuildingPanel` declaration that shadowed the real one
// (the fuller, paste-parsing version below was previously dead code).
const SELECTED_PLAN_WS_KEY = "minime_plan_selected_ws_id";
const CHAT_DOCK_KEY = "minime_plan_chatdock_collapsed";
// NEW — collapsible project-picker sidebar, same pattern as the chat
// dock's own collapse above.
const PROJECTS_KEY = "minime_plan_projects_collapsed";
const PROMOTE_TARGETS = ["build", "test", "growth"];
const PROMOTE_LABELS = {
  build: "Build",
  test: "Test",
  growth: "Growth",
};

// --- Start Building (§5.6) — the one genuinely live panel in this
// domain. Auto-parses handoff_packager's own summary sentence
// (confirmed verbatim from eo/result_render.py: since handoff_packager's
// result has no "text"/"issues"/"fixed_code"/"code"/"answer"/"papers",
// it falls through to the summary branch, so this IS exactly what
// renders in chat). Manual fields stay as the fallback/override in case
// the sentence's exact wording ever drifts. Requires the
// SessionContext.jsx openScopedSubChat/sendTask appSlug patch — without
// it this silently falls back to today's un-scoped dispatch.
function StartBuildingPanel({ wsId, openScopedSubChat, onOpenChat }) {
  const [pasted, setPasted] = useState("");
  const [appSlug, setAppSlug] = useState("");
  const [cycleGoal, setCycleGoal] = useState("");
  const [starting, setStarting] = useState(false);

  function parsePasted(text) {
    setPasted(text);
    // Matches handoff_packager.py's exact f-string:
    // '...first cycle target: "{target_feature}"... app_slug "{app_slug}"...'
    const slugMatch = /app_slug "([^"]+)"/.exec(text);
    const targetMatch = /first cycle target: "([^"]+)"/.exec(text);
    if (slugMatch) setAppSlug(slugMatch[1]);
    if (targetMatch) setCycleGoal(`Implement ${targetMatch[1]} as scoped in the PRD's first cycle.`);
  }

  async function start() {
    if (!appSlug.trim() || !cycleGoal.trim()) return;
    setStarting(true);
    try {
      const chatId = await openScopedSubChat(wsId, cycleGoal.trim(), appSlug.trim());
      onOpenChat?.(chatId);
    } finally {
      setStarting(false);
    }
  }

  return (
    <div className="space-y-4 max-w-lg">
      <div>
        <label htmlFor="plan-handoff-pasted" className="text-[10px] uppercase tracking-wide text-[var(--neutral-500)]">
          Paste handoff_packager's chat response (optional — auto-fills the fields below)
        </label>
        <textarea
          id="plan-handoff-pasted"
          name="planHandoffPasted"
          value={pasted}
          onChange={(e) => parsePasted(e.target.value)}
          placeholder='Handoff ready for "..." — 4 feature(s), first cycle target: "Auth". Scoped to app_slug "my-app_ab12cd34"...'
          rows={2}
          className="w-full mt-1 bg-black/30 border border-[var(--neutral-800)] rounded px-3 py-2 text-xs outline-none focus:border-[var(--cyber-amber)] font-mono"
        />
      </div>
      <div>
        <label htmlFor="plan-app-slug" className="text-[10px] uppercase tracking-wide text-[var(--neutral-500)]">App slug</label>
        <input
          id="plan-app-slug"
          value={appSlug}
          onChange={(e) => setAppSlug(e.target.value)}
          placeholder="my-app_ab12cd34"
          className="w-full mt-1 bg-black/30 border border-[var(--neutral-800)] rounded px-3 py-2 text-xs outline-none focus:border-[var(--cyber-amber)] font-mono"
        />
      </div>
      <div>
        <label htmlFor="plan-cycle-goal" className="text-[10px] uppercase tracking-wide text-[var(--neutral-500)]">First task / cycle goal</label>
        <textarea
          id="plan-cycle-goal"
          name="planCycleGoal"
          value={cycleGoal}
          onChange={(e) => setCycleGoal(e.target.value)}
          placeholder="Implement Auth as scoped in the PRD's first cycle."
          rows={3}
          className="w-full mt-1 bg-black/30 border border-[var(--neutral-800)] rounded px-3 py-2 text-xs outline-none focus:border-[var(--cyber-amber)]"
        />
      </div>
      <button
        onClick={start}
        disabled={starting || !appSlug.trim() || !cycleGoal.trim()}
        className="flex items-center gap-1.5 text-xs bg-[var(--cyber-amber)] text-black rounded px-3 py-2 font-medium disabled:opacity-50"
      >
        <Rocket size={13} /> {starting ? "Starting…" : "Start building this"}
      </button>
    </div>
  );
}

const SUB_TABS = [
  { id: "prd", label: "PRD", icon: FileText },
  { id: "architecture", label: "Architecture", icon: GitBranch },
  { id: "schema", label: "Schema", icon: Database },
  { id: "api_contract", label: "API Contract", icon: Webhook },
  { id: "devils_advocate", label: "Devil's Advocate", icon: Skull },
  { id: "feasibility", label: "Feasibility", icon: Calculator },
  { id: "wireframes", label: "Wireframes", icon: LayoutTemplate },
  { id: "blueprint", label: "Blueprint", icon: Cpu },
  { id: "start_building", label: "Start Building", icon: Rocket },
];

// Strips an optional ```mermaid fenced code block wrapper so a raw paste
// of either the bare diagram or the fenced chat-rendered form both work.
function unfenceMermaid(text) {
  const m = /```(?:mermaid)?\s*\n?([\s\S]*?)```/.exec(text || "");
  return (m ? m[1] : text || "").trim();
}

function PlanTab({ onOpenChat, initialWorkspaceId, onConsumeInitialWorkspaceId, onPromoted, onActiveWorkspaceChange }) {
  const { promoteWorkspace, openScopedSubChat,
    fetchPanelContent, savePanelContent,
    // toggleInstructionStep removed — patch 7: only BlueprintView's
    // Instructions view used it, and that view moved to BuildTab.jsx.
    fetchDeviceSpec, refreshPartPrices } = useSession();
  const { workspaces, fetchWorkspaces } = useWorkspaces();   // FIX — was destructured off useSession(), which no longer serves it; this call site was missed at the time
  const { chats } = useChatList();   // CHANGED — Item 2 concern split, slice 4: was useSession()
  // NEW — step 3e follow-up fix: the embedded WorkspaceChatPanel below was
  // NOT actually dock-driven despite the old comment here claiming so —
  // it had no workspaceId prop, so it read messages/sessionId off
  // useSession() (legacy/global) while switchChat here (dock-based) wrote
  // into a dock slot the visible panel never read. Fixed below by passing
  // workspaceId={activeWs?.id} to the panel (now the same key switchChat
  // already resolves to, and the same key `dock` below already uses for
  // WireframesPanel).
  const { switchChat, renameChat, deleteChat, createWorkspaceChat } = useWorkspaceDockActions();
  // NEW — item #11 / C1: same row-highlight source ChatSidebar's nested
  // chat rows use, so a chat opened from here highlights consistently
  // whether it was opened from the global sidebar or from in-tab.
  const activeChatId = useLastActiveChatId();

  // PARITY FIX — Plan only shows plan-stage workspaces now, same as every
  // other stage tab; a research project promoted from Research lands
  // here, not floating in every tab regardless of stage.
  const planProjects = workspaces.filter((w) => (w.active_stages || [w.stage]).includes("plan"));

  const [activeWsId, setActiveWsId] = useState(null);
  const [subTab, setSubTab] = useState("prd");
  const [restoredSelection, setRestoredSelection] = useState(false);
  // PARITY FIX — promote-to-Build busy/error state, same shape as
  // NotebooksTab's promote-to-Research and TasksTab's promote-to-Test.
  const [promoting, setPromoting] = useState(false);
  const [promoteError, setPromoteError] = useState(null);
  const [promoteTargetStage, setPromoteTargetStage] = useState("build");
  // NEW — §2.6 step 4: "complete" (existing behavior, leaves this tab)
  // vs "partial" (stays active here too, per §2.1/§2.2). Same toggle as
  // NotebooksTab/ResearchTab.
  const [promoteMode, setPromoteMode] = useState("complete");
  // PARITY FIX — right-hand chat dock collapse state, same pattern as
  // Notebooks/Research (own independent localStorage key).
  const [chatDockCollapsed, setChatDockCollapsed] = useState(false);
  const [projectsCollapsed, setProjectsCollapsed] = useState(false); // NEW — collapsible project-picker sidebar
  // PARITY FIX — which plan project's kebab menu (rename/delete/members)
  // is open. ManageWorkspaceModal already exists fully built, just never
  // wired into this tab.
  const [managingWorkspace, setManagingWorkspace] = useState(null);
  // NEW — item #10 / B3: native "create project" trigger, same pattern
  // as ResearchTab's B2. This tab can now create its own plan-stage
  // workspace directly, instead of requiring a promotion from Research
  // or the chat sidebar's folder button — those remain valid paths in,
  // this is just no longer the only one.
  const [showCreateModal, setShowCreateModal] = useState(false);
  // NEW — issue #3: nested-chat create/rename/delete state, same shape as
  // ResearchTab's/NotebooksTab's own.
  const [creatingChatForWs, setCreatingChatForWs] = useState(null);
  const [editingChatId, setEditingChatId] = useState(null);
  const [editChatTitle, setEditChatTitle] = useState("");
  const [pendingDeleteChat, setPendingDeleteChat] = useState(null);

  useEffect(() => {
    setChatDockCollapsed(localStorage.getItem(CHAT_DOCK_KEY) === "1");
    setProjectsCollapsed(localStorage.getItem(PROJECTS_KEY) === "1"); // NEW — collapsible project sidebar
  }, []);

  function toggleChatDock() {
    setChatDockCollapsed((prev) => {
      localStorage.setItem(CHAT_DOCK_KEY, !prev ? "1" : "0");
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

  // Restore last-selected plan project on mount, same pattern as
  // TasksTab's SELECTED_BUILD_WS_KEY restore effect.
  useEffect(() => {
    const savedId = localStorage.getItem(SELECTED_PLAN_WS_KEY);
    if (savedId) setActiveWsId(savedId);
    setRestoredSelection(true);
  }, []);

  useEffect(() => {
    if (!restoredSelection || !activeWsId) return;
    localStorage.setItem(SELECTED_PLAN_WS_KEY, activeWsId);
  }, [activeWsId, restoredSelection]);

  // A promote-and-navigate hand-off from Research (via AppShell)
  // pre-selects the just-promoted workspace, then clears itself so it
  // doesn't re-fire on a later unrelated tab switch — same shape as
  // ResearchTab's own initialWorkspaceId consumption.
  useEffect(() => {
    if (initialWorkspaceId) {
      setActiveWsId(initialWorkspaceId);
      onConsumeInitialWorkspaceId?.();
    }
  }, [initialWorkspaceId, onConsumeInitialWorkspaceId]);

  // Auto-select the first plan project once loaded, or recover if a
  // previously-saved selection was promoted onward / deleted.
  useEffect(() => {
    if (!restoredSelection || planProjects.length === 0) return;
    const stillExists = activeWsId && planProjects.some((w) => w.id === activeWsId);
    if (!stillExists) setActiveWsId(planProjects[0].id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [planProjects, activeWsId, restoredSelection]);

  const activeWs = planProjects.find((w) => w.id === activeWsId) || null;

  // NEW — item #1: the Data bubble now lives in AppShell's top nav, not
  // floating over this tab's own content, so this just reports which
  // project (if any) is active instead of rendering the bubble itself.
  useEffect(() => {
    onActiveWorkspaceChange?.(activeWs?.id || null, activeWs?.name);
  }, [activeWs?.id, activeWs?.name, onActiveWorkspaceChange]);

  // NEW — step 3e: WireframesPanel's "re-send edit into whichever chat is
  // currently open" only makes sense scoped to activeWs's own dock now —
  // WorkspaceChatPanel below is already reading/writing that same dock
  // (step 3d), so this keeps both in sync instead of one reading the
  // dock and the other reading a legacy sessionId nothing updates anymore.
  const dock = useWorkspaceDock(activeWs?.id);

  // NEW — patch 3 (chat-to-panel writes): api/task_runner.py's
  // _write_plan_panels() (patch 2) writes a matching role's output
  // straight into eo/panel_content.py as a side effect of a tier-3 chat
  // turn completing — but the six panels below only ever fetched their
  // saved content on mount, so that write sat in the database unseen
  // until the person happened to reload. Counting assistant turns (not
  // dock.state.messages.length itself) means a panel's fetch effect
  // re-fires once a run actually finishes, not the instant the user's
  // own message is optimistically appended to `messages` — see
  // WorkspaceDockContext.jsx's finishRun()/buildAssistantMessage() for
  // where the "assistant" entry actually lands, well after dispatch.
  // Cheap to recompute every render (dock.state.messages is only ever
  // replaced, never mutated in place, and is bounded to one chat's
  // history) and correct across a chat switch too — switchChat() reloads
  // `messages` for the newly-active chat, which is exactly when a panel
  // SHOULD re-check for newer saved content anyway.
  const assistantTurnSignal = dock.state.messages.filter((m) => m.role === "assistant").length;

  // NEW — live-refetch fix (patch 3 follow-up): assistantTurnSignal
  // above only advances for the dock/tab that actually dispatched the
  // run. api/task_runner.py's _write_plan_panels() now fires
  // PANEL_CONTENT_UPDATED on the workspace's own Pusher channel (not
  // the per-session channel) after each successful panel write, so a
  // panel write started from one chat/tab/dock is seen by every other
  // one that has this same workspace open too — e.g. a second browser
  // tab, or a different sub-tab's chat dock, sitting on the same
  // project. Subscribes only while a workspace is actually active;
  // re-subscribes on workspace switch, same lifecycle as `dock` above.
  const [wsPanelPushSignal, setWsPanelPushSignal] = useState(0);
  useEffect(() => {
    if (!activeWs?.id) return undefined;
    const pusher = getPusherClient();
    if (!pusher) return undefined; // Pusher env vars not set — live refresh disabled, falls back to per-mount fetch

    const channelName = `workspace-${activeWs.id.replace(/[^A-Za-z0-9_=@,.;-]/g, "-")}`;
    const channel = pusher.subscribe(channelName);
    const handler = (eventType) => {
      if (eventType !== "panel_content_updated") return;
      setWsPanelPushSignal((n) => n + 1);
    };
    channel.bind_global(handler);

    return () => {
      channel.unbind_global(handler);
      pusher.unsubscribe(channelName);
    };
  }, [activeWs?.id]);

  // NEW — patch 3 (chat-to-panel writes) + live-refetch fix (patch 3
  // follow-up): combines the local assistant-turn counter (fires for
  // the dock/tab/chat that actually dispatched the run, the instant its
  // own run finishes — no Pusher round-trip needed) with the workspace
  // push counter above (fires for every OTHER dock/tab/chat watching
  // this same workspace, via the PANEL_CONTENT_UPDATED event). Either
  // one advancing is a real, independent reason for the six panels
  // below to re-fetch, so a simple sum is enough — the six panel
  // components below only care that this number changed since their
  // last fetch, never by how much or which source moved it.
  const planPanelRefreshSignal = assistantTurnSignal + wsPanelPushSignal;

  // FIX — sub-tabs were a ternary chain (conditional render), which
  // unmounts whichever sub-tab you leave and destroys its local state
  // (a paste-box's contents, wireframe edits, an in-progress
  // Start Building form). Same "stays mounted, hidden via CSS"
  // technique AppShell.jsx already uses for top-level tabs, applied
  // one level down for this tab's own sub-tabs — same fix as ResearchTab.
  const [visitedSubTabs, setVisitedSubTabs] = useState(() => new Set([subTab]));
  useEffect(() => {
    setVisitedSubTabs((prev) => (prev.has(subTab) ? prev : new Set(prev).add(subTab)));
  }, [subTab]);

  // PARITY FIX — promotes the plan project to Build and hands off
  // navigation to AppShell, same onPromoted(nextStage, wsId) contract
  // NotebooksTab/ResearchTab/TasksTab already use.
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
      {/* Project picker — a "plan project" is just a workspace, same as a
          "notebook"/"research project" is. No new container concept. */}
      {projectsCollapsed ? (
        <div className="w-10 shrink-0 border-r border-[var(--neutral-800)] flex flex-col items-center py-3 gap-3">
          <button onClick={toggleProjects} className="text-[var(--neutral-500)] hover:text-[var(--neutral-300)]" title="Show projects">
            <ChevronRight size={16} />
          </button>
        </div>
      ) : (
      <div className="w-56 shrink-0 border-r border-[var(--neutral-800)] flex flex-col">
        <div className="px-3 py-3 border-b border-[var(--neutral-800)] flex items-center justify-between">
          <span className="text-xs font-medium text-[var(--neutral-400)] flex items-center gap-1.5">
            <STAGE_THEME.plan.Icon size={13} className={STAGE_THEME.plan.color} /> Plan projects
          </span>
          {/* NEW — item #10 / B3: native create, same stage-aware modal
              ResearchTab's B2 wired up first. */}
          <div className="flex items-center gap-2">
            <button
              onClick={() => setShowCreateModal(true)}
              title="New plan project"
              className="text-[var(--neutral-500)] hover:text-[var(--neutral-200)]"
            >
              <Plus size={14} />
            </button>
            {/* NEW — collapsible sidebar, same affordance as ChatSidebar's
                own ChevronLeft toggle. */}
            <button onClick={toggleProjects} title="Hide projects" className="text-[var(--neutral-500)] hover:text-[var(--neutral-300)]">
              <ChevronLeft size={14} />
            </button>
          </div>
        </div>
        <div className="flex-1 overflow-y-auto">
          {planProjects.length === 0 && (
            <p className="px-3 py-3 text-xs text-[var(--neutral-600)]">
              No plan projects yet — create one above, promote a research project from the Research tab, or use the chat sidebar's <FolderOpen size={11} className="inline" /> button.
            </p>
          )}
          {planProjects.map((ws) => {
            // NEW — item #11 / C1: nested chat list, mirrors ChatSidebar's
            // memberChats pattern. Unlike ChatSidebar (a flat, always-
            // expanded list across every workspace), this tab already has
            // a single-selection model — one project active at a time —
            // so "expand" here just means "is the active project" (the
            // existing ChevronRight already marked that row as selected;
            // now it also means "expanded"). Selecting a different
            // project collapses the previous one's chat list the same way
            // it already swaps the whole right-hand panel.
            const isActive = ws.id === activeWsId;
            const memberChats = isActive ? chats.filter((c) => ws.chat_ids.includes(c.id)) : [];
            return (
              <div key={ws.id} className="border-b border-[var(--neutral-900)]">
                <div
                  className={`group flex items-center gap-1 ${
                    isActive
                      ? "bg-[var(--neutral-800-a70)] text-[var(--neutral-100)]"
                      : "text-[var(--neutral-300)] hover:bg-[var(--neutral-900)]"
                  }`}
                >
                  <button
                    onClick={() => setActiveWsId(ws.id)}
                    className="flex-1 min-w-0 flex items-center justify-between gap-1 px-3 py-2 text-left text-xs"
                  >
                    <span className="flex items-center min-w-0">
                      <WorkspaceStageIcons workspace={ws} />
                      <span className="truncate">{ws.name}</span>
                    </span>
                    {isActive && <ChevronRight size={12} className="text-[var(--neutral-500)] shrink-0" />}
                  </button>
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
                  <button
                    onClick={() => setManagingWorkspace(ws)}
                    title="Rename or delete project"
                    className="shrink-0 pr-2 text-[var(--neutral-600)] opacity-0 group-hover:opacity-100 hover:text-[var(--neutral-200)]"
                  >
                    <MoreVertical size={13} />
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
                          id={`chat-title-${chat.id}`}
                          name="chatTitle"
                          aria-label="Chat title"
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
      )}

      <div className="flex-1 min-h-0 flex flex-col">
        {/* PARITY FIX — title + promote row, same shape as Notebooks/Tasks —
            this was missing entirely, so a plan project had no path
            forward to Build. */}
        {activeWs && (
          <div className="flex items-center justify-between px-3 py-2 border-b border-[var(--neutral-800)]">
            <h2 className="text-sm font-medium text-[var(--neutral-100)] truncate">{activeWs.name}</h2>
            <div className="flex items-center gap-2 shrink-0">
              {(() => {
                // NEW — §2.2: exclude stages already active for this
                // workspace — same rule as Notebooks/Research.
                const activeHere = activeWs.active_stages || [activeWs.stage];
                const availableTargets = PROMOTE_TARGETS.filter((s) => !activeHere.includes(s));
                const targetStage = availableTargets.includes(promoteTargetStage)
                  ? promoteTargetStage
                  : availableTargets[0];
                if (!availableTargets.length) return null;
                return (
                  <>
                    <label className="sr-only" htmlFor="plan-promote-target">Promote to</label>
                    <select
                      id="plan-promote-target"
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

        <div className="flex-1 min-h-0 overflow-y-auto p-4 relative">
          {!activeWs ? (
            <p className="text-xs text-[var(--neutral-600)]">Pick or create a project to get started.</p>
          ) : (
            SUB_TABS.filter((t) => visitedSubTabs.has(t.id)).map((t) => (
              <div key={t.id} style={{ display: subTab === t.id ? "contents" : "none" }}>
                {t.id === "prd" && (
                  <MarkdownPastePanel
                    workspaceId={activeWs.id}
                    panelKey="prd"
                    fetchPanelContent={fetchPanelContent}
                    savePanelContent={savePanelContent}
                    refreshSignal={planPanelRefreshSignal}
                    placeholder="prd_writer's output lands here automatically once it runs in this project's chat."
                    paste_hint="Includes a Features/Priorities/First-cycle-scope section, per prd_writer's brief. You can also paste or edit it manually below."
                  />
                )}
                {t.id === "architecture" && (
                  <DiagramPastePanel
                    workspaceId={activeWs.id}
                    panelKey="architecture"
                    fetchPanelContent={fetchPanelContent}
                    savePanelContent={savePanelContent}
                    refreshSignal={planPanelRefreshSignal}
                    roleLabel="architecture_diagrammer"
                  />
                )}
                {t.id === "schema" && (
                  <DiagramPastePanel
                    workspaceId={activeWs.id}
                    panelKey="schema"
                    fetchPanelContent={fetchPanelContent}
                    savePanelContent={savePanelContent}
                    refreshSignal={planPanelRefreshSignal}
                    roleLabel="schema_diagrammer"
                  />
                )}
                {t.id === "api_contract" && (
                  <MarkdownPastePanel
                    workspaceId={activeWs.id}
                    panelKey="api_contract"
                    fetchPanelContent={fetchPanelContent}
                    savePanelContent={savePanelContent}
                    refreshSignal={planPanelRefreshSignal}
                    placeholder="api_contract_writer's endpoint table lands here automatically once it runs in this project's chat."
                  />
                )}
                {t.id === "devils_advocate" && (
                  <MarkdownPastePanel
                    workspaceId={activeWs.id}
                    panelKey="devils_advocate"
                    fetchPanelContent={fetchPanelContent}
                    savePanelContent={savePanelContent}
                    refreshSignal={planPanelRefreshSignal}
                    placeholder="devils_advocate's critique lands here automatically once it runs in this project's chat."
                  />
                )}
                {t.id === "feasibility" && (
                  <MarkdownPastePanel
                    workspaceId={activeWs.id}
                    panelKey="feasibility"
                    fetchPanelContent={fetchPanelContent}
                    savePanelContent={savePanelContent}
                    refreshSignal={planPanelRefreshSignal}
                    placeholder="feasibility_estimator's output lands here automatically once it runs in this project's chat."
                    estimateBanner="Rough complexity signal — not a time/cost estimate (Part 5 §5.4)"
                  />
                )}
                {t.id === "wireframes" && (
                  <WireframesPanel
                    workspaceId={activeWs.id}
                    fetchPanelContent={fetchPanelContent}
                    savePanelContent={savePanelContent}
                    sessionId={dock.state.sessionId}
                    sendTask={dock.sendTask}
                  />
                )}
                {t.id === "blueprint" && (
                  <BlueprintView
                    workspaceId={activeWs.id}
                    fetchDeviceSpec={fetchDeviceSpec}
                    refreshPartPrices={refreshPartPrices}
                    refreshSignal={planPanelRefreshSignal}
                  />
                )}
                {t.id === "start_building" && (
                  <StartBuildingPanel
                    wsId={activeWs.id}
                    openScopedSubChat={openScopedSubChat}
                    onOpenChat={onOpenChat}
                  />
                )}
              </div>
            ))
          )}
        </div>
      </div>

      {/* PARITY FIX — desktop dock, side-by-side, lg+, same as
          Notebooks/Research. Step 3e follow-up fix: workspaceId prop
          added below so this actually resolves the ws:${activeWs.id}
          dock slot (previously bare, silently left on the legacy global
          sessionId — same gap Research/Build/Test all had). */}
      <div className="hidden lg:flex shrink-0 border-l border-[var(--neutral-800)]" style={{ width: chatDockCollapsed ? undefined : 560 }}>
        <WorkspaceChatPanel collapsed={chatDockCollapsed} onToggleCollapse={toggleChatDock} workspaceId={activeWs?.id} stacked />
      </div>
      {!chatDockCollapsed && (
        <div className="lg:hidden fixed inset-0 z-40 bg-[var(--neutral-950)]">
          <WorkspaceChatPanel collapsed={false} onToggleCollapse={toggleChatDock} workspaceId={activeWs?.id} stacked />
        </div>
      )}
      {chatDockCollapsed && (
        <button
          onClick={toggleChatDock}
          title="Open chat"
          className="lg:hidden fixed bottom-4 right-4 z-40 bg-[var(--cyber-amber)] text-black rounded-full p-3 shadow-lg"
        >
          <MessageSquare size={18} />
        </button>
      )}

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
          stage="plan"
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

// NEW — patch 4 (frontend): distinguishes "chat auto-filled this panel"
// from "a person typed/edited this panel," per eo/panel_content.py's
// content_source column (migration 0005, patch 4 backend). contentSource
// is null for a panel with nothing saved yet (fetchPanelContent's own
// empty-content shape) — renders nothing in that case, since an empty
// panel has no source to label. Shared across MarkdownPastePanel and
// DiagramPastePanel below rather than duplicated per-component, same as
// unfenceMermaid() just above.
function PanelSourceBadge({ contentSource, updatedAt }) {
  if (!contentSource) return null;
  const isChat = contentSource === "chat";
  const label = isChat ? "Auto-filled from chat" : "Manually edited";
  const title = updatedAt ? new Date(updatedAt).toLocaleString() : undefined;
  return (
    <span
      title={title}
      className={
        "inline-flex items-center gap-1 text-[10px] px-2 py-0.5 rounded-full border " +
        (isChat
          ? "border-[var(--cyber-amber)]/40 bg-[var(--cyber-amber)]/10 text-[var(--cyber-amber)]"
          : "border-[var(--neutral-700)] bg-[var(--neutral-900)]/60 text-[var(--neutral-500)]")
      }
    >
      {isChat && <Sparkles size={10} />}
      {label}
    </span>
  );
}

// --- Shared paste-pattern panel for PRD / API Contract / Devil's
// Advocate / Feasibility — all plain generic_worker roles with no
// per-run history store, same textarea-then-Markdown shape ResearchTab's
// ContradictionsPanel already established. `estimateBanner`, when
// given, renders the same amber "AI-estimated" callout ContradictionsPanel
// uses for consensus_meter (§3.8's labeling discipline, applied here per
// §5.4's identical requirement for feasibility_estimator).
//
// FIX — this now persists through eo/panel_content.py under `panelKey`
// (one of "prd"/"api_contract"/"devils_advocate"/"feasibility"), keyed
// per workspaceId. Previously this was pure local state: since the
// panel stays mounted across sub-tab switches AND across switching
// which plan project is active, a paste would silently keep showing on
// screen even after switching to a *different* project — this fetches
// fresh content on every workspaceId change instead.
//
// PATCH 3 (chat-to-panel writes): `refreshSignal` is PlanTab's
// planPanelRefreshSignal — an assistant-turn counter for this project's
// chat dock. Previously this effect only ever ran on mount (well, on
// workspaceId/panelKey change), so api/task_runner.py's now-automatic
// write-back (patch 2) sat unseen until a manual reload. Adding
// refreshSignal to the dependency array makes it re-fetch every time a
// chat turn finishes, live, the same "stays mounted, re-fetch on a
// changing key" idea the workspaceId dependency already established —
// this is just a second kind of "the saved content might have changed
// out from under us" event, not a new mechanism.
function MarkdownPastePanel({ workspaceId, panelKey, fetchPanelContent, savePanelContent, refreshSignal, placeholder, paste_hint, estimateBanner }) {
  const [raw, setRaw] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState(null);
  // NEW — patch 4 (frontend): mirrors eo/panel_content.py's content_source
  // column (migration 0005) so PanelSourceBadge below can render it.
  const [contentSource, setContentSource] = useState(null);
  const [contentUpdatedAt, setContentUpdatedAt] = useState(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setSavedAt(null);
    fetchPanelContent(workspaceId, panelKey).then((saved) => {
      if (cancelled) return;
      setRaw(saved?.content || "");
      setContentSource(saved?.content_source || null);
      setContentUpdatedAt(saved?.updated_at || null);
      setLoading(false);
    });
    return () => { cancelled = true; };
  }, [workspaceId, panelKey, fetchPanelContent, refreshSignal]);

  async function handleSave() {
    setSaving(true);
    // Optimistic — api/routes/workspace_data.py's put_workspace_panel_content
    // always writes content_source="manual" (eo/panel_content.py's
    // set_content() default), so this is never actually wrong; flipping it
    // immediately means the badge doesn't lag a full round trip behind the
    // Save button's own "Saving…" state.
    setContentSource("manual");
    try {
      const saved = await savePanelContent(workspaceId, panelKey, raw);
      setSavedAt(Date.now());
      setContentUpdatedAt(saved?.updated_at || new Date().toISOString());
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
        {placeholder} Saved per project — the box below updates live when a matching chat
        run finishes, or you can paste or edit it here yourself; either way, saving here
        overwrites whatever was there before, same as Research's Extraction Table/
        Contradictions tabs.
        {paste_hint && <> {paste_hint}</>}
      </p>
      <textarea
        id="plan-markdown-paste-raw"
        name="planMarkdownPasteRaw"
        value={raw}
        onChange={(e) => setRaw(e.target.value)}
        placeholder="Filled automatically once the matching role runs — or paste/edit it here yourself…"
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
        <PanelSourceBadge contentSource={contentSource} updatedAt={contentUpdatedAt} />
      </div>
      {raw.trim() && (
        <div className={estimateBanner ? "border border-[var(--cyber-amber)]/40 bg-[var(--cyber-amber)]/5 rounded-lg p-3" : ""}>
          {estimateBanner && (
            <p className="text-[10px] uppercase tracking-wide text-[var(--cyber-amber)] mb-2">
              {estimateBanner}
            </p>
          )}
          <Markdown>{raw}</Markdown>
        </div>
      )}
    </div>
  );
}

// --- Architecture / Schema — same paste pattern, rendered via the
// existing MermaidDiagram.jsx instead of Markdown. Accepts either a raw
// mermaid string or a ```mermaid fenced block (unfenceMermaid strips the
// fence if present), since it's not certain which form eo/result_render.py
// renders these two roles' {"mermaid": "..."} bus-key output as in chat.
// FIX — persists via eo/panel_content.py under panelKey ("architecture"
// or "schema"), same reasoning as MarkdownPastePanel above.
//
// PATCH 3 (chat-to-panel writes): `refreshSignal` — see
// MarkdownPastePanel's own comment just above for what this is and why
// it's in the fetch effect's dependency array.
function DiagramPastePanel({ workspaceId, panelKey, fetchPanelContent, savePanelContent, refreshSignal, roleLabel }) {
  const [raw, setRaw] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState(null);
  // NEW — patch 4 (frontend): same content_source wiring as
  // MarkdownPastePanel above — see that component's own comments for
  // the reasoning, identical here.
  const [contentSource, setContentSource] = useState(null);
  const [contentUpdatedAt, setContentUpdatedAt] = useState(null);
  const mermaidText = unfenceMermaid(raw);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setSavedAt(null);
    fetchPanelContent(workspaceId, panelKey).then((saved) => {
      if (cancelled) return;
      setRaw(saved?.content || "");
      setContentSource(saved?.content_source || null);
      setContentUpdatedAt(saved?.updated_at || null);
      setLoading(false);
    });
    return () => { cancelled = true; };
  }, [workspaceId, panelKey, fetchPanelContent, refreshSignal]);

  async function handleSave() {
    setSaving(true);
    setContentSource("manual"); // optimistic — see MarkdownPastePanel's handleSave for why this is always correct
    try {
      const saved = await savePanelContent(workspaceId, panelKey, raw);
      setSavedAt(Date.now());
      setContentUpdatedAt(saved?.updated_at || new Date().toISOString());
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
        {roleLabel}'s diagram lands here automatically once it runs in this project's chat.
        You can also paste it yourself below — either the raw Mermaid syntax or a fenced
        <code className="mx-1 text-[var(--neutral-400)]">```mermaid</code> block copied from a chat message — or edit it after the fact.
      </p>
      <textarea
        id="plan-diagram-paste-raw"
        name="planDiagramPasteRaw"
        value={raw}
        onChange={(e) => setRaw(e.target.value)}
        placeholder={"Filled automatically once " + roleLabel + " runs — or paste/edit Mermaid syntax here yourself…"}
        rows={6}
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
        <PanelSourceBadge contentSource={contentSource} updatedAt={contentUpdatedAt} />
      </div>
      {mermaidText && (
        <div className="border border-[var(--neutral-800)] rounded-lg overflow-hidden p-3 bg-[var(--neutral-950-a50)]">
          <MermaidDiagram mermaidText={mermaidText} />
        </div>
      )}
    </div>
  );
}

// --- Wireframes — paste the initial HTML, then edit via the existing
// WireframePreview.jsx round trip. Per WireframePreview's own docstring,
// onRequestEdit reuses the ordinary chat-send function, and the edit
// round-trip only works while the CURRENTLY ACTIVE chat (sessionId) is
// the same one that actually ran wireframe_sketcher — flagged plainly
// here rather than hidden, same discipline as every other known
// simplification in this domain.
// FIX — the pasted HTML now persists via eo/panel_content.py under
// panelKey "wireframes". The live edit-round-trip (sendTask, scoped to
// the currently open chat) is unchanged and still session-scoped, not
// something this store can fix — only the paste itself survives reload now.
//
// NOT wired to PanelSourceBadge (patch 4, decided this pass) — "wireframes"
// was never one of PLAN_ROLE_PANEL_MAP's six roles (see eo/panel_content.py's
// own comment on that map), so write_panel_from_role() never writes this
// panel_key and content_source would read "manual" unconditionally, every
// time, for every workspace — a badge that can only ever show one static
// label isn't telling the person anything a badge is for. Revisit only if
// wireframe_sketcher ever gets a direct-write path of its own.
function WireframesPanel({ workspaceId, fetchPanelContent, savePanelContent, sessionId, sendTask }) {
  const [raw, setRaw] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState(null);
  const html = unfenceMermaid(raw.replace(/```html/i, "```")); // reuse the same fence-stripper for ```html blocks

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setSavedAt(null);
    fetchPanelContent(workspaceId, "wireframes").then((saved) => {
      if (cancelled) return;
      setRaw(saved?.content || "");
      setLoading(false);
    });
    return () => { cancelled = true; };
  }, [workspaceId, fetchPanelContent]);

  async function handleSave() {
    setSaving(true);
    try {
      await savePanelContent(workspaceId, "wireframes", raw);
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
        Paste wireframe_sketcher's HTML output below (raw or a fenced <code>```html</code> block).
        "Send edit" below re-sends the edit instruction into whichever chat is currently open
        (session <code>{sessionId ? sessionId.slice(0, 8) : "none"}</code>) — this only produces a
        real follow-up wireframe if that's the same chat that generated this one (§5.5/§5.7).
      </p>
      <textarea
        id="plan-wireframe-paste-raw"
        name="planWireframePasteRaw"
        value={raw}
        onChange={(e) => setRaw(e.target.value)}
        placeholder="<!doctype html>..."
        rows={6}
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
      <WireframePreview
        html={html}
        screenLabel="Pasted wireframe"
        onRequestEdit={sendTask ? (instruction) => sendTask(instruction) : undefined}
      />
    </div>
  );
}

// --- Blueprint — Parts / Wiring / Mech / Instructions. UNLIKE every
// other sub-tab above, this isn't a paste-and-save panel: it reads
// agents/hardware_speccer.py's structured output (device_spec: parts,
// wiring, mech, instructions), fetched once per workspace select via
// fetchDeviceSpec — persisted under eo/workspace_facts.py's `custom`
// dict (see api/server.py's GET .../device-spec), not
// eo/panel_content.py, since panel_content is for one opaque pasted
// string and this has real per-part/per-step structure. A nested small-
// tab-bar picks which of the four slices to render, same pattern
// NotebooksTab.jsx already uses for its own seven sub-views.
// patch 7 (T2/T3 Plan/Build split): "instructions" removed from this list
// — InstructionChecklist now renders in BuildTab.jsx instead. Parts/
// Wiring/Mech stay here as design specs; the checklist was the one
// during-the-work progress tracker among the four, so it moved to sit
// with Build's other progress tracking (the Missing/In Progress/Done
// kanban) instead.
const BLUEPRINT_VIEWS = [
  { id: "parts", label: "Parts" },
  { id: "wiring", label: "Wiring" },
  { id: "mech", label: "Mech" },
];

// BUG FIX (2026-08-12): BlueprintView never re-fetched after
// hardware_speccer wrote a fresh spec mid-conversation -- its effect
// only ever depended on [workspaceId, fetchDeviceSpec], so a person had
// to switch workspaces (or reload the page) to see Parts/Wiring/Mech
// actually populate after a hardware build request finished, even once
// the routing + stage_output bugs above are fixed and hardware_speccer
// genuinely runs and writes the spec. Every one of the six PRD-family
// panels above already solved this exact problem via `refreshSignal`
// (planPanelRefreshSignal, passed down from PlanTab -- see patch 3's
// own comment on that combined counter): BlueprintView just never
// received the prop, since Blueprint's write path (hardware_speccer's
// own direct workspace_facts.custom write) is different from
// eo/panel_content.py's set_content() the other six panels use, and
// that plumbing gap was never revisited when Blueprint was added.
// planPanelRefreshSignal already fires on ANY assistant turn finishing
// in this workspace's chat (session-local) or ANY other
// dock/tab/browser-tab's PANEL_CONTENT_UPDATED push for this workspace
// (cross-tab) -- both are equally valid "a role just finished, go
// re-check" signals, and hardware_speccer finishing is exactly that,
// so reusing the same counter (rather than plumbing a whole second
// Pusher subscription just for device-spec writes) is correct here too.
function BlueprintView({ workspaceId, fetchDeviceSpec, refreshPartPrices, refreshSignal }) {
  const [spec, setSpec] = useState(null);
  const [loading, setLoading] = useState(true);
  const [view, setView] = useState("parts");
  const [refreshing, setRefreshing] = useState(false);
  // NEW — step 4 (wiring diagram UI surface): which of the two wiring
  // sub-views is showing -- the existing force-graph ("graph", WiringGraph.jsx,
  // Bugs 6-8's pin-labeled arrows) or the new deterministic Mermaid diagram
  // ("diagram", hardware_speccer.py's _build_wiring_mermaid() from step 3,
  // rendered via the same MermaidDiagram.jsx every other Blueprint/PRD
  // diagram already uses). Kept local to BlueprintView rather than a third
  // top-level BLUEPRINT_VIEWS entry -- this toggles *within* the Wiring
  // slice, it isn't a fourth Parts/Wiring/Mech sibling. Resets to "graph"
  // implicitly on every workspace switch since this state isn't persisted.
  const [wiringSubView, setWiringSubView] = useState("graph");

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetchDeviceSpec(workspaceId).then((data) => {
      if (cancelled) return;
      setSpec(data);
      setLoading(false);
    });
    return () => { cancelled = true; };
  }, [workspaceId, fetchDeviceSpec, refreshSignal]);

  async function handleRefreshPrices() {
    setRefreshing(true);
    try {
      const updatedParts = await refreshPartPrices(workspaceId, spec.parts);
      setSpec((prev) => ({ ...prev, parts: updatedParts }));
    } finally {
      setRefreshing(false);
    }
  }

  if (loading) {
    return (
      <div className="text-xs text-[var(--neutral-600)] flex items-center gap-1.5">
        <Loader2 size={12} className="animate-spin" /> Loading…
      </div>
    );
  }

  // patch 7: instructions.phases dropped from this check — Blueprint no
  // longer renders an Instructions view, so a spec that has only
  // instructions (no parts/wiring yet) shouldn't count as "hasSpec" here,
  // or the nav below would show tabs with nothing in any of them. (Mech
  // was never part of this check pre-patch either — left as-is.)
  const hasSpec = spec && (spec.parts?.length || spec.wiring?.nodes?.length);

  if (!hasSpec) {
    return (
      <p className="text-xs text-[var(--neutral-600)]">
        No device spec generated yet — run hardware_speccer from this project's chat once a PRD exists.
      </p>
    );
  }

  return (
    <div className="space-y-3">
      <nav className="flex gap-1">
        {BLUEPRINT_VIEWS.map((v) => (
          <button
            key={v.id}
            onClick={() => setView(v.id)}
            className={`text-xs rounded px-2.5 py-1 ${
              view === v.id
                ? "bg-[var(--accent)] text-[var(--accent-text)] font-medium"
                : "text-[var(--neutral-500)] hover:text-[var(--neutral-300)]"
            }`}
          >
            {v.label}
          </button>
        ))}
      </nav>

      {view === "parts" && (
        <PartsTable parts={spec.parts} refreshing={refreshing} onRefreshPrices={handleRefreshPrices} />
      )}
      {view === "wiring" && (
        <div className="space-y-2">
          {/* NEW — step 4: only shown once a spec actually carries a
              wiring.mermaid string -- step 3's _build_wiring_mermaid()
              always sets this key going forward, but a spec generated
              before that patch (or one whose wiring has no edges to
              diagram at all) simply won't have it. Falling back to
              graph-only in that case rather than showing a toggle to a
              blank/broken second view. */}
          {spec.wiring?.mermaid && (
            <div className="flex gap-1">
              {[
                { id: "graph", label: "Graph" },
                { id: "diagram", label: "Diagram" },
              ].map((sv) => (
                <button
                  key={sv.id}
                  onClick={() => setWiringSubView(sv.id)}
                  className={`text-[11px] rounded px-2 py-0.5 border ${
                    wiringSubView === sv.id
                      ? "bg-[var(--accent)] text-[var(--accent-text)] border-[var(--accent)] font-medium"
                      : "text-[var(--neutral-500)] border-[var(--neutral-800)] hover:text-[var(--neutral-300)]"
                  }`}
                >
                  {sv.label}
                </button>
              ))}
            </div>
          )}
          {wiringSubView === "diagram" && spec.wiring?.mermaid ? (
            <MermaidDiagram
              mermaidText={spec.wiring.mermaid}
              showControls
              exportFilename="wiring-diagram"
            />
          ) : (
            <WiringGraph wiring={spec.wiring} />
          )}
        </div>
      )}
      {view === "mech" && <MechView mech={spec.mech} parts={spec.parts} />}
    </div>
  );
}

// Item 6 (perf audit, tab-body pass): PlanTab takes props from its parent
// (onOpenChat, initialWorkspaceId, onConsumeInitialWorkspaceId, onPromoted,
// onActiveWorkspaceChange). Wrapped in memo() now that SessionContext's
// useCallback pass (item 2) means its stable-identity props/callbacks stay
// stable across unrelated parent re-renders -- prop objects/arrays it reads
// (workspaces, chats, etc.) are only ever replaced, never mutated in place, so
// a shallow prop comparison here is meaningful.
export default memo(PlanTab);
