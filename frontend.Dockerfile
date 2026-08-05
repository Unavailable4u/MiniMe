# MiniMe frontend — Next.js 14 app
#
# Multi-stage: the build stage needs devDependencies (tailwind, postcss)
# and the full node_modules tree, but the final image only needs the
# compiled .next output + production deps, which keeps the shipped image
# much smaller than a single-stage build would.

FROM node:20-slim AS deps
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci

FROM node:20-slim AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY frontend/ .
# SENTRY_AUTH_TOKEN etc. are read by next.config.js's withSentryConfig at
# build time to upload source maps — build still succeeds without them
# (per the comment in next.config.js), just without source-mapped stack
# traces in Sentry. Pass them via --build-arg if/when you want that.
RUN npm run build

FROM node:20-slim AS runner
WORKDIR /app
ENV NODE_ENV=production

COPY --from=builder /app/next.config.js ./
COPY --from=builder /app/public ./public
COPY --from=builder /app/.next ./.next
COPY --from=builder /app/package.json ./package.json
COPY --from=builder /app/node_modules ./node_modules

EXPOSE 3000
CMD ["npm", "start"]
