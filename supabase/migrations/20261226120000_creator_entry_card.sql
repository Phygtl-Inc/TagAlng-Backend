-- What the creator entry screen actually paints.
--
-- The walkthrough's screen 01 (C-ENTRY-WELCOME-ASK-VID2) opens on a card that reads
--
--     [photo]  Sarah Reyes
--              Runners · Lake Nona
--
-- over Lana's video hello. place_claim_card — the one read behind /c/<handle> — returns
-- the place's name, its members' noun, a blurb and a count, and none of the other three
-- facts on that card: who the creator IS as a person, their picture, and where the
-- community is run from. The screen has been rendering the community name where the
-- walkthrough shows a person, and nothing at all where it shows a face.
--
-- WHERE THE THREE FACTS LIVE
--   hq_city        — places.hq_city (20261214120000), already written by the claim.
--   creator_name   — place_features, alongside the creator_platform / creator_profile_url
--   creator_avatar   the claim already writes there. A feature row, not a places column,
--                    because that is where every other volunteered fact about a place
--                    lives and these are volunteered by exactly one person on one form.
--
-- DISCLOSURE. All three are things the creator typed in order to be found under their own
-- name, on a page whose whole purpose is to be shared in a bio. place_claim_card returns
-- no member identities and still returns none: the creator is the place here, and their
-- name is the place's name in every sense that matters to this card.

create or replace function public.place_claim_card(p_place_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public
as $$
declare
  p        record;
  v_count  int;
  v_noun   text;
  v_emoji  text;
  v_person text;
  v_avatar text;
begin
  select pl.id, pl.name, pl.place_type, pl.zip, pl.blurb, pl.governance_state,
         pl.first_action, pl.hq_city
    into p
    from public.places pl
   where pl.id = p_place_id;

  if p.id is null then
    return null;
  end if;

  select count(*)::int into v_count
    from public.circle_affiliations a
   where a.place_ref = p_place_id
     and a.dismissed_at is null;

  -- What members call it, rather than the grouping bucket. Ties break alphabetically so
  -- the card does not flicker between equally common words. Null when nobody has named it,
  -- and the card simply omits the phrase rather than inventing one.
  select a.noun, a.emoji into v_noun, v_emoji
    from public.circle_affiliations a
   where a.place_ref = p_place_id
     and a.dismissed_at is null
     and a.noun is not null
   group by a.noun, a.emoji
   order by count(*) desc, a.noun asc
   limit 1;

  -- The person behind the handle. Highest-confidence row wins, then the newest — the same
  -- shape place_features is read with everywhere else. Null stays null: a card with no
  -- face renders the community's own emoji, which is what it does today.
  select f.value into v_person
    from public.place_features f
   where f.place_id = p_place_id and f.key = 'creator_name'
     and nullif(btrim(coalesce(f.value, '')), '') is not null
   order by f.confidence desc nulls last, f.created_at desc
   limit 1;

  select f.value into v_avatar
    from public.place_features f
   where f.place_id = p_place_id and f.key = 'creator_avatar_url'
     and nullif(btrim(coalesce(f.value, '')), '') is not null
   order by f.confidence desc nulls last, f.created_at desc
   limit 1;

  return jsonb_build_object(
    'placeId',         p.id,
    'displayName',     p.name,
    'placeType',       p.place_type,
    'zip',             p.zip,
    'blurb',           p.blurb,
    'noun',            v_noun,
    'emoji',           v_emoji,
    'firstAction',     p.first_action,
    'governanceState', p.governance_state,
    'hasMembers',      v_count > 0,
    'memberCount',     v_count,
    -- A LABEL and nothing else. hq_city appears in no discovery predicate (20261214120000);
    -- this is the "· Lake Nona" half of the entry card's subtitle.
    'hqCity',          p.hq_city,
    'creatorName',     v_person,
    'creatorAvatarUrl',v_avatar);
end;
$$;

comment on function public.place_claim_card(uuid) is
  'Community card for the claim flow and the public /c/<handle> page: place, member '
  'noun/emoji, description, member count, and — for a creator community — the person '
  'behind the handle (name, avatar) plus the city it is run from. No member names, ever; '
  'the creator''s own name is the place''s name here, not a member identity. hq_city is a '
  'label and never a distance.';

-- The grant 20261114120000 made (anon + authenticated, for the public page) is on the
-- same signature and survives `create or replace`, so it is not restated here — restating
-- it would imply it had been lost.

-- ============================================================================
-- ROLLBACK
--   Re-run 20261113120000_claim_card_show_members.sql: same signature, same return type,
--   so a plain replace puts the older body back and the three new keys simply stop
--   appearing. Every reader takes them with a null-coalesce, so the page degrades to the
--   community's own name and emoji rather than breaking.
-- ============================================================================
