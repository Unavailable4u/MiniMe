"use client";
import { useState, useEffect } from "react";   // CHANGED — add useEffect
import { useSession } from "../context/SessionContext";
import { useWorkspaceDockActions, useLastActiveChatId } from "../context/WorkspaceDockContext"; // NEW — step 3e
import { Plus, Trash2, Pencil, Link2, Settings2, ChevronLeft, ChevronRight, Check, X, FolderPlus, FolderInput } from "lucide-react";
import ManageBatchModal from "./ManageBatchModal";
import CreateWorkspaceModal from "./CreateWorkspaceModal";
import AttachChatToWorkspaceModal from "./AttachChatToWorkspaceModal";
import AddChatToWorkspaceModal from "./AddChatToWorkspaceModal";
import ManageWorkspaceModal from "./ManageWorkspaceModal";
import ConfirmDialog from "./ConfirmDialog";

// NEW — §9.1: color-code batches so grouping is visible at a glance
// without reading labels. Deterministic hash of the batch id → one of a
// small fixed palette, so a given batch always gets the same color across
// reloads (no need to persist a color field on the batch object itself).
const BATCH_ACCENTS = [
  "var(--cyber-cyan)",
  "var(--cyber-magenta)",
  "var(--cyber-amber)",
  "var(--cyber-violet)",
  "var(--cyber-lime)",
];

function hashBatchColor(batchId) {
  let hash = 0;
  for (let i = 0; i < batchId.length; i++) {
    hash = (hash * 31 + batchId.charCodeAt(i)) | 0;
  }
  return BATCH_ACCENTS[Math.abs(hash) % BATCH_ACCENTS.length];
}

