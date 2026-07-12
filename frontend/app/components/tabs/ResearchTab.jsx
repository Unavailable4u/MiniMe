"use client";
import { useState, useEffect, useMemo } from "react";
import { useSession } from "../../context/SessionContext";
import KnowledgeGraphView from "../KnowledgeGraphView";
import ExtractionTableView from "../research/ExtractionTableView";
import Markdown from "../Markdown";
import {
  Search, Share2, Table2, GitCompare, FlaskConical,
  RefreshCw, ExternalLink, FolderOpen,
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
// Extraction table / contradictions / consensus meter / dataset analysis
// are different: they're written to session-scoped memory-bus keys
// (KEYS["extraction_table"], KEYS["contradiction_candidates"], etc.), not
// to persistent per-workspace storage -- there's no GET endpoint that
// hands one of these back outside of the chat run that produced it. So,
// same "known simplification, flagged not hidden" the Notebooks README
// already used for Mind Map/Study (paste the role's raw output in), those
// three sub-tabs take pasted markdown from a completed chat run rather
// than auto-fetching "the latest one" -- there's no per-project "last
// extraction/contradiction output" store yet.

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

export default function ResearchTab({ onOpenChat }) {
  const { workspaces, fetchWorkspaceNodes, fetchGraphEdges, openScopedSubChat } = useSession();
  const [activeWsId, setActiveWsId] = useState(null);
  const [subTab, setSubTab] = useState("sources");

  useEffect(() => {
    if (!activeWsId && workspaces.length > 0) setActiveWsId(workspaces[0].id);
  }, [workspaces, activeWsId]);

  const activeWs = workspaces.find((w) => w.id === activeWsId) || null;

  return (
    <div className="flex h-full">
      {/* Project picker — a "research project" is just a workspace, same
          as a "notebook" is. No new container concept. */}
      <div className="w-56 shrink-0 border-r border-[var(--neutral-800)] flex flex-col">
        <div className="px-3 py-3 border-b border-[var(--neutral-800)]">
          <span className="text-xs font-medium text-[var(--neutral-400)]">Research projects</span>
        </div>
        <div className="flex-1 overflow-y-auto">
          {workspaces.length === 0 && (
            <p className="px-3 py-3 text-xs text-[var(--neutral-600)]">
              No projects yet. Create one from the chat sidebar's <FolderOpen size={11} className="inline" /> button, then come back here.
            </p>
          )}
          {workspaces.map((ws) => (
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
          ) : subTab === "sources" ? (
            <SourcesPanel wsId={activeWs.id} fetchWorkspaceNodes={fetchWorkspaceNodes} openScopedSubChat={openScopedSubChat} onOpenChat={onOpenChat} />
          ) : subTab === "graph" ? (
            <CitationGraphPanel wsId={activeWs.id} fetchWorkspaceNodes={fetchWorkspaceNodes} fetchGraphEdges={fetchGraphEdges} />
          ) : subTab === "extraction" ? (
            <ExtractionPanel />
          ) : subTab === "contradictions" ? (
            <ContradictionsPanel />
          ) : (
            <DatasetPanel wsId={activeWs.id} openScopedSubChat={openScopedSubChat} onOpenChat={onOpenChat} />
          )}
        </div>
      </div>
    </div>
  );
}

// --- Sources — Discovery (§3.3). "Search" hands off to a scoped sub-chat
// (same "click a node, open a scoped sub-chat" hand-off shape Notebooks'
// mind-map click uses) so academic_search + the reasoning roles it feeds
// (researcher/fact_checker/writer/editor) run exactly as the Panel would
// normally staff them. The list below reads back what that run wrote as
// nodes -- real, persisted, live-fetched data, not a paste box.
function SourcesPanel({ wsId, fetchWorkspaceNodes, openScopedSubChat, onOpenChat }) {
  const [query, setQuery] = useState("");
  const [sources, setSources] = useState([]);
  const [loading, setLoading] = useState(false);
  const [searching, setSearching] = useState(false);

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
      onOpenChat?.(chatId);
    } finally {
      setSearching(false);
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
          <div key={s.node_id} className="border border-[var(--neutral-800)] rounded-lg p-3">
            <div className="flex items-start justify-between gap-2">
              <span className="text-xs font-medium text-[var(--neutral-100)]">{s.title}</span>
              <div className="flex gap-1 shrink-0">
                {(s.tags || []).map((tag) => (
                  <span key={tag} className="text-[10px] uppercase tracking-wide text-[var(--cyber-violet)] bg-[var(--cyber-violet)]/10 rounded px-1.5 py-0.5">
                    {tag}
                  </span>
                ))}
              </div>
            </div>
            {s.content && (
              <p className="text-[11px] text-[var(--neutral-400)] mt-1.5 line-clamp-3">{s.content}</p>
            )}
          </div>
        ))}
      </div>
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

// --- Extraction Table (§3.5) — the one genuinely new UI component
// (ExtractionTableView), fed by pasting a completed extraction_table_builder
// run's markdown output (same GFM pipe-table eo/result_render.py already
// renders in chat).
function ExtractionPanel() {
  const [raw, setRaw] = useState("");
  return (
    <div className="space-y-3">
      <p className="text-[11px] text-[var(--neutral-600)]">
        Paste an extraction_table_builder run's markdown table output (from a chat message) below.
        There's no per-project "last extraction" store yet — this is a manual paste step, same as
        Notebooks' Mind Map/Study tabs.
      </p>
      <textarea
        value={raw}
        onChange={(e) => setRaw(e.target.value)}
        placeholder="| Title | Year | Sample Size | Methodology | ... |&#10;|---|---|---|---|---|&#10;| ... |"
        rows={6}
        className="w-full bg-black/30 border border-[var(--neutral-800)] rounded px-3 py-2 text-xs outline-none focus:border-[var(--cyber-violet)] font-mono"
      />
      <ExtractionTableView text={raw} />
    </div>
  );
}

// --- Contradictions & Consensus (§3.6, §3.8) — same paste pattern; the
// AI-estimated-stance banner is Definition-of-Done #6's requirement that
// approximated results (consensus meter especially) are never presented
// with a trained model's implied confidence.
function ContradictionsPanel() {
  const [raw, setRaw] = useState("");
  return (
    <div className="space-y-3">
      <p className="text-[11px] text-[var(--neutral-600)]">
        Paste a contradiction_detector or consensus_meter run's output below.
      </p>
      <textarea
        value={raw}
        onChange={(e) => setRaw(e.target.value)}
        placeholder="Paste the role's markdown output here…"
        rows={6}
        className="w-full bg-black/30 border border-[var(--neutral-800)] rounded px-3 py-2 text-xs outline-none focus:border-[var(--cyber-violet)] font-mono"
      />
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
function DatasetPanel({ wsId, openScopedSubChat, onOpenChat }) {
  const [task, setTask] = useState("");
  const [running, setRunning] = useState(false);

  async function run() {
    if (!task.trim()) return;
    setRunning(true);
    try {
      const chatId = await openScopedSubChat(wsId, task.trim());
      onOpenChat?.(chatId);
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
