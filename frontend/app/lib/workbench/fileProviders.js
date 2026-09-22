// frontend/app/lib/workbench/fileProviders.js — W2.2 (Build Workbench
// plan, decision D6). `FileProvider` interface with two implementations:
// Cloud (workspace_code_files) and, as of W3.1, Local (a paired daemon
// folder) — both behind this exact same shape. Explorer/tabs/search/
// chips (W2.3+) talk to whichever provider is active through this
// contract only, never to a route path directly — that's what makes
// "merge Local Files into Build, daemon later" a real, incremental
// change instead of a rewrite (plan §1.3/§2 D6).
//
// Contract (plan §5, step W2.2):
//   id                          — "cloud" | "local"; for display/logging,
//                                  never branched on by a caller
//   capabilities                — { write, writeNeedsConfirm, history,
//                                    search, terminal, watch }
//   list()                      — Promise<{[path]: meta}>, content-free
//   read(path)                  — Promise<file>, content included
//   write(path, content, opts?) — Promise<file>; opts.baseVersion is
//                                  optional optimistic-concurrency
//                                  (W1.1); throws FileConflictError on
//                                  a 409
//   remove(path)                — Promise<{deleted_paths}>
//   move(fromPath, toPath)      — Promise<file[]>
//   mkdir(path)                 — Promise<file>
//   subscribe(onChange)         — onChange({filePaths}) on every
//                                  code_file_updated for this workspace;
//                                  returns an unsubscribe function
//
// `history(path)` / `restore(path, version)` are included too, even
// though W2.2's own CodeView doesn't call them yet — they're straight
// wrappers around routes W1.1 already shipped
// (GET/POST .../code/files/{path}/history|restore), and
// `capabilities.history: true` should actually mean something rather
// than being a flag nothing can act on until W2.6 gets here.
//
// CloudFileProvider wraps the existing `/api/workspaces/{id}/code/...`
// routes (api/routes/code.py) exactly as CodeView's own fetch() calls
// already did pre-W2.2, plus the W0.1 Pusher subscription — previously
// inlined directly in BuildTab.jsx's CodeView (see that component's own
// git history for the pre-W2.2 shape) — folded in here so every
// consumer of this provider gets live refresh for free instead of each
// reinventing the channel-name/bind_global/unbind_global dance.
//
// LocalFileProvider (W3.1) wraps api/routes/local_workspace.py's
// list_dir/read_file/status routes instead, over a PAIRED local folder
// via the daemon. Three real differences from Cloud, all load-bearing:
//   - list() has no flat listing on the server to ask for (the daemon's
//     list_dir only returns one directory's own children) — see this
//     provider's own comment for the bounded, ignore-listed walk that
//     turns repeated list_dir calls into the same flat {path: meta}
//     shape buildFileTree() already expects, unchanged, from Cloud.
//   - capabilities.writeNeedsConfirm is true: a person's write needs a
//     human OK on the other end before it touches their disk. write()
//     itself is deliberately NOT wired to that flow here — a generic
//     caller that didn't check writeNeedsConfirm first should get a
//     loud error, not a silent no-op or a surprise unconfirmed write.
//     EditorWorkbench's save flow calls propose()/confirm() directly
//     instead — see saveFile() there and this provider's own
//     propose()/confirm()/deny() below.
//   - capabilities.history/watch are both false: there's no version
//     table for local files (only workspace_code_files has one) and no
//     push channel that knows when something outside this tab changed
//     a file on disk.
"use client";
import { authHeaders } from "../../context/SessionContext";
import { getPusherClient } from "../pusherClient";

/**
 * Thrown by write() when a `baseVersion` was supplied and the server's
 * PUT rejected it with a 409 — see api/routes/code.py's put_code_file()
 * docstring. `current` is the server's present file shape (content,
 * version, updated_at/_by — the same shape every other method here
 * resolves to), so a caller can offer Reload / Keep mine / Compare
 * (W2.5) without a second round trip.
 */
export class FileConflictError extends Error {
  constructor(current) {
    super("File changed on the server since it was last read");
    this.name = "FileConflictError";
    this.current = current;
  }
}

