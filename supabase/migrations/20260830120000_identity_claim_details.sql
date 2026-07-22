-- TagAlng: identity claims absorb enrichment instead of freezing at first mention.
-- "I swim" → "I'm a weekend swimmer" → "state-level" must land on ONE thread:
-- label upgrades, sub-facts accumulate in details, synonyms union, confidence rises.

alter table public.user_identity_claims
  add column if not exists details text[] not null default '{}';

comment on column public.user_identity_claims.details is
  'Short user-visible sub-facts accumulated across turns for the same thread '
  '(e.g. {"Swims every weekend","Competes at state level"}). Capped ~5 by the worker; '
  'folded into the claim embedding so enrichment sharpens semantic matching.';

-- Profile-wall reader: same filters as 20260805 (active + non-transient), now with details.
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
  details text[],
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
    coalesce(c.details, '{}') as details,
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

-- Dashboard RPC: claims objects now carry details (jsonb shape is additive, callers
-- that ignore the key are unaffected). Body otherwise identical to 20260610.
create or replace function public.get_my_profile_dashboard()
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
stable
as $$
declare
  v_caller uuid := auth.uid();
  v_profile jsonb;
  v_summary text;
  v_spans jsonb;
  v_claims jsonb;
  v_stats jsonb;
  v_events_hosted int;
  v_events_attended int;
  v_check_ins int;
  v_peers_met int;
begin
  if v_caller is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  select public.get_my_profile() into v_profile;

  select
    s.context->>'mapped_summary',
    coalesce(s.context->'spans', '[]'::jsonb)
  into v_summary, v_spans
  from public.lana_sessions s
  where s.user_id = v_caller
    and s.purpose = 'profile_intake'
    and s.status = 'completed'
  order by s.completed_at desc nulls last, s.updated_at desc
  limit 1;

  select coalesce(jsonb_agg(
    jsonb_build_object(
      'id', c.id,
      'concept', c.concept,
      'label', c.label,
      'tone', c.tone,
      'confidence', c.confidence,
      'disclosure', c.disclosure,
      'synonyms', c.synonyms,
      'details', coalesce(c.details, '{}'),
      'source_quote', c.source_quote,
      'bucket', c.bucket,
      'created_at', c.created_at
    ) order by c.confidence desc, c.created_at desc
  ), '[]'::jsonb)
  into v_claims
  from public.user_identity_claims c
  where c.user_id = v_caller
    and c.dismissed_at is null;

  select count(*)::int
  into v_events_hosted
  from public.events e
  where e.host_id = v_caller
    and e.status in ('open', 'completed');

  select count(*)::int
  into v_events_attended
  from public.event_requests er
  where er.requester_id = v_caller
    and er.status in ('approved', 'attended');

  select count(*)::int
  into v_check_ins
  from public.thread_events te
  where te.actor_id = v_caller
    and te.event_type = 'check_in';

  select count(distinct er2.requester_id)::int
  into v_peers_met
  from public.event_requests er1
  join public.event_requests er2
    on er2.event_id = er1.event_id
   and er2.requester_id <> v_caller
  where er1.requester_id = v_caller
    and er1.status in ('approved', 'attended')
    and er2.status in ('approved', 'attended');

  v_stats := jsonb_build_object(
    'events_hosted', coalesce(v_events_hosted, 0),
    'events_attended', coalesce(v_events_attended, 0),
    'check_ins', coalesce(v_check_ins, 0),
    'peers_met', coalesce(v_peers_met, 0)
  );

  return jsonb_build_object(
    'profile', coalesce(v_profile, '{}'::jsonb),
    'mapped_summary', v_summary,
    'spans', coalesce(v_spans, '[]'::jsonb),
    'claims', coalesce(v_claims, '[]'::jsonb),
    'stats', v_stats
  );
end;
$$;
