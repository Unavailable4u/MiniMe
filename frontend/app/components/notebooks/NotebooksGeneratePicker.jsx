"use client";
import { useState } from "react";
import { AlertCircle } from "lucide-react";
import { useWorkspaceDock } from "../../context/WorkspaceDockContext";
import { TARGETS } from "../../lib/notebookCapabilities";    // NEW — Phase 1 step 1.1: promoted out of this file
import BranchRow from "./BranchRow";   // NEW — Phase 2 step 2.10: promoted out of this file, see BranchRow.jsx

// CHANGED — Notebooks Chat-First refinement, Phase 2 step 2.9. This file
// used to render the picker itself: a chip-multiselect popover, a
// free-text field that parsed into the same chips, a scope selector, and
// a manual "Generate" button (see the guide's original §4.1 note, kept
// below for history). Chat is now the ONLY entry point that dispatches a
// generation (steps 2.5-2.8's real tool-calling classification, plus the
// 2.6 keyword short-circuit) — 2.5-2.8 having proven stable per the
// plan's own step 2.8, that popover/chips/button trigger UI is now dead
// weight in NotebooksTab.jsx and this step removes it.
//
// What stays, per the plan's step 2.9 note ("keep runGenerate/branch-
// status logic"):
//   1. `parseFreeText`/`TARGETS` — still re-exported below for
//      WorkspaceChatPanel.jsx's `import { parseFreeText, TARGETS } from
//      "./notebooks/NotebooksGeneratePicker"` (that import site's cleanup
//      is still deferred, same as before this step — unrelated to the UI
//      removal here).
//   2. `runGenerate` — kept as the one remaining callable dispatch
//      function, now taking its targets/scope as explicit arguments
//      rather than falling back to the popover's local chip-selection
//      state (that state no longer exists). Nothing in this file calls
//      it anymore now that the "Run" button is gone; it's kept as the
//      still-correct entry point for the day some other manual affordance
//      (a "Regenerate" button, say) needs one.
//   3. The branch-status display — rather than a popover-gated readout of
//      THIS component's own local run, it now renders unconditionally,
//      reading straight off `dock.state.notebooksGenerateRun` — the SAME
//      key WorkspaceChatPanel.jsx's runGenerateTarget() writes to for
//      every chat-triggered run (see that function's step-2.10 comment).
//      That means NotebooksTab.jsx keeps a live "what just generated"
//      readout in its header even with the trigger UI gone, without this
//      component needing to know or care that chat is what's driving it.
//
// ORIGINAL NOTE (Notebooks integration guide §4.1, kept for context):
// "picker and free-text aren't really two separate systems — free text
// is just an alternate way to pre-fill the same picker UI." That picker
// popover is what step 2.9 removes; the free-text parser it fed
// (parseFreeText below) predates it and outlives it, reused by
// WorkspaceChatPanel's own tryHandleGenerateIntent().

// Phase 1 step 1.1: this table used to live here (see the now-moved
// history of REMOVED "Backlinks"/"Workflows" entries in
// frontend/app/lib/notebookCapabilities.js). It's re-exported from this
// file so WorkspaceChatPanel's existing `import { parseFreeText, TARGETS }
// from "./notebooks/NotebooksGeneratePicker"` keeps working unchanged —
// that import site gets cleaned up in a later step, not this one.
export { TARGETS };

const TARGETS_BY_KEY = Object.fromEntries(TARGETS.map((t) => [t.key, t]));

// Guide §4.2/§9.3: "the PDF I just uploaded" — a deictic reference
// resolves against the Sources list's most-recent item. Explicit source
// titles (min length, same false-positive guard as
// agents/backlink_detector.py's MIN_TITLE_LENGTH) are also matched.
const RECENT_SOURCE_PHRASES = ["just uploaded", "latest source", "most recent source", "the pdf i just", "i just added"];
const MIN_TITLE_MATCH_LENGTH = 4;

