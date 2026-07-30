import {
  Sparkles, Layers, BookMarked, GraduationCap, Network, Mic, Video, ListChecks,
} from "lucide-react";

// Notebooks — Chat-First Refinement, Phase 1 step 1.1: promoted out of
// NotebooksGeneratePicker.jsx, same shape as before (key/label/icon/
// subTab/keywords). This is the single source of truth for every
// registered Notebooks generation target — NotebooksGeneratePicker.jsx
// and WorkspaceChatPanel.jsx both import from here now instead of each
// other. Later Phase 1 steps add description/scopeAllowed/endpoint to
// each entry; this step only moves the array, no shape change yet.
//
// Phase 1 step 1.2: added `description` to every entry — one plain
// sentence, written the way you'd explain the target to someone who's
// never seen the picker. This is the exact string Phase 2 hands the LLM
// as a tool description, so it's written for the model's benefit as much
// as any future human-facing help menu, not just as a label gloss.
//
// Phase 1 step 1.3: added `scopeAllowed` to every entry — "whole" |
// "sources" | "topic". All 7 pre-existing targets run over the whole
// notebook (the guide's own framing: "workflows are topic-scoped, not
// whole-notebook like the rest" — i.e. everything already in this table
// is the "rest"). This isn't a behavior change yet; nothing reads this
// field until Phase 2's tool-calling / scope-validation lands.
//
// Phase 1 step 1.4: added `endpoint` to every entry. All 7 pre-existing
// targets dispatch through the single existing route,
// `POST /api/workspaces/{ws_id}/notebooks/generate` (api/server.py's
// `NOTEBOOKS_GENERATE_TARGETS` table) — the `key` below is what
// distinguishes them in that route's request body, there's no per-target
// route today. Phase 5's podcast/video_overview/workflow entries will be
// the first ones with their own dedicated endpoint strings.
//
// Phase 1 step 1.5: stub entries for the three targets the guide adds in
// Phase 1 step 2 (podcast, video_overview) and step 8's workflow entry.
// `enabled: false` keeps them out of the picker and out of Phase 2's
// tool list until Phase 5 gives them real, workspace-scoped endpoints —
// podcast/video_overview today only exist as the separate, non-workspace-
// scoped `POST /api/notes/podcast/synthesize` and
// `POST /api/notes/video-overview` routes (see guide §0), which is why
// `endpoint` is `null` here rather than pointing at those. `workflow`
// already has a real per-topic call (`generateTopicWorkflow()` →
// `build_topic_workflow()`) but isn't registered as a capability yet.
export const TARGETS = [
  { key: "clusters", label: "Clusters", icon: Layers, subTab: "insights", keywords: ["cluster", "clusters", "group notes", "organize notes"], description: "Group the workspace's notes and sources into topic clusters, so related material is organized together instead of a flat list.", scopeAllowed: "whole", endpoint: "POST /api/workspaces/{ws_id}/notebooks/generate" },
  { key: "facts", label: "Facts", icon: BookMarked, subTab: "insights", keywords: ["fact", "facts"], description: "Pull out standalone factual statements from the sources and list them as discrete, citable facts.", scopeAllowed: "whole", endpoint: "POST /api/workspaces/{ws_id}/notebooks/generate" },
  { key: "suggested_notes", label: "Suggested notes", icon: Sparkles, subTab: "insights", keywords: ["suggested note", "suggest notes", "scan for notes", "note suggestions", "note candidates"], description: "Scan the sources for note-worthy passages and propose draft notes the user can accept or discard.", scopeAllowed: "whole", endpoint: "POST /api/workspaces/{ws_id}/notebooks/generate" },
  { key: "study_flashcards", label: "Flashcards", icon: GraduationCap, subTab: "study", keywords: ["flashcard", "flash card"], description: "Generate a set of question/answer flashcards for studying the selected scope.", scopeAllowed: "whole", endpoint: "POST /api/workspaces/{ws_id}/notebooks/generate" },
  { key: "study_quiz", label: "Quiz", icon: GraduationCap, subTab: "study", keywords: ["quiz"], description: "Generate a graded quiz covering the selected scope, which the user can take and submit for scoring.", scopeAllowed: "whole", endpoint: "POST /api/workspaces/{ws_id}/notebooks/generate" },
  { key: "study_guide", label: "Study guide", icon: GraduationCap, subTab: "study", keywords: ["study guide"], description: "Produce a structured written study guide summarizing and organizing the selected scope for review.", scopeAllowed: "whole", endpoint: "POST /api/workspaces/{ws_id}/notebooks/generate" },
  { key: "mindmap", label: "Mind map", icon: Network, subTab: "diagrams", keywords: ["mind map", "mindmap", "concept map"], description: "Build a visual mind map of the concepts in the selected scope and how they relate to each other.", scopeAllowed: "whole", endpoint: "POST /api/workspaces/{ws_id}/notebooks/generate" },
  { key: "podcast", label: "Podcast", icon: Mic, subTab: "insights", keywords: ["podcast"], description: "Generate a two-host audio podcast episode discussing the selected scope.", scopeAllowed: "whole", endpoint: null, enabled: false },
  { key: "video_overview", label: "Video overview", icon: Video, subTab: "insights", keywords: ["video overview", "video summary"], description: "Generate a narrated video overview summarizing the selected scope.", scopeAllowed: "whole", endpoint: null, enabled: false },
  { key: "workflow", label: "Workflow", icon: ListChecks, subTab: "diagrams", keywords: ["workflow", "study plan", "learning path"], description: "Build a step-by-step study workflow for a single topic.", scopeAllowed: "topic", endpoint: null, enabled: false },
  // REMOVED — chat audit: "Backlinks" used to trigger agents/concept_linker.py's
  // link_concepts() by hand here, but the Library tab's BacklinksView no
  // longer even renders that graph (it shows eo/secondary_data.py's
  // auto-built topic tree instead — see NotebooksTab.jsx's own comment on
  // that view). Real topic-to-topic connection detection
  // (agents/backlink_detector.py's run_after_source_manager()) already
  // runs automatically on every upload, no button needed — this entry
  // was a dead end pointing at a graph nothing displays. Per explicit
  // request: no manual "generate backlinks" affordance anywhere.
  // REMOVED (step 3/5) — "Workflows" used to be a batch Generate target
  // hitting agents/workflow_suggester.py's suggest_workflows() over the
  // whole notebook/scope. That's now dead server-side (NOTEBOOKS_GENERATE_TARGETS
  // no longer includes "workflows" — api/server.py step 3) in favor of
  // per-topic generation: clicking a MindMapView node now calls
  // build_topic_workflow() through SessionContext's generateTopicWorkflow()
  // (step 4) and lands in WorkflowsView as a per-topic result, not a
  // picker chip. See NotebooksTab.jsx's DiagramsView (step 8) for the
  // new wiring.
];
