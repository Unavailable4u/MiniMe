"use client";
import { useEffect, useMemo, useRef, useState, memo } from "react";
import { useSession } from "../../context/SessionContext";
import { useWorkspaces } from "../../context/WorkspacesContext";   // FIX — Item 2 concern split, slice 3 follow-up: this file was missed when workspaces/fetchWorkspaces moved out of useSession()
import { useChatList } from "../../context/ChatListContext";   // NEW — Item 2 concern split, slice 4
import IngestionDropzone from "../notebooks/IngestionDropzone";
import FlashcardFlipper from "../notebooks/FlashcardFlipper";
import WorkspaceStageIcons, { STAGE_THEME } from "../WorkspaceStageIcons"; // NEW — item #2: colored per-stage icon + per-project stage badges
import QuizRunner from "../notebooks/QuizRunner";
import StudyGuideViewer from "../notebooks/StudyGuideViewer";
import KnowledgeGraphView from "../KnowledgeGraphView";
import MermaidDiagram from "../MermaidDiagram";
import NotebooksGeneratePicker from "../notebooks/NotebooksGeneratePicker"; // NEW — Notebooks integration guide §4.1: picker/chip-confirmation "Generate" flow
import ConfirmDialog from "../ConfirmDialog";           // NEW — §2/§3 fix: was already built, unused here
import ManageWorkspaceModal from "../ManageWorkspaceModal"; // NEW — §3 fix: was already built (rename/delete/members), unused here
import WorkspaceChatPanel from "../WorkspaceChatPanel";  // NEW — §6.2: embedded chat + WorkingPanel dock
import { useWorkspaceDockActions, useLastActiveChatId } from "../../context/WorkspaceDockContext"; // NEW — step 3e; useLastActiveChatId added for issue #3 nested-chat row highlight
import {
  NotebookText, Plus, MessageSquareText, MessageSquare, FileText, GitBranch, Network,
  GraduationCap, Sparkles, X, Check, ChevronRight, ChevronLeft, BookMarked, Loader2, Layers,
  Trash2, MoreVertical, ArrowUpRight, Pencil, RefreshCw, ListChecks, RotateCcw,
  Wrench, Send, // NEW — Data Layer architecture §8a: Corrections tab
  GitCompare, // NEW — Data Layer architecture §8c: Patch Review tab
  Library, // NEW — Sources + Backlinks merged into one "Library" sub-tab
  Waypoints, // NEW — Mind Map + Workflows merged into one "Diagrams" sub-tab
  Lightbulb, // NEW — Facts + Clusters + Suggested notes merged into one "Insights" sub-tab
} from "lucide-react";

const SUB_TABS = [
  // CHANGED — Sources + Backlinks merged into one "Library" sub-tab
  // (same grouping move as Quiz/Flashcards -> Study). LibraryView below
  // renders both, side by side when the chat dock is collapsed (there's
  // room) and stacked — Backlinks on top, Sources below — when it's
  // open (there isn't).
  { id: "library", label: "Library", icon: Library },
  // CHANGED — Mind Map + Workflows merged into one "Diagrams" sub-tab
  // (same grouping move as Sources/Backlinks -> Library). DiagramsView
  // below always stacks them — Mind Map on top, Workflows below — never
  // side by side, since Mind Map alone can already be as wide/tall as a
  // whole notebook's worth of content and needs the room to itself.
  { id: "diagrams", label: "Diagrams", icon: Waypoints },
  { id: "study", label: "Study", icon: GraduationCap },
  // CHANGED — Facts + Clusters + Suggested notes merged into one
  // "Insights" sub-tab (same grouping move as Library/Diagrams above).
  // InsightsView below groups Facts and Clusters together on one side
  // (they're both "structured things the system pulled out of your
  // sources") and Suggested notes on the other, side by side when the
  // dock is collapsed and stacked — Suggested notes on top, Facts &
  // Clusters below — when it's open, same interactive layout Library
  // uses for Sources/Backlinks.
  { id: "insights", label: "Insights", icon: Lightbulb },
  // CHANGED — Corrections + Patch Review merged into one "Corrections"
  // sub-tab (same grouping move as Library/Diagrams/Insights above).
  // CorrectionsView below renders both the capture form (§8a, wired to
  // §8b's locator) and the before/after review queue (§8c) unchanged,
  // just nested under one tab instead of two.
  { id: "corrections", label: "Corrections", icon: Wrench },
];

// NEW — §4 fix: persist which notebook and sub-tab were selected, same
// localStorage pattern AppShell.jsx uses for ACTIVE_TAB_KEY, so a page
// refresh doesn't drop you back to "no notebook selected."
const SELECTED_NOTEBOOK_KEY = "minime_notebooks_selected_id";
const SUB_TAB_KEY = "minime_notebooks_subtab";
// NEW — bug audit §8 ("unread/new content" dots). Per-workspace,
// client-side "when did I last look at this sub-tab" store — the
// cheap/no-backend-change option the audit guide flags, since this is
// genuinely personal/ephemeral state (like the workflow-checklist
// progress in §7), not notebook content that needs to sync across
// devices/users. One JSON blob per workspace: { [subTabId]: isoString }.
const LAST_VIEWED_PREFIX = "minime_notebooks_lastviewed";
// NEW — bug audit §7 follow-up ("click a step to check it off"): per the
// guide's own suggested simplest-durable option, checklist progress is
// genuinely personal/ephemeral (like the dot-viewed state above), not
// notebook content that needs to sync across devices/users, so it's
// localStorage rather than a panel_content/backend write. Keyed by
// workflow title rather than an index since Regenerate can reorder or
// drop workflows — a title match is more likely to still mean "the same
// procedure" than a positional one.
const WORKFLOW_PROGRESS_PREFIX = "minime_notebooks_workflow_progress";
// Only these four sub-tabs get a dot. "Suggested notes" and "Clusters"
// already carry a pending-count badge (see the SUB_TABS.map below) which
// serves the same "there's something to look at" purpose — stacking a
// dot on top would just be visual noise for those two. "Sources" has no
// generated-content concept to go stale, and "Facts" candidates
// (eo/workspace_facts.py's propose_fact) are flagged dormant in that
// module's own comments — no agent calls it yet, so there's nothing to
// diff there today. Revisit if that changes.
const UNREAD_DOT_TABS = ["diagrams", "library", "study"];
// Which eo/panel_content.py panel_key(s) back each dot-eligible sub-tab
// that's driven by panel_content specifically (backlinks/clusters/
// candidates aren't — they compare against graph_edges/candidate
// timestamps directly instead, see latestTabTimestamp below).
const TAB_PANEL_KEYS = {
  diagrams: ["mindmap", "suggested_route", "suggested_workflows"],
  study: ["study_flashcards", "study_quiz", "study_guide"],
};
// NEW — §6.2: separate collapse key from WorkspaceChatPanel's own internal
// WORKING_PANEL_KEY — this one folds away the *whole* dock (chat +
// WorkingPanel together), same "own toggle, own storage key" pattern the
// left ChatSidebar already uses for itself.
const CHAT_DOCK_KEY = "minime_notebooks_chatdock_collapsed";
// NEW — collapsible project-picker sidebar, same pattern as the chat
// dock's own collapse above.
const PROJECTS_KEY = "minime_notebooks_projects_collapsed";
const PROMOTE_TARGETS = ["research", "plan", "build", "test", "growth"];
const PROMOTE_LABELS = {
  research: "Research",
  plan: "Plan",
  build: "Build",
  test: "Test",
  growth: "Growth",
};

function timeAgo(iso) {
  if (!iso) return "";
  try { return new Date(iso).toLocaleDateString(); } catch { return ""; }
}

// --- Sources sub-view ------------------------------------------------------

// Groups flat source nodes into { root, children[] } trees using the
// "same_source" edges eo/graph_edges.py already writes when a source
// (e.g. a PDF) splits into multiple section nodes — see
// agents/source_ingestor.py's write_ingested_source(): every section
// after the first gets a same_source edge pointing back at the first
// section, which is exactly the parent/child relationship this needs.
// A source that never split (edges has nothing for it) just becomes a
// childless root, same as before.
function groupSourceNodes(nodes, edges) {
  const parentOf = new Map(); // child node_id -> root node_id
  for (const e of edges) {
    if (e.relation !== "same_source") continue;
    const fromId = (e.from_node_id || "").split(":", 3)[2];
    const toId = (e.to_node_id || "").split(":", 3)[2];
    if (fromId && toId) parentOf.set(fromId, toId);
  }
  const byId = new Map(nodes.map((n) => [n.node_id, n]));
  const childrenOf = new Map(); // root node_id -> [child nodes]
  for (const [childId, rootId] of parentOf) {
    if (!byId.has(childId) || !byId.has(rootId)) continue;
    if (!childrenOf.has(rootId)) childrenOf.set(rootId, []);
    childrenOf.get(rootId).push(byId.get(childId));
  }
  const groups = [];
  for (const n of nodes) {
    if (parentOf.has(n.node_id)) continue; // it's a child, rendered under its root
    const children = (childrenOf.get(n.node_id) || []).sort((a, b) =>
      (a.title || "").localeCompare(b.title || "", undefined, { numeric: true })
    );
    groups.push({ root: n, children });
  }
  return groups;
}

// A root's title is written as "{title} — {heading}" when it has
// siblings (write_ingested_source() above) — e.g. "Notes — Page 1".
// Strip that trailing " — <heading>" for the group header so it reads
// as the source's own name rather than repeating "— Page 1" on the
// row that's about to show a "N pages" child list right underneath it.
// Falls back to the raw title when there's nothing to strip (a
// single-section source, or a root whose real title happens not to
// contain " — ").
function groupDisplayTitle(root, hasChildren) {
  const title = root.title || root.node_id;
  if (!hasChildren) return title;
  const idx = title.lastIndexOf(" — ");
  return idx > 0 ? title.slice(0, idx) : title;
}

function SourceRow({ node, onSelectNode, onRequestDelete, onRename, indent }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(node.title || node.node_id);
  const [saving, setSaving] = useState(false);

  async function save() {
    const trimmed = draft.trim();
    if (!trimmed || trimmed === node.title) { setEditing(false); setDraft(node.title || node.node_id); return; }
    setSaving(true);
    try {
      await onRename(node, trimmed);
      setEditing(false);
    } catch (err) {
      alert(`Rename failed: ${err.message || err}`); // NEW — simple inline failure surface, no toast system in this file yet
    } finally {
      setSaving(false);
    }
  }

  if (editing) {
    return (
      <div className={`flex items-center gap-2 px-3 py-2 rounded-lg border border-[var(--cyber-cyan)] ${indent ? "ml-5" : ""}`}>
        <input
          autoFocus
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") save();
            if (e.key === "Escape") { setEditing(false); setDraft(node.title || node.node_id); }
          }}
          disabled={saving}
          className="flex-1 min-w-0 bg-black/30 border border-[var(--neutral-800)] rounded px-2 py-1 text-xs outline-none focus:border-[var(--cyber-cyan)]"
        />
        <button onClick={save} disabled={saving} className="shrink-0 text-[var(--neutral-400)] hover:text-green-400 disabled:opacity-50">
          <Check size={13} />
        </button>
        <button onClick={() => { setEditing(false); setDraft(node.title || node.node_id); }} disabled={saving} className="shrink-0 text-[var(--neutral-400)] hover:text-red-400 disabled:opacity-50">
          <X size={13} />
        </button>
      </div>
    );
  }

  return (
    <div
      className={`group w-full flex items-center justify-between gap-2 px-3 py-2 rounded-lg border border-[var(--neutral-800)] hover:border-[var(--neutral-700)] ${indent ? "ml-5" : ""}`}
    >
      <button
        onClick={() => onSelectNode(node)}
        className="flex-1 min-w-0 flex items-center justify-between gap-2 text-left"
      >
        <span className="text-xs text-[var(--neutral-200)] truncate">{node.title || node.node_id}</span>
        <span className="text-[10px] text-[var(--neutral-600)] shrink-0">{timeAgo(node.created_at)}</span>
      </button>
      <button
        onClick={(e) => { e.stopPropagation(); setDraft(node.title || node.node_id); setEditing(true); }}
        title="Rename source"
        className="shrink-0 text-[var(--neutral-600)] opacity-0 group-hover:opacity-100 hover:text-[var(--cyber-cyan)]"
      >
        <Pencil size={13} />
      </button>
      <button
        onClick={() => onRequestDelete(node)}
        title="Delete source"
        className="shrink-0 text-[var(--neutral-600)] opacity-0 group-hover:opacity-100 hover:text-red-400"
      >
        <Trash2 size={13} />
      </button>
    </div>
  );
}

function SourceGroup({ group, onSelectNode, onRequestDelete, onRename }) {
  const [open, setOpen] = useState(false);
  const hasChildren = group.children.length > 0;

  if (!hasChildren) {
    return <SourceRow node={group.root} onSelectNode={onSelectNode} onRequestDelete={onRequestDelete} onRename={onRename} />;
  }

  return (
    <div className="space-y-1">
      <div className="group w-full flex items-center justify-between gap-2 px-3 py-2 rounded-lg border border-[var(--neutral-800)] hover:border-[var(--neutral-700)]">
        <button
          onClick={() => setOpen((v) => !v)}
          className="flex-1 min-w-0 flex items-center gap-1.5 text-left"
        >
          <ChevronRight size={13} className={`shrink-0 text-[var(--neutral-600)] transition-transform ${open ? "rotate-90" : ""}`} />
          <span className="text-xs text-[var(--neutral-200)] truncate">{groupDisplayTitle(group.root, true)}</span>
          <span className="text-[10px] text-[var(--neutral-600)] shrink-0">
            {group.children.length + 1} pages
          </span>
        </button>
        <button
          onClick={() => onSelectNode(group.root)}
          className="text-[10px] text-[var(--neutral-600)] shrink-0 hover:text-[var(--neutral-300)]"
        >
          {timeAgo(group.root.created_at)}
        </button>
        <button
          onClick={() => onRequestDelete({ ...group.root, _siblingCount: group.children.length })}
          title="Delete source"
          className="shrink-0 text-[var(--neutral-600)] opacity-0 group-hover:opacity-100 hover:text-red-400"
        >
          <Trash2 size={13} />
        </button>
      </div>
      {open && (
        <div className="space-y-1">
          <SourceRow node={group.root} onSelectNode={onSelectNode} onRequestDelete={onRequestDelete} onRename={onRename} indent />
          {group.children.map((c) => (
            <SourceRow key={c.node_id} node={c} onSelectNode={onSelectNode} onRequestDelete={onRequestDelete} onRename={onRename} indent />
          ))}
        </div>
      )}
    </div>
  );
}

function SourcesView({ workspaceId, nodes, edges, loading, onIngested, onSelectNode, onDeleteNode, onRenameNode }) {
  const [pendingDelete, setPendingDelete] = useState(null); // NEW — §2 fix: node awaiting delete confirmation
  const [deleting, setDeleting] = useState(false);

  async function confirmDelete() {
    if (!pendingDelete) return;
    setDeleting(true);
    try {
      await onDeleteNode(pendingDelete.node_id);
      setPendingDelete(null);
    } finally {
      setDeleting(false);
    }
  }

  const groups = groupSourceNodes(nodes, edges || []);

  return (
    <div className="space-y-4">
      <IngestionDropzone workspaceId={workspaceId} onIngested={onIngested} />
      <div>
        <div className="text-[10px] uppercase tracking-wide text-[var(--neutral-600)] mb-2">
          {loading ? "Loading…" : `${nodes.length} source${nodes.length === 1 ? "" : "s"}`}
        </div>
        <div className="space-y-1">
          {groups.map((g) => (
            <SourceGroup key={g.root.node_id} group={g} onSelectNode={onSelectNode} onRequestDelete={setPendingDelete} onRename={onRenameNode} />
          ))}
          {!loading && nodes.length === 0 && (
            <p className="text-xs text-[var(--neutral-600)]">No sources ingested yet — drop a file or paste a link above.</p>
          )}
        </div>
      </div>
      <ConfirmDialog
        open={!!pendingDelete}
        title="Delete source"
        message={
          `Delete "${pendingDelete?.title || pendingDelete?.node_id}"` +
          (pendingDelete?._siblingCount
            ? ` and its ${pendingDelete._siblingCount} other page${pendingDelete._siblingCount === 1 ? "" : "s"}?`
            : "?") +
          ` This also removes any links to it in Backlinks and Clusters, and clears generated Mind Map/Study/Facts/Clusters so they can be regenerated from what's left.`
        }
        confirmLabel={deleting ? "Deleting…" : "Delete"}
        onConfirm={confirmDelete}
        onCancel={() => setPendingDelete(null)}
      />
    </div>
  );
}

