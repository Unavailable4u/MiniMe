// Next.js instrumentation hook (stable since Next 13.4, no config flag
// needed). This is what actually loads sentry.server.config.js and
// sentry.edge.config.js — without it those two files are inert.
// sentry.client.config.js does NOT go through here; withSentryConfig's
// webpack plugin injects that one into the client bundle directly.
// https://docs.sentry.io/platforms/javascript/guides/nextjs/manual-setup/

export async function register() {
  if (process.env.NEXT_RUNTIME === "nodejs") {
    await import("./sentry.server.config");
  }

  if (process.env.NEXT_RUNTIME === "edge") {
    await import("./sentry.edge.config");
  }
}
