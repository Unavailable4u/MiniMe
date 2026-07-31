// Notebooks — Chat-First Refinement, Phase 3 step 3.1: the hardcoded
// post-generation "would you like me to also generate..." affinity map.
//
// This step ONLY defines the map — nothing reads it yet. Step 3.2 is what
// wires WorkspaceDockContext.jsx's generation_done handling (see its
// `generationNotifications` reducer branch) to look a finished panel_key
// up in here and have WorkspaceChatPanel.jsx append a one-tap suggestion
// message. Keeping the data and the wiring as separate steps/patches
// mirrors how Phase 1 separated "define the manifest" from "make anything
// read it."
//
// Keys/values are Phase 1 capability keys (frontend/app/lib/
// notebookCapabilities.js's TARGETS[].key — the same strings
// api/server.py's NOTEBOOKS_GENERATE_TARGETS and CAPABILITIES_MANIFEST
// use), not display labels, so step 3.2 can dispatch the suggested
// follow-up the same way Phase 2's chat-triggered path already dispatches
// any other capability — no new lookup table to keep in sync.
//
// Pairings, straight from the guide (§ Phase 3 step 1):
//   flashcards -> quiz          (study_flashcards -> study_quiz)
//   quiz -> study guide         (study_quiz -> study_guide)
//   mindmap -> workflow         (mindmap -> workflow)
//   clusters -> suggested_notes (clusters -> suggested_notes)
//
// Deliberately a plain one-to-one object, not a list of many-to-many
// rules: the guide's own framing is "one suggestion, dismissible, never
// repeated for the same pairing in a session" (see the plan's open
// decisions section) — one hardcoded next-step per completed target is
// enough for that, and keeps step 3.2's lookup a single property access
// (`NOTEBOOK_AFFINITIES[panelKey]`) rather than a filter/sort over
// candidates.
//
// `workflow` is topic-scoped (scopeAllowed: "topic" in
// notebookCapabilities.js) while mindmap is whole-notebook, so step 3.2
// will need to carry the topic id through to the suggested action's
// re-send — noted here, not solved here.
export const NOTEBOOK_AFFINITIES = {
  study_flashcards: "study_quiz",
  study_quiz: "study_guide",
  mindmap: "workflow",
  clusters: "suggested_notes",
};

// Small accessor, not a new capability lookup: returns the paired
// capability's key for a just-finished panel_key, or null if that target
// has no defined follow-up. Kept here (rather than inlined at every future
// call site) so step 3.2's WorkspaceDockContext.jsx change and any later
// consumer share one place that knows "no entry" and "explicitly no
// follow-up" both mean the same thing — return null, suggest nothing.
export function getSuggestedFollowUp(panelKey) {
  return NOTEBOOK_AFFINITIES[panelKey] ?? null;
}
