-- Cluster/database bootstrap. Run ONCE, as a superuser, before the first
-- migration. Roles and grants live here rather than in Alembic because they are
-- not schema objects and must exist before any table does.
--
--   psql -v ON_ERROR_STOP=1 -f sql/bootstrap.sql -d law_agent
--
-- Set real passwords before running outside a laptop.

-- ── roles ────────────────────────────────────────────────────────────────
-- Two roles, and the split is the whole point:
--
--   law_agent_owner  owns the tables, runs migrations. Never used by the app.
--   ai_service       what the FastAPI process connects as. Owns nothing, so
--                    row-level security actually applies to it — a table owner
--                    bypasses RLS unless FORCE ROW LEVEL SECURITY is also set,
--                    and relying on that alone is one setting away from a leak.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'law_agent_owner') THEN
        CREATE ROLE law_agent_owner LOGIN PASSWORD 'change-me-owner';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ai_service') THEN
        CREATE ROLE ai_service LOGIN PASSWORD 'change-me-service';
    END IF;
END
$$;

-- ── schema ───────────────────────────────────────────────────────────────
CREATE SCHEMA IF NOT EXISTS ai AUTHORIZATION law_agent_owner;

-- ── the boundary ─────────────────────────────────────────────────────────
-- The AI service must not be able to read the product domain. A naming
-- convention is not a boundary; a revoked grant is. If the product backend
-- later shares this database, its tables land in `public` and remain
-- unreadable from this role.
REVOKE ALL ON SCHEMA public FROM ai_service;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM ai_service;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM ai_service;

GRANT USAGE ON SCHEMA ai TO ai_service;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA ai TO ai_service;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA ai TO ai_service;

-- Tables created by later migrations get the same grants automatically.
ALTER DEFAULT PRIVILEGES FOR ROLE law_agent_owner IN SCHEMA ai
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO ai_service;
ALTER DEFAULT PRIVILEGES FOR ROLE law_agent_owner IN SCHEMA ai
    GRANT USAGE, SELECT ON SEQUENCES TO ai_service;

-- Neither role should be creating tables outside `ai`.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- ── search_path ──────────────────────────────────────────────────────────
-- The app never writes a schema-qualified name by hand.
ALTER ROLE ai_service       IN DATABASE law_agent SET search_path = ai;
ALTER ROLE law_agent_owner  IN DATABASE law_agent SET search_path = ai, public;
