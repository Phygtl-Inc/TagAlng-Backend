-- 20270204120000_learned_interests · checks (local validation container only)

begin;

insert into public.blocks (id) values ('blk-learn-a'), ('blk-learn-b') on conflict do nothing;

insert into auth.users (id, email, email_confirmed_at, is_anonymous) values
  ('00000000-0000-0000-0000-0000000001a1', 'asker@example.com',   now(), false),
  ('00000000-0000-0000-0000-0000000001a2', 'tommaso@example.com', now(), false),
  ('00000000-0000-0000-0000-0000000001a3', 'once@example.com',    now(), false),
  ('00000000-0000-0000-0000-0000000001a4', 'removed@example.com', now(), false),
  ('00000000-0000-0000-0000-0000000001a5', 'faraway@example.com', now(), false),
  ('00000000-0000-0000-0000-0000000001a6', 'trails@example.com',  now(), false)
on conflict (id) do nothing;

update public.users set home_block_id = 'blk-learn-a', nickname = split_part(a.email, '@', 1)
from auth.users a where a.id = public.users.id and a.id in (
  '00000000-0000-0000-0000-0000000001a1', '00000000-0000-0000-0000-0000000001a2',
  '00000000-0000-0000-0000-0000000001a3', '00000000-0000-0000-0000-0000000001a4',
  '00000000-0000-0000-0000-0000000001a6');
update public.users set home_block_id = 'blk-learn-b', nickname = 'faraway'
where id = '00000000-0000-0000-0000-0000000001a5';

do $$
declare
  r record;
  t constant uuid := '00000000-0000-0000-0000-0000000001a2';
begin
  -- Same day: 1st and 2nd search do not learn, the 3rd does (3 in all).
  select * into r from public.record_learned_interest(t, '  AI ', 'event_search');
  assert not r.learned and not r.newly_learned and r.topic = 'ai' and r.label = 'AI', 'first';
  select * into r from public.record_learned_interest(t, 'ai', 'people_search');
  assert not r.learned, 'second, same day';
  select * into r from public.record_learned_interest(t, 'AI', 'event_search');
  assert r.learned and r.newly_learned, 'third learns';
  select * into r from public.record_learned_interest(t, 'AI', 'event_search');
  assert r.learned and not r.newly_learned, 'already learned is not new';

  -- Two different days learn on the second search.
  perform public.record_learned_interest('00000000-0000-0000-0000-0000000001a3', 'chess', 'event_search');
  update public.learned_interests set last_search_day = current_date - 1
  where user_id = '00000000-0000-0000-0000-0000000001a3';
  select * into r from public.record_learned_interest('00000000-0000-0000-0000-0000000001a3', 'Chess', 'event_search');
  assert r.newly_learned, 'second day learns';

  -- A one-off search never learns.
  perform public.record_learned_interest('00000000-0000-0000-0000-0000000001a6', 'ai', 'event_search');

  -- Removed stays removed: no counting, never re-learned.
  perform public.record_learned_interest('00000000-0000-0000-0000-0000000001a4', 'ai', 'event_search');
  update public.learned_interests set removed_at = now()
  where user_id = '00000000-0000-0000-0000-0000000001a4';
  for i in 1..5 loop
    select * into r from public.record_learned_interest('00000000-0000-0000-0000-0000000001a4', 'ai', 'event_search');
    assert not r.learned, 'removed never re-learns';
  end loop;
  assert (select event_searches from public.learned_interests
          where user_id = '00000000-0000-0000-0000-0000000001a4') = 1, 'removed not counted';

  -- Learned elsewhere: other block.
  for i in 1..3 loop
    perform public.record_learned_interest('00000000-0000-0000-0000-0000000001a5', 'ai', 'event_search');
  end loop;

  -- Junk is refused.
  select * into r from public.record_learned_interest(t, 'x', 'event_search');
  assert r is null or r.topic is null, 'too short';
  select * into r from public.record_learned_interest(t, 'ai', 'gossip');
  assert r is null or r.topic is null, 'unknown kind';

  -- One passing mention a week.
  assert public.claim_learned_mention(t, 'ai'), 'first mention';
  perform public.record_learned_interest(t, 'pizza', 'event_search');
  perform public.record_learned_interest(t, 'pizza', 'event_search');
  perform public.record_learned_interest(t, 'pizza', 'event_search');
  assert not public.claim_learned_mention(t, 'pizza'), 'second mention within a week';
  assert not public.claim_learned_mention('00000000-0000-0000-0000-0000000001a6', 'ai'), 'not learned';
  raise notice 'learned_interests: record/mention checks passed';
end;
$$;

-- People search as the asker.
select set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-0000000001a1', true);
select set_config('request.jwt.claims', '{"sub":"00000000-0000-0000-0000-0000000001a1","role":"authenticated"}', true);

do $$
declare
  got text[];
begin
  select array_agg(nickname order by nickname) into got
  from public.find_peers_by_learned_interest(array['ai']);
  -- tommaso only: trails searched once, removed was removed, faraway is another block.
  assert got = array['tommaso'], 'ai: ' || coalesce(got::text, 'none');

  select array_agg(learned_kind) into got from public.find_peers_by_learned_interest(array['AI']);
  assert got = array['events'], 'kind: ' || coalesce(got::text, 'none');

  -- Whole word: "a" / "i" are too short, "trail" never hits "ai".
  select array_agg(nickname) into got from public.find_peers_by_learned_interest(array['trai']);
  assert got is null, 'no substring hits';

  -- Faded after 90 days untouched.
  update public.learned_interests set last_seen_at = now() - interval '91 days'
  where user_id = '00000000-0000-0000-0000-0000000001a2' and topic = 'ai';
  select array_agg(nickname) into got from public.find_peers_by_learned_interest(array['ai']);
  assert got is null, 'faded: ' || coalesce(got::text, 'none');
  update public.learned_interests set last_seen_at = now()
  where user_id = '00000000-0000-0000-0000-0000000001a2' and topic = 'ai';

  raise notice 'learned_interests: people search checks passed';
end;
$$;

-- Clients read only their own rows and cannot write.
set local role authenticated;
do $$
begin
  assert (select count(*) from public.learned_interests) = 0, 'asker has none and sees none';
  begin
    insert into public.learned_interests (user_id, topic, label)
    values ('00000000-0000-0000-0000-0000000001a1', 'hacked', 'hacked');
    assert false, 'client insert must fail';
  exception when insufficient_privilege then null;
  end;
  begin
    perform public.record_learned_interest('00000000-0000-0000-0000-0000000001a1', 'ai', 'event_search');
    assert false, 'client record must fail';
  exception when insufficient_privilege then null;
  end;
  raise notice 'learned_interests: client permission checks passed';
end;
$$;
reset role;

rollback;
