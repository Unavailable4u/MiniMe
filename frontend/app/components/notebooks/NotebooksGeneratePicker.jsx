"use client";
import { useState, useMemo, useRef, useEffect } from "react";
import {
  Sparkles, X, Play, Loader2, Check, AlertCircle, ChevronRight,
  Layers, BookMarked, GraduationCap, Network, GitBranch,
} from "lucide-react";

// Notebooks integration guide §4.1: "picker and free-text aren't really
// two separate systems — free text is just an alternate way to pre-fill
// the same picker UI." This is that picker: a chip-multiselect of every
// wired Generate target (api/server.py's NOTEBOOKS_GENERATE_TARGETS),
// plus a free-text field that locally parses into the same chips rather
// than a second, separate flow.
//
// SCOPE NOTE for whoever picks this up next: the guide's real free-text
// entry point is meant to be the chat compose box itself (typing "make
// flashcards" into WorkspaceChatPanel should short-circuit the normal
// staffed-dispatcher send and land here instead). That's a deeper change
// to a send path shared by every other domain tab, and is deliberately
// NOT touched by this patch — this free-text field lives inside the
// picker's own popover, a manual "Generate" click away, not inside the
// chat textarea. Wiring the chat box as a second entry point into this
// same component is the natural next step.
//
// Similarly, the parse below is a small local keyword matcher, not an
// LLM call — good enough to pre-fill chips for review, but it's the
// "accepting the misparse risk" the guide calls out (§4.1), which is
// exactly why nothing here ever auto-dispatches without the chip row
// being visible first (except the guide's own single-unambiguous-target
// exception, see AUTO-RUN below).

const TARGETS = [
  { key: "clusters", label: "Clusters", icon: Layers, subTab: "clusters", keywords: ["cluster", "clusters", "group notes", "organize notes"] },
  { key: "facts", label: "Facts", icon: BookMarked, subTab: "facts", keywords: ["fact", "facts"] },
  { key: "suggested_notes", label: "Suggested notes", icon: Sparkles, subTab: "candidates", keywords: ["suggested note", "suggest notes", "scan for notes", "note suggestions", "note candidates"] },
  { key: "study_flashcards", label: "Flashcards", icon: GraduationCap, subTab: "study", keywords: ["flashcard", "flash card"] },
  { key: "study_quiz", label: "Quiz", icon: GraduationCap, subTab: "study", keywords: ["quiz"] },
  { key: "study_guide", label: "Study guide", icon: GraduationCap, subTab: "study", keywords: ["study guide"] },
  { key: "mindmap", label: "Mind map", icon: Network, subTab: "mindmap", keywords: ["mind map", "mindmap", "concept map"] },
  { key: "backlinks", label: "Backlinks", icon: GitBranch, subTab: "backlinks", keywords: ["backlink", "backlinks", "concept graph", "concept links"] },
];

const TARGETS_BY_KEY = Object.fromEntries(TARGETS.map((t) => [t.key, t]));

// Guide §4.2/§9.3: "the PDF I just uploaded" — a deictic reference
// resolves against the Sources list's most-recent item. Explicit source
// titles (min length, same false-positive guard as
// agents/backlink_detector.py's MIN_TITLE_LENGTH) are also matched.
const RECENT_SOURCE_PHRASES = ["just uploaded", "latest source", "most recent source", "the pdf i just", "i just added"];
const MIN_TITLE_MATCH_LENGTH = 4;

// Local, no-LLM parse of free text into {targetKeys, sourceNodeIds}.
// Order-preserving against TARGETS so chips render in a stable order
// regardless of the order words appear in the sentence.
function parseFreeText(text, nodes) {
  const lower = (text || "").toLowerCase();
  const targetKeys = TARGETS.filter((t) => t.keywords.some((kw) => lower.includes(kw))).map((t) => t.key);

  let sourceNodeIds = [];
  if (RECENT_SOURCE_PHRASES.some((p) => lower.includes(p)) && nodes.length > 0) {
    const mostRecent = [...nodes].sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""))[0];
    if (mostRecent) sourceNodeIds = [mostRecent.node_id];
  } else {
    sourceNodeIds = nodes
      .filter((n) => (n.title || "").trim().length >= MIN_TITLE_MATCH_LENGTH && lower.includes((n.title || "").toLowerCase()))
      .map((n) => n.node_id);
  }

  return { targetKeys, sourceNodeIds };
}

function TargetChip({ target, selected, onToggle }) {
  const Icon = target.icon;
  return (
    <button
      type="button"
      onClick={() => onToggle(target.key)}
      className={`flex items-center gap-1.5 text-xs rounded-full px-3 py-1.5 border transition-colors ${
        selected
          ? "bg-[var(--accent)] text-[var(--accent-text)] border-[var(--accent)] font-medium"
          : "border-[var(--neutral-700)] text-[var(--neutral-400)] hover:border-[var(--neutral-500)] hover:text-[var(--neutral-200)]"
      }`}
    >
      <Icon size={12} /> {target.label}
    </button>
  );
}

