-- ============================================================================
-- Attribute salience (the profile "slider")
-- ----------------------------------------------------------------------------
-- ANSWER (Asjid's question: "how should the system treat the slider values for
-- the query?"):
--
--   The slider is a THIRD, orthogonal axis on a claim. We already store:
--     • confidence  — how sure Lana is the claim is TRUE   (the "95% from your story")
--     • disclosure  — WHO may see it (public/mutual/private)
--   The slider adds:
--     • salience    — how much the USER wants it to STEER matching
--
--   Treat salience as a SOFT, VIEWER-RELATIVE, MULTIPLICATIVE weight on that
--   claim's contribution to the match score — NOT a hard filter.
--     - OFF  = weight 0 → the claim does not steer ranking at all. It is NOT
--              deleted and NOT a filter that removes candidates; it simply
--              stops contributing. (Still visible per disclosure/tier rules.)
--     - LOW/MED/HIGH = 0.5 / 1.0 / 2.0 multipliers (MED = neutral default).
--   Rules that make it behave:
--     1. VIEWER-RELATIVE: each person ranks their OWN candidate list with THEIR
--        OWN sliders ("how much *I* care"). Matching surfaces are per-viewer.
--     2. × CONFIDENCE: effective weight = salience_weight × confidence, so a
--        cranked-HIGH but shakily-inferred claim can't dominate on a guess.
--     3. NORMALIZED: divide by the sum of the viewer's active weights so a user
--        who sets everything HIGH doesn't inflate absolute scores — keeps
--        min_similarity thresholds and cross-user comparisons meaningful.
--     4. RE-RANK, NOT GATE: the onion rings / candidate pool are unchanged; the
--        slider only reorders WITHIN the pool. HIGH boosts, it never *requires*
--        (a hard requirement would give day-zero pioneers an empty list —
--        violates "matching is never a dead end", ZIP-unlock spec §4).
--
-- SCOPE: 1 enum + 1 additive column (default 'med') + 1 immutable lookup fn +
--        1 NEW weighted match fn (sibling — the existing
--        match_peers_by_claim_vectors_for_user is left untouched).
--        Non-destructive. Idempotent. No backfill needed (default covers all
--        existing + future rows; MED = today's uniform behavior).
-- REAL SCHEMA USED (verified 2026-07-27, tagalng-prod):
--        user_identity_claims(concept,label,bucket,embedding,confidence,
--        disclosure,dismissed_at), users(id,home_block_id,nickname,
--        profile_photo_url). Mirrors match_peers_by_claim_vectors_for_user.
-- ROLLBACK: at the bottom.
-- ============================================================================

begin;

-- 1) SALIENCE VOCABULARY (mirrors the UI slider stops exactly).
do $$
begin
  if not exists (select 1 from pg_type where typname = 'claim_salience') then
    create type claim_salience as enum ('off', 'low', 'med', 'high');
  end if;
end $$;

-- 2) THE SLIDER, PERSISTED. Default 'med' = neutral → existing rows keep today's
--    equal-weight behavior with zero backfill. OFF is a real state, not NULL.
alter table public.user_identity_claims
  add column if not exists salience claim_salience not null default 'med';

comment on column public.user_identity_claims.salience is
  'User-set match salience (profile slider). Orthogonal to confidence (truth) '
  'and disclosure (visibility). off=excluded from ranking (not deleted); '
  'low/med/high = 0.5/1.0/2.0 soft multipliers. See LANA_ATTRIBUTE_SALIENCE_SPEC_v1.';

-- 3) WEIGHT LOOKUP — single source of truth for the numeric scale (tunable here).
create or replace function public.claim_salience_weight(p_s claim_salience)
returns real language sql immutable as $$
  select case p_s
    when 'off'  then 0.0::real
    when 'low'  then 0.5::real
    when 'med'  then 1.0::real
    when 'high' then 2.0::real
  end;
$$;

-- 4) WEIGHTED PEER MATCH — sibling of match_peers_by_claim_vectors_for_user.
--    Same candidate pool + same heritage/faith guard; only the SCORE changes:
--    weighted_score = Σ_over_my_active_concepts( best_sim × salience_w × confidence )
--                     ─────────────────────────────────────────────────────────────
--                                    Σ my_active_weights
--    (self-normalizes to a peer's own active weights if the viewer has none set,
--     so it never divides by zero and never returns empty on a technicality.)
create or replace function public.match_peers_by_claim_vectors_weighted_for_user(
  p_user_id uuid,
  p_limit integer default 10,
  p_min_similarity real default 0.70
)
returns table(
  peer_user_id uuid,
  nickname text,
  avatar_url text,
  weighted_score real,
  raw_similarity real,
  matching_peer_label text,
  matching_peer_concept text,
  has_exact_concept_match boolean,
  matching_my_label text,
  shared_labels text[]
)
language plpgsql
stable security definer
set search_path to 'pg_catalog', 'public', 'extensions'
as $function$
declare
  v_block_id text;
