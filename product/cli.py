"""
Operational commands.

    python -m product.cli keygen              # create a signing key
    python -m product.cli keys                # list keys, mark the active one
    python -m product.cli jwks                # print the public document
    python -m product.cli token <user_id>     # mint a token, for testing
    python -m product.cli grant <user> <org> <role>

`keygen` is the one that has to run before anything else works: with no key the
service can verify nothing and issue nothing, and `/readyz` says so.
"""

import argparse
import json
import sys

from sqlalchemy import select

from product.config import settings
from product.db.base import tx
from product.db.models import Membership, Organization, User
from product.security import keys, tokens


def cmd_keygen(args) -> int:
    key = keys.generate()
    print(f"created {key.path}")
    print(f"kid: {key.kid}")
    existing = keys.load()
    if len(existing) > 1:
        print()
        print(f"{len(existing)} keys now on disk. JWKS publishes all of them, so")
        print("tokens signed by either still verify. To switch signing over:")
        print()
        print(f"  PRODUCT_ACTIVE_KID={key.kid}")
        print()
        print("Delete the old PEM only after every token it signed has expired")
        print(f"(access TTL is {settings().access_token_ttl_seconds}s).")
    else:
        print()
        print("This is the only key. Point the AI service at:")
        print(f"  JWKS_URL={settings().jwks_url}")
    return 0


def cmd_keys(args) -> int:
    loaded = keys.load()
    if not loaded:
        print(f"no keys in {settings().keys_dir}", file=sys.stderr)
        print("run: python -m product.cli keygen", file=sys.stderr)
        return 1
    try:
        active_kid = keys.active().kid
    except keys.NoSigningKey as exc:
        print(f"warning: {exc}", file=sys.stderr)
        active_kid = None

    for kid, key in loaded.items():
        marker = "* active" if kid == active_kid else "        "
        print(f"{marker}  {kid}  {key.path.name}")
    return 0


def cmd_jwks(args) -> int:
    print(json.dumps(keys.jwks(), indent=2))
    return 0


def cmd_token(args) -> int:
    """Mint an access token for an existing user, without a password.

    For wiring up the AI service and for curl. It goes through exactly the same
    signing path as a login, so a token from here is indistinguishable from a
    real one — which is the point, and also why this is a CLI command and not
    an endpoint.
    """
    with tx() as db:
        user = db.scalar(
            select(User).where(User.id == args.user_id, User.deleted_at.is_(None))
        )
        if user is None:
            print(f"no such user: {args.user_id}", file=sys.stderr)
            return 1
        membership = db.scalar(
            select(Membership).where(Membership.user_id == user.id)
        )
        access = tokens.issue_access_token(
            user_id=user.id,
            organization_id=membership.organization_id if membership else None,
            role=membership.role if membership else None,
            anonymous=user.is_anonymous,
        )
    print(access.token)
    return 0


def cmd_grant(args) -> int:
    with tx() as db:
        user = db.scalar(select(User).where(User.id == args.user_id))
        if user is None:
            print(f"no such user: {args.user_id}", file=sys.stderr)
            return 1
        org = db.scalar(
            select(Organization).where(Organization.id == args.organization_id)
        )
        if org is None:
            print(f"no such organization: {args.organization_id}", file=sys.stderr)
            return 1

        row = db.scalar(
            select(Membership).where(
                Membership.user_id == user.id,
                Membership.organization_id == org.id,
            )
        )
        if row is None:
            db.add(
                Membership(
                    user_id=user.id, organization_id=org.id, role=args.role
                )
            )
        else:
            row.role = args.role
    print(f"{args.user_id} is now {args.role} of {args.organization_id}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="product.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("keygen", help="create a new signing key").set_defaults(
        func=cmd_keygen
    )
    sub.add_parser("keys", help="list signing keys").set_defaults(func=cmd_keys)
    sub.add_parser("jwks", help="print the public JWKS document").set_defaults(
        func=cmd_jwks
    )

    p_token = sub.add_parser("token", help="mint an access token for a user")
    p_token.add_argument("user_id")
    p_token.set_defaults(func=cmd_token)

    p_grant = sub.add_parser("grant", help="set a user's role in an organization")
    p_grant.add_argument("user_id")
    p_grant.add_argument("organization_id")
    p_grant.add_argument(
        "role", choices=("owner", "admin", "lawyer", "client")
    )
    p_grant.set_defaults(func=cmd_grant)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
