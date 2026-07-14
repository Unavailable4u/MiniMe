"use client";
import { useState, useEffect, useRef } from "react";
import { useSession } from "../context/SessionContext";
import { useAuth } from "../context/AuthContext";
import { Pencil, Check, X, FolderMinus, Trash2, UserPlus, LogOut, ShieldAlert, Eye, EyeOff, Download, Upload } from "lucide-react";
import ConfirmDialog from "./ConfirmDialog";

// Part 8.9: mirrors eo/chat_workspace.py's five-tier role model exactly —
// viewer < editor < moderator < partner <= owner. Kept as a local const
// rather than fetched from the server since it's a fixed hierarchy, same
// as the backend's own _ROLE_RANK; if that ever changes server-side this
// needs to change too, but a role hierarchy isn't the kind of thing that
// changes without a deliberate, coordinated frontend update anyway.
const ROLE_RANK = { viewer: 0, editor: 1, moderator: 2, partner: 3, owner: 3 };
const rank = (r) => ROLE_RANK[r] ?? -1;
const ASSIGNABLE_ROLES = ["viewer", "editor", "moderator", "partner"];

export default function ManageWorkspaceModal({ workspace, allChats, onClose }) {
  const {
    renameWorkspace, removeWorkspaceChat, deleteWorkspace, workspaces,
    exportWorkspace, importWorkspace,   // NEW — Part 8.7
    fetchWorkspaceMembers, addWorkspaceMember, updateWorkspaceMemberRole,
    removeWorkspaceMember, leaveWorkspaceMembership, forceRemoveOwner,
    fetchWorkspaceVotes, castWorkspaceVote,
    setWorkspaceAttribution, setMemberAttributionGrant,
  } = useSession();
  const { user } = useAuth();

  // Always read the freshest copy from context state — actions below
  // (role changes, votes resolving, leaving) can change owner_id,
  // is_joint, or show_attribution out from under a stale prop.
  const liveWorkspace = workspaces.find((w) => w.id === workspace.id) || workspace;

  const [name, setName] = useState(workspace.name);
  const [editingName, setEditingName] = useState(false);
  const [pendingRemove, setPendingRemove] = useState(null); // { chat, deleteChat }
  const [pendingDeleteWs, setPendingDeleteWs] = useState(false);

  // NEW — Part 8.7: per-workspace backup/restore.
  const [exportBusy, setExportBusy] = useState(false);
  const [importBusy, setImportBusy] = useState(false);
  const fileInputRef = useRef(null);

  async function handleExport() {
    setExportBusy(true);
    setError(null);
    try {
      const manifest = await exportWorkspace(workspace.id);
      const blob = new Blob([JSON.stringify(manifest, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${(liveWorkspace.name || "workspace").replace(/[^a-z0-9]+/gi, "-").toLowerCase()}-backup.json`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setExportBusy(false);
    }
  }

  async function handleImportFile(e) {
    const file = e.target.files?.[0];
    e.target.value = ""; // allow re-selecting the same file next time
    if (!file) return;
    setImportBusy(true);
    setError(null);
    try {
      const text = await file.text();
      const manifest = JSON.parse(text);
      await importWorkspace(workspace.id, manifest);
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setImportBusy(false);
    }
  }

  const [members, setMembers] = useState([]);
  const [membersLoading, setMembersLoading] = useState(true);
  const [votes, setVotes] = useState(null);
  const [error, setError] = useState(null);

  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteRole, setInviteRole] = useState("viewer");
  const [inviteBusy, setInviteBusy] = useState(false);

  const [pendingRemoveMember, setPendingRemoveMember] = useState(null);
  const [pendingForceRemoveOwner, setPendingForceRemoveOwner] = useState(false);
  const [pendingLeave, setPendingLeave] = useState(false);
  const [successorId, setSuccessorId] = useState("");

  const chatMembers = allChats.filter((c) => (liveWorkspace.chat_ids || workspace.chat_ids).includes(c.id));

  async function loadMembers() {
    try {
      const m = await fetchWorkspaceMembers(workspace.id);
      setMembers(m);
      return m;
    } catch (e) {
      setError(String(e.message || e));
      return [];
    }
  }

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setMembersLoading(true);
      await loadMembers();
      if (!cancelled) setMembersLoading(false);
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspace.id]);

  useEffect(() => {
    if (!liveWorkspace.is_joint) { setVotes(null); return; }
    let cancelled = false;
    fetchWorkspaceVotes(workspace.id).then((v) => { if (!cancelled) setVotes(v); }).catch(() => {});
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspace.id, liveWorkspace.is_joint]);

  const isOwner = liveWorkspace.owner_id === user?.id;
  const myMember = members.find((m) => m.user_id === user?.id);
  const myRole = isOwner ? "owner" : (myMember ? myMember.role : null);

  const canManageMembership = rank(myRole) >= rank("moderator");
  const canManagePartners = myRole === "owner" || myRole === "partner";
  const canForceRemoveOwner = myRole === "partner"; // owner can't force-remove themself — that's leave/successor
  const canVote = myRole === "partner" && liveWorkspace.is_joint;
  const canToggleAttribution =
    myRole === "owner" || myRole === "partner" || (myRole === "moderator" && myMember?.can_toggle_attribution);
  const canGrantAttribution = myRole === "owner" || myRole === "partner";

  const assignableRolesForMe = canManagePartners ? ASSIGNABLE_ROLES : ASSIGNABLE_ROLES.filter((r) => r !== "partner");
  const eligiblePartners = members.filter((m) => m.role === "partner" && m.user_id !== user?.id);

  function canChangeRoleFor(m) {
    if (!canManageMembership) return false;
    if (m.role === "partner" && !canManagePartners) return false;
    return true;
  }

  async function saveName() {
    if (name.trim() && name.trim() !== workspace.name) await renameWorkspace(workspace.id, name.trim());
    setEditingName(false);
  }

  async function confirmRemove() {
    const { chat, deleteChat } = pendingRemove;
    await removeWorkspaceChat(workspace.id, chat.id, deleteChat);
    setPendingRemove(null);
  }

  async function confirmDeleteWorkspace() {
    await deleteWorkspace(workspace.id);
    setPendingDeleteWs(false);
    onClose();
  }

  async function handleInvite(e) {
    e.preventDefault();
    if (!inviteEmail.trim() || inviteBusy) return;
    setError(null);
    setInviteBusy(true);
    try {
      await addWorkspaceMember(workspace.id, inviteEmail.trim(), inviteRole);
      setInviteEmail("");
      setInviteRole("viewer");
      await loadMembers();
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setInviteBusy(false);
    }
  }

  async function handleRoleChange(m, newRole) {
    if (newRole === m.role) return;
    setError(null);
    try {
      await updateWorkspaceMemberRole(workspace.id, m.user_id, newRole);
      await loadMembers();
    } catch (e) {
      setError(String(e.message || e));
    }
  }

  async function confirmRemoveMember() {
    if (!pendingRemoveMember) return;
    setError(null);
    try {
      await removeWorkspaceMember(workspace.id, pendingRemoveMember.user_id);
      await loadMembers();
    } catch (e) {
      setError(String(e.message || e));
    }
    setPendingRemoveMember(null);
  }

  async function confirmForceRemoveOwner() {
    setError(null);
    try {
      await forceRemoveOwner(workspace.id);
      await loadMembers();
    } catch (e) {
      setError(String(e.message || e));
    }
    setPendingForceRemoveOwner(false);
  }

  async function confirmLeave() {
    setError(null);
    try {
      await leaveWorkspaceMembership(workspace.id, isOwner ? (successorId || null) : null);
      onClose();
    } catch (e) {
      setError(String(e.message || e));
      setPendingLeave(false);
    }
  }

  async function handleVote(target) {
    setError(null);
    try {
      const result = await castWorkspaceVote(workspace.id, target);
      setVotes(result);
      await loadMembers(); // a resolved vote removes the winner's member row
    } catch (e) {
      setError(String(e.message || e));
    }
  }

  async function handleToggleAttribution() {
    setError(null);
    try {
      await setWorkspaceAttribution(workspace.id, !liveWorkspace.show_attribution);
    } catch (e) {
      setError(String(e.message || e));
    }
  }

  async function handleToggleGrant(m) {
    setError(null);
    try {
      await setMemberAttributionGrant(workspace.id, m.user_id, !m.can_toggle_attribution);
      await loadMembers();
    } catch (e) {
      setError(String(e.message || e));
    }
  }

  function shortId(id) {
    return id && id.length > 14 ? `${id.slice(0, 6)}…${id.slice(-4)}` : id;
  }
  function displayName(m) {
    if (!m) return "";
    return m.name || m.email || shortId(m.user_id);
  }

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onClick={onClose}>
      <div
        className="bg-[var(--neutral-900)] border border-[var(--neutral-700)] rounded-lg p-4 w-96 max-h-[80vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-1 mb-3">
          {editingName ? (
            <>
              <input
                autoFocus
                value={name}
                onChange={(e) => setName(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && saveName()}
                className="flex-1 bg-[var(--neutral-950)] border border-[var(--neutral-700)] rounded px-1.5 py-0.5 text-sm outline-none"
              />
              <button onClick={saveName}><Check size={14} className="text-green-400" /></button>
              <button onClick={() => { setName(workspace.name); setEditingName(false); }}><X size={14} className="text-[var(--neutral-500)]" /></button>
            </>
          ) : (
            <>
              <h3 className="text-sm font-medium text-[var(--neutral-200)] flex-1 truncate">{workspace.name}</h3>
              <button onClick={() => setEditingName(true)} title="Rename project">
                <Pencil size={13} className="text-[var(--neutral-500)] hover:text-[var(--neutral-200)]" />
              </button>
            </>
          )}
        </div>

        {error && (
          <div className="flex items-start gap-1.5 mb-3 text-xs text-red-400 bg-red-950/30 border border-red-900/50 rounded px-2 py-1.5">
            <ShieldAlert size={13} className="shrink-0 mt-0.5" />
            <span className="flex-1">{error}</span>
            <button onClick={() => setError(null)}><X size={12} /></button>
          </div>
        )}

        {/* --- Chats in this project --- */}
        <div className="mb-3">
          <p className="text-[10px] uppercase tracking-wide text-[var(--neutral-600)] mb-1">Chats</p>
          <div className="space-y-1">
            {chatMembers.map((chat) => (
              <div key={chat.id} className="flex items-center justify-between gap-2 text-xs text-[var(--neutral-300)] py-1">
                <span className="truncate">{chat.title}</span>
                <div className="flex items-center gap-2 shrink-0">
                  <button onClick={() => setPendingRemove({ chat, deleteChat: false })} title="Remove from project" className="text-[var(--neutral-500)] hover:text-[var(--neutral-200)]">
                    <FolderMinus size={13} />
                  </button>
                  <button onClick={() => setPendingRemove({ chat, deleteChat: true })} title="Delete chat entirely" className="text-[var(--neutral-500)] hover:text-red-400">
                    <Trash2 size={13} />
                  </button>
                </div>
              </div>
            ))}
            {chatMembers.length === 0 && <p className="text-xs text-[var(--neutral-600)]">No chats in this project yet.</p>}
          </div>
        </div>

        {/* --- Members --- */}
        <div className="mb-3 pt-2 border-t border-[var(--neutral-800)]">
          <p className="text-[10px] uppercase tracking-wide text-[var(--neutral-600)] mb-1">Members</p>

          {membersLoading ? (
            <p className="text-xs text-[var(--neutral-600)]">Loading…</p>
          ) : (
            <div className="space-y-1">
              {liveWorkspace.is_joint && (
                <p className="text-xs text-[var(--neutral-500)] italic py-0.5">
                  This project is jointly owned by its partners — no single owner right now.
                </p>
              )}

              {members.map((m) => (
                <div key={m.user_id} className="flex items-center justify-between gap-2 text-xs py-1">
                  <div className="flex items-center gap-1.5 min-w-0">
                    {m.avatar_url ? (
                      <img src={m.avatar_url} alt="" className="w-5 h-5 rounded-full shrink-0 object-cover" />
                    ) : (
                      <div className="w-5 h-5 rounded-full shrink-0 bg-[var(--neutral-700)] flex items-center justify-center text-[9px] text-[var(--neutral-300)]">
                        {(m.name || m.email || "?").charAt(0).toUpperCase()}
                      </div>
                    )}
                    <div className="min-w-0">
                      <p className="truncate text-[var(--neutral-200)] leading-tight">
                        {m.user_id === user?.id ? "You" : (m.name || m.email || m.user_id)}
                      </p>
                      {m.email && m.name && (
                        <p className="truncate text-[10px] text-[var(--neutral-600)] leading-tight">{m.email}</p>
                      )}
                    </div>
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    {m.role !== "owner" && canChangeRoleFor(m) ? (
                      <select
                        value={m.role}
                        onChange={(e) => handleRoleChange(m, e.target.value)}
                        className="text-[10px] bg-[var(--neutral-800)] text-[var(--neutral-300)] rounded px-1 py-0.5 outline-none border border-[var(--neutral-700)]"
                      >
                        {assignableRolesForMe.includes(m.role) ? null : (
                          <option value={m.role}>{m.role}</option>
                        )}
                        {assignableRolesForMe.map((r) => (
                          <option key={r} value={r}>{r}</option>
                        ))}
                      </select>
                    ) : (
                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-[var(--neutral-800)] text-[var(--neutral-400)]">{m.role}</span>
                    )}

                    {m.role === "moderator" && canGrantAttribution && (
                      <button
                        onClick={() => handleToggleGrant(m)}
                        title={m.can_toggle_attribution ? "Revoke attribution-toggle right" : "Grant attribution-toggle right"}
                        className={m.can_toggle_attribution ? "text-[var(--cyber-cyan,#22d3ee)]" : "text-[var(--neutral-500)] hover:text-[var(--neutral-200)]"}
                      >
                        {m.can_toggle_attribution ? <Eye size={13} /> : <EyeOff size={13} />}
                      </button>
                    )}

                    {m.role === "owner" && canForceRemoveOwner && (
                      <button
                        onClick={() => setPendingForceRemoveOwner(true)}
                        title="Force-remove owner (puts project into joint ownership)"
                        className="text-[var(--neutral-500)] hover:text-red-400"
                      >
                        <Trash2 size={13} />
                      </button>
                    )}

                    {m.role !== "owner" && canManageMembership && !(m.role === "partner" && !canManagePartners) && (
                      <button
                        onClick={() => setPendingRemoveMember(m)}
                        title="Remove from project"
                        className="text-[var(--neutral-500)] hover:text-red-400"
                      >
                        <X size={13} />
                      </button>
                    )}
                  </div>
                </div>
              ))}
              {members.length === 0 && (
                <p className="text-xs text-[var(--neutral-600)]">No members yet.</p>
              )}
            </div>
          )}

          {canManageMembership && (
            <form onSubmit={handleInvite} className="flex items-center gap-1 mt-2">
              <input
                type="email"
                required
                placeholder="Invite by email"
                value={inviteEmail}
                onChange={(e) => setInviteEmail(e.target.value)}
                className="flex-1 min-w-0 bg-[var(--neutral-950)] border border-[var(--neutral-700)] rounded px-1.5 py-1 text-xs outline-none"
              />
              <select
                value={inviteRole}
                onChange={(e) => setInviteRole(e.target.value)}
                className="text-[10px] bg-[var(--neutral-800)] text-[var(--neutral-300)] rounded px-1 py-1 outline-none border border-[var(--neutral-700)]"
              >
                {assignableRolesForMe.map((r) => (
                  <option key={r} value={r}>{r}</option>
                ))}
              </select>
              <button type="submit" disabled={inviteBusy} title="Invite" className="text-[var(--neutral-400)] hover:text-[var(--neutral-100)] disabled:opacity-50">
                <UserPlus size={15} />
              </button>
            </form>
          )}
        </div>

        {/* --- Ownership voting (joint state only) --- */}
        {liveWorkspace.is_joint && votes && (
          <div className="mb-3 pt-2 border-t border-[var(--neutral-800)]">
            <p className="text-[10px] uppercase tracking-wide text-[var(--neutral-600)] mb-1">
              Owner vote ({votes.votes.length}/{votes.total_partners} cast)
            </p>
            {canVote ? (
              <div className="space-y-1">
                {eligiblePartners.map((p) => {
                  const myVote = votes.votes.find((v) => v.voter_id === user?.id);
                  const voteCount = votes.votes.filter((v) => v.vote_target === p.user_id).length;
                  return (
                    <button
                      key={p.user_id}
                      onClick={() => handleVote(p.user_id)}
                      className={`w-full flex items-center justify-between text-xs px-2 py-1 rounded border ${
                        myVote?.vote_target === p.user_id
                          ? "border-[var(--neutral-500)] text-[var(--neutral-100)]"
                          : "border-[var(--neutral-800)] text-[var(--neutral-400)] hover:border-[var(--neutral-700)]"
                      }`}
                    >
                      <span className="truncate">{displayName(p)}</span>
                      <span className="text-[10px] text-[var(--neutral-500)]">{voteCount} vote{voteCount !== 1 ? "s" : ""}</span>
                    </button>
                  );
                })}
                <button
                  onClick={() => handleVote(null)}
                  className={`w-full text-xs px-2 py-1 rounded border ${
                    votes.votes.find((v) => v.voter_id === user?.id && v.vote_target === null)
                      ? "border-[var(--neutral-500)] text-[var(--neutral-100)]"
                      : "border-[var(--neutral-800)] text-[var(--neutral-400)] hover:border-[var(--neutral-700)]"
                  }`}
                >
                  Stay joint
                </button>
              </div>
            ) : (
              <p className="text-xs text-[var(--neutral-600)]">Only partners can vote on a new owner.</p>
            )}
          </div>
        )}

        {/* --- Attribution --- */}
        <div className="mb-3 pt-2 border-t border-[var(--neutral-800)] flex items-center justify-between">
          <div>
            <p className="text-xs text-[var(--neutral-300)]">Show who sent each message</p>
            <p className="text-[10px] text-[var(--neutral-600)]">Viewers/editors only see authorship if this is on.</p>
          </div>
          {canToggleAttribution ? (
            <button
              onClick={handleToggleAttribution}
              className={`text-[10px] px-2 py-1 rounded shrink-0 ${
                liveWorkspace.show_attribution
                  ? "bg-[var(--neutral-700)] text-[var(--neutral-100)]"
                  : "bg-[var(--neutral-800)] text-[var(--neutral-500)]"
              }`}
            >
              {liveWorkspace.show_attribution ? "On" : "Off"}
            </button>
          ) : (
            <span className="text-[10px] text-[var(--neutral-600)] shrink-0">{liveWorkspace.show_attribution ? "On" : "Off"}</span>
          )}
        </div>

        {/* --- Backup (Part 8.7) --- */}
        <section className="mb-3 pt-2 border-t border-[var(--neutral-800)]">
          <p className="text-[10px] uppercase tracking-wide text-[var(--neutral-600)] mb-2">Backup</p>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={handleExport}
              disabled={exportBusy}
              className="flex items-center gap-1.5 text-xs text-[var(--neutral-400)] hover:text-[var(--neutral-200)] border border-[var(--neutral-800)] rounded-lg px-3 py-1.5 disabled:opacity-50"
            >
              <Download size={12} /> {exportBusy ? "Exporting…" : "Export"}
            </button>
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              disabled={importBusy}
              className="flex items-center gap-1.5 text-xs text-[var(--neutral-400)] hover:text-[var(--neutral-200)] border border-[var(--neutral-800)] rounded-lg px-3 py-1.5 disabled:opacity-50"
            >
              <Upload size={12} /> {importBusy ? "Importing…" : "Import"}
            </button>
            <input ref={fileInputRef} type="file" accept="application/json" className="hidden" onChange={handleImportFile} />
          </div>
          <p className="text-[var(--neutral-600)] text-[10px] mt-1.5">
            Export downloads a portable backup of your own chats in this
            workspace. Import restores a backup's chats as new chats owned
            by you, attached to this workspace.
          </p>
        </section>

        {/* --- Footer: leave / delete / close --- */}
        <div className="flex justify-between items-center pt-2 border-t border-[var(--neutral-800)]">
          <div className="flex items-center gap-3">
            {myRole && (
              <button onClick={() => setPendingLeave(true)} className="flex items-center gap-1 text-xs text-[var(--neutral-400)] hover:text-[var(--neutral-200)]">
                <LogOut size={12} /> Leave
              </button>
            )}
            {(isOwner || myRole === "partner") && (
              <button onClick={() => setPendingDeleteWs(true)} className="text-xs text-red-400/80 hover:text-red-400">Delete project</button>
            )}
          </div>
          <button onClick={onClose} className="text-xs text-[var(--neutral-400)] px-3 py-1.5">Close</button>
        </div>
      </div>

      <ConfirmDialog
        open={!!pendingRemove}
        title={pendingRemove?.deleteChat ? "Delete chat" : "Remove from project"}
        message={
          pendingRemove?.deleteChat
            ? `Delete "${pendingRemove?.chat?.title}"? Its messages and memory can't be recovered.`
            : `Remove "${pendingRemove?.chat?.title}" from "${workspace.name}"? The chat stays, it just stops auto-sharing memory with the rest of the project.`
        }
        confirmLabel={pendingRemove?.deleteChat ? "Delete" : "Remove"}
        tone="danger"
        onConfirm={confirmRemove}
        onCancel={() => setPendingRemove(null)}
      />
      <ConfirmDialog
        open={pendingDeleteWs}
        title="Delete project"
        message={`Delete "${workspace.name}"? Member chats survive and just stop auto-sharing memory with each other.`}
        confirmLabel="Delete"
        tone="danger"
        onConfirm={confirmDeleteWorkspace}
        onCancel={() => setPendingDeleteWs(false)}
      />
      <ConfirmDialog
        open={!!pendingRemoveMember}
        title="Remove member"
        message={`Remove ${pendingRemoveMember ? displayName(pendingRemoveMember) : ""} from "${workspace.name}"? They lose access to every chat in this project.`}
        confirmLabel="Remove"
        tone="danger"
        onConfirm={confirmRemoveMember}
        onCancel={() => setPendingRemoveMember(null)}
      />
      <ConfirmDialog
        open={pendingForceRemoveOwner}
        title="Remove owner"
        message={`Force-remove the current owner? "${workspace.name}" becomes jointly owned by its partners until a vote resolves a new owner.`}
        confirmLabel="Remove owner"
        tone="danger"
        onConfirm={confirmForceRemoveOwner}
        onCancel={() => setPendingForceRemoveOwner(false)}
      />

      {pendingLeave && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" onClick={() => setPendingLeave(false)}>
          <div
            className="w-80 rounded-lg p-4 bg-[var(--neutral-900)] border border-[var(--neutral-700)]"
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="text-sm font-medium text-[var(--neutral-200)] mb-2">Leave project</h3>
            {isOwner ? (
              <>
                <p className="text-xs text-[var(--neutral-400)] mb-2">
                  You're the owner. Hand ownership to a partner, or leave the project jointly owned.
                </p>
                {eligiblePartners.length > 0 && (
                  <select
                    value={successorId}
                    onChange={(e) => setSuccessorId(e.target.value)}
                    className="w-full mb-2 text-xs bg-[var(--neutral-950)] border border-[var(--neutral-700)] rounded px-2 py-1.5 outline-none"
                  >
                    <option value="">Make joint (no successor)</option>
                    {eligiblePartners.map((p) => (
                      <option key={p.user_id} value={p.user_id}>Transfer to {displayName(p)}</option>
                    ))}
                  </select>
                )}
              </>
            ) : (
              <p className="text-xs text-[var(--neutral-400)] mb-3">
                Leave "{workspace.name}"? You lose access to every chat in this project.
              </p>
            )}
            <div className="flex justify-end gap-2 mt-1">
              <button onClick={() => setPendingLeave(false)} className="text-xs px-3 py-1.5 text-[var(--neutral-400)]">Cancel</button>
              <button onClick={confirmLeave} className="text-xs px-3 py-1.5 rounded bg-red-500/90 text-white font-medium">Leave</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}