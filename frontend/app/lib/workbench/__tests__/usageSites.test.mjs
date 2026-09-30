// W7.1b (Build Workbench plan) — tests for lib/workbench/usageSites.js.
// Run: node frontend/app/lib/workbench/__tests__/usageSites.test.mjs
import { loadSource } from "./loadSource.mjs";

const { componentNamesFromSource, findUsageSites, usageCandidatePaths, isComponentSourcePath, MAX_USAGE_SITES, MAX_COMPONENT_NAMES } =
  loadSource("../usageSites.js");

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
function assert(cond, msg) {
  if (!cond) {
    failures++;
    console.error(`FAIL: ${msg}`);
  } else {
    console.log(`PASS: ${msg}`);
  }
}

// --- isComponentSourcePath ---------------------------------------------------
assertEqual(["a.jsx", "a.tsx", "a.js", "a.mjs", "a.ts", "A.vue", "A.svelte", "A.JSX"].map(isComponentSourcePath), [true, true, true, true, true, true, true, true], "source extensions are accepted");
assertEqual(["a.css", "a.html", "a.md", "a", "", null, 5].map(isComponentSourcePath), [false, false, false, false, false, false, false], "non-source paths / junk are rejected");

// --- componentNamesFromSource -------------------------------------------------
assertEqual(componentNamesFromSource("src/Button.jsx", "export function Button(p) {\n  return <button/>;\n}\n"), ["Button"], "exported function component");
assertEqual(componentNamesFromSource("src/Button.jsx", "export default function Button() { return null }"), ["Button"], "export default function");
assertEqual(componentNamesFromSource("src/Card.jsx", "const Card = ({ title }) => <div>{title}</div>;\nexport default Card;\n"), ["Card"], "arrow component + default export ident (deduped)");
assertEqual(componentNamesFromSource("src/Card.jsx", "const Card = props => <div/>;"), ["Card"], "single-param arrow");
assertEqual(componentNamesFromSource("src/Fancy.jsx", "const Fancy = React.memo(function Inner() { return null });"), ["Fancy", "Inner"], "memo wrapper and its inner function");
assertEqual(componentNamesFromSource("src/Field.tsx", "export const Field = forwardRef<HTMLInputElement, P>((p, ref) => <input ref={ref}/>);"), ["Field"], "forwardRef with generics");
assertEqual(componentNamesFromSource("src/Legacy.jsx", "class Legacy extends React.Component { render() { return null } }"), ["Legacy"], "class component");
assertEqual(componentNamesFromSource("src/App.jsx", "export default Main;\nfunction Main() { return null }\nfunction helper() {}"), ["Main"], "default-exported identifier comes first; lowercase helpers ignored");
assertEqual(componentNamesFromSource("src/consts.js", "const API_URL = (x) => x;\nconst MAX = () => 1;"), [], "SHOUTY_CONSTANTS are not components (and `consts` is not PascalCase, so no filename fallback)");
assertEqual(componentNamesFromSource("src/Nav.jsx", "// nothing recognisable here\n"), ["Nav"], "falls back to a PascalCase file name");
assertEqual(componentNamesFromSource("src/nav.jsx", "// nothing\n"), [], "no fallback for a lowercase file name");
assertEqual(componentNamesFromSource("src/MyButton.vue", "<template><button/></template>"), ["MyButton", "my-button"], "a .vue file is named by its filename, plus kebab-case");
assertEqual(componentNamesFromSource("src/Chip.svelte", ""), ["Chip", "chip"], "a .svelte file likewise");
assertEqual(componentNamesFromSource("styles/app.css", "function Foo() {}"), [], "a non-source file can't define a component");
assertEqual(componentNamesFromSource("src/A.jsx", null), [], "null text does not throw (no names, no PascalCase-with-lowercase fallback for `A`)");
const many = Array.from({ length: 12 }, (_, i) => `function Comp${i}() {}`).join("\n");
assertEqual(componentNamesFromSource("src/Many.jsx", many).length, MAX_COMPONENT_NAMES, "names are capped");

// --- findUsageSites -----------------------------------------------------------
const app = [
  "import { Button } from './Button';",                       // 1
  "export function App() {",                                   // 2
  "  return (",                                                // 3
  "    <div>",                                                 // 4
  '      <Button variant="primary" onClick={() => go(1)}>',    // 5
  "        Buy now",                                           // 6
  "      </Button>",                                           // 7
  "      <Button",                                             // 8
  '        variant="ghost"',                                   // 9
  "        onClick={() => a > b}",                             // 10
  "      />",                                                  // 11
  "    </div>",                                                // 12
  "  );",                                                      // 13
  "}",                                                         // 14
].join("\n");

let out = findUsageSites({ "src/App.jsx": app }, ["Button"]);
assertEqual(out.sites.length, 2, "two usages found (the closing tag is not one)");
assertEqual([out.sites[0].fromLine, out.sites[0].toLine], [5, 5], "single-line opening tag: lines 5-5");
assertEqual(out.sites[0].snippet, '<Button variant="primary" onClick={() => go(1)}>', "the opening tag only — `=>` inside {} does not end it");
assertEqual(app.slice(out.sites[0].from, out.sites[0].to), out.sites[0].snippet, "from/to index the source text exactly");
assertEqual([out.sites[1].fromLine, out.sites[1].toLine], [8, 11], "multi-line self-closing tag: lines 8-11, `a > b` inside {} skipped");
assert(out.sites[1].snippet.endsWith("/>"), "…and it ends at the real `/>`");
assertEqual(out.sites[0].preview, '<Button variant="primary" onClick={() => go(1)}>', "preview is the trimmed first line");
assertEqual(out.truncated, false, "not truncated");