// Local, no-LLM parse of free text into {targetKeys, sourceNodeIds}.
// Order-preserving against TARGETS so chips render in a stable order
// regardless of the order words appear in the sentence. UNCHANGED by
// step 2.9 — still used by WorkspaceChatPanel.jsx's tryHandleGenerateIntent().
export function parseFreeText(text, nodes) {
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

// CHANGED — step 2.9: `nodes`/`onComplete` (only ever needed by the now-
// removed scope-source checklist and its post-run refresh) are dropped.
// `generateNotebooks` stays, since runGenerate below still needs it.
export default function NotebooksGeneratePicker({ workspaceId, generateNotebooks, onNavigateSubTab }) {
  const [runError, setRunError] = useState(null);

  // NEW — guide §5: same dock key WorkingPanel.jsx resolves to for this
  // workspace (both key off `ws:${workspaceId}`, see
  // WorkspaceDockContext.jsx's normalizeDockKey) — writing
  // notebooksGenerateRun here is how the Working Panel's multi-branch
  // graph learns about a run this component (or, since step 2.9, chat's
  // own runGenerateTarget) kicked off, with no other plumbing between
  // the two components.
  const dock = useWorkspaceDock(workspaceId, null);

  // NEW — guide §5: attach each target's human label and subTab before
  // mirroring into the dock, since RoutingTraceGraph's branch nodes
  // display `label` (falling back to the raw panel_key) and
  // WorkingPanel.jsx's click handler needs `subTab` to navigate — the
  // dock has no other access to the TARGETS table above.
  function withLabels(list) {
    return list.map((b) => ({ ...b, label: TARGETS_BY_KEY[b.panel_key]?.label, subTab: TARGETS_BY_KEY[b.panel_key]?.subTab }));
  }

  // CHANGED — step 2.9: takes targets/scope explicitly now instead of
  // falling back to the popover's local selectedTargets/scopeMode state
  // (removed this step, see file header). Currently uncalled from
  // anywhere in this file — kept as the still-correct dispatch entry
  // point, see file header point 2.
  async function runGenerate(targets, scope) {
    if (!targets || targets.length === 0) return;
    setRunError(null);
    const runningBranches = targets.map((key) => ({ panel_key: key, status: "running" }));
    dock.setDockState({ notebooksGenerateRun: { targets, branches: withLabels(runningBranches) } });
    try {
      const { branches: result } = await generateNotebooks(workspaceId, targets, scope);
      dock.setDockState({ notebooksGenerateRun: { targets, branches: withLabels(result) } });
    } catch (err) {
      setRunError(String(err.message || err));
      // The whole request failed (e.g. network error) before any
      // per-target result came back -- nothing branch-shaped to show on
      // the graph, so clear rather than leave a stale "running" run
      // sitting there forever.
      dock.setDockState({ notebooksGenerateRun: null });
    }
  }

  // CHANGED — step 2.9: the branch-status row now reads straight off the
  // dock's own notebooksGenerateRun (populated by WorkspaceChatPanel's
  // runGenerateTarget on every chat-triggered run) instead of this
  // component's own local `branches` state — there's no popover-gated
  // run originating here anymore for local state to track.
  const branches = dock.state?.notebooksGenerateRun?.branches;

  if (!runError && !(branches && branches.length > 0)) return null;

  return (
    <div className="flex items-center gap-2">
      {runError && (
        <p className="flex items-center gap-1 text-[11px] text-red-400"><AlertCircle size={12} /> {runError}</p>
      )}
      {/* Per-branch status — guide §5's minimal first cut: each target
          gets its own Generating/Done/Error state rather than one shared
          result. The full multi-branch Working Panel graph
          (RoutingTraceGraph.jsx) is a separate, follow-on piece — this
          is just a lightweight inline readout so a chat-triggered run
          isn't only visible in the chat thread. */}
      {branches && branches.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {branches.map((b) => (
            <BranchRow key={b.panel_key} branch={b} onNavigate={(subTab) => onNavigateSubTab?.(subTab)} />
          ))}
        </div>
      )}
    </div>
  );
}
