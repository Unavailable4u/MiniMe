// frontend/app/lib/preview/inspectorRuntime.js — W6.4 (Build Workbench
// plan). "Inspector runtime + message protocol": the injected script
// that turns a live preview into something you can point at and select
// an ELEMENT in, resolved back to the exact source range W6.3's
// instrumentSource() stamped onto it (the `data-mm="path:l:c:l:c"`
// contract — see instrument.js's own header, which this module never
// re-derives, only reads).
//
// Same two-half shape as consoleBridge.js (that file's own header
// explains why): buildInspectorScript(nonce) is the runtime, as a
// plain ES5-flavored string (this executes INSIDE the preview's own
// iframe, running whatever JS engine that page's own <!DOCTYPE> gets —
// no arrow functions/template literals/optional chaining, no
// eval/new Function anywhere in it); injectInspectorRuntime(html,
// nonce) is the parse5 tree-mutate-then-serialize step that actually
// gets it into a bundled document, following the exact same
// "never break the preview" contract every other file in this
// directory already commits to.
//
// Protocol (plan §5, step W6.4's own "Build" line) — every message
// either direction carries `nonce` so PreviewPane.jsx's listener can
// tell a message from THIS build's bridge apart from a stale one, same
// reasoning as consoleBridge.js's own header:
//
//   frame  -> parent   {source:"minime-preview", nonce, type:"minime:select",
//                        mm, tag, classes, textPreview, htmlPreview,
//                        rect, styles, ancestors, instanceCount, dynamic}
//   frame  -> parent   {source:"minime-preview", nonce, type:"minime:inspectExited"}
//                       -- NOT in the plan's own message list, but a direct
//                       consequence of "Esc exits inspect mode": if the
//                       FRAME can turn inspect mode off on its own (Esc),
//                       the PARENT's crosshair toggle has to find out, or
//                       it's left showing "on" for a mode that's actually
//                       off. This is that notification.
//   parent -> frame    {source:"minime-parent", nonce, type:"minime:inspect", on}
//   parent -> frame    {source:"minime-parent", nonce, type:"minime:highlight", mm}
//
// `minime:highlight` is received and drawn by this module (a highlight
// outline distinct from the hover outline) but nothing in THIS step
// sends one — the trigger ("cursor movement in the editor... so the
// preview outlines the element you're editing") is W6.5's own job. This
// just makes sure the wire format and the frame-side handler for it
// already exist so W6.5 only has to add a sender.
//
// Caps (same "cap sizes" discipline consoleBridge.js's own header
// documents, same numbers where the shape overlaps — an arg/message is
// an arg/message either bridge is posting):
import * as parse5 from "parse5";

export const MAX_TEXT_PREVIEW_LENGTH = 200;
export const MAX_HTML_PREVIEW_LENGTH = 1024; // plan's own explicit "htmlPreview(≤1KB)"
export const MAX_ANCESTORS = 10;

/**
 * The runtime script, as a string, with `nonce` baked in via
 * JSON.stringify — same safety note as consoleBridge.js's own
 * buildBridgeScript() (a nonce containing a quote/backslash must not
 * break out of the embedded string literal).
 *
 * @param {string} nonce
 * @returns {string}
 */
