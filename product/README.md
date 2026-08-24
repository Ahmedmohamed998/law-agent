# Law Agent — Product Backend

Identity, organizations, and token issuing. The other half of the system from
the AI service in [`app/`](../app): this side owns *who people are*, that side
owns *what they asked*.

The two share one Postgres instance and nothing else. No foreign key crosses
between them, no table is read by both, and no code is imported across. The
only coupling is a signed token and the public key that validates it.

| | This service | AI service |
|---|---|---|
| Port | `:8001` | `:8000` |
| Schema | `public` | `ai` |
| App role | `product_service` | `ai_service` |
| Owner role | `product_owner` | `law_agent_owner` |
| Signs tokens | **yes** — only holder of a private key | never |
| Verifies tokens | locally, own keys | via JWKS over HTTP |

---

## Setup

### 1. Roles and grants (once, as a superuser)

```bash
psql -U postgres -d law_agent -f product/sql/bootstrap_product.sql
```

This creates `product_owner` and `product_service`, gives them `public`, and —
the part that matters — **revokes their access to `ai`**. The AI service's own
bootstrap already revoked `ai_service` from `public`. Between the two files,
neither service can read the other's tables even though both connect to one
database.

### 2. Configuration

```bash
cp product/.env.example product/.env
```

Every variable is prefixed `PRODUCT_` so it cannot collide with the AI
service's `.env` in the same checkout.

### 3. Schema

```bash
alembic -c product/alembic.ini upgrade head
```

Its bookkeeping table is `alembic_version_product`, deliberately distinct from
the AI service's `alembic_version` — sharing one would make each migration run
treat the other's revisions as unknown heads.

### 4. A signing key

```bash
python -m product.cli keygen
```

Nothing works before this. With no key the service can neither issue nor
verify, and `/readyz` says exactly that.

### 5. Run

```bash
python -m product.main          # http://127.0.0.1:8001
```

### 6. Point the AI service at it

In the **root** `.env`:

```bash
JWKS_URL=http://127.0.0.1:8001/.well-known/jwks.json
JWT_ISSUER=http://127.0.0.1:8001
JWT_AUDIENCE=law-agent-ai
```

Setting `JWKS_URL` makes `DEV_ALLOW_UNVERIFIED_TOKENS` unreachable in the AI
service, so real verification cannot be accidentally left off.

---

## The endpoints

### Public keys

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/.well-known/jwks.json` | What the AI service fetches and caches |
| `GET` | `/.well-known/openid-configuration` | Discovery, so the issuer and JWKS URL are not hard-coded twice |

### Auth

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/auth/anonymous` | Token for a pre-signup visitor |
| `POST` | `/auth/signup` | Register — upgrades an anonymous user in place |
| `POST` | `/auth/login` | Email + password |
| `POST` | `/auth/refresh` | Rotate; detects reuse |
| `POST` | `/auth/logout` | Revoke the token family |
| `GET` | `/auth/me` | Profile and memberships |
| `DELETE` | `/auth/me` | Self-erasure |
| `POST` | `/auth/introspect` | Decode a token and say why it fails |

