
-- 1. Create event_interest_leads table
CREATE TABLE IF NOT EXISTS public.event_interest_leads (
  id                 uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  event_id           text        NOT NULL,
  event_title        text        NOT NULL,
  event_category     text,
  event_time         text,
  event_location     text,
  contact_type       text        NOT NULL CHECK (contact_type IN ('email', 'phone')),
  contact_value      text        NOT NULL,
  contact_normalized text        NOT NULL,
  source             text        NOT NULL DEFAULT 'launch_site',
  campaign           text        DEFAULT 'day_zero_event_interest',
  utm_source         text,
  utm_medium         text,
  utm_campaign       text,
  landing_page       text,
  referrer           text,
  user_agent         text,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);

-- 2. Unique index: one lead per (event, contact)
CREATE UNIQUE INDEX IF NOT EXISTS uq_event_interest_leads_event_contact
  ON public.event_interest_leads (event_id, contact_normalized);

-- 3. Descending index on created_at for recency queries
CREATE INDEX IF NOT EXISTS idx_event_interest_leads_created_at
  ON public.event_interest_leads (created_at DESC);

-- 4. Enable RLS
ALTER TABLE public.event_interest_leads ENABLE ROW LEVEL SECURITY;
;
