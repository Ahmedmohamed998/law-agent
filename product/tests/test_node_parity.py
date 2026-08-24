"""
Is the Node backend interchangeable with the Python one?

This is the acceptance gate for the migration. It talks to a running service
over HTTP and checks two things that must both hold before the Python
implementation can be retired:

  1. **The token contract.** A token minted by that service verifies exactly
     the way `app/api/auth.py` verifies one — JWKS fetched over HTTP, key
     selected by `kid`, decoded with `audience="law-agent-ai"`. This is Python
     verifying a Node-minted token, which is literally the production path.

  2. **Behavioural parity.** Signup, the in-place anonymous upgrade, login,
     refresh rotation, reuse detection, and the organization rules behave the
     same as the Python service's own `test_flows.py` asserts.

Deliberately black-box and implementation-agnostic: point it at :8001 whichever
service is listening there, and the results should be identical. That is the
whole claim.

    # against Node
    python -m product.tests.test_node_parity

    # against the Python implementation, to prove the test is fair
    python -m product.tests.test_node_parity --base http://127.0.0.1:8001
"""

import argparse
import sys
import uuid

import httpx
import jwt

# What app/api/auth.py demands. Duplicated as literals on purpose: importing
# them from either implementation would make the test pass by construction.
AI_SERVICE_AUDIENCE = "law-agent-ai"
AI_SERVICE_ALGORITHMS = ["RS256", "EdDSA"]
AI_SERVICE_REQUIRED = ["exp", "sub"]

PASSWORD = "correct-horse-battery"
RUN = uuid.uuid4().hex[:8]

ok = 0
fail = 0


def check(desc: str, condition: bool, extra: str = "") -> None:
    global ok, fail
    if condition:
        ok += 1
        print(f"  ok:   {desc}")
    else:
        fail += 1
        print(f"  FAIL: {desc} {extra}")


def email(name: str) -> str:
    return f"{name}+{RUN}@lawagent-qa.com"


def verify_as_ai_service(token: str, jwks_url: str, issuer: str) -> dict:
    """Reproduce app/api/auth.py::principal, including the JWKS fetch."""
    client = jwt.PyJWKClient(jwks_url, cache_keys=True)
    signing_key = client.get_signing_key_from_jwt(token).key
    return jwt.decode(
        token,
        signing_key,
        algorithms=AI_SERVICE_ALGORITHMS,
        audience=AI_SERVICE_AUDIENCE,
        issuer=issuer,
        options={"require": AI_SERVICE_REQUIRED},
    )


