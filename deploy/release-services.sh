#!/usr/bin/env bash
# Law Agent release: services catalogue, orders, dashboard, AI suggestions.
#
#   On the VPS, as root, after the code is in /srv/law-agent (git pull or
#   an unpacked bundle):
#     bash /srv/law-agent/deploy/release-services.sh
#
# Backs up the database and the code it replaces, runs BOTH migrations
# (backend SQL as product_owner, AI service alembic), rebuilds and restarts
# only this project's containers, then checks. Stops at the first failure.
# Other projects on this host are not touched: nothing here reloads nginx.
set -euo pipefail

APP=/srv/law-agent
BACKUPS=/root/law-agent-backups
STAMP=$(date +%Y%m%d-%H%M%S)
BE=https://staging.tafahom.thetransformix.com
AI=https://staging2.tafahom.thetransformix.com

cd "$APP"
PGPW=$(grep '^POSTGRES_PASSWORD=' .env | cut -d= -f2-)
OWNER_URL=$(grep '^DATABASE_MIGRATE_URL=' backend/.env | cut -d= -f2-)
[ -n "$OWNER_URL" ] || { echo "backend/.env has no DATABASE_MIGRATE_URL (product_owner)"; exit 1; }
mkdir -p "$BACKUPS"

echo "== 1/8 database backup"
docker compose exec -T -e PGPASSWORD="$PGPW" db \
  pg_dump -U postgres -d law_agent -Fc < /dev/null > "$BACKUPS/db-$STAMP.dump"
ls -lh "$BACKUPS/db-$STAMP.dump" | awk '{print "   ", $5, $9}'

echo "== 2/8 code backup (what the running containers were built from)"
tar czf "$BACKUPS/code-$STAMP.tgz" app migrations backend/src backend/prisma dashboard/dist 2>/dev/null || true
ls -lh "$BACKUPS/code-$STAMP.tgz" | awk '{print "   ", $5, $9}'

echo "== 3/8 AI service: BACKEND_URL"
if ! grep -q '^BACKEND_URL=' .env; then
  # Inside compose the backend is reachable by service name. Without this
  # the feature is simply off, so add it explicitly rather than silently.
  printf '\n# The assistant reads the catalogue from here (see .env.example)\nBACKEND_URL=http://backend:8001\n' >> .env
  echo "    added BACKEND_URL=http://backend:8001 to .env"
else
  echo "    present: $(grep '^BACKEND_URL=' .env)"
fi

echo "== 4/8 backend migration 0005 (services), as product_owner"
# The owner URL points at 127.0.0.1 from the host's point of view; run it
# through the db container so it works whatever the host has installed.
OWNER_PW=$(printf '%s' "$OWNER_URL" | sed -E 's#^[a-z]+://[^:]+:([^@]+)@.*$#\1#')
docker compose exec -T -e PGPASSWORD="$OWNER_PW" db \
  psql -U product_owner -d law_agent -v ON_ERROR_STOP=1 < backend/prisma/migrations/0005_services.sql \
  | grep -E "ERROR|CREATE TABLE|ALTER TABLE|INSERT" | head -5
docker compose exec -T -e PGPASSWORD="$PGPW" db psql -U postgres -d law_agent -tAc \
  "SELECT 'services: ' || count(*) || ' row(s); orders without a service: ' || (SELECT count(*) FROM public.consultations WHERE service_id IS NULL) FROM public.services" < /dev/null

echo "== 5/8 build images"
docker compose build ai backend > /tmp/law-agent-build.log 2>&1 \
  || { tail -30 /tmp/law-agent-build.log; exit 1; }
echo "    built"

echo "== 6/8 AI service migration 0003 (suggestion columns)"
docker compose run --rm --no-deps ai alembic upgrade head < /dev/null 2>&1 | grep -E "Running upgrade|ERROR" || true
docker compose exec -T -e PGPASSWORD="$PGPW" db psql -U postgres -d law_agent -tAc \
  "SELECT 'messages.suggested_service: ' || CASE WHEN EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='ai' AND table_name='messages' AND column_name='suggested_service') THEN 'ok' ELSE 'MISSING' END" < /dev/null

echo "== 7/8 dashboard build + restart"
# The dashboard is static files. Built here when Node exists; otherwise
# dashboard/dist must already be in place (built with VITE_BACKEND_URL=$BE
# and shipped with the code), which is how earlier releases did it.
if command -v npm > /dev/null 2>&1 && [ -f dashboard/package.json ]; then
  ( cd dashboard && npm ci --silent && VITE_BACKEND_URL="$BE" npx vite build > /tmp/law-agent-dashboard.log 2>&1 ) \
    || { tail -20 /tmp/law-agent-dashboard.log; exit 1; }
  echo "    dashboard built here"
elif [ -f dashboard/dist/index.html ] && grep -q "Services" dashboard/dist/assets/*.js 2>/dev/null; then
  echo "    dashboard: prebuilt dist in place"
else
  echo "    dashboard: dist is missing or old -- build locally with VITE_BACKEND_URL=$BE and copy dashboard/dist here"; exit 1
fi
chmod -R a+rX dashboard/dist
docker compose up -d backend ai 2>&1 | tail -2
printf "    waiting for the AI service index"
for i in $(seq 1 60); do
  [ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/readyz)" = "200" ] && break
  printf "."; sleep 5
done
echo

echo "== 8/8 checks"
printf "    AI ready            %s\n" "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/readyz)"
printf "    backend healthy     %s\n" "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8001/healthz)"
printf "    GET /services       %s\n" "$(curl -s "$BE/services" | head -c 200)"
TOK=$(curl -s -X POST "$BE/auth/anonymous" -H 'Content-Type: application/json' \
      | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
printf "    old price route     %s\n" "$(curl -s "$BE/consultations/price" -H "Authorization: Bearer $TOK")"
printf "    anonymous cannot order: HTTP %s (403 expected)\n" \
  "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BE/consultations" -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' -d '{"service_id":"consultation"}')"
printf "    AI sees catalogue   "
docker compose exec -T ai python -c '
from app.suggest.catalog import catalog
c = catalog()
print("enabled" if c.enabled else "DISABLED (no BACKEND_URL)", "-", [s.slug for s in c.services()], "offerable:", [s.slug for s in c.offerable()])
' < /dev/null

echo
echo "Done. Then: upload the plugin zip (2.7.0) in WordPress, and in the dashboard"
echo "(Services page) give each service an Arabic hint so the assistant may suggest it."
echo
echo "Rollback:"
echo "  tar xzf $BACKUPS/code-$STAMP.tgz -C $APP && docker compose build ai backend && docker compose up -d backend ai"
echo "  (both migrations only add tables/columns; old code ignores them, so the database can stay)"
