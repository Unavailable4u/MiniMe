// frontend/securityHeaders.js
// Patch SX-28 -- basic security headers for every page of the frontend.
//
// WHY THIS IS A SEPARATE FILE: next.config.js pulls in @sentry/nextjs and
// cannot be loaded by a plain `node --test` run. This module has no
// dependencies, so next.config.js `require`s it and
// app/lib/__tests__/securityHeaders.test.mjs checks the exact values.
//
// WHAT EACH HEADER DOES
//   X-Content-Type-Options: nosniff
//       The browser must trust the Content-Type we send and not guess a
//       "better" type (for example run a .txt file as script).
//   Referrer-Policy: strict-origin-when-cross-origin
//       Other sites get only our origin (no path or query) and only over
//       https; same-site requests still get the full URL.
//   X-Frame-Options: DENY
//       No other site can put this app in an iframe (clickjacking). This does
//       NOT affect the iframes the app itself creates (srcDoc previews, the
//       URL preview): the header is about who may frame *our* pages.
//       SX-29 adds the modern equivalent, CSP frame-ancestors 'none'.
//   Permissions-Policy: camera=(), geolocation=(), microphone=()
//       Turns off these three browser features for the app and for any
//       iframe inside it. At the time of this patch nothing in app/ calls
//       getUserMedia, MediaRecorder or geolocation (voice notes are uploaded
//       as audio FILES through /api/notes/voice), so nothing breaks. If a
//       recording feature is added later, change microphone=() to
//       microphone=(self) here and update the test.
//
// NOT HERE: Strict-Transport-Security (HSTS). That is set at the reverse
// proxy, so a local `next dev` over http is never told to force https.
// Content-Security-Policy is SX-29.

const securityHeaders = [
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "X-Frame-Options", value: "DENY" },
  {
    key: "Permissions-Policy",
    value: "camera=(), geolocation=(), microphone=()",
  },
];

module.exports = { securityHeaders };
