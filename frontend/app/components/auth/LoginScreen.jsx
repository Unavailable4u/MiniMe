
// frontend/app/components/auth/LoginScreen.jsx
"use client";
import { useState } from "react";
import {
  Terminal,
  KeyRound,
  Eye,
  EyeOff,
  Zap,
  ArrowRight,
  UserPlus,
  UserRound,
  ChevronRight,
  Check,
  LogIn,
} from "lucide-react";
import { useAuth } from "../../context/AuthContext";

// ---------------------------------------------------------------------
// "Auth Matrix" redesign — Operator Login / Registration screen.
//
// Deliberately styled with its own arbitrary-value palette (surface
// #0b0e14, primary-container #ff5168, secondary-container #00eefc,
// tertiary-container #b56eff, etc.) instead of the app-wide --cyber-*
// / --neutral-* tokens in globals.css. This is a scoped visual upgrade
// for just this one screen — the rest of the app stays on the cyan
// cyber theme for now. When the whole interface gets rebuilt on this
// palette later, these hexes (matching the design source's DESIGN.md)
// are the values to promote into globals.css/tailwind.config.js.
//
// SSO buttons (Google/Facebook/GitHub/LinkedIn) are intentionally
// inert — AuthContext.jsx only implements email/password auth via
// Supabase right now, no OAuth providers are wired up. Kept as plain
// `type="button"` with no onClick so the design renders exactly as
// given; wire these up (or remove the ones you don't want) once the
// corresponding Supabase provider is configured.
// ---------------------------------------------------------------------

const inputClass =
  "w-full bg-[#0b0e14] focus:bg-[#191c22] text-[#e1e2eb] placeholder:text-[#ae8788] font-['JetBrains_Mono'] text-[13px] tracking-[0.04em] font-semibold pl-10 pr-4 py-2.5 rounded transition-all outline-none shadow-[inset_0_2px_6px_rgba(0,0,0,0.6)]";

const ssoBigClass =
  "group relative flex items-center justify-center gap-2 px-3 py-2.5 bg-[#272a31] hover:bg-[#363940] text-[#e1e2eb] transition-all rounded shadow-[0_2px_12px_rgba(0,0,0,0.3)]";

const ssoSmallClass =
  "group flex items-center justify-center gap-1.5 px-2 py-1.5 bg-[#1d2026] hover:bg-[#272a31] transition-colors rounded";

