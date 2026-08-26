# Product backend — identity, tokens, consultations.
#
# Three stages: install everything to build, compile, then install production
# dependencies only for the image that actually runs.

# ── dependencies ──────────────────────────────────────────────────────────
FROM node:22-slim AS deps

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

# ── build ─────────────────────────────────────────────────────────────────
FROM node:22-slim AS build

WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .

# Prisma 7 reads the connection URL from prisma.config.ts, which resolves
# DATABASE_MIGRATE_URL through `env()` and throws when it is unset — even for
# `generate`, which only needs the schema file. These placeholders exist to
# get past that; nothing connects at build time, and the running container
# gets the real URLs from its env file.
ENV DATABASE_URL="postgresql://build:build@localhost:5432/build" \
    DATABASE_MIGRATE_URL="postgresql://build:build@localhost:5432/build"

RUN npx prisma generate && npm run build

# ── production dependencies ───────────────────────────────────────────────
FROM node:22-slim AS proddeps

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev

# ── runtime ───────────────────────────────────────────────────────────────
FROM node:22-slim AS runtime

ENV NODE_ENV=production

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=proddeps --chown=node:node /app/node_modules ./node_modules
COPY --from=build    --chown=node:node /app/dist          ./dist
COPY --from=build    --chown=node:node /app/generated     ./generated
COPY --chown=node:node package.json ./
COPY --chown=node:node prisma/ ./prisma/

# The signing keys are NOT baked in. They are generated once on the server
# with `docker compose run --rm backend npm run keygen` and live in a bind
# mount, because a private key inside an image is a private key in every
# registry and every layer cache that image touches.

USER node

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8001/healthz || exit 1

CMD ["node", "dist/main.js"]
