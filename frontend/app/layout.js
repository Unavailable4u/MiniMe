import "./globals.css";
import * as Sentry from "@sentry/nextjs";

export function generateMetadata() {
  return {
    title: "MiniMe",
    description: "EO-gated multi-agent system — chat interface",
    other: {
      ...Sentry.getTraceData(),
    },
  };
}

// NEW — mobile keyboard/notch fix. There was no viewport export at all
// before this (app/page.js — the marketing landing page, a different
// route — has its own, but that doesn't apply to "/app", which inherits
// straight from this root layout with Next's bare default). Two things
// that default was missing, both load-bearing for AppShell.jsx's
// mobile shell:
//   - viewportFit: "cover" — lets the page draw under a notch/home-
//     indicator instead of leaving a hard-coded system-UI-colored bar,
//     the prerequisite for using `env(safe-area-inset-*)` at all.
//   - interactiveWidget: "resizes-content" — tells the (Chromium/
//     Android) browser to actually shrink the visual viewport when the
//     on-screen keyboard opens, rather than overlaying it on top of
//     unshrunk content. Combined with globals.css's `app-shell-viewport`
//     (100dvh, further refined by AppShell.jsx's visualViewport effect)
//     this is what stops the composer's bottom edge from ending up
//     underneath the keyboard. iOS Safari has no equivalent meta value
//     (it doesn't support interactive-widget) — the visualViewport
//     effect is what covers it there instead.
export const viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
  interactiveWidget: "resizes-content",
};

export default function RootLayout({ children }) {
  return (
    // suppressHydrationWarning: useDensity.js applies the saved
    // comfortable/compact preference to document.documentElement as
    // soon as the module is imported client-side (to avoid a flash of
    // the wrong density), which the server can never know about ahead
    // of time -- this is an intentional, expected mismatch on <html>.
    <html lang="en" suppressHydrationWarning>
      <body className="min-h-screen">{children}</body>
    </html>
  );
}