export function buildInspectorScript(nonce) {
  return (
    "(function(){\n" +
    "if (window.__mmInspectorInstalled__) return;\n" +
    "window.__mmInspectorInstalled__ = true;\n" +
    "var MM_NONCE = " + JSON.stringify(String(nonce)) + ";\n" +
    "var MAX_TEXT = " + MAX_TEXT_PREVIEW_LENGTH + ";\n" +
    "var MAX_HTML = " + MAX_HTML_PREVIEW_LENGTH + ";\n" +
    "var MAX_ANCESTORS = " + MAX_ANCESTORS + ";\n" +
    "var inspecting = false;\n" +
    "var overlayEl = null;\n" + // hover outline, follows the cursor
    "var labelEl = null;\n" + // small tag/size readout next to the hover outline
    "var highlightEl = null;\n" + // minime:highlight's own outline -- independent of hover, can be showing at the same time
    "function mmTruncate(str, max) {\n" +
    "  if (typeof str !== 'string') return str;\n" +
    "  return str.length > max ? (str.slice(0, max) + '…') : str;\n" +
    "}\n" +
    "function mmPost(payload) {\n" +
    "  try {\n" +
    "    var msg = { source: 'minime-preview', nonce: MM_NONCE };\n" +
    "    for (var k in payload) if (Object.prototype.hasOwnProperty.call(payload, k)) msg[k] = payload[k];\n" +
    "    window.parent.postMessage(msg, '*');\n" +
    "  } catch (e) { /* must never break the preview itself */ }\n" +
    "}\n" +
    // A box drawn with `position: fixed` in the FRAME's own viewport
    // coordinates -- getBoundingClientRect() is already viewport-relative,
    // so this needs no scroll-offset math, and stays correctly placed
    // under the cursor even if the preview's own page scrolls.
    "function mmMakeBox(color) {\n" +
    "  var el = document.createElement('div');\n" +
    "  el.style.position = 'fixed';\n" +
    "  el.style.zIndex = '2147483647';\n" + // max safe z-index -- must always win over the page's own content
    "  el.style.pointerEvents = 'none';\n" +
    "  el.style.border = '1.5px solid ' + color;\n" +
    "  el.style.background = color + '1a';\n" + // ~10% alpha fill, same color as the border
    "  el.style.boxSizing = 'border-box';\n" +
    "  el.style.transition = 'none';\n" +
    "  document.documentElement.appendChild(el);\n" +
    "  return el;\n" +
    "}\n" +
    "function mmPositionBox(el, rect) {\n" +
    "  el.style.left = rect.left + 'px';\n" +
    "  el.style.top = rect.top + 'px';\n" +
    "  el.style.width = rect.width + 'px';\n" +
    "  el.style.height = rect.height + 'px';\n" +
    "  el.style.display = 'block';\n" +
    "}\n" +
    // Created lazily (on first use, not at install time) -- this script
    // is injected as the first child of <head> so it runs before
    // document.body exists; nothing needs the overlay/label DOM nodes
    // until a real hover, which can only happen once the page has
    // actually loaded.
    "function mmEnsureOverlay() {\n" +
    "  if (!document.body) return false;\n" +
    "  if (!overlayEl) overlayEl = mmMakeBox('#4f7cff');\n" +
    "  if (!highlightEl) highlightEl = mmMakeBox('#f59e0b');\n" +
    "  if (!labelEl) {\n" +
    "    labelEl = document.createElement('div');\n" +
    "    labelEl.style.position = 'fixed';\n" +
    "    labelEl.style.zIndex = '2147483647';\n" +
    "    labelEl.style.pointerEvents = 'none';\n" +
    "    labelEl.style.background = '#4f7cff';\n" +
    "    labelEl.style.color = '#fff';\n" +
    "    labelEl.style.font = '11px monospace';\n" +
    "    labelEl.style.padding = '2px 5px';\n" +
    "    labelEl.style.borderRadius = '3px';\n" +
    "    labelEl.style.whiteSpace = 'nowrap';\n" +
    "    labelEl.style.display = 'none';\n" +
    "    document.documentElement.appendChild(labelEl);\n" +
    "  }\n" +
    "  return true;\n" +
    "}\n" +
    "function mmHideHover() {\n" +
    "  if (overlayEl) overlayEl.style.display = 'none';\n" +
    "  if (labelEl) labelEl.style.display = 'none';\n" +
    "}\n" +
    "function mmHideHighlight() {\n" +
    "  if (highlightEl) highlightEl.style.display = 'none';\n" +
    "}\n" +
    // The heart of "elements created at runtime (no data-mm) resolve to
    // the nearest static ancestor" -- Element.closest('[data-mm]')
    // already walks up through exactly the ancestors this needs, so
    // there is no separate DOM-walking loop to get wrong here. Returns
    // null when NOTHING up to (and including) <html> has a data-mm --
    // an entirely-dynamic page with no static instrumented markup at
    // all, which callers treat as "nothing to select".
    "function mmResolve(target) {\n" +
    "  if (!target || typeof target.closest !== 'function') return null;\n" +
    "  var el = target.closest('[data-mm]');\n" +
    "  if (!el) return null;\n" +
    "  return { el: el, dynamic: el !== target };\n" +
    "}\n" +
    "function mmAncestorChain(el) {\n" +
    "  var chain = [];\n" +
    "  var node = el.parentElement;\n" +
    "  while (node && chain.length < MAX_ANCESTORS) {\n" +
    "    var mm = node.getAttribute && node.getAttribute('data-mm');\n" +
    "    if (mm) chain.push(mm);\n" +
    "    node = node.parentElement;\n" +
    "  }\n" +
    "  return chain;\n" +
    "}\n" +
    // getComputedStyle's shorthand properties (padding/margin) DO return
    // a single resolved string for most engines when read this way
    // (e.g. '8px 16px') -- good enough for a hover/select readout, not a
    // claim of pixel-perfect style introspection.
    "function mmStyles(el) {\n" +
    "  var computed = window.getComputedStyle(el);\n" +
    "  return {\n" +
    "    color: computed.color,\n" +
    "    background: computed.backgroundColor,\n" +
    "    fontSize: computed.fontSize,\n" +
    "    padding: computed.padding,\n" +
    "    margin: computed.margin,\n" +
    "    display: computed.display\n" +
    "  };\n" +
    "}\n" +
    "function mmRect(rect) {\n" +
    "  return { x: rect.x, y: rect.y, top: rect.top, left: rect.left, right: rect.right, bottom: rect.bottom, width: rect.width, height: rect.height };\n" +
    "}\n" +
    "function mmLabelText(el, dynamic) {\n" +
    "  var rect = el.getBoundingClientRect();\n" +
    "  var cls = el.className && typeof el.className === 'string' ? el.className.trim().split(/\\s+/).filter(Boolean) : [];\n" +
    "  var text = el.tagName.toLowerCase() + (cls.length ? '.' + cls.join('.') : '') + ' ' + Math.round(rect.width) + '×' + Math.round(rect.height);\n" +
    "  return dynamic ? (text + ' · dynamic — nearest source element') : text;\n" +
    "}\n" +
    "function mmOnMouseMove(event) {\n" +
    "  if (!inspecting || !mmEnsureOverlay()) return;\n" +
    "  var resolved = mmResolve(event.target);\n" +
    "  if (!resolved) { mmHideHover(); return; }\n" +
    "  var rect = resolved.el.getBoundingClientRect();\n" +
    "  mmPositionBox(overlayEl, rect);\n" +
    "  labelEl.textContent = mmLabelText(resolved.el, resolved.dynamic);\n" +
    "  labelEl.style.display = 'block';\n" +
    // Label sits just above the outlined box, flipping below it near
    // the top of the viewport so it's never clipped off-screen.
    "  var labelTop = rect.top - 20;\n" +
    "  labelEl.style.left = Math.max(0, rect.left) + 'px';\n" +
    "  labelEl.style.top = (labelTop < 0 ? rect.bottom + 2 : labelTop) + 'px';\n" +
    "}\n" +
    "function mmOnClick(event) {\n" +
    "  if (!inspecting) return;\n" +
    // Capture-phase + prevent/stop, per the plan's own line -- a real
    // <button type=submit> or <a href> must not actually submit/navigate
    // while the person is trying to SELECT it, not activate it.
    "  event.preventDefault();\n" +
    "  event.stopPropagation();\n" +
    "  var resolved = mmResolve(event.target);\n" +
    "  if (!resolved) return;\n" +
    "  var el = resolved.el;\n" +
    "  var mm = el.getAttribute('data-mm');\n" +
    "  var rect = el.getBoundingClientRect();\n" +
    "  var cls = el.className && typeof el.className === 'string' ? el.className.trim().split(/\\s+/).filter(Boolean) : [];\n" +
    "  var instanceCount = 1;\n" +
    "  try { instanceCount = document.querySelectorAll('[data-mm=\"' + mm.replace(/\"/g, '\\\\\"') + '\"]').length; } catch (e) {}\n" +
    "  mmPost({\n" +
    "    type: 'minime:select',\n" +
    "    mm: mm,\n" +
    "    tag: el.tagName.toLowerCase(),\n" +
    "    classes: cls,\n" +
    "    textPreview: mmTruncate((el.textContent || '').replace(/\\s+/g, ' ').trim(), MAX_TEXT),\n" +
    "    htmlPreview: mmTruncate(el.outerHTML || '', MAX_HTML),\n" +
    "    rect: mmRect(rect),\n" +
    "    styles: mmStyles(el),\n" +
    "    ancestors: mmAncestorChain(el),\n" +
    "    instanceCount: instanceCount,\n" +
    "    dynamic: resolved.dynamic\n" +
    "  });\n" +
    "}\n" +
    "function mmSetInspecting(on) {\n" +
    "  inspecting = !!on;\n" +
    "  if (!inspecting) { mmHideHover(); }\n" +
    "}\n" +
    "function mmOnKeyDown(event) {\n" +
    "  if (event.key === 'Escape' && inspecting) {\n" +
    "    mmSetInspecting(false);\n" +
    "    mmPost({ type: 'minime:inspectExited' });\n" +
    "  }\n" +
    "}\n" +
    "window.addEventListener('mousemove', mmOnMouseMove, true);\n" +
    "window.addEventListener('click', mmOnClick, true);\n" + // capture: true -- must see the click before the page's own handlers do
    "window.addEventListener('keydown', mmOnKeyDown, true);\n" +
    "window.addEventListener('message', function (event) {\n" +
    "  if (event.source !== window.parent) return;\n" +
    "  var data = event.data;\n" +
    "  if (!data || data.source !== 'minime-parent' || data.nonce !== MM_NONCE) return;\n" +
    "  if (data.type === 'minime:inspect') {\n" +
    "    mmSetInspecting(!!data.on);\n" +
    "  } else if (data.type === 'minime:highlight') {\n" +
    "    if (!mmEnsureOverlay()) return;\n" +
    "    if (!data.mm) { mmHideHighlight(); return; }\n" +
    "    var target = null;\n" +
    "    try { target = document.querySelector('[data-mm=\"' + String(data.mm).replace(/\"/g, '\\\\\"') + '\"]'); } catch (e) {}\n" +
    "    if (!target) { mmHideHighlight(); return; }\n" +
    "    mmPositionBox(highlightEl, target.getBoundingClientRect());\n" +
    "  }\n" +
    "});\n" +
    "})();"
  );
}

