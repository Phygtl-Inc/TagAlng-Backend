-- TagAlng: mark transient (temporary, non-durable) identity claims and keep them
-- off the identity wall. A sprained ankle / upcoming trip / passing mood is worth
-- knowing for recommendations, but it is not "who you are".

alter table public.user_identity_claims
  add column if not exists transient boolean not null default false;

comment on column public.user_identity_claims.transient is
  'True for temporary states (injury, illness, upcoming trip, passing mood). Persisted '
  'for context but excluded from the identity wall via get_my_identity_claims().';

-- Recreate the profile-wall reader so transient claims never surface as identity
-- threads. RETURNS TABLE shape is unchanged; drop-then-create mirrors the prior migration.
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
    and coalesce(c.transient, false) = false
  order by c.confidence desc, c.created_at desc;
$$;
grant execute on function public.get_my_identity_claims() to authenticated;
