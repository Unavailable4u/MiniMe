"use client";
import { useState } from "react";
import AppShell from "../AppShell";
import LoginScreen from "./LoginScreen";
import LoadingScreen from "../LoadingScreen";
import { useAuth } from "../../context/AuthContext";
import { BootProgressProvider, useBootProgress } from "../../context/BootProgressContext";

// Perf audit §2.3 step C: split out of page.js so page.js itself can be a
// Server Component (see its own comment) — this still has to be a client
// component because it reads useAuth() reactively, to catch a sign-out
// that happens mid-session (e.g. token expiry, or the user hitting
// "sign out" in AccountMenu) without a full page navigation. page.js's
// server-side check only ever runs once, on the initial request.
//
// NEW — branded loading screen: covers the transition from either
// "landing page -> /app" or "LoginScreen -> signed in" straight through
// to AppShell actually being ready, instead of the old bare "Loading…"
// text (still kept below as the pre-auth-resolved fallback) or, once
// signed in, nothing at all while AppShell's own fetches ran invisibly
// behind an empty screen. See BootProgressContext.jsx for how the splash
// knows when AppShell is really done, and LoadingScreen.jsx for the
// animation itself.
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

  // BootProgressProvider has to sit ABOVE AppShell (not inside it) so
  // AuthedShell below and AppShellBody (nested deep inside AppShell's own
  // provider stack) can both reach the same context instance — see
  // BootProgressContext.jsx's header comment.
  return (
    <BootProgressProvider>
      <AuthedShell />
    </BootProgressProvider>
  );
}

// Split out from the `if (!user)` branch above so it can call
// useBootProgress() — AppShell (and everything inside it, including
// SessionProvider) mounts here exactly as before, immediately once a
// real signed-in user exists, so its bootstrap fetches start firing
// right away. The splash sits on top of it, not in front of it: it
// unmounts itself (via onDone, once BootProgressContext reports `ready`
// and the splash's own reveal animation finishes) to hand off to the
// AppShell that's been loading underneath it the whole time — never a
// second, separate load after the splash goes away.
function AuthedShell() {
  const { progress, ready } = useBootProgress();
  const [splashDone, setSplashDone] = useState(false);

  return (
    <>
      <AppShell />
      {!splashDone && (
        <LoadingScreen progress={progress} ready={ready} onDone={() => setSplashDone(true)} />
      )}
    </>
  );
}
