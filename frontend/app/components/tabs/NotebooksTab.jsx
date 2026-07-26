"use client";
import { useEffect, useRef, useState } from "react";
import { useSession } from "../../context/SessionContext";
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
  GraduationCap, Sparkles, X, Check, ChevronRight, BookMarked, Loader2, Layers,
  Trash2, MoreVertical, ArrowUpRight, Pencil, RefreshCw, ListChecks, RotateCcw,
  Wrench, Send, // NEW — Data Layer architecture §8a: Corrections tab
  GitCompare, // NEW — Data Layer architecture §8c: Patch Review tab
} from "lucide-react";

const SUB_TABS = [
  { id: "sources", label: "Sources", icon: FileText },
  { id: "mindmap", label: "Mind Map", icon: Network },
  { id: "backlinks", label: "Backlinks", icon: GitBranch },
  { id: "workflows", label: "Workflows", icon: ListChecks },
  { id: "study", label: "Study", icon: GraduationCap },
  { id: "facts", label: "Facts", icon: BookMarked },
  { id: "clusters", label: "Clusters", icon: Layers },
  { id: "candidates", label: "Suggested notes", icon: Sparkles },
  // NEW — Data Layer architecture §8a: capture, wired to §8b's locator
  // + §8c's Patch Review pending store as of this patch.
  { id: "corrections", label: "Corrections", icon: Wrench },
  // NEW — Data Layer architecture §8c: before/after review + accept/
  // reject for whatever the Corrections tab's submissions located.
  { id: "patch-review", label: "Patch Review", icon: GitCompare },
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
const UNREAD_DOT_TABS = ["mindmap", "backlinks", "workflows", "study"];
// Which eo/panel_content.py panel_key(s) back each dot-eligible sub-tab
// that's driven by panel_content specifically (backlinks/clusters/
// candidates aren't — they compare against graph_edges/candidate
// timestamps directly instead, see latestTabTimestamp below).
const TAB_PANEL_KEYS = {
  mindmap: ["mindmap"],
  workflows: ["suggested_workflows"],
  study: ["study_flashcards", "study_quiz", "study_guide"],
};
// NEW — §6.2: separate collapse key from WorkspaceChatPanel's own internal
// WORKING_PANEL_KEY — this one folds away the *whole* dock (chat +
// WorkingPanel together), same "own toggle, own storage key" pattern the
// left ChatSidebar already uses for itself.
const CHAT_DOCK_KEY = "minime_notebooks_chatdock_collapsed";
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
function MindMapView({ workspaceId, onOpenSubChat, fetchPanelContent, generateNotebooks }) {
  const [content, setContent] = useState("");
  const [updatedAt, setUpdatedAt] = useState(null);
  const [loading, setLoading] = useState(true);
  const [regenerating, setRegenerating] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetchPanelContent(workspaceId, "mindmap").then((saved) => {
      if (cancelled) return;
      setContent(saved?.content || "");
      setUpdatedAt(saved?.updated_at || null);
      setLoading(false);
    });
    return () => { cancelled = true; };
  }, [workspaceId, fetchPanelContent]);

  async function handleRegenerate() {
    setRegenerating(true);
    setError(null);
    try {
      const { branches } = await generateNotebooks(workspaceId, ["mindmap"], null);
      const branch = branches.find((b) => b.panel_key === "mindmap");
      if (branch?.status === "error") throw new Error(branch.error || "Mind map generation failed");
      setContent(branch?.result?.content || "");
      setUpdatedAt(branch?.result?.updated_at || null);
    } catch (err) {
      setError(String(err.message || err));
    } finally {
      setRegenerating(false);
    }
  }

  if (loading) {
    return <div className="text-xs text-[var(--neutral-600)] flex items-center gap-1.5"><Loader2 size={12} className="animate-spin" /> Loading…</div>;
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <p className="text-xs text-[var(--neutral-500)]">
          {content
            ? "Click any node to open a sub-chat scoped to this notebook."
            : "No mind map yet — Generate reads this notebook's sources and proposes one."}
        </p>
        <div className="flex items-center gap-2 shrink-0">
          {updatedAt && !regenerating && (
            <span className="text-[10px] text-[var(--neutral-600)]">Generated {timeAgo(updatedAt)}</span>
          )}
          <button
            onClick={handleRegenerate}
            disabled={regenerating}
            className="flex items-center gap-1.5 text-xs bg-[var(--accent)] text-[var(--accent-text)] rounded px-3 py-1.5 font-medium disabled:opacity-50"
          >
            {regenerating ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
            {content ? "Regenerate" : "Generate"}
          </button>
        </div>
      </div>
      {error && <p className="text-[11px] text-red-400">{error}</p>}
      {content ? (
        <div className="rounded-lg border border-[var(--neutral-800)] bg-black/30 p-4">
          <MermaidDiagram
            mermaidText={content}
            onNodeClick={(label) => onOpenSubChat(workspaceId, `Tell me more about "${label}" using this notebook's sources.`)}
            hideSourceOnFail /* NEW — bug #6a fix */
            showControls /* NEW — §7 refinements #5/#6: zoom/pan + export as image */
            maxHeight={520}
            exportFilename="mind-map"
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

function BacklinksView({ workspaceId, nodes, edges, nodeSummaries, loading, onDetect, onSelectNode, generateNotebooks, onRegenerated }) {
  // NEW — Notebooks integration guide §6.6: "Backlinks subtab likely
  // needs its own Regenerate-equivalent trigger too... worth deciding
  // whether Generate's backlinks target should also be reachable from
  // a button on this subtab itself, not just the picker." Same
  // handleRegenerate shape as MindMapView above -- calls the picker's
  // own generateNotebooks(..., ["backlinks"], null) endpoint rather
  // than a separate one-off route, so this button and a chat/picker
  // "Generate backlinks" command are the exact same call.
  const [regenerating, setRegenerating] = useState(false);
  const [status, setStatus] = useState(null); // last run's {status, edges_created} or {error}

  async function handleRegenerate() {
    setRegenerating(true);
    setStatus(null);
    try {
      const { branches } = await generateNotebooks(workspaceId, ["backlinks"], null);
      const branch = branches.find((b) => b.panel_key === "backlinks");
      if (branch?.status === "error") throw new Error(branch.error || "Concept graph generation failed");
      setStatus(branch?.result || { status: "done" });
      await onRegenerated?.();
    } catch (err) {
      setStatus({ error: String(err.message || err) });
    } finally {
      setRegenerating(false);
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <p className="text-xs text-[var(--neutral-500)]">
          Concept links between sources in this notebook — click a node to see why it's connected.
        </p>
        <div className="flex items-center gap-3 shrink-0">
          <button
            onClick={onDetect}
            title="Cheap sanity check: case-insensitive substring match — does one source's text literally contain another source's title? No LLM call."
            className="text-[11px] text-[var(--neutral-400)] hover:text-[var(--neutral-200)]"
          >
            Quick title-match scan
          </button>
          <button
            onClick={handleRegenerate}
            disabled={regenerating}
            title="LLM pass: judges conceptual relatedness between sources and writes a relation phrase + summary for each link — powers the click-to-see-rationale panel below."
            className="flex items-center gap-1.5 text-xs bg-[var(--accent)] text-[var(--accent-text)] rounded px-3 py-1.5 font-medium disabled:opacity-50"
          >
            {regenerating ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
            Regenerate concept graph
          </button>
        </div>
      </div>
      {status?.error && <p className="text-[11px] text-red-400">{status.error}</p>}
      {status && !status.error && status.status === "up_to_date" && (
        <p className="text-[11px] text-[var(--neutral-600)]">Already up to date — no new sources since the last run.</p>
      )}
      {status && !status.error && status.status === "done" && (
        <p className="text-[11px] text-[var(--neutral-600)]">
          {(status.edges_created || []).length} new concept link{(status.edges_created || []).length === 1 ? "" : "s"} found.
        </p>
      )}
      <div className="h-[420px] rounded-lg border border-[var(--neutral-800)] overflow-hidden">
        {loading ? (
          <div className="h-full flex items-center justify-center text-xs text-[var(--neutral-600)]">Loading…</div>
        ) : nodes.length === 0 ? (
          <div className="h-full flex items-center justify-center text-xs text-[var(--neutral-600)]">Nothing to graph yet.</div>
        ) : (
          <KnowledgeGraphView nodes={nodes} edges={edges} nodeSummaries={nodeSummaries} onSelectNode={onSelectNode} />
        )}
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
                <button
                  onClick={() =>
                    onOpenSubChat?.(workspaceId, `Explain this step in more detail: "${step.label}" (part of the "${workflow.title}" procedure), using this notebook's sources.`)
                  }
                  title="Ask about this step"
                  className="shrink-0 text-[var(--neutral-600)] hover:text-[var(--cyber-cyan)]"
                >
                  <MessageSquareText size={11} />
                </button>
              )}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

function WorkflowsView({ workspaceId, onOpenSubChat, fetchPanelContent, generateNotebooks }) {
  const [workflows, setWorkflows] = useState(null); // null = not loaded yet
  const [updatedAt, setUpdatedAt] = useState(null);
  const [loading, setLoading] = useState(true);
  const [regenerating, setRegenerating] = useState(false);
  const [error, setError] = useState(null);

  function parseWorkflows(raw) {
    if (!raw) return [];
    try {
      const parsed = JSON.parse(raw);
      return Array.isArray(parsed?.workflows) ? parsed.workflows : [];
    } catch {
      return []; // malformed/legacy content shouldn't crash the tab — treat as "nothing generated"
    }
  }

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetchPanelContent(workspaceId, "suggested_workflows").then((saved) => {
      if (cancelled) return;
      setWorkflows(parseWorkflows(saved?.content));
      setUpdatedAt(saved?.updated_at || null);
      setLoading(false);
    });
    return () => { cancelled = true; };
  }, [workspaceId, fetchPanelContent]);

  async function handleRegenerate() {
    setRegenerating(true);
    setError(null);
    try {
      const { branches } = await generateNotebooks(workspaceId, ["workflows"], null);
      const branch = branches.find((b) => b.panel_key === "workflows");
      if (branch?.status === "error") throw new Error(branch.error || "Workflow generation failed");
      setWorkflows(parseWorkflows(branch?.result?.content));
      setUpdatedAt(branch?.result?.updated_at || null);
    } catch (err) {
      setError(String(err.message || err));
    } finally {
      setRegenerating(false);
    }
  }

  if (loading) {
    return <div className="text-xs text-[var(--neutral-600)] flex items-center gap-1.5"><Loader2 size={12} className="animate-spin" /> Loading…</div>;
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <p className="text-xs text-[var(--neutral-500)]">
          {workflows?.length
            ? "Step-by-step procedures found in this notebook's sources."
            : "No clear step-by-step processes found in this notebook — that's a normal result for purely conceptual material."}
        </p>
        <div className="flex items-center gap-2 shrink-0">
          {updatedAt && !regenerating && (
            <span className="text-[10px] text-[var(--neutral-600)]">Generated {timeAgo(updatedAt)}</span>
          )}
          <button
            onClick={handleRegenerate}
            disabled={regenerating}
            className="flex items-center gap-1.5 text-xs bg-[var(--accent)] text-[var(--accent-text)] rounded px-3 py-1.5 font-medium disabled:opacity-50"
          >
            {regenerating ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
            Generate
          </button>
        </div>
      </div>
      {error && <p className="text-[11px] text-red-400">{error}</p>}
      {workflows?.length > 0 && (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          {workflows.map((wf, i) => (
            <WorkflowCard key={`${wf.title}-${i}`} workflow={wf} workspaceId={workspaceId} onOpenSubChat={onOpenSubChat} />
          ))}
        </div>
      )}
    </div>
  );
}

// --- Study sub-view ------------------------------------------------------
// §4.5/§4.7: flashcard flipper, quiz runner, study-guide viewer — plain
// generated Markdown pasted in from a chat run, same "paste the role's
// stage_output text" pattern the Mind Map view above already uses.

function StudyView({ workspaceId }) {
  const { synthesizePodcast, buildVideoOverview, fetchPanelContent, savePanelContent } = useSession();
  const [kind, setKind] = useState("flashcards");
  const [text, setText] = useState("");
  const [rendered, setRendered] = useState("");
  const [quizNodeId, setQuizNodeId] = useState("");
  // NEW — persistence for the three paste-and-Load kinds (flashcards,
  // quiz, study_guide) via eo/panel_content.py.
  //
  // CHANGED — bug audit §9 trace: this used to build the panel_key by
  // templating `study_${kind}` directly. That's correct for two of the
  // three kinds (flashcards -> study_flashcards, quiz -> study_quiz) but
  // wrong for the third: kind "study_guide" already has the prefix, so
  // the template produced "study_study_guide" -- not a real panel_key
  // (eo/panel_content.py's VALID_PANEL_KEYS only has "study_guide"), so
  // every fetchPanelContent call for this tab hit a 400, which
  // fetchPanelContent's own !res.ok branch silently swallows into an
  // empty-content result -- indistinguishable from "nothing generated
  // yet." Worse, savePanelContent doesn't check res.ok at all, so
  // clicking Load still called setSavedAt(Date.now()) and showed
  // "Saved" even though the PUT itself 400'd and nothing was persisted.
  // Net effect: a Study Guide built via Regenerate (which correctly
  // saves under plain "study_guide") was invisible on this tab, and
  // anything pasted+Loaded here appeared to save but silently didn't.
  // Explicit map instead of a template removes the ambiguity for good.
  const PANEL_KEY_BY_KIND = { flashcards: "study_flashcards", quiz: "study_quiz", study_guide: "study_guide" };
  const PERSISTED_KINDS = Object.keys(PANEL_KEY_BY_KIND);
  const [loadingText, setLoadingText] = useState(true);
  const [savingText, setSavingText] = useState(false);
  const [savedAt, setSavedAt] = useState(null);
  const [saveError, setSaveError] = useState("");

  useEffect(() => {
    if (!PERSISTED_KINDS.includes(kind)) { setLoadingText(false); return; }
    let cancelled = false;
    setLoadingText(true);
    setSavedAt(null);
    fetchPanelContent(workspaceId, PANEL_KEY_BY_KIND[kind]).then((saved) => {
      if (cancelled) return;
      const content = saved?.content || "";
      setText(content);
      setRendered(content);
      setLoadingText(false);
    });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspaceId, kind]);

  async function handleLoad() {
    setRendered(text);
    if (!PERSISTED_KINDS.includes(kind)) return;
    setSavingText(true);
    setSaveError("");
    try {
      await savePanelContent(workspaceId, PANEL_KEY_BY_KIND[kind], text);
      setSavedAt(Date.now());
    } catch (err) {
      setSaveError(String(err.message || err));
    } finally {
      setSavingText(false);
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
      {kind === "video_overview" ? (
        <p className="text-xs text-[var(--neutral-500)]">
          Paste the Markdown from a <code className="text-amber-300">slide_planner</code> chat run, then build a
          narrated slideshow using audio from a podcast you've already synthesized under the same title below.
        </p>
      ) : (
        <p className="text-xs text-[var(--neutral-500)]">
          Paste the Markdown from a <code className="text-amber-300">{kind === "flashcards" ? "flashcard_writer" : kind === "quiz" ? "quiz_writer" : kind === "study_guide" ? "study_guide_writer" : "podcast_scriptwriter"}</code> chat run.
        </p>
      )}
      {kind !== "video_overview" && loadingText && PERSISTED_KINDS.includes(kind) ? (
        <div className="text-xs text-[var(--neutral-600)] flex items-center gap-1.5"><Loader2 size={12} className="animate-spin" /> Loading saved text…</div>
      ) : kind !== "video_overview" && (
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={5}
          placeholder={kind === "podcast" ? "HOST A: Welcome back to the show...\nHOST B: Today we're covering..." : undefined}
          className="w-full bg-black/30 border border-[var(--neutral-800)] rounded px-2 py-1.5 text-xs font-mono outline-none focus:border-[var(--cyber-cyan)]"
        />
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
      {kind === "quiz" && (
        <input
          value={quizNodeId}
          onChange={(e) => setQuizNodeId(e.target.value)}
          placeholder="Quiz node_id (optional — enables progress tracking)"
          className="w-full bg-black/30 border border-[var(--neutral-800)] rounded px-2 py-1.5 text-xs outline-none focus:border-[var(--cyber-cyan)]"
        />
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
      ) : (
        <>
          <div className="flex items-center gap-2">
            <button
              onClick={handleLoad}
              disabled={savingText}
              className="text-xs bg-[var(--accent)] text-[var(--accent-text)] rounded px-3 py-1.5 font-medium disabled:opacity-50"
            >
              {savingText ? "Saving…" : "Load & Save"}
            </button>
            {savedAt && !savingText && <span className="text-[11px] text-[var(--neutral-600)]">Saved</span>}
            {saveError && !savingText && <span className="text-[11px] text-red-400">{saveError}</span>}
          </div>

          {rendered && (
            <div className="rounded-lg border border-[var(--neutral-800)] p-4">
              {kind === "flashcards" && <FlashcardFlipper markdownText={rendered} />}
              {kind === "quiz" && <QuizRunner quizText={rendered} workspaceId={workspaceId} quizNodeId={quizNodeId || undefined} />}
              {kind === "study_guide" && <StudyGuideViewer markdownText={rendered} />}
            </div>
          )}
        </>
      )}
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

// --- Corrections sub-view ----------------------------------------------
// Data Layer architecture §8a: capture -- a file-scope picker (one
// source, or "All files") and a plain-language box describing what's
// wrong. As of §8c, submitting actually runs the pipeline: the server
// hands the text to §8b's agents/correction_locator.py, and either
// queues a located candidate in eo/correction_candidates.py's pending
// store for the Patch Review tab to render as a before/after, or comes
// back with a plain reason there was nothing to locate -- shown here
// inline, since a dead end never reaches Patch Review at all.
function CorrectionsView({ workspaceId, nodes, edges, submitCorrection, onQueued }) {
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
                <p className="text-[10px] text-green-400">Located a match — check Patch Review to accept or discard it.</p>
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
    return <p className="text-xs text-[var(--neutral-600)]">Nothing waiting for review — corrections that find a match on the Corrections tab show up here.</p>;
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
        <p className="text-xs text-[var(--neutral-300)] whitespace-pre-wrap">{node.content}</p>
      </div>
    </div>
  );
}

// --- Main tab ------------------------------------------------------

export default function NotebooksTab({ onPromoted, onActiveWorkspaceChange }) {
   const {
     workspaces, fetchWorkspaces, createWorkspace, chats, promoteWorkspace,
     fetchWorkspaceNodes, deleteWorkspaceNode, renameWorkspaceNode, fetchGraphEdges, detectBacklinks, fetchNodeSummaries,
     fetchNoteCandidates, acceptNoteCandidate, rejectNoteCandidate,
     fetchWorkspaceFacts, saveWorkspaceFacts, fetchFactCandidates, acceptFactCandidate, rejectFactCandidate,
     submitCorrection, fetchPatchCandidates, acceptPatchCandidate, rejectPatchCandidate,
     fetchPanelContent, savePanelContent, fetchPanelContentList,
     generateNotebooks,
     proposeClusters, fetchClusterCandidates, acceptClusterCandidate, rejectClusterCandidate,
    openScopedSubChat,
  } = useSession();
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
  const [subTab, setSubTab] = useState("sources");
  const [nodes, setNodes] = useState([]);
  const [edges, setEdges] = useState([]);
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
  // NEW — issue #3: nested-chat create/rename/delete state, same shape as
  // ResearchTab's own (and ChatSidebar's editingId/editTitle/pendingDelete
  // before that) — scoped to this tab's notebook list.
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
  // so the initial (pre-restore) null/"sources" values don't overwrite
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
    if (tabId === "backlinks") {
      return edges.reduce((max, e) => (e.created_at && (!max || e.created_at > max) ? e.created_at : max), null);
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
    const [nodeList, edgeList, candidateList, clusterCandidateList, summaries, panels] = await Promise.all([
      fetchWorkspaceNodes(wsId),
      fetchGraphEdges(wsId),
      fetchNoteCandidates(wsId),
      fetchClusterCandidates(wsId),
      fetchNodeSummaries(wsId),
      fetchPanelContentList(wsId), // NEW — §8: powers the unread-dot indicator
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
    else { setNodes([]); setEdges([]); setCandidates([]); setClusterCandidates([]); setNodeSummaries({}); setPanelContent({}); }
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

  async function handleOpenSubChat(wsId, prompt) {
    const chatId = await openScopedSubChat(wsId, prompt);
    await openInDock(chatId);
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
      <div className="w-56 shrink-0 border-r border-[var(--neutral-800)] flex flex-col h-full">
        <div className="flex items-center justify-between px-3 py-3 border-b border-[var(--neutral-800)]">
          <span className="text-xs font-medium text-[var(--neutral-400)] flex items-center gap-1.5">
            <NotebookText size={13} className={STAGE_THEME.note.color} /> Notebooks
          </span>
          <button onClick={() => setCreating((c) => !c)} title="New notebook" className="text-[var(--neutral-400)] hover:text-[var(--neutral-100)]">
            <Plus size={15} />
          </button>
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

      {/* Selected notebook */}
      <div className="flex-1 min-h-0 overflow-y-auto">
        {!selected ? (
          <div className="h-full flex items-center justify-center text-sm text-[var(--neutral-600)]">
            Select or create a notebook to get started.
          </div>
        ) : (
          <div className="relative p-5 space-y-4 max-w-3xl">
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
                    {t.id === "candidates" && candidates.length > 0 && (
                      <span className="ml-0.5 text-[10px] bg-amber-500/20 text-amber-300 rounded-full px-1.5">{candidates.length}</span>
                    )}
                    {t.id === "clusters" && clusterCandidates.length > 0 && (
                      <span className="ml-0.5 text-[10px] bg-amber-500/20 text-amber-300 rounded-full px-1.5">{clusterCandidates.length}</span>
                    )}
                    {/* NEW — §8: unread dot for panel-generated sub-tabs
                        (Mind Map / Backlinks / Workflows / Study) — see
                        UNREAD_DOT_TABS/hasUnseenUpdate above for why
                        Suggested notes/Clusters use their existing count
                        badge instead. */}
                    {UNREAD_DOT_TABS.includes(t.id) && subTab !== t.id && hasUnseenUpdate(t.id) && (
                      <span
                        className="ml-0.5 w-1.5 h-1.5 rounded-full bg-amber-400 shrink-0"
                        title="New content since you last viewed this tab"
                      />
                    )}
                  </button>
                ))}
              </nav>
              {/* NEW — Notebooks integration guide §4.1: the picker +
                  free-text chip-confirmation "Generate" command, wired
                  straight to POST .../notebooks/generate. Refreshes
                  nodes/edges/candidates on completion since Clusters,
                  Facts, and Suggested Notes all land in lists this tab
                  already renders from loadNotebookData's state. */}
              <NotebooksGeneratePicker
                workspaceId={selected.id}
                nodes={nodes}
                generateNotebooks={generateNotebooks}
                onComplete={() => loadNotebookData(selected.id)}
                onNavigateSubTab={setSubTab}
              />
            </div>

            {subTab === "sources" && (
              <SourcesView
                workspaceId={selected.id}
                nodes={nodes}
                edges={edges}
                loading={loadingNodes}
                onIngested={() => loadNotebookData(selected.id)}
                onSelectNode={setPreviewNode}
                onDeleteNode={async (nodeId) => {
                  await deleteWorkspaceNode(selected.id, nodeId);
                  await loadNotebookData(selected.id);
                }}
                onRenameNode={async (node, title) => {
                  await renameWorkspaceNode(selected.id, node.node_id, title);
                  await loadNotebookData(selected.id);
                }}
              />
            )}
            {subTab === "mindmap" && (
              <MindMapView
                workspaceId={selected.id}
                onOpenSubChat={handleOpenSubChat}
                fetchPanelContent={fetchPanelContent}
                generateNotebooks={generateNotebooks}
              />
            )}
            {subTab === "backlinks" && (
              <BacklinksView
                workspaceId={selected.id}
                nodes={nodes}
                edges={edges}
                nodeSummaries={nodeSummaries}
                loading={loadingNodes}
                onDetect={async () => { await detectBacklinks(selected.id); await loadNotebookData(selected.id); }}
                onSelectNode={setPreviewNode}
                generateNotebooks={generateNotebooks}
                onRegenerated={() => loadNotebookData(selected.id)}
              />
            )}
            {subTab === "workflows" && (
              <WorkflowsView
                workspaceId={selected.id}
                onOpenSubChat={handleOpenSubChat}
                fetchPanelContent={fetchPanelContent}
                generateNotebooks={generateNotebooks}
              />
            )}
            {subTab === "study" && <StudyView workspaceId={selected.id} />}
            {subTab === "facts" && (
              <FactsView
                workspaceId={selected.id}
                fetchWorkspaceFacts={fetchWorkspaceFacts}
                saveWorkspaceFacts={saveWorkspaceFacts}
                fetchFactCandidates={fetchFactCandidates}
                acceptFactCandidate={acceptFactCandidate}
                rejectFactCandidate={rejectFactCandidate}
                factsRefreshSignal={factsRefreshSignal}
              />
            )}
            {subTab === "clusters" && (
              <ClustersView
                candidates={clusterCandidates}
                loading={loadingClusters}
                scanning={scanningClusters}
                onScan={handleScanClusters}
                onAccept={handleAcceptCluster}
                onReject={handleRejectCluster}
              />
            )}
            {subTab === "candidates" && (
              <CandidatesView
                workspaceId={selected.id}
                candidates={candidates}
                onAccept={async (candidateId) => { await acceptNoteCandidate(selected.id, candidateId); await loadNotebookData(selected.id); }}
                onReject={async (candidateId) => { await rejectNoteCandidate(selected.id, candidateId); await loadNotebookData(selected.id); }}
              />
            )}
            {subTab === "corrections" && (
              <CorrectionsView
                workspaceId={selected.id}
                nodes={nodes}
                edges={edges}
                submitCorrection={submitCorrection}
                onQueued={() => setPatchReviewRefreshSignal((n) => n + 1)}
              />
            )}
            {subTab === "patch-review" && (
              <PatchReviewView
                workspaceId={selected.id}
                fetchPatchCandidates={fetchPatchCandidates}
                acceptPatchCandidate={acceptPatchCandidate}
                rejectPatchCandidate={rejectPatchCandidate}
                refreshSignal={patchReviewRefreshSignal}
              />
            )}
          </div>
        )}
      </div>

      {/* Desktop dock — side-by-side, lg+. */}
      <div className="hidden lg:flex shrink-0 border-l border-[var(--neutral-800)]" style={{ width: chatDockCollapsed ? undefined : 560 }}>
        <WorkspaceChatPanel collapsed={chatDockCollapsed} onToggleCollapse={toggleChatDock} workspaceId={selected?.id} onNavigateSubTab={setSubTab} />
      </div>

      {/* Below lg — full-screen overlay instead of a side dock, so this
          tab never depends on the standalone Chat tab, at any width. */}
      {!chatDockCollapsed && (
        <div className="lg:hidden fixed inset-0 z-40 bg-[var(--neutral-950)]">
          <WorkspaceChatPanel collapsed={false} onToggleCollapse={toggleChatDock} workspaceId={selected?.id} onNavigateSubTab={setSubTab} />
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