async function parseErrorDetail(res) {
  const body = await res.json().catch(() => null);
  return body?.detail || `${res.status} ${res.statusText}`;
}

/**
 * @param {{workspaceId: string, apiUrl: string}} config
 * @returns a FileProvider backed by workspace_code_files (cloud storage)
 */
export function createCloudFileProvider({ workspaceId, apiUrl }) {
  const base = `${apiUrl}/api/workspaces/${workspaceId}/code`;

  return {
    id: "cloud",
    capabilities: {
      write: true,
      writeNeedsConfirm: false,
      history: true,
      search: false, // W2.6
      terminal: false, // local-only, see W3.1
      watch: true,
    },

    async list() {
      const res = await fetch(`${base}/files`, { headers: await authHeaders() });
      if (!res.ok) throw new Error(await parseErrorDetail(res));
      return res.json();
    },

    async read(path) {
      // `{file_path:path}` on the backend takes the raw slashes as-is —
      // no encoding needed, file_path is already shape-validated
      // server-side (workspace_code_files._validate_file_path), same
      // as every pre-W2.2 call site.
      const res = await fetch(`${base}/files/${path}`, { headers: await authHeaders() });
      if (!res.ok) throw new Error(await parseErrorDetail(res));
      return res.json();
    },

    async write(path, content, { baseVersion, language } = {}) {
      // No base_version sent unless the caller passes one — None keeps
      // the pre-W1.1 blind-overwrite behavior (see
      // workspace_code_files.write_file()'s own docstring). W2.2's
      // CodeView doesn't pass one yet (W2.5 is what starts doing that);
      // this provider is already ready for it.
      const res = await fetch(`${base}/files/${path}`, {
        method: "PUT",
        headers: await authHeaders({ json: true }),
        body: JSON.stringify({ content, language, base_version: baseVersion ?? null }),
      });
      if (res.status === 409) {
        const body = await res.json().catch(() => null);
        throw new FileConflictError(body?.detail || null);
      }
      if (!res.ok) throw new Error(await parseErrorDetail(res));
      return res.json();
    },

    async remove(path) {
      const res = await fetch(`${base}/files/${path}`, {
        method: "DELETE",
        headers: await authHeaders(),
      });
      if (!res.ok) throw new Error(await parseErrorDetail(res));
      return res.json();
    },

    async move(fromPath, toPath) {
      const res = await fetch(`${base}/move`, {
        method: "POST",
        headers: await authHeaders({ json: true }),
        body: JSON.stringify({ from: fromPath, to: toPath }),
      });
      if (!res.ok) throw new Error(await parseErrorDetail(res));
      return res.json();
    },

    async mkdir(path) {
      const res = await fetch(`${base}/folders`, {
        method: "POST",
        headers: await authHeaders({ json: true }),
        body: JSON.stringify({ path }),
      });
      if (!res.ok) throw new Error(await parseErrorDetail(res));
      return res.json();
    },

    async history(path) {
      const res = await fetch(`${base}/files/${path}/history`, { headers: await authHeaders() });
      if (!res.ok) throw new Error(await parseErrorDetail(res));
      return res.json();
    },

    async restore(path, version) {
      const res = await fetch(`${base}/files/${path}/restore`, {
        method: "POST",
        headers: await authHeaders({ json: true }),
        body: JSON.stringify({ version }),
      });
      if (!res.ok) throw new Error(await parseErrorDetail(res));
      return res.json();
    },

    /**
     * W0.1's Pusher subscription, relocated here from BuildTab.jsx's
     * CodeView so it lives with the provider whose data it invalidates
     * rather than the one component that happened to render it first —
     * whatever ends up showing the file tree in W2.3 gets live refresh
     * for free. Channel name, event name and payload shape are all
     * unchanged: `workspace-${id}` (sanitised the same way PlanTab.jsx's
     * own subscription is), `code_file_updated`, `{file_path?,
     * file_paths?}`.
     */
    subscribe(onChange) {
      const pusher = getPusherClient();
      if (!pusher) return () => {}; // Pusher env vars not set — live refresh disabled

      const channelName = `workspace-${workspaceId.replace(/[^A-Za-z0-9_=@,.;-]/g, "-")}`;
      const channel = pusher.subscribe(channelName);
      const handler = (eventType, data) => {
        if (eventType !== "code_file_updated") return;
        const filePaths = data?.file_paths?.length
          ? data.file_paths
          : data?.file_path
          ? [data.file_path]
          : [];
        onChange({ filePaths });
      };
      channel.bind_global(handler);

      return () => {
        channel.unbind_global(handler);
        pusher.unsubscribe(channelName);
      };
    },
  };
}

