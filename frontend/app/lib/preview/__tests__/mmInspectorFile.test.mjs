// W6.6 (Build Workbench plan) — tests for lib/preview/mmInspectorFile.js.
// Run: node frontend/app/lib/preview/__tests__/mmInspectorFile.test.mjs
import * as parse5 from "parse5";
import { loadSource } from "../../workbench/__tests__/loadSource.mjs";
import { buildBridgeScript } from "../../preview/consoleBridge.js";
import { buildInspectorScript } from "../../preview/inspectorRuntime.js";

const { buildMmInspectorFileContent } = loadSource("../../preview/mmInspectorFile.js", {
  imports: { "./consoleBridge": { buildBridgeScript }, "./inspectorRuntime": { buildInspectorScript } },
});

let failures = 0;
function assert(cond, msg) {
  if (!cond) {
    failures++;
    console.error(`FAIL: ${msg}`);
  } else {
    console.log(`PASS: ${msg}`);
  }
}

const content = buildMmInspectorFileContent("n1");

assert(content.includes("__mmConsoleBridgeInstalled__"), "contains the console bridge");
assert(content.includes("__mmInspectorInstalled__"), "contains the inspector runtime");
assert(content.indexOf("__mmConsoleBridgeInstalled__") < content.indexOf("__mmInspectorInstalled__"), "the console bridge comes first (order doesn't matter functionally, but this pins it so a future edit doesn't reorder them by accident)");
assert((content.match(/"n1"/g) || []).length >= 2, "the same nonce is embedded in BOTH runtimes");
assert(!content.includes("<script") && !content.includes("</script"), "plain JS content, no HTML wrapper of any kind -- this is imported as a module, not injected into a document");

// The result is valid, standalone JS (both IIFEs, back to back) — a
// real Node vm compile check, same tier consoleBridge.test.mjs/
// inspectorRuntime.test.mjs already use for their own individual halves.
{
  const vm = await import("node:vm");
  let threw = false;
  try {
    new vm.Script(content);
  } catch {
    threw = true;
  }
  assert(!threw, "the combined file is syntactically valid JavaScript");
}

// A different nonce per build must not leak the previous one in.
const contentB = buildMmInspectorFileContent("n2");
assert(!contentB.includes('"n1"'), "a fresh nonce produces a fresh file with no trace of the previous one");

if (failures > 0) {
  console.error(`\n${failures} assertion(s) failed.`);
  process.exit(1);
} else {
  console.log("\nAll mmInspectorFile.js tests passed.");
}
