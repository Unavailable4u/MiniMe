
// frontend/app/components/auth/AccountMenu.jsx
"use client";
import { useState } from "react";
import { useAuth } from "../../context/AuthContext";
import { X } from "lucide-react";

export default function AccountMenu() {
  const { user, signOut, updateProfile, authError } = useAuth();
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState("");
  const [avatarUrl, setAvatarUrl] = useState("");
  const [saving, setSaving] = useState(false);

  if (!user) return null; // AppShell only renders once signed in, but stay defensive

  const meta = user.user_metadata || {};
  const displayName = meta.full_name || meta.name;
  const avatar = meta.avatar_url || meta.picture;

  function openEditor() {
    setName(displayName || "");
    setAvatarUrl(avatar || "");
    setEditing(true);
  }

  async function handleSave(e) {
    e.preventDefault();
    setSaving(true);
    try {
      await updateProfile({ displayName: name.trim(), avatarUrl: avatarUrl.trim() });
      setEditing(false);
      setOpen(false);
    } catch {
      // authError from context already holds a message; the panel stays open to show it.
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex items-center gap-1.5 text-xs text-[var(--neutral-500)] hover:text-[var(--neutral-300)] rounded-lg px-2 py-1 max-w-[180px]"
        title={user.email}
      >
        {avatar ? (
          <img src={avatar} alt="" className="w-5 h-5 rounded-full shrink-0 object-cover" />
        ) : (
          <div className="w-5 h-5 rounded-full shrink-0 bg-[var(--neutral-700)] flex items-center justify-center text-[9px] text-[var(--neutral-300)]">
            {(displayName || user.email || "?").charAt(0).toUpperCase()}
          </div>
        )}
        <span className="truncate">{displayName || user.email}</span>
      </button>
      {open && (
        <>
          {/* Click-outside catcher — same pattern as any other dropdown in this codebase would use */}
          <div className="fixed inset-0 z-40" onClick={() => { setOpen(false); setEditing(false); }} />
          <div className="absolute right-0 mt-1 w-56 rounded-lg border border-[var(--neutral-800)] bg-[var(--neutral-900)] shadow-lg py-1 z-50">
            {editing ? (
              <form onSubmit={handleSave} className="px-3 py-2 space-y-2">
                <div className="flex items-center justify-between">
                  <p className="text-[10px] uppercase tracking-wide text-[var(--neutral-600)]">Edit profile</p>
                  <button type="button" onClick={() => setEditing(false)}>
                    <X size={12} className="text-[var(--neutral-500)]" />
                  </button>
                </div>
                <div>
                  <label className="block text-[10px] text-[var(--neutral-500)] mb-0.5">Name</label>
                  <input
                    type="text"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    placeholder="Shown to project collaborators"
                    className="w-full text-xs bg-[var(--neutral-950)] border border-[var(--neutral-700)] rounded px-2 py-1 outline-none"
                  />
                </div>
                <div>
                  <label className="block text-[10px] text-[var(--neutral-500)] mb-0.5">Avatar URL</label>
                  <input
                    type="url"
                    value={avatarUrl}
                    onChange={(e) => setAvatarUrl(e.target.value)}
                    placeholder="https://…"
                    className="w-full text-xs bg-[var(--neutral-950)] border border-[var(--neutral-700)] rounded px-2 py-1 outline-none"
                  />
                </div>
                {authError && <p className="text-[10px] text-red-400">{authError}</p>}
                <button
                  type="submit"
                  disabled={saving}
                  className="w-full text-xs bg-[var(--neutral-700)] text-[var(--neutral-100)] rounded px-2 py-1.5 disabled:opacity-50"
                >
                  {saving ? "Saving…" : "Save"}
                </button>
              </form>
            ) : (
              <>
                <button
                  type="button"
                  onClick={openEditor}
                  className="w-full text-left text-xs text-[var(--neutral-400)] hover:text-[var(--neutral-200)] px-3 py-1.5"
                >
                  Edit profile
                </button>
                <button
                  type="button"
                  onClick={signOut}
                  className="w-full text-left text-xs text-[var(--neutral-400)] hover:text-[var(--neutral-200)] px-3 py-1.5"
                >
                  Sign out
                </button>
              </>
            )}
          </div>
        </>
      )}
    </div>
  );
}
