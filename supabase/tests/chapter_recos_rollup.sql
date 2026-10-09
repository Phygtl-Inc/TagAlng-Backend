-- 20270203120000_chapter_recos_rollup · behaviour checks
-- (local validation container only — never against dev or prod)
--   psql -h 127.0.0.1 -p 55432 -U supabase_admin -d postgres -v ON_ERROR_STOP=1 \
--        -f supabase/tests/chapter_recos_rollup.sql
-- Everything runs in one transaction and is rolled back. Any failed assert aborts it.
--
-- A community's tips roll up across its family (community_family): itself, its parent, and
-- the chapters the caller may see — all of them for a member of the parent, only her own
-- otherwise — never a sibling chapter, never an unrelated community. Rows from another
-- community of the family carry origin_place_id/name; the community's own rows do not.

begin;

insert into auth.users (id, email, email_confirmed_at, is_anonymous) values
  ('00000000-0000-0000-0000-0000000fa001', 'parent-member@example.com',  now(), false),
  ('00000000-0000-0000-0000-0000000fa002', 'chapter-member@example.com', now(), false),
  ('00000000-0000-0000-0000-0000000fa003', 'stranger@example.com',       now(), false),
  ('00000000-0000-0000-0000-0000000fa011', 'author-parent@example.com',  now(), false),
  ('00000000-0000-0000-0000-0000000fa012', 'author-rcc@example.com',     now(), false),
  ('00000000-0000-0000-0000-0000000fa013', 'author-chess@example.com',   now(), false),
  ('00000000-0000-0000-0000-0000000fa014', 'author-other@example.com',   now(), false)
on conflict (id) do nothing;

-- SJSU (parent) with two chapters, RCC and Chess Club, plus an unrelated community.
insert into public.places (id, google_place_id, name, lat, lng) values
  ('00000000-0000-0000-0000-0000000fb001', 'rr-sjsu',  'SJSU',              37.33, -121.88),
  ('00000000-0000-0000-0000-0000000fb004', 'rr-other', 'Moms Orlando',      28.54, -81.38);
insert into public.places (id, google_place_id, name, lat, lng, parent_place_ref) values
  ('00000000-0000-0000-0000-0000000fb002', 'rr-rcc',   'RCC',        37.33, -121.88, '00000000-0000-0000-0000-0000000fb001'),
  ('00000000-0000-0000-0000-0000000fb003', 'rr-chess', 'Chess Club', 37.33, -121.88, '00000000-0000-0000-0000-0000000fb001');

insert into public.circle_affiliations (user_id, circle_type, circle_key, place_ref, status, source) values
  ('00000000-0000-0000-0000-0000000fa001', 'school', 'sjsu', '00000000-0000-0000-0000-0000000fb001', 'confirmed', 'profile_add'),
  ('00000000-0000-0000-0000-0000000fa002', 'hobby',  'rcc',  '00000000-0000-0000-0000-0000000fb002', 'confirmed', 'profile_add');

-- One tip shared in each community.
insert into public.local_signals (id, user_id, intent, detail_text, reco_name, reco_type, circle_place_ref) values
  ('00000000-0000-0000-0000-0000000fc001', '00000000-0000-0000-0000-0000000fa011', 'tip_share', 'tip in SJSU',  'Library',   'location',   '00000000-0000-0000-0000-0000000fb001'),
  ('00000000-0000-0000-0000-0000000fc002', '00000000-0000-0000-0000-0000000fa012', 'tip_share', 'tip in RCC',   'AI reader', 'product',    '00000000-0000-0000-0000-0000000fb002'),
  ('00000000-0000-0000-0000-0000000fc003', '00000000-0000-0000-0000-0000000fa013', 'tip_share', 'tip in Chess', 'Chess app', 'product',    '00000000-0000-0000-0000-0000000fb003'),
  ('00000000-0000-0000-0000-0000000fc004', '00000000-0000-0000-0000-0000000fa014', 'tip_share', 'tip in Moms',  'Pediatric', 'professional', '00000000-0000-0000-0000-0000000fb004');

create temp table got (viewer text, place text, signal_id uuid, origin_place_id uuid, origin_place_name text);
grant all on got to authenticated;

-- Each viewer reads each community, as themselves (auth.uid() = request.jwt.claim.sub).
do $$
declare
  v record;
  c record;
