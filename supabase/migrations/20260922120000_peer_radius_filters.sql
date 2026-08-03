-- ============================================================================
-- PEER RADIUS, PART 2 — the remaining four peer-search paths
--
-- 20260921120000 converted the main find-peers flow. Four other entry points
-- still scope candidates with `home_block_id = <caller's block>`, so
-- "find people near me" behaved differently from "find runners near me":
--
--   find_peers_by_attr_filter              lexical token AND-match
--   find_peers_by_claim_filters            structured bucket+term filters
--   find_peers_by_claim_semantic           embedding fallback ("gymmer")
--   match_peers_by_claim_vectors_for_user  context-pack peer hints
--
-- (public.get_cluster_peers is also block-scoped but has no worker call site;
-- deliberately left alone rather than converted blind.)
--
-- Each gets a `_near` twin. Originals are untouched and keep their callers
-- until LANA_PEER_RADIUS_MATCH is on.
--
-- ALSO: extracts the candidate-set query into public.peers_within_radius() and
-- re-points match_peers_within_radius at it, so "who counts as near me" has one
-- definition instead of five copies that can drift apart.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- 0. peers_within_radius(user, radius) -> (peer_id, distance_meters)
--
-- Every other user with a resolvable coarse point inside the radius, blocked
-- users excluded. Block centroid preferred over ZIP centroid, matching
-- user_origin_point.
-- ----------------------------------------------------------------------------
create or replace function public.peers_within_radius(
  p_user_id uuid,
  p_radius_meters double precision default 8000
)
returns table(peer_id uuid, distance_meters double precision)
language plpgsql
stable
security definer
set search_path to 'pg_catalog', 'public', 'extensions'
as $function$
declare
  v_caller uuid := auth.uid();
  v_origin extensions.geography;
  v_radius double precision := greatest(100, least(coalesce(p_radius_meters, 8000), 200000));
begin
  if p_user_id is null then
    raise exception 'user_id_required' using errcode = 'P0001';
  end if;
  -- Without this, any authenticated user could ask who lives near anyone else.
  if v_caller is not null and v_caller <> p_user_id then
    raise exception 'forbidden' using errcode = 'P0001';
  end if;

  select o.origin into v_origin from public.user_origin_point(p_user_id) o;
  if v_origin is null then
    return;                      -- unlocatable caller: empty, never an error
  end if;

  return query
  select
    u.id,
    extensions.st_distance(
      coalesce(
        b.centroid,
        extensions.st_setsrid(extensions.st_makepoint(z.lng, z.lat), 4326)::extensions.geography
      ),
      v_origin
    )::double precision
  from public.users u
  left join public.blocks b
    on b.id = u.home_block_id and b.centroid is not null
  left join public.zip_centroids z
    on z.zip5 = public.normalize_zip5(u.home_zip)
  where u.id <> p_user_id
    and not public.lana_is_blocked(p_user_id, u.id)
    and coalesce(
          b.centroid,
          extensions.st_setsrid(extensions.st_makepoint(z.lng, z.lat), 4326)::extensions.geography
        ) is not null
    and extensions.st_dwithin(
          coalesce(
            b.centroid,
            extensions.st_setsrid(extensions.st_makepoint(z.lng, z.lat), 4326)::extensions.geography
          ),
          v_origin,
          v_radius
        );
end;
$function$;

comment on function public.peers_within_radius(uuid, double precision) is
  'Candidate set for every radius peer search: other users whose coarse point '
  '(block centroid, else ZIP centroid) is within p_radius_meters, blocked users '
  'excluded. One definition so the five search paths cannot drift apart.';

grant execute on function public.peers_within_radius(uuid, double precision)
  to authenticated, service_role;


