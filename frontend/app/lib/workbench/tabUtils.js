// frontend/app/lib/workbench/tabUtils.js — W2.3a (Build Workbench plan).
// Pure decision logic for the workbench's tab strip and for keeping
// open buffers in step with the server. Split out of the components
// for the same reason editorUtils.js and fileTree.js are: the
// interesting part of each of these is a "given this state, what
// should happen" rule, and a rule is far easier to get right (and to
// keep right) when it's a function with a test than when it's tangled
// into an effect. No imports — see fileTree.js's header; the test
// loader in __tests__/tabUtils.test.mjs enforces it.

/**
 * Display labels for a tab strip. Normally just the file name; when
 * two open tabs share one (`src/index.js` and `public/index.js`), each
 * of those also gets a `hint` — its folder — so they can be told
 * apart. The hint for a root-level file is "root".
 *
 * @param {string[]} paths - open tabs, in strip order
 * @returns {{path: string, name: string, hint: string|null}[]}
 */
export function tabLabels(paths) {
  const nameOf = (p) => p.slice(p.lastIndexOf("/") + 1);
  const counts = new Map();
  for (const p of paths) counts.set(nameOf(p), (counts.get(nameOf(p)) || 0) + 1);
  return paths.map((path) => {
    const name = nameOf(path);
    if (counts.get(name) === 1) return { path, name, hint: null };
    const i = path.lastIndexOf("/");
    return { path, name, hint: i === -1 ? "root" : path.slice(0, i) };
  });
}

/**
 * Which tab should be active after `closing` (an iterable of paths) is
 * removed from `tabs`. Anything other than the active tab being closed
 * leaves the active tab alone. When it IS being closed: the nearest
 * survivor to its right, else the nearest to its left, else null —
 * "stay where you were in the strip", which is what people expect from
 * every tabbed editor. (Most-recently-used ordering is the other
 * common choice; it needs a history the store doesn't keep, and
 * neighbour-picking is predictable enough.)
 *
 * @param {string[]} tabs - strip order, BEFORE the close
 * @param {string|null} activePath
 * @param {Iterable<string>} closing
 * @returns {string|null}
 */
export function nextActiveAfterClose(tabs, activePath, closing) {
  const closingSet = new Set(closing);
  if (activePath == null || !closingSet.has(activePath)) return activePath;

  const idx = tabs.indexOf(activePath);
  if (idx === -1) {
    // The active path isn't in the strip. Not a state the workbench
    // produces (SET_ACTIVE_PATH always adds the tab), so this is a
    // defensive branch: fall back to the last survivor rather than
    // throwing on a state we didn't expect.
    const survivors = tabs.filter((p) => !closingSet.has(p));
    return survivors.length ? survivors[survivors.length - 1] : null;
  }
  for (let i = idx + 1; i < tabs.length; i += 1) {
    if (!closingSet.has(tabs[i])) return tabs[i];
  }
  for (let i = idx - 1; i >= 0; i -= 1) {
    if (!closingSet.has(tabs[i])) return tabs[i];
  }
  return null;
}

/**
 * Compares every open buffer against the server's file list (each
 * entry carries a monotonically increasing `version`, W1.1) and says
 * what to do about the ones that fell behind. This replaces W0.1/W2.2's
 * "the Pusher event named my ONE open file" check: with several tabs
 * open, a refresh has to look at all of them, and comparing versions
 * (instead of trusting an event payload) also covers a dropped Pusher
 * connection or a backgrounded tab with the same code path — one list
 * fetch, then only the files that actually changed get re-read.
 *
 * Rules, per open tab that has a loaded buffer:
 *  - server no longer has the file -> `drop` the tab, unless it has
 *    unsaved edits (then it stays; Save re-creates the file, and
 *    silently throwing the edits away is the one thing this must not do);
 *  - server version is ahead of the buffer's:
 *      not dirty -> `reload` (nothing to lose);
 *      dirty     -> `stale` (show the "Changed on server" banner) —
 *                   but not if it's already stale, or if the person
 *                   already chose "Keep mine" for this server version
 *                   (`dismissed[path]`), which would otherwise nag
 *                   again on every refresh until they save;
 *  - a path in `busy` (a save is in flight) is skipped: its own write
 *    is what put the server ahead, and saveSuccess reconciles it.
 *
 * @param {object} args
 * @param {string[]} args.tabs
 * @param {{[path: string]: {version?: number, dirty?: boolean, stale?: boolean}}} args.buffers
 * @param {{[path: string]: {version?: number}}} args.meta - FileProvider.list()'s result
 * @param {{[path: string]: number}} [args.dismissed] - server version the person waved off, per path
 * @param {Iterable<string>} [args.busy]
 * @returns {{reload: string[], stale: string[], drop: string[]}}
 */
