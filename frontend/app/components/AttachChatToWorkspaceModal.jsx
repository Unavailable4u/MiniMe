"use client";
import { useSession } from "../context/SessionContext";

export default function AttachChatToWorkspaceModal({ chat, workspaces, onClose }) {
  const { addWorkspaceChat } = useSession();

  const candidates = (workspaces || []).filter((workspace) => !workspace.chat_ids?.includes(chat.id));

  async function pickWorkspace(workspaceId) {
    await addWorkspaceChat(workspaceId, chat.id);
    onClose();
  }

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onClick={onClose}>
      <div
        className="bg-[var(--neutral-900)] border border-[var(--neutral-700)] rounded-lg p-4 w-80 max-h-[70vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="text-sm font-medium text-[var(--neutral-200)] mb-3">Add "{chat.title}" to a project</h3>
        <div className="space-y-1">
          {candidates.map((workspace) => (
            <button
              key={workspace.id}
              onClick={() => pickWorkspace(workspace.id)}
              className="w-full text-left text-xs text-[var(--neutral-300)] hover:bg-[var(--neutral-800)] rounded px-2 py-1.5 truncate"
            >
              {workspace.name}
            </button>
          ))}
          {candidates.length === 0 && (
            <p className="text-xs text-[var(--neutral-600)]">No projects yet — create one from this chat instead.</p>
          )}
        </div>
        <div className="flex justify-end mt-4">
          <button onClick={onClose} className="text-xs text-[var(--neutral-400)] px-3 py-1.5">Close</button>
        </div>
      </div>
    </div>
  );
}
