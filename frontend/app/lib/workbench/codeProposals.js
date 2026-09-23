// frontend/app/lib/workbench/codeProposals.js — W5.1b (Build Workbench
// plan, step 197): the frontend half of W5.1's proposal endpoints —
// see backend/eo/code_proposals.py's own module docstring for the
// full lifecycle this wraps (create -> pending -> resolve ->
// accepted/rejected/partial/stale/failed).
//
// Plain exported async functions, not a stateful provider — unlike
// fileProviders.js's createCloudFileProvider() (constructed once per
// workbench mount, bound to one workspace's {workspaceId, apiUrl} for
// the whole Explorer/tabs/search/chips surface), a proposal is
// something ONE chat send kicks off at a time from
// WorkspaceChatPanel.jsx's Edit-mode composer — there's no shared
// per-workspace state here worth wrapping in a factory closure, so
// every function below just takes `apiUrl`/`workspaceId` as plain
// arguments, same shape as SessionContext.jsx's/
// WorkspaceDockContext.jsx's own `${API_URL}/api/...` call sites.
//
// What this step wires up, concretely: WorkspaceChatPanel.jsx's
// Ask|Edit toggle (W4.2) can now actually select "Edit" (previously
// disabled, "Coming in W5"), and a chip-bearing Edit-mode send calls
// createProposal() below instead of the Ask-mode chat path. The
// result renders as a plain, read-only list in that panel — no
// Keep/Undo (W5.3), no cross-session pending tray (W5.4). That's a
// deliberate scope line, not an oversight: this step's job is making
// the real round trip (create -> live status update via Pusher)
// reachable from the UI at all, not building the merge view on top of
// it.
"use client";
import { authHeaders } from "../../context/SessionContext";
import { getPusherClient } from "../pusherClient";

/**
 * Thrown by resolveProposal() on a 409 — same role FileConflictError
 * plays in fileProviders.js, just carrying a LIST of conflicting files
 * (eo/code_proposals.py's ProposalStaleError.stale_files, one entry
 * per file that moved since this proposal was created) instead of
 * one, since a proposal can touch several files at once. Nothing in
 * this step's UI catches this yet (there's no resolve() call site
 * until W5.3's Keep/Undo view exists to send decisions) — exported
 * now so that step doesn't have to invent it later.
 */
export class ProposalStaleError extends Error {
  constructor(staleFiles) {
    super("One or more files changed on the server since this proposal was created");
    this.name = "ProposalStaleError";
    this.staleFiles = staleFiles || [];
  }
}

async function parseErrorDetail(res) {
  const body = await res.json().catch(() => null);
  return body?.detail || `${res.status} ${res.statusText}`;
}

// The plan's own ref shape (migrations/0010_code_proposals.sql's own
// comment on the `refs` column: `{id, kind, path, fromLine, toLine,
// snippet, hash, provider, ...}`) — trimmed down from
// codeContext.js's full in-memory ref (which also carries `from`/`to`
// character offsets and a client-only `truncated` flag CM6 needs for
// live position-tracking) to exactly the fields a proposal on the
// server ever reads or displays. Mirrors sendCodeChatMessage's own
// `trimmedRefs` in WorkspaceChatPanel.jsx, which does the same
// trim for the Ask-mode persisted-message path, just keeping
// `snippet`/`hash` here too since eo/code_proposals.py's
// _paths_from_refs() only READS kind/path but the row still stores
// the whole ref for W5.4's proposal card to show what was selected.
function toProposalRef(ref) {
  return {
    id: ref.id,
    kind: ref.kind,
    path: ref.path,
    fromLine: ref.fromLine ?? null,
    toLine: ref.toLine ?? null,
    snippet: ref.snippet ?? null,
    hash: ref.hash ?? null,
    provider: ref.provider ?? null,
  };
}

/**
 * POST .../code/proposals — see api/routes/code_edit.py's
 * create_code_proposal() docstring: this call is synchronous end to
 * end (today's stub generator runs inline, W5.2's real agent call
 * will too) and returns the FULLY STORED proposal, status included —
 * no polling needed for this step's own round trip.
 *
 * `refs`: codeContext.js's full ref objects — trimmed to
 * toProposalRef()'s shape before the request body is built, so this
 * function's caller never has to remember to do that trim itself.
 * Throws (via parseErrorDetail) on a 400 (empty instruction, no refs,
 * a folder ref, an unsupported ref kind) — see create_proposal()'s
 * own docstring for that closed set; a generation FAILURE is NOT an
 * error here, it's a normal 200 with `status: "failed"` on the
 * returned proposal (same reasoning that function's docstring gives),
 * so callers should check `.status`, not just catch.
 */
