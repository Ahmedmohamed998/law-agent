-- Services: the catalogue a client buys from, and what an order is for.
--
--   psql -h 127.0.0.1 -U product_owner -d law_agent \
--        -f prisma/migrations/0005_services.sql
--
-- As product_owner, like 0003 explains: the application role's grants come
-- from that role's default privileges and only cover tables it creates.
--
-- WHAT CHANGES
--
-- A consultation stops being the only thing for sale. `services` is the
-- catalogue — name, description, price — edited in the dashboard and read
-- everywhere else. A row in `consultations` (kept under that name; it is the
-- orders table now) points at the service it bought and carries a snapshot
-- of the name and price at the time, so editing a service later never
-- rewrites what a client actually paid for.
--
-- The price lives here and nowhere else. CONSULTATION_PRICE_CENTS, which used
-- to be the single source, becomes the seed for the one service that already
-- existed; after this migration the environment variable is not consulted.

BEGIN;

CREATE TABLE IF NOT EXISTS public.services (
    id                 VARCHAR(32)  PRIMARY KEY,
    -- Stable, human, unique: what URLs and the AI's suggestions refer to.
    slug               VARCHAR(64)  NOT NULL,
    name_ar            VARCHAR(160) NOT NULL,
    description_ar     TEXT         NOT NULL DEFAULT '',
    price_cents        INTEGER      NOT NULL,
    currency           VARCHAR(3)   NOT NULL DEFAULT 'SAR',
    -- Inactive services are not offered but still referenced by old orders.
    -- There is deliberately no delete.
    active             BOOLEAN      NOT NULL DEFAULT TRUE,
    sort_order         INTEGER      NOT NULL DEFAULT 100,
    -- The lawyer needs the conversation (a consultation); the order is then
    -- handed over with the chat summary, as consultations have always been.
    needs_conversation BOOLEAN      NOT NULL DEFAULT FALSE,
    -- The client must describe the case in writing before paying.
    needs_notes        BOOLEAN      NOT NULL DEFAULT FALSE,
    -- When the assistant may propose this service, in Arabic, for the
    -- classifier: "عندما يريد العميل فهم أو تعديل عقد قبل توقيعه".
    ai_hint            TEXT         NOT NULL DEFAULT '',
    -- Sold on the site, but never pushed by the assistant.
    suggestable        BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT uq_services_slug     UNIQUE (slug),
    CONSTRAINT ck_services_price    CHECK (price_cents > 0),
    CONSTRAINT ck_services_slug     CHECK (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$')
);

CREATE INDEX IF NOT EXISTS ix_services_active ON public.services (active, sort_order);

-- The one service that has always existed. 50000 SAR-halalas is what the
-- live configuration charges today; edit it in the dashboard afterwards.
INSERT INTO public.services
    (id, slug, name_ar, description_ar, price_cents, currency, sort_order,
     needs_conversation, needs_notes, ai_hint, suggestable)
VALUES
    ('01K5CONSULTATION0000000000', 'consultation',
     'استشارة قانونية',
     'جلسة مع محامٍ مختص في نظام العمل لمراجعة حالتك والإجابة عن أسئلتك.',
     50000, 'SAR', 10,
     TRUE, FALSE,
     'عندما تحتاج حالة العميل إلى رأي محامٍ مباشر أو متابعة شخصية لا يكفي فيها الجواب العام.',
     TRUE)
ON CONFLICT (slug) DO NOTHING;

-- ── orders ───────────────────────────────────────────────────────────────

ALTER TABLE public.consultations
    ADD COLUMN IF NOT EXISTS service_id   VARCHAR(32) REFERENCES public.services (id),
    ADD COLUMN IF NOT EXISTS service_name VARCHAR(160),
    ADD COLUMN IF NOT EXISTS client_notes TEXT;

-- Every existing order was a consultation.
UPDATE public.consultations
   SET service_id   = (SELECT id FROM public.services WHERE slug = 'consultation'),
       service_name = 'استشارة قانونية'
 WHERE service_id IS NULL;

ALTER TABLE public.consultations
    ALTER COLUMN service_id   SET NOT NULL,
    ALTER COLUMN service_name SET NOT NULL;

CREATE INDEX IF NOT EXISTS ix_consultations_service ON public.consultations (service_id, created_at DESC);

-- Two more states, both set by a lawyer in the dashboard once the money is
-- in: work started, work delivered. Paid stays the state a webhook produces.
ALTER TABLE public.consultations DROP CONSTRAINT IF EXISTS ck_consultations_status;
ALTER TABLE public.consultations
    ADD CONSTRAINT ck_consultations_status
    CHECK (status IN ('pending', 'paid', 'in_progress', 'completed', 'cancelled', 'refunded'));

-- ── grants, same shape as 0003 ───────────────────────────────────────────

GRANT SELECT, INSERT, UPDATE, DELETE ON public.services TO product_service;

-- Re-asserted: the AI service reads the catalogue over HTTP, never from here.
REVOKE ALL ON public.services FROM ai_service;

COMMIT;
