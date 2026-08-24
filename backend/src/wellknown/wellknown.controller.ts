/**
 * Public key distribution — the entire integration surface between this
 * service and the AI service.
 *
 * The AI service's JWKS_URL points here, it fetches this document, caches it
 * for an hour, and verifies every token against it. Nothing else crosses.
 *
 * Both endpoints are unauthenticated by design: they contain only public keys,
 * and requiring a credential to fetch the key that validates credentials is a
 * chicken-and-egg problem with no upside.
 */

import { Controller, Get, Header } from "@nestjs/common";

import { KEY_ALGORITHM, KeyService } from "../security/key.service";
import { loadConfig } from "../config/configuration";

// Verifiers cache anyway, but a cache header keeps a busy fleet from
// re-fetching on every cold process. Shorter than the AI service's own
// hour-long cache, so a newly published key is picked up promptly.
const CACHE_CONTROL = "public, max-age=300";

@Controller(".well-known")
export class WellKnownController {
  private readonly config = loadConfig();

  constructor(private readonly keys: KeyService) {}

  /**
   * Every public key this service signs with, current and outgoing.
   *
   * Both must be here during a rotation. Publishing only the active key
   * invalidates every token signed by the previous one the moment it is
   * removed — and because verifiers cache for up to an hour, that failure
   * arrives staggered and looks like an intermittent bug rather than a
   * rotation mistake.
   */
  @Get("jwks.json")
  @Header("Cache-Control", CACHE_CONTROL)
  jwks() {
    return this.keys.jwks();
  }

  /**
   * Minimal discovery document.
   *
   * Not a full OIDC provider — no authorization endpoint, no userinfo. It
   * exists so a client library can find the issuer and the JWKS URL without
   * them being hard-coded in two repositories.
   */
  @Get("openid-configuration")
  @Header("Cache-Control", CACHE_CONTROL)
  openidConfiguration() {
    const issuer = this.config.jwtIssuer.replace(/\/+$/, "");
    return {
      issuer: this.config.jwtIssuer,
      jwks_uri: `${issuer}/.well-known/jwks.json`,
      token_endpoint: `${issuer}/auth/login`,
      id_token_signing_alg_values_supported: [KEY_ALGORITHM],
      claims_supported: ["sub", "aud", "iss", "exp", "iat", "org_id", "role", "anon"],
      grant_types_supported: ["password", "refresh_token"],
    };
  }
}
