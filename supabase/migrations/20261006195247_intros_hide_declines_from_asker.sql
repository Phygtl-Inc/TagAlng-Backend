-- Applied directly to PROD on 2026-10-06 19:52 UTC and committed here afterwards, verbatim
-- from supabase_migrations.schema_migrations, so the repo's history matches prod's and
-- `supabase db push` stops refusing. Prod will not re-run it.

-- The product promise is "two yeses, or nothing happens". get_my_intros honoured it
-- (status='proposed' only); RLS did not — intros_select_parties let the initiator read
-- the row directly and see status='declined', i.e. exactly who turned them down.
--
-- A decline must now be indistinguishable from an intro that quietly expired.
-- The candidate keeps full visibility: they made the decision, it is theirs to see.

drop policy if exists intros_select_parties on public.intros;

create policy intros_select_parties on public.intros
for select using (
  candidate_id = auth.uid()
  or (initiator_id = auth.uid() and status <> 'declined')
);

comment on table public.intros is
  'Two-sided consent. RLS hides a declined intro from the initiator so a refusal is '
  'indistinguishable from an expiry — the asker never learns who said no. '
  'intros_no_client_write blocks all client writes; accept/decline go through the RPCs.';
