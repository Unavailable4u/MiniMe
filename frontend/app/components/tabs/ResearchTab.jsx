import { useState, useEffect, useMemo } from "react";
import { useSession } from "../../context/SessionContext";
import KnowledgeGraphView from "../KnowledgeGraphView";
import ExtractionTableView from "../research/ExtractionTableView";
import Markdown from "../Markdown";
import ConfirmDialog from "../ConfirmDialog";   // NEW — §2 fix: same delete affordance as Notebooks' Sources tab
import WorkspaceChatPanel from "../WorkspaceChatPanel";  // NEW — §6.2b: embedded chat + WorkingPanel dock, same as Notebooks
import {
  Search, Share2, Table2, GitCompare, FlaskConical,
  RefreshCw, ExternalLink, FolderOpen, Plus, X, Loader2, Sparkles, Trash2, MessageSquare,
  ArrowUpRight,
} from "lucide-react";

// Part 3 §3.9 — Research as a dedicated top-level section, same shape as
// Notebooks (§4.7): a project (= workspace, exactly like "notebook" ==
// workspace_id there) picker on the left, sub-tabs on the right.
//
// What's genuinely NEW backend-facing work here vs. reused: `academic_search`
// already writes every found paper as a Part 0 node (section: "research",
// node_type: "source") and every citation as a "cites" edge (§3.3);
// `citation_graph_builder` is a read-only view over exactly that data
// (§3.4) -- so "Sources" and "Citation Graph" below are real, live-fetched
// data via the same fetchWorkspaceNodes()/fetchGraphEdges() Notebooks
// already uses, no new endpoints needed.
//
// Contradictions/consensus meter and dataset analysis are still paste-
// or handoff-based: they're written to session-scoped memory-bus keys
// (KEYS["contradiction_candidates"], etc.), not persistent per-workspace
// storage, so there's no GET endpoint to fetch "the latest one" outside
// the chat run that produced it. Same known simplification the Notebooks
// README used for Mind Map/Study.
//
// Extraction Table is the exception: agents/note_table_builder.py has
// its own direct endpoint (POST /api/workspaces/{id}/table) that reads
// a workspace's ingested nodes and extracts user-specified fields on
// demand — no chat run or memory-bus key required — so that sub-tab
// below calls it directly instead of asking the user to paste anything.
// Manual paste is kept as a fallback for markdown table output that
// came from elsewhere.

const SUB_TABS = [
  { id: "sources", label: "Sources", icon: Search },
  { id: "graph", label: "Citation Graph", icon: Share2 },
  { id: "extraction", label: "Extraction Table", icon: Table2 },
  { id: "contradictions", label: "Contradictions & Consensus", icon: GitCompare },
  { id: "dataset", label: "Dataset Analysis", icon: FlaskConical },
];

// Edge from_node_id/to_node_id are "node:{workspace_id}:{node_id}"
// (eo/graph_edges.py); node.node_id from list_nodes() is already the bare
// id. Normalize here so KnowledgeGraphView's ForceGraphBase can actually
// match link endpoints to node ids.
function bareNodeId(fullId) {
  const parts = (fullId || "").split(":");
  return parts.length >= 3 ? parts.slice(2).join(":") : fullId;
}

// NEW — §6.2b: right-hand chat dock collapse key. Deliberately separate
// from Notebooks' own "minime_notebooks_chatdock_collapsed" (and from
// WorkspaceChatPanel's internal WORKING_PANEL_KEY) — folding the dock
// away in Research shouldn't affect Notebooks' dock state or vice versa,
// same reasoning §6.2a already applied.
const CHAT_DOCK_KEY = "minime_research_chatdock_collapsed";
const PROMOTE_TARGETS = ["plan", "build", "test", "growth"];
const PROMOTE_LABELS = {
  plan: "Plan",
  build: "Build",
  test: "Test",
  growth: "Growth",
};

