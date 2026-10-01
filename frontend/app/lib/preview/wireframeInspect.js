// frontend/app/lib/preview/wireframeInspect.js — W8.6 (Build Workbench
// plan). What WireframePreview.jsx needs to give a wireframe the same
// point-and-select inspector the workbench preview has (W6.3 stamps
// `data-mm`, W6.4 injects the runtime and reports clicks) — minus the
// parts that only make sense for a project file.
//
// The difference that shapes everything here: a wireframe is NOT a
// workspace file. It lives in the Wireframes panel's own saved text, so a
// click has no editor tab to jump to and no code chip to add. What a
// selection is FOR is the "Send edit" bar: "make THIS button bigger" is
// sent with the element's description and its exact source, so
// wireframe_sketcher edits the right thing instead of guessing from a
// sentence. Everything below serves that.
//
//   buildWireframeFrame()       the preview copy: instrument -> inject.
//   selectionFromMessage()      a minime:select -> {element, snippet}.
//   buildWireframeEditInstruction()  the edit message, scoped or not.
//
// Source mapping uses a VIRTUAL path (WIREFRAME_MM_PATH) in `data-mm`:
// the contract is `path:startLine:startCol:endLine:endCol` (see
// instrument.js's header), and the path only has to say which "file" the
// range is in — here, the one wireframe. A select carrying any other path
// is ignored, so a page can't point the edit bar at text it didn't come
// from. Instrumented HTML exists ONLY in the srcDoc handed to the iframe
// (plan §7.3): the text saved, sent to chat, or filed as index.html is
// always the original.
//
// The two collaborators that need parse5 (instrument / injectRuntime) are
// passed in rather than imported, so the orchestration is testable under
// plain `node` without a bundler; the component supplies the real ones.
import { parseMm, rangeFromMm } from "./mmRange";
import { describeElement, elementFromMessage } from "../workbench/elementRef";

/** The virtual path every wireframe `data-mm` carries. */
export const WIREFRAME_MM_PATH = "wireframe.html";

/** A selected element's source is quoted in the chat message up to this length. */
export const MAX_SNIPPET_CHARS = 1500;

// inspectorRuntime.js's install guard — present in the output only if the
// runtime actually got injected (injectInspectorRuntime() returns its
// input untouched, never throws, when it can't).
const RUNTIME_MARKER = "__mmInspectorInstalled__";

/**
 * The document the preview iframe loads, and whether it can be inspected.
 * Never throws and never makes the preview WORSE: when there is nothing to
 * instrument (or either step fails) the wireframe is returned byte for
 * byte as it was before W8.6, `inspectable: false`, with a `note` only
 * when something that looked instrumentable failed.
 *
 * @param {object} args
 * @param {string} args.html - the wireframe, as shown (original text)
 * @param {string} args.nonce - this build's nonce; the runtime echoes it in every message
 * @param {(code: string, path: string) => Promise<{code: string, instrumented: boolean, note: string|null}>} args.instrument - instrument.js's instrumentSource
 * @param {(html: string, nonce: string) => string} args.injectRuntime - inspectorRuntime.js's injectInspectorRuntime
 * @returns {Promise<{srcDoc: string, inspectable: boolean, note: string|null}>}
 */
export async function buildWireframeFrame({ html, nonce, instrument, injectRuntime }) {
  const source = typeof html === "string" ? html : "";
  if (!source.trim()) return { srcDoc: "", inspectable: false, note: null };

  let result;
  try {
    result = await instrument(source, WIREFRAME_MM_PATH);
  } catch {
    return { srcDoc: source, inspectable: false, note: "Element inspector unavailable for this wireframe." };
  }
  if (!result || result.instrumented !== true || typeof result.code !== "string") {
    return { srcDoc: source, inspectable: false, note: (result && result.note) || null };
  }

  let srcDoc;
  try {
    srcDoc = injectRuntime(result.code, nonce);
  } catch {
    return { srcDoc: source, inspectable: false, note: "Element inspector unavailable for this wireframe." };
  }
  if (typeof srcDoc !== "string" || !srcDoc.includes(RUNTIME_MARKER)) {
    return { srcDoc: source, inspectable: false, note: "Element inspector unavailable for this wireframe." };
  }
  return { srcDoc, inspectable: true, note: null };
}

/**
 * A `minime:select` message from the frame, as the edit bar's selection.
 *
 * `sourceHtml` MUST be the exact text the frame was built from (the
 * component keeps it alongside the nonce), not whatever the textarea says
 * now: the mm range is a position in THAT text, and a wireframe edited
 * since would map the click onto the wrong characters.
 *
 * @param {any} data - the message body (untrusted: it comes from inside a sandboxed page)
 * @param {string} sourceHtml
 * @returns {{mm: string, element: object, snippet: string, snippetTruncated: boolean, line: number} | null}
 *   null when the message has no usable range, or the range isn't in the wireframe
 */
export function selectionFromMessage(data, sourceHtml) {
  const picked = elementFromMessage(data); // type-checks and caps every field
  if (!picked) return null;
  const parsed = parseMm(picked.mm);
  if (!parsed || parsed.path !== WIREFRAME_MM_PATH) return null;
  const { snippet } = rangeFromMm(typeof sourceHtml === "string" ? sourceHtml : "", parsed);
  const truncated = snippet.length > MAX_SNIPPET_CHARS;
  return {
    mm: picked.mm,
    element: picked.element,
    snippet: truncated ? snippet.slice(0, MAX_SNIPPET_CHARS) : snippet,
    snippetTruncated: truncated,
    line: parsed.startLine,
  };
}

// A fence one backtick longer than the longest run inside `text`, so a
// snippet that itself contains ``` can't close the block early.
function fenceFor(text) {
  let longest = 0;
  const runs = text.match(/`+/g) || [];
  for (const run of runs) longest = Math.max(longest, run.length);
  return "`".repeat(Math.max(3, longest + 1));
}

/**
 * The message "Send edit" puts into the chat. Unscoped it is exactly what
 * WireframePreview sent before W8.6 (`For the "X" wireframe: …`); with a
 * selected element it adds what that element rendered as and its source,
 * and says to leave the rest alone.
 *
 * @param {object} args
 * @param {string} [args.screenLabel]
 * @param {string} args.instruction
 * @param {ReturnType<typeof selectionFromMessage>} [args.selection]
 * @returns {string}
 */
export function buildWireframeEditInstruction({ screenLabel, instruction, selection }) {
  const text = (instruction || "").trim();
  const head = screenLabel ? `For the "${screenLabel}" wireframe: ${text}` : text;
  if (!selection) return head;

  const lines = [
    head,
    "",
    "Apply this to the selected element only, and leave the rest of the wireframe as it is.",
    describeElement(selection.element),
  ];
  if (selection.snippet) {
    const fence = fenceFor(selection.snippet);
    lines.push(
      `Its current HTML${selection.snippetTruncated ? " (truncated)" : ""}, from line ${selection.line}:`,
      `${fence}html`,
      selection.snippet,
      fence
    );
  }
  return lines.join("\n");
}
