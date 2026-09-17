# Law Agent — تفاهم

Arabic labour-law assistant for a Saudi law firm. Two services joined by RS256
and a JWKS document, with WordPress as the marketing site, identity provider,
and chat client. Read `docs/API.md` for the wire contract and `backend/README.md`
for the identity/billing side before changing either.

## Where things run

| Host | What | Where |
|---|---|---|
| `tafahom.thetransformix.com` | WordPress + chat widget | Hostinger (not the VPS) |
| `staging.tafahom.thetransformix.com` | Node backend `:8001` | VPS `165.22.84.74` |
| `staging2.tafahom.thetransformix.com` | Python AI service `:8000` | same VPS |
| `dashboard.tafahom.thetransformix.com` | Admin dashboard (static) | same VPS |

The `staging` names are the permanent production names. The VPS is **shared
with ~9 other client projects** behind one nginx: always `nginx -t` before
reload and check other sites after. Everything lives in `/srv/law-agent` under
docker compose; secrets in `.env`, `backend/.env`, and
`/root/law-agent-credentials.txt` on the server. **Never in git.**

## Layout

- `app/` — Python AI service (FastAPI). Retrieval, conversations, SSE streaming.
- `backend/` — Node/NestJS. Identity, tokens, consultations, Paymob, admin API.
- `product/` — the earlier Python identity service, kept as the parity reference. Its Alembic migration creates `public.users` and must run before the billing SQL.
- `dashboard/` — Vite/React admin dashboard. Build with `VITE_BACKEND_URL` set.
- `wordpress-plugin/law-agent-chat/` — **the canonical plugin source.** Build the zip with Python `zipfile` (POSIX paths), never PowerShell `Compress-Archive`. Shortcodes: `[law_agent_chat]`, `[law_agent_services]`, `[law_agent_consultations]`; WooCommerce My Account tab "طلباتي" at `/my-account-2/consultations/`.
- `app/suggest/` — the assistant proposing a service: catalogue cache, classifier, gates. Evaluation set `tests/suggestions.jsonl`.
- `deploy/` — nginx vhosts, rate limits, deployment README.

## Rules that were learned the hard way

- **Billing migrations run as `product_owner`, not `postgres`.** Grants to the app role come from `ALTER DEFAULT PRIVILEGES FOR ROLE product_owner` and only apply to tables that role creates. `0003` repairs ownership idempotently if this was done wrong.
- **Price is server-side.** `POST /consultations` takes `service_id`, never an amount; the price is the `services` row's, snapshotted onto the order. The widget reads `GET /services` (public). `CONSULTATION_PRICE_CENTS` only seeded the migrated default.
- **The catalogue is edited in the dashboard, nowhere else.** WordPress displays services; the AI service reads the same public list to know what it may suggest (`app/suggest`), and is off without `BACKEND_URL`.
- **Anonymous visitors never see a payment step in the chat.** Booking button and offers become "sign in" until there is an account; the backend refuses them anyway.
- **The admin key never reaches a browser.** Dashboard access is an `owner`/`admin` org membership; `/admin/billing/*` accepts either the key (for crons) or a staff token.
- **WordPress asserts, the backend signs.** `/auth/wordpress` verifies an HMAC assertion minted in PHP over `LAW_AGENT_SHARED_SECRET`; the AI service is untouched. WP-backed users have no `password_hash` and cannot use `/auth/login`.
- **Proxy the chat path through nothing.** SSE through PHP or a buffering proxy arrives as one lump. `staging2`'s vhost carries `proxy_buffering off`.
- **CORS is the application's.** Anchored regex, explicit alternation — a wildcard would admit every other project on the box.
- **Widget is single-theme.** The site has no dark mode; `prefers-color-scheme` was removed on purpose.

## Testing against production

```bash
# mint an anonymous token and stream a question
TOK=$(curl -s -X POST https://staging.tafahom.thetransformix.com/auth/anonymous \
  -H 'Content-Type: application/json' | jq -r .access_token)
```
See `product/tests/test_payment_loop.py` for the full funnel. `client_message_id`
must be ≥ 6 chars.

## Open work

See the last commits on `deploy/go-live` and `plugin/popup-embedding`. Known
gaps: the `refused` CTA fires on abuse (refund risk), no `delete_user` hook,
dashboard IP allow-list is commented out, no retention policy, retrieval
citations sometimes miss the article the answer cites.
