-- reco_aspect · canonical key matching
--
-- WHY THIS EXISTS
--
-- The whole value of aspect-level rating is the count: "4 of 8 people mentioned the
-- owner". If "the owner", "the guy who runs it" and "ownership" are three aspect_keys,
-- every count is 1 forever and the surface is worthless.
--
-- So before writing a new aspect for a subject, the worker asks: does this subject
-- already have an aspect that MEANS this? Above the similarity floor, reuse that key.
--
-- Deliberately scoped per subject. "Service" at a restaurant and "service" at a
-- mechanic are not the same aspect and must not be merged into a global vocabulary —
-- that is exactly the category-template failure this work exists to escape.
--
-- Matches on label_embedding ("label (key)"), which is the same text the worker probes
-- with. The content embedding ("label: answer") is Find's, and comparing a bare label to
-- it measured two different kinds of text. It also makes an aspect matchable from the
-- moment it is opened, not only once someone answers it.

create or replace function public.match_reco_aspect_key(
  p_subject_ref    uuid,
  p_embedding      extensions.vector(768),
  p_min_similarity real default 0.82
)
returns table (
  aspect_key   text,
  aspect_label text,
  similarity   real,
  n_people     bigint
)
language sql
stable
security definer
set search_path = pg_catalog, public, extensions
as $$
  -- One row per existing key for this subject, represented by its most-used label and
  -- the centroid of its label embeddings. Centroid, not nearest member: a single odd
  -- phrasing should not become the magnet that pulls unrelated aspects in.
  with keys as (
    select
      a.aspect_key,
      mode() within group (order by a.aspect_label) as aspect_label,
      count(distinct a.author_id)                   as n_people,
      avg(a.label_embedding)::extensions.vector(768) as centroid
    from public.reco_aspect a
    where a.subject_ref = p_subject_ref
      and a.label_embedding is not null
    group by a.aspect_key
  )
  select
    k.aspect_key,
    k.aspect_label,
    (1 - (k.centroid <=> p_embedding))::real as similarity,
    k.n_people
  from keys k
  where (1 - (k.centroid <=> p_embedding)) >= p_min_similarity
  order by k.centroid <=> p_embedding
  limit 1;
$$;

comment on function public.match_reco_aspect_key(uuid, extensions.vector, real) is
  'Nearest existing aspect_key for a subject, by label-embedding centroid. Returns nothing '
  'below p_min_similarity so the worker falls back to a new key. Per-subject by design: '
  'aspect vocabularies are local, not global. Service role only.';

-- Supabase grants execute on new public functions to anon/authenticated by default, so
-- revoking from PUBLIC alone leaves both of them able to call it.
revoke all on function public.match_reco_aspect_key(uuid, extensions.vector, real)
  from public, anon, authenticated;
grant execute on function public.match_reco_aspect_key(uuid, extensions.vector, real)
  to service_role;

-- No index of its own: the per-subject scan rides reco_aspect_subject_idx, and N per
-- subject is small enough that no vector index is wanted (the candidate set is one
-- subject's aspects, not a corpus).