// boundaries
const tricky = [
  "const a: Array<Button> = [];",          // generic: preceded by a word char
  "<ButtonGroup />",                       // different component
  "<Button.Group />",                      // member access: different component
  "<Button:x />",                          // namespaced
  "// <Button /> in a comment",            // comment line
  " * <Button /> in a doc block",          // doc-comment line
  "{/* <Button /> */}",                    // JSX comment
  "</Button>",                             // closing tag
  "<Button>ok</Button>",                   // the one real usage
].join("\n");
out = findUsageSites({ "src/t.jsx": tricky }, ["Button"]);
assertEqual(out.sites.map((s) => s.fromLine), [9], "only the real usage on line 9 matches");

// quotes / braces inside the tag
const quoted = `<Button label="a > b" title='x > y' data={{ k: "}" }} tpl={\`a>\${1}\`}>go</Button>`;
out = findUsageSites({ "src/q.jsx": quoted }, ["Button"]);
assertEqual(out.sites[0].snippet, quoted.slice(0, quoted.indexOf(">go<") + 1), "`>` inside quotes, braces and a template literal doesn't end the tag");

// unterminated tag falls back to the first line
out = findUsageSites({ "src/u.jsx": "<Button foo={\nbar" }, ["Button"]);
assertEqual([out.sites[0].fromLine, out.sites[0].toLine, out.sites[0].snippet], [1, 1, "<Button foo={"], "an unterminated tag settles for its first line");

// exclusions
out = findUsageSites({ "src/Button.jsx": "<Button/>", "src/App.jsx": "<Button/>", "node_modules/x/i.jsx": "<Button/>", "dist/a.js": "<Button/>", "src/a.css": "<Button/>" }, ["Button"], { excludePath: "src/Button.jsx" });
assertEqual(out.sites.map((s) => s.path), ["src/App.jsx"], "the component's own file, vendored/build dirs and non-source files are skipped");

// ordering, several names, kebab-case
out = findUsageSites({ "z.vue": "<my-button/>", "a.vue": "<MyButton/>" }, ["MyButton", "my-button"]);
assertEqual(out.sites.map((s) => [s.path, s.name]), [["a.vue", "MyButton"], ["z.vue", "my-button"]], "sorted by path; each name matches its own form");

// regex metacharacters in a name can't break the search
out = findUsageSites({ "a.jsx": "<A$B/>" }, ["A$B"]);
assertEqual(out.sites.length, 1, "`$` in a component name is matched literally");
out = findUsageSites({ "a.jsx": "<Button/>" }, ["B.tton", "(", ""]);
assertEqual(out.sites.length, 0, "invalid / metacharacter names match nothing and don't throw");
assertEqual(findUsageSites({ "a.jsx": "<Button/>" }, []).sites, [], "no names: no sites");
assertEqual(findUsageSites(null, ["Button"]).sites, [], "null files: no sites");

// cap
const lots = Array.from({ length: MAX_USAGE_SITES + 10 }, () => "<Button/>").join("\n");
out = findUsageSites({ "a.jsx": lots }, ["Button"]);
assertEqual([out.sites.length, out.truncated], [MAX_USAGE_SITES, true], "results are capped and flagged truncated");
out = findUsageSites({ "a.jsx": lots }, ["Button"], { max: 3 });
assertEqual(out.sites.length, 3, "`max` option is honoured");

// CRLF: lines still count, offsets still index the text
const crlf = "x\r\n<Button a=\"1\"\r\n  b=\"2\">\r\n";
out = findUsageSites({ "c.jsx": crlf }, ["Button"]);
assertEqual([out.sites[0].fromLine, out.sites[0].toLine], [2, 3], "CRLF source: line numbers are right");
assertEqual(crlf.slice(out.sites[0].from, out.sites[0].to), out.sites[0].snippet, "CRLF source: offsets index the raw text");

// --- usageCandidatePaths -------------------------------------------------------
assertEqual(usageCandidatePaths(["b.jsx", "a.tsx", "x.css", "node_modules/q/i.js", "src/Own.jsx", "dist.js"], "src/Own.jsx"), ["a.tsx", "b.jsx", "dist.js"], "candidates: source only, no vendored dirs, own file excluded, sorted; a FILE named like a skipped dir is kept");
assertEqual(usageCandidatePaths(null), [], "null paths: []");

// --- a hostile file can't hang the scan -------------------------------------------
const t0 = Date.now();
findUsageSites({ "h.jsx": "<Button " + "{".repeat(200000) }, ["Button"]);
findUsageSites({ "h2.jsx": "<Button ".repeat(20000) }, ["Button"]);
assert(Date.now() - t0 < 3000, "pathological input finishes quickly (tag scan is capped)");

if (failures) {
  console.error(`\n${failures} failure(s)`);
  process.exit(1);
}
console.log("\nAll usageSites tests passed");
