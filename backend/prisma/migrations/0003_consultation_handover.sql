-- Handover context on a consultation, and the currency default.
--
--   psql -h 127.0.0.1 -U product_owner -d law_agent \
--        -f prisma/migrations/0003_consultation_handover.sql
--
-- WHY THIS EXISTS
--
-- `schema.prisma` declared `chat_language` and `escalation_summary` on
-- consultations, and billing.service.ts writes both when it escalates a paid
-- session. 0002_billing.sql never created them. Prisma introspects rather than
-- authors this schema, so nothing reconciled the two, and the parity suite
-- covers identity rather than billing — so the gap survived until the first
-- real `POST /consultations`, which failed with:
--
--   P2022  column consultations.chat_language does not exist
--
-- That is the worst place to find it: the row is created before the gateway is
-- contacted, so the buyer got an error instead of a checkout page.

BEGIN;

-- What the AI service reports back when a session is handed over: the language
-- the conversation was held in, and a short summary for the lawyer picking it
-- up. Both nullable — a consultation can be bought with no conversation
-- attached, and escalation can fail while the payment stands.
ALTER TABLE public.consultations
    ADD COLUMN IF NOT EXISTS chat_language      VARCHAR(16),
    ADD COLUMN IF NOT EXISTS escalation_summary TEXT;

-- The gateway is ksa.paymob.com and the service now prices every consultation
-- from CONSULTATION_CURRENCY, which defaults to SAR. The column default was
-- left at EGP from an earlier region assumption; it is only a fallback, since
-- the application always passes a currency explicitly, but a default that
-- contradicts the configured region is a trap for the next person reading it.
--
-- Existing rows are untouched: a default applies to future inserts only.
ALTER TABLE public.consultations
    ALTER COLUMN currency SET DEFAULT 'SAR';

-- ── ownership repair ─────────────────────────────────────────────────────
--
-- 0002_billing.sql must be run as product_owner: the grant to product_service
-- comes from ALTER DEFAULT PRIVILEGES FOR ROLE product_owner, which only
-- applies to tables that role creates. Run it as a superuser instead — easy to
-- do, because superuser auth is the path of least resistance during a deploy —
-- and the tables end up owned by postgres, the default privileges never fire,
-- and the application role gets `42501 permission denied` on its first write.
--
-- Idempotent and safe on a correctly-built database, where it changes nothing.
DO $$
DECLARE t text;
BEGIN
  FOR t IN
    SELECT tablename FROM pg_tables
    WHERE schemaname = 'public' AND tableowner <> 'product_owner'
  LOOP
    EXECUTE format('ALTER TABLE public.%I OWNER TO product_owner', t);
  END LOOP;
END $$;

-- DML only. The application role owns nothing, so it cannot alter its own
-- tables and row-level security genuinely applies to it — a table owner
-- bypasses RLS unless FORCE is also set, and relying on that alone is one
-- setting away from a leak.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO product_service;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO product_service;

ALTER DEFAULT PRIVILEGES FOR ROLE product_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO product_service;
ALTER DEFAULT PRIVILEGES FOR ROLE product_owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO product_service;

-- Re-asserted last, because the blanket grants above touch every table in the
-- schema: the AI service must never read the product domain.
REVOKE ALL ON SCHEMA public FROM ai_service;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM ai_service;

COMMIT;