export async function createProposal(apiUrl, workspaceId, { instruction, refs, sessionId }) {
  const res = await fetch(`${apiUrl}/api/workspaces/${workspaceId}/code/proposals`, {
    method: "POST",
    headers: await authHeaders({ json: true }),
    body: JSON.stringify({
      instruction,
      refs: (refs || []).map(toProposalRef),
      session_id: sessionId ?? null,
    }),
  });
  if (!res.ok) throw new Error(await parseErrorDetail(res));
  return res.json();
}

/**
 * GET .../code/proposals[?status=pending] — most recent first. Not
 * called anywhere yet in this step (there's no pending tray until
 * W5.4), but a from-scratch fetch is exactly what that tray's own
 * mount-time load will need, same "the plumbing exists before the
 * component that uses it" shape createProposal()'s Pusher-ready
 * subscribeToProposalEvents() below already takes.
 */
export async function listProposals(apiUrl, workspaceId, { status } = {}) {
  const qs = status ? `?status=${encodeURIComponent(status)}` : "";
  const res = await fetch(`${apiUrl}/api/workspaces/${workspaceId}/code/proposals${qs}`, {
    headers: await authHeaders(),
  });
  if (!res.ok) throw new Error(await parseErrorDetail(res));
  return res.json();
}

/**
 * GET .../code/proposals/{id} — full shape, files[] (original +
 * proposed per file) included. What W5.3's merge view will fetch once
 * a proposal card is actually clicked; not called anywhere yet.
 */
export async function getProposal(apiUrl, workspaceId, proposalId) {
  const res = await fetch(
    `${apiUrl}/api/workspaces/${workspaceId}/code/proposals/${proposalId}`,
    { headers: await authHeaders() }
  );
  if (!res.ok) throw new Error(await parseErrorDetail(res));
  return res.json();
}

/**
 * POST .../code/proposals/{id}/resolve — `decisions`:
 * [{path, decision: "keep"|"undo", finalContent?}], mirroring
 * eo/code_proposals.py's resolve_proposal() own `decisions` shape
 * (camelCase here, converted to the wire's `final_content` below —
 * every other field name already matches). Not called anywhere yet;
 * W5.3's Keep/Undo view is the intended caller. Throws
 * ProposalStaleError on a 409, same shape/reasoning as
 * fileProviders.js's write() throwing FileConflictError on its own
 * 409.
 */
export async function resolveProposal(apiUrl, workspaceId, proposalId, decisions) {
  const res = await fetch(
    `${apiUrl}/api/workspaces/${workspaceId}/code/proposals/${proposalId}/resolve`,
    {
      method: "POST",
      headers: await authHeaders({ json: true }),
      body: JSON.stringify({
        files: (decisions || []).map((d) => ({
          path: d.path,
          decision: d.decision,
          final_content: d.finalContent ?? null,
        })),
      }),
    }
  );
  if (res.status === 409) {
    const body = await res.json().catch(() => null);
    throw new ProposalStaleError(body?.detail?.stale_files || []);
  }
  if (!res.ok) throw new Error(await parseErrorDetail(res));
  return res.json();
}

/**
 * relay/emitter.py's CODE_PROPOSAL_READY / CODE_PROPOSAL_RESOLVED, on
 * the same `workspace-${id}` channel fileProviders.js's
 * CloudFileProvider.subscribe() already binds to for
 * `code_file_updated` — same sanitization, same bind_global +
 * eventType filter shape, so a second subscriber on this channel
 * (this one) behaves identically to the first. Returns an unsubscribe
 * function, same contract as that provider's subscribe().
 *
 * Both payloads are `{workspace_id, proposal_id, status}` only (see
 * that module's own comment on why — never file content), which is
 * already everything `onReady`/`onResolved` need: WorkspaceChatPanel.jsx's
 * own read-only proposal list (this step) matches on `proposal_id`
 * against what createProposal() already returned it synchronously, so
 * a resolution from ANOTHER tab/session (the only case this
 * subscription actually adds anything for, today — this tab's own
 * create already has the row) still flips that entry's status live.
 * `onReady` is included for symmetry / for W5.4's pending tray to use
 * later; nothing in this step passes one.
 */
export function subscribeToProposalEvents(workspaceId, { onReady, onResolved } = {}) {
  const pusher = getPusherClient();
  if (!pusher) return () => {}; // Pusher env vars not set — live updates disabled

  const channelName = `workspace-${workspaceId.replace(/[^A-Za-z0-9_=@,.;-]/g, "-")}`;
  const channel = pusher.subscribe(channelName);
  const handler = (eventType, data) => {
    if (eventType === "code_proposal_ready") onReady?.(data);
    else if (eventType === "code_proposal_resolved") onResolved?.(data);
  };
  channel.bind_global(handler);

  return () => {
    channel.unbind_global(handler);
    pusher.unsubscribe(channelName);
  };
}
