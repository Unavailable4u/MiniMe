
// frontend/app/components/auth/LoginScreen.jsx
"use client";
import { useState } from "react";
import { useAuth } from "../../context/AuthContext";

export default function LoginScreen() {
  const { signInWithPassword, signUp, authError } = useAuth();
  const [mode, setMode] = useState("signin"); // "signin" | "signup"
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [notice, setNotice] = useState(null);

  async function handleSubmit(e) {
    e.preventDefault();
    setSubmitting(true);
    setNotice(null);
    try {
      if (mode === "signin") {
        await signInWithPassword(email, password);
        // No further action needed here — AuthProvider's onAuthStateChange
        // listener updates `session`, and page.js's Gate re-renders into
        // AppShell automatically once it does.
      } else {
        // full_name lands in Supabase's user_metadata immediately — see
        // AuthContext.signUp()'s comment on why this can't wait for a
        // profiles-table row (no session exists yet pre-confirmation).
        await signUp(email, password, name.trim() ? { full_name: name.trim() } : {});
        // Supabase's default project settings require email confirmation
        // before a new account can sign in — surface that explicitly
        // rather than leaving the user staring at an unchanged form.
        setNotice("Account created. Check your email to confirm it, then sign in.");
        setMode("signin");
      }
    } catch {
      // authError from context already holds a message; nothing else to do.
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="h-screen flex items-center justify-center px-4">
      <div className="w-full max-w-sm">
        <h1 className="text-lg font-medium text-[var(--neutral-300)] mb-1">MiniMe</h1>
        <p className="text-xs text-[var(--neutral-500)] mb-6">
          {mode === "signin" ? "Sign in to your account" : "Create an account"}
        </p>

        <form onSubmit={handleSubmit} className="space-y-3">
          {mode === "signup" && (
            <div>
              <label className="block text-xs text-[var(--neutral-500)] mb-1">Name</label>
              <input
                type="text"
                autoComplete="name"
                placeholder="Optional — shown to project collaborators"
                value={name}
                onChange={(e) => setName(e.target.value)}
                className="w-full rounded-lg border border-[var(--neutral-800)] bg-transparent px-3 py-2 text-sm text-[var(--neutral-200)] outline-none focus:border-[var(--accent)]"
              />
            </div>
          )}
          <div>
            <label className="block text-xs text-[var(--neutral-500)] mb-1">Email</label>
            <input
              type="email"
              required
              autoComplete="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="w-full rounded-lg border border-[var(--neutral-800)] bg-transparent px-3 py-2 text-sm text-[var(--neutral-200)] outline-none focus:border-[var(--accent)]"
            />
          </div>
          <div>
            <label className="block text-xs text-[var(--neutral-500)] mb-1">Password</label>
            <input
              type="password"
              required
              minLength={6}
              autoComplete={mode === "signin" ? "current-password" : "new-password"}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full rounded-lg border border-[var(--neutral-800)] bg-transparent px-3 py-2 text-sm text-[var(--neutral-200)] outline-none focus:border-[var(--accent)]"
            />
          </div>

          {authError && <p className="text-xs text-red-400">{authError}</p>}
          {notice && <p className="text-xs text-emerald-400">{notice}</p>}

          <button
            type="submit"
            disabled={submitting}
            className="w-full rounded-lg bg-[var(--accent)] text-[var(--accent-text)] text-sm font-medium py-2 disabled:opacity-50"
          >
            {submitting ? "…" : mode === "signin" ? "Sign in" : "Sign up"}
          </button>
        </form>

        <button
          type="button"
          onClick={() => {
            setMode(mode === "signin" ? "signup" : "signin");
            setNotice(null);
            setName("");
          }}
          className="mt-4 text-xs text-[var(--neutral-500)] hover:text-[var(--neutral-300)]"
        >
          {mode === "signin" ? "Need an account? Sign up" : "Already have an account? Sign in"}
        </button>
      </div>
    </div>
  );
}
