// W6.6 (Build Workbench plan) — tests for lib/preview/reactBundle.js.
// Run: node frontend/app/lib/preview/__tests__/reactBundle.test.mjs
import { loadSource } from "../../workbench/__tests__/loadSource.mjs";

const { shouldBundleFile, pickReactEntry, parseDependencies, withInjectedImport, inspectorPathFor } = loadSource("../../preview/reactBundle.js");

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

// --- shouldBundleFile --------------------------------------------------------

assertEqual(shouldBundleFile("src/App.jsx"), true, "a plain source file is bundled");
assertEqual(shouldBundleFile("src/logo.png"), false, "an image is skipped");
assertEqual(shouldBundleFile("package-lock.json"), false, "package-lock.json is skipped by exact basename, even though its extension is plain .json");
assertEqual(shouldBundleFile("yarn.lock"), false, "a lockfile is skipped");
assertEqual(shouldBundleFile("node_modules/react/index.js"), false, "anything under node_modules is skipped");
assertEqual(shouldBundleFile("src/node_modules_data.js"), true, "a FILENAME merely containing 'node_modules' is not a directory segment match");
assertEqual(shouldBundleFile("dist/bundle.js"), false, "a build output directory is skipped");
assertEqual(shouldBundleFile(""), false, "an empty path is skipped");
assertEqual(shouldBundleFile(null), false, "a non-string path doesn't throw");
assertEqual(shouldBundleFile("README.md"), true, "a plain text file with no special extension is bundled");
assertEqual(shouldBundleFile("src/styles.css"), true, "CSS is bundled");

// --- pickReactEntry -----------------------------------------------------------

assertEqual(pickReactEntry(["src/App.jsx", "src/main.jsx", "package.json"]), "src/main.jsx", "prefers the Vite convention (src/main.jsx) when present");
assertEqual(pickReactEntry(["src/index.js", "src/App.js"]), "src/index.js", "falls back to a CRA-style src/index.js");
assertEqual(pickReactEntry(["index.js"]), "index.js", "falls back to a bare root index.js");
assertEqual(pickReactEntry(["src/App.jsx"]), null, "no known entry convention present -> null, not a guess");
assertEqual(pickReactEntry([]), null, "an empty project -> null");
assertEqual(pickReactEntry(["src/main.tsx", "src/main.jsx"]), "src/main.jsx", "candidate order matters: main.jsx wins over main.tsx per the fixed priority list");

// --- parseDependencies -------------------------------------------------------

assertEqual(
  parseDependencies('{"dependencies": {"react": "^18.2.0"}, "devDependencies": {"vite": "^5.0.0"}}'),
  { dependencies: { react: "^18.2.0" }, devDependencies: { vite: "^5.0.0" } },
  "extracts both dependency maps"
);
assertEqual(parseDependencies('{"name": "x"}'), { dependencies: {}, devDependencies: {} }, "a package.json with neither field -> both empty");
assertEqual(parseDependencies("{not valid json"), { dependencies: {}, devDependencies: {} }, "malformed JSON never throws -- empty deps instead");
assertEqual(parseDependencies(null), { dependencies: {}, devDependencies: {} }, "a null/missing package.json -> empty deps");
assertEqual(parseDependencies(""), { dependencies: {}, devDependencies: {} }, "an empty string -> empty deps");
assertEqual(
  parseDependencies('{"dependencies": {"react": "^18.0.0", "bad": 5, "also-bad": null}}'),
  { dependencies: { react: "^18.0.0" }, devDependencies: {} },
  "a non-string version value for a dependency is dropped rather than passed through"
);
assertEqual(parseDependencies('{"dependencies": ["react"]}'), { dependencies: {}, devDependencies: {} }, "dependencies as an array (malformed) is treated as absent, not thrown on");

// --- inspectorPathFor -------------------------------------------------------

assertEqual(inspectorPathFor("src/main.jsx"), "src/mm-inspector.js", "placed next to the entry, same directory");
assertEqual(inspectorPathFor("index.js"), "mm-inspector.js", "a root-level entry places it at the root too, no leading/double slash");
assertEqual(inspectorPathFor("a/b/c/main.jsx"), "a/b/c/mm-inspector.js", "a deeply nested entry still resolves via a plain relative import");

// --- withInjectedImport ---------------------------------------------------------

const files = { "src/main.jsx": 'import App from "./App";\nrender(<App/>);', "src/App.jsx": "export default function App() {}" };
const injected = withInjectedImport(files, "src/main.jsx", "./mm-inspector.js");
assertEqual(injected["src/main.jsx"], 'import "./mm-inspector.js";\nimport App from "./App";\nrender(<App/>);', "prepends the import line to the entry's own code");
assertEqual(injected["src/App.jsx"], files["src/App.jsx"], "every other file is untouched");
assertEqual(files["src/main.jsx"].startsWith("import App"), true, "the ORIGINAL files object passed in is never mutated");
assertEqual(withInjectedImport(files, null, "./mm-inspector.js"), files, "no entry found -> the files object is returned as-is (same reference), not a needless copy");
assertEqual(withInjectedImport(files, "src/missing.jsx", "./mm-inspector.js"), files, "an entry path not actually present in files is a no-op");

if (failures > 0) {
  console.error(`\n${failures} assertion(s) failed.`);
  process.exit(1);
} else {
  console.log("\nAll reactBundle.js tests passed.");
}