-- ----------------------------------------------------------------------------
-- 0b. Re-point 20260921120000's matcher at the shared helper. Behaviour is
--     identical -- this only removes the duplicated candidate query.
-- ----------------------------------------------------------------------------
create or replace function public.match_peers_within_radius(
  p_user_id uuid,
  p_radius_meters double precision default 8000,
  p_limit int default 20,
  p_min_similarity real default 0.65,
  p_locale text default 'en'
)
returns table(
  peer_user_id uuid,
  nickname text,
  avatar_url text,
  similarity_score real,
  matching_peer_label text,
  matching_peer_concept text,
  has_exact_concept_match boolean,
  distance_meters double precision,
  distance_text text
)
language plpgsql
stable
security definer
set search_path to 'pg_catalog', 'public', 'extensions'
as $function$
declare
  v_caller uuid := auth.uid();
begin
  if p_user_id is null then
    raise exception 'user_id_required' using errcode = 'P0001';
  end if;
  if v_caller is not null and v_caller <> p_user_id then
    raise exception 'forbidden' using errcode = 'P0001';
  end if;

  return query
  with in_radius as (
    select r.peer_id, r.distance_meters as dist
    from public.peers_within_radius(p_user_id, p_radius_meters) r
  ),
  caller_claims as (
    select c.concept, c.label, c.embedding
    from public.user_identity_claims c
    where c.user_id = p_user_id
      and c.dismissed_at is null
      and c.disclosure = 'public'
      and c.embedding is not null
  ),
  peer_claim_pairs as (
    select
      pc.user_id as peer_id,
      pc.concept as peer_concept,
      pc.label as peer_label,
      (1 - (cc.embedding <=> pc.embedding))::real as sim
    from caller_claims cc
    join public.user_identity_claims pc
      on pc.user_id <> p_user_id
     and pc.dismissed_at is null
     and pc.disclosure = 'public'
     and pc.embedding is not null
    join in_radius ir on ir.peer_id = pc.user_id
    where (1 - (cc.embedding <=> pc.embedding)) >= p_min_similarity
  ),
  peer_best as (
    select distinct on (p.peer_id)
      p.peer_id, p.peer_concept, p.peer_label, p.sim
    from peer_claim_pairs p
    order by p.peer_id, p.sim desc
  ),
  exact_concepts as (
    select distinct pc.user_id as peer_id
    from public.user_identity_claims cc
    join public.user_identity_claims pc
      on pc.concept = cc.concept
     and pc.user_id <> p_user_id
     and pc.dismissed_at is null
     and pc.disclosure = 'public'
    join in_radius ir on ir.peer_id = pc.user_id
    where cc.user_id = p_user_id
      and cc.dismissed_at is null
      and cc.disclosure = 'public'
  )
  select
    pb.peer_id,
    u.nickname,
    u.profile_photo_url,
    pb.sim,
    pb.peer_label,
    pb.peer_concept,
    exists (select 1 from exact_concepts ec where ec.peer_id = pb.peer_id),
    ir.dist,
    public.humanize_distance_text(ir.dist, p_locale)
  from peer_best pb
  join in_radius ir on ir.peer_id = pb.peer_id
  join public.users u on u.id = pb.peer_id
  order by pb.sim desc, ir.dist asc, u.nickname asc nulls last
  limit greatest(1, least(coalesce(p_limit, 20), 50));
end;
$function$;


-- ----------------------------------------------------------------------------
-- 1. find_peers_by_attr_filter_near — lexical token AND-match, radius-scoped.
--    Caller is auth.uid(), as in the original: the worker calls this with the
--    user's JWT via call_rpc.
-- ----------------------------------------------------------------------------
create or replace function public.find_peers_by_attr_filter_near(
  p_filter_text text,
  p_radius_meters double precision default 8000,
  p_limit int default 5,
  p_locale text default 'en'
)
returns table(
  peer_user_id uuid,
  nickname text,
  avatar_url text,
  similarity_score real,
  matching_peer_label text,
  matching_peer_concept text,
  has_exact_concept_match boolean,
  distance_meters double precision,
  distance_text text
)
language plpgsql
security definer
set search_path to 'pg_catalog', 'public', 'extensions'
stable
as $function$
declare
  v_caller uuid := auth.uid();
  v_filter text := lower(trim(coalesce(p_filter_text, '')));
  v_tokens text[];
  v_need int;
