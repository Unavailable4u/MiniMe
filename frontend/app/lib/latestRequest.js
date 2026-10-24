// frontend/app/lib/latestRequest.js
//
// Patch B05 (M2C Phase 0) — "latest request wins" guard.
//
// The problem this solves
// -----------------------
// BuildTab's board loads `/api/tasks/workspace/{id}` for the selected
// project. If the user switches project A -> B while A's response is still
// in flight, and A's (slower) response lands AFTER B's, the old code called
// setData() with A's payload and the board showed project A's tasks under
// project B's name. A `cancelled` flag in the effect cannot fix that on its
// own because refresh() is also called from other places (the Deploy and
// Monitoring cards, after an action), and those calls bypass the effect.
//
// How it works
// ------------
// A guard hands out a monotonically increasing token per request:
//
//     const token = guard.begin();          // right before the fetch
//     ...await the response...
//     if (!guard.isLatest(token)) return;   // a newer request started: drop
//
// Only the most recently begun request is "latest", so it does not matter in
// which order responses arrive. It covers both cases at once: a different
// project (the switch always begins a new request) and the same project
// refreshed twice (the newer one wins). invalidate() makes every in-flight
// request stale without starting a new one, which is what "no project
// selected any more" needs.
//
// Deliberately dependency-free (no imports) so plain `node` can load it via
// lib/workbench/__tests__/loadSource.mjs, like streamTicket.js. Pure and
// synchronous: it cannot throw and holds no timers.

/**
 * @returns {{ begin: () => number, isLatest: (token: number) => boolean, invalidate: () => void }}
 */
export function createLatestGuard() {
  let latest = 0;
  return {
    /** Start a new request; every earlier request becomes stale. */
    begin() {
      latest += 1;
      return latest;
    },
    /** True only for the token returned by the most recent begin(). */
    isLatest(token) {
      return token === latest;
    },
    /** Make all in-flight requests stale without starting a new one. */
    invalidate() {
      latest += 1;
    },
  };
}
