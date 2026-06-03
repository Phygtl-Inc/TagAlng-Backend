-- TagAlng: Lana UI fields on identity claims (source quote + bucket for profile cards)

alter table public.user_identity_claims
  add column if not exists source_quote text,
  add column if not exists bucket text;
alter table public.user_identity_claims
  drop constraint if exists user_identity_claims_bucket_check;
alter table public.user_identity_claims
  add constraint user_identity_claims_bucket_check check (
    bucket is null
    or bucket in ('heritage', 'stage', 'vicinity', 'faith', 'activity', 'interest', 'general')
  );
comment on column public.user_identity_claims.source_quote is
  'Exact phrase from user story Lana mapped (e.g. Latino mom). UI only — not used for matching.';
comment on column public.user_identity_claims.bucket is
  'UI theme bucket for card color: heritage, stage, vicinity, faith, activity, interest, general.';
-- Postgres cannot change RETURNS TABLE shape via CREATE OR REPLACE (42P13).
drop function if exists public.get_my_identity_claims();
create function public.get_my_identity_claims()
returns table (
  id uuid,
  concept text,
  label text,
  tone text,
  confidence real,
  disclosure public.claim_disclosure,
  synonyms text[],
  source_quote text,
  bucket text,
  created_at timestamptz
)
language sql
stable
security definer
set search_path = public
as $$
  select
    c.id,
    c.concept,
    c.label,
    c.tone,
    c.confidence,
    c.disclosure,
    c.synonyms,
    c.source_quote,
    c.bucket,
    c.created_at
  from public.user_identity_claims c
  where c.user_id = auth.uid()
    and c.dismissed_at is null
  order by c.confidence desc, c.created_at desc;
$$;
grant execute on function public.get_my_identity_claims() to authenticated;