begin
  if v_caller is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;
  if char_length(v_filter) < 2 then
    raise exception 'filter_too_short' using errcode = 'P0001';
  end if;

  -- Stop-word list kept byte-identical to the original: 'block' and 'near'
  -- are stripped here, so "find runners near me" tokenises to {runners}.
  select coalesce(array_agg(tok), '{}') into v_tokens
  from (
    select distinct tok
    from unnest(regexp_split_to_array(v_filter, '[^a-z0-9]+')) tok
    where length(tok) >= 2
      and tok not in (
        'find','a','an','the','with','on','my','block','which','are','of',
        'near','me','who','speak','for','to','looking','some','any','good',
        'know','want','please','can','you','help','moms','dads'
      )
  ) t;

  v_need := coalesce(array_length(v_tokens, 1), 0);
  if v_need = 0 then
    return;
  end if;

  return query
  with in_radius as (
    select r.peer_id, r.distance_meters as dist
    from public.peers_within_radius(v_caller, p_radius_meters) r
  ),
  token_hits as (
    select distinct c.user_id as peer_id, tok as hit_tok, c.label as match_label
    from unnest(v_tokens) tok
    cross join lateral (
      select c.*
      from public.user_identity_claims c
      join in_radius ir on ir.peer_id = c.user_id
      where c.user_id <> v_caller
        and c.dismissed_at is null
        and c.disclosure = 'public'
        and (
          lower(c.label) like '%' || tok || '%'
          or lower(c.concept) like '%' || tok || '%'
          or exists (
            select 1 from unnest(coalesce(c.synonyms, '{}')) s
            where lower(s) like '%' || tok || '%'
          )
        )
    ) c
  ),
  peer_ok as (
    select peer_id
    from token_hits
    group by peer_id
    having count(distinct hit_tok) >= v_need
  ),
  labels as (
    select th.peer_id,
           string_agg(distinct th.match_label, ' · ' order by th.match_label) as labels
    from token_hits th
    join peer_ok p on p.peer_id = th.peer_id
    group by th.peer_id
  )
  select
    p.peer_id,
    u.nickname,
    u.profile_photo_url,
    null::real,
    l.labels,
    null::text,
    false,
    ir.dist,
    public.humanize_distance_text(ir.dist, p_locale)
  from peer_ok p
  join public.users u on u.id = p.peer_id
  join labels l on l.peer_id = p.peer_id
  join in_radius ir on ir.peer_id = p.peer_id
  order by ir.dist asc, u.nickname asc nulls last
  limit greatest(1, least(coalesce(p_limit, 5), 20));
end;
$function$;

comment on function public.find_peers_by_attr_filter_near(text, double precision, int, text) is
  'Radius twin of find_peers_by_attr_filter. Lexical matches are binary, so '
  'nearest-first is the honest order here (no cosine exists to rank by).';

revoke all on function public.find_peers_by_attr_filter_near(text, double precision, int, text) from public, anon;
grant execute on function public.find_peers_by_attr_filter_near(text, double precision, int, text) to authenticated, service_role;


-- ----------------------------------------------------------------------------
-- 2. find_peers_by_claim_filters_near — structured bucket+term AND, radius-scoped.
-- ----------------------------------------------------------------------------
create or replace function public.find_peers_by_claim_filters_near(
  p_filters jsonb,
  p_radius_meters double precision default 8000,
  p_limit int default 5,
  p_locale text default 'en'
)
returns table(
  peer_user_id uuid,
  nickname text,
  avatar_url text,
  similarity_score real,
  matching_peer_label text,
  matching_peer_concept text,
  has_exact_concept_match boolean,
  distance_meters double precision,
  distance_text text
)
language plpgsql
security definer
set search_path to 'pg_catalog', 'public', 'extensions'
stable
as $function$
declare
  v_caller uuid := auth.uid();
  v_n_filters int;
