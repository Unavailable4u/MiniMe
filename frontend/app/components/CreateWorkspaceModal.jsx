"use client";
import { useState } from "react";
import { useSession } from "../context/SessionContext";

export default function CreateWorkspaceModal({ onClose }) {
  const { createWorkspace } = useSession();
  const [name, setName] = useState("");

  async function save() {
    if (!name.trim()) return;
    await createWorkspace(name.trim());
    onClose();
  }

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onClick={onClose}>
      <div className="bg-neutral-900 border border-neutral-700 rounded-lg p-4 w-80" onClick={(e) => e.stopPropagation()}>
        <h3 className="text-sm font-medium text-neutral-200 mb-3">New project</h3>
        <input
          autoFocus
          value={name}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && save()}
          placeholder="Project name"
          className="w-full bg-neutral-950 border border-neutral-700 rounded px-2 py-1.5 text-xs outline-none mb-4"
        />
        <div className="flex justify-end gap-2">
          <button onClick={onClose} className="text-xs text-neutral-400 px-3 py-1.5">Cancel</button>
          <button onClick={save} className="text-xs bg-neutral-100 text-neutral-900 rounded px-3 py-1.5 font-medium">Create</button>
        </div>
      </div>
    </div>
  );
}