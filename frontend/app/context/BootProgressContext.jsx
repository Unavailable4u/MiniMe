"use client";
// frontend/app/context/BootProgressContext.jsx
//
// Backs the branded splash screen (LoadingScreen.jsx) that covers the
// screen from the moment a user is signed in until AppShell has actually
// finished its first-load bootstrap. Exists as its own top-level context
// (rather than living inside AppShell's own provider stack, or being
// prop-drilled) because the two things that need it sit on opposite
// sides of AppShell's tree: Gate.jsx (an ANCESTOR of AppShell) needs to
// read {progress, ready} to drive the splash, while AppShellBody (a
// DESCENDANT, nested inside NotificationsProvider > UsageStatsProvider >
// WorkspacesProvider > ChatListProvider > SessionProvider >
// WorkspaceDockProvider) is the thing that actually knows when each
// bootstrap fetch resolves and needs to call markTaskDone(). A plain
// context whose Provider Gate.jsx renders ABOVE <AppShell/> lets both
// ends `useContext` the same instance without threading a callback down
// through five unrelated provider layers.
//
// Tracked tasks intentionally mirror AppShellBody's own mount effect
// exactly (see AppShell.jsx: fetchBatches, fetchWorkspaces, and the
// refreshChatList -> switchChat/createNewChat chain) — this file doesn't
// invent its own idea of "loaded," it just counts the same three real
// network round trips AppShell was already waiting on before this
// existed. Add a key here (and a matching markTaskDone call in
// AppShell.jsx) if a future bootstrap step should also gate the splash.
import { createContext, useContext, useState, useMemo, useCallback } from "react";

const BootProgressContext = createContext(null);

const TASK_IDS = ["batches", "workspaces", "chatBootstrap"];

export function BootProgressProvider({ children }) {
  const [doneTasks, setDoneTasks] = useState(() => new Set());

  // Fires once per task id no matter how many times it's called (a
  // failed fetch's .finally() and its own retry logic could both call
  // this for the same id) — Set dedupes for free, and the no-op early
  // return skips a state update entirely when nothing actually changed.
  const markTaskDone = useCallback((taskId) => {
    setDoneTasks((prev) => {
      if (prev.has(taskId)) return prev;
      const next = new Set(prev);
      next.add(taskId);
      return next;
    });
  }, []);

  const progress = Math.min(100, Math.round((doneTasks.size / TASK_IDS.length) * 100));
  const ready = doneTasks.size >= TASK_IDS.length;

  const value = useMemo(() => ({ progress, ready, markTaskDone }), [progress, ready, markTaskDone]);

  return <BootProgressContext.Provider value={value}>{children}</BootProgressContext.Provider>;
}

export function useBootProgress() {
  const ctx = useContext(BootProgressContext);
  // No provider above (shouldn't happen given how Gate.jsx is wired, but
  // safer than crashing): report as already done rather than leaving a
  // caller waiting on a signal that will never arrive.
  return ctx || { progress: 100, ready: true, markTaskDone: () => {} };
}
