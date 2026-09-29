// frontend/app/lib/workbench/elementRef.js — W6.5 (Build Workbench
// plan). The `element` half of a `kind: "element"` code-context ref:
// what a click in the live preview (inspectorRuntime.js's
// minime:select) knew about the DOM node, kept next to the code range
// it resolves to so W7.1 can hand the agent both ("tag/classes/computed
// styles + the element's exact code range").
//
// Pure functions, no imports (same family as fileTree.js / tabUtils.js
// / mmRange.js), so plain `node` can test them.
//
// Trust boundary: a minime:select payload is data from inside a
// sandboxed iframe running whatever the person's own page does — it
// carries this build's nonce, but a page can still post nonsense with
// it (PreviewPane.jsx's listener only proves the message came from OUR
// frame, not that its contents are well-formed). elementFromMessage()
// is where that nonsense stops: every field is type-checked and
// length-capped before anything downstream (a chip label, a prompt)
// sees it.

const MAX_CLASSES = 20;
const MAX_CLASS_LENGTH = 80;
const MAX_TEXT = 200;
const MAX_STYLE_VALUE = 120;
const STYLE_KEYS = ["color", "background", "fontSize", "padding", "margin", "display"];

function str(value, max) {
  return typeof value === "string" ? value.slice(0, max) : "";
}

/**
 * @param {any} data - a minime:select message body
 * @returns {{mm: string, element: {tag: string, classes: string[], textPreview: string, styles: Record<string,string>, dynamic: boolean, instanceCount: number}} | null}
 *   null when there is no usable `mm` — a select with nothing to
 *   resolve to a source range is not something W6.5 can act on
 */
export function elementFromMessage(data) {
  if (!data || typeof data.mm !== "string" || !data.mm) return null;
  const styles = {};
  for (const key of STYLE_KEYS) {
    const value = data.styles && str(data.styles[key], MAX_STYLE_VALUE);
    if (value) styles[key] = value;
  }
  return {
    mm: data.mm,
    element: {
      tag: str(data.tag, 40),
      classes: Array.isArray(data.classes)
        ? data.classes.filter((c) => typeof c === "string" && c).slice(0, MAX_CLASSES).map((c) => c.slice(0, MAX_CLASS_LENGTH))
        : [],
      textPreview: str(data.textPreview, MAX_TEXT),
      styles,
      dynamic: data.dynamic === true,
      instanceCount:
        typeof data.instanceCount === "number" && Number.isFinite(data.instanceCount) && data.instanceCount >= 1
          ? Math.floor(data.instanceCount)
          : 1,
    },
  };
}

/**
 * The chip's label: `button.btn-primary`. At most two classes — a
 * utility-class-heavy element (Tailwind) would otherwise turn a chip
 * into a paragraph; the full list is still on the ref for the model.
 *
 * @param {{tag?: string, classes?: string[]}} element
 * @returns {string}
 */
export function elementLabel(element) {
  const tag = (element && element.tag) || "element";
  const classes = (element && element.classes) || [];
  return tag + classes.slice(0, 2).map((c) => `.${c}`).join("");
}

/**
 * Plain-text description of the element for the chat message, placed
 * ahead of the fenced code block: the code says WHAT the source is,
 * this says what it RENDERED as (computed styles are something no
 * amount of reading the JSX/HTML tells you, and are exactly what a
 * "make this button red" request needs).
 *
 * @param {{tag?: string, classes?: string[], textPreview?: string, styles?: Record<string,string>, dynamic?: boolean, instanceCount?: number}} element
 * @returns {string}
 */
export function describeElement(element) {
  const el = element || {};
  const classAttr = el.classes && el.classes.length ? ` class="${el.classes.join(" ")}"` : "";
  let line = `Selected element in the live preview: <${el.tag || "element"}${classAttr}>`;
  if (el.textPreview) line += ` with text "${el.textPreview}"`;
  const notes = [];
  if (el.instanceCount > 1) notes.push(`this source element renders ${el.instanceCount} times`);
  if (el.dynamic) notes.push("the clicked node was created at runtime, so this is its nearest source element");
  if (notes.length) line += ` (${notes.join("; ")})`;
  const styleParts = Object.entries(el.styles || {}).map(([k, v]) => `${k}: ${v}`);
  if (styleParts.length) line += `\nComputed style: ${styleParts.join("; ")}`;
  return line;
}