// ---------------------------------------------------------------------
// W3.1 — LocalFileProvider
// ---------------------------------------------------------------------

// Skipped outright while walking a paired folder — the same defaults
// most local dev tools already ignore by convention. A paired folder is
// expected to be a real project (see daemon/README.md), not an
// arbitrary drive, but these are exactly the directories that turn a
// "list everything" walk into thousands of pointless round trips over
// the daemon's websocket if someone pairs a repo root that still has
// them checked out.
const LOCAL_IGNORE_DIR_NAMES = new Set([
  "node_modules", ".git", ".next", ".venv", "venv", "__pycache__",
  "dist", "build", ".turbo", ".cache", "target", ".idea", ".vscode",
]);

// Safety valve, not a expected ceiling: a normal paired project won't
// come close. Bounds the damage from a folder paired one level too high
// (a whole drive, a monorepo with no ignore-listed vendor dir) to "the
// explorer shows a truncated listing", not "hundreds of list_dir round
// trips before the tab is usable".
const LOCAL_LIST_MAX_ENTRIES = 5000;

/**
 * @param {{workspaceId: string, apiUrl: string}} config
 * @returns a FileProvider backed by a paired local folder, over the
 *   daemon (eo/local_workspace_tools.py via api/routes/local_workspace.py)
 */
