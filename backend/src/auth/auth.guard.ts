/**
 * The caller's identity, and the admin key.
 *
 * This service verifies its OWN tokens locally, against the private keys it
 * already holds — no HTTP hop, no JWKS fetch, because the issuer and the
 * verifier are the same process. The AI service does the opposite, and that
 * asymmetry is correct: it must never be able to reach a private key.
 */

import {
  CanActivate,
  ExecutionContext,
  Injectable,
  createParamDecorator,
} from "@nestjs/common";
import { timingSafeEqual } from "node:crypto";
import type { Request } from "express";

import { forbidden, unauthorized } from "../common/errors";
import { loadConfig } from "../config/configuration";
import { TokenService } from "../security/token.service";
import { AppError } from "../common/errors";
import { HttpStatus } from "@nestjs/common";

export interface Caller {
  userId: string;
  organizationId: string | null;
  role: string | null;
  anonymous: boolean;
}

/** Attached to the request by AuthGuard; read with @CurrentCaller(). */
export interface RequestWithCaller extends Request {
  caller?: Caller;
}

export function bearerFrom(req: Request): string | null {
  const header = req.headers.authorization;
  if (!header || !header.startsWith("Bearer ")) return null;
  return header.slice(7).trim() || null;
}

export async function callerFromToken(
  tokens: TokenService,
  token: string,
): Promise<Caller> {
  let claims;
  try {
    claims = await tokens.verifyOwnToken(token);
  } catch (err) {
    throw unauthorized(String((err as Error).message).slice(0, 120));
  }

  if (claims.typ !== "access") {
    // A refresh token is opaque and could never decode here, but a future
    // signed artefact (an invite, an email confirmation) would. Reject
    // anything not explicitly an access token, rather than discovering later
    // that a password-reset link was also a login.
    throw unauthorized("not an access token");
  }

  return {
    userId: String(claims.sub),
    organizationId: (claims.org_id as string | undefined) ?? null,
    role: (claims.role as string | undefined) ?? null,
    anonymous: claims.anon === true,
  };
}

@Injectable()
export class AuthGuard implements CanActivate {
  constructor(private readonly tokens: TokenService) {}

  async canActivate(context: ExecutionContext): Promise<boolean> {
    const req = context.switchToHttp().getRequest<RequestWithCaller>();
    const token = bearerFrom(req);
    if (!token) throw unauthorized("missing bearer token");
    req.caller = await callerFromToken(this.tokens, token);
    return true;
  }
}

/**
 * Rejects anonymous callers.
 *
 * An anonymous user may hold a conversation; they may not own organizations,
 * add members, or delete accounts.
 */
@Injectable()
export class RegisteredGuard implements CanActivate {
  canActivate(context: ExecutionContext): boolean {
    const req = context.switchToHttp().getRequest<RequestWithCaller>();
    if (req.caller?.anonymous) {
      throw forbidden("this action requires a registered account");
    }
    return true;
  }
}

/**
 * Service-to-service authorization for the maintenance endpoints.
 *
 * A shared header rather than a user token: the callers are other *services*,
 * and modelling them as a very privileged user account would mean a credential
 * that can also log in.
 */
@Injectable()
export class AdminKeyGuard implements CanActivate {
  private readonly config = loadConfig();

  canActivate(context: ExecutionContext): boolean {
    const configured = this.config.adminApiKey;
    if (!configured) {
      throw new AppError(
        HttpStatus.SERVICE_UNAVAILABLE,
        "admin_unconfigured",
        "ADMIN_API_KEY is not set",
      );
    }

    const req = context.switchToHttp().getRequest<Request>();
    const provided = String(req.headers["x-admin-key"] ?? "");

    // Constant-time. A plain !== leaks the key one byte at a time to anyone
    // willing to measure enough requests. Buffers must match in length first,
    // because timingSafeEqual throws otherwise.
    const a = Buffer.from(provided);
    const b = Buffer.from(configured);
    if (a.length !== b.length || !timingSafeEqual(a, b)) {
      throw forbidden("bad admin key");
    }
    return true;
  }
}

export const CurrentCaller = createParamDecorator(
  (_data: unknown, context: ExecutionContext): Caller => {
    const req = context.switchToHttp().getRequest<RequestWithCaller>();
    if (!req.caller) throw unauthorized("no verified caller on request");
    return req.caller;
  },
);