begin
  if p_user_id is null then
    raise exception 'user_id_required' using errcode = 'P0001';
  end if;

  select u.home_block_id into v_block_id
  from public.users u where u.id = p_user_id;

  if v_block_id is null then
    return;
  end if;

  return query
  with caller_claims as (
    -- viewer's own public, non-dismissed, NON-OFF claims + their effective weight
    select
      c.concept, c.label, c.bucket, c.embedding,
      (public.claim_salience_weight(c.salience)
         * greatest(coalesce(c.confidence, 1.0), 0.0))::real as w
    from public.user_identity_claims c
    where c.user_id = p_user_id
      and c.dismissed_at is null
      and c.disclosure = 'public'
      and c.salience <> 'off'          -- OFF opts the attribute out of steering
  ),
  norm as (
    select nullif(sum(w), 0)::real as total from caller_claims
  ),
  peer_claim_pairs as (
    select
      pc.user_id  as peer_id,
      pc.concept  as peer_concept,
      pc.label    as peer_label,
      cc.concept  as my_concept,
      cc.label    as my_label,
      cc.w        as my_w,
      (cc.concept = pc.concept) as exact_pair,
      case
        when cc.concept = pc.concept
          then coalesce((1 - (cc.embedding <=> pc.embedding))::real, 1.0::real)
        else (1 - (cc.embedding <=> pc.embedding))::real
      end as sim
    from caller_claims cc
    join public.user_identity_claims pc
      on pc.user_id <> p_user_id
     and pc.dismissed_at is null
     and pc.disclosure = 'public'
    join public.users u on u.id = pc.user_id
    where u.home_block_id = v_block_id
      and (
        cc.concept = pc.concept
        or (
          cc.embedding is not null
          and pc.embedding is not null
          and (1 - (cc.embedding <=> pc.embedding)) >= p_min_similarity
          and not (
            coalesce(cc.bucket, 'general') in ('heritage', 'faith')
            and coalesce(pc.bucket, 'general') in ('heritage', 'faith')
            and cc.concept <> pc.concept
          )
        )
      )
  ),
  -- one row per (peer, my_concept): a single attribute is counted once
  best_per_concept as (
    select distinct on (peer_id, my_concept)
      peer_id, my_concept, my_label, my_w, peer_label, peer_concept, exact_pair, sim
    from peer_claim_pairs
    order by peer_id, my_concept, exact_pair desc, sim desc
  ),
  peer_agg as (
    select
      peer_id,
      (sum(sim * my_w) / coalesce((select total from norm), sum(my_w)))::real as weighted_score,
      max(sim)::real as raw_similarity
    from best_per_concept
    group by peer_id
  ),
  -- representative attribute = the one with the highest weighted contribution
  peer_top as (
    select distinct on (peer_id)
      peer_id, my_label, peer_label, peer_concept, exact_pair
    from best_per_concept
    order by peer_id, (sim * my_w) desc, exact_pair desc
  ),
  peer_shared as (
    select peer_id, array_agg(peer_label order by best_sim desc) as shared_labels
    from (
      select peer_id, peer_label, max(sim) as best_sim
      from best_per_concept
      where exact_pair
      group by peer_id, peer_label
    ) s
    group by peer_id
  )
  select
    pa.peer_id as peer_user_id,
    u.nickname,
    u.profile_photo_url as avatar_url,
    pa.weighted_score,
    pa.raw_similarity,
    pt.peer_label as matching_peer_label,
    pt.peer_concept as matching_peer_concept,
    pt.exact_pair as has_exact_concept_match,
    pt.my_label as matching_my_label,
    coalesce(ps.shared_labels, '{}') as shared_labels
  from peer_agg pa
  join public.users u on u.id = pa.peer_id
  left join peer_top pt on pt.peer_id = pa.peer_id
  left join peer_shared ps on ps.peer_id = pa.peer_id
  order by pa.weighted_score desc, pa.raw_similarity desc, u.nickname asc nulls last
  limit greatest(1, least(coalesce(p_limit, 10), 50));
end;
$function$;

commit;

-- ============================================================================
-- TEST PLAN (run after apply):
--   -- default is neutral → weighted list ~ mirrors the current matcher's set:
--   select peer_user_id, weighted_score, matching_my_label
--     from match_peers_by_claim_vectors_weighted_for_user('<user>', 10, 0.70);
--   -- crank one attribute to HIGH and confirm peers sharing it rise:
--   update user_identity_claims set salience='high'
--     where user_id='<user>' and concept='<looking_meet_or_playgroup_concept>';
--   -- turn one OFF and confirm it stops steering (peers only-sharing it drop),
--   -- but the peer pool does NOT collapse (still discoverable via other claims):
--   update user_identity_claims set salience='off'
--     where user_id='<user>' and concept='<some_concept>';
--   -- weight scale sanity:
--   select claim_salience_weight('off'), claim_salience_weight('low'),
--          claim_salience_weight('med'), claim_salience_weight('high');  -- 0,0.5,1,2
-- NOTES FOR REVIEW:
--   • Non-destructive: existing match_peers_by_claim_vectors_for_user untouched;
--     app can switch to the weighted fn behind a flag, then retire the old one.
--   • All-OFF user = deliberate opt-out of attribute steering (returns empty from
--     THIS fn by design); other surfaces (events, intros) still reach them.
--   • Scale (0/0.5/1/2) lives only in claim_salience_weight() — tune there.
-- ROLLBACK:
--   drop function if exists public.match_peers_by_claim_vectors_weighted_for_user(uuid, integer, real);
--   drop function if exists public.claim_salience_weight(claim_salience);
--   alter table public.user_identity_claims drop column if exists salience;
--   drop type if exists claim_salience;
-- ============================================================================