// --- Mind Map sub-view ------------------------------------------------------
// §4.7: extends MermaidDiagram.jsx (currently static, non-interactive
// SVG) with click handling that opens a scoped sub-chat. The mind map's
// own Mermaid source comes from the `mapper` role's chat output — pasted
// in here, then saved to eo/panel_content.py under panel_key "mindmap"
// so it survives a reload or a sub-tab switch instead of vanishing with
// local component state (the previous behavior).

// NEW — guide §6.5 (Phase 2): manual paste is gone. `generate_mindmap`
// (agents/mind_mapper.py, via api/server.py's notebooks_generate
// "mindmap" target) is the only writer of panel_content's "mindmap" key
// now — this subtab is a pure viewer plus a Regenerate action that
// re-runs the same call and overwrites the saved content, same
// last-write-wins posture as every other panel_content write (guide §9
// leans silent-overwrite, no separate confirmation step here).
// NEW — chat audit fix: agents/mind_mapper.py:generate_suggested_route()
// existed but had no caller anywhere — this view is that missing wiring.
// Two modes sharing one viewer: "mindmap" (the existing topic-overview
// diagram) and "route" (agents/backlink_detector.py's own
// "prerequisite-of" edges rendered as a study-order flowchart — "what
// should I read before what"). Same MermaidDiagram, same Regenerate
// action, same click-to-sub-chat behavior; only the fetched panel_key
// and the Generate target name change between the two.
const MINDMAP_MODES = [
  { id: "mindmap", label: "Topic Map", panelKey: "mindmap", target: "mindmap" },
  { id: "route", label: "Study Path", panelKey: "suggested_route", target: "suggested_route" },
];

function MindMapView({ workspaceId, onTopicSelect, fetchPanelContent, generateNotebooks }) {
  const [mode, setMode] = useState("mindmap");
  const [content, setContent] = useState("");
  const [updatedAt, setUpdatedAt] = useState(null);
  const [loading, setLoading] = useState(true);
  const [regenerating, setRegenerating] = useState(false);
  const [error, setError] = useState(null);

  const activeMode = MINDMAP_MODES.find((m) => m.id === mode) || MINDMAP_MODES[0];

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetchPanelContent(workspaceId, activeMode.panelKey).then((saved) => {
      if (cancelled) return;
      setContent(saved?.content || "");
      setUpdatedAt(saved?.updated_at || null);
      setLoading(false);
    });
    return () => { cancelled = true; };
  }, [workspaceId, fetchPanelContent, activeMode.panelKey]);

  async function handleRegenerate() {
    setRegenerating(true);
    setError(null);
    try {
      const { branches } = await generateNotebooks(workspaceId, [activeMode.target], null);
      const branch = branches.find((b) => b.panel_key === activeMode.target);
      if (branch?.status === "error") throw new Error(branch.error || "Generation failed");
      setContent(branch?.result?.content || "");
      setUpdatedAt(branch?.result?.updated_at || null);
    } catch (err) {
      setError(String(err.message || err));
    } finally {
      setRegenerating(false);
    }
  }

  const emptyHint = mode === "mindmap"
    ? "No mind map yet — Generate reads this notebook's sources and proposes one."
    : "No study path yet — Generate looks for \"study X before Y\" links Backlinks has already found between topics.";

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-1 rounded-lg border border-[var(--neutral-800)] p-0.5 w-fit">
        {MINDMAP_MODES.map((m) => (
          <button
            key={m.id}
            onClick={() => { if (!regenerating) setMode(m.id); }}
            className={`text-[11px] px-2.5 py-1 rounded-md font-medium transition-colors ${
              mode === m.id
                ? "bg-[var(--accent)] text-[var(--accent-text)]"
                : "text-[var(--neutral-400)] hover:text-[var(--neutral-200)]"
            }`}
          >
            {m.label}
          </button>
        ))}
      </div>

      <div className="flex items-center justify-between gap-2">
        <p className="text-xs text-[var(--neutral-500)]">
          {content
            ? "Click any node to generate a step-by-step workflow for that topic."
            : emptyHint}
        </p>
        <div className="flex items-center gap-2 shrink-0">
          {updatedAt && !regenerating && (
            <span className="text-[10px] text-[var(--neutral-600)]">Generated {timeAgo(updatedAt)}</span>
          )}
          <button
            onClick={handleRegenerate}
            disabled={regenerating || loading}
            className="flex items-center gap-1.5 text-xs bg-[var(--accent)] text-[var(--accent-text)] rounded px-3 py-1.5 font-medium disabled:opacity-50"
          >
            {regenerating ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
            {content ? "Regenerate" : "Generate"}
          </button>
        </div>
      </div>
      {error && <p className="text-[11px] text-red-400">{error}</p>}
      {loading ? (
        <div className="text-xs text-[var(--neutral-600)] flex items-center gap-1.5"><Loader2 size={12} className="animate-spin" /> Loading…</div>
      ) : content ? (
        <div className="rounded-lg border border-[var(--neutral-800)] bg-black/30 p-4">
          <MermaidDiagram
            mermaidText={content}
            onNodeClick={(label) => onTopicSelect?.(label)}
            hideSourceOnFail /* NEW — bug #6a fix */
            showControls /* NEW — §7 refinements #5/#6: zoom/pan + export as image */
            maxHeight={520}
            exportFilename={mode === "mindmap" ? "mind-map" : "study-path"}
          />
        </div>
      ) : (
        !regenerating && (
          <div className="rounded-lg border border-dashed border-[var(--neutral-800)] p-8 text-center text-xs text-[var(--neutral-600)]">
            Nothing generated yet for this notebook.
          </div>
        )
      )}
    </div>
  );
}

// --- Backlinks sub-view ------------------------------------------------------
// §4.7: reuses KnowledgeGraphView.jsx (Part 0/3) — third domain to use it,
// no new graph renderer.
//
// CHANGED — Overlap/Live-Viz guide, "replace entirely" decision: this used
// to show the primary structural node/edge graph (same_source/clustered_with/
// references + concept_linker's free-form relations, driven by the two
// buttons below). It now shows eo/secondary_data.py's topic tree instead
// (GET /api/workspaces/{ws_id}/topics/graph) — the tree Source Manager/
// Backlink Detector build automatically on every upload, no manual scan
// needed. "Quick title-match scan" and "Regenerate concept graph" are
// DROPPED from this view rather than kept as dead buttons: both write to
// the structural graph this subtab no longer renders, so triggering them
// here produced no visible change. If that structural graph still needs a
// manual-trigger UI, its natural home is the Sources subtab now, not here
// -- flagging rather than assuming, since that's a separate call.
//
// `nodeSummaries` here is topic summaries built client-side from `nodes`
// below (each topic node already carries its own `summary`), not
// eo/node_summaries.py's map — passing it still turns on
// KnowledgeGraphView's "concept-graph mode" (rationale panel + brighter
// non-structural edges), which is exactly the click-to-see-summary/
// relation behavior this view wants for a topic node.
function BacklinksView({ nodes, edges, pulsingIds, loading, onSelectNode }) {
  const topicSummaries = useMemo(
    () => Object.fromEntries((nodes || []).map((n) => [n.node_id, n.summary || ""])),
    [nodes],
  );

  return (
    <div className="space-y-3">
      <p className="text-xs text-[var(--neutral-500)]">
        Topic tree for this notebook — hover a link to see its relation, click a node to see its summary and why it's connected.
      </p>
      <div className="h-[420px] rounded-lg border border-[var(--neutral-800)] overflow-hidden">
        {loading ? (
          <div className="h-full flex items-center justify-center text-xs text-[var(--neutral-600)]">Loading…</div>
        ) : nodes.length === 0 ? (
          <div className="h-full flex items-center justify-center text-xs text-[var(--neutral-600)]">Nothing to graph yet.</div>
        ) : (
          <KnowledgeGraphView
            nodes={nodes}
            edges={edges}
            nodeSummaries={topicSummaries}
            pulsingIds={pulsingIds}
            onSelectNode={onSelectNode}
          />
        )}
      </div>
    </div>
  );
}


// --- Library (Sources + Backlinks) --------------------------------------
// NEW — Sources and Backlinks merged into one "Library" sub-tab (same
// grouping move as Quiz/Flashcards -> Study), rendering both SourcesView
// and BacklinksView unchanged rather than rebuilding either.
//
// Layout responds to `dockOpen` (NotebooksTab passes `!chatDockCollapsed`):
// when the chat dock is collapsed there's real width to spare on the
// right, so Sources/Backlinks sit side by side (Sources left — same side
// it already lived on — Backlinks right); once the dock opens and eats
// that width back, side-by-side would force the page to scroll
// sideways (the exact problem the chat dock's own stacked layout was
// built to avoid — see WorkspaceChatPanel's `stacked` prop), so this
// switches to stacked instead, Backlinks on top since it's the shorter
// of the two and Sources (with its drop-zone) reads better with the
// full column beneath it.
function LibraryView({
  workspaceId, nodes, edges, loading, onIngested, onSelectNode, onDeleteNode, onRenameNode,
  topicNodes, topicEdges, topicPulsingIds, dockOpen,
}) {
  const sources = (
    <SourcesView
      workspaceId={workspaceId}
      nodes={nodes}
      edges={edges}
      loading={loading}
      onIngested={onIngested}
      onSelectNode={onSelectNode}
      onDeleteNode={onDeleteNode}
      onRenameNode={onRenameNode}
    />
  );
  const backlinks = (
    <BacklinksView
      nodes={topicNodes}
      edges={topicEdges}
      pulsingIds={topicPulsingIds}
      loading={loading}
      onSelectNode={onSelectNode}
    />
  );

  if (dockOpen) {
    // Stacked — Backlinks on top, Sources below.
    return (
      <div className="flex flex-col gap-6">
        <div className="min-w-0">
          <h3 className="text-[10px] uppercase tracking-wide text-[var(--neutral-600)] mb-2 flex items-center gap-1.5">
            <GitBranch size={11} /> Backlinks
          </h3>
          {backlinks}
        </div>
        <div className="min-w-0 border-t border-[var(--neutral-800)] pt-6">
          <h3 className="text-[10px] uppercase tracking-wide text-[var(--neutral-600)] mb-2 flex items-center gap-1.5">
            <FileText size={11} /> Sources
          </h3>
          {sources}
        </div>
      </div>
    );
  }

  // Side by side — Sources left, Backlinks right.
  return (
    <div className="flex gap-6 items-start">
      <div className="flex-1 min-w-0">
        <h3 className="text-[10px] uppercase tracking-wide text-[var(--neutral-600)] mb-2 flex items-center gap-1.5">
          <FileText size={11} /> Sources
        </h3>
        {sources}
      </div>
      <div className="flex-1 min-w-0 border-l border-[var(--neutral-800)] pl-6">
        <h3 className="text-[10px] uppercase tracking-wide text-[var(--neutral-600)] mb-2 flex items-center gap-1.5">
          <GitBranch size={11} /> Backlinks
        </h3>
        {backlinks}
      </div>
    </div>
  );
}


