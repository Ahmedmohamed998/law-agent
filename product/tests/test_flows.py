"""
End-to-end flows against a real Postgres.

Deliberately not mocked and not SQLite. Three things here only behave correctly
on the real database — the partial unique index on email, the
`ck_users_anonymous_shape` check constraint, and `SELECT ... FOR UPDATE` in the
refresh path — and a substitute backend would pass while production fails.

Requires the product schema to be migrated:

    alembic -c product/alembic.ini upgrade head

    python -m product.tests.test_flows
"""

import sys
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from product.db.base import tx
from product.db.models import ErasureRequest, Organization, User
from product.main import app

client = TestClient(app)

# Every run uses fresh addresses so a failed run never poisons the next one.
RUN = uuid.uuid4().hex[:8]
CREATED_USERS: list[str] = []
CREATED_ORGS: list[str] = []


def email(name: str) -> str:
    return f"{name}+{RUN}@lawagent-qa.com"


def track(response) -> dict:
    body = response.json()
    if "user" in body:
        CREATED_USERS.append(body["user"]["user_id"])
    return body


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── anonymous and the in-place upgrade ────────────────────────────────────


def test_anonymous_gets_a_real_token():
    r = client.post("/auth/anonymous")
    assert r.status_code == 201, r.text
    body = track(r)
    assert body["user"]["is_anonymous"] is True
    assert body["user"]["email"] is None
    assert body["access_token"] and body["refresh_token"]


def test_signup_upgrades_the_anonymous_user_in_place():
    """The claim the whole anonymous design rests on: the user id does not
    change, so conversations already stored against it on the AI side stay
    with the account."""
    anon = track(client.post("/auth/anonymous"))
    anon_id = anon["user"]["user_id"]

    r = client.post(
        "/auth/signup",
        json={"email": email("upgrade"), "password": "correct-horse-battery"},
        headers=auth(anon["access_token"]),
    )
    assert r.status_code == 201, r.text
    body = r.json()

    assert body["user"]["user_id"] == anon_id, "signup must keep the same id"
    assert body["user"]["is_anonymous"] is False
    assert body["user"]["email"] == email("upgrade")


def test_signup_without_an_anonymous_token_creates_a_new_user():
    anon = track(client.post("/auth/anonymous"))
    r = client.post(
        "/auth/signup",
        json={"email": email("fresh"), "password": "correct-horse-battery"},
    )
    assert r.status_code == 201, r.text
    body = track(r)
    assert body["user"]["user_id"] != anon["user"]["user_id"]


def test_duplicate_email_is_refused_without_confirming_it_exists():
    addr = email("dupe")
    first = client.post(
        "/auth/signup", json={"email": addr, "password": "correct-horse-battery"}
    )
    assert first.status_code == 201
    track(first)

    second = client.post(
        "/auth/signup", json={"email": addr, "password": "different-password-x"}
    )
    assert second.status_code == 409, second.text
    # The message must not confirm registration — that would make signup an
    # oracle for which addresses hold accounts.
    assert "already" not in second.json()["error"]["message"].lower()


def test_email_is_case_insensitive():
    addr = email("CaseTest")
    r = client.post(
        "/auth/signup", json={"email": addr.upper(), "password": "correct-horse-battery"}
    )
    assert r.status_code == 201, r.text
    track(r)
    # Same address, different case — the partial unique index must catch it.
    again = client.post(
        "/auth/signup", json={"email": addr.lower(), "password": "correct-horse-battery"}
    )
    assert again.status_code == 409


# ── login ─────────────────────────────────────────────────────────────────


def test_login_and_wrong_password():
    addr = email("login")
    track(client.post("/auth/signup", json={"email": addr, "password": "correct-horse-battery"}))

    good = client.post("/auth/login", json={"email": addr, "password": "correct-horse-battery"})
    assert good.status_code == 200, good.text
    assert good.json()["access_token"]

    bad = client.post("/auth/login", json={"email": addr, "password": "wrong-password-here"})
    assert bad.status_code == 401
    assert bad.json()["error"]["code"] == "unauthenticated"

    unknown = client.post(
        "/auth/login", json={"email": email("nobody"), "password": "correct-horse-battery"}
    )
    # Identical response to a wrong password: no account enumeration.
    assert unknown.status_code == 401
    assert unknown.json() == bad.json()


