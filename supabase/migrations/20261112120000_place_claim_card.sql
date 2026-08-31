-- Lana · the community card an operator sees while claiming ─────────────────────────
-- The claim flow showed a sentence ("people are already using Lana here"). It should show
-- the community the way the app does: the place, what people call it, and how many.
--
-- discover_communities_near is the app's card, but it is scoped to one member's
-- reachability — the right rule for a neighbour, the wrong one for an operator who is not
-- a member of anything. So this is a claim-context card: same facts, no names, no
-- reachability, and service-role only.
--
-- The member count is withheld below three. That is the same instinct as the attestation
-- floor in LOCATIONS_VALUE_PROP §6 ("nothing publishes below three attesters. Below that
-- it de-anonymises at our density"): at one or two members, a count plus a place name is
-- close to naming people. hasMembers still says the community is real.
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

  -- What members actually call it, rather than the grouping bucket. Ties break on the
  -- alphabetically first noun so the card does not flicker between equally common words.
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
    -- The blurb is AI-authored from facts that include the member count, so at one or
    -- two members it can state in prose the number the field below withholds. Same floor
    -- for both, or the suppression is decorative.
    'blurb',           case when v_count >= 3 then p.blurb end,
    'noun',            v_noun,
    'emoji',           v_emoji,
    'firstAction',     p.first_action,
    'governanceState', p.governance_state,
    'hasMembers',      v_count > 0,
    -- Null below the floor: the card still says the community exists.
    'memberCount',     case when v_count >= 3 then v_count end);
end;
$$;

comment on function public.place_claim_card(uuid) is
  'Community card for the claim flow: place, member noun/emoji, blurb, and a member count '
  'only at three or more. No names, ever. Service-role only — the caller is an operator '
  'who is not a member, so reachability rules do not apply and must not be borrowed.';

revoke execute on function public.place_claim_card(uuid) from public, anon, authenticated;
