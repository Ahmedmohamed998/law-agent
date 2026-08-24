-- Product-domain roles and grants. Run ONCE, as a superuser, after
-- sql/bootstrap.sql and before the first product migration.
--
--   psql -v ON_ERROR_STOP=1 -f product/sql/bootstrap_product.sql -d law_agent
--
-- Set real passwords before running outside a laptop.
--
-- The AI service's bootstrap already revoked `ai_service` from `public`. This
-- file completes the other half of that boundary: it gives the product roles
-- ownership of `public` and denies them `ai` — so neither service can read the
-- other's tables even though both connect to one database. A naming convention
-- is not a boundary; a revoked grant is.

-- ── roles ────────────────────────────────────────────────────────────────
--   product_owner    owns public.*, runs product migrations. Never the app.
--   product_service  what the FastAPI process connects as. Owns nothing.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'product_owner') THEN
        CREATE ROLE product_owner LOGIN PASSWORD 'change-me-owner';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'product_service') THEN
        CREATE ROLE product_service LOGIN PASSWORD 'change-me-service';
    END IF;
END
$$;

-- ── ownership of public ──────────────────────────────────────────────────
ALTER SCHEMA public OWNER TO product_owner;
GRANT USAGE, CREATE ON SCHEMA public TO product_owner;
GRANT USAGE ON SCHEMA public TO product_service;

-- ── the boundary, in the other direction ─────────────────────────────────
-- The product backend must not be able to read conversations. Those rows hold
-- dismissals, unpaid-wage disputes and salary details, and the identity
-- service has no reason to see any of it.
REVOKE ALL ON SCHEMA ai FROM product_service;
REVOKE ALL ON SCHEMA ai FROM product_owner;
REVOKE ALL ON ALL TABLES IN SCHEMA ai FROM product_service;
REVOKE ALL ON ALL TABLES IN SCHEMA ai FROM product_owner;

-- ── application grants ───────────────────────────────────────────────────
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO product_service;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO product_service;

-- Tables created by later migrations get the same grants automatically.
ALTER DEFAULT PRIVILEGES FOR ROLE product_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO product_service;
ALTER DEFAULT PRIVILEGES FOR ROLE product_owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO product_service;

-- ── search_path ──────────────────────────────────────────────────────────
-- Neither product role should ever resolve a bare table name into `ai`.
ALTER ROLE product_service IN DATABASE law_agent SET search_path = public;
ALTER ROLE product_owner   IN DATABASE law_agent SET search_path = public;
