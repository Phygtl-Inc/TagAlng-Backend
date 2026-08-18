-- ---------------------------------------------------------------------------
-- Repair: 20261026 shipped get_my_profile_dashboard with three claim keys missing.
-- ---------------------------------------------------------------------------
-- That migration recreated the function by hand and silently dropped
-- `source_quote`, `subject_name` and `subject_age` from every projected claim. The
-- PWA parses the dashboard strictly (`source_quote` is nullABLE, not nullish — the
-- key has to be there), so the whole payload failed validation and the profile
-- drawer rendered "Couldn't load your profile" (2026-08-18).
--
-- 20261026 is already recorded as applied, so fixing that file repairs nothing on a
-- database that ran it: a migration is immutable once it lands, and the repair needs
-- its own version. Both files now carry the same correct body, so a fresh database
-- and a repaired one converge.
--
-- The body below was extracted from 20261023120000 and edited only to add the
-- `users.portrait` fallback — never retyped. Recreating a function by hand is what
-- caused this; a `security definer` function is the last place to do it.
--
-- ROLLBACK: re-run 20261023120000_owner_claims_subject.sql (drops the portrait
-- fallback with it).
-- ---------------------------------------------------------------------------

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
  v_portrait text;
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

  -- The intake line wins when there is one (it is what they were shown at signup);
  -- otherwise the stored portrait, which is how a chat-built profile gets prose and
  -- how the drawer stopped making a second round trip for it.
  select u.portrait into v_portrait from public.users u where u.id = v_caller;
  v_summary := coalesce(nullif(trim(v_summary), ''), nullif(trim(v_portrait), ''));

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
      'created_at', c.created_at,
      -- Who the thread is about. Owner's own wall, so the child's name and age
      -- ride along here — and only here.
      'subject_kind', c.subject_kind,
      'subject_name', c.subject_name,
      'subject_age', case
        when c.subject_birth_year is null then null
        else extract(year from now())::int - c.subject_birth_year
      end
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