def test_me_returns_the_caller():
    addr = email("me")
    body = track(
        client.post("/auth/signup", json={"email": addr, "password": "correct-horse-battery"})
    )
    r = client.get("/auth/me", headers=auth(body["access_token"]))
    assert r.status_code == 200, r.text
    assert r.json()["email"] == addr
    assert r.json()["user_id"] == body["user"]["user_id"]


def test_no_token_is_401():
    assert client.get("/auth/me").status_code == 401


# ── refresh rotation and reuse detection ──────────────────────────────────


def test_refresh_rotates_the_token():
    body = track(
        client.post("/auth/signup", json={"email": email("rot"), "password": "correct-horse-battery"})
    )
    first = body["refresh_token"]

    r = client.post("/auth/refresh", json={"refresh_token": first})
    assert r.status_code == 200, r.text
    second = r.json()["refresh_token"]
    assert second != first, "refresh must rotate"

    # The new one works.
    assert client.post("/auth/refresh", json={"refresh_token": second}).status_code == 200


def test_reusing_a_spent_refresh_token_revokes_the_family():
    body = track(
        client.post("/auth/signup", json={"email": email("reuse"), "password": "correct-horse-battery"})
    )
    first = body["refresh_token"]

    second = client.post("/auth/refresh", json={"refresh_token": first}).json()["refresh_token"]

    # Replaying the spent token: this is the leak signal.
    replay = client.post("/auth/refresh", json={"refresh_token": first})
    assert replay.status_code == 401
    assert "reuse" in replay.json()["error"]["message"].lower()

    # ...and the descendant is now dead too, because at that point the
    # attacker and the real user cannot be told apart.
    after = client.post("/auth/refresh", json={"refresh_token": second})
    assert after.status_code == 401, "the whole family must be revoked"


def test_logout_revokes_the_family():
    body = track(
        client.post("/auth/signup", json={"email": email("logout"), "password": "correct-horse-battery"})
    )
    token = body["refresh_token"]
    assert client.post("/auth/logout", json={"refresh_token": token}).status_code == 204
    assert client.post("/auth/refresh", json={"refresh_token": token}).status_code == 401


# ── organizations ─────────────────────────────────────────────────────────


def test_org_creation_puts_org_and_role_in_the_next_token():
    """The org_id claim is what becomes the AI service's RLS tenant key, so
    this is the path that matters most for isolation over there."""
    addr = email("org")
    body = track(
        client.post("/auth/signup", json={"email": addr, "password": "correct-horse-battery"})
    )
    token = body["access_token"]

    r = client.post(
        "/orgs",
        json={"name": "Elwekel Law", "slug": f"elwekel-{RUN}"},
        headers=auth(token),
    )
    assert r.status_code == 201, r.text
    org_id = r.json()["organization_id"]
    CREATED_ORGS.append(org_id)
    assert r.json()["my_role"] == "owner"

    # A token minted BEFORE the org existed does not carry it; a fresh login does.
    fresh = client.post(
        "/auth/login", json={"email": addr, "password": "correct-horse-battery"}
    ).json()
    import jwt

    claims = jwt.decode(fresh["access_token"], options={"verify_signature": False})
    assert claims["org_id"] == org_id
    assert claims["role"] == "owner"


def test_members_can_be_added_and_the_last_owner_cannot_be_removed():
    owner = track(
        client.post("/auth/signup", json={"email": email("owner"), "password": "correct-horse-battery"})
    )
    member_addr = email("member")
    member = track(
        client.post("/auth/signup", json={"email": member_addr, "password": "correct-horse-battery"})
    )

    org = client.post(
        "/orgs",
        json={"name": "Second Firm", "slug": f"second-{RUN}"},
        headers=auth(owner["access_token"]),
    ).json()
    CREATED_ORGS.append(org["organization_id"])
    org_id = org["organization_id"]

    added = client.post(
        f"/orgs/{org_id}/members",
        json={"email": member_addr, "role": "lawyer"},
        headers=auth(owner["access_token"]),
    )
    assert added.status_code == 201, added.text
    assert added.json()["role"] == "lawyer"

    listed = client.get(f"/orgs/{org_id}/members", headers=auth(owner["access_token"]))
    assert listed.status_code == 200
    assert len(listed.json()) == 2

    # A lawyer is a member, not a manager.
    member_token = client.post(
        "/auth/login", json={"email": member_addr, "password": "correct-horse-battery"}
    ).json()["access_token"]
    refused = client.post(
        f"/orgs/{org_id}/members",
        json={"email": email("owner"), "role": "admin"},
        headers=auth(member_token),
    )
    assert refused.status_code == 403, refused.text

    # An organization must never be left with no owner.
    last = client.delete(
        f"/orgs/{org_id}/members/{owner['user']['user_id']}",
        headers=auth(owner["access_token"]),
    )
    assert last.status_code == 403
    assert "last owner" in last.json()["error"]["message"]


