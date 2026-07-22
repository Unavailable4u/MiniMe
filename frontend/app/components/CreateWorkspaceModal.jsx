"use client";
import { useEffect, useState } from "react";
import { useSession } from "../context/SessionContext";

export default function CreateWorkspaceModal({ onClose, initialName = "", sourceChatIds = [] }) {
  const { createWorkspace, createWorkspaceWithChats } = useSession();
  const [name, setName] = useState(initialName);

  const wrappingChats = sourceChatIds.length > 0;

  useEffect(() => {
    setName(initialName);
  }, [initialName]);

  async function save() {
    if (!name.trim()) return;
    if (wrappingChats) {
      await createWorkspaceWithChats(name.trim(), sourceChatIds);
    } else {
      await createWorkspace(name.trim());
    }
    onClose();
  }

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onClick={onClose}>
      <div className="bg-[var(--neutral-900)] border border-[var(--neutral-700)] rounded-lg p-4 w-80" onClick={(e) => e.stopPropagation()}>
        <h3 className="text-sm font-medium text-[var(--neutral-200)] mb-1">
          {wrappingChats ? "Create project from selection" : "New project"}
        </h3>
        {wrappingChats && (
          <p className="text-[11px] text-[var(--neutral-500)] mb-3">
            {sourceChatIds.length === 1 ? "Wrap 1 chat" : `Wrap ${sourceChatIds.length} chats`} into a new project.
          </p>
        )}
        <input
          autoFocus
          value={name}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && save()}
          placeholder="Project name"
          className="w-full bg-[var(--neutral-950)] border border-[var(--neutral-700)] rounded px-2 py-1.5 text-xs outline-none mb-4"
        />
        <div className="flex justify-end gap-2">
          <button onClick={onClose} className="text-xs text-[var(--neutral-400)] px-3 py-1.5">Cancel</button>
          <button onClick={save} className="text-xs bg-[var(--accent)] text-[var(--accent-text)] rounded px-3 py-1.5 font-medium">
            {wrappingChats ? "Create project" : "Create"}
          </button>
        </div>
      </div>
    </div>
  );
}