export default function ResearchTab({ initialWorkspaceId, onConsumeInitialWorkspaceId, onPromoted }) {
  const { workspaces, fetchWorkspaces, promoteWorkspace, fetchWorkspaceNodes, deleteWorkspaceNode, fetchGraphEdges, openScopedSubChat, buildExtractionTable, switchChat, fetchPanelContent, savePanelContent } = useSession();
  const [activeWsId, setActiveWsId] = useState(null);
  const [subTab, setSubTab] = useState("sources");
  // NEW — §8 fix: promote-to-Plan busy/error state, same shape as
  // NotebooksTab's promote-to-Research and TasksTab's promote-to-Test —
  // this link was missing entirely (ResearchTab never destructured
  // promoteWorkspace before), which silently stranded every research
  // project with no way to reach Plan.
  const [promoting, setPromoting] = useState(false);
  const [promoteError, setPromoteError] = useState(null);
  const [promoteTargetStage, setPromoteTargetStage] = useState("plan");
  // NEW — §6.2b: right-hand chat dock collapse state, restored from
  // localStorage on mount — same pattern as NotebooksTab's chatDockCollapsed.
  const [chatDockCollapsed, setChatDockCollapsed] = useState(false);

  useEffect(() => {
    setChatDockCollapsed(localStorage.getItem(CHAT_DOCK_KEY) === "1");
  }, []);

  function toggleChatDock() {
    setChatDockCollapsed((prev) => {
      localStorage.setItem(CHAT_DOCK_KEY, !prev ? "1" : "0");
      return !prev;
    });
  }
  // NEW — same "switch + expand, no tab jump" helper as NotebooksTab.
  async function openInDock(chatId) {
    await switchChat(chatId);
    if (chatDockCollapsed) toggleChatDock();
  }

  // NEW — §8: Research only shows research-stage workspaces now — a
  // notebook promoted from Notebooks lands here, not in both tabs.
  const researchProjects = workspaces.filter((w) => w.stage === "research");

  // NEW — §8: a promote-and-navigate hand-off from Notebooks (via
  // AppShell) pre-selects the just-promoted workspace, then clears
  // itself so it doesn't re-fire on a later unrelated tab switch.
  useEffect(() => {
    if (initialWorkspaceId) {
      setActiveWsId(initialWorkspaceId);
      onConsumeInitialWorkspaceId?.();
    }
  }, [initialWorkspaceId, onConsumeInitialWorkspaceId]);

  useEffect(() => {
    if (!activeWsId && researchProjects.length > 0) setActiveWsId(researchProjects[0].id);
  }, [researchProjects, activeWsId]);

  const activeWs = researchProjects.find((w) => w.id === activeWsId) || null;

  // FIX — sub-tabs were conditionally rendered (ternary chain below),
  // which unmounts whichever sub-tab you leave and destroys its local
  // state (a paste-box's contents, an in-progress search, an in-flight
  // IngestionDropzone-style upload's own progress). Same "stays
  // mounted, hidden via CSS" technique AppShell.jsx already uses for
  // top-level tabs, just applied one level down for this tab's own
  // sub-tabs.
  const [visitedSubTabs, setVisitedSubTabs] = useState(() => new Set([subTab]));
  useEffect(() => {
    setVisitedSubTabs((prev) => (prev.has(subTab) ? prev : new Set(prev).add(subTab)));
  }, [subTab]);

  // NEW — §8 fix: promotes the research project to Plan and hands off
  // navigation to AppShell, same onPromoted(nextStage, wsId) contract
  // NotebooksTab and TasksTab already use — AppShell switches tabs and
  // pre-selects it there via PlanTab's own initialWorkspaceId prop
  // (added alongside this fix, same shape as ResearchTab's own).
  async function handlePromote(wsId, toStage = promoteTargetStage) {
    setPromoting(true);
    setPromoteError(null);
    try {
      await promoteWorkspace(wsId, toStage);
      await fetchWorkspaces();
      onPromoted?.(toStage, wsId);
    } catch (err) {
      setPromoteError(err.message);
    } finally {
      setPromoting(false);
    }
  }

  return (
    <div className="flex h-full">
      {/* Project picker — a "research project" is just a workspace, same
          as a "notebook" is. No new container concept. */}
      <div className="w-56 shrink-0 border-r border-[var(--neutral-800)] flex flex-col">
        <div className="px-3 py-3 border-b border-[var(--neutral-800)]">
          <span className="text-xs font-medium text-[var(--neutral-400)]">Research projects</span>
        </div>
        <div className="flex-1 overflow-y-auto">
          {researchProjects.length === 0 && (
            <p className="px-3 py-3 text-xs text-[var(--neutral-600)]">
              No research projects yet — promote a notebook from the Notebooks tab, or create one from the chat sidebar's <FolderOpen size={11} className="inline" /> button.
            </p>
          )}
          {researchProjects.map((ws) => (
            <button
              key={ws.id}
              onClick={() => setActiveWsId(ws.id)}
              className={`w-full text-left px-3 py-2 text-xs border-b border-[var(--neutral-900)] ${
                ws.id === activeWsId
                  ? "bg-[var(--neutral-800-a70)] text-[var(--neutral-100)]"
                  : "text-[var(--neutral-300)] hover:bg-[var(--neutral-900)]"
              }`}
            >
              {ws.name}
              <span className="text-[var(--neutral-600)]"> · {ws.chat_ids.length}</span>
            </button>
          ))}
        </div>
      </div>

      <div className="flex-1 min-h-0 flex flex-col">
        {/* NEW — §8 fix: title + promote row, same shape as NotebooksTab's
            header — this was missing entirely, so a research project had
            no path forward to Plan. */}
        {activeWs && (
          <div className="flex items-center justify-between px-3 py-2 border-b border-[var(--neutral-800)]">
            <h2 className="text-sm font-medium text-[var(--neutral-100)] truncate">{activeWs.name}</h2>
            <div className="flex items-center gap-2 shrink-0">
              <label className="sr-only" htmlFor="research-promote-target">Promote to</label>
              <select
                id="research-promote-target"
                value={promoteTargetStage}
                onChange={(e) => setPromoteTargetStage(e.target.value)}
                disabled={promoting}
                className="bg-[var(--neutral-900)] border border-[var(--neutral-700)] text-[var(--neutral-200)] rounded-lg px-2 py-1.5 text-xs outline-none disabled:opacity-50"
              >
                {PROMOTE_TARGETS.map((stage) => (
                  <option key={stage} value={stage}>{PROMOTE_LABELS[stage]}</option>
                ))}
              </select>
              <button
                onClick={() => handlePromote(activeWs.id)}
                disabled={promoting}
                className="flex items-center gap-1.5 text-xs border border-[var(--neutral-700)] text-[var(--neutral-200)] rounded-lg px-3 py-1.5 font-medium disabled:opacity-50 shrink-0"
              >
                {promoting ? <Loader2 size={13} className="animate-spin" /> : <ArrowUpRight size={13} />}
                Promote to {PROMOTE_LABELS[promoteTargetStage]} →
              </button>
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

        <div className="flex-1 min-h-0 overflow-y-auto p-4">
          {!activeWs ? (
            <p className="text-xs text-[var(--neutral-600)]">Pick or create a project to get started.</p>
          ) : (
            SUB_TABS.filter((t) => visitedSubTabs.has(t.id)).map((t) => (
              <div key={t.id} style={{ display: subTab === t.id ? "contents" : "none" }}>
                {t.id === "sources" && (
                  <SourcesPanel wsId={activeWs.id} fetchWorkspaceNodes={fetchWorkspaceNodes} deleteWorkspaceNode={deleteWorkspaceNode} openScopedSubChat={openScopedSubChat} openInDock={openInDock} />
                )}
                {t.id === "graph" && (
                  <CitationGraphPanel wsId={activeWs.id} fetchWorkspaceNodes={fetchWorkspaceNodes} fetchGraphEdges={fetchGraphEdges} />
                )}
                {t.id === "extraction" && (
                  <ExtractionPanel
                    wsId={activeWs.id}
                    buildExtractionTable={buildExtractionTable}
                    fetchPanelContent={fetchPanelContent}
                    savePanelContent={savePanelContent}
                  />
                )}
                {t.id === "contradictions" && (
                  <ContradictionsPanel
                    workspaceId={activeWs.id}
                    fetchPanelContent={fetchPanelContent}
                    savePanelContent={savePanelContent}
                  />
                )}
                {t.id === "dataset" && (
                  <DatasetPanel wsId={activeWs.id} openScopedSubChat={openScopedSubChat} openInDock={openInDock} />
                )}
              </div>
            ))
          )}
        </div>
      </div>

      {/* NEW — §6.2b: embedded chat + WorkingPanel dock, scoped to whatever
          chat is currently active in SessionContext — same dock Notebooks
          uses, own independent collapse state/localStorage key so the two
          tabs' dock visibility don't interfere with each other. Hidden
          below lg, matching Notebooks' and WorkingPanel's own breakpoint. */}
      <div className="hidden lg:flex shrink-0 border-l border-[var(--neutral-800)]" style={{ width: chatDockCollapsed ? undefined : 560 }}>
        <WorkspaceChatPanel collapsed={chatDockCollapsed} onToggleCollapse={toggleChatDock} />
      </div>
      {!chatDockCollapsed && (
        <div className="lg:hidden fixed inset-0 z-40 bg-[var(--neutral-950)]">
          <WorkspaceChatPanel collapsed={false} onToggleCollapse={toggleChatDock} />
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
    </div>
  );
}

// --- Sources — Discovery (§3.3). "Search" hands off to a scoped sub-chat
// (same "click a node, open a scoped sub-chat" hand-off shape Notebooks'
// mind-map click uses) so academic_search + the reasoning roles it feeds
// (researcher/fact_checker/writer/editor) run exactly as the Panel would
// normally staff them. The list below reads back what that run wrote as
// nodes -- real, persisted, live-fetched data, not a paste box.
function SourcesPanel({ wsId, fetchWorkspaceNodes, deleteWorkspaceNode, openScopedSubChat, openInDock }) {
  const [query, setQuery] = useState("");
  const [sources, setSources] = useState([]);
  const [loading, setLoading] = useState(false);
  const [searching, setSearching] = useState(false);
  const [pendingDelete, setPendingDelete] = useState(null); // NEW — §2 fix
  const [deleting, setDeleting] = useState(false);

  async function load() {
    setLoading(true);
    const nodes = await fetchWorkspaceNodes(wsId, "source");
    setSources((nodes || []).filter((n) => n.section === "research"));
    setLoading(false);
  }

  useEffect(() => { load(); }, [wsId]); // eslint-disable-line react-hooks/exhaustive-deps

  async function runSearch() {
    if (!query.trim()) return;
    setSearching(true);
    try {
      const chatId = await openScopedSubChat(wsId, `Find recent papers about: ${query.trim()}`);
      await openInDock(chatId);
    } finally {
      setSearching(false);
    }
  }

  async function confirmDelete() {
    if (!pendingDelete) return;
    setDeleting(true);
    try {
      await deleteWorkspaceNode(wsId, pendingDelete.node_id);
      setPendingDelete(null);
      await load();
    } finally {
      setDeleting(false);
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex gap-2">
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && runSearch()}
          placeholder="e.g. transformer attention mechanisms, systematic review of..."
          className="flex-1 bg-black/30 border border-[var(--neutral-800)] rounded px-3 py-2 text-xs outline-none focus:border-[var(--cyber-violet)]"
        />
        <button
          onClick={runSearch}
          disabled={searching || !query.trim()}
          className="text-xs bg-[var(--cyber-violet)] text-black rounded px-3 py-2 font-medium disabled:opacity-50 flex items-center gap-1"
        >
          <Search size={13} /> {searching ? "Dispatching…" : "Search"}
        </button>
        <button
          onClick={load}
          title="Refresh source list"
          className="text-xs text-[var(--neutral-500)] hover:text-[var(--neutral-200)] px-2"
        >
          <RefreshCw size={13} className={loading ? "animate-spin" : ""} />
        </button>
      </div>
      <p className="text-[11px] text-[var(--neutral-600)]">
        Runs academic_search (Semantic Scholar, arXiv, CrossRef, OpenAlex) plus whatever
        writing/synthesis roles the task needs, in this project's own chat — sources found
        get written back here as they're indexed.
      </p>

      {sources.length === 0 && !loading && (
        <p className="text-xs text-[var(--neutral-600)]">No sources indexed in this project yet — run a search above.</p>
      )}

      <div className="space-y-2">
        {sources.map((s) => (
          <div key={s.node_id} className="group border border-[var(--neutral-800)] rounded-lg p-3">
            <div className="flex items-start justify-between gap-2">
              <span className="text-xs font-medium text-[var(--neutral-100)]">{s.title}</span>
              <div className="flex items-center gap-1.5 shrink-0">
                {(s.tags || []).map((tag) => (
                  <span key={tag} className="text-[10px] uppercase tracking-wide text-[var(--cyber-violet)] bg-[var(--cyber-violet)]/10 rounded px-1.5 py-0.5">
                    {tag}
                  </span>
                ))}
                <button
                  onClick={() => setPendingDelete(s)}
                  title="Delete source"
                  className="text-[var(--neutral-600)] opacity-0 group-hover:opacity-100 hover:text-red-400"
                >
                  <Trash2 size={13} />
                </button>
              </div>
            </div>
            {s.content && (
              <p className="text-[11px] text-[var(--neutral-400)] mt-1.5 line-clamp-3">{s.content}</p>
            )}
          </div>
        ))}
      </div>
      <ConfirmDialog
        open={!!pendingDelete}
        title="Delete source"
        message={`Delete "${pendingDelete?.title || pendingDelete?.node_id}"? This also removes any links to it in the Citation Graph.`}
        confirmLabel={deleting ? "Deleting…" : "Delete"}
        onConfirm={confirmDelete}
        onCancel={() => setPendingDelete(null)}
      />
    </div>
  );
}

// --- Citation Graph — no new component needed (§3.4): same
// KnowledgeGraphView.jsx every other domain's node/edge graph uses,
// filtered to this project's section: "research" nodes/edges.
function CitationGraphPanel({ wsId, fetchWorkspaceNodes, fetchGraphEdges }) {
  const [nodes, setNodes] = useState([]);
  const [edges, setEdges] = useState([]);
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState(null);

  async function load() {
    setLoading(true);
    const [allNodes, allEdges] = await Promise.all([
      fetchWorkspaceNodes(wsId), // no node_type filter — quality-flag/gap nodes belong on this graph too
      fetchGraphEdges(wsId),
    ]);
    const researchNodes = (allNodes || []).filter((n) => n.section === "research");
    const researchIds = new Set(researchNodes.map((n) => n.node_id));
    const researchEdges = (allEdges || [])
      .map((e) => ({ ...e, from_node_id: bareNodeId(e.from_node_id), to_node_id: bareNodeId(e.to_node_id) }))
      .filter((e) => researchIds.has(e.from_node_id) && researchIds.has(e.to_node_id));
    setNodes(researchNodes);
    setEdges(researchEdges);
    setLoading(false);
  }

  useEffect(() => { load(); }, [wsId]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="h-full flex flex-col gap-3">
      <div className="flex items-center justify-between">
        <p className="text-[11px] text-[var(--neutral-600)]">
          {nodes.length} source(s), {edges.length} citation/relation edge(s).
        </p>
        <button onClick={load} className="text-xs text-[var(--neutral-500)] hover:text-[var(--neutral-200)] flex items-center gap-1">
          <RefreshCw size={12} className={loading ? "animate-spin" : ""} /> Refresh
        </button>
      </div>
      {nodes.length === 0 ? (
        <p className="text-xs text-[var(--neutral-600)]">No citation data yet — run a search from the Sources tab first.</p>
      ) : (
        <div className="flex-1 min-h-[420px] border border-[var(--neutral-800)] rounded-lg overflow-hidden">
          <KnowledgeGraphView nodes={nodes} edges={edges} onSelectNode={setSelected} />
        </div>
      )}
      {selected && (
        <div className="border border-[var(--neutral-800)] rounded-lg p-3">
          <p className="text-xs font-medium text-[var(--neutral-100)]">{selected.title}</p>
          {selected.content && <p className="text-[11px] text-[var(--neutral-400)] mt-1">{selected.content}</p>}
          {selected.tags?.length > 0 && (
            <p className="text-[10px] text-[var(--neutral-600)] mt-1">{selected.tags.join(" · ")}</p>
          )}
        </div>
      )}
    </div>
  );
}

// --- Extraction Table (§3.5) — auto-generated via
// agents/note_table_builder.py (POST /api/workspaces/{id}/table): the
// user names the fields they want, we extract them from every ingested
// source in the project, one LLM call per source, merged into one row
// each. Manual paste is kept as a fallback for output from a chat run
// that used a different role, but it's no longer the primary path.
const NODE_TYPE_OPTIONS = [
  { value: "", label: "All content" },
  { value: "source", label: "Sources only" },
];

function ExtractionPanel({ wsId, buildExtractionTable, fetchPanelContent, savePanelContent }) {
  const [fields, setFields] = useState([]);
  const [fieldDraft, setFieldDraft] = useState("");
  const [nodeType, setNodeType] = useState("");
  const [expanded, setExpanded] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState(null);
  const [result, setResult] = useState(null);
  const [showPaste, setShowPaste] = useState(false);
  // FIX — the manual-paste fallback now persists through
  // eo/panel_content.py under panel_key "extraction_manual", same
  // pattern as ContradictionsPanel just below (and PlanTab.jsx's
  // MarkdownPastePanel). Previously this was pure local state and lost
  // its content on unmount or project switch just like Contradictions
  // did before that fix. Note: the auto-generated `result` above (from
  // buildExtractionTable) is deliberately NOT persisted here — it's a
  // live endpoint call, same class as Sources/Citation Graph, and can
  // be regenerated on demand rather than needing a saved copy.
  const [raw, setRaw] = useState("");
  const [rawLoading, setRawLoading] = useState(true);
  const [rawSaving, setRawSaving] = useState(false);
  const [rawSavedAt, setRawSavedAt] = useState(null);

  useEffect(() => {
    let cancelled = false;
    setRawLoading(true);
    setRawSavedAt(null);
    fetchPanelContent(wsId, "extraction_manual").then((saved) => {
      if (cancelled) return;
      setRaw(saved?.content || "");
      setRawLoading(false);
    });
    return () => { cancelled = true; };
  }, [wsId, fetchPanelContent]);

  async function handleRawSave() {
    setRawSaving(true);
    try {
      await savePanelContent(wsId, "extraction_manual", raw);
      setRawSavedAt(Date.now());
    } finally {
      setRawSaving(false);
    }
  }

  function addField() {
    const name = fieldDraft.trim();
    if (!name || fields.includes(name)) return;
    setFields((f) => [...f, name]);
    setFieldDraft("");
  }

  function removeField(name) {
    setFields((f) => f.filter((x) => x !== name));
  }

  async function generate() {
    if (fields.length === 0 || !wsId) return;
    setGenerating(true);
    setError(null);
    try {
      const data = await buildExtractionTable(wsId, fields, { nodeType, expanded });
      setResult(data);
    } catch (e) {
      setError(e.message || "Failed to generate table.");
      setResult(null);
    } finally {
      setGenerating(false);
    }
  }

  return (
    <div className="space-y-4">
      <div className="border border-[var(--neutral-800)] rounded-lg p-3 space-y-3">
        <div>
          <label className="text-[11px] text-[var(--neutral-500)]">Fields to extract</label>
          <div className="flex flex-wrap gap-1.5 mt-1.5 mb-2">
            {fields.map((f) => (
              <span
                key={f}
                className="flex items-center gap-1 text-[11px] bg-[var(--cyber-violet)]/10 text-[var(--cyber-violet)] rounded px-2 py-1"
              >
                {f}
                <button onClick={() => removeField(f)} className="hover:text-[var(--neutral-100)]">
                  <X size={11} />
                </button>
              </span>
            ))}
            {fields.length === 0 && (
              <span className="text-[11px] text-[var(--neutral-600)]">
                e.g. "sample size", "methodology", "key finding"
              </span>
            )}
          </div>
          <div className="flex gap-2">
            <input
              value={fieldDraft}
              onChange={(e) => setFieldDraft(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); addField(); } }}
              placeholder="Add a field name and press Enter"
              className="flex-1 bg-black/30 border border-[var(--neutral-800)] rounded px-3 py-1.5 text-xs outline-none focus:border-[var(--cyber-violet)]"
            />
            <button
              onClick={addField}
              disabled={!fieldDraft.trim()}
              className="text-xs text-[var(--neutral-300)] border border-[var(--neutral-800)] rounded px-2.5 py-1.5 hover:border-[var(--neutral-700)] disabled:opacity-30 flex items-center gap-1"
            >
              <Plus size={12} /> Add
            </button>
          </div>
        </div>

        <div className="flex items-center gap-4 flex-wrap">
          <div className="flex items-center gap-2">
            <label className="text-[11px] text-[var(--neutral-500)]">Source scope</label>
            <select
              value={nodeType}
              onChange={(e) => setNodeType(e.target.value)}
              className="bg-black/30 border border-[var(--neutral-800)] rounded px-2 py-1 text-xs outline-none focus:border-[var(--cyber-violet)]"
            >
              {NODE_TYPE_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
          </div>
          <label className="flex items-center gap-1.5 text-[11px] text-[var(--neutral-500)] cursor-pointer">
            <input type="checkbox" checked={expanded} onChange={(e) => setExpanded(e.target.checked)} />
            Thorough mode (more workers, slower)
          </label>
        </div>

        <button
          onClick={generate}
          disabled={generating || fields.length === 0 || !wsId}
          className="text-xs bg-[var(--cyber-violet)] text-black rounded px-3 py-2 font-medium disabled:opacity-50 flex items-center gap-1.5"
        >
          {generating ? <Loader2 size={13} className="animate-spin" /> : <Sparkles size={13} />}
          {generating ? "Extracting…" : "Generate table"}
        </button>

        {error && <p className="text-[11px] text-red-400">{error}</p>}
      </div>

      {result?.summary && (
        <p className="text-[11px] text-[var(--neutral-500)]">{result.summary}</p>
      )}
      {result && <ExtractionTableView data={result} />}

      <div>
        <button
          onClick={() => setShowPaste((s) => !s)}
          className="text-[11px] text-[var(--neutral-600)] hover:text-[var(--neutral-400)] underline underline-offset-2"
        >
          {showPaste ? "Hide manual paste" : "Or paste a table from a chat run instead"}
        </button>
        {showPaste && (
          rawLoading ? (
            <div className="text-xs text-[var(--neutral-600)] flex items-center gap-1.5 mt-2">
              <Loader2 size={12} className="animate-spin" /> Loading…
            </div>
          ) : (
            <div className="space-y-2 mt-2">
              <p className="text-[11px] text-[var(--neutral-600)]">
                Saved per project — pasting here again for the same project overwrites the
                previous paste.
              </p>
              <textarea
                value={raw}
                onChange={(e) => setRaw(e.target.value)}
                placeholder="| Title | Year | Sample Size | Methodology | ... |&#10;|---|---|---|---|---|&#10;| ... |"
                rows={6}
                className="w-full bg-black/30 border border-[var(--neutral-800)] rounded px-3 py-2 text-xs outline-none focus:border-[var(--cyber-violet)] font-mono"
              />
              <div className="flex items-center gap-2">
                <button
                  onClick={handleRawSave}
                  disabled={rawSaving}
                  className="text-xs bg-[var(--cyber-violet)] text-black rounded px-3 py-1.5 font-medium disabled:opacity-50"
                >
                  {rawSaving ? "Saving…" : "Save"}
                </button>
                {rawSavedAt && !rawSaving && <span className="text-[11px] text-[var(--neutral-600)]">Saved</span>}
              </div>
              {raw.trim() && <ExtractionTableView text={raw} />}
            </div>
          )
        )}
      </div>
    </div>
  );
}

// --- Contradictions & Consensus (§3.6, §3.8) — same paste pattern; the
// AI-estimated-stance banner is Definition-of-Done #6's requirement that
// approximated results (consensus meter especially) are never presented
// with a trained model's implied confidence.
//
// FIX — this now persists through eo/panel_content.py under panel_key
// "contradictions", keyed per workspaceId — same pattern PlanTab.jsx's
// MarkdownPastePanel already uses for PRD/API Contract/Devil's Advocate/
// Feasibility. Previously this was pure local state: since this panel
// stays mounted across sub-tab switches (visitedSubTabs) AND across
// switching which research project is active, a paste would silently
// keep showing on screen even after switching to a *different* project.
// Fetches fresh content on every workspaceId change instead.
function ContradictionsPanel({ workspaceId, fetchPanelContent, savePanelContent }) {
  const [raw, setRaw] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setSavedAt(null);
    fetchPanelContent(workspaceId, "contradictions").then((saved) => {
      if (cancelled) return;
      setRaw(saved?.content || "");
      setLoading(false);
    });
    return () => { cancelled = true; };
  }, [workspaceId, fetchPanelContent]);

  async function handleSave() {
    setSaving(true);
    try {
      await savePanelContent(workspaceId, "contradictions", raw);
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
        Paste a contradiction_detector or consensus_meter run's output below. Saved per
        project — pasting here again for the same project overwrites the previous paste.
      </p>
      <textarea
        value={raw}
        onChange={(e) => setRaw(e.target.value)}
        placeholder="Paste the role's markdown output here…"
        rows={6}
        className="w-full bg-black/30 border border-[var(--neutral-800)] rounded px-3 py-2 text-xs outline-none focus:border-[var(--cyber-violet)] font-mono"
      />
      <div className="flex items-center gap-2">
        <button
          onClick={handleSave}
          disabled={saving}
          className="text-xs bg-[var(--cyber-violet)] text-black rounded px-3 py-1.5 font-medium disabled:opacity-50"
        >
          {saving ? "Saving…" : "Save"}
        </button>
        {savedAt && !saving && <span className="text-[11px] text-[var(--neutral-600)]">Saved</span>}
      </div>
      {raw.trim() && (
        <div className="border border-[var(--cyber-amber)]/40 bg-[var(--cyber-amber)]/5 rounded-lg p-3">
          <p className="text-[10px] uppercase tracking-wide text-[var(--cyber-amber)] mb-2">
            AI-estimated — not verified (§3.8)
          </p>
          <Markdown>{raw}</Markdown>
        </div>
      )}
    </div>
  );
}

// --- Dataset Analysis (§3.7) — dataset_analyst wraps sandbox_tester.py's
// existing E2B sandbox; no upload endpoint exists yet for it (it resolves
// a dataset path from task text or KEYS["dataset_path"], not a file
// picker — see agents/dataset_analyst.py's own docstring), so this hands
// off to a scoped sub-chat exactly like Sources' search does. Any chart
// the sandbox generates renders inline in that chat's AgentStepList step,
// not a second time here.
function DatasetPanel({ wsId, openScopedSubChat, openInDock }) {
  const [task, setTask] = useState("");
  const [running, setRunning] = useState(false);

  async function run() {
    if (!task.trim()) return;
    setRunning(true);
    try {
      const chatId = await openScopedSubChat(wsId, task.trim());
      await openInDock(chatId);
    } finally {
      setRunning(false);
    }
  }

  return (
    <div className="space-y-3">
      <p className="text-[11px] text-[var(--neutral-600)]">
        Describe the dataset (by filename) and the question — e.g. "analyze sales.csv: show the
        trend by region for Q3". There's no file-upload step for this yet; the dataset needs to
        already be a readable file the task can name.
      </p>
      <textarea
        value={task}
        onChange={(e) => setTask(e.target.value)}
        placeholder="analyze sales.csv: show the trend by region for Q3"
        rows={3}
        className="w-full bg-black/30 border border-[var(--neutral-800)] rounded px-3 py-2 text-xs outline-none focus:border-[var(--cyber-violet)]"
      />
      <button
        onClick={run}
        disabled={running || !task.trim()}
        className="text-xs bg-[var(--cyber-violet)] text-black rounded px-3 py-2 font-medium disabled:opacity-50 flex items-center gap-1"
      >
        <ExternalLink size={13} /> {running ? "Dispatching…" : "Run in chat"}
      </button>
    </div>
  );
}