begin
  for v in select * from (values
      ('parent_member',  '00000000-0000-0000-0000-0000000fa001'),
      ('chapter_member', '00000000-0000-0000-0000-0000000fa002'),
      ('stranger',       '00000000-0000-0000-0000-0000000fa003')) t(name, id)
  loop
    perform set_config('request.jwt.claim.sub', v.id, true);
    perform set_config('request.jwt.claims', json_build_object('sub', v.id, 'role', 'authenticated')::text, true);
    for c in select * from (values
        ('sjsu',  '00000000-0000-0000-0000-0000000fb001'::uuid),
        ('rcc',   '00000000-0000-0000-0000-0000000fb002'::uuid),
        ('moms',  '00000000-0000-0000-0000-0000000fb004'::uuid)) t(name, id)
    loop
      insert into got
      select v.name, c.name, r.signal_id, r.origin_place_id, r.origin_place_name
        from public.recent_neighbor_tips(p_circle_place_id => c.id, p_limit => 50) r;
    end loop;
  end loop;
end;
$$;

do $$
declare
  ids text;
begin
  -- A member of the parent, inside SJSU: SJSU's own tip + every chapter's, labelled.
  select string_agg(right(signal_id::text, 3), ',' order by signal_id) into ids
    from got where viewer = 'parent_member' and place = 'sjsu';
  assert ids = '001,002,003', format('parent member in SJSU sees all chapters, got %s', ids);
  assert (select origin_place_id from got where viewer = 'parent_member' and place = 'sjsu'
           and signal_id = '00000000-0000-0000-0000-0000000fc001') is null,
    'the community''s own row carries no origin';
  assert (select origin_place_name from got where viewer = 'parent_member' and place = 'sjsu'
           and signal_id = '00000000-0000-0000-0000-0000000fc002') = 'RCC',
    'a chapter row says where it was shared';

  -- A member of RCC only, inside SJSU: SJSU + her own chapter, never the sibling.
  select string_agg(right(signal_id::text, 3), ',' order by signal_id) into ids
    from got where viewer = 'chapter_member' and place = 'sjsu';
  assert ids = '001,002', format('chapter member in SJSU sees parent + own chapter, got %s', ids);

  -- Anyone inside RCC: RCC + its parent (labelled SJSU), never the sibling chapter.
  select string_agg(right(signal_id::text, 3), ',' order by signal_id) into ids
    from got where viewer = 'chapter_member' and place = 'rcc';
  assert ids = '001,002', format('inside RCC: own + parent, got %s', ids);
  assert (select origin_place_name from got where viewer = 'chapter_member' and place = 'rcc'
           and signal_id = '00000000-0000-0000-0000-0000000fc001') = 'SJSU',
    'the parent row inside a chapter says SJSU';
  select string_agg(right(signal_id::text, 3), ',' order by signal_id) into ids
    from got where viewer = 'stranger' and place = 'rcc';
  assert ids = '001,002', format('stranger inside RCC: own + parent, got %s', ids);

  -- A stranger looking at the parent gets the parent alone (community tips are public).
  select string_agg(right(signal_id::text, 3), ',' order by signal_id) into ids
    from got where viewer = 'stranger' and place = 'sjsu';
  assert ids = '001', format('stranger in SJSU sees the parent alone, got %s', ids);

  -- A community with no family reads exactly as before: its own tip, unlabelled.
  select string_agg(right(signal_id::text, 3), ',' order by signal_id) into ids
    from got where viewer = 'parent_member' and place = 'moms';
  assert ids = '004', format('unrelated community unchanged, got %s', ids);
  assert not exists (select 1 from got where place = 'moms' and origin_place_id is not null),
    'a family of one carries no origin';
end;
$$;

-- The reco-type chip counts follow the same family.
do $$
declare
  n_product int;
  n_location int;
begin
  perform set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-0000000fa002', true);
  perform set_config('request.jwt.claims', '{"sub":"00000000-0000-0000-0000-0000000fa002","role":"authenticated"}', true);
  select coalesce(sum(n) filter (where reco_type = 'product'), 0),
         coalesce(sum(n) filter (where reco_type = 'location'), 0)
    into n_product, n_location
    from public.neighbor_tip_type_counts(p_circle_place_id => '00000000-0000-0000-0000-0000000fb001');
  -- Inside SJSU, the RCC member counts SJSU (location) + RCC (product), not Chess (product).
  assert n_product = 1 and n_location = 1,
    format('type counts follow the family, got product=%s location=%s', n_product, n_location);
end;
$$;

-- Grants restated after the drop/recreate: anon must not execute either reader.
do $$
begin
  assert not has_function_privilege('anon',
    'public.recent_neighbor_tips(text, double precision, int, text, uuid, text[])', 'execute'),
    'anon cannot execute recent_neighbor_tips';
  assert not has_function_privilege('anon',
    'public.find_neighbor_tips(text, text, text, int, text, double precision, uuid, extensions.vector, real, text[])',
    'execute'),
    'anon cannot execute find_neighbor_tips';
  assert has_function_privilege('authenticated',
    'public.recent_neighbor_tips(text, double precision, int, text, uuid, text[])', 'execute'),
    'authenticated can execute recent_neighbor_tips';
end;
$$;

\echo 'chapter_recos_rollup: all checks passed'
rollback;
