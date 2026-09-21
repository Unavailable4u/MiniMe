// frontend/app/lib/workbench/explorerOps.js — W2.4 (Build Workbench
// plan). The rules behind the explorer's file operations, as plain
// functions: is this a legal name, what does "Duplicate" call the copy,
// which moves does a drop amount to (and is any of them illegal), what
// does the delete confirmation say. Split out of the components for the
// same reason tabUtils.js is — each of these is a "given this state,
// what should happen" rule, and a rule is far easier to keep right as a
// tested function than as a branch inside an event handler. The async
// half (talking to the provider, keeping open tabs in step) is
// hooks/useExplorerOps.js; this file never touches either.
//
// Only import: ./fileTree, for the path helpers (the test passes it in
// through loadSource's `imports` map).
//
// Names checked here mirror the server's own rules
// (eo/workspace_code_files._validate_file_path: the character set, the
// 512-char path limit, no `..`), so a bad name is refused in the input,
// instantly and with a reason, instead of as a 400 after a round trip.
// The server stays the authority — everything below is a courtesy in
// front of it, and every server refusal is still surfaced.
import { basename, dirname, isPlaceholderPath, isSameOrDescendant } from "./fileTree";

/** workspace_code_files._MAX_PATH_LENGTH. */
export const MAX_PATH_LENGTH = 512;

// workspace_code_files._VALID_PATH_CHARS, minus "/" (this is one name,
// not a path).
const NAME_CHARS = /^[A-Za-z0-9_.[\]()@+~=, -]+$/;

/** `dir` + `name` as a workspace path; `dir` is "" for the project root. */
export function joinPath(dir, name) {
  return dir ? `${dir}/${name}` : name;
}

/**
 * Every path that "exists" in a project: each file plus every folder
 * above it. Folders have no rows of their own (see fileTree.js), so a
 * name check has to look at both — a new file can't be called `src`
 * if a `src/` folder is there.
 *
 * @param {Iterable<string>} filePaths
 * @returns {Set<string>}
 */
export function entryPaths(filePaths) {
  const all = new Set();
  for (const path of filePaths) {
    all.add(path);
    let dir = dirname(path);
    while (dir && !all.has(dir)) {
      all.add(dir);
      dir = dirname(dir);
    }
  }
  return all;
}

/**
 * Checks a name typed into the explorer's inline input.
 *
 * @param {string} rawName
 * @param {object} ctx
 * @param {string} ctx.dir - the folder it will live in ("" = root)
 * @param {Set<string>} ctx.taken - entryPaths() of the project
 * @param {string} [ctx.selfPath] - when renaming: the entry's own
 *   current path, which is allowed to "collide" with itself (renaming
 *   `a.js` to `a.js`, or fixing only its letter case, isn't a clash)
 * @returns {{name: string, error: string|null}} the trimmed name to use, and why not (null = fine)
 */
export function checkEntryName(rawName, { dir, taken, selfPath }) {
  const name = String(rawName ?? "").trim();
  const fail = (error) => ({ name, error });
  if (!name) return fail("Enter a name.");
  if (name.includes("/") || name.includes("\\")) return fail("A name can't contain / or \\.");
  if (name === "." || name === "..") return fail(`"${name}" isn't allowed as a name.`);
  if (!NAME_CHARS.test(name)) {
    return fail("Use letters, numbers, spaces and . _ - [ ] ( ) @ + ~ = , only.");
  }
  const path = joinPath(dir, name);
  if (path.length > MAX_PATH_LENGTH) return fail("That path is too long.");
  if (path !== selfPath && taken.has(path)) return fail(`"${name}" already exists here.`);
  return { name, error: null };
}

/**
 * Drops path segments that are already covered by an ancestor in the
 * same list — selecting a folder AND a file inside it to delete or move
 * means "the folder", and acting on both would delete/move the file
 * twice (the second time failing). Also removes duplicates; keeps the
 * order of what survives.
 *
 * @param {string[]} paths
 * @returns {string[]}
 */
export function collapseNested(paths) {
  const unique = [...new Set(paths)];
  return unique.filter((p) => !unique.some((other) => other !== p && isSameOrDescendant(p, other)));
}

/** The file paths that are `target` or under it. */
export function pathsUnder(filePaths, target) {
  return filePaths.filter((p) => isSameOrDescendant(p, target));
}

