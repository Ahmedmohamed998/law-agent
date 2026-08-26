# Deployment

Everything runs in Docker except nginx, which is native on the host.

| Host | Serves | Container port |
|---|---|---|
| `tafahom.thetransformix.com` | WordPress + the chat widget | — |
| `staging.tafahom.thetransformix.com` | Product backend (Node) | 8001 |
| `staging2.tafahom.thetransformix.com` | AI service (Python) | 8000 |

Compose publishes both ports as `127.0.0.1:800x`, so native nginx proxies to
them exactly as it would to a bare process — the vhosts here need no Docker
awareness at all. Publishing on `0.0.0.0` instead would put an unencrypted
API on the public internet beside the TLS one.

## First deploy

```bash
git clone <repo> /srv/law-agent && cd /srv/law-agent
git lfs pull                       # data/chroma is LFS; without it the index is missing

cp .env.example .env               # AI service + POSTGRES_PASSWORD
cp backend/.env.example backend/.env
# fill both in — see the comments in each file

docker compose up -d db

# Roles and schemas: superuser, once.
docker compose exec -T db psql -v ON_ERROR_STOP=1 -U postgres -d law_agent < sql/bootstrap.sql
docker compose exec -T db psql -v ON_ERROR_STOP=1 -U postgres -d law_agent < product/sql/bootstrap_product.sql

# Identity tables (Prisma introspects these, it does not author them).
docker compose run --rm --no-deps -v "$PWD/product:/app/product" \
    -e PRODUCT_DATABASE_MIGRATE_URL="postgresql+psycopg://product_owner:PASS@db:5432/law_agent" \
    ai alembic -c product/alembic.ini upgrade head

# Billing tables — AS product_owner, NOT as postgres. The grant to
# product_service comes from ALTER DEFAULT PRIVILEGES FOR ROLE product_owner,
# which only applies to tables that role creates. Run these as a superuser and
# the application gets `42501 permission denied` on its first write.
docker compose exec -T -e PGPASSWORD=PASS db \
    psql -v ON_ERROR_STOP=1 -U product_owner -h 127.0.0.1 -d law_agent \
    < backend/prisma/migrations/0002_billing.sql
docker compose exec -T -e PGPASSWORD=PASS db \
    psql -v ON_ERROR_STOP=1 -U product_owner -h 127.0.0.1 -d law_agent \
    < backend/prisma/migrations/0003_consultation_handover.sql

# change the four role passwords from the bootstrap defaults, then put the
# real ones in the two .env files
docker compose exec db psql -U postgres -d law_agent

docker compose build
docker compose run --rm ai alembic upgrade head
docker compose run --rm tools npm run keygen      # writes ./backend/keys
docker compose run --rm tools npm run kid:check   # ACTIVE_KID for backend/.env

docker compose up -d
docker compose ps                                 # both must reach (healthy)
```

`tools` is the backend's build stage, kept out of the running stack by a
compose profile. It is where `keygen`, `kid:check`, `db:check` and
`webhook:simulate` live, because they need `tsx` and the TypeScript sources —
neither of which belongs in a runtime image.

## Docker specifics worth knowing

**Hostnames, not localhost.** Inside the compose network the database is
`db`, the backend is `backend`, the AI service is `ai`. A container pointed at
`localhost` resolves to itself; it is the classic first-deploy failure.

**`JWKS_URL` is internal, `JWT_ISSUER` is public.** The AI service fetches
keys over the compose network (`http://backend:8001/.well-known/jwks.json`)
rather than leaving the host and coming back through nginx. `JWT_ISSUER` is
different — it is compared against the `iss` claim the backend signed, so it
must be the public HTTPS URL.

**`BIND_HOST=0.0.0.0` in containers.** Both services default to loopback,
which is right on bare metal and useless in a container: `127.0.0.1` there is
the container's own loopback and nothing, not even the host, can reach it.
Compose overrides it, and the published `127.0.0.1:800x` port is what keeps
the service private.

