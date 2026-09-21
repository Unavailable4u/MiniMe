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
// W2.4 additions (the explorer's operations need them): PLACEHOLDER_NAME /
// isPlaceholderPath / realFilePaths (the `.gitkeep` an empty folder is
// stored as stays out of sight), flattenVisible + filterPaths +
// rangeBetween (the explorer renders, filters, keyboard-navigates and
// range-selects one flat list of rows), and isSameOrDescendant /
// remapPath (path-prefix arithmetic shared by rename, move and the tab
// store). Still no imports.
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

// ---- W2.4 --------------------------------------------------------------

/**
 * What an "empty folder" is stored as: workspace_code_files has no
 * directory rows, so POST .../code/folders writes an empty
 * `<folder>/.gitkeep` to give the folder something to exist as (see
 * eo/workspace_code_files.create_folder()). It's plumbing, not a file
 * anyone made: the explorer keeps it out of the row list, the file
 * count and the filter, while the folder it props up still shows.
 */
export const PLACEHOLDER_NAME = ".gitkeep";

export function isPlaceholderPath(path) {
  return basename(path) === PLACEHOLDER_NAME;
}

/** `filesMeta`'s paths minus the folder placeholders. */
export function realFilePaths(filesMeta) {
  return Object.keys(filesMeta || {}).filter((p) => !isPlaceholderPath(p));
}

/** `path` is `ancestor` itself or lies somewhere under it ("a/b" is under "a"; "ab" is not). */
export function isSameOrDescendant(path, ancestor) {
  return path === ancestor || path.startsWith(`${ancestor}/`);
}

/**
 * Where `path` ends up after `from` is renamed/moved to `to`: `to`
 * itself, or `to` plus the rest of the path when `path` is inside the
 * moved folder. Anything not under `from` comes back unchanged.
 */
export function remapPath(path, from, to) {
  if (path === from) return to;
  if (path.startsWith(`${from}/`)) return to + path.slice(from.length);
  return path;
}

/**
 * The tree as the flat list of rows the explorer draws, in display
 * order. One list is what makes keyboard navigation (next/previous
 * row), shift-click ranges and drag targets simple — they're all
 * "index in this array".
 *
 * `visible` is null normally: a folder shows its children only if it's
 * in `expanded`. When a filter is active (`visible` = filterPaths()'s
 * result) only the matching files and their folders are listed, and
 * those folders count as open whatever `expanded` says — hiding a match
 * behind a collapsed folder would defeat the filter.
 *
 * @param {object} root - buildFileTree()'s root
 * @param {Set<string>} expanded - open folder paths
 * @param {{files: Set<string>, dirs: Set<string>}|null} [visible]
 * @returns {{path: string, name: string, type: "dir"|"file", depth: number, parent: string, open?: boolean}[]}
 *   `parent` is the containing folder's path ("" at the root)
 */
export function flattenVisible(root, expanded, visible = null) {
  const rows = [];
  function walk(node, depth, parent) {
    for (const entry of sortedChildren(node)) {
      if (entry.type === "dir") {
        if (visible && !visible.dirs.has(entry.path)) continue;
        const open = visible ? true : expanded.has(entry.path);
        rows.push({ path: entry.path, name: entry.name, type: "dir", depth, parent, open });
        if (open) walk(entry, depth + 1, entry.path);
      } else {
        if (entry.name === PLACEHOLDER_NAME) continue;
        if (visible && !visible.files.has(entry.path)) continue;
        rows.push({ path: entry.path, name: entry.name, type: "file", depth, parent });
      }
    }
  }
  walk(root, 0, "");
  return rows;
}

/**
 * The explorer's filter box. A file matches when its whole path
 * contains every whitespace-separated term, case-insensitively — so
 * "src app" finds `src/App.jsx`, and a bare "test" finds everything in
 * a `tests/` folder. Returns null for a blank query (= no filtering),
 * else the matching files plus every folder above them.
 *
 * @param {string[]} paths
 * @param {string} query
 * @returns {{files: Set<string>, dirs: Set<string>}|null}
 */
export function filterPaths(paths, query) {
  const terms = String(query || "")
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean);
  if (terms.length === 0) return null;
  const files = new Set();
  const dirs = new Set();
  for (const path of paths) {
    if (isPlaceholderPath(path)) continue;
    const lower = path.toLowerCase();
    if (terms.every((t) => lower.includes(t))) {
      files.add(path);
      for (const d of ancestorDirs(path)) dirs.add(d);
    }
  }
  return { files, dirs };
}

/**
 * Paths of every row from `fromPath` to `toPath` inclusive, in list
 * order, whichever way round they are — a shift-click range. If the
 * anchor isn't in the list any more (its folder was collapsed) the
 * range is just the target.
 */
export function rangeBetween(rows, fromPath, toPath) {
  const j = rows.findIndex((r) => r.path === toPath);
  if (j === -1) return [];
  const i = rows.findIndex((r) => r.path === fromPath);
  if (i === -1) return [toPath];
  const lo = Math.min(i, j);
  const hi = Math.max(i, j);
  return rows.slice(lo, hi + 1).map((r) => r.path);
}
