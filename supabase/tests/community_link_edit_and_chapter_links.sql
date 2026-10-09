-- 20270131120000_community_link_edit_and_chapter_links · behaviour checks
-- (local validation container only — never against dev or prod)
--   psql -h 127.0.0.1 -p 55432 -U supabase_admin -d postgres -v ON_ERROR_STOP=1 \
--        -f supabase/tests/community_link_edit_and_chapter_links.sql
-- Everything runs in one transaction and is rolled back. Any failed assert aborts it.
--
-- The backfill over chapters that existed BEFORE the migration is proven separately by
-- seeding chapters, then applying the migration (see the PR). Section 1 re-runs the same
-- path here: chapters inserted with the trigger off, then the migration's loop.

begin;

insert into auth.users (id, email, email_confirmed_at, is_anonymous) values
  ('00000000-0000-0000-0000-0000000cc001', 'op-parent@example.com',  now(), false),
  ('00000000-0000-0000-0000-0000000cc002', 'op-other@example.com',   now(), false),
  ('00000000-0000-0000-0000-0000000cc003', 'op-chapter@example.com', now(), false),
  ('00000000-0000-0000-0000-0000000cc004', 'stranger@example.com',   now(), false)
on conflict (id) do nothing;

insert into public.protected_handles (normalized_handle, reason, active)
values ('nike-runners', 'test brand', true)
on conflict (normalized_handle) do update set active = true;

-- Two parents with links, one parent without, each run by its operator (claimed_by →
-- places_sync_operator makes the operator row).
insert into public.places (id, google_place_id, name, lat, lng, created_by) values
  ('00000000-0000-0000-0000-0000000dd001', 'cl-sjsu',  'SJSU Campus',  37.33, -121.88, '00000000-0000-0000-0000-0000000cc001'),
  ('00000000-0000-0000-0000-0000000dd002', 'cl-moms',  'Moms Orlando', 28.54, -81.38,  '00000000-0000-0000-0000-0000000cc002'),
  ('00000000-0000-0000-0000-0000000dd003', 'cl-plain', 'Plain Club',   28.54, -81.38,  '00000000-0000-0000-0000-0000000cc002');
insert into public.place_claims (place_id, requested_by, status, verification_method, submitted_at, resolved_at) values
  ('00000000-0000-0000-0000-0000000dd001', '00000000-0000-0000-0000-0000000cc001', 'verified', 'manual_founder', now(), now()),
  ('00000000-0000-0000-0000-0000000dd002', '00000000-0000-0000-0000-0000000cc002', 'verified', 'manual_founder', now(), now());
update public.places set governance_state = 'operator_verified', verified_at = now(),
       claimed_by = '00000000-0000-0000-0000-0000000cc001', handle = 'sjsu-campus'
 where id = '00000000-0000-0000-0000-0000000dd001';
update public.places set governance_state = 'operator_verified', verified_at = now(),
       claimed_by = '00000000-0000-0000-0000-0000000cc002', handle = 'moms-orlando'
 where id = '00000000-0000-0000-0000-0000000dd002';
update public.places set claimed_by = '00000000-0000-0000-0000-0000000cc002'
 where id = '00000000-0000-0000-0000-0000000dd003';

-- Confirmed members of SJSU, for community_settings' parent.memberCount.
insert into public.circle_affiliations (user_id, circle_type, circle_key, place_ref, status, source) values
  ('00000000-0000-0000-0000-0000000cc001', 'school', 'sjsu_campus', '00000000-0000-0000-0000-0000000dd001', 'confirmed', 'profile_add'),
  ('00000000-0000-0000-0000-0000000cc004', 'school', 'sjsu_campus', '00000000-0000-0000-0000-0000000dd001', 'confirmed', 'profile_add'),
  ('00000000-0000-0000-0000-0000000cc003', 'school', 'sjsu_campus', '00000000-0000-0000-0000-0000000dd001', 'suggested', 'profile_add'),
  -- A member of another community, who must not count toward SJSU.
  ('00000000-0000-0000-0000-0000000cc002', 'other', 'moms_orlando', '00000000-0000-0000-0000-0000000dd002', 'confirmed', 'profile_add');

