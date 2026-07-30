import {
  Sparkles, Layers, BookMarked, GraduationCap, Network, Mic, Video, ListChecks,
} from "lucide-react";

// Phase 1 step 1.7: same NEXT_PUBLIC_API_URL convention SessionContext.jsx
// uses for every other fetch() in the app — duplicated here rather than
// imported from there because SessionContext.jsx pulls in AuthContext/
// supabase/pusher, none of which this plain data module should depend
// on just to read one env var.
const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

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
// Phase 1 step 1.7: this array below is no longer the source of truth for
// label/subTab/description/scopeAllowed/endpoint/enabled — step 1.6's
// `GET /api/capabilities` (api/server.py's CAPABILITIES_MANIFEST) is.
// It stays as the *initial* values (and the sole source for `icon` and
// `keywords`, which the manifest deliberately omits — see api/server.py's
// step 1.6 comment) so every existing call site — NotebooksGeneratePicker's
// chip row, parseFreeText's keyword scan, WorkspaceChatPanel's
// TARGETS_BY_KEY — keeps working synchronously with zero shape change,
// per step 1.8's "zero visible/behavioral change" requirement. Below,
// syncCapabilitiesFromServer() fetches the manifest once and merges the
// server's values onto these entries *in place*, so every module that
// already did `import { TARGETS }` picks up the synced fields for free
// through the same array/object references — no call-site changes
// needed. A later step (past this plan's Phase 1) can migrate call
// sites to a proper `useCapabilities()` hook if sync-in-place stops
// being enough; for now it's the smallest change that makes
// `/api/capabilities` the single source of truth without an async
// rewrite of every consumer in one patch.
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

// Phase 1 step 1.7 (cont'd): keyed lookup used both by the merge below
// and re-exported for any call site that wants O(1) access instead of
// re-deriving its own `TARGETS_BY_KEY` (NotebooksGeneratePicker.jsx and
// WorkspaceChatPanel.jsx currently both build their own local copy of
// this same reduction — left as-is here rather than refactored in this
// step, to keep this patch to "one source of truth for the data," not
// also a call-site cleanup).
const TARGETS_BY_KEY = Object.fromEntries(TARGETS.map((t) => [t.key, t]));

// Fields the server manifest (api/server.py's CAPABILITIES_MANIFEST) owns.
// Deliberately excludes `icon` and `keywords` — see the step 1.6 comment
// in api/server.py for why those stay frontend-only.
const SERVER_OWNED_FIELDS = ["label", "subTab", "description", "scopeAllowed", "endpoint", "enabled"];

let syncPromise = null;

// Fetches GET /api/capabilities once and merges each entry's server-owned
// fields onto the matching TARGETS[] object *in place*, keyed by `key`.
// Mutating in place (rather than replacing `TARGETS` with a new array) is
// what lets this stay a one-line addition at every existing import site:
// NotebooksGeneratePicker.jsx's `export { TARGETS }` and
// WorkspaceChatPanel.jsx's `import { ..., TARGETS }` both hold a
// reference to this same array/these same objects, so they see the
// synced values without re-importing or subscribing to anything.
//
// No visible effect yet (step 1.8's smoke test): nothing in the current
// UI renders `description`/`scopeAllowed`/`endpoint`, and `label`/
// `subTab`/`enabled` only ever change if the server manifest disagrees
// with the local defaults above, which it doesn't as of this step —
// step 1.6's CAPABILITIES_MANIFEST was hand-written to match. This
// starts mattering once Phase 2 reads `description` to build the LLM's
// tool list.
//
// Deliberately silent on failure (offline, server not up yet, CORS
// during local dev, etc.) — this fetch augments already-correct local
// defaults, it doesn't replace them, so a failed sync should never be
// visible to the user or block rendering. Guarded by `typeof window`
// so this module can still be imported during Next.js's server-side
// render/build without attempting a network call there.
export function syncCapabilitiesFromServer() {
  if (typeof window === "undefined") return Promise.resolve();
  if (!syncPromise) {
    syncPromise = fetch(`${API_URL}/api/capabilities`)
      .then((res) => (res.ok ? res.json() : Promise.reject(new Error(`capabilities fetch ${res.status}`))))
      .then((data) => {
        for (const remote of data.capabilities || []) {
          const local = TARGETS_BY_KEY[remote.key];
          if (!local) continue;   // server knows a capability this build's TARGETS doesn't yet — ignore for now
          for (const field of SERVER_OWNED_FIELDS) {
            if (field in remote) local[field] = remote[field];
          }
        }
      })
      .catch((err) => {
        console.warn("notebookCapabilities: /api/capabilities sync failed, using local TARGETS defaults", err);
      });
  }
  return syncPromise;
}

// Kick off the sync as soon as this module is first imported (i.e. as
// soon as anything touches the Notebooks tab or chat panel), rather than
// requiring every consumer to remember to call this — same "import runs
// it" shape as e.g. pusherClient.js's client singleton.
syncCapabilitiesFromServer();
