-- Lana · the claim card shows the member count, always ─────────────────────────────
-- 20261112120000 withheld memberCount and blurb below three members, on my own
-- de-anonymisation argument. That was not asked for, and it suppressed the one thing the
-- card exists to say: a place with two members rendered as "people are already here",
-- which reads as no information at all.
--
-- Product decision (Asjid, repeatedly): show the community's title and its member count.
-- So the count is always returned, and the description with it. Names are still never
-- returned — that part was never in question.
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
begin
  select pl.id, pl.name, pl.place_type, pl.zip, pl.blurb, pl.governance_state, pl.first_action
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
    'memberCount',     v_count);
end;
$$;

comment on function public.place_claim_card(uuid) is
  'Community card for the claim flow: place, member noun/emoji, description, and the '
  'member count. No member names, ever. Service-role only — the caller is an operator who '
  'is not a member, so member reachability rules do not apply and must not be borrowed.';

revoke execute on function public.place_claim_card(uuid) from public, anon, authenticated;