export default function LoginScreen() {
  const { signInWithPassword, signUp, authError } = useAuth();
  const [mode, setMode] = useState("signin"); // "signin" | "signup"
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [remember, setRemember] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [notice, setNotice] = useState(null);

  const isSignup = mode === "signup";

  function switchMode() {
    setMode(isSignup ? "signin" : "signup");
    setNotice(null);
    setName("");
  }

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
    <div className="h-screen w-full flex items-center justify-center px-4 overflow-hidden relative bg-[#0b0e14]">
      {/* Ambient cyber glows */}
      <div className="absolute top-1/4 left-1/2 -translate-x-1/2 -translate-y-1/2 w-[680px] h-[680px] bg-[#ff5168]/10 rounded-full blur-[140px] pointer-events-none" />
      <div className="absolute bottom-10 left-10 w-96 h-96 bg-[#00eefc]/5 rounded-full blur-[110px] pointer-events-none" />
      <div className="absolute top-20 right-10 w-80 h-80 bg-[#b56eff]/10 rounded-full blur-[100px] pointer-events-none" />

      {/* key={mode} forces a remount so .auth-fade-in replays on every
          sign in / sign up toggle, not just on first page load. */}
      <div key={mode} className="auth-fade-in relative w-full max-w-[480px] z-10">
        <div className="relative w-full bg-[#0b0e14]/90 backdrop-blur-2xl rounded shadow-[0_0_50px_rgba(255,81,104,0.12)] overflow-hidden border border-white/5">
          {/* HUD ribbon — dot/label color flips crimson↔cyan between
              sign-in and sign-up so the mode swap reads at a glance. */}
          <div className="w-full bg-[#191c22] px-4 py-2 flex items-center gap-2">
            <span
              className={`w-2 h-2 rounded-full animate-pulse ${
                isSignup ? "bg-[#00eefc]" : "bg-[#ff5168]"
              }`}
            />
            <span
              className={`font-['JetBrains_Mono'] text-[10px] tracking-[0.08em] uppercase ${
                isSignup ? "text-[#00eefc]" : "text-[#ff5168]"
              }`}
            >
              {isSignup
                ? "New Operator Registration // Auth Matrix v0.880"
                : "Operator Login // Auth Matrix v0.880"}
            </span>
          </div>

          <div className="p-6 md:p-8 flex flex-col gap-6">
            {/* Brand */}
            <div className="flex flex-col items-center text-center gap-2">
              <div className="relative flex items-center justify-center">
                <div className="absolute inset-0 bg-[#ff5168]/30 blur-xl rounded-full" />
                <img
                  src="/minime-logo.svg"
                  alt="MiniMe"
                  className="relative w-16 h-16 object-contain rounded drop-shadow-[0_0_15px_rgba(255,81,104,0.6)]"
                />
              </div>
              <div className="flex flex-col items-center gap-0.5">
                <div className="flex items-center gap-1.5">
                  <span className="font-['Syne'] text-[24px] md:text-[32px] leading-[30px] md:leading-[40px] font-bold uppercase tracking-tight text-[#e1e2eb]">
                    MINI ME
                  </span>
                  <span className="font-['Syne'] text-[24px] md:text-[32px] leading-[30px] md:leading-[40px] font-bold uppercase text-[#ff5168] drop-shadow-[0_0_12px_rgba(255,81,104,0.7)]">
                    AI
                  </span>
                </div>
                <p className="font-['JetBrains_Mono'] text-[10px] tracking-[0.08em] uppercase text-[#ae8788]">
                  Engineering-Caliber Intelligence
                </p>
              </div>
            </div>

            {/* SSO grid (design-only, see file header note) */}
            <div className="flex flex-col gap-2">
              <div className="grid grid-cols-2 gap-2">
                <button type="button" className={ssoBigClass}>
                  <span className="absolute inset-x-0 bottom-0 h-0.5 bg-[#ff5168] scale-x-0 group-hover:scale-x-100 transition-transform origin-center" />
                  <GoogleIcon className="w-5 h-5 group-hover:drop-shadow-[0_0_8px_#ff5168] transition-all" />
                  <span className="font-['JetBrains_Mono'] text-[13px] tracking-[0.04em] font-semibold uppercase">
                    Google
                  </span>
                </button>
                <button type="button" className={ssoBigClass}>
                  <span className="absolute inset-x-0 bottom-0 h-0.5 bg-[#d3fbff] scale-x-0 group-hover:scale-x-100 transition-transform origin-center" />
                  <FacebookIcon className="w-5 h-5 fill-[#1877F2] group-hover:drop-shadow-[0_0_8px_#1877F2] transition-all" />
                  <span className="font-['JetBrains_Mono'] text-[13px] tracking-[0.04em] font-semibold uppercase">
                    Facebook
                  </span>
                </button>
              </div>
              <div className="grid grid-cols-2 gap-2">
                <button type="button" className={ssoSmallClass}>
                  <GitHubIcon className="w-4 h-4 fill-[#e1e2eb] group-hover:drop-shadow-[0_0_6px_#ffffff] transition-all" />
                  <span className="font-['JetBrains_Mono'] text-[10px] tracking-[0.08em] uppercase text-[#e7bcbd]">
                    GitHub
                  </span>
                </button>
                <button type="button" className={ssoSmallClass}>
                  <LinkedInIcon className="w-4 h-4 fill-[#0A66C2] group-hover:drop-shadow-[0_0_6px_#0A66C2] transition-all" />
                  <span className="font-['JetBrains_Mono'] text-[10px] tracking-[0.08em] uppercase text-[#e7bcbd]">
                    LinkedIn
                  </span>
                </button>
              </div>
            </div>

            {/* Divider */}
            <div className="relative flex items-center justify-center">
              <div className="w-full h-px bg-[#272a31]" />
              <span className="absolute bg-[#0b0e14] px-2 font-['JetBrains_Mono'] text-[10px] tracking-[0.08em] text-[#ae8788] uppercase">
                Or authenticate with neural credentials
              </span>
            </div>

            {/* Form */}
            <form onSubmit={handleSubmit} className="flex flex-col gap-4">
              {isSignup && (
                <div className="flex flex-col gap-1">
                  <div className="flex items-center justify-between">
                    <label
                      htmlFor="login-name"
                      className="font-['JetBrains_Mono'] text-[11px] tracking-[0.12em] font-bold uppercase text-[#e7bcbd] flex items-center gap-1.5"
                    >
                      <span className="w-1.5 h-1.5 bg-[#b56eff] rounded-full" />
                      Operator Callsign
                    </label>
                    <span className="font-['JetBrains_Mono'] text-[10px] tracking-[0.08em] text-[#ae8788]">
                      OPTIONAL
                    </span>
                  </div>
                  <div className="relative flex items-center">
                    <div className="absolute left-3.5 flex items-center pointer-events-none text-[#b56eff]">
                      <UserRound size={16} />
                    </div>
                    <input
                      id="login-name"
                      type="text"
                      autoComplete="name"
                      placeholder="operator_09"
                      value={name}
                      onChange={(e) => setName(e.target.value)}
                      className={`${inputClass} focus:ring-1 focus:ring-[#b56eff]`}
                    />
                  </div>
                </div>
              )}

              <div className="flex flex-col gap-1">
                <div className="flex items-center justify-between">
                  <label
                    htmlFor="login-email"
                    className="font-['JetBrains_Mono'] text-[11px] tracking-[0.12em] font-bold uppercase text-[#e7bcbd] flex items-center gap-1.5"
                  >
                    <span className="w-1.5 h-1.5 bg-[#ff5168] rounded-full" />
                    Email Address/Username
                  </label>
                  <span className="font-['JetBrains_Mono'] text-[10px] tracking-[0.08em] text-[#ae8788]">
                    HEX://ADDR
                  </span>
                </div>
                <div className="relative flex items-center">
                  <div className="absolute left-3.5 flex items-center pointer-events-none text-[#ffb3b6]">
                    <Terminal size={16} />
                  </div>
                  <input
                    id="login-email"
                    type="email"
                    required
                    autoComplete="email"
                    placeholder="operator_09@gx.aether.ai"
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    className={`${inputClass} focus:ring-1 focus:ring-[#ff5168]`}
                  />
                </div>
              </div>

              <div className="flex flex-col gap-1">
                <div className="flex items-center justify-between">
                  <label
                    htmlFor="login-password"
                    className="font-['JetBrains_Mono'] text-[11px] tracking-[0.12em] font-bold uppercase text-[#e7bcbd] flex items-center gap-1.5"
                  >
                    <span className="w-1.5 h-1.5 bg-[#00eefc] rounded-full" />
                    Passkey
                  </label>
                  {!isSignup && (
                    <a
                      href="#"
                      className="font-['JetBrains_Mono'] text-[10px] tracking-[0.08em] text-[#ffb3b6] hover:text-[#ff5168] transition-colors uppercase"
                    >
                      Forgot password?
                    </a>
                  )}
                </div>
                <div className="relative flex items-center">
                  <div className="absolute left-3.5 flex items-center pointer-events-none text-[#00eefc]">
                    <KeyRound size={16} />
                  </div>
                  <input
                    id="login-password"
                    type={showPassword ? "text" : "password"}
                    required
                    minLength={6}
                    autoComplete={isSignup ? "new-password" : "current-password"}
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    className={`${inputClass} pr-10 focus:ring-1 focus:ring-[#00eefc]`}
                  />
                  <button
                    type="button"
                    onClick={() => setShowPassword((v) => !v)}
                    className="absolute right-3.5 flex items-center text-[#ae8788] hover:text-[#e1e2eb] transition-colors"
                    aria-label={showPassword ? "Hide password" : "Show password"}
                  >
                    {showPassword ? <EyeOff size={16} /> : <Eye size={16} />}
                  </button>
                </div>
              </div>

              {!isSignup && (
                <div className="flex items-center justify-between pt-0.5">
                  <label className="relative flex items-center gap-2 cursor-pointer select-none">
                    <input
                      type="checkbox"
                      checked={remember}
                      onChange={(e) => setRemember(e.target.checked)}
                      className="peer sr-only"
                    />
                    <div className="w-4 h-4 bg-[#1d2026] peer-checked:bg-[#ff5168] flex items-center justify-center rounded-[2px] transition-colors">
                      <Check
                        size={12}
                        strokeWidth={3}
                        className={`text-[#68001a] transition-transform ${
                          remember ? "scale-100" : "scale-0"
                        }`}
                      />
                    </div>
                    <span className="font-['JetBrains_Mono'] text-[10px] tracking-[0.08em] text-[#e1e2eb] uppercase">
                      Remember neural session for 30 days
                    </span>
                  </label>
                </div>
              )}

              {authError && (
                <p className="font-['JetBrains_Mono'] text-[11px] tracking-wide text-[#ffb4ab]">
                  {authError}
                </p>
              )}
              {notice && (
                <p className="font-['JetBrains_Mono'] text-[11px] tracking-wide text-[#00eefc]">
                  {notice}
                </p>
              )}

              <button
                type="submit"
                disabled={submitting}
                className="group relative w-full mt-1 py-2.5 px-6 bg-gradient-to-r from-[#ff5168] to-[#be0037] hover:to-[#ff5168] text-[#68001a] font-['JetBrains_Mono'] text-[11px] tracking-[0.12em] font-bold uppercase rounded transition-all shadow-[0_0_24px_rgba(255,81,104,0.45)] hover:shadow-[0_0_32px_rgba(255,81,104,0.7)] flex items-center justify-center gap-2 overflow-hidden disabled:opacity-50 disabled:cursor-not-allowed"
              >
                <span className="absolute top-0 left-0 w-full h-px bg-[#ffb3b6]/40" />
                {isSignup ? (
                  <UserPlus size={16} className="group-hover:scale-110 transition-transform" />
                ) : (
                  <Zap size={16} className="group-hover:rotate-45 transition-transform" />
                )}
                <span>
                  {submitting
                    ? "Processing…"
                    : isSignup
                    ? "Register Operator // Create Account"
                    : "Initialize Workspace // Sign In"}
                </span>
                <ArrowRight size={16} className="group-hover:translate-x-1 transition-transform" />
              </button>

              <div className="flex flex-col items-center gap-1.5 pt-1">
                <button
                  type="button"
                  onClick={switchMode}
                  className="group relative w-full py-2.5 px-6 bg-[#1d2026] hover:bg-[#272a31] text-[#e1e2eb] hover:text-[#00dbe9] font-['JetBrains_Mono'] text-[11px] tracking-[0.12em] font-bold uppercase rounded transition-all shadow-[0_2px_12px_rgba(0,0,0,0.3)] flex items-center justify-center gap-2"
                >
                  {isSignup ? (
                    <LogIn size={16} className="text-[#00eefc]" />
                  ) : (
                    <UserPlus size={16} className="text-[#00eefc]" />
                  )}
                  <span>
                    {isSignup
                      ? "Already an Operator? // Sign In"
                      : "Register New Operator // Sign Up"}
                  </span>
                  <ChevronRight size={16} className="group-hover:translate-x-1 transition-transform" />
                </button>
                <span className="w-1.5 h-1.5 rounded-full bg-[#00eefc] animate-pulse" />
              </div>
            </form>
          </div>
        </div>
      </div>
    </div>
  );
}

