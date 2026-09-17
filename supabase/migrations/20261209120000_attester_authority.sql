-- Recommender authority · A1, plus the one-line fix A2 needs to return anything at all.
-- SPEC_RECOMMENDER_AUTHORITY.md (2026-09-10 · Asjid + Pouya build · Tim gates).
--
-- "A 4.7 Italian restaurant in America is often bad by Italian standards. The raters
-- didn't lie — they had no standing to judge that dimension." This adds the third kind
-- of standing. Place standing is visible_place_members(); community standing is contract
-- v2 §A4.2; DOMAIN standing is this file: you demonstrably know this subject area.
--
-- Per concept, never global (§7). There is no expert badge here and there must never be
-- one — a global score converts a knowledge signal into a status game, which is what
-- Yelp Elite and Local Guides are and what this is meant to replace.
--
-- Authority never decides WHETHER a subject matches an ask. It reorders the set that
-- already matched, inside the ±0.05 topic band. That keeps D9-A8 topic dominance intact.
--
-- ── Three departures from the spec text, each deliberate ──────────────────────────
--
-- 1. least(0.60, NULL) is 0.60 in Postgres, not NULL: LEAST/GREATEST *ignore* null
--    arguments and return the smallest non-null one. The spec's expression therefore
--    hands the 0.60 claim cap to an attester with NO claims for the concept, ranking
--    them above someone who actually said "I'm Turkish" (0.10). Every boolean is
--    coalesced before it reaches the arithmetic. test_authority.py pins this at 0.0.
--
-- 2. The behavioural tier reads local_signals, not attestation + subject. Those are
--    contract v2 items 4 and 8 and neither exists yet. local_signals is already
--    attestation-shaped for this purpose: user_id IS the attester, embedding IS the
--    subject, created_at IS valid_from. So the tier measures something real today and
--    swaps to the graph by replacing ONE cte — signature, return shape and every
--    caller stay exactly as the spec wrote them.
--
-- 3. Corroboration counts evidence, not rows. See the note on claim_agg.
--
-- Tunables are function default arguments, never inlined literals (contract v2 Part 0),
-- so Tim moves them in §6/T4 calibration without a code deploy.

-- ---------------------------------------------------------------------------
-- 0. A2 unblock: match_concepts_by_embedding could never accept a null bucket.
-- ---------------------------------------------------------------------------
-- SPEC §4/A2 calls this with p_bucket := null to search every bucket, because an ask
-- can land in any of them — "from someone Turkish" is heritage, "who has run a marathon"
-- is activity. The predicate was a bare equality, and `null = 'heritage'` is null, not
-- false: the function returned ZERO rows for every null-bucket call. concepts_for_ask()
-- would have resolved nothing, attester_authority() would have been handed an empty
-- array, and the whole feature would have silently scored 0 forever.
create or replace function public.match_concepts_by_embedding(
  p_bucket         text,
  p_embedding      extensions.vector(768),
  p_limit          int,
  p_min_similarity real
) returns table (
  id uuid,
  concept text,
  label text,
  bucket text,
  synonyms text[],
  canonical_example_quote text,
  similarity real
)
language sql
stable
security definer
set search_path = pg_catalog, public, extensions
as $$
  select
    ic.id,
    ic.concept,
    ic.label,
    ic.bucket,
    ic.synonyms,
    ic.canonical_example_quote,
    (1 - (ic.canonical_embedding <=> p_embedding))::real as similarity
  from public.identity_concepts ic
  where (p_bucket is null or ic.bucket = p_bucket)
    and ic.canonical_embedding is not null
    and (1 - (ic.canonical_embedding <=> p_embedding)) >= p_min_similarity
  order by ic.canonical_embedding <=> p_embedding
  limit p_limit;
$$;

grant execute on function public.match_concepts_by_embedding(text, extensions.vector, int, real)
  to service_role;
revoke execute on function public.match_concepts_by_embedding(text, extensions.vector, int, real)
  from public, authenticated, anon;

