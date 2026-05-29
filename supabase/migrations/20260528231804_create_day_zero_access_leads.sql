
-- 1. Create table
CREATE TABLE IF NOT EXISTS public.day_zero_access_leads (
  id                 uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  contact_type       text        NOT NULL CHECK (contact_type IN ('email', 'phone')),
  contact_value      text        NOT NULL,
  contact_normalized text        NOT NULL,
  source             text        NOT NULL DEFAULT 'launch_site',
  campaign           text                 DEFAULT 'day_zero_access',
  utm_source         text,
  utm_medium         text,
  utm_campaign       text,
  landing_page       text,
  referrer           text,
  user_agent         text,
  pwa_opened         boolean     NOT NULL DEFAULT true,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);

-- 2. Unique index on contact_normalized (deduplication key)
CREATE UNIQUE INDEX IF NOT EXISTS day_zero_access_leads_contact_normalized_uidx
  ON public.day_zero_access_leads (contact_normalized);

-- 3. Descending index on created_at (recency queries)
CREATE INDEX IF NOT EXISTS day_zero_access_leads_created_at_desc_idx
  ON public.day_zero_access_leads (created_at DESC);

-- 4. Enable RLS — no public policies; access is service-role only
ALTER TABLE public.day_zero_access_leads ENABLE ROW LEVEL SECURITY;
;