/**
 * The path "Duplicate" gives the copy: `App copy.jsx`, then
 * `App copy 2.jsx`, `App copy 3.jsx`… next to the original, the first
 * one nothing else is using. The extension stays last (a leading-dot
 * name like `.env` has none: `.env copy`); folders get no extension
 * treatment at all.
 *
 * @param {string} path
 * @param {Set<string>} taken - entryPaths() of the project
 * @param {{isDir?: boolean}} [opts]
 */
export function uniqueCopyPath(path, taken, { isDir = false } = {}) {
  const dir = dirname(path);
  const name = basename(path);
  const dot = isDir ? -1 : name.lastIndexOf(".");
  const stem = dot > 0 ? name.slice(0, dot) : name;
  const ext = dot > 0 ? name.slice(dot) : "";
  let candidate = joinPath(dir, `${stem} copy${ext}`);
  for (let n = 2; taken.has(candidate); n += 1) {
    candidate = joinPath(dir, `${stem} copy ${n}${ext}`);
  }
  return candidate;
}

/**
 * What dropping `sources` onto the folder `targetDir` means: the list
 * of moves to make, or the reason it can't be done. All-or-nothing —
 * one illegal item and nothing moves, so a drop never leaves a
 * half-moved selection to untangle.
 *
 * Skipped silently (not errors): items already directly inside the
 * target, since dragging a file onto its own folder is a slip, not a
 * request.
 *
 * @param {object} args
 * @param {string[]} args.sources - paths being dragged (files and/or folders)
 * @param {string} args.targetDir - "" = project root
 * @param {Set<string>} args.taken - entryPaths() of the project
 * @returns {{moves: {from: string, to: string}[], error: string|null}}
 */
export function planDrop({ sources, targetDir, taken }) {
  const where = targetDir ? `"${basename(targetDir)}"` : "the project root";
  const moves = [];
  const landing = new Set();
  for (const from of collapseNested(sources)) {
    const name = basename(from);
    if (dirname(from) === targetDir) continue; // already there
    if (isSameOrDescendant(targetDir, from)) {
      return { moves: [], error: `Can't move "${name}" into itself.` };
    }
    const to = joinPath(targetDir, name);
    if (taken.has(to)) return { moves: [], error: `"${name}" already exists in ${where}.` };
    if (landing.has(to)) return { moves: [], error: `Two items named "${name}" can't go in the same folder.` };
    if (to.length > MAX_PATH_LENGTH) return { moves: [], error: `The new path for "${name}" would be too long.` };
    landing.add(to);
    moves.push({ from, to });
  }
  return { moves, error: null };
}

/**
 * The wording of the delete confirmation. Counts real files only (the
 * `.gitkeep` an empty folder is stored as isn't one) and names any
 * open files whose unsaved edits would go with them.
 *
 * @param {object} args
 * @param {string[]} args.paths - what was selected
 * @param {string[]} args.filePaths - every file path in the project
 * @param {string[]} [args.dirtyPaths] - open files with unsaved edits
 * @returns {{roots: string[], fileCount: number, title: string, message: string}}
 */
export function deleteSummary({ paths, filePaths, dirtyPaths = [] }) {
  const roots = collapseNested(paths);
  const real = filePaths.filter((p) => !isPlaceholderPath(p));
  const under = (root) => real.filter((p) => isSameOrDescendant(p, root));
  const fileCount = new Set(roots.flatMap(under)).size;
  const isFolder = (root) => filePaths.some((p) => p.startsWith(`${root}/`));

  let title;
  let message;
  if (roots.length === 1) {
    const root = roots[0];
    const name = basename(root);
    if (isFolder(root)) {
      title = `Delete folder "${name}"?`;
      message =
        fileCount === 0
          ? "This removes the empty folder."
          : `This removes the folder and ${fileCount === 1 ? "the 1 file" : `the ${fileCount} files`} in it.`;
    } else {
      title = `Delete "${name}"?`;
      message = "This removes the file from the project.";
    }
  } else {
    title = `Delete ${roots.length} items?`;
    message = `This removes ${roots.length} items${fileCount ? ` (${fileCount} ${fileCount === 1 ? "file" : "files"} in total)` : ""}.`;
  }
  message += " You can't undo this.";

  const unsaved = dirtyPaths.filter((p) => roots.some((r) => isSameOrDescendant(p, r)));
  if (unsaved.length === 1) message += ` Unsaved changes in ${basename(unsaved[0])} will be lost.`;
  else if (unsaved.length > 1) message += ` Unsaved changes in ${unsaved.length} open files will be lost.`;

  return { roots, fileCount, title, message };
}
