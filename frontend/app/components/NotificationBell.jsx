// frontend/app/components/NotificationBell.jsx
// NEW — Part 8.9: subscribes (via SessionContext) to the user-{user_id}
// Pusher channel added in relay/emitter.py's emit_user_event(). Purely a
// live, in-session inbox today — there's no GET /api/notifications
// backing this yet, so a page reload clears it. That's a real, known
// limitation (flag it if this needs to survive reloads), not an
// oversight: §8.4's definition of done only asks for the notification
// to fire on the channel, not for persisted history.
"use client";
import { useState } from "react";
import { Bell } from "lucide-react";
import { useSession } from "../context/SessionContext";

const KIND_LABELS = {
  note_proposed: "New note proposed",
};

function describe(note) {
  const label = KIND_LABELS[note.kind] || note.kind || "Notification";
  const title = note.payload?.title;
  return title ? `${label}: "${title}"` : label;
}

function timeAgo(iso) {
  const ms = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(ms / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

export default function NotificationBell({ onOpenChat }) {
  const { notifications, unreadCount, markNotificationsRead } = useSession();
  const [open, setOpen] = useState(false);

  function toggle() {
    setOpen((o) => {
      const next = !o;
      if (next) markNotificationsRead(); // opening the inbox IS reading it, same convention as most bell/inbox UIs
      return next;
    });
  }

  return (
    <div className="relative">
      <button
        type="button"
        onClick={toggle}
        className="relative text-[var(--neutral-500)] hover:text-[var(--neutral-300)] rounded-lg p-1.5"
        title="Notifications"
      >
        <Bell size={16} />
        {unreadCount > 0 && (
          <span className="absolute -top-0.5 -right-0.5 min-w-[14px] h-[14px] px-[3px] rounded-full bg-[var(--accent)] text-[var(--accent-text)] text-[9px] leading-[14px] text-center font-medium">
            {unreadCount > 9 ? "9+" : unreadCount}
          </span>
        )}
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div className="absolute right-0 mt-1 w-80 max-h-96 overflow-y-auto rounded-lg border border-[var(--neutral-800)] bg-[var(--neutral-900)] shadow-lg py-1 z-50">
            <div className="px-3 py-1.5 text-[10px] uppercase tracking-wide text-[var(--neutral-600)]">
              Notifications
            </div>
            {notifications.length === 0 ? (
              <p className="px-3 py-4 text-xs text-[var(--neutral-600)]">
                Nothing yet — proposals and shared-workspace activity from
                other chats will show up here.
              </p>
            ) : (
              notifications.map((note) => (
                <button
                  key={note.id}
                  type="button"
                  onClick={() => {
                    // note_proposed points at a workspace, not a chat directly —
                    // there's no chat_id to hand onOpenChat here, so this just
                    // closes the inbox for now. Wire this to the Notebooks tab
                    // (filtered to note.payload.workspace_id) once that view
                    // supports deep-linking.
                    setOpen(false);
                  }}
                  className="w-full text-left px-3 py-2 hover:bg-[var(--neutral-800)] border-t border-[var(--neutral-850)] first:border-t-0"
                >
                  <p className="text-xs text-[var(--neutral-200)]">{describe(note)}</p>
                  <p className="text-[10px] text-[var(--neutral-600)] mt-0.5">
                    {note.payload?.proposed_by ? `${note.payload.proposed_by} · ` : ""}
                    {timeAgo(note.timestamp)}
                  </p>
                </button>
              ))
            )}
          </div>
        </>
      )}
    </div>
  );
}
