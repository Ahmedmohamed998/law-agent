# Law Agent — AI Service API

HTTP contract for the Arabic labour-law assistant. This service owns
conversations, retrieval, and provenance. It does **not** own users, auth,
billing, or the lawyer dashboard — those belong to the product backend, which
lives in [`backend/`](../backend) and runs on `:8001`.

- Base path: `/v1`
- Content type: `application/json; charset=utf-8`
- Interactive schema while the service is running: `/docs`

---

## 1. Who does what

| Concern | Owner |
|---|---|
| Users, passwords, roles, organizations | Product backend |
| Issuing and rotating tokens | Product backend |
| Subscriptions, payment, lawyer dashboard | Product backend |
| **Verifying** tokens | This service |
| Conversations, messages, citations, feedback | This service |
| Chunking, embeddings, retrieval, generation | This service |

There is no shared database table and no foreign key between the two domains.
The only coupling is the token format and this contract.

## 2. Where the frontend points

Call **this service directly** for the chat endpoints. Do not proxy them
through the product API.

Generation takes roughly 8–17 seconds and is streamed. A proxy hop buffers
`text/event-stream` by default — nginx does, and most app frameworks want to
materialise a response body before forwarding it — so the whole answer arrives
as one lump at the end instead of streaming. This works locally and fails in
staging, which is the worst place to discover it.

The service sends `X-Accel-Buffering: no`; if anything sits in front of it,
that layer also needs `proxy_buffering off`.

CORS is handled here. The allowed origin pattern is currently localhost only —
tell us the frontend's real origin before deploying.

## 3. Authentication

Every `/v1` endpoint requires:

```
Authorization: Bearer <JWT>
```

The token is issued and signed by the **product backend**; this service only
verifies it. What we need from that side:

| Requirement | Value |
|---|---|
| Algorithm | `RS256` or `EdDSA` — asymmetric, never a shared secret |
| Key distribution | A JWKS endpoint we can fetch and cache |
| `sub` | The user id. Becomes `ai.sessions.user_id`, opaque to us |
| `aud` | Must be `law-agent-ai` — a token minted for the product API is rejected here |
| `iss` | The product backend's issuer URL |
| `exp` | Required |
| `org_id` | Optional. The tenant. Enforced by row-level security |
| `role` | Optional. `client` by default |
| `anon` | Optional. `true` for anonymous, pre-signup users |

**Identity is never accepted from the request.** No endpoint takes a user id in
a path, query, or body. If it did, reading someone else's conversations would
be a matter of editing one JSON field.

### Anonymous users

The funnel needs people asking questions before they have an account, so
`user_id` is `NOT NULL` and anonymous sessions get a **token too** — the product
backend issues one with `sub: "anon_7f3c"` and `anon: true`. On signup, the
backend calls us to reassign those sessions to the real `sub`.

That reassignment endpoint is **not built yet**; it needs the signup flow
defined first.

## 4. Errors

One envelope for everything:

```json
{ "error": { "code": "session_not_found", "message": "no such session" } }
```

| HTTP | `code` | When |
|---|---|---|
| 401 | `unauthenticated` | Missing, malformed, expired, or wrongly-audienced token |
| 404 | `session_not_found` | No such session, **or** it belongs to someone else |
| 409 | `session_escalated` | The conversation is with a lawyer; the model will not answer |
| 503 | `not_ready` | Index not loaded or database unreachable |
| 503 | `auth_unconfigured` | `JWKS_URL` unset in a deployment that requires it |
| — | `upstream_unavailable` | Azure failed mid-generation (SSE `error` event) |
| — | `internal_error` | Unexpected failure (SSE `error` event) |

| 403 | `forbidden` | Missing or wrong `X-Admin-Key` on a service-to-service endpoint |
| 503 | `admin_unconfigured` | `ADMIN_API_KEY` unset where a service-to-service endpoint needs it |
| 422 | `invalid_request` | Request body failed validation |

404 is deliberately the same whether the session never existed or belongs to
another user; distinguishing them would leak which session ids are real.

Every status uses the envelope above, including 401 and 422. (Earlier versions
of this service returned FastAPI's raw `{"detail": …}` for both, contradicting
this section — if you wrote a client against that, it now gets `error.code`
like everything else.)

`message` is for developers. User-facing Arabic copy belongs in the frontend.

---

## 5. Endpoints

### `POST /v1/sessions` → 201

```json
{ "lang": "ar" }
```

```json
{
  "session_id": "01KZP1NXSEZADP6AHVC9A5CZ6C",
  "status": "active",
  "title": null,
  "lang": "ar",
  "created_at": "2026-08-10T17:25:58.697768+03:00",
  "last_active_at": "2026-08-10T17:25:58.697768+03:00"
}
```

