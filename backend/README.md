# Law Agent — Backend

Identity, organizations, and billing. The Node half of the system; the Python
half in [`app/`](../app) does retrieval and conversations.

The two share one Postgres instance and nothing else. No foreign key crosses
between them, no table is read by both, and no code is imported across. The
only coupling is a signed token and the public key that validates it — which is
what lets the two halves be written in different languages at all.

| | This service | AI service |
|---|---|---|
| Language | Node / NestJS | Python / FastAPI |
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

Creates `product_owner` and `product_service`, gives them `public`, and — the
part that matters — **revokes their access to `ai`**. The AI service's own
bootstrap already revoked `ai_service` from `public`. Between the two files,
neither service can read the other's tables despite sharing a database.

### 2. Install and configure

```bash
cd backend
npm install
cp .env.example .env
```

### 3. Schema

The identity tables already exist — they were created by the Python service's
Alembic migration and Prisma **introspects** rather than authors them:

```bash
npx prisma db pull      # reads the live schema
npx prisma generate     # builds the client into backend/generated/
```

The billing tables come from a hand-written migration:

```bash
psql -h 127.0.0.1 -U product_owner -d law_agent \
     -f prisma/migrations/0002_billing.sql
```

Hand-written rather than `prisma migrate dev` for a concrete reason: migrate
needs a **shadow database**, which means `CREATEDB` on the migrating role.
`product_owner` deliberately doesn't have it. Explicit DDL applied as the owner
keeps the least-privilege design intact and matches how `ai` is migrated.

### 4. A signing key

```bash
npm run keygen
```

Nothing works before this. With no key the service can neither issue nor
verify, and `/readyz` says exactly that.

**Migrating from the Python service?** Copy its PEMs instead — they work
unchanged:

```bash
cp ../product/keys/*.pem keys/
npm run kid:check     # proves Node derives the same kid
```

Both implement RFC 7638, so the `kid` is identical and the AI service cannot
tell which implementation signed a token.

### 5. Run

```bash
npm run build && npm start      # http://127.0.0.1:8001
npm run start:dev               # watch mode
```

### 6. Point the AI service at it

In the **root** `.env`:

```bash
JWKS_URL=http://127.0.0.1:8001/.well-known/jwks.json
JWT_ISSUER=http://127.0.0.1:8001
JWT_AUDIENCE=law-agent-ai
ADMIN_API_KEY=<same value as backend/.env>
```

Setting `JWKS_URL` makes `DEV_ALLOW_UNVERIFIED_TOKENS` unreachable in the AI
service, so real verification cannot be accidentally left off.

---

## Endpoints

### Public keys

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/.well-known/jwks.json` | What the AI service fetches and caches |
| `GET` | `/.well-known/openid-configuration` | Discovery, so issuer and JWKS URL aren't hard-coded twice |

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
| `POST` | `/orgs/{id}/members` | Add or re-role (owner/admin only) |
| `DELETE` | `/orgs/{id}/members/{userId}` | Remove; refuses the last owner |

### Billing

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/consultations/price` | What a consultation costs |
| `POST` | `/consultations` | Buy a consultation; returns a Paymob payment key |
| `GET` | `/consultations` | The caller's consultations |
| `GET` | `/consultations/{id}` | One consultation |
| `POST` | `/webhooks/paymob` | Gateway callback — **no auth guard**, HMAC verified |

**The buyer does not name the price.** `POST /consultations` takes no
`amount_cents` and no `currency`; both come from `CONSULTATION_PRICE_CENTS` and
`CONSULTATION_CURRENCY`. They used to be request fields, which meant the amount
charged was whatever the browser sent — a number anyone could edit in devtools
before paying. A client still sending them now gets `400 invalid_request`
naming the field, rather than having its price silently ignored.

`GET /consultations/price` exists so the frontend can put a figure on the
button without holding one of its own. It needs a token but not an account: an
anonymous visitor sees the offer before they sign up, which is the funnel.

### Maintenance — requires `X-Admin-Key`