function BranchRow({ branch, onNavigate }) {
  const target = TARGETS_BY_KEY[branch.panel_key];
  const label = target?.label || branch.panel_key;
  const Icon = target?.icon || Sparkles;
  return (
    <div className="flex items-center justify-between gap-2 px-2.5 py-1.5 rounded-lg border border-[var(--neutral-800)]">
      <div className="flex items-center gap-2 min-w-0">
        <Icon size={13} className="shrink-0 text-[var(--neutral-500)]" />
        <span className="text-xs text-[var(--neutral-200)] truncate">{label}</span>
      </div>
      <div className="flex items-center gap-2 shrink-0">
        {branch.status === "running" && <Loader2 size={13} className="animate-spin text-[var(--neutral-500)]" />}
        {branch.status === "done" && branch.result?.status === "up_to_date" && (
          <span className="text-[10px] text-[var(--neutral-500)]">up to date</span>
        )}
        {branch.status === "done" && branch.result?.status !== "up_to_date" && (
          <Check size={13} className="text-green-400" />
        )}
        {branch.status === "error" && (
          <span className="flex items-center gap-1 text-[10px] text-red-400" title={branch.error}>
            <AlertCircle size={12} /> Error
          </span>
        )}
        {branch.status === "done" && target?.subTab && (
          <button
            type="button"
            onClick={() => onNavigate(target.subTab)}
            title={`Open ${label}`}
            className="text-[var(--neutral-500)] hover:text-[var(--cyber-cyan)]"
          >
            <ChevronRight size={13} />
          </button>
        )}
      </div>
    </div>
  );
}