-- ── 1 · backfill + automatic assignment ──────────────────────────────────────

alter table public.places disable trigger places_assign_chapter_handle_trg;
insert into public.places (id, google_place_id, name, lat, lng, parent_place_ref, created_at) values
  ('00000000-0000-0000-0000-0000000ee001', 'cl-c1', 'Stroller Walk',  37.33, -121.88, '00000000-0000-0000-0000-0000000dd001', now() - interval '3 days'),
  ('00000000-0000-0000-0000-0000000ee002', 'cl-c2', 'Stroller Walk',  37.33, -121.88, '00000000-0000-0000-0000-0000000dd001', now() - interval '2 days'),
  ('00000000-0000-0000-0000-0000000ee003', 'cl-c3', 'Stroller Walk',  37.33, -121.88, '00000000-0000-0000-0000-0000000dd001', now() - interval '1 days'),
  ('00000000-0000-0000-0000-0000000ee011', 'cl-c4', 'Stroller Walk',  28.54, -81.38,  '00000000-0000-0000-0000-0000000dd002', now() - interval '1 days'),
  ('00000000-0000-0000-0000-0000000ee021', 'cl-c5', 'Plain Walkers',  28.54, -81.38,  '00000000-0000-0000-0000-0000000dd003', now() - interval '1 days');
alter table public.places enable trigger places_assign_chapter_handle_trg;

-- The migration's backfill loop, verbatim.
do $$
declare
  r record;
begin
  for r in
    select p.id from public.places p
     where p.parent_place_ref is not null and p.chapter_handle is null
     order by p.created_at asc nulls last, p.id
  loop
    update public.places set chapter_handle = null where id = r.id;
  end loop;
end;
$$;

do $$
begin
  assert (select chapter_handle from public.places where id = '00000000-0000-0000-0000-0000000ee001') = 'strollerwalk',
    'oldest chapter gets the compact word';
  assert (select chapter_handle from public.places where id = '00000000-0000-0000-0000-0000000ee002') = 'stroller-walk',
    'second gets the dashed form';
  assert (select chapter_handle from public.places where id = '00000000-0000-0000-0000-0000000ee003') = 'strollerwalk2',
    'third gets a numeric suffix';
  assert (select count(distinct chapter_handle) from public.places
           where parent_place_ref = '00000000-0000-0000-0000-0000000dd001') =
         (select count(*) from public.places
           where parent_place_ref = '00000000-0000-0000-0000-0000000dd001'),
    'unique within the parent';
  -- The same word under another parent is fine.
  assert (select chapter_handle from public.places where id = '00000000-0000-0000-0000-0000000ee011') = 'strollerwalk',
    'same chapter handle allowed under a different parent';
  assert (select chapter_handle_renamed_at from public.places where id = '00000000-0000-0000-0000-0000000ee001') is null,
    'automatic assignment is not the rename';
end;
$$;

-- The index refuses a duplicate written around the trigger's choice.
do $$
begin
  begin
    update public.places set chapter_handle = 'strollerwalk'
     where id = '00000000-0000-0000-0000-0000000ee002';
    assert false, 'duplicate under same parent must fail';
  exception when unique_violation then null;
  end;
end;
$$;

-- attach_chapter: a standalone community attached to SJSU whose word is taken there.
insert into public.places (id, google_place_id, name, lat, lng, created_by) values
  ('00000000-0000-0000-0000-0000000ee004', 'cl-c6', 'Stroller Walk', 37.33, -121.88, '00000000-0000-0000-0000-0000000cc001');
