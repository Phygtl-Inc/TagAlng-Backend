-- TagAlng Phase 1: Row Level Security

alter table public.blocks enable row level security;
alter table public.cohorts enable row level security;
alter table public.waitlist_signups enable row level security;
alter table public.block_waitlist_counts enable row level security;
alter table public.audit_log enable row level security;
alter table public.analytics_events enable row level security;

-- Public read: blocks (atlas map), cohorts (picker), waitlist counts (ticker)
create policy "blocks_select_public"
  on public.blocks for select
  to anon, authenticated
  using (true);

create policy "cohorts_select_public"
  on public.cohorts for select
  to anon, authenticated
  using (true);

create policy "block_counts_select_public"
  on public.block_waitlist_counts for select
  to anon, authenticated
  using (true);

-- Waitlist: anyone can insert (reCAPTCHA verified server-side before insert in Phase 1 API)
-- Phone rows not readable by anon
create policy "waitlist_insert_anon"
  on public.waitlist_signups for insert
  to anon, authenticated
  with check (true);

create policy "waitlist_no_public_select"
  on public.waitlist_signups for select
  to anon
  using (false);

-- Analytics: insert-only from clients (refine properties — no raw PII in props)
create policy "analytics_insert"
  on public.analytics_events for insert
  to anon, authenticated
  with check (true);

create policy "analytics_no_public_select"
  on public.analytics_events for select
  to anon
  using (false);

-- Audit: no public access (service role / admin Phase 3)
create policy "audit_no_public"
  on public.audit_log for all
  to anon, authenticated
  using (false)
  with check (false);

-- Admin policies (service_role bypasses RLS; Phase 3: authenticated admin role)