export function planBufferSync({ tabs, buffers, meta, dismissed = {}, busy = [] }) {
  const busySet = new Set(busy);
  const plan = { reload: [], stale: [], drop: [] };

  for (const path of tabs) {
    const buffer = buffers[path];
    if (!buffer || busySet.has(path)) continue;

    const entry = meta[path];
    if (!entry) {
      if (!buffer.dirty) plan.drop.push(path);
      continue;
    }

    const serverVersion = entry.version ?? 0;
    if (serverVersion <= (buffer.version ?? 0)) continue;

    if (!buffer.dirty) {
      plan.reload.push(path);
    } else if (!buffer.stale && serverVersion > (dismissed[path] ?? 0)) {
      plan.stale.push(path);
    }
  }
  return plan;
}

/**
 * W2.5: which open buffers should have an autosave pending, and for
 * what text. Pure — EditorWorkbench.jsx runs it after every render and
 * keeps one debounce timer per returned path, restarting a path's timer
 * only when its `edited` text changed (so an unrelated re-render never
 * postpones somebody else's save).
 *
 * A buffer qualifies when it has unsaved edits and NONE of these hold:
 *  - it's `stale` (the server moved on underneath it — the banner asks
 *    the person first; saving now would just produce a conflict);
 *  - a save for it is already in flight (`busy`) — when that finishes
 *    the workbench re-plans, so edits typed during the round trip are
 *    picked up then rather than dropped;
 *  - it has an unresolved save conflict (`conflicts`) — Reload theirs /
 *    Keep mine is a decision only the person can make;
 *  - its last attempt FAILED for exactly this text (`failed[path]` is
 *    that text). Without this an offline person would retry every
 *    debounce period forever; with it, the next keystroke (different
 *    text) is what earns the next retry.
 *
 * @param {object} args
 * @param {string[]} args.tabs
 * @param {{[path: string]: {dirty?: boolean, stale?: boolean, edited?: string}}} args.buffers
 * @param {Iterable<string>} [args.busy]
 * @param {{[path: string]: unknown}} [args.conflicts]
 * @param {{[path: string]: string}} [args.failed]
 * @returns {{path: string, edited: string}[]}
 */
export function planAutosave({ tabs, buffers, busy = [], conflicts = {}, failed = {} }) {
  const busySet = new Set(busy);
  const has = (obj, key) => Object.prototype.hasOwnProperty.call(obj, key);
  const out = [];
  for (const path of tabs) {
    const buffer = buffers[path];
    if (!buffer || !buffer.dirty || buffer.stale) continue;
    if (busySet.has(path) || has(conflicts, path)) continue;
    if (has(failed, path) && failed[path] === buffer.edited) continue;
    out.push({ path, edited: buffer.edited });
  }
  return out;
}

/**
 * Collapses the per-tab flags the strip and explorer care about —
 * dirty (unsaved edits), stale (changed on the server underneath
 * them), loading (a tab whose file hasn't arrived yet) — into ONE
 * string. Why a string: every keystroke replaces the whole `buffers`
 * object in the editor store, so anything derived from it as an object
 * or Set is a fresh identity every keystroke and would re-render
 * memoized children (the explorer, the tab strip) on each one. A
 * string compares by value, so those children only re-render when a
 * flag actually flips (the dot appearing on the first keystroke, not
 * on all the rest).
 *
 * Format: one `path\tflags` line per tab, `flags` being any of `d` `s`
 * `l`. Workspace paths can't contain a tab or a newline (both fail
 * workspace_code_files._validate_file_path), so this is unambiguous.
 *
 * @param {string[]} tabs
 * @param {{[path: string]: {dirty?: boolean, stale?: boolean}}} buffers
 * @returns {string}
 */
export function encodeTabFlags(tabs, buffers) {
  return tabs
    .map((path) => {
      const b = buffers[path];
      const flags = !b ? "l" : `${b.dirty ? "d" : ""}${b.stale ? "s" : ""}`;
      return `${path}\t${flags}`;
    })
    .join("\n");
}

/** Inverse of encodeTabFlags(): `{[path]: {dirty, stale, loading}}`. */
export function decodeTabFlags(key) {
  const out = {};
  if (!key) return out;
  for (const line of key.split("\n")) {
    const i = line.indexOf("\t");
    const path = line.slice(0, i);
    const flags = line.slice(i + 1);
    out[path] = { dirty: flags.includes("d"), stale: flags.includes("s"), loading: flags.includes("l") };
  }
  return out;
}