do $$
declare j jsonb;
begin
  assert (select chapter_handle from public.places where id = '00000000-0000-0000-0000-0000000ee004') is null,
    'a standalone community has no chapter handle';
  j := public.attach_chapter('00000000-0000-0000-0000-0000000cc001',
                             '00000000-0000-0000-0000-0000000ee004',
                             '00000000-0000-0000-0000-0000000dd001');
  assert (select chapter_handle from public.places where id = '00000000-0000-0000-0000-0000000ee004') = 'strollerwalk3',
    'attach collision gets the next suffix: ' ||
    coalesce((select chapter_handle from public.places where id = '00000000-0000-0000-0000-0000000ee004'), 'null');
  -- Detach keeps the word; re-attach where it is free keeps it, where it is taken moves.
  perform public.detach_chapter('00000000-0000-0000-0000-0000000cc001', '00000000-0000-0000-0000-0000000ee004');
  assert (select chapter_handle from public.places where id = '00000000-0000-0000-0000-0000000ee004') = 'strollerwalk3',
    'detach keeps the chapter handle';
end;
$$;

-- The chapter-only operator.
insert into public.place_managers (place_id, user_id, role, verification_method, verified_at, added_by)
values ('00000000-0000-0000-0000-0000000ee001', '00000000-0000-0000-0000-0000000cc003', 'operator',
        'claimed_by_sync', now(), '00000000-0000-0000-0000-0000000cc003');

-- ── 2 · check_community_link statuses ────────────────────────────────────────

do $$
declare
  c1 uuid := '00000000-0000-0000-0000-0000000ee001';   -- SJSU / strollerwalk
  j  jsonb;
begin
  -- not_operator: a stranger, and nobody signed in.
  perform set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-0000000cc004', true);
  j := public.check_community_link(c1, 'anything-new');
  assert j->>'status' = 'not_operator' and j->>'kind' = 'chapter', 'stranger ' || j::text;
  -- The other parent's operator has no standing on SJSU's chapter.
  perform set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-0000000cc002', true);
  j := public.check_community_link(c1, 'anything-new');
  assert j->>'status' = 'not_operator', 'other parent operator ' || j::text;
  perform set_config('request.jwt.claim.sub', '', true);
  j := public.check_community_link(c1, 'anything-new');
  assert j->>'status' = 'not_operator', 'signed out ' || j::text;

  -- The chapter's own operator.
  perform set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-0000000cc003', true);
  j := public.check_community_link(c1, 'Night Walkers');
  assert j->>'status' = 'available' and j->>'normalizedHandle' = 'night-walkers'
     and j->>'kind' = 'chapter', 'available ' || j::text;
  j := public.check_community_link(c1, 'strollerwalk');
  assert j->>'status' = 'unchanged', 'unchanged ' || j::text;
  j := public.check_community_link(c1, 'stroller-walk');
  assert j->>'status' = 'unavailable' and j->>'reason' = 'taken', 'sibling taken ' || j::text;
  -- A word a chapter under ANOTHER parent holds is free here.
  j := public.check_community_link('00000000-0000-0000-0000-0000000ee002', 'strollerwalk');
  assert j->>'status' = 'not_operator', 'cc003 runs only ee001 ' || j::text;
  j := public.check_community_link(c1, 'ab');
  assert j->>'status' = 'invalid' and j->>'reason' = 'too_short', 'too short ' || j::text;
  j := public.check_community_link(c1, '---');
  assert j->>'status' = 'invalid' and j->>'reason' = 'malformed', 'malformed ' || j::text;
  j := public.check_community_link(c1, repeat('a', 49));
  assert j->>'status' = 'invalid' and j->>'reason' = 'too_long', 'too long ' || j::text;

  -- The parent's operator may check a chapter too.
  perform set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-0000000cc001', true);
  j := public.check_community_link('00000000-0000-0000-0000-0000000ee002', 'strollerwalk');
  assert j->>'status' = 'unavailable' and j->>'reason' = 'taken', 'parent operator, taken ' || j::text;

  -- A community (no parent): the global handle rules.
  j := public.check_community_link('00000000-0000-0000-0000-0000000dd001', 'sjsu-spartans');
  assert j->>'status' = 'available' and j->>'kind' = 'community', 'community available ' || j::text;
  j := public.check_community_link('00000000-0000-0000-0000-0000000dd001', 'sjsu-campus');
  assert j->>'status' = 'unchanged', 'community unchanged ' || j::text;
  j := public.check_community_link('00000000-0000-0000-0000-0000000dd001', 'moms-orlando');
  assert j->>'status' = 'unavailable' and j->>'reason' = 'taken', 'community taken ' || j::text;
  j := public.check_community_link('00000000-0000-0000-0000-0000000dd001', 'nike-runners');
  assert j->>'status' = 'unavailable' and j->>'reason' = 'protected', 'protected ' || j::text;
  -- A bare word: the rename mints its proof for an operator with a confirmed email
  -- (20270202120000), so it is available. supabase/tests/rename_mints_bare_word_proof.sql.
  j := public.check_community_link('00000000-0000-0000-0000-0000000dd001', 'spartans');
  assert j->>'status' = 'available', 'bare word, confirmed operator ' || j::text;
  -- A chapter whose parent has no link is checked as a community.
  perform set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-0000000cc002', true);
  j := public.check_community_link('00000000-0000-0000-0000-0000000ee021', 'plain-walkers');
  assert j->>'kind' = 'community' and j->>'status' = 'available', 'linkless parent ' || j::text;
