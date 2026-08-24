/**
 * Token minting: short-lived signed access tokens, long-lived opaque refresh
 * tokens.
 *
 * The two are deliberately different kinds of object. The access token is a
 * JWT because the AI service must validate it with no call back here — it
 * holds the public key and nothing else. The refresh token is NOT a JWT: it is
 * opaque random bytes, meaningful only against a row, because it must be
 * revocable, and a self-contained signed token cannot be revoked without
 * inventing the very lookup that avoids.
 *
 * That asymmetry is why the access TTL is minutes. There is no denylist for
 * access tokens; expiry is the only revocation they have.
 */

import { Injectable } from "@nestjs/common";
import { createHash, randomBytes } from "node:crypto";
import { SignJWT, decodeProtectedHeader, jwtVerify, type JWTPayload } from "jose";

import { loadConfig } from "../config/configuration";
import { KEY_ALGORITHM, KeyService } from "./key.service";

// 32 bytes of urandom. Long enough that guessing is not a threat model, short
// enough to sit in a JSON body without complaint.
const REFRESH_TOKEN_BYTES = 32;

export interface AccessToken {
  token: string;
  expiresIn: number;
  kid: string;
}

export interface ClaimContext {
  userId: string;
  organizationId?: string | null;
  role?: string | null;
  anonymous?: boolean;
}

@Injectable()
export class TokenService {
  private readonly config = loadConfig();

  constructor(private readonly keys: KeyService) {}

  /**
   * Sign an access token.
   *
   * The claim names are fixed by what the AI service reads in
   * `app/api/auth.py` — `sub`, `org_id`, `role`, `anon` — so they are not
   * configurable. Renaming one here does not break anything here; it breaks
   * the other service, at runtime, as a 401 with no obvious cause.
   */
  async issueAccessToken(ctx: ClaimContext): Promise<AccessToken> {
    const key = await this.keys.active();
    const now = Math.floor(Date.now() / 1000);
    const exp = now + this.config.accessTokenTtlSeconds;

    // Absent, not empty. A user in no organization must not get org_id: "" —
    // an empty string is a *different* RLS tenant key on the AI side and would
    // quietly bucket every individual user together under it.
    const claims: JWTPayload = {
      // Distinguishes this from any other signed artefact this service might
      // later produce (invites, email links). A verifier that assumes every
      // token it holds is an access token is one feature away from wrong.
      typ: "access",
    };
    if (ctx.organizationId) claims.org_id = ctx.organizationId;
    if (ctx.role) claims.role = ctx.role;
    if (ctx.anonymous) claims.anon = true;

    const token = await new SignJWT(claims)
      .setProtectedHeader({
        alg: KEY_ALGORITHM,
        // The `kid` lets the AI service pick the right public key out of a
        // JWKS holding several. Without it, rotation means trying every key
        // and hoping.
        kid: key.kid,
      })
      .setIssuer(this.config.jwtIssuer)
      // An array. The AI service's `audience=` check passes when the claim is
      // a list containing its own name, so one token is valid everywhere.
      .setAudience(this.config.jwtAudiences)
      .setSubject(ctx.userId)
      .setIssuedAt(now)
      .setNotBefore(now)
      .setExpirationTime(exp)
      // Nothing consumes it today; it is here so adding a denylist later does
      // not require re-issuing every token in circulation to give them ids.
      .setJti(randomBytes(9).toString("base64url"))
      .sign(key.privateKey);

    return { token, expiresIn: this.config.accessTokenTtlSeconds, kid: key.kid };
  }

  /**
   * Verify a token this service issued.
   *
   * Selects the key by the token's own `kid`, not by whichever key is
   * currently active — during a rotation those differ, and validating only
   * against the active key would reject every still-valid token signed by the
   * outgoing one. That is precisely the bug this service exists to avoid
   * causing in others.
   */
  async verifyOwnToken(token: string): Promise<JWTPayload> {
    const { kid } = decodeProtectedHeader(token);
    if (!kid) throw new Error("token has no kid");

    const key = this.keys.byKid(kid);
    if (!key) throw new Error(`unknown kid: ${kid}`);

    const { payload } = await jwtVerify(token, key.privateKey, {
      algorithms: [KEY_ALGORITHM],
      issuer: this.config.jwtIssuer,
      audience: this.config.jwtAudiences,
      requiredClaims: ["exp", "sub"],
    });
    return payload;
  }

  /**
   * Returns [plaintext, sha256hex].
   *
   * The plaintext is returned to the caller exactly once, in the response that
   * created it. Only the hash is stored, so a database dump yields nothing
   * replayable.
   */
  newRefreshToken(): [string, string] {
    const plaintext = randomBytes(REFRESH_TOKEN_BYTES).toString("base64url");
    return [plaintext, this.hashRefreshToken(plaintext)];
  }

  /**
   * Plain SHA-256, deliberately not a password hash.
   *
   * Argon2 is correct for passwords because they are low-entropy and
   * guessable. This value is 256 bits of urandom: there is nothing to
   * brute-force, and a deliberately slow hash would only add latency to every
   * refresh.
   */
  hashRefreshToken(plaintext: string): string {
    return createHash("sha256").update(plaintext, "utf8").digest("hex");
  }

  refreshExpiry(): Date {
    return new Date(Date.now() + this.config.refreshTokenTtlSeconds * 1000);
  }
}
