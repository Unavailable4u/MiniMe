"use client";
import { useState } from "react";
import { useSession } from "../context/SessionContext";
import ConfirmDialog from "./ConfirmDialog";

export default function ManageBatchModal({ batch, allChats, onClose }) {
  const { unlinkBatchMembers, renameBatch, deleteBatch } = useSession();
  const [checked, setChecked] = useState(new Set());
  const [editingName, setEditingName] = useState(false);
  const [name, setName] = useState(batch.name);
  const [confirmDissolve, setConfirmDissolve] = useState(false);
  const [confirmDeleteAll, setConfirmDeleteAll] = useState(false);

  const members = allChats.filter((c) => batch.member_chat_ids.includes(c.id));
  const wouldDissolve = members.length - checked.size <= 1;

  function toggle(id) {
    const next = new Set(checked);
    next.has(id) ? next.delete(id) : next.add(id);
    setChecked(next);
  }

  function handleUnlinkClick() {
    if (checked.size === 0) return;
    if (wouldDissolve) setConfirmDissolve(true);
    else doUnlink();
  }

  async function doUnlink() {
    await unlinkBatchMembers(batch.id, Array.from(checked));
    setConfirmDissolve(false);
    onClose();
  }

  async function commitRename() {
    if (name.trim() && name !== batch.name) await renameBatch(batch.id, name.trim());
    setEditingName(false);
  }

  return (
    <>
      <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50" onClick={onClose}>
        <div
          className="w-96 rounded-lg p-4"
          style={{ background: "var(--cyber-panel)", border: "1px solid var(--cyber-border)" }}
          onClick={(e) => e.stopPropagation()}
        >
          {editingName ? (
            <div className="flex items-center gap-1 mb-3">
              <input
                autoFocus
                value={name}
                onChange={(e) => setName(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && commitRename()}
                className="flex-1 bg-black/30 border border-neutral-700 rounded px-2 py-1 text-sm"
              />
              <button onClick={commitRename} className="text-xs" style={{ color: "var(--cyber-cyan)" }}>
                Save
              </button>
            </div>
          ) : (
            <h3
              className="text-sm font-display mb-3 cursor-pointer"
              style={{ color: "var(--cyber-text)" }}
              onClick={() => setEditingName(true)}
              title="Click to rename"
            >
              {batch.name}
            </h3>
          )}

          <p className="text-xs mb-2" style={{ color: "var(--cyber-dim)" }}>
            Check the chats you want to unlink from this batch.
          </p>
          <div className="space-y-1 mb-3 max-h-52 overflow-y-auto">
            {members.map((c) => (
              <label key={c.id} className="flex items-center gap-2 text-xs py-1">
                <input type="checkbox" checked={checked.has(c.id)} onChange={() => toggle(c.id)} />
                {c.title}
              </label>
            ))}
          </div>
          {wouldDissolve && checked.size > 0 && (
            <p className="text-[11px] mb-3" style={{ color: "var(--cyber-magenta)" }}>
              Only 1 chat would remain — the whole batch will be dissolved.
            </p>
          )}

          <div className="flex justify-between items-center pt-2 border-t" style={{ borderColor: "var(--cyber-border)" }}>
            <button
              onClick={() => setConfirmDeleteAll(true)}
              className="text-xs"
              style={{ color: "var(--cyber-magenta)" }}
            >
              Delete entire batch
            </button>
            <div className="flex gap-2">
              <button onClick={onClose} className="text-xs px-3 py-1.5" style={{ color: "var(--cyber-dim)" }}>
                Close
              </button>
              <button
                onClick={handleUnlinkClick}
                disabled={checked.size === 0}
                className="text-xs px-3 py-1.5 rounded font-medium disabled:opacity-40"
                style={{ background: "var(--cyber-cyan)", color: "var(--cyber-bg)" }}
              >
                Unlink selected
              </button>
            </div>
          </div>
        </div>
      </div>

      <ConfirmDialog
        open={confirmDissolve}
        title="This will dissolve the batch"
        message="Unlinking these chats leaves only 1 chat behind, which isn't a group anymore — the batch will be removed entirely and that chat will go back to being unlinked."
        confirmLabel="Unlink & dissolve"
        tone="danger"
        onConfirm={doUnlink}
        onCancel={() => setConfirmDissolve(false)}
      />
      <ConfirmDialog
        open={confirmDeleteAll}
        title="Delete this batch"
        message={`Delete "${batch.name}"? All ${members.length} chats stay, but they'll stop sharing memory with each other.`}
        confirmLabel="Delete batch"
        tone="danger"
        onConfirm={async () => {
          await deleteBatch(batch.id);
          setConfirmDeleteAll(false);
          onClose();
        }}
        onCancel={() => setConfirmDeleteAll(false)}
      />
    </>
  );
}
