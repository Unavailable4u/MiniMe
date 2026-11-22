// frontend/app/lib/avatar.js
// Patch SX-03 -- avatar URL check for the landing-page nav.
//
// WHY THIS EXISTS: the nav avatar URL comes from the signed-in user's OAuth
// metadata (user_metadata.avatar_url / .picture), which the user can edit.
// components/LandingPage.jsx used to paste it into an HTML string
// (innerHTML = '<img src="' + url + '" />'), so a value such as
//     x" onerror="alert(1)
// closed the attribute and ran script on the landing page.
//
// LandingPage.jsx now builds the <img> element with createElement and only
// sets `src` from this function's result. This function is the second
// layer: it accepts a value only if it is a string that parses as an
// absolute https: URL, and returns the normalised form (new URL(...).href,
// which percent-encodes quotes, spaces and angle brackets). Anything else
// returns null and the nav falls back to the user's initial letter.
//
// No `import` lines on purpose: lib/__tests__/avatar.test.mjs loads this file
// through lib/workbench/__tests__/loadSource.mjs with no `imports` map, so an
// added import fails loudly (same rule as lib/streamTicket.js).

export function safeAvatarUrl(value) {
  if (typeof value !== "string") return null;
  var parsed;
  try {
    parsed = new URL(value);
  } catch (e) {
    return null; // not an absolute URL (this also covers "", `x" onerror=...`)
  }
  if (parsed.protocol !== "https:") return null;
  return parsed.href;
}