begin
  if v_caller is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  v_n_filters := coalesce(jsonb_array_length(p_filters), 0);
  if v_n_filters < 1 then
    return;
  end if;

  return query
  with in_radius as (
    select r.peer_id, r.distance_meters as dist
    from public.peers_within_radius(v_caller, p_radius_meters) r
  ),
  filter_spec as (
    select
      nullif(f->>'bucket', '') as bucket,
      (
        select coalesce(array_agg(lower(t)), '{}')
        from jsonb_array_elements_text(f->'terms') t
        where length(trim(t)) >= 2
      ) as terms
    from jsonb_array_elements(p_filters) f
  ),
  valid_filters as (
    select * from filter_spec where coalesce(array_length(terms, 1), 0) > 0
  ),
  peer_ok as (
    select ir.peer_id
    from in_radius ir
    where (
      select count(*) from valid_filters vf
    ) = (
      select count(*)
      from valid_filters vf
      where exists (
        select 1
        from public.user_identity_claims c
        where c.user_id = ir.peer_id
          and c.dismissed_at is null
          and c.disclosure = 'public'
          and (vf.bucket is null or c.bucket = vf.bucket)
          and exists (
            select 1 from unnest(vf.terms) term
            where lower(c.concept) like '%' || term || '%'
               or lower(c.label) like '%' || term || '%'
               or term = any(
                 select lower(s) from unnest(coalesce(c.synonyms, '{}')) s
               )
          )
      )
    )
  ),
  peer_labels as (
    select
      po.peer_id,
      string_agg(distinct c.label, ' · ' order by c.label) as labels,
      min(c.concept) as concept
    from peer_ok po
    join public.user_identity_claims c on c.user_id = po.peer_id
      and c.dismissed_at is null
      and c.disclosure = 'public'
    where exists (
      select 1
      from valid_filters vf
      where (vf.bucket is null or c.bucket = vf.bucket)
        and exists (
          select 1 from unnest(vf.terms) term
          where lower(c.concept) like '%' || term || '%'
             or lower(c.label) like '%' || term || '%'
             or term = any(
               select lower(s) from unnest(coalesce(c.synonyms, '{}')) s
             )
        )
    )
    group by po.peer_id
  )
  select
    po.peer_id,
    u.nickname,
    u.profile_photo_url,
    null::real,
    coalesce(pl.labels, 'shared traits'),
    pl.concept,
    false,
    ir.dist,
    public.humanize_distance_text(ir.dist, p_locale)
  from peer_ok po
  join public.users u on u.id = po.peer_id
  join in_radius ir on ir.peer_id = po.peer_id
  left join peer_labels pl on pl.peer_id = po.peer_id
  order by ir.dist asc, u.nickname asc nulls last
  limit greatest(1, least(coalesce(p_limit, 5), 20));
end;
$function$;

comment on function public.find_peers_by_claim_filters_near(jsonb, double precision, int, text) is
  'Radius twin of find_peers_by_claim_filters. Same AND semantics and same '
  'no-fabricated-score rule; candidates come from distance, not block equality.';

revoke all on function public.find_peers_by_claim_filters_near(jsonb, double precision, int, text) from public, anon;
grant execute on function public.find_peers_by_claim_filters_near(jsonb, double precision, int, text) to authenticated, service_role;


