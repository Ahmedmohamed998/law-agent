"""
The cross-service contract test.

This is the test worth having. Everything else in the product backend is
ordinary CRUD that fails loudly when it breaks; the token contract fails
*quietly* — a claim renamed here does not break anything here, it breaks
`app/api/auth.py` in the other service, at runtime, as a 401 with no obvious
cause.

So this verifies a token minted by this service exactly the way the AI service
verifies it: fetch the JWKS document, select the key by `kid`, decode with
`audience="law-agent-ai"`, and require the same claims. If this passes, the two
halves are wired correctly.

Runs with or without pytest:

    python -m product.tests.test_contract
    pytest product/tests/test_contract.py
"""

import sys

import jwt

from product.config import settings
from product.security import keys, tokens

# What app/api/auth.py demands. Duplicated as literals on purpose: importing
# them from the AI service would make this test pass by construction, which is
# the one thing it must not do.
AI_SERVICE_AUDIENCE = "law-agent-ai"
AI_SERVICE_ALGORITHMS = ["RS256", "EdDSA"]
AI_SERVICE_REQUIRED = ["exp", "sub"]


def _verify_as_ai_service(token: str) -> dict:
    """Reproduce app/api/auth.py::principal, minus the HTTP fetch."""
    jwk_set = jwt.PyJWKSet.from_dict(keys.jwks())
    kid = jwt.get_unverified_header(token)["kid"]
    signing_key = next(k for k in jwk_set.keys if k.key_id == kid)

    return jwt.decode(
        token,
        signing_key.key,
        algorithms=AI_SERVICE_ALGORITHMS,
        audience=AI_SERVICE_AUDIENCE,
        issuer=settings().jwt_issuer,
        options={"require": AI_SERVICE_REQUIRED},
    )


def test_token_verifies_as_the_ai_service_would():
    access = tokens.issue_access_token(
        user_id="01TESTUSER0000000000000000",
        organization_id="01TESTORG00000000000000000",
        role="client",
    )
    claims = _verify_as_ai_service(access.token)

    # These four names are the entire wire contract. Renaming any of them is a
    # breaking change to the other service.
    assert claims["sub"] == "01TESTUSER0000000000000000"
    assert claims["org_id"] == "01TESTORG00000000000000000"
    assert claims["role"] == "client"
    assert "anon" not in claims  # absent, not false, for a registered user


def test_anonymous_token_carries_the_anon_flag():
    access = tokens.issue_access_token(user_id="01ANON0000000000000000000A", anonymous=True)
    claims = _verify_as_ai_service(access.token)
    assert claims["anon"] is True
    assert claims["sub"] == "01ANON0000000000000000000A"


def test_org_and_role_are_absent_for_an_individual():
    """A user in no organization must not get empty-string claims.

    The AI service reads a missing `org_id` as "no tenant"; an empty string
    would be a *different* tenant key, and would quietly bucket every
    individual user together under it.
    """
    access = tokens.issue_access_token(user_id="01SOLO000000000000000000AA")
    claims = _verify_as_ai_service(access.token)
    assert "org_id" not in claims
    assert "role" not in claims


def test_audience_is_a_list_containing_both_services():
    access = tokens.issue_access_token(user_id="01AUD0000000000000000000AA")
    unverified = jwt.decode(access.token, options={"verify_signature": False})
    assert isinstance(unverified["aud"], list)
    assert AI_SERVICE_AUDIENCE in unverified["aud"]


def test_wrong_audience_is_rejected():
    """A token minted for some other service must not open this one."""
    access = tokens.issue_access_token(user_id="01WRONG00000000000000000AA")
    jwk_set = jwt.PyJWKSet.from_dict(keys.jwks())
    kid = jwt.get_unverified_header(access.token)["kid"]
    signing_key = next(k for k in jwk_set.keys if k.key_id == kid)

    try:
        jwt.decode(
            access.token,
            signing_key.key,
            algorithms=AI_SERVICE_ALGORITHMS,
            audience="some-other-api",
            issuer=settings().jwt_issuer,
            options={"require": AI_SERVICE_REQUIRED},
        )
    except jwt.InvalidAudienceError:
        return
    raise AssertionError("a token for another audience was accepted")


def test_expired_token_is_rejected():
    """Expiry is the ONLY revocation an access token has — there is no
    denylist — so a verifier that ignores `exp` makes every leaked token
    permanent.

    Minted with a negative TTL rather than verified with negative leeway:
    leeway shifts `iat` and `nbf` too, so the token fails as not-yet-valid and
    the test passes for the wrong reason.
    """
    s = settings()
    original_ttl = s.access_token_ttl_seconds
    try:
        s.access_token_ttl_seconds = -60
        access = tokens.issue_access_token(user_id="01EXPIRED000000000000000AA")
    finally:
        s.access_token_ttl_seconds = original_ttl

    try:
        _verify_as_ai_service(access.token)
    except jwt.ExpiredSignatureError:
        return
    raise AssertionError("an expired token was accepted")


def test_jwks_exposes_no_private_material():
    """The document that crosses the boundary must be public-only.

    `d`, `p`, `q` are RSA private components. One of them in this document
    means the AI service — and anyone who can reach the endpoint — can mint
    tokens, which is the single failure that collapses the whole design.
    """
    document = keys.jwks()
    assert document["keys"], "no keys published"
    for entry in document["keys"]:
        for private_member in ("d", "p", "q", "dp", "dq", "qi"):
            assert private_member not in entry, f"JWKS leaked {private_member}"
        assert entry["kty"] == "RSA"
        assert entry["use"] == "sig"
        assert entry["kid"]


def test_rotation_keeps_old_tokens_valid(tmp_path=None):
    """During a rotation both keys are published, so tokens signed by the
    outgoing key still verify. This is the step operators skip."""
    import tempfile
    from pathlib import Path

    original_dir = settings().keys_dir
    original_kid = settings().active_kid

    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        settings().keys_dir = directory
        settings().active_kid = ""
        keys.reload()

        old = keys.generate()
        token_from_old = tokens.issue_access_token(user_id="01ROT0000000000000000000AA").token

        new = keys.generate()
        settings().active_kid = new.kid
        keys.reload()

        assert old.kid != new.kid
        published = {e["kid"] for e in keys.jwks()["keys"]}
        assert published == {old.kid, new.kid}, "rotation must publish both keys"

        # The old token still verifies against the published set.
        claims = _verify_as_ai_service(token_from_old)
        assert claims["sub"] == "01ROT0000000000000000000AA"

    settings().keys_dir = original_dir
    settings().active_kid = original_kid
    keys.reload()


TESTS = [
    test_token_verifies_as_the_ai_service_would,
    test_anonymous_token_carries_the_anon_flag,
    test_org_and_role_are_absent_for_an_individual,
    test_audience_is_a_list_containing_both_services,
    test_wrong_audience_is_rejected,
    test_expired_token_is_rejected,
    test_jwks_exposes_no_private_material,
    test_rotation_keeps_old_tokens_valid,
]


def main() -> int:
    failures = 0
    for test in TESTS:
        try:
            test()
        except Exception as exc:
            failures += 1
            print(f"FAIL  {test.__name__}")
            print(f"      {type(exc).__name__}: {exc}")
        else:
            print(f"ok    {test.__name__}")
    print()
    print(f"{len(TESTS) - failures}/{len(TESTS)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