function findFirst(node, tagName) {
  if (node.tagName === tagName) return node;
  for (const child of node.childNodes || []) {
    const found = findFirst(child, tagName);
    if (found) return found;
  }
  return null;
}

/**
 * Inserts buildInspectorScript(nonce) as the first child of `html`'s
 * <head> (falling back to <html>) — same placement, same "return the
 * input untouched rather than ever break the preview" contract, and
 * (necessarily) the same parse5 approach consoleBridge.js's own
 * injectConsoleBridge() already uses; see that function's docstring for
 * why this can't just be a naive string-splice on arbitrary HTML.
 *
 * Composable with injectConsoleBridge(): PreviewPane.jsx calls both,
 * one after the other, on the same `html` string with the SAME nonce —
 * two independent parse5 round trips rather than one combined injector,
 * matching this directory's existing "one file, one concern" split
 * (bundleStatic/instrument/consoleBridge are all already separate
 * modules the caller composes, not one do-everything function).
 *
 * @param {string} html - bundleStatic()'s own output (already possibly
 *   run through injectConsoleBridge())
 * @param {string} nonce - the SAME nonce PreviewPane.jsx passed to
 *   injectConsoleBridge() for this build, so one parent-side listener
 *   can verify messages from either bridge against one value
 * @returns {string}
 */
export function injectInspectorRuntime(html, nonce) {
  let document;
  try {
    document = parse5.parse(html, { sourceCodeLocationInfo: false });
  } catch {
    return html;
  }

  const target = findFirst(document, "head") || findFirst(document, "html");
  if (!target) return html;

  const scriptNode = {
    nodeName: "script",
    tagName: "script",
    attrs: [],
    namespaceURI: target.namespaceURI,
    childNodes: [],
    parentNode: target,
  };
  scriptNode.childNodes = [{ nodeName: "#text", value: buildInspectorScript(nonce), parentNode: scriptNode }];

  target.childNodes = target.childNodes || [];
  target.childNodes.unshift(scriptNode);

  try {
    return parse5.serialize(document);
  } catch {
    return html;
  }
}
