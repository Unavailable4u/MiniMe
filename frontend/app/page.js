"use client";
import AppShell from "./components/AppShell";
import LoginScreen from "./components/auth/LoginScreen";
import { AuthProvider, useAuth } from "./context/AuthContext";

// Split out of Page() so it can sit inside AuthProvider and call
// useAuth() directly — same reasoning AppShellBody() already documents
// for its own split from AppShell().
function Gate() {
  const { authLoading, user } = useAuth();

  if (authLoading) {
    // Deliberately brief/neutral — this shows for a moment on every
    // reload while supabase-js resolves the persisted session, not a
    // real loading state a user should ever dwell on.
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

export default function Page() {
  return (
    <AuthProvider>
      <Gate />
    </AuthProvider>
  );
}
