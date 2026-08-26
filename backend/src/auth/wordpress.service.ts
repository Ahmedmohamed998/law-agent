/**
 * Verifying a WordPress login assertion.
 *
 * WordPress cannot mint our tokens — it holds no private key, and giving it
 * one would mean a second runtime that can sign for the identity domain. What
 * it can do is *assert*, over a shared secret, that a particular WordPress
 * user is logged in right now. This service checks that assertion; the
 * caller then issues a normal RS256 token, so the AI service is unaware any
 * of this happened.
 *
 * The assertion is a bearer credential. Everything below exists because for
 * its lifetime, whoever holds it is that user:
 *
 *   * HMAC-SHA256 over the exact bytes that were signed, compared in constant
 *     time. A plain !== on a hex digest leaks it a byte at a time to anyone
 *     willing to measure enough requests.
 *   * Sixty seconds. Long enough for a page load, short enough that a leaked
 *     one is usually already dead.
 *   * Single use, enforced by a primary key rather than a lookup, because two
 *     concurrent presentations would both pass a lookup.
 *   * Bound to the site that issued it, so an assertion from a staging
 *     WordPress cannot log someone into production.
 */

import { Injectable, Logger } from "@nestjs/common";
import { createHmac, timingSafeEqual } from "node:crypto";

import { AppError } from "../common/errors";
import { HttpStatus } from "@nestjs/common";
import { loadConfig } from "../config/configuration";
import { PrismaService } from "../prisma/prisma.service";

export interface WordPressClaims {
  wp_user_id: number;
  email: string | null;
  display_name: string | null;
  wp_role: string | null;
  site: string;
  iat: number;
  exp: number;
  jti: string;
}

function unauthorizedAssertion(reason: string): AppError {
  // One message for every failure mode. Telling a caller *which* check failed
  // turns this into an oracle for forging the next attempt.
  Logger.warn(`wordpress assertion rejected: ${reason}`, "WordPressService");
  return new AppError(
    HttpStatus.UNAUTHORIZED,
    "unauthenticated",
    "the WordPress assertion could not be verified",
  );
}

function b64urlDecode(input: string): Buffer {
  return Buffer.from(input.replace(/-/g, "+").replace(/_/g, "/"), "base64");
}

@Injectable()
export class WordPressService {
  private readonly config = loadConfig();

  constructor(private readonly prisma: PrismaService) {}

  get configured(): boolean {
    return Boolean(this.config.wordpressSharedSecret);
  }

  /**
   * Verify `<base64url(payload)>.<base64url(hmac)>` and consume its jti.
   *
   * Resolves to the claims, or throws a single indistinguishable 401.
   */
  async verify(assertion: string): Promise<WordPressClaims> {
    const secret = this.config.wordpressSharedSecret;
    if (!secret) {
      throw new AppError(
        HttpStatus.SERVICE_UNAVAILABLE,
        "wordpress_unconfigured",
        "WORDPRESS_SHARED_SECRET is not set",
      );
    }

    const dot = assertion.indexOf(".");
    if (dot < 1 || dot === assertion.length - 1) {
      throw unauthorizedAssertion("malformed");
    }
    const encodedPayload = assertion.slice(0, dot);
    const providedSig = assertion.slice(dot + 1);

    // Signed over the ENCODED payload, not the decoded object. Re-serialising
    // JSON to check a signature is how key-ordering and unicode-escaping
    // differences between PHP and Node turn into intermittent failures.
    const expected = createHmac("sha256", secret).update(encodedPayload).digest();
    const provided = b64urlDecode(providedSig);

    if (
      provided.length !== expected.length ||
      !timingSafeEqual(provided, expected)
    ) {
      throw unauthorizedAssertion("bad signature");
    }

    let claims: WordPressClaims;
    try {
      claims = JSON.parse(b64urlDecode(encodedPayload).toString("utf8"));
    } catch {
      throw unauthorizedAssertion("payload is not json");
    }

    if (!Number.isInteger(claims.wp_user_id) || claims.wp_user_id <= 0) {
      throw unauthorizedAssertion("no wp_user_id");
    }
    if (typeof claims.jti !== "string" || claims.jti.length < 16 || claims.jti.length > 64) {
      throw unauthorizedAssertion("no usable jti");
    }

    const now = Math.floor(Date.now() / 1000);
    if (typeof claims.exp !== "number" || claims.exp < now) {
      throw unauthorizedAssertion("expired");
    }
    // A clock far ahead is either a misconfigured server or someone extending
    // the window; either way the assertion is not usable now.
    if (claims.exp - now > 300) {
      throw unauthorizedAssertion("expiry too distant");
    }

    const site = this.config.wordpressSiteUrl;
    if (site && claims.site !== site) {
      throw unauthorizedAssertion(`wrong site: ${claims.site}`);
    }

    await this.consume(claims);
    return claims;
  }

  /**
   * Burn the jti. The insert IS the check: a duplicate raises a unique
   * violation, which is exactly the replay we are refusing.
   */
  private async consume(claims: WordPressClaims): Promise<void> {
    try {
      await this.prisma.wp_assertions.create({
        data: {
          jti: claims.jti,
          wp_user_id: claims.wp_user_id,
          expires_at: new Date(claims.exp * 1000),
        },
      });
    } catch (err) {
      if ((err as { code?: string }).code === "P2002") {
        throw unauthorizedAssertion("replayed jti");
      }
      throw err;
    }

    // Opportunistic sweep. Rows matter only until the assertion they record
    // could no longer be replayed, and a cron for this would be one more
    // thing to run.
    this.prisma.wp_assertions
      .deleteMany({ where: { expires_at: { lt: new Date(Date.now() - 60_000) } } })
      .catch(() => undefined);
  }

  /**
   * WordPress role -> Law Agent role.
   *
   * Only `administrator` earns staff access; everything else is a client.
   * Deliberately a allow-list, so a new WordPress role added by some plugin
   * cannot quietly become a Law Agent admin.
   */
  static mapRole(wpRole: string | null | undefined): string | null {
    if (wpRole === "administrator") return "admin";
    if (wpRole === "editor") return "lawyer";
    return null;
  }
}
