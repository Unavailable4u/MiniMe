"use client";
import { useState } from "react";
import { useSession } from "../context/SessionContext";
import { Pencil, Check, X, FolderMinus, Trash2 } from "lucide-react";
import ConfirmDialog from "./ConfirmDialog";

export default function ManageWorkspaceModal({ workspace, allChats, onClose }) {
  const { renameWorkspace, removeWorkspaceChat, deleteWorkspace } = useSession();
  const [name, setName] = useState(workspace.name);
  const [editingName, setEditingName] = useState(false);
  const [pendingRemove, setPendingRemove] = useState(null); // { chat, deleteChat }
  const [pendingDeleteWs, setPendingDeleteWs] = useState(false);

  const members = allChats.filter((c) => workspace.chat_ids.includes(c.id));

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

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onClick={onClose}>
      <div className="bg-[var(--neutral-900)] border border-[var(--neutral-700)] rounded-lg p-4 w-80 max-h-[70vh] overflow-y-auto" onClick={(e) => e.stopPropagation()}>
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

        <div className="space-y-1 mb-3">
          {members.map((chat) => (
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
          {members.length === 0 && <p className="text-xs text-[var(--neutral-600)]">No chats in this project yet.</p>}
        </div>

        <div className="flex justify-between items-center pt-2 border-t border-[var(--neutral-800)]">
          <button onClick={() => setPendingDeleteWs(true)} className="text-xs text-red-400/80 hover:text-red-400">Delete project</button>
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
    </div>
  );
}