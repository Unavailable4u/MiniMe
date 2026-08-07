const { withSentryConfig } = require("@sentry/nextjs");

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  webpack: (config) => {
    // Known upstream issue in @sentry/nextjs v10: it bundles
    // @apm-js-collab/tracing-hooks (used by its "orchestrion" auto-instrumentation)
    // even when the Pino integration isn't enabled, and webpack can't statically
    // resolve that package's ESM entry. It's a harmless dev-time warning, not a
    // build failure — see https://github.com/getsentry/sentry-javascript/issues/18199
    config.ignoreWarnings = [
      ...(config.ignoreWarnings || []),
      { module: /@sentry[\\/]server-utils[\\/]build[\\/]cjs[\\/]orchestrion/ },
    ];
    return config;
  },
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
  // disableLogger / automaticVercelMonitors moved under `webpack` per the
  // v10 deprecation notice ("DEPRECATION WARNING: ... Use webpack.* instead")
  webpack: {
    treeshake: {
      removeDebugLogging: true,
    },
    automaticVercelMonitors: false,
  },
});
