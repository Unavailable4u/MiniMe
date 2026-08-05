const { withSentryConfig } = require("@sentry/nextjs");

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
};

// withSentryConfig wraps the Next.js config to:
// - upload source maps to Sentry on build (needs SENTRY_ORG / SENTRY_PROJECT
//   / SENTRY_AUTH_TOKEN env vars in CI; builds still succeed without them,
//   just without source-mapped stack traces)
// - inject sentry.client.config.js into the client bundle
// - tunnel client events through /monitoring to dodge ad-blockers (disabled
//   below since it adds a rewrite rule most local setups don't need yet)
module.exports = withSentryConfig(nextConfig, {
  silent: true,
  org: process.env.SENTRY_ORG,
  project: process.env.SENTRY_PROJECT,
  authToken: process.env.SENTRY_AUTH_TOKEN,
  widenClientFileUpload: true,
  tunnelRoute: undefined,
  disableLogger: true,
  automaticVercelMonitors: false,
});