// Inline SSO brand marks — copied verbatim from the design source's
// code.html so colors/paths match the mockup exactly, just wrapped as
// components instead of raw <svg> blocks.
function GoogleIcon({ className }) {
  return (
    <svg className={className} viewBox="0 0 24 24">
      <path fill="#EA4335" d="M12 5c1.6 0 3 .6 4.1 1.6l3.1-3.1C17.3 1.7 14.8 1 12 1 7.4 1 3.5 3.6 1.6 7.4l3.7 2.9C6.2 7.3 8.9 5 12 5z" />
      <path fill="#4285F4" d="M23.5 12.3c0-.8-.1-1.7-.2-2.3H12v4.6h6.5c-.3 1.5-1.1 2.8-2.4 3.7l3.7 2.9c2.2-2 3.7-5.1 3.7-8.9z" />
      <path fill="#FBBC05" d="M5.3 14.7c-.2-.7-.4-1.5-.4-2.3s.2-1.6.4-2.3L1.6 7.2C.6 9.2 0 11.5 0 14s.6 4.8 1.6 6.8l3.7-2.9z" />
      <path fill="#34A853" d="M12 23c3.2 0 6-1.1 8-3l-3.7-2.9c-1.1.7-2.5 1.2-4.3 1.2-3.1 0-5.8-2.3-6.7-5.3L1.6 15.9C3.5 19.7 7.4 22.3 12 23z" />
    </svg>
  );
}