def test_a_stranger_gets_404_not_403_for_someone_elses_org():
    """404, not 403 — otherwise this endpoint enumerates other firms' org ids."""
    stranger = track(
        client.post("/auth/signup", json={"email": email("stranger"), "password": "correct-horse-battery"})
    )
    owner = track(
        client.post("/auth/signup", json={"email": email("owner2"), "password": "correct-horse-battery"})
    )
    org = client.post(
        "/orgs",
        json={"name": "Private Firm", "slug": f"private-{RUN}"},
        headers=auth(owner["access_token"]),
    ).json()
    CREATED_ORGS.append(org["organization_id"])

    r = client.get(
        f"/orgs/{org['organization_id']}/members", headers=auth(stranger["access_token"])
    )
    assert r.status_code == 404, r.text


# ── erasure ───────────────────────────────────────────────────────────────


def test_erasure_deletes_locally_and_records_the_instruction():
    """The invariant is that the instruction SURVIVES, whatever the AI service
    does.

    Deliberately not asserting a single outcome: with the AI service running
    this delivers, and with it down it stays pending with the error recorded.
    Both are correct. Pinning one would make the test a check on whether a
    second process happened to be up, and an earlier version of this test did
    exactly that — it asserted `pending` because the AI service had no erasure
    endpoint, so closing that gap turned a passing test red for the right
    reason. What must never happen is a deleted user with no record of the
    instruction, which is a silent compliance failure.
    """
    body = track(
        client.post("/auth/signup", json={"email": email("erase"), "password": "correct-horse-battery"})
    )
    user_id = body["user"]["user_id"]

    r = client.delete("/auth/me", headers=auth(body["access_token"]))
    assert r.status_code == 202, r.text
    assert r.json()["user_id"] == user_id
    assert r.json()["attempts"] == 1

    status = r.json()["status"]
    assert status in ("delivered", "pending"), status
    if status == "pending":
        # Undelivered is acceptable; undelivered and *unexplained* is not.
        assert r.json()["last_error"], "a failed delivery must record why"

    with tx() as db:
        assert db.scalar(select(User).where(User.id == user_id)) is None, "user must be gone"
        record = db.scalar(select(ErasureRequest).where(ErasureRequest.user_id == user_id))
        assert record is not None, "the instruction must outlive the user"
        assert record.status == status
        if status == "delivered":
            assert record.delivered_at is not None


# ── health ────────────────────────────────────────────────────────────────


def test_readyz_reports_the_key_and_the_database():
    r = client.get("/readyz")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert r.json()["kid"]


def test_jwks_is_served_and_public_only():
    r = client.get("/.well-known/jwks.json")
    assert r.status_code == 200
    for entry in r.json()["keys"]:
        assert "d" not in entry


TESTS = [
    test_anonymous_gets_a_real_token,
    test_signup_upgrades_the_anonymous_user_in_place,
    test_signup_without_an_anonymous_token_creates_a_new_user,
    test_duplicate_email_is_refused_without_confirming_it_exists,
    test_email_is_case_insensitive,
    test_login_and_wrong_password,
    test_me_returns_the_caller,
    test_no_token_is_401,
    test_refresh_rotates_the_token,
    test_reusing_a_spent_refresh_token_revokes_the_family,
    test_logout_revokes_the_family,
    test_org_creation_puts_org_and_role_in_the_next_token,
    test_members_can_be_added_and_the_last_owner_cannot_be_removed,
    test_a_stranger_gets_404_not_403_for_someone_elses_org,
    test_erasure_deletes_locally_and_records_the_instruction,
    test_readyz_reports_the_key_and_the_database,
    test_jwks_is_served_and_public_only,
]


def cleanup() -> None:
    """Remove everything this run created. Test rows in a real database are
    only acceptable if they do not accumulate."""
    with tx() as db:
        if CREATED_ORGS:
            db.execute(delete(Organization).where(Organization.id.in_(CREATED_ORGS)))
        if CREATED_USERS:
            db.execute(delete(User).where(User.id.in_(CREATED_USERS)))
            db.execute(
                delete(ErasureRequest).where(ErasureRequest.user_id.in_(CREATED_USERS))
            )


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
    cleanup()
    print()
    print(f"{len(TESTS) - failures}/{len(TESTS)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