def run(base: str) -> None:
    jwks_url = f"{base}/.well-known/jwks.json"
    disco = httpx.get(f"{base}/.well-known/openid-configuration", timeout=10).json()
    issuer = disco["issuer"]

    print(f"target : {base}")
    print(f"issuer : {issuer}")
    print()

    # ── 1. the token contract ───────────────────────────────────────────
    print("=== token contract (Python verifying, as the AI service does) ===")

    signup = httpx.post(
        f"{base}/auth/signup",
        json={"email": email("parity"), "password": PASSWORD},
        timeout=15,
    )
    check("signup returns 201", signup.status_code == 201, signup.text[:160])
    body = signup.json()
    token, user_id = body["access_token"], body["user"]["user_id"]

    claims = verify_as_ai_service(token, jwks_url, issuer)
    check("token verifies against fetched JWKS", claims["sub"] == user_id)
    check("no anon claim for a registered user", "anon" not in claims)
    check("no org_id for a user in no organization", "org_id" not in claims)
    check("no role for a user in no organization", "role" not in claims)

    unverified = jwt.decode(token, options={"verify_signature": False})
    check("aud is a list", isinstance(unverified["aud"], list), str(unverified["aud"]))
    check("aud contains law-agent-ai", AI_SERVICE_AUDIENCE in unverified["aud"])
    check("typ is access", unverified.get("typ") == "access")
    check("kid present in header", "kid" in jwt.get_unverified_header(token))

    doc = httpx.get(jwks_url, timeout=10).json()
    leaked = [m for k in doc["keys"] for m in ("d", "p", "q", "dp", "dq", "qi") if m in k]
    check("JWKS carries no private material", not leaked, str(leaked))
    check("JWKS keys are RSA/sig", all(k["kty"] == "RSA" and k["use"] == "sig" for k in doc["keys"]))

    try:
        jwt.decode(
            token,
            jwt.PyJWKClient(jwks_url).get_signing_key_from_jwt(token).key,
            algorithms=AI_SERVICE_ALGORITHMS,
            audience="some-other-api",
            issuer=issuer,
            options={"require": AI_SERVICE_REQUIRED},
        )
        check("wrong audience rejected", False, "it was accepted")
    except jwt.InvalidAudienceError:
        check("wrong audience rejected", True)

    # ── 2. anonymous and the in-place upgrade ───────────────────────────
    print()
    print("=== anonymous upgrade ===")

    anon = httpx.post(f"{base}/auth/anonymous", timeout=15)
    check("anonymous returns 201", anon.status_code == 201, anon.text[:160])
    anon_body = anon.json()
    anon_id = anon_body["user"]["user_id"]
    check("anonymous user flagged", anon_body["user"]["is_anonymous"] is True)

    anon_claims = verify_as_ai_service(anon_body["access_token"], jwks_url, issuer)
    check("anon claim is true", anon_claims.get("anon") is True)

    upgraded = httpx.post(
        f"{base}/auth/signup",
        json={"email": email("upgrade"), "password": PASSWORD},
        headers={"Authorization": f"Bearer {anon_body['access_token']}"},
        timeout=15,
    )
    check("signup with anon token returns 201", upgraded.status_code == 201, upgraded.text[:160])
    up_body = upgraded.json()
    check("user id UNCHANGED after upgrade", up_body["user"]["user_id"] == anon_id,
          f"{anon_id} -> {up_body['user']['user_id']}")
    check("no longer anonymous", up_body["user"]["is_anonymous"] is False)
    up_claims = verify_as_ai_service(up_body["access_token"], jwks_url, issuer)
    check("anon claim gone after upgrade", "anon" not in up_claims)

    # ── 3. login ────────────────────────────────────────────────────────
    print()
    print("=== login ===")

    addr = email("login")
    httpx.post(f"{base}/auth/signup", json={"email": addr, "password": PASSWORD}, timeout=15)

    good = httpx.post(f"{base}/auth/login", json={"email": addr, "password": PASSWORD}, timeout=15)
    check("correct password returns 200", good.status_code == 200, good.text[:160])

    bad = httpx.post(f"{base}/auth/login", json={"email": addr, "password": "wrong-password-xx"}, timeout=15)
    check("wrong password returns 401", bad.status_code == 401)
    check("401 uses the error envelope", bad.json().get("error", {}).get("code") == "unauthenticated",
          bad.text[:160])

    unknown = httpx.post(
        f"{base}/auth/login", json={"email": email("nobody"), "password": PASSWORD}, timeout=15
    )
    check("unknown account is indistinguishable from wrong password",
          unknown.status_code == 401 and unknown.json() == bad.json(), unknown.text[:160])

    dupe = httpx.post(f"{base}/auth/signup", json={"email": addr, "password": PASSWORD}, timeout=15)
    check("duplicate email returns 409", dupe.status_code == 409, dupe.text[:160])
    check("409 does not confirm the address exists",
          "already" not in dupe.json().get("error", {}).get("message", "").lower())

    upper = httpx.post(
        f"{base}/auth/signup", json={"email": addr.upper(), "password": PASSWORD}, timeout=15
    )
    check("email is case-insensitive", upper.status_code == 409, upper.text[:160])

    # ── 4. refresh rotation and reuse detection ─────────────────────────
    print()
    print("=== refresh rotation ===")

    rot = httpx.post(
        f"{base}/auth/signup", json={"email": email("rotate"), "password": PASSWORD}, timeout=15
    ).json()
    first = rot["refresh_token"]

    second_resp = httpx.post(f"{base}/auth/refresh", json={"refresh_token": first}, timeout=15)
    check("refresh returns 200", second_resp.status_code == 200, second_resp.text[:160])
    second = second_resp.json()["refresh_token"]
    check("refresh rotates the token", second != first)

    replay = httpx.post(f"{base}/auth/refresh", json={"refresh_token": first}, timeout=15)
    check("replaying a spent token returns 401", replay.status_code == 401)
    check("reuse is named in the message",
          "reuse" in replay.json().get("error", {}).get("message", "").lower(),
          replay.text[:160])

    after = httpx.post(f"{base}/auth/refresh", json={"refresh_token": second}, timeout=15)
    check("the whole family is revoked (the bug that rolled back)",
          after.status_code == 401, f"got {after.status_code}")

    # ── 5. logout ───────────────────────────────────────────────────────
    print()
    print("=== logout ===")
    out = httpx.post(
        f"{base}/auth/signup", json={"email": email("logout"), "password": PASSWORD}, timeout=15
    ).json()
    check("logout returns 204",
          httpx.post(f"{base}/auth/logout", json={"refresh_token": out["refresh_token"]},
                     timeout=15).status_code == 204)
    check("revoked token cannot refresh",
          httpx.post(f"{base}/auth/refresh", json={"refresh_token": out["refresh_token"]},
                     timeout=15).status_code == 401)

    # ── 6. profile and organizations ────────────────────────────────────
    print()
    print("=== profile and organizations ===")

    owner = httpx.post(
        f"{base}/auth/signup", json={"email": email("owner"), "password": PASSWORD}, timeout=15
    ).json()
    oh = {"Authorization": f"Bearer {owner['access_token']}"}

    me = httpx.get(f"{base}/auth/me", headers=oh, timeout=15)
    check("me returns the caller", me.status_code == 200 and me.json()["user_id"] == owner["user"]["user_id"],
          me.text[:160])
    check("me without a token is 401", httpx.get(f"{base}/auth/me", timeout=15).status_code == 401)

    org = httpx.post(
        f"{base}/orgs", json={"name": "Elwekel Law", "slug": f"elwekel-{RUN}"},
        headers=oh, timeout=15,
    )
    check("org creation returns 201", org.status_code == 201, org.text[:160])
    org_id = org.json()["organization_id"]
    check("creator is owner", org.json()["my_role"] == "owner")

    fresh = httpx.post(
        f"{base}/auth/login", json={"email": email("owner"), "password": PASSWORD}, timeout=15
    ).json()
    fresh_claims = verify_as_ai_service(fresh["access_token"], jwks_url, issuer)
    check("org_id lands in the next token", fresh_claims.get("org_id") == org_id, str(fresh_claims))
    check("role lands in the next token", fresh_claims.get("role") == "owner")

    member_addr = email("member")
    member = httpx.post(
        f"{base}/auth/signup", json={"email": member_addr, "password": PASSWORD}, timeout=15
    ).json()

    added = httpx.post(
        f"{base}/orgs/{org_id}/members", json={"email": member_addr, "role": "lawyer"},
        headers=oh, timeout=15,
    )
    check("member added as lawyer", added.status_code == 201 and added.json()["role"] == "lawyer",
          added.text[:160])

    listed = httpx.get(f"{base}/orgs/{org_id}/members", headers=oh, timeout=15)
    check("both members listed", listed.status_code == 200 and len(listed.json()) == 2,
          listed.text[:160])

    mh = {"Authorization": f"Bearer {member['access_token']}"}
    refused = httpx.post(
        f"{base}/orgs/{org_id}/members", json={"email": email("owner"), "role": "admin"},
        headers=mh, timeout=15,
    )
    check("a lawyer cannot manage membership", refused.status_code == 403, refused.text[:160])

    last = httpx.delete(
        f"{base}/orgs/{org_id}/members/{owner['user']['user_id']}", headers=oh, timeout=15
    )
    check("the last owner cannot be removed", last.status_code == 403, last.text[:160])

    stranger = httpx.post(
        f"{base}/auth/signup", json={"email": email("stranger"), "password": PASSWORD}, timeout=15
    ).json()
    sh = {"Authorization": f"Bearer {stranger['access_token']}"}
    hidden = httpx.get(f"{base}/orgs/{org_id}/members", headers=sh, timeout=15)
    check("a stranger gets 404, not 403 (no org enumeration)", hidden.status_code == 404,
          f"got {hidden.status_code}")

    # ── 7. validation ───────────────────────────────────────────────────
    print()
    print("=== validation ===")
    short = httpx.post(
        f"{base}/auth/signup", json={"email": email("short"), "password": "abc"}, timeout=15
    )
    check("short password rejected", short.status_code in (400, 422), short.text[:160])
    check("validation uses the error envelope", "error" in short.json(), short.text[:160])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8001")
    args = parser.parse_args()

    run(args.base.rstrip("/"))
    print()
    print(f"{ok} passed, {fail} failed")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
