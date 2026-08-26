-- WordPress as the credential authority.
--
--   psql -U product_owner -d law_agent -f prisma/migrations/0004_wordpress_identity.sql
--
-- WordPress owns passwords, password reset and email verification — none of
-- which this service has. It does NOT own tokens: the backend still holds the
-- only private key and still signs every access token, so the AI service is
-- untouched by this change and keeps verifying the same JWKS. A new identity
-- source is one endpoint, not a re-architecture.

BEGIN;

-- The link. Keyed on the WordPress user id rather than the email, because an
-- email is something a person changes and the identity must survive that.
ALTER TABLE public.users
    ADD COLUMN IF NOT EXISTS wp_user_id INTEGER,
    ADD COLUMN IF NOT EXISTS wp_role    VARCHAR(32);

-- One Law Agent user per WordPress user. Partial, so the many rows with no
-- WordPress link (anonymous visitors) do not collide on NULL.
CREATE UNIQUE INDEX IF NOT EXISTS uq_users_wp_user_id
    ON public.users (wp_user_id)
    WHERE wp_user_id IS NOT NULL AND deleted_at IS NULL;

-- A registered user must still be identifiable, but the credential can now
-- live elsewhere.
--
-- ck_users_anonymous_shape required a password_hash on every non-anonymous
-- row, which was exactly right while this service owned passwords. A
-- WordPress-backed user has an email and no password_hash — WordPress holds
-- it — so the original constraint rejects them outright.
--
-- The invariant that actually matters is unchanged: an anonymous row carries
-- no identity, and a registered row carries an email plus *some* credential.
-- What counts as a credential is now local password or WordPress link.
ALTER TABLE public.users
    DROP CONSTRAINT IF EXISTS ck_users_anonymous_shape;

ALTER TABLE public.users
    ADD CONSTRAINT ck_users_anonymous_shape CHECK (
        (is_anonymous AND email IS NULL AND password_hash IS NULL)
        OR
        (NOT is_anonymous AND email IS NOT NULL
             AND (password_hash IS NOT NULL OR wp_user_id IS NOT NULL))
    );

-- Replay protection for the login assertion.
--
-- The assertion is a bearer credential for sixty seconds. Anything that can
-- read one — a proxy log, a browser extension, a shared screen — could
-- otherwise present it again and be issued a fresh token pair. A unique
-- constraint is the check, rather than a lookup-then-insert, because two
-- concurrent presentations would both pass a lookup.
CREATE TABLE IF NOT EXISTS public.wp_assertions (
    jti        VARCHAR(64)  PRIMARY KEY,
    wp_user_id INTEGER      NOT NULL,
    used_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ  NOT NULL
);

-- Rows are only interesting until the assertion they record could no longer
-- be replayed. Swept on use; the index keeps that sweep cheap.
CREATE INDEX IF NOT EXISTS ix_wp_assertions_expiry
    ON public.wp_assertions (expires_at);

-- Same ownership rule as everything else in this schema: the application role
-- owns nothing and gets DML only.
ALTER TABLE public.wp_assertions OWNER TO product_owner;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.wp_assertions TO product_service;
REVOKE ALL ON public.wp_assertions FROM ai_service;

COMMIT;
