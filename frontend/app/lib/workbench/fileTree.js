// frontend/app/lib/workbench/fileTree.js — W2.3a (Build Workbench plan).
// Pure functions behind Explorer.jsx's tree. `buildFileTree()` used to
// live inline in BuildTab.jsx next to the old CodeView (patch 10, step
// 16); the plan's own W2.3 line ("Explorer keeps `buildFileTree()`")
// moves it here unchanged in behavior, alongside the few small helpers
// the Explorer needs on top of it.
//
// No imports on purpose — same reasoning as editorUtils.js: everything
// here is plain data in / plain data out, which is what lets
// __tests__/fileTree.test.mjs load THIS file's real source and run it
// with plain `node` (no browser, no bundler, no JS test runner — the
// repo has none, see editorStore.test.mjs's own header). Adding an
// `import` here would break that test's loader on purpose: it refuses
// to run a file that has one.
//
// Data shape: workspace_code_files.list_files() returns a flat
// `{file_path: meta}` map with no separate directory rows (that
// module's own docstring calls this out and says the tree is built
// client-side), so folders only exist implicitly, as prefixes of file
// paths.

/**
 * @param {{[path: string]: object}} filesMeta - FileProvider.list()'s result
 * @returns {{type: "dir", name: string, path: string, children: object}}
 *   a root dir node. `children` is keyed by `"d:<name>"` / `"f:<name>"`
 *   rather than the bare name: workspace paths are just strings, so
 *   nothing stops a file `a` and a folder `a/` (from `a/b.js`) from
 *   coexisting, and with bare-name keys the second one written would
 *   either overwrite the first or — as the pre-W2.3a version did —
 *   try to descend into a file node and throw, white-screening the
 *   whole Build tab. Callers should iterate via sortedChildren(),
 *   never index `children` directly.
 */
export function buildFileTree(filesMeta) {
  const root = { type: "dir", name: "", path: "", children: {} };
  for (const path of Object.keys(filesMeta || {}).sort()) {
    const parts = path.split("/");
    let node = root;
    let acc = "";
    parts.forEach((part, i) => {
      acc = acc ? `${acc}/${part}` : part;
      if (i === parts.length - 1) {
        node.children[`f:${part}`] = { type: "file", name: part, path: acc, meta: filesMeta[path] };
      } else {
        const key = `d:${part}`;
        if (!node.children[key]) {
          node.children[key] = { type: "dir", name: part, path: acc, children: {} };
        }
        node = node.children[key];
      }
    });
  }
  return root;
}

/**
 * A dir node's children in display order: directories first
 * (alphabetical), then files (alphabetical) — the same convention as
 * most file-tree UIs, so generated folders like `src/` / `tests/`
 * don't get interleaved with loose root files. (This is the ordering
 * the old inline TreeNode applied on every render.)
 */
export function sortedChildren(dirNode) {
  return Object.values(dirNode.children).sort((a, b) => {
    if (a.type !== b.type) return a.type === "dir" ? -1 : 1;
    return a.name.localeCompare(b.name);
  });
}

/** Last path segment: "src/App.jsx" -> "App.jsx". */
export function basename(path) {
  const i = path.lastIndexOf("/");
  return i === -1 ? path : path.slice(i + 1);
}

/** Everything before the last "/": "src/a/App.jsx" -> "src/a"; "" for a root file. */
export function dirname(path) {
  const i = path.lastIndexOf("/");
  return i === -1 ? "" : path.slice(0, i);
}

/**
 * Every folder that contains `path`, outermost first:
 * "src/a/App.jsx" -> ["src", "src/a"]. Used to expand the tree down to
 * the active file.
 */
export function ancestorDirs(path) {
  const parts = path.split("/");
  const dirs = [];
  let acc = "";
  for (let i = 0; i < parts.length - 1; i += 1) {
    acc = acc ? `${acc}/${parts[i]}` : parts[i];
    dirs.push(acc);
  }
  return dirs;
}

/**
 * The set of first-level folders across `paths` — what the tree
 * auto-expands the first time files show up, so it isn't a single
 * collapsed root on first load (the old loadFileList() did this
 * inline).
 */
export function topLevelDirs(paths) {
  const dirs = new Set();
  for (const path of paths) {
    if (path.includes("/")) dirs.add(path.split("/")[0]);
  }
  return dirs;
}