export default function ChatSidebar({ collapsed, onToggle }) {
  const { chats, batches, workspaces } = useSession();
  // NEW — step 3e: these five used to come from useSession() and wrote
  // into one shared global sessionId/messages. ChatSidebar is global
  // (item #6, kept that way on purpose) and lists chats belonging to
  // many different workspaces, so it needs the key-agnostic dock hook —
  // each call resolves its own dock slot from the chatId involved.
  const { switchChat, createNewChat, renameChat, deleteChat, linkChats } = useWorkspaceDockActions();
  // NEW — step 3e: replaces the old `sessionId === chat.id` highlight.
  // There's no longer one shared "the active chat" once different docks
  // can each be showing a different chat at once — see this hook's own
  // comment in WorkspaceDockContext.jsx.
  const activeChatId = useLastActiveChatId();
  const [editingId, setEditingId] = useState(null);
  const [editTitle, setEditTitle] = useState("");
  const [linkingId, setLinkingId] = useState(null);
  const [pendingDelete, setPendingDelete] = useState(null); // chat object or null
  // NEW — §4: which batch's manage modal is open. The modal itself is
  // built in §5 (ManageBatchModal) — for now this just tracks selection
  // so wiring the modal in next is a one-line render addition.
  const [managingBatch, setManagingBatch] = useState(null);
  const [managingWorkspace, setManagingWorkspace] = useState(null);
  const [creatingWorkspace, setCreatingWorkspace] = useState(false);
  const [creatingProjectFrom, setCreatingProjectFrom] = useState(null);
  const [attachingChatToProject, setAttachingChatToProject] = useState(null);
  const [addingToWorkspace, setAddingToWorkspace] = useState(null);
  // NEW — §8: search bar. Client-side only — chats/batches/workspaces are
  // already loaded in state, no backend endpoint needed.
  const [query, setQuery] = useState("");

  function openManageBatch(batch) {
    setManagingBatch(batch);
  }

  const workspacedChatIds = new Set(workspaces.flatMap((w) => w.chat_ids));
// A chat that's somehow in both a workspace and a batch is shown under
// the workspace only — it's the more structural, persistent grouping.
  const batchedChatIds = new Set(
    batches.flatMap((b) => b.member_chat_ids).filter((id) => !workspacedChatIds.has(id))
  );
  const unbatchedChats = chats.filter((c) => !batchedChatIds.has(c.id) && !workspacedChatIds.has(c.id));

  // NEW — §8: search filtering. A group (workspace/batch) stays visible if
  // its own name matches OR any of its member chats match — and in that
  // second case we only show the matching members, not the whole group,
  // so a search for one chat title doesn't dump every batch-mate on screen.
  const q = query.trim().toLowerCase();
  const chatMatches = (chat) => !q || chat.title.toLowerCase().includes(q);

  const filteredWorkspaces = q
    ? workspaces.filter(
        (ws) => ws.name.toLowerCase().includes(q) || chats.some((c) => ws.chat_ids.includes(c.id) && chatMatches(c))
      )
    : workspaces;

  const filteredBatches = q
    ? batches.filter(
        (b) => b.name.toLowerCase().includes(q) || chats.some((c) => b.member_chat_ids.includes(c.id) && chatMatches(c))
      )
    : batches;

  const filteredUnbatchedChats = unbatchedChats.filter(chatMatches);

  if (collapsed) {
    return (
      <div className="w-10 shrink-0 border-r border-[var(--neutral-800)] flex flex-col items-center py-3 gap-3">
        <button onClick={onToggle} className="text-[var(--neutral-500)] hover:text-[var(--neutral-300)]" title="Show chats">
          <ChevronRight size={16} />
        </button>
      </div>
    );
  }

  function startRename(chat) {
    setEditingId(chat.id);
    setEditTitle(chat.title);
  }

  async function commitRename(chatId) {
    if (editTitle.trim()) await renameChat(chatId, editTitle.trim());
    setEditingId(null);
  }

  function askDelete(chat) {
    setPendingDelete(chat);
  }

  function openCreateProject(chatIds, initialName) {
    setCreatingProjectFrom({ chatIds, initialName });
  }

  function openAttachToProject(chat) {
    setAttachingChatToProject(chat);
  }

  async function confirmDelete() {
    await deleteChat(pendingDelete.id);
    setPendingDelete(null);
  }

  // Extracted from the original flat-list row so batched (indented) and
  // unbatched chats render identically — same click/rename/link/delete
  // behavior either way, just an indent + no per-row Link2 badge once a
  // chat is grouped (the batch header above it already communicates that).
  function renderChatRow(chat, { indent = false, accentColor = null, allowProjectActions = false } = {}) {
    return (
      <div
        key={chat.id}
        className={`group px-3 py-2 border-b border-[var(--neutral-900)] cursor-pointer ${indent ? "pl-6" : ""} ${
          chat.id === activeChatId ? "bg-[var(--neutral-800-a70)]" : "hover:bg-[var(--neutral-900)]"
        }`}
        style={accentColor ? { borderLeft: `2px solid ${accentColor}` } : undefined}
        onClick={() => editingId !== chat.id && switchChat(chat.id)}
      >
        {editingId === chat.id ? (
          <div className="flex items-center gap-1" onClick={(e) => e.stopPropagation()}>
            <input
              autoFocus
              value={editTitle}
              onChange={(e) => setEditTitle(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && commitRename(chat.id)}
              className="flex-1 bg-[var(--neutral-950)] border border-[var(--neutral-700)] rounded px-1.5 py-0.5 text-xs outline-none"
            />
            <button onClick={() => commitRename(chat.id)}><Check size={13} className="text-green-400" /></button>
            <button onClick={() => setEditingId(null)}><X size={13} className="text-[var(--neutral-500)]" /></button>
          </div>
        ) : (
          <div className="flex items-center justify-between gap-1">
            <span className="text-xs text-[var(--neutral-200)] truncate">{chat.title}</span>
            <div className="hidden group-hover:flex items-center gap-1.5 shrink-0">
              {allowProjectActions && (
                <>
                  <button onClick={(e) => { e.stopPropagation(); openCreateProject([chat.id], chat.title); }} title="Create project from chat">
                    <FolderPlus size={12} className="text-[var(--neutral-500)] hover:text-[var(--neutral-200)]" />
                  </button>
                  <button onClick={(e) => { e.stopPropagation(); openAttachToProject(chat); }} title="Add chat to project">
                    <FolderInput size={12} className="text-[var(--neutral-500)] hover:text-[var(--neutral-200)]" />
                  </button>
                </>
              )}
              {/* Batched chats already show a badge in their batch header
                  above, so only render the per-row Link2 badge for chats
                  linked the old, non-batch way. */}
              {!indent && chat.linked_chat_ids?.length > 0 && (
                <Link2 size={12} className="text-[var(--neutral-500)]" title={`Linked to ${chat.linked_chat_ids.length} chat(s)`} />
              )}
              {/* Manual per-chat link button hidden once a chat is
                  batched — batch membership (managed via §5's "Manage
                  batch" modal) is now the single source of truth for its
                  linked_chat_ids, so editing it here directly would fight
                  memory_batch.py's _sync_members on the next batch edit. */}
              {!indent && (
                <button onClick={(e) => { e.stopPropagation(); setLinkingId(chat.id); }} title="Share memory with other chats">
                  <Link2 size={12} className="text-[var(--neutral-500)] hover:text-[var(--neutral-200)]" />
                </button>
              )}
              <button onClick={(e) => { e.stopPropagation(); startRename(chat); }} title="Rename">
                <Pencil size={12} className="text-[var(--neutral-500)] hover:text-[var(--neutral-200)]" />
              </button>
              <button onClick={(e) => { e.stopPropagation(); askDelete(chat); }} title="Delete">
                <Trash2 size={12} className="text-[var(--neutral-500)] hover:text-red-400" />
              </button>
            </div>
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="w-64 shrink-0 border-r border-[var(--neutral-800)] flex flex-col h-full">
      <div className="flex items-center justify-between px-3 py-3 border-b border-[var(--neutral-800)]">
        <span className="text-xs font-medium text-[var(--neutral-400)]">Chats</span>
        <div className="flex items-center gap-2">
          <button onClick={createNewChat} title="New chat" className="text-[var(--neutral-400)] hover:text-[var(--neutral-100)]">
            <Plus size={15} />
          </button>
          <button onClick={() => setCreatingWorkspace(true)} title="New project" className="text-[var(--neutral-400)] hover:text-[var(--neutral-100)]">
            <FolderPlus size={15} />
          </button>
          <button onClick={onToggle} title="Hide chats" className="text-[var(--neutral-500)] hover:text-[var(--neutral-300)]">
            <ChevronLeft size={15} />
          </button>
        </div>
      </div>

      <div className="px-3 py-2 border-b border-[var(--neutral-900)]">
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search chats, batches, projects…"
          className="w-full bg-black/30 border border-[var(--neutral-800)] rounded px-2 py-1 text-xs outline-none focus:border-[var(--cyber-cyan)]"
        />
      </div>

      <div className="flex-1 overflow-y-auto">
        {filteredWorkspaces.map((ws) => {
          const memberChats = chats.filter((c) => ws.chat_ids.includes(c.id));
          // Whole group matched by name → show every member. Only a member
          // matched → show just the matching ones (still lets you jump
          // straight to the chat you searched for).
          const visibleMembers = ws.name.toLowerCase().includes(q) ? memberChats : memberChats.filter(chatMatches);
          return (
            <div key={ws.id} className="border-b border-[var(--neutral-900)]">
              <div className="flex items-center justify-between px-3 py-1.5 bg-black/20">
                <span className="text-[10px] uppercase tracking-wide" style={{ color: "var(--cyber-magenta)" }}>
                  {ws.name} · {ws.chat_ids.length}
                </span>
                <div className="flex items-center gap-2">
                  <button onClick={() => setAddingToWorkspace(ws)} title="Add chat to project">
                    <FolderInput size={11} className="text-[var(--neutral-500)] hover:text-[var(--neutral-200)]" />
                  </button>
                  <button onClick={() => setManagingWorkspace(ws)} title="Manage project">
                    <Settings2 size={11} className="text-[var(--neutral-500)] hover:text-[var(--neutral-200)]" />
                  </button>
                </div>
              </div>
              {visibleMembers.map((chat) => renderChatRow(chat, { indent: true }))}
            </div>
          );
        })}

        {filteredBatches.map((batch) => {
          const memberChats = chats.filter(
            (c) => batch.member_chat_ids.includes(c.id) && !workspacedChatIds.has(c.id)
          );
          if (memberChats.length === 0) return null; // all members moved into a workspace
          const visibleMembers = batch.name.toLowerCase().includes(q) ? memberChats : memberChats.filter(chatMatches);
          if (visibleMembers.length === 0) return null;
          const accentColor = hashBatchColor(batch.id);
          return (
            <div key={batch.id} className="border-b border-[var(--neutral-900)]">
              <div className="flex items-center justify-between px-3 py-1.5 bg-black/20">
                <span className="text-[10px] uppercase tracking-wide flex items-center" style={{ color: accentColor }}>
                  <Link2 size={10} className="inline mr-1" />
                  {batch.name} · {memberChats.length}
                </span>
                <div className="flex items-center gap-2">
                  <button onClick={() => openCreateProject(memberChats.map((chat) => chat.id), batch.name)} title="Create project from batch">
                    <FolderPlus size={11} className="text-[var(--neutral-500)] hover:text-[var(--neutral-200)]" />
                  </button>
                  <button onClick={() => openManageBatch(batch)} title="Manage batch">
                    <Settings2 size={11} className="text-[var(--neutral-500)] hover:text-[var(--neutral-200)]" />
                  </button>
                </div>
              </div>
              {visibleMembers.map((chat) => renderChatRow(chat, { indent: true, accentColor }))}
            </div>
          );
        })}

        {filteredUnbatchedChats.map((chat) => renderChatRow(chat, { allowProjectActions: true }))}
      </div>

      {linkingId && (
        <LinkChatsModal
          chatId={linkingId}
          allChats={chats}
          onClose={() => setLinkingId(null)}
        />
      )}

      {managingBatch && (
        <ManageBatchModal
          batch={batches.find((b) => b.id === managingBatch.id) || managingBatch}
          allChats={chats}
          onClose={() => setManagingBatch(null)}
        />
      )}

      <ConfirmDialog
        open={!!pendingDelete}
        title="Delete chat"
        message={`Delete "${pendingDelete?.title}"? Its messages and memory can't be recovered.`}
        confirmLabel="Delete"
        tone="danger"
        onConfirm={confirmDelete}
        onCancel={() => setPendingDelete(null)}
      />

      {creatingWorkspace && <CreateWorkspaceModal onClose={() => setCreatingWorkspace(false)} />}

      {creatingProjectFrom && (
        <CreateWorkspaceModal
          onClose={() => setCreatingProjectFrom(null)}
          initialName={creatingProjectFrom.initialName}
          sourceChatIds={creatingProjectFrom.chatIds}
        />
      )}

      {attachingChatToProject && (
        <AttachChatToWorkspaceModal
          chat={attachingChatToProject}
          workspaces={workspaces}
          onClose={() => setAttachingChatToProject(null)}
        />
      )}

      {addingToWorkspace && (
        <AddChatToWorkspaceModal
          workspace={workspaces.find((w) => w.id === addingToWorkspace.id) || addingToWorkspace}
          allChats={chats}
          workspacedChatIds={workspacedChatIds}
          onClose={() => setAddingToWorkspace(null)}
        />
      )}

      {managingWorkspace && (
        <ManageWorkspaceModal
          workspace={workspaces.find((w) => w.id === managingWorkspace.id) || managingWorkspace}
          allChats={chats}
          onClose={() => setManagingWorkspace(null)}
        />
      )}
    </div>
  );
}

// NEW — §6: this modal is now "Create batch" in practice. It keeps the
// name LinkChatsModal (and the old linkChats() path stays available for
// power users linking a chat that ISN'T going into a batch), but saving
// with 1+ chats selected now calls createBatch() so the result is a real
// mutual-membership group, not a one-directional linked_chat_ids edit.
// This only ever opens for unbatched chats — the Link2 button that opens
// it is already hidden on batched rows (see renderChatRow above), so
// there's no "chat already in a batch" case to special-case here.
function LinkChatsModal({ chatId, allChats, onClose }) {
  const { linkChats, createBatch, estimateBatch } = useSession();   // CHANGED
  const current = allChats.find((c) => c.id === chatId);
  const [selected, setSelected] = useState(new Set(current?.linked_chat_ids || []));
  const [estimate, setEstimate] = useState(null);   // NEW — §9.2

  function toggle(id) {
    const next = new Set(selected);
    next.has(id) ? next.delete(id) : next.add(id);
    setSelected(next);
  }

  // NEW — §9.2: recompute as the selection changes. Nothing to estimate
  // until there's at least one other chat picked — an empty selection
  // isn't forming a batch at all.
  useEffect(() => {
    if (selected.size === 0) { setEstimate(null); return; }
    let cancelled = false;
    estimateBatch([chatId, ...Array.from(selected)]).then((result) => {
      if (!cancelled) setEstimate(result);
    });
    return () => { cancelled = true; };
  }, [selected, chatId, estimateBatch]);

  async function save() {
    if (selected.size > 0) {
      // Selecting at least one other chat forms a real batch now, instead
      // of the old one-directional link. Default name is editable right
      // after via ManageBatchModal's rename affordance (§5).
      const defaultName = `${current?.title || "Chat"} + ${selected.size} more`;
      await createBatch(defaultName, [chatId, ...Array.from(selected)]);
    } else {
      // Nothing selected: fall back to clearing any old-style links.
      await linkChats(chatId, []);
    }
    onClose();
  }

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onClick={onClose}>
      <div
        className="bg-[var(--neutral-900)] border border-[var(--neutral-700)] rounded-lg p-4 w-80 max-h-[70vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="text-sm font-medium text-[var(--neutral-200)] mb-1">Share memory with "{current?.title}"</h3>
        <p className="text-xs text-[var(--neutral-500)] mb-3">
          Pick chats to group with this one — they'll all share memory with each other as a batch.
        </p>
        <div className="space-y-1">
          {allChats.filter((c) => c.id !== chatId).map((c) => (
            <label key={c.id} className="flex items-center gap-2 text-xs text-[var(--neutral-300)] py-1 cursor-pointer">
              <input type="checkbox" checked={selected.has(c.id)} onChange={() => toggle(c.id)} />
              {c.title}
            </label>
          ))}
          {allChats.length <= 1 && (
            <p className="text-xs text-[var(--neutral-600)]">No other chats yet.</p>
          )}
        </div>

        {/* NEW — §9.2: goes right here, between the checkbox list and the
            Cancel/Save row — so it's the last thing you see before deciding */}
        {estimate && selected.size > 0 && (
          <p className={`text-[11px] mt-3 ${estimate.max_tokens_per_message > 800 ? "text-red-400" : "text-[var(--neutral-500)]"}`}>
            ~{estimate.max_tokens_per_message} tokens of shared context injected per message across {estimate.member_count} chats
          </p>
        )}

        <div className="flex justify-end gap-2 mt-4">
          <button onClick={onClose} className="text-xs text-[var(--neutral-400)] px-3 py-1.5">Cancel</button>
          <button onClick={save} className="text-xs bg-[var(--accent)] text-[var(--accent-text)] rounded px-3 py-1.5 font-medium">Save</button>
        </div>
      </div>
    </div>
  );
}