**Two bind mounts, deliberately.** `./data` holds the 79MB Chroma index,
which Chroma opens read-write even to query and which should survive an image
rebuild. `./backend/keys` holds the private signing key: a key baked into an
image is a key in every registry and layer cache that image reaches. Back up
`backend/keys` separately — losing it invalidates every refresh token in
circulation.

**Rebuilding after a code change:**

```bash
git pull && docker compose up -d --build
```

Restarting the AI service rebuilds the BM25 index over 4,011 chunks before
the port answers, which is why its healthcheck has a 180s start period.

## nginx (native, on the host)

```bash
# nginx: zones first (they live in http{}), then the snippets, then the vhosts.
cp deploy/nginx/00-law-agent-limits.conf          /etc/nginx/conf.d/
cp deploy/nginx/snippets-law-agent-proxy-backend.conf \
   /etc/nginx/snippets/law-agent-proxy-backend.conf
cp deploy/nginx/snippets-law-agent-proxy-ai.conf \
   /etc/nginx/snippets/law-agent-proxy-ai.conf
cp deploy/nginx/staging*.conf                     /etc/nginx/sites-available/
ln -s /etc/nginx/sites-available/staging.tafahom.thetransformix.com.conf  /etc/nginx/sites-enabled/
ln -s /etc/nginx/sites-available/staging2.tafahom.thetransformix.com.conf /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
```

There are no systemd units for the services themselves. Compose supervises
them (`restart: unless-stopped`), and the stack comes back with the Docker
daemon at boot:

```bash
systemctl enable docker
```

Two mechanisms racing to start the same process is worse than either alone.

## The one setting that matters

`staging2` carries `proxy_buffering off`. Generation takes 8–17 seconds and is
delivered as `text/event-stream`; nginx buffers that content type by default,
which holds the whole answer and delivers it in one lump at the end. The page
looks frozen, then fills instantly.

It works on a laptop with no proxy in front, so this fails for the first time
in staging. The service sends `X-Accel-Buffering: no` as its half of the fix.

Verify after any nginx change:

```bash
curl -N -X POST https://staging2.tafahom.thetransformix.com/v1/sessions/<id>/messages/stream \
     -H "Authorization: Bearer <token>" \
     -H "Content-Type: application/json" \
     -d '{"content":"كم مدة الإجازة السنوية؟","client_message_id":"smoke-1"}'
```

Text must trickle. Everything arriving at once means something is still
buffering — check any CDN in front as well.

## CORS is the application's

Neither vhost sets `Access-Control-*`. Both services answer preflight
themselves, from `CORS_ORIGIN_REGEX`, and a duplicated
`Access-Control-Allow-Origin` is rejected by every browser. Set it in the two
`.env` files, anchored:

```
CORS_ORIGIN_REGEX=^https://tafahom\.thetransformix\.com$
```

The anchors matter on the Node side: it passes the pattern to Express `cors`,
which uses `.test()` — a substring match. Unanchored,
`tafahom\.thetransformix\.com` also matches
`https://tafahom.thetransformix.com.example.net`. The Python side uses
`fullmatch` and is unaffected, but keep the two identical.

## Rate limits

`00-law-agent-limits.conf` throttles `/auth/login`, `/auth/anonymous` and
`/auth/signup`, which the chat widget makes internet-facing. There is no
application-level throttling; this is it.

Two deliberate exclusions:

- **`/webhooks/paymob`** — a gateway retries on failure, so throttling it
  loses money rather than protecting anything. The HMAC is what makes that
  endpoint safe to expose.
- **Per-user generation limits** — each streamed answer is a paid model call,
  but an IP is a poor proxy for a person behind a corporate NAT. That limit
  belongs in the application, where the caller's identity is known. The zone
  here is a backstop against a stuck client, not a usage policy.

## Internal endpoints

Both vhosts have a commented `allow`/`deny` block over their admin paths.
`/v1/admin/*` and `/admin/erasure` are called by the other service, never a
browser — if both run on this host, close them to the internet entirely.

`/admin/billing/*` is the exception: the dashboard reaches it from a browser
with a staff login, so restrict it by source address rather than blocking it.
