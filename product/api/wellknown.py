"""
Public key distribution.

This is the entire integration surface between the two services. The AI
service's `JWKS_URL` points here, it fetches this document, caches it for an
hour, and verifies every token against it. Nothing else crosses the boundary.

Both endpoints are unauthenticated by design — they contain only public keys,
and requiring a credential to fetch the key that validates credentials is a
chicken-and-egg problem with no upside.
"""

from fastapi import APIRouter, Response

from product.config import settings
from product.security import keys

router = APIRouter(prefix="/.well-known", tags=["well-known"])

# Verifiers cache anyway, but a cache header keeps a busy fleet from re-fetching
# on every cold process. Shorter than the AI service's own hour-long cache, so
# a newly published key is picked up promptly during a rotation.
_CACHE_CONTROL = "public, max-age=300"


@router.get("/jwks.json")
def jwks(response: Response):
    """Every public key this service signs with, current and outgoing.

    Both must be here during a rotation. Publishing only the active key
    invalidates every token signed by the previous one the moment it is
    removed — and because verifiers cache for up to an hour, that failure
    arrives staggered and looks like an intermittent bug rather than a
    rotation mistake.
    """
    response.headers["Cache-Control"] = _CACHE_CONTROL
    return keys.jwks()


@router.get("/openid-configuration")
def openid_configuration(response: Response):
    """Minimal discovery document.

    Not a full OIDC provider — there is no authorization endpoint and no
    userinfo. It exists so a client library can find the issuer and the JWKS
    URL without them being hard-coded in two repositories.
    """
    s = settings()
    issuer = s.jwt_issuer.rstrip("/")
    response.headers["Cache-Control"] = _CACHE_CONTROL
    return {
        "issuer": s.jwt_issuer,
        "jwks_uri": f"{issuer}/.well-known/jwks.json",
        "token_endpoint": f"{issuer}/auth/login",
        "id_token_signing_alg_values_supported": [s.jwt_algorithm],
        "claims_supported": ["sub", "aud", "iss", "exp", "iat", "org_id", "role", "anon"],
        "grant_types_supported": ["password", "refresh_token"],
    }
