// frontend/app/lib/workbench/proposalRefs.js — W7.1b (Build Workbench
// plan, "Element chip → agent context", frontend half).
//
// What a code-context ref looks like on the wire to
// POST .../code/proposals. This used to be a private function inside
// codeProposals.js; it moved here because it is the one place that
// decides WHAT THE SERVER GETS, and W7.1 needs that decision tested:
// codeProposals.js imports the session (authHeaders) and Pusher, so
// plain `node` can't load it, while this file has no imports (same
// dependency-free family as fileTree.js / elementRef.js).
//
// The bug this fixes: W6.5 put the clicked element's tag / classes /
// text / computed styles on `ref.element`, and W7.1a's backend reads
// exactly that (backend/eo/code_element_context.py's
// sanitize_element) — but the old trim listed seven fields by name and
// `element` wasn't one of them, so the agent never saw it.
//
// Trust: `element` was already type-checked and length-capped by
// elementRef.js's elementFromMessage() when it left the preview
// iframe, and the server re-validates it (it is page data, so it is
// never trusted on the way in). The copy below is a second, shape-only
// filter so a ref object carrying extra client-side state can't leak
// into a request body.

// The computed-style keys the inspector reports (elementRef.js's
// STYLE_KEYS, and backend STYLE_KEYS — keep the three in step).
const STYLE_KEYS = ["color", "background", "fontSize", "padding", "margin", "display"];

function plainElement(el) {
  if (!el || typeof el !== "object" || Array.isArray(el)) return null;
  const styles = {};
  if (el.styles && typeof el.styles === "object") {
    for (const key of STYLE_KEYS) {
      if (typeof el.styles[key] === "string" && el.styles[key]) styles[key] = el.styles[key];
    }
  }
  return {
    tag: typeof el.tag === "string" ? el.tag : "",
    classes: Array.isArray(el.classes) ? el.classes.filter((c) => typeof c === "string" && c) : [],
    textPreview: typeof el.textPreview === "string" ? el.textPreview : "",
    styles,
    dynamic: el.dynamic === true,
    instanceCount: Number.isFinite(el.instanceCount) && el.instanceCount >= 1 ? Math.floor(el.instanceCount) : 1,
  };
}

/**
 * The ref shape eo/code_proposals.py's `refs` column stores — trimmed
 * from codeContext.js's full in-memory ref (which also carries
 * CodeMirror `from`/`to` offsets and a client-only `truncated` flag)
 * to what the server reads or shows. `element` rides along for
 * `kind: "element"` refs only, and only when it has the expected
 * shape; every other kind keeps exactly the fields it always had.
 *
 * @param {object} ref - a codeContext.js ref
 * @returns {{id: string, kind: string, path: string, fromLine: number|null, toLine: number|null, snippet: string|null, hash: string|null, provider: string|null, element?: object}}
 */
export function toProposalRef(ref) {
  const out = {
    id: ref.id,
    kind: ref.kind,
    path: ref.path,
    fromLine: ref.fromLine ?? null,
    toLine: ref.toLine ?? null,
    snippet: ref.snippet ?? null,
    hash: ref.hash ?? null,
    provider: ref.provider ?? null,
  };
  if (ref.kind === "element") {
    const element = plainElement(ref.element);
    if (element) out.element = element;
  }
  return out;
}
