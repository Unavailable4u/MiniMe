// W6.5 (Build Workbench plan) — tests for lib/workbench/elementRef.js.
// Run: node frontend/app/lib/workbench/__tests__/elementRef.test.mjs
import { loadSource } from "./loadSource.mjs";

const { elementFromMessage, elementLabel, describeElement } = loadSource("../elementRef.js");

let failures = 0;
function assertEqual(actual, expected, msg) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) {
    failures++;
    console.error(`FAIL: ${msg}\n  expected: ${e}\n  actual:   ${a}`);
  } else {
    console.log(`PASS: ${msg}`);
  }
}

// --- elementFromMessage -------------------------------------------------------

const good = elementFromMessage({
  type: "minime:select",
  mm: "index.html:4:2:7:8",
  tag: "button",
  classes: ["btn-primary", "large"],
  textPreview: "Save",
  styles: { color: "rgb(0, 0, 0)", background: "red", fontSize: "14px", padding: "8px", margin: "0px", display: "block", extra: "ignored" },
  dynamic: true,
  instanceCount: 3,
  rect: { width: 1 },
});
assertEqual(good.mm, "index.html:4:2:7:8", "keeps the mm");
assertEqual(good.element.tag, "button", "keeps the tag");
assertEqual(good.element.classes, ["btn-primary", "large"], "keeps the classes");
assertEqual(Object.keys(good.element.styles).sort(), ["background", "color", "display", "fontSize", "margin", "padding"], "keeps ONLY the six known style keys — an unknown key from the page is dropped");
assertEqual(good.element.dynamic, true, "keeps dynamic");
assertEqual(good.element.instanceCount, 3, "keeps instanceCount");
assertEqual("rect" in good.element, false, "fields the chip doesn't need (rect) are not carried onto the ref");

assertEqual(elementFromMessage(null), null, "null message -> null");
assertEqual(elementFromMessage({ tag: "div" }), null, "no mm -> null (nothing to resolve to a source range)");
assertEqual(elementFromMessage({ mm: "" }), null, "an empty mm -> null");
assertEqual(elementFromMessage({ mm: 5 }), null, "a non-string mm -> null");

const hostile = elementFromMessage({
  mm: "a.html:1:0:1:5",
  tag: "x".repeat(500),
  classes: ["ok", 7, null, "y".repeat(500), ...Array.from({ length: 50 }, (_, i) => `c${i}`)],
  textPreview: "t".repeat(5000),
  styles: { color: "c".repeat(5000) },
  instanceCount: "lots",
});
assertEqual(hostile.element.tag.length, 40, "an oversized tag is capped");
assertEqual(hostile.element.classes.length, 20, "the class list is capped");
assertEqual(hostile.element.classes.every((c) => typeof c === "string" && c.length <= 80), true, "non-string classes are dropped, long ones capped");
assertEqual(hostile.element.textPreview.length, 200, "textPreview is capped");
assertEqual(hostile.element.styles.color.length, 120, "a style value is capped");
assertEqual(hostile.element.instanceCount, 1, "a non-numeric instanceCount falls back to 1");

assertEqual(elementFromMessage({ mm: "a.html:1:0:1:5", instanceCount: 0 }).element.instanceCount, 1, "instanceCount below 1 is clamped to 1");

// --- elementLabel ---------------------------------------------------------------

assertEqual(elementLabel({ tag: "button", classes: ["btn-primary"] }), "button.btn-primary", "tag + class");
assertEqual(elementLabel({ tag: "div", classes: ["a", "b", "c", "d"] }), "div.a.b", "at most two classes on a chip");
assertEqual(elementLabel({ tag: "p", classes: [] }), "p", "no classes -> just the tag");
assertEqual(elementLabel({}), "element", "an empty element still yields a label");
assertEqual(elementLabel(null), "element", "null doesn't throw");

// --- describeElement --------------------------------------------------------------

const text = describeElement(good.element);
assertEqual(text.startsWith('Selected element in the live preview: <button class="btn-primary large">'), true, "starts with the tag and its classes");
assertEqual(text.includes('with text "Save"'), true, "includes the text preview");
assertEqual(text.includes("renders 3 times"), true, "notes a multiply-rendered element");
assertEqual(text.includes("created at runtime"), true, "notes a dynamic resolution");
assertEqual(text.includes("Computed style: color: rgb(0, 0, 0);"), true, "includes computed styles");

assertEqual(describeElement({ tag: "p", classes: [], instanceCount: 1, styles: {} }), "Selected element in the live preview: <p>", "a bare element is just the tag — no empty parens or style line");

if (failures > 0) {
  console.error(`\n${failures} assertion(s) failed.`);
  process.exit(1);
} else {
  console.log("\nAll elementRef.js tests passed.");
}
