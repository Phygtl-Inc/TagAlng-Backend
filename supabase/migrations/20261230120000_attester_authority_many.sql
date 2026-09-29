-- Recommender authority · P1 read path: public-only standing, many attesters per call.
--
-- app/reco_authority.py ranks a results page by the standing of the people behind it
-- ("a barber recommended by someone from Spain") and shows their own words as the reason.
-- Two changes make that safe and cheap:
--
-- 1. attester_authority() gains p_public_only (default false). A results page is a public
--    surface: it may only rank by, or quote, claims their owner shared publicly. The
--    function is otherwise byte-for-byte the 20261209120000 body. The old 5-argument
--    signature is dropped rather than overloaded — PostgREST cannot choose between two
--    overloads whose defaults cover the same named call. Every caller passes named
--    arguments (app/authority.py), so none of them changes.
--
-- 2. attester_authority_many() — the same function applied row by row, not a second
--    implementation, so the scoring, the coalesce guards (least(0.60, NULL) = 0.60) and the
--    self-only rule stay in one place. Always public-only. p_as_of is per row, parallel to
--    p_user_ids: each recommendation is scored as of the moment it was made, so a claim
--    written afterwards cannot back-date standing onto it. `idx` is the 1-based position
--    in p_user_ids, so the caller maps rows back even when one attester appears twice.

drop function if exists public.attester_authority(uuid, uuid[], timestamptz, boolean, real);

create or replace function public.attester_authority(
  p_user_id            uuid,
  p_concept_ids        uuid[],
  p_as_of              timestamptz default now(),
  p_include_relations  boolean default false,
  p_behav_min_sim      real default 0.60,
  p_public_only        boolean default false
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
      -- A claim is standing we may SHOW or RANK BY in front of other people only when its
      -- owner shared it publicly (reco_cohort's rule: private claims never form a cohort).
      -- Ranking a "from someone from Spain" page by a private heritage claim discloses it
      -- just as surely as printing the quote. Default off so the directed ask, which picks
      -- recipients privately, keeps its behaviour.
      and (not p_public_only or uic.disclosure = 'public')
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

comment on function public.attester_authority(uuid, uuid[], timestamptz, boolean, real, boolean) is
  'Domain standing of one attester on specific concepts, as of a moment (SPEC_RECOMMENDER_'
  'AUTHORITY §4/A1). Claims cap at 0.60; only behavioural evidence goes above. '
  'p_public_only restricts claims to disclosure = public (any surface shown to others). '
  'Never rendered as a number — callers render evidence_quote. Per concept, never global.';

revoke all on function public.attester_authority(uuid, uuid[], timestamptz, boolean, real, boolean)
  from public, anon;
grant execute on function public.attester_authority(uuid, uuid[], timestamptz, boolean, real, boolean)
  to authenticated, service_role;

create or replace function public.attester_authority_many(
  p_user_ids    uuid[],
  p_as_of       timestamptz[],
  p_concept_ids uuid[]
)
returns table (
  idx             int,
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
  select u.idx::int, a.concept_id, a.authority, a.evidence_kinds, a.evidence_quote
  from unnest(p_user_ids, p_as_of) with ordinality as u(user_id, as_of, idx)
  cross join lateral public.attester_authority(
    p_user_id     => u.user_id,
    p_concept_ids => p_concept_ids,
    p_as_of       => coalesce(u.as_of, now()),
    p_public_only => true
  ) a
  where u.user_id is not null
    and cardinality(p_concept_ids) > 0;
$$;

comment on function public.attester_authority_many(uuid[], timestamptz[], uuid[]) is
  'attester_authority(p_public_only => true) for many (attester, moment) pairs in one call; '
  'idx is the 1-based position in p_user_ids. Service role only — the score is internal.';

-- Narrower than attester_authority (which authenticated may call): this is a server-side
-- batch over OTHER people's standing, and no client has a reason to run it.
revoke all on function public.attester_authority_many(uuid[], timestamptz[], uuid[])
  from public, anon, authenticated;
grant execute on function public.attester_authority_many(uuid[], timestamptz[], uuid[])
  to service_role;