`title` is filled from the first user message. `status` is `active`,
`escalated`, or `closed`.

### `GET /v1/sessions?limit=30` → 200

Array of the above, newest activity first. Only the caller's own sessions.

### `DELETE /v1/sessions/{session_id}` → 204

Soft delete. The session disappears from listings and history immediately.

### `GET /v1/sessions/{session_id}/messages` → 200

```json
[
  {
    "message_id": "01KZ…",
    "seq": 1,
    "role": "user",
    "content": "كم مدة الإجازة السنوية؟",
    "source_label": null,
    "sources": [],
    "created_at": "2026-08-10T17:26:01+03:00"
  },
  {
    "message_id": "01KZ…",
    "seq": 2,
    "role": "assistant",
    "content": "مدة الإجازة السنوية …",
    "source_label": "documents",
    "sources": [
      {
        "citation": "نظام العمل، المادة التاسعة بعد المائة",
        "doc_title": "نظام العمل",
        "article_label": "التاسعة بعد المائة",
        "reason": "hybrid"
      }
    ],
    "created_at": "2026-08-10T17:26:10+03:00"
  }
]
```

**Order by `seq`, never by `created_at`.** Two messages can land in the same
millisecond, and a timestamp sort will occasionally render an answer above its
own question.

### `POST /v1/sessions/{session_id}/messages` → 200

Blocking. Use the streaming variant for the chat UI; this one is for scripts
and tests.

```json
{
  "content": "كم مدة الإجازة السنوية؟",
  "client_message_id": "turn-1-aaa"
}
```

```json
{
  "message_id": "01KZ…",
  "seq": 2,
  "content": "مدة الإجازة السنوية في نظام العمل السعودي هي **21 يومًا** …",
  "source_label": "documents",
  "sources": [ … ],
  "latency_ms": 9206
}
```

`content` is Markdown — bold, lists, and tables (penalty schedules come back as
real Markdown tables). Render it as Markdown with `dir="rtl"`.

### `POST /v1/sessions/{session_id}/messages/stream` → `text/event-stream`

Same request body. Named events, in this order:

| Event | Payload | When |
|---|---|---|
| `meta` | `{"message_id", "seq"}` | Immediately — render the empty bubble |
| `sources` | `{"sources": [...]}` | Before the first token |
| `token` | `{"delta": "…"}` | Repeatedly. **Append**, don't replace |
| `done` | `{"source_label", "latency_ms"}` | Final |
| `error` | `{"code", "message"}` | Instead of `done` |

`sources` arrives first on purpose: retrieval takes under a second and
generation about nine, so citations can render while the answer is still
typing.

```
event: meta
data: {"message_id": "01KZ…", "seq": 4}

event: sources
data: {"sources": [{"citation": "لائحة العمالة المنزلية…", "reason": "hybrid", …}]}

event: token
data: {"delta": "بالنسبة "}

event: done
data: {"source_label": "documents", "latency_ms": 8676}
```

`delta` is the **new tail only**. Concatenate them.

A replayed `client_message_id` streams the stored answer back as a single
`token` event, with `"replayed": true` on `meta`.

### `POST /v1/messages/{message_id}/feedback` → 204

```json
{ "rating": "up", "reason": "optional free text" }
```

One rating per message; posting again overwrites it.

### `POST /v1/sessions/{session_id}/escalate` → 204

Flips the session to `escalated`. Every later message on it returns
`409 session_escalated` **without calling the model**. This is the circuit
breaker for handing a conversation to a lawyer.

---

## 5b. Service-to-service endpoints

Two endpoints the *other* services call. They authenticate with a shared header
rather than a user token, because their callers are services, not people:

```
X-Admin-Key: <ADMIN_API_KEY>
```

Neither can read a conversation. Both are outside the per-user model by
necessity — erasure is cross-tenant by definition, and a payment gateway
webhook carries no user identity at all. **Do not expose these to the
frontend**; they belong on an internal route or behind network rules.

### `DELETE /v1/users/{user_id}/data` → 200

```json
{ "user_id": "01KZ…", "sessions_deleted": 3 }
```

Hard-deletes every session, message, source row and feedback for that user.
This is the contract that replaces the missing foreign key: nothing cascades
from your users table, so you must tell us.

Idempotent — purging an already-purged user deletes nothing and still returns
200, so a retry after an ambiguous failure is always safe.

### `POST /v1/admin/sessions/{session_id}/escalate` → 204

Flips a session to `escalated` without a user token, so a paid-consultation
webhook can trigger it. Same effect as the user-authenticated
`/v1/sessions/{id}/escalate`: every later message returns `409` and the model
is never called again.