export function createLocalFileProvider({ workspaceId, apiUrl }) {
  const base = `${apiUrl}/api/workspaces/${workspaceId}/local`;

  async function apiPost(path, body) {
    const res = await fetch(`${base}${path}`, {
      method: "POST",
      headers: await authHeaders({ json: true }),
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(await parseErrorDetail(res));
    return res.json();
  }

  return {
    id: "local",
    capabilities: {
      // `write` stays false — permanently, not just until some later
      // patch. It is deliberately NOT flipped now that Save itself is
      // wired (EditorWorkbench.jsx's saveFile()): Explorer.jsx keys its
      // New/Rename/Delete/Duplicate affordances off capabilities.write
      // (see canModify there), and remove()/move()/mkdir() below still
      // throw — those file-tree operations were never in this plan's
      // scope for Local (only Cloud's workspace_code_files got them, in
      // W1.2), so lighting up buttons that always fail would be worse
      // than leaving them hidden. Editing a file's CONTENT is a
      // different question from changing the tree, and that one IS
      // wired — through writeNeedsConfirm below, not this flag.
      // EditorWorkbench reads `write || writeNeedsConfirm` wherever the
      // question is "can this buffer be typed into" (its readOnly prop)
      // and reads `write` alone wherever the question is "can the tree
      // itself change" (Explorer's canModify) — see that component's
      // own comment on the split.
      write: false,
      // Save becomes "Propose write": EditorWorkbench's saveFile() calls
      // propose("write_file", {path, content}) below instead of write()
      // (which keeps throwing — see its own comment), and a human
      // confirms on PendingActionBar before anything touches disk. No
      // optimistic-concurrency story here the way Cloud's base_version
      // gives one: the daemon keeps no version table for local files,
      // so a confirmed write simply overwrites whatever is on disk at
      // that moment, the same as any other local edit would.
      writeNeedsConfirm: true,
      history: false, // no version table for local files — HistoryPanel shows a fitting empty state instead of calling this
      search: false, // unused: Project Search (W2.6) works over any provider's read(), regardless of this flag — see projectSearch.js's own header
      terminal: true,
      watch: false, // no push channel for changes made outside this tab
    },

    /** Cheap poll target — see api/routes/local_workspace.py's own docstring. Not part of the base FileProvider contract; hooks/useDaemonStatus.js is the intended caller. */
    async status() {
      const res = await fetch(`${base}/status`, { headers: await authHeaders() });
      if (!res.ok) return { live: false };
      return res.json();
    },

    /**
     * The daemon has no flat "list everything" call, only list_dir for
     * one directory at a time — so this walks the tree breadth-first,
     * skipping LOCAL_IGNORE_DIR_NAMES and stopping at
     * LOCAL_LIST_MAX_ENTRIES, and hands back the same flat
     * `{path: {file_path, size}}` shape Cloud's list() does. That's a
     * real trade-off (this fetches the whole tree up front rather than
     * lazily per folder, unlike the daemon's own list_dir), made on
     * purpose so Explorer.jsx/fileTree.js — buildFileTree(), the
     * expand/collapse tree, the filter box — need ZERO changes to work
     * with Local: they only ever see a flat filesMeta, exactly like
     * they already do for Cloud. An unreadable subdirectory (permission
     * error, deleted mid-walk) is skipped rather than failing the whole
     * listing; an unreadable ROOT is not — that's "no daemon" or a bad
     * pairing, and callers (EditorWorkbench) are expected to check
     * status() before ever calling this rather than routing that
     * failure through here as a normal empty listing.
     */
    async list() {
      const root = await apiPost("/list_dir", { path: "." });
      const filesMeta = {};
      const queue = (root.entries || []).map((entry) => ({ dir: ".", entry }));
      let count = 0;

      while (queue.length > 0 && count < LOCAL_LIST_MAX_ENTRIES) {
        const { dir, entry } = queue.shift();
        if (LOCAL_IGNORE_DIR_NAMES.has(entry.name)) continue;
        const path = dir === "." ? entry.name : `${dir}/${entry.name}`;
        if (entry.type === "dir") {
          try {
            const data = await apiPost("/list_dir", { path });
            for (const child of data.entries || []) queue.push({ dir: path, entry: child });
          } catch {
            // Skipped, not fatal — see this method's own header.
          }
        } else {
          // No `version` field: every local file compares equal to
          // itself on a manual refresh (see planBufferSync()'s callers)
          // rather than ever looking "changed on the server" — a real
          // consequence of `watch: false`, not an oversight. No
          // `language` either: CodeEditor's own loadLanguageExtension()
          // already infers syntax highlighting from the path/extension,
          // not from this field — it only ever fed the status bar's
          // label, which falls back to "plain text" gracefully.
          filesMeta[path] = { file_path: path, size: entry.size ?? null, version: 0 };
          count += 1;
        }
      }
      return filesMeta;
    },

    async read(path) {
      const data = await apiPost("/read_file", { path });
      return { file_path: data.path, content: data.content, truncated: !!data.truncated };
    },

    async write() {
      // See capabilities.writeNeedsConfirm's own comment above — a
      // generic caller that writes without checking it first should
      // fail loudly, not silently propose nothing and return as if it
      // worked.
      throw new Error("Local files need confirmation before writing — this provider isn't wired to Save yet");
    },

    async remove() {
      throw new Error("Deleting local files isn't wired into the explorer yet");
    },

    async move() {
      throw new Error("Moving local files isn't wired into the explorer yet");
    },

    async mkdir() {
      throw new Error("Creating local folders isn't wired into the explorer yet");
    },

    /**
     * Not part of the base FileProvider contract — TerminalPanel.jsx
     * and EditorWorkbench.jsx's own save flow (saveFile(), for a
     * writeNeedsConfirm provider) both call these directly, the same
     * propose -> human clicks Confirm/Deny on PendingActionBar ->
     * daemon runs it round trip components/TerminalPanel.jsx already
     * uses for execute_command.
     */
    async propose(tool, params) {
      return apiPost("/propose", { tool, params });
    },
    async confirm(actionId) {
      return apiPost("/confirm", { action_id: actionId });
    },
    async deny(actionId) {
      return apiPost("/deny", { action_id: actionId });
    },

    subscribe() {
      return () => {}; // watch: false — see this provider's own header
    },
  };
}
