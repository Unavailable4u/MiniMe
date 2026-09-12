// frontend/app/lib/clearLocalAppState.js
//
// BUGFIX (sign-out -> sign-in leaks the previous account's ids): every
// "remember the last X" feature in this app -- active chat, active tab,
// sidebar/dock collapse state, per-tab selected workspace, notebooks
// sub-tab, test-run history, and more -- is a plain localStorage key, and
// there are ~15 of them scattered across AppShell.jsx, SessionContext.jsx,
// WorkspaceDockContext.jsx, and every tab file. None of them were ever
// cleared on sign-out, which is the confirmed root cause of the 404s seen
// right after signing back in (a stale chat_id/workspace_id pulled out of
// storage on the very next mount, for a chat/workspace that doesn't belong
// to -- or doesn't exist for -- the newly signed-in account). A brand-new
// signup hits this even harder: it has no data of its own, so ANY leftover
// id from a previous session in the same browser is guaranteed invalid,
// not just sometimes stale.
//
// Every one of those keys already shares the "minime_" prefix (verified by
// grepping every localStorage.setItem/getItem call site in frontend/app),
// so a single prefix sweep clears all of them -- including any added later
// -- without call sites needing to name each key individually and drift
// out of sync as new ones get added.
//
// Extracted into its own module (rather than living inline in
// AuthContext.jsx) because there are TWO independent sign-out entry
// points in this app: AuthContext.jsx's signOut() (used by AccountMenu.jsx
// inside the real app at /app), and LandingPage.jsx's own vanilla-JS
// "Sign out" button (the marketing page at /, which deliberately doesn't
// use AuthContext at all -- see that file's header comment). Both call
// supabase.auth.signOut() directly and both need this same cleanup, so it
// lives somewhere both can import without one depending on the other.
export function clearLocalAppState() {
  if (typeof window === "undefined") return;
  const keysToClear = [];
  for (let i = 0; i < localStorage.length; i++) {
    const key = localStorage.key(i);
    if (!key) continue;
    if (key.startsWith("minime_")) keysToClear.push(key);
    // BUGFIX (stale pre-migration auth fossil): before supabaseClient.js
    // switched to @supabase/ssr's createBrowserClient(), the plain
    // supabase-js client wrote the session to localStorage under this
    // exact key. The live session now lives in a cookie instead, so
    // nothing ever updates or reads this key again — it just sits there
    // holding whatever (now long-expired) session was last active before
    // the migration, ready to confuse the next person who goes looking
    // for "the" access token in localStorage. Harmless to the app either
    // way (nothing reads it), but worth sweeping up on sign-out so it
    // doesn't outlive its usefulness in any given browser profile.
    if (key.startsWith("sb-") && key.endsWith("-auth-token")) keysToClear.push(key);
  }
  keysToClear.forEach((key) => localStorage.removeItem(key));
}
