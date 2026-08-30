import {
  Sparkles, Layers, BookMarked, GraduationCap, Network, Mic, Video, ListChecks, Drama, PenLine,
  Presentation,
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
  // CHANGED — Phase 2 step 2.4 revisit (Phase 5 step 5.8 finding): the
  // keyword arrays below for suggested_notes/study_quiz/study_guide/
  // mindmap got a few phrases added, each one lifted directly from a
  // TEST_CASES phrasing in scripts/test_capability_coverage.py that
  // misfired to no-tool-call 3/3 in the 5.8 run. tryHandleGenerateIntent()
  // (WorkspaceChatPanel.jsx) checks this synchronous keyword match
  // BEFORE the LLM classifier ever runs, so these catch the exact
  // failing phrasings for free, with zero added latency -- the paired
  // description/prompt fixes (api/server.py's CAPABILITIES_MANIFEST,
  // utils/llm_client.py's CLASSIFY_INTENT_SYSTEM_PROMPT) are what cover
  // rephrasings of the same intent this fast path doesn't happen to
  // match verbatim. Kept deliberately as specific multi-word phrases
  // (not single generic words like "summary" or "test") to avoid
  // false-positiving on unrelated messages that happen to contain one
  // of those words -- same "specific phrase, not a bare keyword"
  // pattern the pre-existing entries below already use.
  // CHANGED — 2026-08-01 gap fix: description corrected to match actual
  // behavior (chat-transcript decision scan, not a source scan) -- see
  // api/server.py's CAPABILITIES_MANIFEST comment on this same entry for
  // the full finding. "topic_notes" right below is the real source-scan
  // tool this description used to (wrongly) describe.
  { key: "suggested_notes", label: "Suggested notes", icon: Sparkles, subTab: "insights", keywords: ["suggested note", "suggest notes", "scan for notes", "note suggestions", "note candidates", "worth taking notes", "anything worth noting", "save that as a note"], description: "Scan the recent chat conversation in this notebook for a decision, insight, or action item worth saving as a note, and propose a draft the user can accept or discard. This does NOT read the sources/topics themselves -- it only looks at what's been said in chat. Use this for requests like 'was there anything worth noting in our conversation' or 'save that as a note' -- NOT for 'write a note about <topic>' or 'summarize this topic as a note' (see the topic_notes tool for that), pulling out standalone facts (see the facts tool), or grouping sources by topic (see the clusters tool).", scopeAllowed: "whole", endpoint: "POST /api/workspaces/{ws_id}/notebooks/generate" },
  // NEW — 2026-08-01 gap fix: source-grounded, single-topic note
  // generator. scopeAllowed: "topic" -- same category as "workflow"
  // below, not "whole" like every pre-existing Generate target.
  { key: "topic_notes", label: "Notes on this topic", icon: PenLine, subTab: "insights", keywords: ["write a note on", "note on this topic", "summarize this topic as a note", "notes on"], description: "Write one draft note summarizing a SPECIFIC topic's actual source material, for the user to accept or discard. Requires a single topic in scope (e.g. after clicking a Mind Map node, or naming a topic by title) -- reads that topic's real source excerpts, not the chat conversation. Use this for requests like 'write a note on <topic>', 'summarize this topic as a note', or 'give me notes on <topic>'. NOT for a whole-notebook scan of the chat for things worth noting (see the suggested_notes tool for that).", scopeAllowed: "topic", endpoint: "POST /api/workspaces/{ws_id}/notebooks/generate" },
  { key: "study_flashcards", label: "Flashcards", icon: GraduationCap, subTab: "study", keywords: ["flashcard", "flash card"], description: "Generate a set of question/answer flashcards for studying the selected scope.", scopeAllowed: "whole", endpoint: "POST /api/workspaces/{ws_id}/notebooks/generate" },
  { key: "study_quiz", label: "Quiz", icon: GraduationCap, subTab: "study", keywords: ["quiz", "quiz me", "test me on this", "test my understanding"], description: "Generate a graded quiz covering the selected scope, which the user can take and submit for scoring. Use this whenever the user wants to be quizzed or tested on the material -- e.g. 'quiz me', 'test my understanding', 'test me on this' -- even if they don't use the word 'quiz'.", scopeAllowed: "whole", endpoint: "POST /api/workspaces/{ws_id}/notebooks/generate" },
  { key: "study_guide", label: "Study guide", icon: GraduationCap, subTab: "study", keywords: ["study guide", "summary i can study", "written summary to study"], description: "Produce a structured written study guide summarizing and organizing the selected scope for review. Use this for requests for a prose summary or write-up to study from -- e.g. 'give me a summary I can study from', 'write me a summary', 'summarize this for review' -- as opposed to a visual mind map (see the mindmap tool) or a list of standalone facts (see the facts tool). Do NOT use this for 'a step-by-step study workflow' or 'a study plan' requests -- those ask for an ordered sequence of steps, not a written summary, and no tool for that exists yet, so don't call anything for them.", scopeAllowed: "whole", endpoint: "POST /api/workspaces/{ws_id}/notebooks/generate" },
  { key: "mindmap", label: "Mind map", icon: Network, subTab: "diagrams", keywords: ["mind map", "mindmap", "concept map", "map out the connections", "how these relate", "how these topics connect"], description: "Build a visual mind map of the concepts in the selected scope and how they relate to each other. Use this for requests to see or map out how topics/concepts connect or relate -- e.g. 'map out the connections between these topics', 'show me how these relate' -- as opposed to grouping sources into topic buckets (see the clusters tool).", scopeAllowed: "whole", endpoint: "POST /api/workspaces/{ws_id}/notebooks/generate" },
  // CHANGED — Phase 5 step 5.7: endpoint/enabled flipped from the Phase
  // 1.5 stub (endpoint: null, enabled: false) now that api/server.py's
  // CAPABILITIES_MANIFEST flips the same way in this step's companion
  // change. These are just the *initial* local defaults per this file's
  // step 1.7 comment above — syncCapabilitiesFromServer() overwrites
  // `endpoint`/`enabled` (and every other SERVER_OWNED_FIELDS entry)
  // with whatever GET /api/capabilities actually returns the moment it
  // resolves, so keeping these two literally in sync with the server
  // isn't load-bearing — but leaving them stale here (still `false`)
  // would mean anything that reads TARGETS before that fetch resolves
  // (e.g. first paint) sees the old, wrong disabled state for a beat
  // longer than it needs to.
  { key: "podcast", label: "Podcast", icon: Mic, subTab: "insights", keywords: ["podcast"], description: "Generate a two-host audio podcast episode discussing the selected scope.", scopeAllowed: "whole", endpoint: "POST /api/workspaces/{ws_id}/notebooks/podcast", enabled: true },
  // NEW — Video Overview reuse patch, step 1 (backend) / Chat wiring
  // patch, step 4 (this entry): slide_deck_planner's output now has a
  // standalone Generate target of its own server-side
  // (_generate_slide_deck(), panel_key "slide_deck") -- this entry is
  // what makes it reachable from the picker chip row AND from chat's
  // tool-call dispatch. Without this, TARGETS_BY_KEY[key] in
  // WorkspaceChatPanel.jsx's tryHandleClassifiedToolCall() would come
  // back undefined for "slide_deck" even after the backend started
  // returning it from GET /api/capabilities -- syncCapabilitiesFromServer()
  // below only merges server fields onto an ALREADY-PRESENT local entry
  // (`if (!local) continue`), it never adds a brand-new key on its own.
  // scopeAllowed/endpoint/description here are just the initial local
  // defaults per this file's step 1.7 comment above; the server sync
  // overwrites them with CAPABILITIES_MANIFEST's real values (currently
  // "sources", the shared .../notebooks/generate route, and a
  // pasted-slide-text-aware description) the moment that fetch resolves.
  { key: "slide_deck", label: "Presentation", icon: Presentation, subTab: "insights", keywords: ["presentation", "slide deck", "slides", "make me slides", "build a deck"], description: "Generate a slide deck outline summarizing the selected scope, with no narration or video attached.", scopeAllowed: "whole", endpoint: "POST /api/workspaces/{ws_id}/notebooks/generate", enabled: true },
  // CHANGED — Phase 5 step 5.8 finding: "Give me a video walkthrough of
  // this material." misfired to no-tool-call 3/3 -- "walkthrough"/
  // "explainer" added as keyword + description synonyms, same fix
  // pattern as the block above.
  { key: "video_overview", label: "Video overview", icon: Video, subTab: "insights", keywords: ["video overview", "video summary", "video walkthrough", "explainer video"], description: "Generate a narrated video overview summarizing the selected scope -- a short explainer/walkthrough video. Use this for requests like 'video overview', 'video summary', 'explainer video', or 'video walkthrough'.", scopeAllowed: "whole", endpoint: "POST /api/workspaces/{ws_id}/notebooks/video_overview", enabled: true },
  // NEW — Phase 5 step 5.11: no Phase 1.5 stub for this one (rehearsal
  // didn't exist as a concept until step 5.9), so this is a brand-new
  // entry rather than a stub flip like podcast/video_overview above.
  // `endpoint` points at the shared .../notebooks/generate dispatch
  // route (not a dedicated .../notebooks/presentation_rehearsal route --
  // this plan never asked for one), matching how clusters/facts/etc.
  // reach NOTEBOOKS_GENERATE_TARGETS in api/server.py. As with every
  // other server-owned field here, this is just the initial local
  // default -- syncCapabilitiesFromServer() overwrites it with whatever
  // CAPABILITIES_MANIFEST's matching entry actually says the moment the
  // fetch resolves.
  { key: "presentation_rehearsal", label: "Presentation rehearsal", icon: Drama, subTab: "insights", keywords: ["rehearsal", "rehearse", "mock q&a", "mock interview", "practice defending", "thesis defense", "presentation practice"], description: "Generate an interactive audio rehearsal for defending or presenting the selected scope -- a mock Q&A or practice run, not a straight recap. Supports a 'judge' mode (a skeptical panelist grills you), 'two_host' mode (a friendly co-presenter walk-through), and 'devils_advocate' mode (a debate partner pushes back), each at 'novice' or 'expert' difficulty. Use this for requests like 'help me rehearse my presentation', 'quiz me like a thesis defense', 'practice defending this', or 'mock Q&A on this material'.", scopeAllowed: "whole", endpoint: "POST /api/workspaces/{ws_id}/notebooks/generate", enabled: true },
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