export default function NotebooksGeneratePicker({ workspaceId, nodes, generateNotebooks, onComplete, onNavigateSubTab }) {
  const [open, setOpen] = useState(false);
  const [freeText, setFreeText] = useState("");
  const [selectedTargets, setSelectedTargets] = useState([]);
  const [scopeMode, setScopeMode] = useState("whole"); // "whole" | "sources"
  const [selectedSourceIds, setSelectedSourceIds] = useState([]);
  const [running, setRunning] = useState(false);
  const [branches, setBranches] = useState(null); // null until a run has started
  const [runError, setRunError] = useState(null);
  const popoverRef = useRef(null);

  useEffect(() => {
    if (!open) return;
    function onClickOutside(e) {
      if (popoverRef.current && !popoverRef.current.contains(e.target)) setOpen(false);
    }
    document.addEventListener("mousedown", onClickOutside);
    return () => document.removeEventListener("mousedown", onClickOutside);
  }, [open]);

  function resetForNewRun() {
    setBranches(null);
    setRunError(null);
  }

  function toggleTarget(key) {
    resetForNewRun();
    setSelectedTargets((prev) => (prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key]));
  }

  function toggleSource(nodeId) {
    resetForNewRun();
    setSelectedSourceIds((prev) => (prev.includes(nodeId) ? prev.filter((id) => id !== nodeId) : [...prev, nodeId]));
  }

  // Guide §4.1: free text pre-fills the SAME chip row a manual click
  // opens empty — it never dispatches by itself except the single-
  // unambiguous-target, no-scope-language case, which still shows the
  // chip briefly rather than sending silently.
  function handleFreeTextSubmit(e) {
    e.preventDefault();
    const { targetKeys, sourceNodeIds } = parseFreeText(freeText, nodes);
    if (targetKeys.length === 0) return; // nothing recognized — leave the chips as they are, no silent no-op run
    resetForNewRun();
    setSelectedTargets(targetKeys);
    if (sourceNodeIds.length > 0) {
      setScopeMode("sources");
      setSelectedSourceIds(sourceNodeIds);
    }
    const hasScopeLanguage = sourceNodeIds.length > 0;
    if (targetKeys.length === 1 && !hasScopeLanguage) {
      // AUTO-RUN — guide §4.1's one exception: "a single unambiguous
      // target with no scope language... pre-fill and auto-run, since
      // there's nothing to misparse."
      runGenerate(targetKeys, null);
    }
  }

  async function runGenerate(targetsOverride, scopeOverride) {
    const targets = targetsOverride || selectedTargets;
    if (targets.length === 0) return;
    const scope = scopeOverride !== undefined
      ? scopeOverride
      : (scopeMode === "sources" && selectedSourceIds.length > 0 ? { source_node_ids: selectedSourceIds } : null);

    setRunning(true);
    setRunError(null);
    setBranches(targets.map((key) => ({ panel_key: key, status: "running" })));
    try {
      const { branches: result } = await generateNotebooks(workspaceId, targets, scope);
      setBranches(result);
      onComplete?.();
    } catch (err) {
      setRunError(String(err.message || err));
      setBranches(null);
    } finally {
      setRunning(false);
    }
  }

  const canRun = selectedTargets.length > 0 && !running;

  return (
    <div className="relative" ref={popoverRef}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex items-center gap-1.5 text-xs rounded-lg px-3 py-1.5 border border-[var(--neutral-700)] text-[var(--neutral-200)] hover:border-[var(--cyber-cyan)] font-medium"
      >
        <Sparkles size={13} /> Generate
      </button>

      {open && (
        <div className="absolute right-0 mt-2 w-96 max-h-[70vh] overflow-y-auto rounded-lg border border-[var(--neutral-700)] bg-[var(--neutral-900)] shadow-xl z-30 p-3 space-y-3">
          <div className="flex items-center justify-between">
            <span className="text-xs font-medium text-[var(--neutral-300)]">Generate</span>
            <button onClick={() => setOpen(false)} className="text-[var(--neutral-500)] hover:text-[var(--neutral-300)]">
              <X size={14} />
            </button>
          </div>

          {/* Free-text pre-fill — guide §4.1: a second entry point into
              the same chip row below, not a separate flow. */}
          <form onSubmit={handleFreeTextSubmit} className="flex gap-1.5">
            <input
              value={freeText}
              onChange={(e) => setFreeText(e.target.value)}
              placeholder='e.g. "make flashcards and a mind map"'
              className="flex-1 min-w-0 bg-black/30 border border-[var(--neutral-800)] rounded px-2 py-1.5 text-xs outline-none focus:border-[var(--cyber-cyan)]"
            />
            <button
              type="submit"
              title="Parse into chips"
              className="shrink-0 text-xs text-[var(--neutral-400)] hover:text-[var(--cyber-cyan)] px-2"
            >
              Parse
            </button>
          </form>

          {/* Target chips — the picker's own manual entry point, and
              also where free-text parses land for review before Run. */}
          <div className="flex flex-wrap gap-1.5">
            {TARGETS.map((t) => (
              <TargetChip key={t.key} target={t} selected={selectedTargets.includes(t.key)} onToggle={toggleTarget} />
            ))}
          </div>

          {/* Scope — guide §4.2: blank/whole notebook, or explicit
              source picks. With §3's PDF fix, every source is now one
              whole-file node, so this is a clean multi-select of files. */}
          <div className="space-y-1.5">
            <div className="flex items-center gap-1 text-[10px] text-[var(--neutral-500)]">
              <button
                type="button"
                onClick={() => { resetForNewRun(); setScopeMode("whole"); }}
                className={`rounded px-1.5 py-0.5 ${scopeMode === "whole" ? "bg-[var(--neutral-800)] text-[var(--neutral-200)]" : "hover:text-[var(--neutral-300)]"}`}
              >
                Whole notebook
              </button>
              <button
                type="button"
                onClick={() => { resetForNewRun(); setScopeMode("sources"); }}
                className={`rounded px-1.5 py-0.5 ${scopeMode === "sources" ? "bg-[var(--neutral-800)] text-[var(--neutral-200)]" : "hover:text-[var(--neutral-300)]"}`}
              >
                Specific sources{selectedSourceIds.length > 0 ? ` (${selectedSourceIds.length})` : ""}
              </button>
            </div>
            {scopeMode === "sources" && (
              <div className="max-h-32 overflow-y-auto space-y-1 border border-[var(--neutral-800)] rounded-lg p-1.5">
                {nodes.length === 0 && <p className="text-[10px] text-[var(--neutral-600)] px-1">No sources yet.</p>}
                {nodes.map((n) => (
                  <label key={n.node_id} className="flex items-center gap-1.5 px-1 py-0.5 text-[11px] text-[var(--neutral-300)] cursor-pointer">
                    <input
                      type="checkbox"
                      checked={selectedSourceIds.includes(n.node_id)}
                      onChange={() => toggleSource(n.node_id)}
                      className="accent-[var(--accent)]"
                    />
                    <span className="truncate">{n.title || n.node_id}</span>
                  </label>
                ))}
              </div>
            )}
          </div>

          <button
            type="button"
            onClick={() => runGenerate()}
            disabled={!canRun}
            className="w-full flex items-center justify-center gap-1.5 bg-[var(--accent)] text-[var(--accent-text)] rounded-lg px-3 py-1.5 text-xs font-medium disabled:opacity-50"
          >
            {running ? <Loader2 size={13} className="animate-spin" /> : <Play size={13} />}
            Run {selectedTargets.length > 0 ? `(${selectedTargets.length})` : ""}
          </button>

          {runError && (
            <p className="flex items-center gap-1 text-[11px] text-red-400"><AlertCircle size={12} /> {runError}</p>
          )}

          {/* Per-branch status — guide §5's minimal first cut: each
              target gets its own Generating/Done/Error state rather
              than one shared result. The full multi-branch Working
              Panel graph (RoutingTraceGraph.jsx) is a separate,
              follow-on piece — this is just the picker's own inline
              feedback so Run isn't a black box in the meantime. */}
          {branches && branches.length > 0 && (
            <div className="space-y-1 pt-1 border-t border-[var(--neutral-800)]">
              {branches.map((b) => (
                <BranchRow
                  key={b.panel_key}
                  branch={b}
                  onNavigate={(subTab) => { onNavigateSubTab?.(subTab); setOpen(false); }}
                />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
