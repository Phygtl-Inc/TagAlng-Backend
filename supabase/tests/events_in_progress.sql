-- 20270122120000_restore_prod_oct6_changes · checks (local validation container only)
-- An event already under way is "happening now"; a finished one is not; private stays hidden.

begin;

insert into auth.users (id, email, email_confirmed_at, is_anonymous)
values ('00000000-0000-0000-0000-0000000000c1', 'host@example.com', now(), false)
on conflict (id) do nothing;

create temp table pt as select 37.3352::float8 as lat, -121.8811::float8 as lng;

insert into public.events (id, host_id, title, starts_at, ends_at, has_time, status, location, is_private)
select x.id::uuid, '00000000-0000-0000-0000-0000000000c1', x.title, x.s, x.e, x.ht, 'open',
       extensions.st_setsrid(extensions.st_makepoint(pt.lng, pt.lat), 4326)::extensions.geography,
       x.priv
from pt, (values
  ('00000000-0000-0000-0000-00000000e001', 'Job fair (running now)', now() - interval '1 hour', now() + interval '2 hours', true,  false),
  ('00000000-0000-0000-0000-00000000e002', 'Already over',           now() - interval '3 hours', now() - interval '1 hour', true,  false),
  ('00000000-0000-0000-0000-00000000e003', 'Tomorrow',               now() + interval '1 day',   null,                      true,  false),
  ('00000000-0000-0000-0000-00000000e004', 'Date-only today',        date_trunc('day', now()),   null,                      false, false),
  ('00000000-0000-0000-0000-00000000e005', 'Private, running now',   now() - interval '1 hour', now() + interval '2 hours', true,  true),
  ('00000000-0000-0000-0000-00000000e006', 'Started 1h ago, no end', now() - interval '1 hour', null,                      true,  false)
) as x(id, title, s, e, ht, priv);

select set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-0000000000c1', true);
select set_config('request.jwt.claims', '{"sub":"00000000-0000-0000-0000-0000000000c1","role":"authenticated"}', true);

do $$
declare
  want text[] := array['Date-only today', 'Job fair (running now)', 'Started 1h ago, no end', 'Tomorrow'];
  got  text[];
  q    extensions.vector(768) := ('[' || array_to_string(array_fill(0.01::real, array[768]), ',') || ']')::extensions.vector;
begin
  select array_agg(title order by title) into got
    from pt, public.get_activities_near_point(pt.lat, pt.lng);
  assert got = want, 'get_activities_near_point: ' || coalesce(got::text, 'none');

  select array_agg(title order by title) into got
    from pt, public.get_nearby_activities(pt.lat, pt.lng);
  assert got = want, 'get_nearby_activities: ' || coalesce(got::text, 'none');

  select array_agg(title order by title) into got
    from pt, public.get_nearby_activities_authed(pt.lat, pt.lng);
  assert got = want, 'get_nearby_activities_authed: ' || coalesce(got::text, 'none');

  select array_agg(title order by title) into got
    from pt, public.search_events_semantic(q, pt.lat, pt.lng);
  assert got = want, 'search_events_semantic: ' || coalesce(got::text, 'none');

  raise notice 'events_in_progress: all checks passed';
end;
$$;

rollback;
