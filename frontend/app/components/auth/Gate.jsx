"use client";
import AppShell from "../AppShell";
import LoginScreen from "./LoginScreen";
import { useAuth } from "../../context/AuthContext";

// Perf audit §2.3 step C: split out of page.js so page.js itself can be a
// Server Component (see its own comment) — this still has to be a client
// component because it reads useAuth() reactively, to catch a sign-out
// that happens mid-session (e.g. token expiry, or the user hitting
// "sign out" in AccountMenu) without a full page navigation. page.js's
// server-side check only ever runs once, on the initial request.
export default function Gate() {
  const { authLoading, user } = useAuth();

  if (authLoading) {
    // With initialUser now seeded from page.js's server-side check (see
    // AuthContext.jsx), this only shows if that check somehow didn't
    // resolve to null/object — kept as a safety net, not the common path.
    return (
      <div className="h-screen flex items-center justify-center text-xs text-[var(--neutral-500)]">
        Loading…
      </div>
    );
  }

  if (!user) return <LoginScreen />;

  // AppShell (and everything inside it, including SessionProvider) only
  // ever mounts once a real signed-in user exists — SessionProvider's
  // own effects fire authenticated fetch() calls on mount, so it must
  // never mount before this point.
  return <AppShell />;
}