// --- Workflows sub-view ------------------------------------------------------
// Bug audit §7 (new feature): agents/workflow_suggester.py finds 0-4
// step-by-step procedures described in the notebook's sources and
// diagrams each one — a genuinely different job from Mind Map (one
// whole-notebook overview) and Backlinks (relationships BETWEEN
// sources). Static v1: renders each returned workflow as its own card
// (title, description, flowchart, plain steps list). Per the audit
// guide's own build order, click-to-check-off steps are a deliberate
// follow-up on top of this, not bundled in — get the content right
// first.
// Same loading/Regenerate/empty-state shape as MindMapView, but the
// saved content here is a JSON blob (api/server.py's _generate_workflows
// json.dumps()s agents/workflow_suggester.py's {"workflows": [...]}
// result into panel_content, same as every other structured-not-Markdown
// panel would), so this view parses on load instead of treating it as
// plain text.
// NEW — bug audit §7 follow-up: click-to-check-off. `completedSteps` is a
// Set<string> of step ids, state lives here (not in MermaidDiagram) so it
// can be persisted per workflow and reset independently of the diagram
// re-rendering. Loaded from/saved to localStorage keyed by workspace +
// workflow title (see WORKFLOW_PROGRESS_PREFIX comment above) — a single
// study session's progress that's fine to lose in private browsing, not
// notebook content that needs a backend round trip.
function WorkflowCard({ workflow, workspaceId, onOpenSubChat }) {
  const storageKey = `${WORKFLOW_PROGRESS_PREFIX}:${workspaceId}:${workflow.title}`;
  const [completedSteps, setCompletedSteps] = useState(() => new Set());

  // NEW — Notebooks Chat-First refinement, step 6.11.a: verification only,
  // no behavior change. Confirms what agents/workflow_suggester.py's
  // build_topic_workflow() actually put on `workflow` by the time it
  // reaches this component, for BOTH paths that can hand WorkflowCard a
  // workflow prop — a live Mind Map click (topic_workflow_endpoint's raw
  // `return workflow`) and a hydrated one (api/server.py's
  // topic_workflows-panel merge, step 7.3). topic_id can legitimately be
  // None (build_topic_workflow()'s own generic-fallback-on-no-match case)
  // — topic_key is the one that's never None and is what 6.11.b/6.11.c
  // will actually thread through onOpenSubChat -> sendTask -> /api/task.
  // Remove this once 6.11.b lands and the values are visibly flowing
  // through the real pipeline instead.
  useEffect(() => {
    console.debug("[6.11.a] WorkflowCard topic scope check", {
      title: workflow.title,
      topic_id: workflow.topic_id ?? null,
      topic_key: workflow.topic_key ?? null,
    });
  }, [workflow.title, workflow.topic_id, workflow.topic_key]);

  useEffect(() => {
    try {
      const raw = localStorage.getItem(storageKey);
      setCompletedSteps(new Set(raw ? JSON.parse(raw) : []));
    } catch {
      setCompletedSteps(new Set());
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only re-read on identity change (workspace/title), not every render
  }, [storageKey]);

  function persist(next) {
    setCompletedSteps(next);
    try {
      localStorage.setItem(storageKey, JSON.stringify([...next]));
    } catch {
      // localStorage can throw (private browsing quota, etc.) — progress
      // just won't survive a reload in that case, not worth surfacing.
    }
  }

  function toggleStep(id) {
    const next = new Set(completedSteps);
    if (next.has(id)) next.delete(id); else next.add(id);
    persist(next);
  }

  function resetProgress() {
    persist(new Set());
  }

  const stepTypes = Object.fromEntries((workflow.steps || []).map((s) => [s.id, s.type]));
  // Only "step" entries count toward the checklist — a decision node
  // isn't something you complete, it's a branch point (guide refinement
  // #2), so it's excluded from both the denominator and "current step".
  const checkableSteps = (workflow.steps || []).filter((s) => s.type !== "decision");
  const doneCount = checkableSteps.filter((s) => completedSteps.has(s.id)).length;
  const currentStep = checkableSteps.find((s) => !completedSteps.has(s.id));

  return (
    <div className="rounded-lg border border-[var(--neutral-800)] p-3 space-y-2">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h4 className="text-sm font-medium text-[var(--neutral-200)]">{workflow.title}</h4>
          {workflow.description && (
            <p className="text-xs text-[var(--neutral-500)] mt-0.5">{workflow.description}</p>
          )}
        </div>
        {checkableSteps.length > 0 && (
          <div className="flex items-center gap-1.5 shrink-0 text-[10px] text-[var(--neutral-500)]">
            <span>{doneCount} of {checkableSteps.length} done</span>
            {doneCount > 0 && (
              <button
                onClick={resetProgress}
                title="Reset progress"
                className="text-[var(--neutral-600)] hover:text-[var(--neutral-300)]"
              >
                <RotateCcw size={11} />
              </button>
            )}
          </div>
        )}
      </div>
      <div className="rounded-md border border-[var(--neutral-800)] bg-black/20 p-2">
        <MermaidDiagram
          mermaidText={workflow.mermaid}
          hideSourceOnFail
          completedSteps={completedSteps}
          onToggleStep={toggleStep}
          stepTypes={stepTypes}
          currentStepId={currentStep?.id || null}
          showControls /* NEW — §7 refinements #5/#6: zoom/pan + export as image */
          maxHeight={340}
          exportFilename={workflow.title}
        />
      </div>
      {workflow.steps?.length > 0 && (
        <ol className="space-y-1 text-xs text-[var(--neutral-400)] list-decimal list-inside">
          {workflow.steps.map((step) => (
            <li
              key={step.id}
              className={`flex items-center gap-1.5 ${step.type === "decision" ? "italic" : ""} ${
                completedSteps.has(step.id) ? "line-through opacity-50" : ""
              } ${step.id === currentStep?.id ? "text-[var(--cyber-cyan)]" : ""}`}
            >
              <span className="flex-1">{step.label}</span>
              {step.type !== "decision" && (
                <>
                  <button
                    onClick={() =>
                      onOpenSubChat?.(workspaceId, `Explain this step in more detail: "${step.label}" (part of the "${workflow.title}" procedure), using this notebook's sources.`)
                    }
                    title="Ask about this step"
                    className="shrink-0 text-[var(--neutral-600)] hover:text-[var(--cyber-cyan)]"
                  >
                    <MessageSquareText size={11} />
                  </button>
                  {/* NEW — Notebooks Chat-First refinement, Phase 6 step
                      6.10: click alone (no typed text) starts a scoped
                      sub-chat already primed to work through the step,
                      not just explain it -- reuses the exact same
                      onOpenSubChat -> handleOpenSubChat ->
                      openScopedSubChat -> sendTask path as "Ask about
                      this step" above, just a different synthetic
                      message. Steps 6.11 (pull sources/notes scoped to
                      the topic into that chat's response) and 6.12 (mark
                      this step "ongoing" in eo/study_progress.py on this
                      same click) build on top of this wiring next. */}
                  <button
                    onClick={() =>
                      // NEW — step 6.11.b: thread topic_key through as an
                      // optional 3rd arg (6.11.a confirmed topic_key is the
                      // field that's never None). Nothing downstream reads
                      // it yet — handleOpenSubChat/openScopedSubChat just
                      // pass it along one hop further. 6.11.c is what
                      // actually puts it on the wire to /api/task.
                      onOpenSubChat?.(workspaceId, `Let's work through: "${step.label}"`, workflow.topic_key ?? null)
                    }
                    title="Work through this step"
                    className="shrink-0 text-[var(--neutral-600)] hover:text-[var(--cyber-cyan)]"
                  >
                    <Send size={11} />
                  </button>
                </>
              )}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

// CHANGED — step 7: WorkflowsView no longer fetches or generates anything
// itself. Whole-notebook "workflows" generation is gone (step 3 dropped it
// from NOTEBOOKS_GENERATE_TARGETS); workflows are now built one topic at a
// time from a Mind Map node click (agents/workflow_suggester.py's
// build_topic_workflow, step 1/2/4). DiagramsView (step 8) owns the actual
// per-topic requests and state; this component is a pure renderer over
// whatever it's handed.
//
// `results` is an ordered array of per-topic entries:
//   { label: string, status: "loading" | "error" | "done", workflow?: {...}, error?: string }
// — one entry per topic node clicked so far this session, most-recent
// first (DiagramsView's call). Nothing here reads from or writes to
// panel_content; these are ephemeral, same as the mind map click itself.
function WorkflowsView({ workspaceId, results, onOpenSubChat, onDismiss }) {
  if (!results?.length) {
    return (
      <div className="rounded-lg border border-dashed border-[var(--neutral-800)] p-8 text-center text-xs text-[var(--neutral-600)]">
        Click any node in the Mind Map above to generate a step-by-step workflow for that topic.
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <p className="text-xs text-[var(--neutral-500)]">
        Step-by-step workflows generated for the topics you've clicked in the Mind Map above.
      </p>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        {results.map((r) => (
          <div key={r.label} className="space-y-1.5">
            <div className="flex items-center justify-between gap-1.5">
              <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-wide text-[var(--neutral-600)]">
                <ListChecks size={11} /> {r.label}
              </div>
              {/* NEW — step 9: dismiss a card without waiting for it to be
                  bumped off by new clicks; harmless for a loading card too
                  (it just abandons the in-flight result on arrival, see
                  DiagramsView's pendingLabelsRef guard). */}
              <button
                onClick={() => onDismiss?.(r.label)}
                title="Dismiss"
                className="shrink-0 text-[var(--neutral-600)] hover:text-[var(--neutral-300)]"
              >
                <X size={11} />
              </button>
            </div>
            {r.status === "loading" && (
              <div className="rounded-lg border border-[var(--neutral-800)] p-3 text-xs text-[var(--neutral-600)] flex items-center gap-1.5">
                <Loader2 size={12} className="animate-spin" /> Generating workflow…
              </div>
            )}
            {r.status === "error" && (
              <div className="rounded-lg border border-[var(--neutral-800)] p-3 text-xs text-red-400">
                {r.error || "Couldn't generate a workflow for this topic."} Click the node again to retry.
              </div>
            )}
            {r.status === "done" && r.workflow && (
              <WorkflowCard workflow={r.workflow} workspaceId={workspaceId} onOpenSubChat={onOpenSubChat} />
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

// --- Progress board (Not Started / Ongoing / Done) --------------------
// NEW — Notebooks Chat-First refinement, Phase 6 step 6.9. Board view
// for eo/study_progress.py, sourced from the Mind Map's own topic list
// (`topicNodes` — the same GET /api/workspaces/{ws_id}/topics/graph
// data BacklinksView above already renders) rather than keeping a
// second list of "what topics exist." A topic with no
// eo/study_progress.py record at all just renders in the Not Started
// column — the same sparse-storage, implicit-default contract
// study_progress.get_progress() itself documents, mirrored here
// client-side instead of trusting the server to pre-populate every
// topic_id.
//
// Status moves automatically off two real signals already wired
// server-side (build_topic_workflow()'s first-click hook, step 6.6; a
// passing quiz attempt, step 6.7) — this board just reflects those. The
// per-card buttons below are the manual-override path (step 6.5's PUT
// route, via setWorkspaceProgress from SessionContext) for anything
// those signals miss, same "no confirmation needed, low-stakes and
// reversible" posture the guide already settled on for markTopicDone()
// (step 6.8)'s chat tool.
const PROGRESS_COLUMNS = [
  { status: "not_started", label: "Not Started" },
  { status: "ongoing", label: "Ongoing" },
  { status: "done", label: "Done" },
];

function ProgressCard({ topic, onSetStatus, busy }) {
  return (
    <div className="rounded-lg border border-[var(--neutral-800)] p-2.5 space-y-1.5">
      <div className="text-xs text-[var(--neutral-200)] truncate" title={topic.title}>{topic.title}</div>
      <div className="flex flex-wrap items-center gap-1">
        {PROGRESS_COLUMNS.filter((c) => c.status !== topic.status).map((c) => (
          <button
            key={c.status}
            onClick={() => onSetStatus(topic.topicId, c.status)}
            disabled={busy}
            className="text-[10px] px-1.5 py-0.5 rounded border border-[var(--neutral-800)] text-[var(--neutral-500)] hover:text-[var(--cyber-cyan)] hover:border-[var(--cyber-cyan)] disabled:opacity-50"
          >
            → {c.label}
          </button>
        ))}
      </div>
    </div>
  );
}

function ProgressBoardView({ workspaceId, topicNodes, fetchWorkspaceProgress, setWorkspaceProgress }) {
  const [progress, setProgress] = useState({});
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetchWorkspaceProgress(workspaceId).then((board) => {
      if (cancelled) return;
      setProgress(board || {});
      setLoading(false);
    });
    return () => { cancelled = true; };
  }, [workspaceId, fetchWorkspaceProgress]);

  // Board topics = the Mind Map's own topic tree, never a second source
  // of truth for "what topics exist" (see this section's header
  // comment). `topicId` here is the topic tree's `node_id`, the exact
  // same value build_topic_workflow() returns as `topic_id`/uses to key
  // study_progress by (agents/workflow_suggester.py), so a record
  // written by a Mind Map click lines up with a row here with no extra
  // lookup.
  const topics = (topicNodes || []).map((n) => ({
    topicId: n.node_id,
    title: n.title || n.node_id,
    status: progress[n.node_id]?.status || "not_started",
  }));

  async function handleSetStatus(topicId, status) {
    setBusyId(topicId);
    const prevStatus = progress[topicId]?.status || "not_started";
    // Optimistic update — a board should feel instant; rolled back on
    // failure rather than left stuck on a status the server rejected.
    setProgress((prev) => ({ ...prev, [topicId]: { ...(prev[topicId] || {}), status } }));
    try {
      await setWorkspaceProgress(workspaceId, topicId, { status });
    } catch (err) {
      setProgress((prev) => ({ ...prev, [topicId]: { ...(prev[topicId] || {}), status: prevStatus } }));
      alert(`Couldn't update progress: ${err.message || err}`);
    } finally {
      setBusyId(null);
    }
  }

  if (loading) {
    return <div className="text-xs text-[var(--neutral-600)]">Loading progress…</div>;
  }
  if (topics.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-[var(--neutral-800)] p-8 text-center text-xs text-[var(--neutral-600)]">
        No topics yet — generate a Mind Map above to populate the board.
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
      {PROGRESS_COLUMNS.map((col) => {
        const colTopics = topics.filter((t) => t.status === col.status);
        return (
          <div key={col.status} className="space-y-2">
            <div className="text-[10px] uppercase tracking-wide text-[var(--neutral-600)] flex items-center justify-between">
              <span>{col.label}</span>
              <span className="text-[var(--neutral-700)]">{colTopics.length}</span>
            </div>
            <div className="space-y-2">
              {colTopics.length === 0 ? (
                <div className="text-[10px] text-[var(--neutral-700)] px-1">—</div>
              ) : (
                colTopics.map((t) => (
                  <ProgressCard key={t.topicId} topic={t} onSetStatus={handleSetStatus} busy={busyId === t.topicId} />
                ))
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

// --- Diagrams (Mind Map + Workflows) --------------------------------------
// NEW — Mind Map and Workflows merged into one "Diagrams" sub-tab (same
// grouping move as Sources/Backlinks -> Library), rendering MindMapView
// and WorkflowsView unchanged rather than rebuilding either.
//
// Always stacked, Mind Map on top and Workflows below — never side by
// side. A mind map covers a whole notebook's worth of content and can
// end up wide or tall on its own; splitting the row with Workflows next
// to it would only squeeze the one view that most needs the room.
//
// Mind Map's width still isn't fixed, though: it's rendered inside
// NotebooksTab's own content pane, whose max-width already expands as
// the chat dock and the project sidebar collapse (see the "diagrams"
// branch of contentMaxWidthClass() below) — so a big mind map keeps
// getting more room the same way Library's side-by-side layout does,
// it just never needs a second column to do it.
// CHANGED — step 8: DiagramsView now owns the per-topic workflow state
// that WorkflowsView (step 7) just renders. A Mind Map node click no
// longer opens a sub-chat -- it calls generateTopicWorkflow(wsId, label)
// (SessionContext, step 4) and drops the result into `topicWorkflows`,
// an ordered array of
// { label, status: "loading" | "error" | "done", workflow?, error? },
// most-recent click first.
// NEW — step 9 polish, on top of step 8's plumbing:
//   1. Re-click on an already-*loading* topic is a no-op — `pendingLabelsRef`
//      tracks in-flight labels so a double-click (or clicking the same node
//      twice before the first request resolves) can't fire a second
//      request. Re-click on an already-*loaded* (done/error) topic is
//      intentionally still allowed through — it's how you retry an error
//      card (WorkflowsView already tells the user "click the node again to
//      retry") or force a fresh generation, and it bumps that topic back to
//      the front of the list the same way a first click would.
//   2. Loading state is inherently per-topic already (each entry in
//      `topicWorkflows` carries its own `status`), so nothing else is
//      needed there — one topic's request never blocks another's.
//   3. Switching workspaces clears `topicWorkflows` (and any in-flight
//      labels): these results are ephemeral scratch space for the notebook
//      you were just looking at, not something that should bleed into a
//      different workspace's Diagrams tab.
//   4. Dismissing a card removes it from the list outright; if that card
//      was still loading, `pendingLabelsRef` is NOT cleared for it, so the
//      in-flight request can still finish without erroring, but its result
//      is simply dropped when it lands (see the `done`/`error` setters
//      below, which check `dismissedLabelsRef` before re-inserting).
function DiagramsView({
  workspaceId, onOpenSubChat, fetchPanelContent, generateNotebooks, generateTopicWorkflow, onActiveContext,
  topicNodes, fetchWorkspaceProgress, setWorkspaceProgress,   // NEW — step 6.9: progress board, sourced from the Mind Map's topic list
}) {
  const [topicWorkflows, setTopicWorkflows] = useState([]);
  const pendingLabelsRef = useRef(new Set());
  const dismissedLabelsRef = useRef(new Set());

  // Edge case #3: notebook-scoped scratch state, reset on workspace switch.
  // CHANGED — step 7 persistence fix: these results used to live only in
  // this state, so a tab switch or refresh silently discarded them. Now
  // that topic_workflow_endpoint (step 7.2) merge-persists every result
  // under panel_content's "topic_workflows" key, re-hydrate from it here
  // instead of always starting from an empty list. `topic_key` (see
  // agents/workflow_suggester.py's build_topic_workflow docstring) isn't
  // rendered anywhere -- WorkflowsView keys/dedupes by `label`, same as
  // a fresh click -- so hydrated entries slot in exactly like live ones.
  useEffect(() => {
    let cancelled = false;
    setTopicWorkflows([]);
    pendingLabelsRef.current = new Set();
    dismissedLabelsRef.current = new Set();
    (async () => {
      let saved = {};
      try {
        const panel = await fetchPanelContent(workspaceId, "topic_workflows");
        saved = panel?.content ? JSON.parse(panel.content) : {};
        if (!saved || typeof saved !== "object" || Array.isArray(saved)) saved = {};
      } catch {
        saved = {}; // missing/unparseable row -> same as "nothing generated yet"
      }
      if (cancelled) return;
      const hydrated = Object.values(saved)
        .filter((w) => w && w.title && w.topic_label)
        .map((w) => ({ label: w.topic_label, status: "done", workflow: w }));
      if (hydrated.length) setTopicWorkflows(hydrated);
    })();
    return () => {
      cancelled = true;
    };
  }, [workspaceId, fetchPanelContent]);

  async function handleTopicSelect(label) {
    // Edge case #1: ignore a click on a topic that's already in flight.
    if (pendingLabelsRef.current.has(label)) return;
    pendingLabelsRef.current.add(label);
    // NEW — step 2.6a: topics here are identified by label (there's no
    // separate node_id -- see build_topic_workflow's own docstring), so
    // id and label are the same string.
    onActiveContext?.({ type: "topic", id: label, label });
    dismissedLabelsRef.current.delete(label); // a fresh click un-dismisses it
    setTopicWorkflows((prev) => [{ label, status: "loading" }, ...prev.filter((r) => r.label !== label)]);
    try {
      const workflow = await generateTopicWorkflow(workspaceId, label);
      pendingLabelsRef.current.delete(label);
      if (dismissedLabelsRef.current.has(label)) return; // edge case #4: dismissed while loading
      setTopicWorkflows((prev) => prev.map((r) => (r.label === label ? { label, status: "done", workflow } : r)));
    } catch (err) {
      pendingLabelsRef.current.delete(label);
      if (dismissedLabelsRef.current.has(label)) return; // edge case #4: dismissed while loading
      setTopicWorkflows((prev) => prev.map((r) => (r.label === label ? { label, status: "error", error: String(err.message || err) } : r)));
    }
  }

  // Edge case #4: dismiss a card. If it's still loading, its in-flight
  // request is left to finish (aborting the underlying fetch isn't wired
  // up here) but the result is dropped on arrival, see above.
  function handleDismissTopic(label) {
    dismissedLabelsRef.current.add(label);
    setTopicWorkflows((prev) => prev.filter((r) => r.label !== label));
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="min-w-0">
        <h3 className="text-[10px] uppercase tracking-wide text-[var(--neutral-600)] mb-2 flex items-center gap-1.5">
          <Network size={11} /> Mind Map
        </h3>
        <MindMapView
          workspaceId={workspaceId}
          onTopicSelect={handleTopicSelect}
          fetchPanelContent={fetchPanelContent}
          generateNotebooks={generateNotebooks}
        />
      </div>
      <div className="min-w-0 border-t border-[var(--neutral-800)] pt-6">
        <h3 className="text-[10px] uppercase tracking-wide text-[var(--neutral-600)] mb-2 flex items-center gap-1.5">
          <ListChecks size={11} /> Workflows
        </h3>
        <WorkflowsView
          workspaceId={workspaceId}
          results={topicWorkflows}
          onOpenSubChat={onOpenSubChat}
          onDismiss={handleDismissTopic}
        />
      </div>
      {/* NEW — step 6.9: Not Started/Ongoing/Done board, stacked below
          Workflows same as Workflows sits below Mind Map — sourced from
          `topicNodes` (the Mind Map's own topic tree) rather than a
          second topic list. */}
      <div className="min-w-0 border-t border-[var(--neutral-800)] pt-6">
        <h3 className="text-[10px] uppercase tracking-wide text-[var(--neutral-600)] mb-2 flex items-center gap-1.5">
          <ListChecks size={11} /> Progress
        </h3>
        <ProgressBoardView
          workspaceId={workspaceId}
          topicNodes={topicNodes}
          fetchWorkspaceProgress={fetchWorkspaceProgress}
          setWorkspaceProgress={setWorkspaceProgress}
        />
      </div>
    </div>
  );
}

// --- Study sub-view ------------------------------------------------------
// §4.5/§4.7: flashcard flipper, quiz runner, study-guide viewer — plain
// generated Markdown pasted in from a chat run, same "paste the role's
// stage_output text" pattern the Mind Map view above already uses.

function StudyView({ workspaceId }) {
  const { synthesizePodcast, buildVideoOverview, fetchPanelContent, generateNotebooks } = useSession();
  const [kind, setKind] = useState("flashcards");
  const [text, setText] = useState("");
  const [rendered, setRendered] = useState("");
  // Flashcards/Quiz/Study Guide are fully auto-generated now: Generate
  // calls agents/study_generator.py (via api/server.py's
  // NOTEBOOKS_GENERATE_TARGETS "study_flashcards"/"study_quiz"/
  // "study_guide" branches, the same generateNotebooks() dispatch
  // WorkflowsView above already uses) instead of asking the user to run
  // a separate chat and paste the Markdown back in here. The backend
  // already saves the result under the matching panel_content key, so
  // there's no separate "Load & Save" step -- Generate IS the save.
  const PANEL_KEY_BY_KIND = { flashcards: "study_flashcards", quiz: "study_quiz", study_guide: "study_guide" };
  const PERSISTED_KINDS = Object.keys(PANEL_KEY_BY_KIND);
  const LABEL_BY_KIND = { flashcards: "flashcard deck", quiz: "quiz", study_guide: "study guide" };
  const [loadingRendered, setLoadingRendered] = useState(true);
  const [updatedAt, setUpdatedAt] = useState(null);
  const [generating, setGenerating] = useState(false);
  const [generateError, setGenerateError] = useState("");
  // Stable per-workspace, per-kind id so quiz-attempt progress tracking
  // (eo/quiz_progress.py) works automatically -- it's only ever used as
  // a local lookup key for attempt history, never validated against the
  // knowledge graph, so a synthetic id is fine and means the person
  // never has to type one in.
  const quizNodeId = `panel:${workspaceId}:study_quiz`;

  useEffect(() => {
    if (!PERSISTED_KINDS.includes(kind)) { setLoadingRendered(false); return; }
    let cancelled = false;
    setLoadingRendered(true);
    setGenerateError("");
    fetchPanelContent(workspaceId, PANEL_KEY_BY_KIND[kind]).then((saved) => {
      if (cancelled) return;
      setRendered(saved?.content || "");
      setUpdatedAt(saved?.updated_at || null);
      setLoadingRendered(false);
    });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspaceId, kind]);

  async function handleGenerate() {
    setGenerating(true);
    setGenerateError("");
    try {
      const { branches } = await generateNotebooks(workspaceId, [PANEL_KEY_BY_KIND[kind]], null);
      const branch = branches.find((b) => b.panel_key === PANEL_KEY_BY_KIND[kind]);
      if (branch?.status === "error") throw new Error(branch.error || `Couldn't generate this ${LABEL_BY_KIND[kind]}.`);
      setRendered(branch?.result?.content || "");
      setUpdatedAt(branch?.result?.updated_at || null);
    } catch (err) {
      setGenerateError(String(err.message || err));
    } finally {
      setGenerating(false);
    }
  }

  // NEW — Part 4 §4.4: podcast synthesis state. Kept separate from
  // `rendered` (the paste-and-Load flow above) since this kind doesn't
  // render the pasted text directly — it round-trips through the
  // synthesis endpoint first and renders an <audio> player from the
  // result instead.
  const [podcastTitle, setPodcastTitle] = useState("podcast");
  const [podcastAudioUrl, setPodcastAudioUrl] = useState("");
  const [synthesizing, setSynthesizing] = useState(false);
  const [synthesizeError, setSynthesizeError] = useState("");
  // Tracks which titles have been successfully synthesized in this
  // session — the video-overview build below needs a podcast already on
  // disk under a matching title (see build_video_overview_endpoint's
  // 404 message), so the UI surfaces that dependency instead of letting
  // the user hit the error blind.
  const [synthesizedTitles, setSynthesizedTitles] = useState(() => new Set());

  async function handleSynthesize() {
    setSynthesizing(true);
    setSynthesizeError("");
    setPodcastAudioUrl("");
    try {
      const url = await synthesizePodcast(text, podcastTitle);
      setPodcastAudioUrl(url);
      setSynthesizedTitles((prev) => new Set(prev).add(podcastTitle));
    } catch (err) {
      setSynthesizeError(String(err.message || err));
    } finally {
      setSynthesizing(false);
    }
  }

  // NEW — Part 4 §4.4: Video Overview state. Reuses podcastTitle above
  // (rather than a separate field) since the backend requires the two to
  // match exactly — it locates the already-synthesized mp3 on disk by
  // that title instead of re-synthesizing it.
  const [slideText, setSlideText] = useState("");
  const [videoTitle, setVideoTitle] = useState("video_overview");
  const [videoUrl, setVideoUrl] = useState("");
  const [buildingVideo, setBuildingVideo] = useState(false);
  const [videoError, setVideoError] = useState("");

  async function handleBuildVideo() {
    setBuildingVideo(true);
    setVideoError("");
    setVideoUrl("");
    try {
      const url = await buildVideoOverview(slideText, podcastTitle, videoTitle);
      setVideoUrl(url);
    } catch (err) {
      setVideoError(String(err.message || err));
    } finally {
      setBuildingVideo(false);
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        {["flashcards", "quiz", "study_guide", "podcast", "video_overview"].map((k) => (
          <button
            key={k}
            onClick={() => { setKind(k); setRendered(""); setText(""); }}
            className={`text-xs rounded-lg px-3 py-1 ${kind === k ? "bg-[var(--accent)] text-[var(--accent-text)] font-medium" : "text-[var(--neutral-500)] hover:text-[var(--neutral-300)]"}`}
          >
            {k === "flashcards" ? "Flashcards" : k === "quiz" ? "Quiz" : k === "study_guide" ? "Study guide" : k === "podcast" ? "Podcast" : "Video overview"}
          </button>
        ))}
      </div>
      {kind === "video_overview" && (
        <p className="text-xs text-[var(--neutral-500)]">
          Paste the Markdown from a <code className="text-amber-300">slide_planner</code> chat run, then build a
          narrated slideshow using audio from a podcast you've already synthesized under the same title below.
        </p>
      )}
      {kind === "podcast" && (
        <p className="text-xs text-[var(--neutral-500)]">
          Paste the Markdown from a <code className="text-amber-300">podcast_scriptwriter</code> chat run.
        </p>
      )}
      {kind === "video_overview" && (
        <textarea
          value={slideText}
          onChange={(e) => setSlideText(e.target.value)}
          rows={5}
          placeholder={"# Title\n## Section heading\nSection body text..."}
          className="w-full bg-black/30 border border-[var(--neutral-800)] rounded px-2 py-1.5 text-xs font-mono outline-none focus:border-[var(--cyber-cyan)]"
        />
      )}
      {kind === "podcast" && (
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={5}
          placeholder={"HOST A: Welcome back to the show...\nHOST B: Today we're covering..."}
          className="w-full bg-black/30 border border-[var(--neutral-800)] rounded px-2 py-1.5 text-xs font-mono outline-none focus:border-[var(--cyber-cyan)]"
        />
      )}

      {PERSISTED_KINDS.includes(kind) && (
        <div className="space-y-3">
          <div className="flex items-center justify-between gap-2">
            <p className="text-xs text-[var(--neutral-500)]">
              {rendered
                ? "Generated from this notebook's sources."
                : `No ${LABEL_BY_KIND[kind]} yet — Generate reads this notebook's sources and writes one.`}
            </p>
            <div className="flex items-center gap-2 shrink-0">
              {updatedAt && !generating && (
                <span className="text-[10px] text-[var(--neutral-600)]">Generated {timeAgo(updatedAt)}</span>
              )}
              <button
                onClick={handleGenerate}
                disabled={generating || loadingRendered}
                className="flex items-center gap-1.5 text-xs bg-[var(--accent)] text-[var(--accent-text)] rounded-lg px-3 py-1.5 font-medium disabled:opacity-50"
              >
                {generating ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
                {generating ? "Generating…" : rendered ? "Regenerate" : "Generate"}
              </button>
            </div>
          </div>
          {generateError && <p className="text-xs text-red-400">{generateError}</p>}

          {loadingRendered ? (
            <div className="text-xs text-[var(--neutral-600)] flex items-center gap-1.5">
              <Loader2 size={12} className="animate-spin" /> Loading…
            </div>
          ) : rendered && (
            <div className="rounded-lg border border-[var(--neutral-800)] p-4">
              {kind === "flashcards" && <FlashcardFlipper markdownText={rendered} />}
              {kind === "quiz" && <QuizRunner quizText={rendered} workspaceId={workspaceId} quizNodeId={quizNodeId} />}
              {kind === "study_guide" && <StudyGuideViewer markdownText={rendered} />}
            </div>
          )}
        </div>
      )}

      {kind === "podcast" ? (
        <div className="space-y-2">
          <input
            value={podcastTitle}
            onChange={(e) => setPodcastTitle(e.target.value)}
            placeholder="Title (used as the audio filename)"
            className="w-full bg-black/30 border border-[var(--neutral-800)] rounded px-2 py-1.5 text-xs outline-none focus:border-[var(--cyber-cyan)]"
          />
          <button
            onClick={handleSynthesize}
            disabled={synthesizing || !text.trim()}
            className="flex items-center gap-1.5 text-xs bg-[var(--accent)] text-[var(--accent-text)] rounded-lg px-3 py-1.5 font-medium disabled:opacity-50"
          >
            {synthesizing && <Loader2 size={12} className="animate-spin" />}
            {synthesizing ? "Synthesizing…" : "Synthesize"}
          </button>
          {synthesizeError && (
            <p className="text-xs text-red-400">{synthesizeError}</p>
          )}
          {podcastAudioUrl && (
            <div className="rounded-lg border border-[var(--neutral-800)] p-3 space-y-2">
              <audio controls src={podcastAudioUrl} className="w-full" />
              <a
                href={podcastAudioUrl}
                download={`${podcastTitle || "podcast"}.mp3`}
                className="text-[11px] text-[var(--neutral-400)] hover:text-[var(--neutral-200)]"
              >
                Download mp3
              </a>
            </div>
          )}
        </div>
      ) : kind === "video_overview" ? (
        <div className="space-y-2">
          <input
            value={podcastTitle}
            onChange={(e) => setPodcastTitle(e.target.value)}
            placeholder="Podcast title (must match an already-synthesized podcast)"
            className="w-full bg-black/30 border border-[var(--neutral-800)] rounded px-2 py-1.5 text-xs outline-none focus:border-[var(--cyber-cyan)]"
          />
          {!synthesizedTitles.has(podcastTitle) && (
            <p className="text-[11px] text-amber-400">
              No podcast synthesized under this title yet this session. Switch to the Podcast tab and synthesize
              one with this exact title first, or the build below will fail.
            </p>
          )}
          <input
            value={videoTitle}
            onChange={(e) => setVideoTitle(e.target.value)}
            placeholder="Video title (used as the output filename)"
            className="w-full bg-black/30 border border-[var(--neutral-800)] rounded px-2 py-1.5 text-xs outline-none focus:border-[var(--cyber-cyan)]"
          />
          <button
            onClick={handleBuildVideo}
            disabled={buildingVideo || !slideText.trim() || !podcastTitle.trim()}
            className="flex items-center gap-1.5 text-xs bg-[var(--accent)] text-[var(--accent-text)] rounded-lg px-3 py-1.5 font-medium disabled:opacity-50"
          >
            {buildingVideo && <Loader2 size={12} className="animate-spin" />}
            {buildingVideo ? "Building…" : "Build video overview"}
          </button>
          {videoError && (
            <p className="text-xs text-red-400">{videoError}</p>
          )}
          {videoUrl && (
            <div className="rounded-lg border border-[var(--neutral-800)] p-3 space-y-2">
              <video controls src={videoUrl} className="w-full rounded" />
              <a
                href={videoUrl}
                download={`${videoTitle || "video_overview"}.mp4`}
                className="text-[11px] text-[var(--neutral-400)] hover:text-[var(--neutral-200)]"
              >
                Download mp4
              </a>
            </div>
          )}
        </div>
      ) : null}
    </div>
  );
}

// --- Candidates sub-view ------------------------------------------------------
// §4.6: the silent note-taking agent's proposals — never auto-committed.

function CandidatesView({ workspaceId, candidates, onAccept, onReject }) {
  if (candidates.length === 0) {
    return <p className="text-xs text-[var(--neutral-600)]">No suggested notes right now — the silent note-taker proposes one here when it spots a decision or action item in another chat in this notebook.</p>;
  }
  return (
    <div className="space-y-2">
      {/* FIX — bug audit §9: accept/reject now address a candidate by its
          stable candidate_id instead of its list index i (kept as the
          React `key` only, never sent to the server) — see
          eo/note_candidates.py's module docstring for why an index isn't
          safe once two users can be reviewing this same pending list. */}
      {candidates.map((c, i) => (
        <div key={c.candidate_id ?? i} className="rounded-lg border border-[var(--neutral-800)] p-3">
          <div className="text-xs font-medium text-[var(--neutral-200)]">{c.title}</div>
          <p className="text-xs text-[var(--neutral-400)] mt-1 whitespace-pre-wrap">{c.content}</p>
          <div className="flex items-center gap-2 mt-2">
            <button onClick={() => onAccept(c.candidate_id)} className="flex items-center gap-1 text-[11px] text-green-400 hover:text-green-300">
              <Check size={12} /> Accept
            </button>
            <button onClick={() => onReject(c.candidate_id)} className="flex items-center gap-1 text-[11px] text-red-400 hover:text-red-300">
              <X size={12} /> Discard
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}

// --- Clusters sub-view ------------------------------------------------------
// agents/note_clusterer.py, §4.3 — deterministic KMeans over each node's
// existing embedding (no extra LLM/quota cost), proposed as accept/discard
// candidates — never auto-applied. Accepting a candidate links every member
// node to the cluster's first node with a "clustered_with" edge, same graph
// primitive the Backlinks tab's edges already use. Scan is explicit (like
// Backlinks' "Quick title-match scan" button) rather than automatic, since it's
// a real recompute over every node in the notebook, not a passive fetch.

function ClustersView({ candidates, loading, scanning, onScan, onAccept, onReject }) {
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <p className="text-xs text-[var(--neutral-500)]">
          Suggested groupings of related sources, based on their existing embeddings — nothing is linked until you accept a group.
        </p>
        <button
          onClick={onScan}
          disabled={scanning}
          className="text-[11px] text-[var(--neutral-400)] hover:text-[var(--neutral-200)] disabled:opacity-50 shrink-0"
        >
          {scanning ? "Scanning…" : "Detect clusters"}
        </button>
      </div>

      {loading ? (
        <div className="text-xs text-[var(--neutral-600)] flex items-center gap-1.5"><Loader2 size={12} className="animate-spin" /> Loading…</div>
      ) : candidates.length === 0 ? (
        <p className="text-xs text-[var(--neutral-600)]">No suggested clusters right now — click "Detect clusters" to scan (needs at least 4 sources with embeddings).</p>
      ) : (
        <div className="space-y-2">
          {candidates.map((c) => (
            <div key={c.candidate_id} className="rounded-lg border border-[var(--neutral-800)] p-3">
              <div className="text-xs font-medium text-[var(--neutral-200)]">{c.suggested_label}</div>
              <ul className="mt-1.5 space-y-0.5">
                {c.titles.map((t, i) => (
                  <li key={i} className="text-xs text-[var(--neutral-400)] truncate">· {t || c.node_ids[i]}</li>
                ))}
              </ul>
              <div className="flex items-center gap-2 mt-2">
                <button onClick={() => onAccept(c.candidate_id)} className="flex items-center gap-1 text-[11px] text-green-400 hover:text-green-300">
                  <Check size={12} /> Accept
                </button>
                <button onClick={() => onReject(c.candidate_id)} className="flex items-center gap-1 text-[11px] text-red-400 hover:text-red-300">
                  <X size={12} /> Discard
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// --- Facts sub-view ------------------------------------------------------
// eo/workspace_facts.py, §0.3 — durable per-notebook facts (brand voice,
// target user, tech stack, plus a free-form `custom` bucket) that get
// folded into every agent prompt for this workspace automatically. This
// panel is the "settings-panel-facing surface" the module's docstring
// describes. Agent-proposed additions land in the candidates list below
// instead of overwriting the live facts directly — same accept/reject
// shape as the Suggested Notes tab, so an agent guess never silently
// clobbers something the user set on purpose.

// NEW — exported (not just used internally) so GrowthTab's `voice`
// sub-tab can import this directly instead of re-implementing fact
// editing a second time. Design doc §2.2: "Directly reuse NotebooksTab's
// FactsView component... eo/workspace_facts.py is already
// workspace-scoped, not domain-scoped, so a Growth-stage workspace
// calling the same fetchWorkspaceFacts/saveWorkspaceFacts functions
// NotebooksTab already uses gets brand voice for free." No behavior
// change here — same component, same props contract, just no longer
// module-private.
export function FactsView({ workspaceId, fetchWorkspaceFacts, saveWorkspaceFacts, fetchFactCandidates, acceptFactCandidate, rejectFactCandidate, factsRefreshSignal }) {
  const [facts, setFacts] = useState({ brand_voice: "", target_user: "", tech_stack: [], custom: {} });
  const [techStackText, setTechStackText] = useState("");
  const [customEntries, setCustomEntries] = useState([]); // [{key, value}]
  const [candidates, setCandidates] = useState([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState(null);

  async function load() {
    setLoading(true);
    const [f, c] = await Promise.all([
      fetchWorkspaceFacts(workspaceId),
      fetchFactCandidates(workspaceId),
    ]);
    setFacts(f);
    setTechStackText((f.tech_stack || []).join(", "));
    setCustomEntries(Object.entries(f.custom || {}).map(([key, value]) => ({ key, value: String(value) })));
    setCandidates(c);
    setLoading(false);
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspaceId]);

  // NEW — bug audit §9: candidates-only refresh, separate from the full
  // load() above and deliberately NOT re-fetching `facts` itself. This
  // fires on every parent loadNotebookData (e.g. right after the
  // Generate picker finishes a Facts scan) so a freshly-proposed
  // candidate actually shows up while sitting on this tab -- but it must
  // not touch `facts`/`techStackText`/`customEntries`, or a scan
  // finishing while the user has an unsaved edit in the brand-voice/
  // target-user/custom-facts fields would silently blow that edit away
  // mid-typing. The skip-on-first-run guard avoids a redundant fetch
  // immediately after load() already fetched the same thing on mount.
  const skipFirst = useRef(true);
  useEffect(() => {
    if (skipFirst.current) { skipFirst.current = false; return; }
    if (!workspaceId) return;
    fetchFactCandidates(workspaceId).then(setCandidates);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [factsRefreshSignal]);

  async function handleSave() {
    setSaving(true);
    const custom = {};
    for (const { key, value } of customEntries) {
      if (key.trim()) custom[key.trim()] = value;
    }
    const tech_stack = techStackText.split(",").map((s) => s.trim()).filter(Boolean);
    const saved = await saveWorkspaceFacts(workspaceId, {
      brand_voice: facts.brand_voice || "",
      target_user: facts.target_user || "",
      tech_stack,
      custom,
    });
    setFacts(saved);
    setSaving(false);
    setSavedAt(Date.now());
  }

  if (loading) {
    return <div className="text-xs text-[var(--neutral-600)] flex items-center gap-1.5"><Loader2 size={12} className="animate-spin" /> Loading facts…</div>;
  }

  return (
    <div className="space-y-6 max-w-lg">
      <div className="space-y-3">
        <p className="text-xs text-[var(--neutral-500)]">
          Durable facts about this notebook — folded into every agent prompt automatically, so you don't have to re-explain brand voice, audience, or stack in every chat.
        </p>
        <div>
          <label className="text-[10px] uppercase tracking-wide text-[var(--neutral-600)]">Brand voice</label>
          <textarea
            value={facts.brand_voice || ""}
            onChange={(e) => setFacts((f) => ({ ...f, brand_voice: e.target.value }))}
            rows={2}
            placeholder="e.g. warm, direct, no corporate jargon"
            className="w-full mt-1 bg-black/30 border border-[var(--neutral-800)] rounded px-2 py-1.5 text-xs outline-none focus:border-[var(--cyber-cyan)]"
          />
        </div>
        <div>
          <label className="text-[10px] uppercase tracking-wide text-[var(--neutral-600)]">Target user</label>
          <textarea
            value={facts.target_user || ""}
            onChange={(e) => setFacts((f) => ({ ...f, target_user: e.target.value }))}
            rows={2}
            placeholder="e.g. solo devs shipping side projects"
            className="w-full mt-1 bg-black/30 border border-[var(--neutral-800)] rounded px-2 py-1.5 text-xs outline-none focus:border-[var(--cyber-cyan)]"
          />
        </div>
        <div>
          <label className="text-[10px] uppercase tracking-wide text-[var(--neutral-600)]">Tech stack (comma-separated)</label>
          <input
            value={techStackText}
            onChange={(e) => setTechStackText(e.target.value)}
            placeholder="e.g. Next.js, FastAPI, Postgres"
            className="w-full mt-1 bg-black/30 border border-[var(--neutral-800)] rounded px-2 py-1.5 text-xs outline-none focus:border-[var(--cyber-cyan)]"
          />
        </div>
        <div>
          <div className="flex items-center justify-between">
            <label className="text-[10px] uppercase tracking-wide text-[var(--neutral-600)]">Custom facts</label>
            <button
              type="button"
              onClick={() => setCustomEntries((entries) => [...entries, { key: "", value: "" }])}
              className="text-[11px] text-[var(--neutral-500)] hover:text-[var(--neutral-300)]"
            >
              + Add
            </button>
          </div>
          <div className="space-y-1.5 mt-1">
            {customEntries.map((entry, i) => (
              <div key={i} className="flex items-center gap-1.5">
                <input
                  value={entry.key}
                  onChange={(e) => setCustomEntries((entries) => entries.map((en, j) => (j === i ? { ...en, key: e.target.value } : en)))}
                  placeholder="key"
                  className="w-28 shrink-0 bg-black/30 border border-[var(--neutral-800)] rounded px-2 py-1 text-xs outline-none focus:border-[var(--cyber-cyan)]"
                />
                <input
                  value={entry.value}
                  onChange={(e) => setCustomEntries((entries) => entries.map((en, j) => (j === i ? { ...en, value: e.target.value } : en)))}
                  placeholder="value"
                  className="flex-1 bg-black/30 border border-[var(--neutral-800)] rounded px-2 py-1 text-xs outline-none focus:border-[var(--cyber-cyan)]"
                />
                <button type="button" onClick={() => setCustomEntries((entries) => entries.filter((_, j) => j !== i))}>
                  <X size={12} className="text-[var(--neutral-600)] hover:text-red-400" />
                </button>
              </div>
            ))}
            {customEntries.length === 0 && (
              <p className="text-[11px] text-[var(--neutral-700)]">No custom facts yet — e.g. deploy_target, repo_url, anything domain-specific.</p>
            )}
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={handleSave}
            disabled={saving}
            className="text-xs bg-[var(--accent)] text-[var(--accent-text)] rounded-lg px-3 py-1.5 font-medium disabled:opacity-50"
          >
            {saving ? "Saving…" : "Save facts"}
          </button>
          {savedAt && !saving && <span className="text-[11px] text-[var(--neutral-600)]">Saved</span>}
        </div>
      </div>

      <div>
        <div className="text-[10px] uppercase tracking-wide text-[var(--neutral-600)] mb-2 flex items-center gap-1.5">
          Agent-suggested facts
          {candidates.length > 0 && (
            <span className="text-[10px] bg-amber-500/20 text-amber-300 rounded-full px-1.5">{candidates.length}</span>
          )}
        </div>
        {candidates.length === 0 ? (
          <p className="text-xs text-[var(--neutral-600)]">Nothing pending — agents propose a fact here when they spot something durable worth remembering, without overwriting what's above.</p>
        ) : (
          <div className="space-y-2">
            {/* FIX — bug audit §9: accept/reject address by the stable
                candidate_id now, not the list index i (kept only as the
                React `key`) — see eo/workspace_facts.py's
                accept_candidate/reject_candidate for why. */}
            {candidates.map((c, i) => (
              <div key={c.candidate_id ?? i} className="rounded-lg border border-[var(--neutral-800)] p-3">
                <div className="text-xs font-medium text-[var(--neutral-200)]">{c.key}</div>
                <p className="text-xs text-[var(--neutral-400)] mt-1 whitespace-pre-wrap">{String(c.value)}</p>
                {c.proposed_by && <p className="text-[10px] text-[var(--neutral-700)] mt-1">proposed by {c.proposed_by}</p>}
                <div className="flex items-center gap-2 mt-2">
                  <button
                    onClick={async () => { await acceptFactCandidate(workspaceId, c.candidate_id); await load(); }}
                    className="flex items-center gap-1 text-[11px] text-green-400 hover:text-green-300"
                  >
                    <Check size={12} /> Accept
                  </button>
                  <button
                    onClick={async () => { await rejectFactCandidate(workspaceId, c.candidate_id); await load(); }}
                    className="flex items-center gap-1 text-[11px] text-red-400 hover:text-red-300"
                  >
                    <X size={12} /> Discard
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// --- Insights (Facts + Clusters + Suggested notes) ------------------------
// NEW — Facts, Clusters, and Suggested notes merged into one "Insights"
// sub-tab (same grouping move as Sources/Backlinks -> Library and Mind
// Map/Workflows -> Diagrams), rendering FactsView, ClustersView, and
// CandidatesView unchanged rather than rebuilding any of them.
//
// Facts and Clusters share a column — both are structured things the
// system pulled out of your sources for you to confirm, just at
// different granularity (a handful of durable facts vs. groups of
// related sources) — stacked vertically within that column, Facts on
// top since it's usually the shorter of the two. Suggested notes (a
// different kind of proposal — whole draft notes, not structured data)
// gets its own column, same "one thing gets its own side" split Library
// uses for Backlinks.
//
// Layout responds to `dockOpen` (NotebooksTab passes `!chatDockCollapsed`),
// same contract as LibraryView: side by side when the dock is closed and
// there's room (Facts & Clusters left — Facts already lived there —
// Suggested notes right), stacked — Suggested notes on top, Facts &
// Clusters below — once the dock reopens and takes the width back.
function InsightsView({
  workspaceId, fetchWorkspaceFacts, saveWorkspaceFacts, fetchFactCandidates, acceptFactCandidate, rejectFactCandidate, factsRefreshSignal,
  clusterCandidates, loadingClusters, scanningClusters, onScanClusters, onAcceptCluster, onRejectCluster,
  candidates, onAcceptCandidate, onRejectCandidate,
  dockOpen,
}) {
  const factsAndClusters = (
    <div className="space-y-6">
      <div>
        <h4 className="text-[10px] uppercase tracking-wide text-[var(--neutral-600)] mb-2 flex items-center gap-1.5">
          <BookMarked size={11} /> Facts
        </h4>
        <FactsView
          workspaceId={workspaceId}
          fetchWorkspaceFacts={fetchWorkspaceFacts}
          saveWorkspaceFacts={saveWorkspaceFacts}
          fetchFactCandidates={fetchFactCandidates}
          acceptFactCandidate={acceptFactCandidate}
          rejectFactCandidate={rejectFactCandidate}
          factsRefreshSignal={factsRefreshSignal}
        />
      </div>
      <div className="border-t border-[var(--neutral-800)] pt-6">
        <h4 className="text-[10px] uppercase tracking-wide text-[var(--neutral-600)] mb-2 flex items-center gap-1.5">
          <Layers size={11} /> Clusters
        </h4>
        <ClustersView
          candidates={clusterCandidates}
          loading={loadingClusters}
          scanning={scanningClusters}
          onScan={onScanClusters}
          onAccept={onAcceptCluster}
          onReject={onRejectCluster}
        />
      </div>
    </div>
  );
  const suggestedNotes = (
    <CandidatesView
      workspaceId={workspaceId}
      candidates={candidates}
      onAccept={onAcceptCandidate}
      onReject={onRejectCandidate}
    />
  );

  if (dockOpen) {
    // Stacked — Suggested notes on top, Facts & Clusters below.
    return (
      <div className="flex flex-col gap-6">
        <div className="min-w-0">
          <h3 className="text-[10px] uppercase tracking-wide text-[var(--neutral-600)] mb-2 flex items-center gap-1.5">
            <Sparkles size={11} /> Suggested notes
          </h3>
          {suggestedNotes}
        </div>
        <div className="min-w-0 border-t border-[var(--neutral-800)] pt-6">
          <h3 className="text-[10px] uppercase tracking-wide text-[var(--neutral-600)] mb-2">Facts &amp; Clusters</h3>
          {factsAndClusters}
        </div>
      </div>
    );
  }

  // Side by side — Facts & Clusters left, Suggested notes right.
  return (
    <div className="flex gap-6 items-start">
      <div className="flex-1 min-w-0">
        <h3 className="text-[10px] uppercase tracking-wide text-[var(--neutral-600)] mb-2">Facts &amp; Clusters</h3>
        {factsAndClusters}
      </div>
      <div className="flex-1 min-w-0 border-l border-[var(--neutral-800)] pl-6">
        <h3 className="text-[10px] uppercase tracking-wide text-[var(--neutral-600)] mb-2 flex items-center gap-1.5">
          <Sparkles size={11} /> Suggested notes
        </h3>
        {suggestedNotes}
      </div>
    </div>
  );
}

// --- Correction capture sub-view ----------------------------------------
// Data Layer architecture §8a: capture -- a file-scope picker (one
// source, or "All files") and a plain-language box describing what's
// wrong. As of §8c, submitting actually runs the pipeline: the server
// hands the text to §8b's agents/correction_locator.py, and either
// queues a located candidate in eo/correction_candidates.py's pending
// store for the Patch Review section below to render as a before/after,
// or comes back with a plain reason there was nothing to locate -- shown
// here inline, since a dead end never reaches Patch Review at all.
//
// RENAMED (was CorrectionsView) — Corrections + Patch Review merged into
// one "Corrections" sub-tab (same grouping move as Sources/Backlinks ->
// Library). This is now the "capture" half nested inside the merged
// CorrectionsView below, same relationship SourcesView/BacklinksView
// have to LibraryView.
function CorrectionCaptureView({ workspaceId, nodes, edges, submitCorrection, onQueued }) {
  const [scopeId, setScopeId] = useState("all");
  const [text, setText] = useState("");
  const [submitting, setSubmitting] = useState(false);
  // Ephemeral, this session only -- the durable record of a submission
  // is whatever candidate it produced (Patch Review tab) or nothing at
  // all (no_match); this list is just per-submission feedback so a
  // "no match" result doesn't vanish the moment it's shown.
  const [history, setHistory] = useState([]);

  // Same grouping SourcesView already uses for its own file list --
  // scoping a correction to "this file" means one row per root source,
  // not one per child page, same granularity a person actually thinks
  // in when they say "the PDF I uploaded" rather than "page 4 of it."
  const fileOptions = groupSourceNodes(nodes || [], edges || []).map((g) => ({
    id: g.root.node_id,
    label: groupDisplayTitle(g.root, g.children.length > 0),
  }));

  async function handleSubmit() {
    const trimmed = text.trim();
    if (!trimmed || submitting) return;
    const scopeLabel = scopeId === "all"
      ? "All files"
      : (fileOptions.find((f) => f.id === scopeId)?.label || scopeId);
    setSubmitting(true);
    try {
      const result = await submitCorrection(workspaceId, {
        text: trimmed,
        scopeNodeId: scopeId === "all" ? null : scopeId,
      });
      setHistory((prev) => [
        {
          id: result?.candidate?.candidate_id || `${Date.now()}-${prev.length}`,
          scopeLabel,
          text: trimmed,
          submittedAt: new Date().toISOString(),
          status: result?.status || "no_match",
          reason: result?.reason || null,
        },
        ...prev,
      ]);
      setText("");
      if (result?.status === "queued") onQueued?.();
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="space-y-4">
      <div className="space-y-2">
        <p className="text-xs text-[var(--neutral-500)]">
          Tell us what's wrong — a wrong fact, a missing connection, anything
          that doesn't match the source. Scope it to one file, or leave it as
          "All files" if it's about the notebook as a whole.
        </p>
        <select
          value={scopeId}
          onChange={(e) => setScopeId(e.target.value)}
          className="w-full bg-black/30 border border-[var(--neutral-800)] rounded px-2 py-1.5 text-xs outline-none focus:border-[var(--cyber-cyan)]"
        >
          <option value="all">All files</option>
          {fileOptions.map((f) => (
            <option key={f.id} value={f.id}>{f.label}</option>
          ))}
        </select>
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder='e.g. "The mind map says the deadline is March 3rd, but the source says March 13th."'
          rows={3}
          className="w-full bg-black/30 border border-[var(--neutral-800)] rounded px-2 py-1.5 text-xs outline-none focus:border-[var(--cyber-cyan)] resize-none"
        />
        <div className="flex justify-end">
          <button
            onClick={handleSubmit}
            disabled={!text.trim() || submitting}
            className="flex items-center gap-1.5 text-xs bg-[var(--accent)] text-[var(--accent-text)] rounded px-3 py-1.5 font-medium disabled:opacity-50"
          >
            {submitting ? <Loader2 size={12} className="animate-spin" /> : <Send size={12} />}
            {submitting ? "Locating…" : "Submit correction"}
          </button>
        </div>
      </div>

      {history.length > 0 ? (
        <div className="space-y-1.5">
          <p className="text-[10px] uppercase tracking-wide text-[var(--neutral-600)]">Submitted this session</p>
          {history.map((c) => (
            <div key={c.id} className="px-3 py-2 rounded-lg border border-[var(--neutral-800)] space-y-1">
              <div className="flex items-center justify-between gap-2">
                <span className="text-[10px] text-[var(--neutral-500)]">{c.scopeLabel}</span>
                <span className="text-[10px] text-[var(--neutral-600)]">{timeAgo(c.submittedAt)}</span>
              </div>
              <p className="text-xs text-[var(--neutral-200)]">{c.text}</p>
              {c.status === "queued" ? (
                <p className="text-[10px] text-green-400">Located a match — see Patch Review below to accept or discard it.</p>
              ) : (
                <p className="text-[10px] text-[var(--neutral-500)]">Couldn't locate a match: {c.reason}</p>
              )}
            </div>
          ))}
        </div>
      ) : (
        <div className="rounded-lg border border-dashed border-[var(--neutral-800)] p-8 text-center text-xs text-[var(--neutral-600)]">
          No corrections submitted yet.
        </div>
      )}
    </div>
  );
}

// --- Patch Review sub-view ----------------------------------------------
// Data Layer architecture §8c: renders each pending correction
// candidate (eo/correction_candidates.py) as a before/after over the
// same three fields agents/correction_locator.py is ever allowed to
// touch -- name, summary, content_hint -- and lets the person accept
// (applies the op via eo/secondary_data.py:apply_patch()) or discard
// it. Same accept/reject visual language as CandidatesView/
// ClustersView above, just with a diff instead of a single content
// block, since what's being reviewed here is a change, not a proposal
// from nothing.
//
// Unchanged by the Corrections/Patch Review merge below — still its own
// component with its own fetch/refreshSignal contract (same "self-
// contained, signal-based refresh" shape FactsView uses inside
// InsightsView), just nested under the Corrections tab instead of
// living on its own tab.
function FieldDiff({ label, before, after }) {
  if (before === after) return null;
  return (
    <div>
      <p className="text-[10px] uppercase tracking-wide text-[var(--neutral-600)]">{label}</p>
      {before ? (
        <p className="text-xs text-red-400/80 line-through whitespace-pre-wrap">{before}</p>
      ) : null}
      <p className="text-xs text-green-400 whitespace-pre-wrap">{after}</p>
    </div>
  );
}

function PatchReviewView({ workspaceId, fetchPatchCandidates, acceptPatchCandidate, rejectPatchCandidate, refreshSignal }) {
  const [candidates, setCandidates] = useState([]);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState(null);

  async function load() {
    setLoading(true);
    try {
      setCandidates(await fetchPatchCandidates(workspaceId));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (workspaceId) load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspaceId, refreshSignal]);

  async function handleAccept(candidateId) {
    setBusyId(candidateId);
    try {
      await acceptPatchCandidate(workspaceId, candidateId);
      await load();
    } finally {
      setBusyId(null);
    }
  }

  async function handleReject(candidateId) {
    setBusyId(candidateId);
    try {
      await rejectPatchCandidate(workspaceId, candidateId);
      await load();
    } finally {
      setBusyId(null);
    }
  }

  if (loading) {
    return <div className="text-xs text-[var(--neutral-600)] flex items-center gap-1.5"><Loader2 size={12} className="animate-spin" /> Loading…</div>;
  }
  if (candidates.length === 0) {
    return <p className="text-xs text-[var(--neutral-600)]">Nothing waiting for review — corrections that find a match above show up here.</p>;
  }
  return (
    <div className="space-y-2">
      {candidates.map((c) => {
        const before = c.before || {};
        const after = c.op?.value || {};
        return (
          <div key={c.candidate_id} className="rounded-lg border border-[var(--neutral-800)] p-3 space-y-2">
            <p className="text-xs text-[var(--neutral-400)] italic">"{c.correction_text}"</p>
            <p className="text-[10px] text-[var(--neutral-600)]">{c.scope_label}</p>
            <div className="space-y-1.5">
              <FieldDiff label="Name" before={before.name} after={after.name} />
              <FieldDiff label="Summary" before={before.summary} after={after.summary} />
              <FieldDiff label="Content hint" before={before.content_hint} after={after.content_hint} />
            </div>
            <div className="flex items-center gap-2 pt-1">
              <button
                onClick={() => handleAccept(c.candidate_id)}
                disabled={busyId === c.candidate_id}
                className="flex items-center gap-1 text-[11px] text-green-400 hover:text-green-300 disabled:opacity-50"
              >
                <Check size={12} /> Accept
              </button>
              <button
                onClick={() => handleReject(c.candidate_id)}
                disabled={busyId === c.candidate_id}
                className="flex items-center gap-1 text-[11px] text-red-400 hover:text-red-300 disabled:opacity-50"
              >
                <X size={12} /> Discard
              </button>
            </div>
          </div>
        );
      })}
    </div>
  );
}

// --- Corrections (capture + Patch Review) --------------------------------
// NEW — Corrections and Patch Review merged into one "Corrections"
// sub-tab (same grouping move as Sources/Backlinks -> Library and
// Facts/Clusters/Suggested notes -> Insights), rendering
// CorrectionCaptureView and PatchReviewView unchanged rather than
// rebuilding either.
//
// Capture and Patch Review are a producer/consumer pair, not two
// independent things to browse side by side the way Sources/Backlinks
// or Facts/Clusters are — a correction has to be submitted before
// there's anything to review — so unlike LibraryView/InsightsView this
// doesn't flip to a side-by-side layout when the chat dock is closed.
// It stays stacked at every width, same "always stacked" call
// DiagramsView makes for Mind Map/Workflows above, just for the
// opposite reason (there it's about one view needing all the room;
// here it's about reading order). Capture stays on top since that's
// the order the workflow actually happens in — submit, then review —
// and `onQueued` bumping `refreshSignal` (passed through from
// NotebooksTab) means an accepted match shows up in the Patch Review
// list below without needing to navigate anywhere.
function CorrectionsView({
  workspaceId, nodes, edges, submitCorrection, onQueued,
  fetchPatchCandidates, acceptPatchCandidate, rejectPatchCandidate, refreshSignal,
}) {
  return (
    <div className="flex flex-col gap-6">
      <div className="min-w-0">
        <h3 className="text-[10px] uppercase tracking-wide text-[var(--neutral-600)] mb-2 flex items-center gap-1.5">
          <Wrench size={11} /> Submit a correction
        </h3>
        <CorrectionCaptureView
          workspaceId={workspaceId}
          nodes={nodes}
          edges={edges}
          submitCorrection={submitCorrection}
          onQueued={onQueued}
        />
      </div>
      <div className="min-w-0 border-t border-[var(--neutral-800)] pt-6">
        <h3 className="text-[10px] uppercase tracking-wide text-[var(--neutral-600)] mb-2 flex items-center gap-1.5">
          <GitCompare size={11} /> Patch Review
        </h3>
        <PatchReviewView
          workspaceId={workspaceId}
          fetchPatchCandidates={fetchPatchCandidates}
          acceptPatchCandidate={acceptPatchCandidate}
          rejectPatchCandidate={rejectPatchCandidate}
          refreshSignal={refreshSignal}
        />
      </div>
    </div>
  );
}

// --- Chat-selection gate -------------------------------------------------
// NEW — a notebook's own content (Library/Diagrams/Study/Insights/
// Corrections, plus everything the docked WorkspaceChatPanel's composer
// can do — attach a file, run Generate) only ever meant anything in the
// context of one specific chat under this workspace; every one of those
// eventually calls something keyed off a session_id. Selecting a project
// with no chat active — whether because it has none yet, or because it
// has some but none of them is the one currently open — used to still
// show all of that, with nothing on screen answering "which chat is this
// about?" This is the pair of empty states that replaces the whole tab
// nav + content area (see the `hasActiveChat` branch in the main render
// below) until that's unambiguous: create the first chat when there
// isn't one, or pick one of the existing ones when there is.
function CreateFirstChatPrompt({ workspace, creating, onCreateChat }) {
  return (
    <div className="rounded-lg border border-dashed border-[var(--neutral-800)] p-10 text-center space-y-3 max-w-md mx-auto mt-6">
      <MessageSquare size={22} className="mx-auto text-[var(--neutral-600)]" />
      <p className="text-sm text-[var(--neutral-300)]">
        "{workspace?.name}" doesn't have a chat yet.
      </p>
      <p className="text-xs text-[var(--neutral-500)]">
        Sources, Diagrams, Study, Insights, and Corrections all live inside a
        chat's context here — create one first so anything you upload or
        submit lands in the right place, instead of nowhere in particular.
      </p>
      <button
        onClick={onCreateChat}
        disabled={creating}
        className="inline-flex items-center gap-1.5 text-xs bg-[var(--accent)] text-[var(--accent-text)] rounded-lg px-3 py-1.5 font-medium disabled:opacity-50"
      >
        {creating ? <Loader2 size={13} className="animate-spin" /> : <Plus size={13} />}
        {creating ? "Creating…" : "Create first chat"}
      </button>
    </div>
  );
}

// NEW — the "has chats, none selected" half of the same gate. No button
// of its own on purpose: the actual affordance is the chat list already
// sitting in the sidebar one column over (or the "+" beside the project
// name for a new one) — this just points at it rather than duplicating
// a chat-picker inline, so there's exactly one place chats get chosen
// from, not two that could drift out of sync.
function SelectChatPrompt({ workspace }) {
  return (
    <div className="rounded-lg border border-dashed border-[var(--neutral-800)] p-10 text-center space-y-3 max-w-md mx-auto mt-6">
      <MessageSquare size={22} className="mx-auto text-[var(--neutral-600)]" />
      <p className="text-sm text-[var(--neutral-300)]">
        No chat selected in "{workspace?.name}".
      </p>
      <p className="text-xs text-[var(--neutral-500)]">
        Sources, Diagrams, Study, Insights, and Corrections are all scoped to
        one chat at a time — pick one from the list on the left (or start a
        new one with the "+" beside the project name) to see them.
      </p>
    </div>
  );
}

// --- Node preview modal ------------------------------------------------------

function NodePreviewModal({ node, onClose }) {
  if (!node) return null;
  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onClick={onClose}>
      <div
        className="bg-[var(--neutral-900)] border border-[var(--neutral-700)] rounded-lg p-4 w-[32rem] max-h-[70vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-2">
          <h3 className="text-sm font-medium text-[var(--neutral-200)]">{node.title || node.node_id}</h3>
          <button onClick={onClose}><X size={14} className="text-[var(--neutral-500)] hover:text-[var(--neutral-200)]" /></button>
        </div>
        <p className="text-[10px] text-[var(--neutral-600)] mb-2">{node.node_type} · {timeAgo(node.created_at)}</p>
        {/* NEW — Backlinks-as-topic-tree: a topic node (node_type ===
            "topic") has no .content -- it's a derived tree entry, not a
            Primary Source. Fall back to its summary, and list instance
            provenance (overlapping_checker.py's "duplicate" folds) when
            present. */}
        {node.node_type === "topic" ? (
          <>
            <p className="text-xs text-[var(--neutral-300)] whitespace-pre-wrap">{node.summary || "No summary yet."}</p>
            {node.instances?.length > 0 && (
              <div className="mt-2 space-y-1 border-t border-[var(--neutral-800)] pt-2">
                <p className="text-[10px] uppercase tracking-wide text-[var(--neutral-600)]">
                  Folded-in instances ({node.instances.length})
                </p>
                {node.instances.map((inst, i) => (
                  <p key={i} className="text-[11px] text-[var(--neutral-400)] whitespace-pre-wrap">{inst.verbatim}</p>
                ))}
              </div>
            )}
          </>
        ) : (
          <p className="text-xs text-[var(--neutral-300)] whitespace-pre-wrap">{node.content}</p>
        )}
      </div>
    </div>
  );
}

// --- Main tab ------------------------------------------------------

function NotebooksTab({ onPromoted, onActiveWorkspaceChange }) {
   const {
     createWorkspace, promoteWorkspace,
     fetchWorkspaceNodes, deleteWorkspaceNode, renameWorkspaceNode, fetchGraphEdges, fetchNodeSummaries,
     fetchTopicsGraph, topicPulsingIds,   // NEW — Backlinks-as-topic-tree
     fetchNoteCandidates, acceptNoteCandidate, rejectNoteCandidate,
     fetchWorkspaceFacts, saveWorkspaceFacts, fetchFactCandidates, acceptFactCandidate, rejectFactCandidate,
     submitCorrection, fetchPatchCandidates, acceptPatchCandidate, rejectPatchCandidate,
     fetchPanelContent, savePanelContent, fetchPanelContentList,
     generateNotebooks,
     generateTopicWorkflow,   // NEW — step 8: per-topic workflow, owned by DiagramsView
     fetchWorkspaceProgress, setWorkspaceProgress,   // NEW — step 6.9: progress board
     proposeClusters, fetchClusterCandidates, acceptClusterCandidate, rejectClusterCandidate,
    openScopedSubChat,
  } = useSession();
  const { workspaces, fetchWorkspaces } = useWorkspaces();   // FIX — was destructured off useSession(), which no longer serves it; this call site was missed at the time
  const { chats } = useChatList();   // CHANGED — Item 2 concern split, slice 4: was useSession()
  // NEW — step 3e: switchChat/createNewChat now resolve the dock for
  // whichever workspace a chat belongs to, instead of writing into one
  // shared SessionContext sessionId. This tab's own <WorkspaceChatPanel>
  // calls below are also updated (in this same patch) to pass
  // workspaceId={selected?.id} — previously they passed neither prop, so
  // the embedded panel was still reading legacy SessionContext state;
  // left as-is, this cutover would have made the panel go blank.
  //
  // NEW — issue #3: renameChat/deleteChat/createWorkspaceChat back the
  // per-project nested-chat mechanic below (same one ResearchTab now
  // uses) — createNewChat (unscoped) is no longer called from this tab.
  const { switchChat, renameChat, deleteChat, createWorkspaceChat } = useWorkspaceDockActions();
  const activeChatId = useLastActiveChatId();

  // NEW — §8: Notebooks only ever shows note-stage workspaces now — once
  // promoted, a workspace moves to the Research tab instead of appearing
  // in both places.
  const notebooks = workspaces.filter((w) => (w.active_stages || [w.stage]).includes("note"));

  const [selectedId, setSelectedId] = useState(null);
  const [subTab, setSubTab] = useState("library");
  const [nodes, setNodes] = useState([]);
  const [edges, setEdges] = useState([]);
  // NEW — Backlinks-as-topic-tree: eo/secondary_data.py's topic tree +
  // connection graph, kept as its OWN state rather than repurposing
  // nodes/edges above -- SourcesView and CorrectionsView still need
  // the primary node/edge graph (source grouping via same_source
  // edges) untouched, so BacklinksView is the only consumer of these.
  const [topicNodes, setTopicNodes] = useState([]);
  const [topicEdges, setTopicEdges] = useState([]);
  // NEW — Notebooks integration guide §6.6 (Phase 3): agent-written
  // per-node blurbs (eo/node_summaries.py), keyed by node_id, fed to
  // KnowledgeGraphView's rationale panel in concept-graph mode.
  const [nodeSummaries, setNodeSummaries] = useState({});
  const [candidates, setCandidates] = useState([]);
  const [clusterCandidates, setClusterCandidates] = useState([]);
  // NEW — bug audit §9 (round-trip audit): FactsView keeps its own
  // fact-candidates fetch separate from this component's nodes/edges/
  // candidates/clusterCandidates state (see the comment on the effect
  // below for why: it also owns an editable facts draft that shouldn't
  // get silently overwritten on every unrelated refresh). But that meant
  // NOTHING told FactsView "the Generate picker just ran a Facts scan" if
  // the user was already sitting on the Facts sub-tab when they ran it
  // from the picker popover -- picker's onComplete only calls
  // loadNotebookData, which never touched FactsView's local state, so
  // the new candidates silently sat in the backend store until the user
  // happened to navigate away and back (remounting FactsView triggers
  // its own load). Bumped once per loadNotebookData call; FactsView
  // re-fetches just its candidates list (not the editable draft) when it
  // changes.
  const [factsRefreshSignal, setFactsRefreshSignal] = useState(0);
  // NEW — Data Layer architecture §8c: bumped when a correction on the
  // Corrections tab locates a match, so the Patch Review tab picks up
  // the new pending candidate even if it's already mounted (same
  // "signal, don't re-derive" shape factsRefreshSignal above uses).
  const [patchReviewRefreshSignal, setPatchReviewRefreshSignal] = useState(0);
  // NEW — §8: { [panel_key]: { updated_at, ... } } from eo/panel_content.py's
  // list_content, and { [subTabId]: isoString } read/written from
  // localStorage — see latestTabTimestamp/hasUnseenUpdate below.
  const [panelContent, setPanelContent] = useState({});
  const [lastViewed, setLastViewed] = useState({});
  const [loadingClusters, setLoadingClusters] = useState(false);
  const [scanningClusters, setScanningClusters] = useState(false);
  const [loadingNodes, setLoadingNodes] = useState(false);
  const [previewNode, setPreviewNode] = useState(null);
  // NEW — Notebooks Chat-First refinement, Phase 2 step 2.6a. Single
  // "what's currently in view" signal shared across every sub-tab,
  // rather than reusing any one view's own local state (previewNode
  // above is transient -- cleared on modal close -- and DiagramsView's
  // topicWorkflows never leaves that component). Shape:
  // { type: "topic" | "source", id, label } | null. Fed by whichever
  // view the person last clicked something in (Library's source rows,
  // Diagrams' Mind Map topic nodes); read by WorkspaceChatPanel as a
  // scope default when a chat message names a capability but no
  // specific topic/source. Deliberately never cleared on sub-tab switch
  // -- "the source I was just looking at" is still a reasonable default
  // a moment later even after navigating away from Library.
  const [activeContext, setActiveContext] = useState(null);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [submittingNotebook, setSubmittingNotebook] = useState(false);
  // NEW — §3 fix: which notebook's kebab menu (rename/delete/members) is
  // open. ManageWorkspaceModal already existed fully built, just never
  // wired into any tab's UI.
  const [managingWorkspace, setManagingWorkspace] = useState(null);
  // NEW — §8: promote-to-Research busy/error state for the button next
  // to "Open chat".
  const [promoting, setPromoting] = useState(false);
  const [promoteError, setPromoteError] = useState(null);
  const [promoteTargetStage, setPromoteTargetStage] = useState("research");
  // NEW — §2.6 step 4: "complete" (today's default — leaves this tab)
  // vs "partial" (stays active here too, per §2.1/§2.2).
  const [promoteMode, setPromoteMode] = useState("complete");
  // NEW — §6.2: right-hand chat dock collapse state, restored from
  // localStorage on mount (same pattern as sidebarCollapsed elsewhere).
  const [chatDockCollapsed, setChatDockCollapsed] = useState(false);
  const [projectsCollapsed, setProjectsCollapsed] = useState(false); // NEW — collapsible project-picker sidebar
  // NEW — issue #3: nested-chat create/rename/delete state, same shape as
  // ResearchTab's own (and ChatSidebar's editingId/editTitle/pendingDelete
  // before that) — scoped to this tab's notebook list.
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
  // NEW — §4 fix: guards the auto-select effect below until we've had a
  // chance to read a saved selection from localStorage, so it doesn't
  // jump to workspaces[0] before the restore runs.
  const [restoredSelection, setRestoredSelection] = useState(false);

  // NEW — §4 fix: restore the last-selected notebook and sub-tab on
  // mount.
  useEffect(() => {
    const savedId = localStorage.getItem(SELECTED_NOTEBOOK_KEY);
    const savedSubTab = localStorage.getItem(SUB_TAB_KEY);
    if (savedId) setSelectedId(savedId);
    if (savedSubTab && SUB_TABS.some((t) => t.id === savedSubTab)) setSubTab(savedSubTab);
    setRestoredSelection(true);
  }, []);

  // NEW — §4 fix: persist selection changes. Guarded on restoredSelection
  // so the initial (pre-restore) null/"library" values don't overwrite
  // what's already saved before the restore effect above has run.
  useEffect(() => {
    if (!restoredSelection || !selectedId) return;
    localStorage.setItem(SELECTED_NOTEBOOK_KEY, selectedId);
  }, [selectedId, restoredSelection]);

  useEffect(() => {
    if (!restoredSelection) return;
    localStorage.setItem(SUB_TAB_KEY, subTab);
  }, [subTab, restoredSelection]);

  // NEW — §8: load this notebook's last-viewed map whenever the selected
  // notebook changes (each workspace has its own key, so switching
  // notebooks doesn't carry stale viewed-state over from the last one).
  useEffect(() => {
    if (!selectedId) { setLastViewed({}); return; }
    try {
      const raw = localStorage.getItem(`${LAST_VIEWED_PREFIX}:${selectedId}`);
      setLastViewed(raw ? JSON.parse(raw) : {});
    } catch {
      setLastViewed({});
    }
  }, [selectedId]);

  // NEW — §8: whatever sub-tab is currently open counts as "viewed" —
  // stamp it every time the open tab (or the notebook itself) changes.
  // This is deliberately a plain "now" stamp rather than something tied
  // to panelContent having finished loading: a dot only ever renders for
  // a sub-tab that ISN'T the active one (see the SUB_TABS.map render
  // below), so it doesn't matter that this fires before this tab's own
  // data has arrived — nothing reads this tab's own viewed timestamp
  // against itself.
  useEffect(() => {
    if (!restoredSelection || !selectedId) return;
    setLastViewed((prev) => {
      const next = { ...prev, [subTab]: new Date().toISOString() };
      try {
        localStorage.setItem(`${LAST_VIEWED_PREFIX}:${selectedId}`, JSON.stringify(next));
      } catch {
        // localStorage can throw (private browsing quota, etc.) — the
        // dot just won't persist across reloads in that case, not worth
        // surfacing an error for.
      }
      return next;
    });
  }, [subTab, selectedId, restoredSelection]);

  // NEW — §8: latest relevant timestamp for a dot-eligible sub-tab, or
  // null if there's nothing generated yet (no dot in that case either
  // way — an empty panel isn't "unread," it's just empty).
  function latestTabTimestamp(tabId) {
    if (tabId === "library") {
      // CHANGED — Overlap/Live-Viz guide, "replace entirely" decision:
      // this used to reduce over `edges` (the primary structural graph's
      // created_at timestamps) to drive the unread-dot. BacklinksView no
      // longer renders that graph -- it shows the topic tree instead --
      // so that timestamp source is now decoupled from what's on screen.
      // eo/secondary_data.py's topics/connections entries carry no
      // created_at of their own (see that module's schema), so there's
      // no equivalent real timestamp to substitute without a schema
      // addition. Returning null (no dot) rather than silently keeping
      // the stale `edges`-based signal, which would tell the user
      // "something changed" pointing at a graph they can no longer see.
      // Flagging rather than assuming a schema change belongs in this
      // patch.
      return null;
    }
    const panelKeys = TAB_PANEL_KEYS[tabId];
    if (!panelKeys) return null;
    return panelKeys.reduce((max, key) => {
      const ts = panelContent[key]?.updated_at;
      return ts && (!max || ts > max) ? ts : max;
    }, null);
  }

  // ISO 8601 strings from datetime.now(timezone.utc).isoformat() sort
  // correctly with plain string comparison, so no Date parsing needed.
  function hasUnseenUpdate(tabId) {
    const latest = latestTabTimestamp(tabId);
    if (!latest) return false;
    const viewed = lastViewed[tabId];
    return !viewed || latest > viewed;
  }

  useEffect(() => {
    // Falls back to the first workspace once workspaces have loaded, but
    // only after the restore effect above has had a chance to set
    // selectedId from localStorage — and also recovers if a previously
    // saved id no longer exists (e.g. that notebook was deleted).
    if (!restoredSelection || notebooks.length === 0) return;
    const stillExists = selectedId && notebooks.some((w) => w.id === selectedId);
    if (!stillExists) setSelectedId(notebooks[0].id);
  }, [notebooks, selectedId, restoredSelection]);

  async function loadNotebookData(wsId) {
    setLoadingNodes(true);
    setLoadingClusters(true);
    const [nodeList, edgeList, candidateList, clusterCandidateList, summaries, panels, topicsGraph] = await Promise.all([
      fetchWorkspaceNodes(wsId),
      fetchGraphEdges(wsId),
      fetchNoteCandidates(wsId),
      fetchClusterCandidates(wsId),
      fetchNodeSummaries(wsId),
      fetchPanelContentList(wsId), // NEW — §8: powers the unread-dot indicator
      fetchTopicsGraph(wsId),      // NEW — Backlinks-as-topic-tree
    ]);
    // FIX — if the user has since selected a different notebook while
    // this fetch was in flight, this result is stale: drop it instead of
    // overwriting what's currently on screen. (Loading flags only get
    // cleared by whichever call actually IS still relevant.)
    if (selectedIdRef.current !== wsId) return;
    setNodes(nodeList);
    setEdges(edgeList);
    setCandidates(candidateList);
    setClusterCandidates(clusterCandidateList);
    setNodeSummaries(summaries || {});
    setPanelContent(panels || {});
    setTopicNodes(topicsGraph?.nodes || []);
    setTopicEdges(topicsGraph?.edges || []);
    setFactsRefreshSignal((s) => s + 1);
    setLoadingNodes(false);
    setLoadingClusters(false);
  }

  // FIX — stale-response guard: `loadNotebookData` is async and can be
  // in flight when the user switches notebooks (e.g. via a slow upload's
  // onIngested callback firing after selectedId has already moved on —
  // see IngestionDropzone). Without this ref, whichever fetch resolves
  // last wins and can silently overwrite the currently-viewed notebook's
  // nodes/edges with a different notebook's data. This ref always holds
  // the *current* selection so loadNotebookData can check "is my result
  // still relevant?" right before committing state.
  const selectedIdRef = useRef(selectedId);
  useEffect(() => { selectedIdRef.current = selectedId; }, [selectedId]);

  useEffect(() => {
    if (selectedId) loadNotebookData(selectedId);
    else { setNodes([]); setEdges([]); setCandidates([]); setClusterCandidates([]); setNodeSummaries({}); setPanelContent({}); setTopicNodes([]); setTopicEdges([]); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId]);

  async function handleScanClusters() {
    setScanningClusters(true);
    try {
      setClusterCandidates(await proposeClusters(selected.id));
    } finally {
      setScanningClusters(false);
    }
  }

  async function handleAcceptCluster(candidateId) {
    await acceptClusterCandidate(selected.id, candidateId);
    setClusterCandidates(await fetchClusterCandidates(selected.id));
  }

  async function handleRejectCluster(candidateId) {
    await rejectClusterCandidate(selected.id, candidateId);
    setClusterCandidates(await fetchClusterCandidates(selected.id));
  }

  async function handleCreateNotebook(e) {
    e.preventDefault();
    if (!newName.trim() || submittingNotebook) return;
    setSubmittingNotebook(true);
    try {
      await createWorkspace(newName.trim());
      setNewName("");
      setCreating(false);
      await fetchWorkspaces();
    } finally {
      setSubmittingNotebook(false);
    }
  }
  // NEW — switches the active chat locally and makes sure the dock (or,
  // below `lg`, the full-screen overlay) is showing it — no tab jump,
  // this tab is self-contained regardless of viewport width.
  async function openInDock(chatId) {
    await switchChat(chatId);
    if (chatDockCollapsed) toggleChatDock();
  }

  // NEW — issue #3: "+" beside a notebook's name. Creates a chat nested
  // directly inside that notebook and opens it — replaces handleOpenChat
  // (and the standalone "Open chat" header button it backed) as the way
  // to reach a notebook's chat.
  async function handleCreateChatInProject(ws) {
    setCreatingChatForWs(ws.id);
    try {
      if (selectedId !== ws.id) setSelectedId(ws.id);
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

  // CHANGED — step 6.11.b: topicId is optional and purely pass-through
  // here. Every existing caller (the "Explain this step" button, and
  // anything else on this onOpenSubChat/handleOpenSubChat path) keeps
  // working unchanged since they simply don't supply a 3rd arg.
  async function handleOpenSubChat(wsId, prompt, topicId = null) {
    const chatId = await openScopedSubChat(wsId, prompt, topicId);
    await openInDock(chatId);
  }

  // NEW — how wide the selected-notebook content pane gets to be, for
  // sub-tabs that actually benefit from more than the default reading
  // width (max-w-3xl, same as every other sub-tab keeps). Both the chat
  // dock (chatDockCollapsed) and this tab's own project sidebar
  // (projectsCollapsed) free up real horizontal space when they
  // collapse, so this reacts to both rather than just one:
  //  - "library"/"insights": widen only once the dock's closed, since
  //    that's the difference between their two columns (Sources/
  //    Backlinks, Facts&Clusters/Suggested notes) fitting side by side
  //    or needing to stack (see LibraryView's/InsightsView's own
  //    dockOpen prop).
  //  - "diagrams": Mind Map can be large in either dimension on its
  //    own (no second column involved, see DiagramsView above), so it
  //    keeps expanding as either the dock or the sidebar collapses, and
  //    drops the cap entirely once both have.
  function contentMaxWidthClass() {
    if (subTab === "library" || subTab === "insights") return chatDockCollapsed ? "max-w-6xl" : "max-w-3xl";
    if (subTab === "diagrams") {
      if (chatDockCollapsed && projectsCollapsed) return "max-w-none";
      if (chatDockCollapsed || projectsCollapsed) return "max-w-5xl";
      return "max-w-3xl";
    }
    return "max-w-3xl";
  }

  // NEW — §8: promotes the notebook to Research and hands off navigation
  // to AppShell, which switches tabs and pre-selects it there.
  //
  // NEW — §2.6 step 4: now threads promoteMode through. AppShell still
  // switches tabs to show the target stage either way (useful to confirm
  // the promote landed) — for "partial" the workspace simply remains
  // selectable back here too, since active_stages now includes both.
  async function handlePromote(wsId, toStage = promoteTargetStage, mode = promoteMode) {
    setPromoting(true);
    setPromoteError(null);
    try {
      await promoteWorkspace(wsId, toStage, mode);
      onPromoted?.(toStage, wsId);
      setPromoteMode("complete");
    } catch (err) {
      setPromoteError(err.message);
    } finally {
      setPromoting(false);
    }
  }

  const selected = notebooks.find((w) => w.id === selectedId);
  // CHANGED — chat-gating, take 2. The first pass gated on "this
  // workspace has at least one chat" (chat_ids.length > 0), but that's
  // not the same thing as "a chat is actually open right now" — a
  // project can have chats and still have none of them selected (e.g.
  // you switch to this project from the sidebar without clicking any of
  // its chat rows, or your last-active chat belongs to a DIFFERENT
  // workspace entirely). In that state the content below was still
  // showing, with no visible answer to "which chat is this about?" —
  // exactly the ambiguity being fixed. `hasChatIds` only decides which
  // empty-state prompt to show (create vs. select); `hasActiveChat` —
  // whether `activeChatId` (the same "last active chat" the sidebar's
  // own `chat.id === activeChatId` highlight below already uses) is one
  // of THIS workspace's chats — is what actually gates the content.
  const workspaceChatIds = selected?.chat_ids || [];
  const hasChatIds = workspaceChatIds.length > 0;
  const hasActiveChat = hasChatIds && !!activeChatId && workspaceChatIds.includes(activeChatId);
  const ActiveIcon = SUB_TABS.find((t) => t.id === subTab)?.icon || FileText;

  // NEW — item #1: the Data bubble now lives in AppShell's top nav, not
  // floating over this tab's own content, so this just reports which
  // notebook (if any) is selected instead of rendering the bubble itself.
  useEffect(() => {
    onActiveWorkspaceChange?.(selected?.id || null, selected?.name);
  }, [selected?.id, selected?.name, onActiveWorkspaceChange]);

  return (
    <div className="flex h-full">
      {/* Notebook picker — this tab's own left column, distinct from the
          chat sidebar (which is hidden while this tab is active). */}
      {projectsCollapsed ? (
        <div className="w-10 shrink-0 border-r border-[var(--neutral-800)] flex flex-col items-center py-3 gap-3">
          <button onClick={toggleProjects} className="text-[var(--neutral-500)] hover:text-[var(--neutral-300)]" title="Show notebooks">
            <ChevronRight size={16} />
          </button>
        </div>
      ) : (
      <div className="w-56 shrink-0 border-r border-[var(--neutral-800)] flex flex-col h-full">
        <div className="flex items-center justify-between px-3 py-3 border-b border-[var(--neutral-800)]">
          <span className="text-xs font-medium text-[var(--neutral-400)] flex items-center gap-1.5">
            <NotebookText size={13} className={STAGE_THEME.note.color} /> Notebooks
          </span>
          <div className="flex items-center gap-2">
            <button onClick={() => setCreating((c) => !c)} title="New notebook" className="text-[var(--neutral-400)] hover:text-[var(--neutral-100)]">
              <Plus size={15} />
            </button>
            {/* NEW — collapsible sidebar, same affordance as ChatSidebar's
                own ChevronLeft toggle. */}
            <button onClick={toggleProjects} title="Hide notebooks" className="text-[var(--neutral-500)] hover:text-[var(--neutral-300)]">
              <ChevronLeft size={14} />
            </button>
          </div>
        </div>
        {creating && (
          <form onSubmit={handleCreateNotebook} className="px-3 py-2 border-b border-[var(--neutral-900)] flex gap-1">
            <input
              autoFocus
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder="Notebook name"
              disabled={submittingNotebook}
              className="flex-1 bg-black/30 border border-[var(--neutral-800)] rounded px-1.5 py-1 text-xs outline-none focus:border-[var(--cyber-cyan)] disabled:opacity-60"
            />
            <button type="submit" disabled={submittingNotebook || !newName.trim()}><Check size={13} className="text-green-400" /></button>
          </form>
        )}
        <div className="flex-1 overflow-y-auto">
          {notebooks.map((ws) => {
            // NEW — issue #3: same expand-to-show-nested-chats mechanic
            // ResearchTab now uses — this tab is also single-selection
            // (one notebook active at a time), so "expand" just means
            // "is the selected notebook," no separate toggle state.
            const isSelected = ws.id === selectedId;
            const memberChats = isSelected ? chats.filter((c) => ws.chat_ids.includes(c.id)) : [];
            return (
              <div key={ws.id} className="border-b border-[var(--neutral-900)]">
                <div
                  className={`group flex items-center gap-1 ${
                    isSelected ? "bg-[var(--neutral-800-a70)]" : "hover:bg-[var(--neutral-900)]"
                  }`}
                >
                  <button
                    onClick={() => setSelectedId(ws.id)}
                    className="flex-1 min-w-0 flex items-center justify-between gap-1 px-3 py-2 text-left"
                  >
                    <span className="flex items-center min-w-0">
                      <WorkspaceStageIcons workspace={ws} />
                      <span className="text-xs text-[var(--neutral-200)] truncate">{ws.name}</span>
                    </span>
                    {isSelected && <ChevronRight size={12} className="text-[var(--neutral-500)] shrink-0" />}
                  </button>
                  {/* NEW — issue #3: "+" creates a chat nested in this
                      notebook, same idea as starting a new chat under a
                      group in the Chat sidebar. */}
                  <button
                    onClick={(e) => { e.stopPropagation(); handleCreateChatInProject(ws); }}
                    title="New chat in this notebook"
                    className="shrink-0 opacity-0 group-hover:opacity-100 text-[var(--neutral-500)] hover:text-[var(--neutral-200)]"
                    disabled={creatingChatForWs === ws.id}
                  >
                    {creatingChatForWs === ws.id ? (
                      <Loader2 size={12} className="animate-spin" />
                    ) : (
                      <Plus size={13} />
                    )}
                  </button>
                  <button
                    onClick={() => setManagingWorkspace(ws)}
                    title="Rename or delete notebook"
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
          {notebooks.length === 0 && (
            <p className="px-3 py-3 text-xs text-[var(--neutral-600)]">No notebooks yet — create one to start ingesting sources.</p>
          )}
        </div>
      </div>
      )}

      {/* Selected notebook */}
      <div className="flex-1 min-h-0 overflow-y-auto">
        {!selected ? (
          <div className="h-full flex items-center justify-center text-sm text-[var(--neutral-600)]">
            Select or create a notebook to get started.
          </div>
        ) : (
          <div className={`relative p-5 space-y-4 ${contentMaxWidthClass()}`}>
            {/* CHANGED — widened from a flat max-w-3xl: Library's
                side-by-side Sources/Backlinks layout (see LibraryView
                below) and Diagrams' Mind Map (see DiagramsView below)
                need the extra room the chat dock's/sidebar's collapsed
                state just freed up on either side; every other sub-tab
                keeps the original narrower reading width — see
                contentMaxWidthClass() above. */}
            <div className="flex items-center justify-between">
              <h2 className="text-base font-medium text-[var(--neutral-100)]">{selected.name}</h2>
              <div className="flex items-center gap-2">
                {(() => {
                  // NEW — §2.2: a workspace can't be "promoted to" a stage
                  // it's already active in — matters now that partial
                  // promote can leave it active in more than one tab.
                  const activeHere = selected.active_stages || [selected.stage];
                  const availableTargets = PROMOTE_TARGETS.filter((s) => !activeHere.includes(s));
                  const targetStage = availableTargets.includes(promoteTargetStage)
                    ? promoteTargetStage
                    : availableTargets[0];
                  if (!availableTargets.length) return null;
                  return (
                    <div className="flex items-center gap-2">
                      <label className="sr-only" htmlFor="notebooks-promote-target">Promote to</label>
                      <select
                        id="notebooks-promote-target"
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
                        className="flex items-center rounded-lg border border-[var(--neutral-700)] overflow-hidden text-xs"
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
                        onClick={() => handlePromote(selected.id, targetStage)}
                        disabled={promoting}
                        className="flex items-center gap-1.5 text-xs border border-[var(--neutral-700)] text-[var(--neutral-200)] rounded-lg px-3 py-1.5 font-medium disabled:opacity-50"
                      >
                        {promoting ? <Loader2 size={13} className="animate-spin" /> : <ArrowUpRight size={13} />}
                        {promoteMode === "partial" ? "Add to" : "Promote to"} {PROMOTE_LABELS[targetStage]} →
                      </button>
                    </div>
                  );
                })()}
              </div>
            </div>
            {promoteError && <p className="text-xs text-red-400">{promoteError}</p>}

            {/* CHANGED — chat-gating, take 2: gates on hasActiveChat now
                (a specific chat selected, not just any chat existing —
                see the comment on hasActiveChat above), and picks
                between two empty states: CreateFirstChatPrompt when the
                notebook has no chats at all, SelectChatPrompt when it
                has chats but none of them is the active one. */}
            {!hasActiveChat ? (
              hasChatIds ? (
                <SelectChatPrompt workspace={selected} />
              ) : (
                <CreateFirstChatPrompt
                  workspace={selected}
                  creating={creatingChatForWs === selected.id}
                  onCreateChat={() => handleCreateChatInProject(selected)}
                />
              )
            ) : (
              <>
            <div className="flex items-center justify-between gap-2 border-b border-[var(--neutral-800)] pb-2">
              <nav className="flex gap-1">
                {SUB_TABS.map((t) => (
                  <button
                    key={t.id}
                    onClick={() => setSubTab(t.id)}
                    className={`flex items-center gap-1.5 text-xs rounded-lg px-3 py-1.5 ${
                      subTab === t.id ? "bg-[var(--accent)] text-[var(--accent-text)] font-medium" : "text-[var(--neutral-500)] hover:text-[var(--neutral-300)]"
                    }`}
                  >
                    <t.icon size={13} /> {t.label}
                    {t.id === "insights" && candidates.length + clusterCandidates.length > 0 && (
                      <span className="ml-0.5 text-[10px] bg-amber-500/20 text-amber-300 rounded-full px-1.5">
                        {candidates.length + clusterCandidates.length}
                      </span>
                    )}
                    {/* NEW — §8: unread dot for panel-generated sub-tabs
                        (Diagrams / Library / Study) — see
                        UNREAD_DOT_TABS/hasUnseenUpdate above for why
                        Insights uses its own count badge above instead. */}
                    {UNREAD_DOT_TABS.includes(t.id) && subTab !== t.id && hasUnseenUpdate(t.id) && (
                      <span
                        className="ml-0.5 w-1.5 h-1.5 rounded-full bg-amber-400 shrink-0"
                        title="New content since you last viewed this tab"
                      />
                    )}
                  </button>
                ))}
              </nav>
              {/* CHANGED — Phase 2 step 2.9: the popover/chips/manual
                  "Generate" button this used to render are gone — chat
                  (steps 2.5-2.8) is now the only way to trigger a
                  generation. What's left is a live status readout of
                  whatever's running or just finished, sourced from the
                  same dock key WorkspaceChatPanel's runGenerateTarget()
                  writes to, so it doesn't need workspace nodes or a
                  post-run refresh callback anymore — see
                  NotebooksGeneratePicker.jsx's file header. */}
              <NotebooksGeneratePicker
                workspaceId={selected.id}
                generateNotebooks={generateNotebooks}
                onNavigateSubTab={setSubTab}
              />
            </div>

            {/* CHANGED — Sources + Backlinks merged into "Library".
                dockOpen tells LibraryView whether the chat dock is
                currently taking up the right-hand side of the screen:
                side-by-side (Sources left, Backlinks right) when it's
                collapsed and there's room, stacked (Backlinks on top,
                Sources below) when it's open and there isn't. */}
            {subTab === "library" && (
              <LibraryView
                workspaceId={selected.id}
                nodes={nodes}
                edges={edges}
                loading={loadingNodes}
                onIngested={() => loadNotebookData(selected.id)}
                onSelectNode={(node) => {
                  setPreviewNode(node);
                  // NEW — step 2.6a: opening a source is a strong signal
                  // it's what the person means by "this"/"here" in chat.
                  setActiveContext({ type: "source", id: node.node_id, label: node.title || node.node_id });
                }}
                onDeleteNode={async (nodeId) => {
                  await deleteWorkspaceNode(selected.id, nodeId);
                  await loadNotebookData(selected.id);
                }}
                onRenameNode={async (node, title) => {
                  await renameWorkspaceNode(selected.id, node.node_id, title);
                  await loadNotebookData(selected.id);
                }}
                topicNodes={topicNodes}
                topicEdges={topicEdges}
                topicPulsingIds={topicPulsingIds}
                dockOpen={!chatDockCollapsed}
              />
            )}
            {/* CHANGED — Mind Map + Workflows merged into "Diagrams",
                always stacked (Mind Map on top, Workflows below — see
                DiagramsView below for why it's never side by side). */}
            {subTab === "diagrams" && (
              <DiagramsView
                workspaceId={selected.id}
                onOpenSubChat={handleOpenSubChat}
                fetchPanelContent={fetchPanelContent}
                generateNotebooks={generateNotebooks}
                generateTopicWorkflow={generateTopicWorkflow}
                onActiveContext={setActiveContext}
                topicNodes={topicNodes}
                fetchWorkspaceProgress={fetchWorkspaceProgress}
                setWorkspaceProgress={setWorkspaceProgress}
              />
            )}
            {subTab === "study" && <StudyView workspaceId={selected.id} />}
            {/* CHANGED — Facts + Clusters + Suggested notes merged into
                "Insights". dockOpen tells InsightsView whether the chat
                dock is currently taking up the right-hand side of the
                screen: side-by-side (Facts & Clusters left, Suggested
                notes right) when it's collapsed and there's room,
                stacked (Suggested notes on top, Facts & Clusters below)
                when it's open and there isn't — same layout contract as
                LibraryView above. */}
            {subTab === "insights" && (
              <InsightsView
                workspaceId={selected.id}
                fetchWorkspaceFacts={fetchWorkspaceFacts}
                saveWorkspaceFacts={saveWorkspaceFacts}
                fetchFactCandidates={fetchFactCandidates}
                acceptFactCandidate={acceptFactCandidate}
                rejectFactCandidate={rejectFactCandidate}
                factsRefreshSignal={factsRefreshSignal}
                clusterCandidates={clusterCandidates}
                loadingClusters={loadingClusters}
                scanningClusters={scanningClusters}
                onScanClusters={handleScanClusters}
                onAcceptCluster={handleAcceptCluster}
                onRejectCluster={handleRejectCluster}
                candidates={candidates}
                onAcceptCandidate={async (candidateId) => { await acceptNoteCandidate(selected.id, candidateId); await loadNotebookData(selected.id); }}
                onRejectCandidate={async (candidateId) => { await rejectNoteCandidate(selected.id, candidateId); await loadNotebookData(selected.id); }}
                dockOpen={!chatDockCollapsed}
              />
            )}
            {/* CHANGED — Corrections + Patch Review merged into
                "Corrections". Always stacked (capture on top, Patch
                Review below — see CorrectionsView above for why this
                one doesn't flip to side-by-side like Library/Insights
                do). onQueued still bumps patchReviewRefreshSignal so an
                accepted match shows up below without navigating away. */}
            {subTab === "corrections" && (
              <CorrectionsView
                workspaceId={selected.id}
                nodes={nodes}
                edges={edges}
                submitCorrection={submitCorrection}
                onQueued={() => setPatchReviewRefreshSignal((n) => n + 1)}
                fetchPatchCandidates={fetchPatchCandidates}
                acceptPatchCandidate={acceptPatchCandidate}
                rejectPatchCandidate={rejectPatchCandidate}
                refreshSignal={patchReviewRefreshSignal}
              />
            )}
              </>
            )}
          </div>
        )}
      </div>

      {/* Desktop dock — side-by-side, lg+. */}
      <div className="hidden lg:flex shrink-0 border-l border-[var(--neutral-800)]" style={{ width: chatDockCollapsed ? undefined : 560 }}>
        <WorkspaceChatPanel collapsed={chatDockCollapsed} onToggleCollapse={toggleChatDock} workspaceId={selected?.id} onNavigateSubTab={setSubTab} stacked hideAttach activeContext={activeContext} />
      </div>

      {/* Below lg — full-screen overlay instead of a side dock, so this
          tab never depends on the standalone Chat tab, at any width. */}
      {!chatDockCollapsed && (
        <div className="lg:hidden fixed inset-0 z-40 bg-[var(--neutral-950)]">
          <WorkspaceChatPanel collapsed={false} onToggleCollapse={toggleChatDock} workspaceId={selected?.id} onNavigateSubTab={setSubTab} stacked hideAttach activeContext={activeContext} />
        </div>
      )}
      {chatDockCollapsed && (
        <button
          onClick={toggleChatDock}
          title="Open chat"
          className="lg:hidden fixed bottom-4 right-4 z-40 bg-[var(--accent)] text-[var(--accent-text)] rounded-full p-3 shadow-lg"
        >
          <MessageSquareText size={18} />
        </button>
      )}

      <NodePreviewModal node={previewNode} onClose={() => setPreviewNode(null)} />
      {/* NEW — issue #3: same delete-confirmation affordance as
          ChatSidebar's own per-chat delete, just scoped to a nested
          notebook chat here. */}
      <ConfirmDialog
        open={!!pendingDeleteChat}
        title="Delete chat"
        message={`Delete "${pendingDeleteChat?.title}"? Its messages and memory can't be recovered.`}
        confirmLabel="Delete"
        tone="danger"
        onConfirm={confirmDeleteChat}
        onCancel={() => setPendingDeleteChat(null)}
      />
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

// Item 6 (perf audit, tab-body pass): NotebooksTab takes props from its parent
// (onPromoted, onActiveWorkspaceChange). Wrapped in memo() now that
// SessionContext's useCallback pass (item 2) means its stable-identity
// props/callbacks stay stable across unrelated parent re-renders -- prop
// objects/arrays it reads (workspaces, chats, etc.) are only ever replaced,
// never mutated in place, so a shallow prop comparison here is meaningful.
export default memo(NotebooksTab);
