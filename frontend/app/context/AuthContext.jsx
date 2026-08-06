
// frontend/app/context/AuthContext.jsx
//
// Part 8.9: real per-user auth, replacing the single shared static API key.
//
// Deliberately a SEPARATE context/provider from SessionContext.jsx, not a
// merge into it — SessionContext's "session" already means something else
// entirely in this codebase (sessionId === chat_id, see its own comments).
// Naming this "AuthContext"/useAuth() avoids that collision outright
// rather than overloading "session" to mean two unrelated things
// depending on which hook you're in.
//
// AuthProvider sits ABOVE SessionProvider in the tree (see page.js) and
// gates whether SessionProvider ever mounts at all — SessionProvider's
// own effects fire real authenticated fetch() calls on mount, so it must
// never mount before a real user is signed in.
"use client";
import { createContext, useContext, useState, useEffect } from "react";
import { supabase } from "../lib/supabaseClient";

const AuthContext = createContext(null);

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth() must be used inside <AuthProvider>");
  return ctx;
}

// initialUser — perf audit §2.3 step C: page.js is now a Server Component
// that checks the session cookie via supabaseServer() before anything
// client-side ever mounts, and passes the result down here. undefined
// (the default) means "no server check happened, resolve client-side as
// before"; null/object means "server already resolved this to
// signed-out/signed-in" and the initial client render should reflect
// that immediately instead of re-showing a loading state for a fetch
// that's redundant. page.js always passes one of null/object, never
// undefined, so this codepath below only matters for any other caller
// that mounts AuthProvider without doing that check first.
function initialSessionFor(initialUser) {
  if (initialUser === undefined) return undefined;
  return initialUser ? { user: initialUser } : null;
}

export function AuthProvider({ children, initialUser }) {
  // undefined = auth state not yet resolved (first paint); null = resolved,
  // signed out; object = resolved, signed in. Kept distinct from `null`
  // deliberately so the Gate component (components/auth/Gate.jsx) can show
  // a loading state instead of flashing the login screen for a moment on
  // every reload — though with initialUser now seeded from the server,
  // that loading state is no longer the common path (see above).
  const [session, setSession] = useState(() => initialSessionFor(initialUser));
  const [authError, setAuthError] = useState(null);

  useEffect(() => {
    supabase.auth.getSession().then(({ data }) => setSession(data.session));

    const { data: listener } = supabase.auth.onAuthStateChange((_event, newSession) => {
      setSession(newSession);
    });
    return () => listener.subscription.unsubscribe();
  }, []);

  async function signInWithPassword(email, password) {
    setAuthError(null);
    const { error } = await supabase.auth.signInWithPassword({ email, password });
    if (error) {
      setAuthError(error.message);
      throw error;
    }
  }

  // metadata (optional): e.g. { full_name }. Written straight into
  // Supabase's user_metadata at signup time via the `options.data` field —
  // this lands immediately regardless of whether email confirmation is
  // required, unlike a profiles-table row would (no session/JWT exists
  // yet to write one with until the user actually confirms and signs in).
  // api/server.py's _lookup_users_by_ids() already reads
  // full_name/name/avatar_url out of this same user_metadata, so nothing
  // downstream needs to change to pick this up.
  async function signUp(email, password, metadata = {}) {
    setAuthError(null);
    const { error } = await supabase.auth.signUp({
      email,
      password,
      options: { data: metadata },
    });
    if (error) {
      setAuthError(error.message);
      throw error;
    }
  }

  async function signOut() {
    await supabase.auth.signOut();
  }

  // Lets a SIGNED-IN user set/change their own display name and avatar
  // later (e.g. from AccountMenu), independent of how they originally
  // signed up. Writes to the same user_metadata bucket signUp() seeds
  // above — one identity source, not a second one to keep in sync.
  // supabase-js's updateUser() only ever touches the CALLER's own row
  // (scoped by their session token), so no backend involvement or extra
  // permission check is needed here.
  async function updateProfile({ displayName, avatarUrl } = {}) {
    setAuthError(null);
    const data = {};
    if (displayName !== undefined) data.full_name = displayName;
    if (avatarUrl !== undefined) data.avatar_url = avatarUrl;
    const { error } = await supabase.auth.updateUser({ data });
    if (error) {
      setAuthError(error.message);
      throw error;
    }
    // updateUser() triggers onAuthStateChange with the refreshed user,
    // so `session`/`user` below update automatically — no manual refetch.
  }

  const value = {
    session,
    user: session?.user ?? null,
    authLoading: session === undefined,
    authError,
    signInWithPassword,
    signUp,
    signOut,
    updateProfile,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