function FacebookIcon({ className }) {
  return (
    <svg className={className} viewBox="0 0 24 24">
      <path d="M24 12.073c0-6.627-5.373-12-12-12s-12 5.373-12 12c0 5.99 4.388 10.954 10.125 11.854v-8.385H7.078v-3.47h3.047V9.43c0-3.007 1.792-4.669 4.533-4.669 1.312 0 2.686.235 2.686.235v2.953H15.83c-1.491 0-1.956.925-1.956 1.874v2.25h3.328l-.532 3.47h-2.796v8.385C19.612 23.027 24 18.062 24 12.073z" />
    </svg>
  );
}

function GitHubIcon({ className }) {
  return (
    <svg className={className} viewBox="0 0 24 24">
      <path
        fillRule="evenodd"
        clipRule="evenodd"
        d="M12 2C6.477 2 2 6.484 2 12.017c0 4.425 2.865 8.18 6.839 9.504.5.092.682-.217.682-.483 0-.237-.008-.868-.013-1.703-2.782.605-3.369-1.343-3.369-1.343-.454-1.158-1.11-1.466-1.11-1.466-.908-.62.069-.608.069-.608 1.003.07 1.53 1.032 1.53 1.032.892 1.53 2.341 1.088 2.91.832.092-.647.35-1.088.636-1.338-2.22-.253-4.555-1.113-4.555-4.951 0-1.093.39-1.988 1.029-2.688-.103-.253-.446-1.272.098-2.65 0 0 .84-.27 2.75 1.026A9.564 9.564 0 0112 6.844c.85.004 1.705.115 2.504.337 1.909-1.296 2.747-1.027 2.747-1.027.546 1.379.202 2.398.1 2.651.64.7 1.028 1.595 1.028 2.688 0 3.848-2.339 4.695-4.566 4.943.359.309.678.92.678 1.855 0 1.338-.012 2.419-.012 2.747 0 .268.18.58.688.482A10.019 10.019 0 0022 12.017C22 6.484 17.522 2 12 2z"
      />
    </svg>
  );
}

function LinkedInIcon({ className }) {
  return (
    <svg className={className} viewBox="0 0 24 24">
      <path d="M19 3a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h14m-.5 15.5v-5.3a3.26 3.26 0 0 0-3.26-3.26c-.85 0-1.84.52-2.28 1.3v-1.11h-2.79v8.37h2.79v-4.93c0-.77.62-1.4 1.39-1.4a1.4 1.4 0 0 1 1.4 1.4v4.93h2.75M6.88 8.56a1.68 1.68 0 0 0 1.68-1.68c0-.93-.75-1.69-1.68-1.69a1.69 1.69 0 0 0-1.69 1.69c0 .93.76 1.68 1.69 1.68m1.39 9.94v-8.37H5.5v8.37h2.77z" />
    </svg>
  );
}
