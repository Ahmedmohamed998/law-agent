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

  port: number;
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

    port: int("PORT", 8001),
    corsOriginRegex:
      process.env.CORS_ORIGIN_REGEX ?? "https?://(localhost|127\\.0\\.0\\.1)(:\\d+)?",
  };
}