end;
$$;

-- ── 3 · rename_chapter_handle · once, alias keeps resolving ──────────────────

do $$
declare
  c1 uuid := '00000000-0000-0000-0000-0000000ee001';
  j  jsonb;
  e  text;
begin
  -- Non-operators are refused.
  perform set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-0000000cc004', true);
  begin
    perform public.rename_chapter_handle(c1, 'night-walkers');
    assert false, 'stranger rename must fail';
  exception when others then
    assert sqlerrm = 'not_operator', 'stranger: ' || sqlerrm;
  end;
  perform set_config('request.jwt.claim.sub', '', true);
  begin
    perform public.rename_chapter_handle(c1, 'night-walkers');
    assert false, 'signed-out rename must fail';
  exception when others then
    assert sqlerrm = 'not_authenticated', 'signed out: ' || sqlerrm;
  end;

  perform set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-0000000cc001', true);
  -- Not a chapter / parent without a link.
  begin
    perform public.rename_chapter_handle('00000000-0000-0000-0000-0000000dd001', 'whatever');
    assert false;
  exception when others then
    assert sqlerrm = 'not_a_chapter', 'not a chapter: ' || sqlerrm;
  end;
  perform set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-0000000cc002', true);
  begin
    perform public.rename_chapter_handle('00000000-0000-0000-0000-0000000ee021', 'whatever');
    assert false;
  exception when others then
    assert sqlerrm = 'parent_has_no_handle', 'linkless parent: ' || sqlerrm;
  end;

  -- The chapter's operator: each refusal, then the one rename.
  perform set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-0000000cc003', true);
  foreach e in array array['stroller-walk:handle_taken', 'strollerwalk:handle_unchanged',
                           '---:handle_shape', 'ab:handle_length'] loop
    begin
      perform public.rename_chapter_handle(c1, split_part(e, ':', 1));
      assert false, 'must refuse ' || e;
    exception when others then
      assert sqlerrm = split_part(e, ':', 2), e || ' got ' || sqlerrm;
    end;
  end loop;

  j := public.rename_chapter_handle(c1, 'Night Walkers');
  assert j->>'chapterHandle' = 'night-walkers'
     and j->>'parentHandle' = 'sjsu-campus'
     and j->>'previousChapterHandle' = 'strollerwalk'
     and (j->>'renamesRemaining')::int = 0
     and (j->>'placeId')::uuid = c1, 'rename result ' || j::text;

  begin
    perform public.rename_chapter_handle(c1, 'day-walkers');
    assert false, 'second rename must fail';
  exception when others then
    assert sqlerrm = 'rename_already_used', 'second rename: ' || sqlerrm;
  end;
  j := public.check_community_link(c1, 'day-walkers');
  assert j->>'status' = 'rename_used', 'check after rename ' || j::text;

  -- The retired word is not handed to a sibling.
  perform set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-0000000cc001', true);
  j := public.check_community_link('00000000-0000-0000-0000-0000000ee003', 'strollerwalk');
  assert j->>'status' = 'unavailable' and j->>'reason' = 'retired', 'retired check ' || j::text;
  begin
    perform public.rename_chapter_handle('00000000-0000-0000-0000-0000000ee003', 'strollerwalk');
    assert false;
  exception when others then
    assert sqlerrm = 'handle_retired_elsewhere', 'retired rename: ' || sqlerrm;
  end;
  -- … nor by the trigger, when something new is attached and named the same.
  perform public.attach_chapter('00000000-0000-0000-0000-0000000cc001',
                                '00000000-0000-0000-0000-0000000ee004',
                                '00000000-0000-0000-0000-0000000dd001');
  assert (select chapter_handle from public.places where id = '00000000-0000-0000-0000-0000000ee004') = 'strollerwalk3',
    're-attach keeps its free word';