-- ----------------------------------------------------------------------------
-- 3. find_peers_by_claim_semantic_near — embedding fallback, radius-scoped.
--    Similarity-first ordering: a cosine DID run here, so distance is only the
--    tiebreak (unlike the two lexical functions above).
-- ----------------------------------------------------------------------------
create or replace function public.find_peers_by_claim_semantic_near(
  p_query_embedding extensions.vector(768),
  p_radius_meters double precision default 8000,
  p_limit int default 5,
  p_min_similarity real default 0.55,
  p_locale text default 'en'
)
returns table(
  peer_user_id uuid,
  nickname text,
  avatar_url text,
  similarity_score real,
  matching_peer_label text,
  matching_peer_concept text,
  has_exact_concept_match boolean,
  distance_meters double precision,
  distance_text text
)
language plpgsql
security definer
set search_path to 'pg_catalog', 'public', 'extensions'
stable
as $function$
declare
  v_caller uuid := auth.uid();
begin
  if v_caller is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;
  if p_query_embedding is null then
    return;
  end if;

  return query
  with in_radius as (
    select r.peer_id, r.distance_meters as dist
    from public.peers_within_radius(v_caller, p_radius_meters) r
  ),
  peer_claims as (
    select
      pc.user_id as peer_id,
      pc.concept as peer_concept,
      pc.label as peer_label,
      (1 - (pc.embedding <=> p_query_embedding))::real as sim
    from public.user_identity_claims pc
    join in_radius ir on ir.peer_id = pc.user_id
    where pc.user_id <> v_caller
      and pc.dismissed_at is null
      and pc.disclosure = 'public'
      and pc.embedding is not null
      and (1 - (pc.embedding <=> p_query_embedding)) >= p_min_similarity
  ),
  peer_best as (
    select distinct on (p.peer_id)
      p.peer_id, p.peer_concept, p.peer_label, p.sim
    from peer_claims p
    order by p.peer_id, p.sim desc
  )
  select
    pb.peer_id,
    u.nickname,
    u.profile_photo_url,
    pb.sim,
    pb.peer_label,
    pb.peer_concept,
    -- The query text is the anchor, not a claim pair: never claim an exact match.
    false,
    ir.dist,
    public.humanize_distance_text(ir.dist, p_locale)
  from peer_best pb
  join public.users u on u.id = pb.peer_id
  join in_radius ir on ir.peer_id = pb.peer_id
  order by pb.sim desc, ir.dist asc, u.nickname asc nulls last
  limit greatest(1, least(coalesce(p_limit, 5), 20));
end;
$function$;

comment on function public.find_peers_by_claim_semantic_near(extensions.vector, double precision, int, real, text) is
  'Radius twin of find_peers_by_claim_semantic. Cosine-first order, distance as '
  'tiebreak -- a real similarity was computed, so it outranks proximity.';

revoke all on function public.find_peers_by_claim_semantic_near(extensions.vector, double precision, int, real, text) from public, anon;
grant execute on function public.find_peers_by_claim_semantic_near(extensions.vector, double precision, int, real, text) to authenticated, service_role;


-- ----------------------------------------------------------------------------
-- 4. match_peers_by_claim_vectors_for_user_near — context-pack peer hints.
--    Service-role path (explicit p_user_id), so it carries the IDOR guard.
--    Ranking rule preserved exactly: proven shared-claim COUNT first, then best
--    pair similarity -- distance only breaks a tie between equals.
-- ----------------------------------------------------------------------------
create or replace function public.match_peers_by_claim_vectors_for_user_near(
  p_user_id uuid,
  p_radius_meters double precision default 8000,
  p_limit int default 10,
  p_min_similarity real default 0.70,
  p_locale text default 'en'
)
returns table(
  peer_user_id uuid,
  nickname text,
  avatar_url text,
  similarity_score real,
  matching_peer_label text,
  matching_peer_concept text,
  has_exact_concept_match boolean,
  matching_my_label text,
  shared_labels text[],
  distance_meters double precision,
  distance_text text
)
language plpgsql
security definer
set search_path to 'pg_catalog', 'public', 'extensions'
stable
as $function$
declare
  v_caller uuid := auth.uid();