### Organizations

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/orgs` | Create; caller becomes owner |
| `GET` | `/orgs` | The caller's organizations |
| `GET` | `/orgs/{id}/members` | List members |
| `POST` | `/orgs/{id}/members` | Add or re-role a member (owner/admin only) |
| `DELETE` | `/orgs/{id}/members/{user_id}` | Remove; refuses the last owner |

### Maintenance — requires `X-Admin-Key`

| Method | Path | Purpose |
|---|---|---|
| `DELETE` | `/admin/users/{user_id}` | Erase a user here and instruct the AI service |
| `GET` | `/admin/erasure` | Requests not yet confirmed delivered |
| `POST` | `/admin/erasure/retry` | Re-attempt delivery |

---

## The token

```json
{
  "iss": "http://127.0.0.1:8001",
  "aud": ["law-agent-ai", "law-agent-product"],
  "sub": "01KZP1NXSEZADP6AHVC9A5CZ6C",
  "org_id": "01KZP2...",
  "role": "client",
  "typ": "access",
  "iat": 1787000000,
  "nbf": 1787000000,
  "exp": 1787000900,
  "jti": "..."
}
```

`sub`, `org_id`, `role`, and `anon` are the entire wire contract — they become
`Principal` in [`app/api/auth.py`](../app/api/auth.py). Renaming any of them
breaks the other service silently, at runtime, as a 401 with no obvious cause.
[`tests/test_contract.py`](tests/test_contract.py) is the guard against that.

`aud` is a **list**, so one token is valid at both APIs. PyJWT's `audience=`
check passes when the claim is a list containing the expected value, so the AI
service required no change to accept this.

Absent claims are absent, never empty strings. A user in no organization gets
no `org_id` — an empty string would be a *different* RLS tenant key on the AI
side and would quietly bucket every individual user together under it.

---

## Anonymous users, and why there is no session-reassignment endpoint

An anonymous visitor gets a **real user row and a real signed token**. Signup
then *upgrades that row in place* and keeps the same id, so `sub` never
changes.

The consequence is that conversations the AI service already stored under that
id simply belong to the registered account afterwards. No cross-service call,
no reassignment endpoint, no window in which someone's history is stranded
under a subject nobody owns.

The alternative — minting a throwaway `anon_7f3c` subject and reassigning
sessions at signup — was in the original design and is what
[`docs/API.md`](../docs/API.md) §3 still describes as an open question. This
implementation removes the question rather than answering it.

To carry history over, the frontend sends the anonymous token on the signup
call:

```
POST /auth/signup
Authorization: Bearer <anonymous token>

{"email": "...", "password": "..."}
```

Without that header the signup still succeeds — it just creates a new user, and
the anonymous conversations stay with the anonymous id.

---

## Token lifetimes

Access tokens last 15 minutes; refresh tokens 30 days.

The short access TTL is not caution, it is the design: **there is no denylist.**
A stolen access token is valid until it expires, and nothing can stop it. The
refresh token carries the long session precisely because it *is* revocable —
it is opaque random bytes stored as a SHA-256 hash, meaningful only against a
row.

Refresh is rotating with reuse detection. Every refresh mints a new token and
marks the old one used; presenting a spent token means it leaked, and the whole
family is revoked. At that point the attacker and the real user are
indistinguishable, so both are logged out.

---

## Key rotation

```bash
python -m product.cli keygen        # writes a second PEM
python -m product.cli keys          # lists both, marks the active one
```

JWKS now publishes **both** keys, so tokens signed by either verify. Then set
`PRODUCT_ACTIVE_KID` to the new kid and restart. Delete the old PEM only after
every token it signed has expired.

Step one is the one people skip. Pull the old public key before its tokens
expire and every request in flight fails at once — and because the AI service
caches JWKS for an hour, the failures arrive staggered and look like an
intermittent bug rather than a rotation mistake.

---

## Tests

```bash
python -m product.tests.test_contract
```

Verifies a minted token exactly the way the AI service verifies it: fetch
JWKS, select by `kid`, decode with `audience="law-agent-ai"`. Also checks that
JWKS leaks no private RSA components, that a wrong audience is rejected, and
that a rotation keeps old tokens valid.

It runs standalone or under pytest, and needs no database.

---

## What is deliberately not built

- **Billing, subscriptions, payments.** Including the paid consultation.
- **The lawyer dashboard** and everything after an escalation.
- **Email delivery** — so no verification mail and no password reset. The
  `email_verified_at` column exists and nothing sets it.
- **OAuth / social login.**
- **Rate limiting.** `/auth/login` is unthrottled; put it behind something
  before this is public.
- **The AI service's erasure endpoint.** `DELETE /v1/users/{id}/data` does not
  exist yet on that side — `repository.purge_user()` implements the deletion
  but is not exposed over HTTP. Until it is, every erasure lands in
  `erasure_requests` as `pending` with a recorded error, which is the correct
  visible state: recorded and undelivered, never lost.
