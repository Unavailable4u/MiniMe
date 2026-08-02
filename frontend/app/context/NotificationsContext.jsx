"use client";
// frontend/app/context/NotificationsContext.jsx
//
// Audit §2.1 / Item 2 (remaining work), first slice of the "split
// SessionContext into concern-based contexts" fix. Extracted verbatim
// from SessionContext.jsx's Part 8.9 notification-inbox code: the
// `notifications`/`unreadCount` state, the user-{id} Pusher subscription
// that fills them, and `markNotificationsRead`.
//
// Picked as the first slice because it was already fully self-contained
// in the god-object — its only external dependency is useAuth() (not
// anything else on SessionContext), and its only consumer outside
// SessionContext.jsx is NotificationBell.jsx. That means this split has
// zero effect on any other consumer: nothing else in the tree re-renders
// when a notification arrives once this is its own context, and this
// context's subscribers no longer re-render on unrelated SessionContext
// changes (a new chat message, a usage tick, etc.) — which was exactly
// §2.1's complaint.
//
// Left as a plain memoized context (not yet useSyncExternalStore) to
// keep this slice's diff small and reviewable on its own. The
// WorkspaceDockContext.jsx pattern (per-key useSyncExternalStore store)
// is still the intended end state for the higher-traffic slices
// (chat/messages, live-run state) — see that file's own header comment —
// but a single always-mounted, app-wide inbox like this one doesn't need
// per-key subscriptions the way a per-dock store does, so plain Context +
// useMemo is the right amount of machinery here.
import { createContext, useContext, useState, useEffect, useCallback, useMemo } from "react";
import { getPusherClient } from "../lib/pusherClient";
import { useAuth } from "./AuthContext";

const NotificationsContext = createContext(null);

export function useNotifications() {
  const ctx = useContext(NotificationsContext);
  if (!ctx) throw new Error("useNotifications() must be used inside <NotificationsProvider>");
  return ctx;
}

export function NotificationsProvider({ children }) {
  const [notifications, setNotifications] = useState([]);   // NEW — Part 8.9: newest first
  const [unreadCount, setUnreadCount] = useState(0);          // NEW — Part 8.9

  // Part 8.4/8.9: subscription on the user's own channel (not the
  // current chat's) — only remounts when the signed-in user changes, not
  // on every switchChat(). Moved here unchanged from SessionContext.jsx.
  const { user } = useAuth();
  useEffect(() => {
    if (!user?.id) return;
    const pusher = getPusherClient();
    if (!pusher) return; // SettingsTab's pusherConnected diagnostic already covers the "not configured" case

    const channelName = `user-${user.id.replace(/[^A-Za-z0-9_=@,.;-]/g, "-")}`;
    const channel = pusher.subscribe(channelName);
    channel.bind("notification", (data) => {
      const note = {
        id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        kind: data?.payload?.kind,
        payload: data?.payload,
        timestamp: data?.timestamp || new Date().toISOString(),
        read: false,
      };
      setNotifications((prev) => [note, ...prev].slice(0, 50)); // cap, same reasoning usageHistory's 300-cap follows
      setUnreadCount((prev) => prev + 1);
    });
    return () => {
      pusher.unsubscribe(channelName);
    };
  }, [user?.id]);

  const markNotificationsRead = useCallback(() => {
    setUnreadCount(0);
    setNotifications((prev) => prev.map((n) => ({ ...n, read: true })));
  }, []);

  const value = useMemo(
    () => ({ notifications, unreadCount, markNotificationsRead }),
    [notifications, unreadCount, markNotificationsRead]
  );

  return (
    <NotificationsContext.Provider value={value}>
      {children}
    </NotificationsContext.Provider>
  );
}