begin
  if p_user_id is null then
    raise exception 'user_id_required' using errcode = 'P0001';
  end if;
  if v_caller is not null and v_caller <> p_user_id then
    raise exception 'forbidden' using errcode = 'P0001';
  end if;

  return query
  with in_radius as (
    select r.peer_id, r.distance_meters as dist
    from public.peers_within_radius(p_user_id, p_radius_meters) r
  ),
  caller_claims as (
    select c.concept, c.label, c.bucket, c.embedding
    from public.user_identity_claims c
    where c.user_id = p_user_id
      and c.dismissed_at is null
      and c.disclosure = 'public'
  ),
  peer_claim_pairs as (
    select
      pc.user_id as peer_id,
      pc.concept as peer_concept,
      pc.label as peer_label,
      cc.label as my_label,
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
    join in_radius ir on ir.peer_id = pc.user_id
    where (
      cc.concept = pc.concept
      or (
        cc.embedding is not null
        and pc.embedding is not null
        and (1 - (cc.embedding <=> pc.embedding)) >= p_min_similarity
        -- Heritage/faith never cross-match on cosine alone (20260823120000).
        and not (
          coalesce(cc.bucket, 'general') in ('heritage', 'faith')
          and coalesce(pc.bucket, 'general') in ('heritage', 'faith')
          and cc.concept <> pc.concept
        )
      )
    )
  ),
  peer_best as (
    select distinct on (p.peer_id)
      p.peer_id, p.peer_concept, p.peer_label, p.my_label, p.exact_pair, p.sim
    from peer_claim_pairs p
    order by p.peer_id, p.exact_pair desc, p.sim desc
  ),
  peer_shared as (
    select s.peer_id, array_agg(s.peer_label order by s.best_sim desc) as shared_labels
    from (
      select p.peer_id, p.peer_label, max(p.sim) as best_sim
      from peer_claim_pairs p
      where p.exact_pair
      group by p.peer_id, p.peer_label
    ) s
    group by s.peer_id
  )
  select
    pb.peer_id,
    u.nickname,
    u.profile_photo_url,
    pb.sim,
    pb.peer_label,
    pb.peer_concept,
    pb.exact_pair,
    pb.my_label,
    coalesce(ps.shared_labels, '{}'),
    ir.dist,
    public.humanize_distance_text(ir.dist, p_locale)
  from peer_best pb
  join public.users u on u.id = pb.peer_id
  join in_radius ir on ir.peer_id = pb.peer_id
  left join peer_shared ps on ps.peer_id = pb.peer_id
  order by
    cardinality(coalesce(ps.shared_labels, '{}')) desc,
    pb.sim desc,
    ir.dist asc,
    u.nickname asc nulls last
  limit greatest(1, least(coalesce(p_limit, 10), 50));
end;
$function$;

comment on function public.match_peers_by_claim_vectors_for_user_near(uuid, double precision, int, real, text) is
  'Radius twin of match_peers_by_claim_vectors_for_user (context-pack hints). '
  'Proven shared-claim count still ranks first; distance is the last tiebreak.';

revoke all on function public.match_peers_by_claim_vectors_for_user_near(uuid, double precision, int, real, text) from public, anon;
grant execute on function public.match_peers_by_claim_vectors_for_user_near(uuid, double precision, int, real, text) to authenticated, service_role;


-- ============================================================================
-- ROLLBACK
-- ----------------------------------------------------------------------------
--   drop function if exists public.match_peers_by_claim_vectors_for_user_near(
--     uuid, double precision, int, real, text);
--   drop function if exists public.find_peers_by_claim_semantic_near(
--     extensions.vector, double precision, int, real, text);
--   drop function if exists public.find_peers_by_claim_filters_near(
--     jsonb, double precision, int, text);
--   drop function if exists public.find_peers_by_attr_filter_near(
--     text, double precision, int, text);
--
-- peers_within_radius cannot be dropped without first restoring
-- match_peers_within_radius to its 20260921120000 body, which inlined the
-- candidate query. The originals are untouched throughout, so reverting the
-- worker alone restores block-equality behaviour with no SQL change at all.
-- ============================================================================