| Method | Path | Purpose |
|---|---|---|
| `DELETE` | `/admin/users/{userId}` | Erase a user and instruct the AI service |
| `GET` | `/admin/erasure` | Requests not yet delivered |
| `POST` | `/admin/erasure/retry` | Re-attempt delivery |

The admin key opens exactly these, and **none of them can read a
conversation**. They exist because their callers are services, not people: a
payment gateway webhook carries no user identity, and erasure is cross-tenant
by definition. Unset `ADMIN_API_KEY` makes them return `503` — fail-closed.

### Dashboard — `X-Admin-Key` **or** an `owner`/`admin` token

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/admin/billing/stats` | Totals and revenue |
| `GET` | `/admin/billing/consultations` | Full list, `?status=`, `?limit=`, `?offset=` |
| `GET` | `/admin/billing/unescalated` | Paid but not handed to a lawyer |
| `POST` | `/admin/billing/retry-escalations` | Re-attempt escalation |

Two different callers reach these: a cron retrying escalations, which has no
user and cannot log in, and a person looking at the dashboard. Each gets the
credential that suits it.

That split is why `dashboard/` no longer asks for the admin key. It used to,
holding it in `localStorage` — so every dashboard user had a copy of the
service-to-service secret, any XSS on that origin exfiltrated it, and it could
neither be revoked for one person nor attributed to anyone. It now signs in at
`/auth/login` and sends the resulting token. Grant access by giving someone an
`admin` or `owner` membership; revoke it by removing the membership.

---

## The token

```json
{
  "iss": "http://127.0.0.1:8001",
  "aud": ["law-agent-ai", "law-agent-product", "law-agent-payments"],
  "sub": "01M0FA00YPHB9YWFHZKBDWC2JC",
  "org_id": "01M0FA0...",
  "role": "client",
  "typ": "access",
  "iat": 1787000000, "nbf": 1787000000, "exp": 1787000900,
  "jti": "..."
}
```

`sub`, `org_id`, `role`, and `anon` are the entire wire contract — they become
`Principal` in [`app/api/auth.py`](../app/api/auth.py). Renaming any of them
breaks the other service silently, at runtime, as a 401 with no obvious cause.

**Absent claims are absent, never empty strings.** A user in no organization
gets no `org_id` — an empty string is a *different* RLS tenant key on the AI
side and would bucket every individual user together under it.

Access tokens last 15 minutes; refresh tokens 30 days. The short access TTL is
the design, not caution: **there is no denylist.** A stolen access token is
valid until it expires. The refresh token carries the long session precisely
because it *is* revocable — opaque random bytes, stored only as a SHA-256 hash.

Refresh rotates with reuse detection. Presenting a spent token means it leaked,
so the whole family is revoked; at that point the attacker and the real user
are indistinguishable.

---

## Key rotation

```bash
npm run keygen      # writes a second PEM
npm run kid:check   # lists what is on disk
```

JWKS now publishes **both** keys, so tokens signed by either verify. Set
`ACTIVE_KID` to the new kid and restart. Delete the old PEM only after every
token it signed has expired.

Step one is the one people skip. Pull the old public key early and every
request in flight fails at once — staggered by an hour of verifier caching, so
it reads as an intermittent bug rather than a rotation mistake.

---

## Paymob

Three things about the callback are worth knowing before touching that code.

**The HMAC is over twenty named fields in a fixed order, not the body.**
`HMAC_FIELDS` in [`src/billing/paymob.service.ts`](src/billing/paymob.service.ts)
is that order. Do not sort it, reorder it, or tidy it. Booleans render
lowercase and absent fields render empty — the two places a port silently
diverges.

**The signature is the authentication.** A gateway callback carries no bearer
token and cannot; Paymob does not know our users. The HMAC is checked before
anything in the payload is trusted, and a rejected callback is recorded and
acted on by nothing.

**Bad signatures still answer `200`.** A 4xx makes the gateway retry, and
retrying a forged callback forever helps nobody.

### ⚠️ Unverified

The field order has **not** been checked against a real Paymob callback. The
simulator builds the concatenation from the same constant the verifier uses, so
if the order is wrong both are wrong together and the tests still pass. **Send
one sandbox transaction and confirm it verifies before going live.**

```bash
npm run webhook:simulate <consultationId>              # a valid signed callback
npm run webhook:simulate <consultationId> --bad-hmac   # a forged one
npm run webhook:simulate <consultationId> --fail       # a declined payment
```

---

## Tests

Run from the repository root, with this service on `:8001`:

```bash
python -m product.tests.test_node_parity          # 46 checks
python -m product.tests.test_payment_loop         # 21 checks, needs the AI service too
```

`test_node_parity.py` is black-box over HTTP and passes against **both** this
service and the Python implementation on `:8002`. Running it against both is
what makes it a fair test rather than a Node-shaped one — and it is the gate
that has to keep passing before `product/` can be deleted.

---

## Toolchain notes

Four choices that look arbitrary and are not:

- **`jose@5`, not 6.** v6 is ESM-only; NestJS and the Prisma client are
  CommonJS. v5 ships a real CJS build with identical crypto.
- **TypeScript 6, not 7.** The Nest CLI needs the programmatic compiler API,
  which 7.0 dropped and 7.1 is expected to restore.
- **`tsx`, not `ts-node`.** ts-node crashes against TS 6/7 (`ts.sys` undefined).
- **`incremental: false`.** `nest build`'s `deleteOutDir` clears `dist/` but
  leaves `tsconfig.tsbuildinfo`, so the next build decides everything is
  current and emits nothing — a build that "succeeds" with an empty `dist/`
  and no error.

Argon2 parameters are pinned to `m=65536, t=3, p=4` — argon2-cffi's defaults,
not this library's. Hashes are portable both ways, but hashing at
`@node-rs/argon2`'s weaker defaults would make `needsRehash` re-hash every
migrated user's password *down* on their next login.

---

## What is deliberately not built

- **The lawyer dashboard** and everything after escalation.
- **Email delivery** — so no verification mail and no password reset. The
  `email_verified_at` column exists and nothing sets it.
- **OAuth / social login.**
- **Rate limiting.** `/auth/login` is unthrottled; put it behind something
  before this is public.
- **Refunds.** The schema has the states; nothing drives them.

## Before this leaves a laptop

- `PAYMOB_HMAC_SECRET` in `.env` is a placeholder that exists to make the money
  path testable. Replace it.
- Database role passwords are still the bootstrap defaults.
- `CORS_ORIGIN_REGEX` allows localhost only.
- The admin endpoints should sit on an internal route or behind network rules,
  not on the public frontend origin.

## Services (the catalogue)

`services` is what the site sells: slug, Arabic name and description, price,
`active`, and three flags — `needs_conversation` (the lawyer gets the chat
transcript and the chat locks on payment, as consultations always did),
`needs_notes` (the client must describe the case before paying) and
`suggestable` + `ai_hint` (whether and when the assistant may propose it).
Edited only through `/admin/billing/services` (staff token), never deleted:
orders reference services, so "remove" is `active = false`.

The orders table is still called `consultations`. Each row snapshots
`service_name` and `amount_cents` at purchase and carries `client_notes`.
`POST /consultations` takes `service_id` (id or slug; absent means
`consultation`, for the plugin release that predates the catalogue) and
prices from the row — `CONSULTATION_PRICE_CENTS` only seeded the migrated
default. Statuses: `pending`, `paid`, `in_progress`, `completed`,
`cancelled`, `refunded`; the middle two are set by staff via
`PATCH /admin/billing/consultations/:id/status`, the money ones by the
webhook. One open order per service per user; an unpaid checkout counts as
open for 30 minutes.

`GET /services` is public and unauthenticated — it is a price list, and the
AI service reads it with no user in hand.

Migration: `prisma/migrations/0005_services.sql`, as `product_owner`.
