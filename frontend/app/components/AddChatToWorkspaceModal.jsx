"use client";
import { useSession } from "../context/SessionContext";

// candidates = chats not already in THIS workspace and not in ANY other
// workspace — a chat living in two "projects" at once would mean its
// linked_chat_ids gets overwritten by whichever workspace's _sync ran
// last, which is silent, confusing behavior. Keep membership exclusive.
export default function AddChatToWorkspaceModal({ workspace, allChats, workspacedChatIds, onClose }) {
  const { addWorkspaceChat } = useSession();

  const candidates = allChats.filter(
    (c) => !workspace.chat_ids.includes(c.id) && !workspacedChatIds.has(c.id)
  );

  async function pick(chatId) {
    await addWorkspaceChat(workspace.id, chatId);
    onClose();
  }

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onClick={onClose}>
      <div className="bg-neutral-900 border border-neutral-700 rounded-lg p-4 w-80 max-h-[70vh] overflow-y-auto" onClick={(e) => e.stopPropagation()}>
        <h3 className="text-sm font-medium text-neutral-200 mb-3">Add a chat to "{workspace.name}"</h3>
        <div className="space-y-1">
          {candidates.map((c) => (
            <button
              key={c.id}
              onClick={() => pick(c.id)}
              className="w-full text-left text-xs text-neutral-300 hover:bg-neutral-800 rounded px-2 py-1.5 truncate"
            >
              {c.title}
            </button>
          ))}
          {candidates.length === 0 && (
            <p className="text-xs text-neutral-600">No available chats — everything's already in a project or in this one.</p>
          )}
        </div>
        <div className="flex justify-end mt-4">
          <button onClick={onClose} className="text-xs text-neutral-400 px-3 py-1.5">Close</button>
        </div>
      </div>
    </div>
  );
}