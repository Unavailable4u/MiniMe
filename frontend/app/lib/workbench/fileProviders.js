// frontend/app/lib/workbench/fileProviders.js — W2.2 (Build Workbench
// plan, decision D6). `FileProvider` interface and its Cloud
// implementation (workspace_code_files). Explorer/tabs/search/chips
// (W2.3+) talk to the active provider through this contract only, never
// to a route path directly.
//
// Patch B10 (M2C build plan, WP B10 — decision §O6): the browser editor
// works on cloud project files only. The W3.1 Local provider (a paired
// daemon folder, with its propose/confirm save flow and terminal
// helpers) was removed together with the Explorer's source switch, so
// Cloud is the one implementation of this contract. The contract below
// is unchanged apart from the capability flags nothing reads any more.
// The backend local_workspace routes and daemon/ stay until M2C Phase 5.
//
// Contract (plan §5, step W2.2):
//   id                          — "cloud"; for display/logging, never
//                                  branched on by a caller
//   capabilities                — { write, history, search, watch,
//                                    findings }
//   list()                      — Promise<{[path]: meta}>, content-free
//   read(path)                  — Promise<file>, content included
//   write(path, content, opts?) — Promise<file>; opts.baseVersion is
//                                  optional optimistic-concurrency
//                                  (W1.1); throws FileConflictError on
//                                  a 409
//   remove(path)                — Promise<{deleted_paths}>
//   move(fromPath, toPath)      — Promise<file[]>
//   mkdir(path)                 — Promise<file>
//   subscribe(onChange)         — onChange({filePaths, agent}) on every
//                                  code_file_updated for this workspace
//                                  (`agent` says who wrote: W8.2 only
//                                  announces the chat run's
//                                  write-back, see aiUpdates.js);
//                                  returns an unsubscribe function
//   findings()                  — W8.3b: Promise<object[]>, the scan/test
//                                  findings behind the Problems panel,
//                                  exactly as the server stores them
//                                  (problems.js's normalizeFindings()
//                                  cleans them up). Always callable;
//                                  resolves [] where
//                                  capabilities.findings is false.
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
import { extractFileUpdate } from "./aiUpdates";

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

// Patch B06 (M2C build plan, WP B06 — readable validation errors).
//
// FastAPI answers a request-validation failure (HTTP 422) with `detail` as
// an ARRAY of objects — [{loc: ["body", "file_path"], msg: "...", type:
// "..."}, ...] — not a string. This helper used to return `body.detail`
// as-is, every caller does `new Error(await parseErrorDetail(res))`, and
// `new Error(array)` stringifies to "[object Object]", which is what the
// Build tab showed the user. formatErrorDetail() below turns whatever
// `detail` is into ONE readable line:
//   - array   -> "file_path: message; other: message". A leading "body" or
//                "query" is dropped from each item's `loc` (it only says
//                where in the request the field lives) and the rest is
//                joined with "." — so a nested field reads
//                "files.0.path: Field required". A `loc` that is only
//                ["body"] (a whole-body error) renders the message alone.
//   - object  -> its `message`, else its `msg`, else compact JSON.
//   - string  -> unchanged.
//   - missing / empty (undefined, null, "", [], {}) -> "", and
//     parseErrorDetail() then falls back to "<status> <statusText>", the
//     pre-B06 fallback, so a response with nothing useful in it reads
//     exactly as before.
//
// Never throws (an error message must not itself become a second error):
// a non-JSON body, a body that isn't an object, and a `detail`
// JSON.stringify can't serialise (e.g. a BigInt) all end up at the
// fallback or skip just the unusable item.
//
// codeProposals.js carries a near-copy of parseErrorDetail() with the same
// bug; deliberately NOT touched by this patch (one WP = one patch) —
// tracked as a follow-up.
function isBlank(value) {
  return typeof value !== "string" || value.trim() === "";
}

function compactJson(value) {
  try {
    const out = JSON.stringify(value);
    return typeof out === "string" ? out : "";
  } catch {
    return "";
  }
}

// "body.file_path" -> "file_path": drop one leading "body"/"query", join
// the rest with ".". Anything that isn't an array (a malformed `loc`) is
// treated as no location rather than guessed at.
function formatLoc(loc) {
  if (!Array.isArray(loc)) return "";
  const parts = loc.filter((p) => typeof p === "string" || typeof p === "number");
  if (parts[0] === "body" || parts[0] === "query") parts.shift();
  return parts.join(".");
}

// One non-array detail value (a top-level object, or one array item that
// isn't a FastAPI {loc, msg} entry) -> a string, "" if nothing usable.
function formatDetailValue(value) {
  if (value == null) return "";
  if (typeof value === "string") return isBlank(value) ? "" : value;
  if (typeof value === "number") return Number.isFinite(value) ? String(value) : "";
  if (Array.isArray(value)) return value.length ? compactJson(value) : "";
  if (typeof value !== "object") return "";
  if (!isBlank(value.message)) return value.message;
  if (!isBlank(value.msg)) return value.msg;
  if (Object.keys(value).length === 0) return "";
  return compactJson(value);
}

function formatErrorDetail(detail) {
  if (!Array.isArray(detail)) return formatDetailValue(detail);
  const lines = [];
  for (const item of detail) {
    if (item && typeof item === "object" && !Array.isArray(item) && !isBlank(item.msg)) {
      const field = formatLoc(item.loc);
      lines.push(field ? `${field}: ${item.msg}` : item.msg);
    } else {
      const text = formatDetailValue(item);
      if (text) lines.push(text);
    }
  }
  return lines.join("; ");
}

export async function parseErrorDetail(res) {
  let fallback = "Request failed";
  try {
    fallback = `${res.status} ${res.statusText}`;
    const body = await res.json();
    return formatErrorDetail(body?.detail) || fallback;
  } catch {
    return fallback;
  }
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
      history: true,
      search: false, // W2.6
      watch: true,
      // W8.3b: the pipeline stores its test/scan findings per cloud file
      // (eo/code_findings.py).
      findings: true,
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
     * W8.3b: GET .../code/findings (api/routes/code.py) — `{findings:
     * [{path, line, severity, message, source, file_version}]}`, empty
     * when nothing has been found. Resolves to the bare array and leaves
     * cleaning it up (bad rows, sorting, staleness) to problems.js, the
     * same split list()/buildFileTree() already has.
     *
     * No subscription of its own, on purpose: the backend stores a run's
     * findings BEFORE it emits code_file_updated, so refetching from the
     * one subscribe() callback below is enough. A second subscription
     * would cost more than it adds — pusher-js hands every
     * pusher.subscribe(name) the SAME channel object, and the cleanup
     * below calls pusher.unsubscribe(name), which removes that channel
     * outright. Whichever subscriber was torn down first would take the
     * other's events with it.
     */
    async findings() {
      const res = await fetch(`${base}/findings`, { headers: await authHeaders() });
      if (!res.ok) throw new Error(await parseErrorDetail(res));
      const body = await res.json();
      return Array.isArray(body?.findings) ? body.findings : [];
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
        // The message is the whole envelope — the paths are under
        // `payload` (see aiUpdates.js's header). Reading `data.file_paths`
        // directly, as this used to, always came back empty.
        onChange(extractFileUpdate(data));
      };
      channel.bind_global(handler);

      return () => {
        channel.unbind_global(handler);
        pusher.unsubscribe(channelName);
      };
    },
  };
}
