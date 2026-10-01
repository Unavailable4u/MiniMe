// frontend/app/mm-inspector.js/route.js — W7.3 (Build Workbench plan).
// Serves GET /mm-inspector.js: the inspector runtime a person adds to
// THEIR OWN dev app (one <script> tag — the preview pane's "Set up
// click-to-code" panel shows it, built by previewUrl.js's
// buildInspectorSnippet()) so the URL preview can hover/click-select
// elements in it.
//
// The plan says `public/mm-inspector.js`. A route handler instead,
// because the runtime already EXISTS as code (inspectorRuntime.js's
// buildInspectorScript(), which every file preview injects): serving it
// from here means there is one source of truth and nothing generated to
// check in and keep in sync — `{external: true}` is that same function
// in its dev-app variant. `force-static` makes Next render it once at
// build time, so at runtime it is served exactly like a file in public/.
//
// It is meant to be loaded cross-origin by a plain <script src> from
// http://localhost:<port>, which needs no CORS headers. The script does
// nothing unless it is inside an iframe AND knows a parent origin to talk
// to (see the EXTERNAL MODE note in inspectorRuntime.js), so loading it
// in a normal tab is inert. middleware.js's matcher excludes this path:
// it is public, and running the session-refresh client for it would be
// pure overhead on every reload of someone's dev app.
import { buildInspectorScript } from "../lib/preview/inspectorRuntime";

export const dynamic = "force-static";

export function GET() {
  return new Response(buildInspectorScript("", { external: true }), {
    headers: {
      "Content-Type": "application/javascript; charset=utf-8",
      // Short: the script changes with MiniMe releases and a stale copy in
      // someone's dev app would quietly speak an old protocol.
      "Cache-Control": "public, max-age=300",
    },
  });
}
