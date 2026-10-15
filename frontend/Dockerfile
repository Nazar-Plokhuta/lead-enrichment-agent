# syntax=docker/dockerfile:1

# ---- Stage 1: dependencies ----------------------------------------------------
FROM node:22-alpine AS deps
WORKDIR /app
# libc6-compat: some prebuilt native binaries (e.g. sharp, SWC) expect glibc symbols on musl.
RUN apk add --no-cache libc6-compat
# Copy manifests only so this layer is cached until the lockfile changes.
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
# corepack resolves the pnpm version pinned by "packageManager" in package.json.
RUN corepack enable && pnpm install --frozen-lockfile

# ---- Stage 2: build -----------------------------------------------------------
FROM node:22-alpine AS builder
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# public/ is optional in the repo (git does not track empty dirs); the runner stage always copies it.
RUN mkdir -p public && corepack enable && pnpm build

# ---- Stage 3: runtime ---------------------------------------------------------
FROM node:22-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME="0.0.0.0"

# The standalone bundle already contains the traced production node_modules.
COPY --from=builder --chown=1000:1000 /app/.next/standalone ./
COPY --from=builder --chown=1000:1000 /app/.next/static ./.next/static
COPY --from=builder --chown=1000:1000 /app/public ./public

# The official node image ships a `node` user with uid/gid 1000.
USER node
EXPOSE 3000
CMD ["node", "server.js"]
