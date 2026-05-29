
-- ── event_interest_leads: INSERT-only for anon ──────────────────────────────
CREATE POLICY "anon_insert_event_interest_leads"
  ON public.event_interest_leads
  FOR INSERT
  TO anon
  WITH CHECK (true);

-- ── event_interest_leads: INSERT-only for authenticated ─────────────────────
CREATE POLICY "authenticated_insert_event_interest_leads"
  ON public.event_interest_leads
  FOR INSERT
  TO authenticated
  WITH CHECK (true);

-- ── day_zero_access_leads: INSERT-only for anon ─────────────────────────────
CREATE POLICY "anon_insert_day_zero_access_leads"
  ON public.day_zero_access_leads
  FOR INSERT
  TO anon
  WITH CHECK (true);

-- ── day_zero_access_leads: INSERT-only for authenticated ────────────────────
CREATE POLICY "authenticated_insert_day_zero_access_leads"
  ON public.day_zero_access_leads
  FOR INSERT
  TO authenticated
  WITH CHECK (true);
;
