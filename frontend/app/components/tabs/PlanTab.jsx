"use client";
import { useState, useEffect } from "react";
import { useSession } from "../../context/SessionContext";
import MermaidDiagram from "../MermaidDiagram";
import WireframePreview from "../WireframePreview";
import Markdown from "../Markdown";
import ManageWorkspaceModal from "../ManageWorkspaceModal"; // NEW — parity fix: rename/delete kebab, same as NotebooksTab
import WorkspaceChatPanel from "../WorkspaceChatPanel";      // NEW — parity fix: embedded chat + WorkingPanel dock, same as Notebooks/Research
import { useWorkspaceDock, useWorkspaceDockActions } from "../../context/WorkspaceDockContext"; // NEW — step 3e
import WorkspaceDataBubble from "../WorkspaceDataBubble";
import PartsTable from "../PartsTable";                       // NEW — Blueprint sub-tab
import WiringGraph from "../WiringGraph";                     // NEW — Blueprint sub-tab
import MechView from "../MechView";                           // NEW — Blueprint sub-tab
import InstructionChecklist from "../InstructionChecklist";   // NEW — Blueprint sub-tab
import {
  FileText, GitBranch, Database, Webhook, Skull, Calculator,
  LayoutTemplate, Rocket, FolderOpen, MoreVertical, ArrowUpRight,
  Loader2, ChevronRight, MessageSquare, Cpu,
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
        <label className="text-[10px] uppercase tracking-wide text-[var(--neutral-500)]">
          Paste handoff_packager's chat response (optional — auto-fills the fields below)
        </label>
        <textarea
          value={pasted}
          onChange={(e) => parsePasted(e.target.value)}
          placeholder='Handoff ready for "..." — 4 feature(s), first cycle target: "Auth". Scoped to app_slug "my-app_ab12cd34"...'
          rows={2}
          className="w-full mt-1 bg-black/30 border border-[var(--neutral-800)] rounded px-3 py-2 text-xs outline-none focus:border-[var(--cyber-amber)] font-mono"
        />
      </div>
      <div>
        <label className="text-[10px] uppercase tracking-wide text-[var(--neutral-500)]">App slug</label>
        <input
          value={appSlug}
          onChange={(e) => setAppSlug(e.target.value)}
          placeholder="my-app_ab12cd34"
          className="w-full mt-1 bg-black/30 border border-[var(--neutral-800)] rounded px-3 py-2 text-xs outline-none focus:border-[var(--cyber-amber)] font-mono"
        />
      </div>
      <div>
        <label className="text-[10px] uppercase tracking-wide text-[var(--neutral-500)]">First task / cycle goal</label>
        <textarea
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

export default function PlanTab({ onOpenChat, initialWorkspaceId, onConsumeInitialWorkspaceId, onPromoted }) {
  const { workspaces, fetchWorkspaces, chats, promoteWorkspace, openScopedSubChat,
    fetchPanelContent, savePanelContent,
    fetchDeviceSpec, refreshPartPrices, toggleInstructionStep } = useSession();
  // NEW — step 3e follow-up fix: the embedded WorkspaceChatPanel below was
  // NOT actually dock-driven despite the old comment here claiming so —
  // it had no workspaceId prop, so it read messages/sessionId off
  // useSession() (legacy/global) while switchChat here (dock-based) wrote
  // into a dock slot the visible panel never read. Fixed below by passing
  // workspaceId={activeWs?.id} to the panel (now the same key switchChat
  // already resolves to, and the same key `dock` below already uses for
  // WireframesPanel).
  const { switchChat } = useWorkspaceDockActions();

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
  // PARITY FIX — which plan project's kebab menu (rename/delete/members)
  // is open. ManageWorkspaceModal already exists fully built, just never
  // wired into this tab.
  const [managingWorkspace, setManagingWorkspace] = useState(null);

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

  // NEW — step 3e: WireframesPanel's "re-send edit into whichever chat is
  // currently open" only makes sense scoped to activeWs's own dock now —
  // WorkspaceChatPanel below is already reading/writing that same dock
  // (step 3d), so this keeps both in sync instead of one reading the
  // dock and the other reading a legacy sessionId nothing updates anymore.
  const dock = useWorkspaceDock(activeWs?.id);

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
      <div className="w-56 shrink-0 border-r border-[var(--neutral-800)] flex flex-col">
        <div className="px-3 py-3 border-b border-[var(--neutral-800)]">
          <span className="text-xs font-medium text-[var(--neutral-400)]">Plan projects</span>
        </div>
        <div className="flex-1 overflow-y-auto">
          {planProjects.length === 0 && (
            <p className="px-3 py-3 text-xs text-[var(--neutral-600)]">
              No plan projects yet — promote a research project from the Research tab, or create one from the chat sidebar's <FolderOpen size={11} className="inline" /> button.
            </p>
          )}
          {planProjects.map((ws) => (
            <div
              key={ws.id}
              className={`group flex items-center gap-1 border-b border-[var(--neutral-900)] ${
                ws.id === activeWsId
                  ? "bg-[var(--neutral-800-a70)] text-[var(--neutral-100)]"
                  : "text-[var(--neutral-300)] hover:bg-[var(--neutral-900)]"
              }`}
            >
              <button
                onClick={() => setActiveWsId(ws.id)}
                className="flex-1 min-w-0 flex items-center justify-between gap-1 px-3 py-2 text-left text-xs"
              >
                <span className="truncate">{ws.name}</span>
                {ws.id === activeWsId && <ChevronRight size={12} className="text-[var(--neutral-500)] shrink-0" />}
              </button>
              <button
                onClick={() => setManagingWorkspace(ws)}
                title="Rename or delete project"
                className="shrink-0 pr-2 text-[var(--neutral-600)] opacity-0 group-hover:opacity-100 hover:text-[var(--neutral-200)]"
              >
                <MoreVertical size={13} />
              </button>
            </div>
          ))}
        </div>
      </div>

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
                    ? "bg-[var(--cyber-amber)] text-black font-medium"
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
          <WorkspaceDataBubble
            workspaceId={activeWs?.id}
            workspaceName={activeWs?.name}
            storageKey="minime_plan_data_bubble_collapsed"
          />
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
                    placeholder="Paste prd_writer's PRD output (from a chat message) below."
                    paste_hint="Includes a Features/Priorities/First-cycle-scope section, per prd_writer's brief."
                  />
                )}
                {t.id === "architecture" && (
                  <DiagramPastePanel
                    workspaceId={activeWs.id}
                    panelKey="architecture"
                    fetchPanelContent={fetchPanelContent}
                    savePanelContent={savePanelContent}
                    roleLabel="architecture_diagrammer"
                  />
                )}
                {t.id === "schema" && (
                  <DiagramPastePanel
                    workspaceId={activeWs.id}
                    panelKey="schema"
                    fetchPanelContent={fetchPanelContent}
                    savePanelContent={savePanelContent}
                    roleLabel="schema_diagrammer"
                  />
                )}
                {t.id === "api_contract" && (
                  <MarkdownPastePanel
                    workspaceId={activeWs.id}
                    panelKey="api_contract"
                    fetchPanelContent={fetchPanelContent}
                    savePanelContent={savePanelContent}
                    placeholder="Paste api_contract_writer's endpoint table output below."
                  />
                )}
                {t.id === "devils_advocate" && (
                  <MarkdownPastePanel
                    workspaceId={activeWs.id}
                    panelKey="devils_advocate"
                    fetchPanelContent={fetchPanelContent}
                    savePanelContent={savePanelContent}
                    placeholder="Paste devils_advocate's critique output below."
                  />
                )}
                {t.id === "feasibility" && (
                  <MarkdownPastePanel
                    workspaceId={activeWs.id}
                    panelKey="feasibility"
                    fetchPanelContent={fetchPanelContent}
                    savePanelContent={savePanelContent}
                    placeholder="Paste feasibility_estimator's output below."
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
                    toggleInstructionStep={toggleInstructionStep}
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
    </div>
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
function MarkdownPastePanel({ workspaceId, panelKey, fetchPanelContent, savePanelContent, placeholder, paste_hint, estimateBanner }) {
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
        {placeholder} Saved per project — pasting here again for the same project overwrites
        the previous paste, same as Research's Extraction Table/Contradictions tabs.
        {paste_hint && <> {paste_hint}</>}
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
function DiagramPastePanel({ workspaceId, panelKey, fetchPanelContent, savePanelContent, roleLabel }) {
  const [raw, setRaw] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState(null);
  const mermaidText = unfenceMermaid(raw);

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
        Paste {roleLabel}'s output below — either the raw Mermaid syntax or a fenced
        <code className="mx-1 text-[var(--neutral-400)]">```mermaid</code> block copied from a chat message.
      </p>
      <textarea
        value={raw}
        onChange={(e) => setRaw(e.target.value)}
        placeholder={"graph TD\n  A[Client] --> B[API]\n  B --> C[(Database)]"}
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
const BLUEPRINT_VIEWS = [
  { id: "parts", label: "Parts" },
  { id: "wiring", label: "Wiring" },
  { id: "mech", label: "Mech" },
  { id: "instructions", label: "Instructions" },
];

function BlueprintView({ workspaceId, fetchDeviceSpec, refreshPartPrices, toggleInstructionStep }) {
  const [spec, setSpec] = useState(null);
  const [loading, setLoading] = useState(true);
  const [view, setView] = useState("parts");
  const [refreshing, setRefreshing] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetchDeviceSpec(workspaceId).then((data) => {
      if (cancelled) return;
      setSpec(data);
      setLoading(false);
    });
    return () => { cancelled = true; };
  }, [workspaceId, fetchDeviceSpec]);

  async function handleRefreshPrices() {
    setRefreshing(true);
    try {
      const updatedParts = await refreshPartPrices(workspaceId, spec.parts);
      setSpec((prev) => ({ ...prev, parts: updatedParts }));
    } finally {
      setRefreshing(false);
    }
  }

  async function handleToggleStep(phaseId, stepId, done) {
    const result = await toggleInstructionStep(workspaceId, stepId, done);
    // toggle endpoint returns the full updated `instructions` object
    // (api/server.py's toggle_instruction_step) -- swap it in directly
    // rather than re-fetching the whole device spec for a one-step change.
    if (result?.instructions) {
      setSpec((prev) => ({ ...prev, instructions: result.instructions }));
    }
  }

  if (loading) {
    return (
      <div className="text-xs text-[var(--neutral-600)] flex items-center gap-1.5">
        <Loader2 size={12} className="animate-spin" /> Loading…
      </div>
    );
  }

  const hasSpec = spec && (spec.parts?.length || spec.wiring?.nodes?.length || spec.instructions?.phases?.length);

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
                ? "bg-[var(--cyber-amber)] text-black font-medium"
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
      {view === "wiring" && <WiringGraph wiring={spec.wiring} />}
      {view === "mech" && <MechView mech={spec.mech} parts={spec.parts} />}
      {view === "instructions" && (
        <InstructionChecklist phases={spec.instructions.phases} onToggleStep={handleToggleStep} />
      )}
    </div>
  );
}
