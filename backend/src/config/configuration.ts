/**
 * Configuration, read once at boot and validated loudly.
 *
 * The Python service used pydantic-settings, which fails at import time on a
 * bad value. Nest's ConfigModule is happy to hand you `undefined` at runtime
 * instead, so the validation below is doing work that framework does not: a
 * missing JWT_ISSUER should stop the process, not produce tokens with
 * `iss: undefined` that the AI service rejects an hour later for no obvious
 * reason.
 */

import { join } from "node:path";

export interface AppConfig {
  databaseUrl: string;
  databaseMigrateUrl: string;

  keysDir: string;
  activeKid: string;
  jwtIssuer: string;
  jwtAudiences: string[];
  accessTokenTtlSeconds: number;
  refreshTokenTtlSeconds: number;

  aiServiceUrl: string;
  adminApiKey: string;

  consultationPriceCents: number;
  consultationCurrency: string;

  wordpressSharedSecret: string;
  wordpressSiteUrl: string;

  port: number;
  bindHost: string;
  corsOriginRegex: string;
}

function required(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(
      `${name} is not set — see backend/.env.example. Refusing to start ` +
        `rather than run with a silently wrong value.`,
    );
  }
  return value;
}

function int(name: string, fallback: number): number {
  const raw = process.env[name];
  if (!raw) return fallback;
  const parsed = Number(raw);
  if (!Number.isInteger(parsed) || parsed <= 0) {
    throw new Error(`${name} must be a positive integer, got ${JSON.stringify(raw)}`);
  }
  return parsed;
}

export function loadConfig(): AppConfig {
  return {
    databaseUrl: required("DATABASE_URL"),
    databaseMigrateUrl: process.env.DATABASE_MIGRATE_URL ?? "",

    keysDir: process.env.KEYS_DIR
      ? join(process.cwd(), process.env.KEYS_DIR)
      : join(process.cwd(), "keys"),
    activeKid: process.env.ACTIVE_KID ?? "",
    jwtIssuer: required("JWT_ISSUER"),
    // A list, not a string. One token is valid at every service that needs it,
    // and the AI service's `audience=` check passes when the claim is an array
    // containing its own name.
    jwtAudiences: (process.env.JWT_AUDIENCES ?? "law-agent-ai,law-agent-product")
      .split(",")
      .map((a) => a.trim())
      .filter(Boolean),
    // Short, because there is no denylist: expiry is an access token's only
    // revocation. The refresh token carries the long session precisely because
    // it *is* revocable.
    accessTokenTtlSeconds: int("ACCESS_TOKEN_TTL_SECONDS", 900),
    refreshTokenTtlSeconds: int("REFRESH_TOKEN_TTL_SECONDS", 60 * 60 * 24 * 30),

    aiServiceUrl: process.env.AI_SERVICE_URL ?? "http://127.0.0.1:8000",
    adminApiKey: process.env.ADMIN_API_KEY ?? "",

    // The price of a consultation is decided here and nowhere else.
    // It used to arrive in the request body, which meant the amount charged
    // was whatever the browser said it was — a number anyone could edit
    // before paying. A price is a property of the product, not of the
    // request.
    consultationPriceCents: int("CONSULTATION_PRICE_CENTS", 50_000),
    // The gateway is ksa.paymob.com (see PAYMOB_BASE), so the default is the
    // currency that account settles in. A mismatch here is rejected by
    // Paymob at order creation rather than silently mischarged.
    consultationCurrency: (process.env.CONSULTATION_CURRENCY ?? "SAR")
      .trim()
      .toUpperCase(),

    // WordPress owns credentials; this secret is how it proves a user is
    // logged in. It never signs a token -- only an assertion this service
    // exchanges for one. Unset means POST /auth/wordpress refuses to run.
    wordpressSharedSecret: process.env.WORDPRESS_SHARED_SECRET ?? "",
    // Checked against the assertion's `site` claim, so an assertion minted by
    // a staging WordPress cannot log someone into production.
    wordpressSiteUrl: (process.env.WORDPRESS_SITE_URL ?? "").replace(/\/+$/, ""),

    port: int("PORT", 8001),
    // Loopback by default: on a bare host nothing should reach this service
    // except the reverse proxy. Inside a container 127.0.0.1 is the
    // container's own loopback and nothing can reach it at all, so there the
    // value is 0.0.0.0 and privacy comes from publishing the port as
    // 127.0.0.1:8001 on the host instead.
    bindHost: process.env.BIND_HOST ?? "127.0.0.1",
    corsOriginRegex:
      process.env.CORS_ORIGIN_REGEX ?? "https?://(localhost|127\\.0\\.0\\.1)(:\\d+)?",
  };
}
