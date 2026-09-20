// frontend/app/lib/workbench/fileProviders.js — W2.2 (Build Workbench
// plan, decision D6). `FileProvider` interface with one implementation
// today (Cloud); `LocalFileProvider` follows in W3.1 once the daemon
// side of things exists, behind this exact same shape. Explorer/tabs/
// search/chips (W2.3+) talk to whichever provider is active through
// this contract only, never to a route path directly — that's what
// makes "merge Local Files into Build, daemon later" a real,
// incremental change instead of a rewrite (plan §1.3/§2 D6).
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