-- ---------------------------------------------------------------------------
-- 1. A1 · attester_authority()
-- ---------------------------------------------------------------------------
-- claim_component     = min(0.60, 0.10·bare + 0.25·specific + 0.15·min(corroborations,2))
-- behaviour_component = min(0.40, 0.20·min(distinct_behavioural_evidence, 2))
-- authority           = claim_component + behaviour_component            -- [0, 1]
--
-- Normative: claims alone cap at 0.60. Only behavioural evidence goes above it. You
-- cannot talk your way to full authority.
create or replace function public.attester_authority(
  p_user_id            uuid,
  p_concept_ids        uuid[],
  p_as_of              timestamptz default now(),
  p_include_relations  boolean default false,
  p_behav_min_sim      real default 0.60
)
returns table (
  concept_id      uuid,
  authority       real,
  evidence_kinds  text[],
  evidence_quote  text
)
language sql
stable
security definer
set search_path = pg_catalog, public, extensions
as $$
  with c as (
    select unnest(p_concept_ids) as concept_id
  ),
  claims as (
    select l.concept_id,
           uic.source_quote,
           uic.details,
           -- ANTI-GAMING (§7, and the reason this function takes a timestamp at all).
           -- Callers pass attestation.valid_from. A claim written AFTER the
           -- recommendation cannot back-date authority onto it.
           (uic.created_at < p_as_of) as counts,
           -- "Specific" = the quote carries detail beyond the label. 40 chars is the
           -- spec's line; it is a proxy for entity density and Tim owns it in T4.
           (length(coalesce(uic.source_quote, '')) >= 40
             and uic.source_quote is distinct from uic.label) as is_specific
    from public.claim_concept_links l
    join public.user_identity_claims uic on uic.id = l.claim_id
    where uic.user_id = p_user_id
      and uic.dismissed_at is null
      and uic.transient = false
      -- §2 says domain standing is "YOU demonstrably know this subject area". A claim
      -- with subject_kind <> 'self' is about the attester's father or child — their
      -- standing, not the attester's. Without this line "my dad grew up in Gaziantep"
      -- (66 chars, specific) scores the CALLER 0.35 and satisfies an explicit "must be
      -- from someone Turkish" requirement. Default off, argument exists so the product
      -- call (do parent/grandparent/household transmit knowledge?) can be reopened
      -- without a deploy.
      and (p_include_relations or uic.subject_kind = 'self')
  ),
  claim_agg as (
    select c.concept_id,
           coalesce(bool_or(cl.counts), false)                    as has_bare,
           coalesce(bool_or(cl.counts and cl.is_specific), false)  as has_specific,
           -- Corroboration counts EVIDENCE, not rows, and the difference is the whole
           -- tier. user_identity_claims is unique on (user_id, concept) where not
           -- dismissed, and the extractor deliberately ENRICHES an existing thread's
           -- details[] rather than inserting a second row. A row count alone would
           -- therefore sit at zero for almost every real user, and the 0.15 term would
           -- be dead code. Rows and details both count; the formula caps at 2 anyway.
           greatest(count(*) filter (where cl.counts) - 1, 0)
             + coalesce(sum(cardinality(cl.details)) filter (where cl.counts), 0)
                                                                  as corroborations,
           -- Longest specific quote. This is what P3 renders; the score never is.
           (array_agg(cl.source_quote order by length(cl.source_quote) desc)
              filter (where cl.counts and cl.is_specific))[1]      as best_quote
    from c
    left join claims cl on cl.concept_id = c.concept_id
    group by 1
  ),
  behav as (
    -- Hard to fake, which is the point of the 0.40 headroom: you have to have actually
    -- recommended things in the domain, or be a confirmed member of a place of that
    -- type. Both halves respect p_as_of.
    select c.concept_id,
           least(2, (
             -- CONTRACT V2 SWAP POINT. When subject + attestation land, this becomes
             --   select count(distinct a.subject_id)
             --   from attestation a join subject s on s.id = a.subject_id
             --   where a.attester = p_user_id and a.valid_to is null
             --     and a.valid_from < p_as_of
             --     and (1 - (s.embedding <=> ic.canonical_embedding)) >= p_behav_min_sim
             -- Nothing outside this subquery changes.
             select count(distinct s.id)
             from public.local_signals s
             where s.user_id = p_user_id
               and s.intent = 'tip_share'
               and s.created_at < p_as_of
               and s.embedding is not null
               and (1 - (s.embedding <=> ic.canonical_embedding)) >= p_behav_min_sim
           ) + (
             select least(1, count(*))
             from public.circle_affiliations ca
             where ca.user_id = p_user_id
               and ca.status = 'confirmed'
               and ca.dismissed_at is null
               and ca.created_at < p_as_of
               and ca.embedding is not null
               and (1 - (ca.embedding <=> ic.canonical_embedding)) >= p_behav_min_sim
           )) as n_behav
    from c
    join public.identity_concepts ic on ic.id = c.concept_id
  )
  select
    c.concept_id,
    -- Every boolean coalesced: see departure 1 at the top of this file.
    (least(0.60, 0.10 * coalesce(ca.has_bare, false)::int::real
                + 0.25 * coalesce(ca.has_specific, false)::int::real
                + 0.15 * least(coalesce(ca.corroborations, 0), 2))
     + least(0.40, 0.20 * coalesce(b.n_behav, 0)))::real            as authority,
    array_remove(array[
      case when coalesce(b.n_behav, 0) > 0         then 'behavioural'  end,
      case when coalesce(ca.corroborations, 0) > 0 then 'corroborated' end,
      case when coalesce(ca.has_specific, false)   then 'specific'     end,
      case when coalesce(ca.has_bare, false)       then 'stated'       end
    ], null)                                                        as evidence_kinds,
    ca.best_quote                                                   as evidence_quote
  from c
  left join claim_agg ca on ca.concept_id = c.concept_id
  left join behav     b  on b.concept_id  = c.concept_id;
$$;

comment on function public.attester_authority(uuid, uuid[], timestamptz, boolean, real) is
  'Domain standing of one attester on specific concepts, as of a moment (SPEC_RECOMMENDER_'
  'AUTHORITY §4/A1). Claims cap at 0.60; only behavioural evidence goes above. Never '
  'rendered as a number — callers render evidence_quote. Per concept, never global.';

revoke all on function public.attester_authority(uuid, uuid[], timestamptz, boolean, real)
  from public, anon;
grant execute on function public.attester_authority(uuid, uuid[], timestamptz, boolean, real)
  to authenticated, service_role;