Idempotent, because payment gateways retry.

---

### `GET /healthz` · `GET /readyz`

`healthz` is liveness. `readyz` returns `{"ok": true, "index_version": "4011c-…"}`
and only succeeds when the retrieval index is loaded *and* the database pool
hands out a connection. Point your load balancer at `readyz`.

---

## 6. `client_message_id` is required

Pick something stable per user action — a UUID generated when the user hits
send, reused across retries.

The frontend *will* double-submit: network retries, impatient double-clicks,
effects firing twice in development. Without this key each one is a duplicate
row and a duplicate Azure charge. With it, the second submit returns the
**same** `message_id` and costs nothing.

Do not regenerate it on retry. That defeats the entire mechanism.

## 7. `source_label`

Every assistant message carries one. It is a structured field, not something to
parse out of the Arabic text.

| Value | Meaning | Suggested treatment |
|---|---|---|
| `documents` | Entirely from the official corpus | Normal |
| `mixed` | Partly from the model's own knowledge | Warning badge |
| `model_knowledge` | Nothing in the corpus covered it | Warning badge |
| `refused` | Declined to answer | Offer the consultation CTA |

For `mixed` and `model_knowledge`, the answer text itself also tags the
unsourced sentences inline and adds a note that the material may be out of
date. The badge is in addition to that, not instead of it.

## 8. Follow-up questions

Handled server-side. Send the new message as-is — do **not** concatenate it with
earlier turns or resend history.

The service resolves references against the last few turns before retrieval, so
«وهل ينطبق على العامل المنزلي؟» becomes a standalone query and retrieves
لائحة العمالة المنزلية. Sending a pre-joined string instead would defeat that
and degrade retrieval.

## 9. Data and deletion

Sessions contain dismissals, unpaid-wage disputes, and salary details. Two
things the product backend must decide before launch:

1. **Retention.** How long conversations are kept. There is no automatic
   expiry today.
2. **Erasure.** There is no foreign key from `ai.sessions` to the users table,
   so **nothing cascades** when a user is deleted on your side. You must tell
   us explicitly — `DELETE /v1/users/{id}/data`, documented in §5b. Settled and
   built; the product backend records every instruction in
   `public.erasure_requests` before attempting delivery, so a failed call costs
   a retry rather than the record.

## 10. Running it locally

```bash
docker compose up -d                    # or an existing local Postgres
psql -U postgres -d law_agent -f sql/bootstrap.sql
alembic upgrade head
python -m app.api.main                  # http://127.0.0.1:8000
```

Environment (see `.env.example`): `DATABASE_URL`, `DATABASE_MIGRATE_URL`,
`JWKS_URL`, `JWT_AUDIENCE`, plus the Azure OpenAI keys.

**Without a token issuer**, set `DEV_ALLOW_UNVERIFIED_TOKENS=true` and send the
user id as the bearer token:

```bash
curl -X POST http://127.0.0.1:8000/v1/sessions \
  -H "Authorization: Bearer u_ahmed" -H "Content-Type: application/json" \
  -d '{"lang":"ar"}'
```

This is ignored the moment `JWKS_URL` is set, so it cannot accidentally ship
with verification disabled.

### Deployment shape

Run **one worker with many threads**, not `--workers 4`. The retrieval index is
held in memory and built once at startup; each extra process is another full
copy of it. Scale threads first, and only add processes after measuring memory.

---

## 11. Open questions for the product backend

Four decisions this contract depended on. Three are now settled:

1. ~~**Token format**~~ — settled: RS256, JWKS, `aud` includes `law-agent-ai`.
   Implemented in `backend/` (Node/NestJS), which signs every token and is the
   only holder of a private key. `product/` is the earlier Python
   implementation of the same contract, kept as the reference the Node port is
   measured against — see `product/tests/test_node_parity.py`, which passes
   against both.
2. ~~**Anonymous tokens**~~ — settled, and the "claim this session" call is no
   longer needed. An anonymous visitor gets a real user row; signup upgrades it
   **in place** and keeps the id, so their conversations are already theirs. No
   reassignment endpoint exists because nothing needs reassigning.
3. ~~**Erasure**~~ — settled: a call to us. See §5b.
4. **Frontend origin** — still open. Set `CORS_ORIGIN_REGEX`; both services
   allow localhost only today.

Remaining, and not on the identity side:

- **Retention.** How long conversations are kept. No automatic expiry exists.
- **Escalation trigger.** Whether the frontend calls escalate after payment
  confirms, or the payments service calls §5b server-side. The second is safer;
  the endpoint is built either way.
