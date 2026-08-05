// This file configures the initialization of Sentry on the client (browser).
// The config you add here will be used whenever a users loads a page in their browser.
// https://docs.sentry.io/platforms/javascript/guides/nextjs/

import * as Sentry from "@sentry/nextjs";

const SENTRY_DSN = process.env.NEXT_PUBLIC_SENTRY_DSN;

// Same "fail quiet if not configured" convention as the backend init —
// only initialize if a DSN is actually set (safe default for local dev).
if (SENTRY_DSN) {
  Sentry.init({
    dsn: SENTRY_DSN,
    environment: process.env.NEXT_PUBLIC_ENVIRONMENT || "development",

    // Full performance tracing (matches backend's traces_sample_rate=1.0).
    tracesSampleRate: 1.0,

    // Session Replay is off by default — separate product, not requested.
    replaysSessionSampleRate: 0,
    replaysOnErrorSampleRate: 0,

    debug: false,
  });
}