end;
$$;

-- ── 4 · resolve_chapter_handle ───────────────────────────────────────────────

do $$
declare j jsonb;
begin
  j := public.resolve_chapter_handle('sjsu-campus', 'night-walkers');
  assert (j->>'placeId')::uuid = '00000000-0000-0000-0000-0000000ee001'
     and j->>'chapterHandle' = 'night-walkers'
     and j->>'link' = 'sjsu-campus/night-walkers'
     and (j->>'viaAlias')::boolean = false
     and j->'parent'->>'handle' = 'sjsu-campus'
     and j->'parent'->>'displayName' = 'SJSU Campus'
     and j ? 'displayName' and j ? 'operatorVerified' and j ? 'isTest' and j ? 'creator',
    'resolve current ' || j::text;
  assert not (j ? 'members') and not (j::text ilike '%@example.com%'), 'no member identities';

  j := public.resolve_chapter_handle('SJSU-Campus', 'strollerwalk');
  assert (j->>'placeId')::uuid = '00000000-0000-0000-0000-0000000ee001'
     and (j->>'viaAlias')::boolean and j->>'chapterHandle' = 'night-walkers',
    'old chapter link keeps resolving ' || coalesce(j::text, 'null');

  assert public.resolve_chapter_handle('moms-orlando', 'night-walkers') is null,
    'wrong parent resolves nothing';
  assert (public.resolve_chapter_handle('moms-orlando', 'strollerwalk')->>'placeId')::uuid
         = '00000000-0000-0000-0000-0000000ee011', 'same word under the other parent';
  assert public.resolve_chapter_handle('sjsu-campus', 'nope-nope') is null, 'unknown chapter';
  assert public.resolve_chapter_handle('no-such-parent', 'night-walkers') is null, 'unknown parent';

  -- An alias stops resolving once the chapter leaves that parent.
  perform public.detach_chapter('00000000-0000-0000-0000-0000000cc001', '00000000-0000-0000-0000-0000000ee001');
  assert public.resolve_chapter_handle('sjsu-campus', 'strollerwalk') is null, 'detached: alias dead';
  assert public.resolve_chapter_handle('sjsu-campus', 'night-walkers') is null, 'detached: link dead';
  -- (A hand-written attach, as RCC's was: the trigger covers every path, not just the RPC.)
  update public.places set parent_place_ref = '00000000-0000-0000-0000-0000000dd001'
   where id = '00000000-0000-0000-0000-0000000ee001';
  assert (select chapter_handle from public.places where id = '00000000-0000-0000-0000-0000000ee001') = 'night-walkers',
    're-attached under the same parent keeps its renamed word';

  -- Moving a chapter that already HAS a word to a parent where that word is taken (or
  -- retired): it gets the next free one there, never a duplicate or a retired link.
  update public.places set parent_place_ref = null
   where id = '00000000-0000-0000-0000-0000000ee011';
  update public.places set parent_place_ref = '00000000-0000-0000-0000-0000000dd001'
   where id = '00000000-0000-0000-0000-0000000ee011';
  assert (select chapter_handle from public.places where id = '00000000-0000-0000-0000-0000000ee011') = 'strollerwalk4',
    'moved chapter re-assigned: ' ||
    coalesce((select chapter_handle from public.places where id = '00000000-0000-0000-0000-0000000ee011'), 'null');
  update public.places set parent_place_ref = '00000000-0000-0000-0000-0000000dd002'
   where id = '00000000-0000-0000-0000-0000000ee011';
  assert (select chapter_handle from public.places where id = '00000000-0000-0000-0000-0000000ee011') = 'strollerwalk4',
    'moved where its word is free: kept';
end;
$$;

-- The parent renames its global handle: the old parent link + chapter still resolve.
do $$
declare j jsonb;
begin
  perform set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-0000000cc001', true);
  perform public.rename_community_handle('00000000-0000-0000-0000-0000000dd001', 'sjsu-spartans');
  j := public.resolve_chapter_handle('sjsu-campus', 'night-walkers');
  assert (j->>'viaAlias')::boolean and j->>'link' = 'sjsu-spartans/night-walkers',
    'parent alias ' || coalesce(j::text, 'null');
  j := public.check_community_link('00000000-0000-0000-0000-0000000dd001', 'sjsu-other');
  assert j->>'status' = 'rename_used' and j->>'kind' = 'community', 'community rename_used ' || j::text;
  j := public.check_community_link('00000000-0000-0000-0000-0000000dd002', 'sjsu-campus');
  assert j->>'status' = 'not_operator', 'cc001 does not run Moms Orlando';
  perform set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-0000000cc002', true);
  j := public.check_community_link('00000000-0000-0000-0000-0000000dd002', 'sjsu-campus');
  assert j->>'status' = 'unavailable' and j->>'reason' = 'retired', 'global alias retired ' || j::text;
end;
$$;

-- ── 5 · community_settings ───────────────────────────────────────────────────

do $$
declare j jsonb;
begin
  perform set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-0000000cc003', true);
  j := public.community_settings('00000000-0000-0000-0000-0000000ee001');
  assert j->>'chapterHandle' = 'night-walkers'
     and j->>'link' = 'sjsu-spartans/night-walkers'
     and (j->>'canRenameLink')::boolean = false
     and (j->'parent'->>'placeId')::uuid = '00000000-0000-0000-0000-0000000dd001'
     and j->'parent'->>'name' = 'SJSU Campus'
     and j->'parent'->>'handle' = 'sjsu-spartans'
     and (j->'parent'->>'memberCount')::int = 2,
    'chapter settings ' || j::text;
  assert j ? 'canRenameHandle' and j ? 'firstAction', 'existing keys kept';

  perform set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-0000000cc002', true);
  j := public.community_settings('00000000-0000-0000-0000-0000000dd002');
  assert j->>'link' = 'moms-orlando' and (j->>'canRenameLink')::boolean
     and j->'parent' = 'null'::jsonb and j->'chapterHandle' = 'null'::jsonb,
    'community settings ' || j::text;
  -- A chapter under a parent with no link: its link is its own handle (none yet).
  j := public.community_settings('00000000-0000-0000-0000-0000000dd003');
  assert j->'parent' = 'null'::jsonb, 'plain club has no parent';
end;
$$;

-- ── 6 · grants ───────────────────────────────────────────────────────────────

set local role anon;
do $$
begin
  begin
    perform public.check_community_link('00000000-0000-0000-0000-0000000ee001', 'x-y');
    assert false, 'anon must not check';
  exception when insufficient_privilege then null;
  end;
  begin
    perform public.rename_chapter_handle('00000000-0000-0000-0000-0000000ee001', 'x-y');
    assert false, 'anon must not rename';
  exception when insufficient_privilege then null;
  end;
  begin
    perform 1 from public.place_chapter_handle_aliases;
    assert false, 'anon must not read aliases';
  exception when insufficient_privilege then null;
  end;
  assert public.resolve_chapter_handle('sjsu-spartans', 'night-walkers') is not null,
    'anon resolves a chapter link';
end;
$$;
reset role;

set local role authenticated;
do $$
begin
  assert public.resolve_chapter_handle('sjsu-spartans', 'night-walkers') is not null,
    'authenticated resolves a chapter link';
  begin
    perform 1 from public.place_chapter_handle_aliases;
    assert false, 'authenticated must not read aliases';
  exception when insufficient_privilege then null;
  end;
end;
$$;
reset role;

select 'community_link_edit_and_chapter_links: all assertions passed' as result;

rollback;
