-- "See similar meets nearby" on the meet detail page (/meet/[id]).
-- Ranks the source event's cluster-mates by affinity first (shared cohort_tags),
-- then venue proximity, then time closeness — a tiered ORDER BY, not a blended
-- score: with a block holding a few dozen open events it stays predictable and
-- debuggable, and degrades to "closest upcoming meets" when nothing shares a tag
-- so the CTA never opens on an empty drawer.
-- Anon-callable like get_cluster_events/get_event_preview: the CTA shows on
-- shared links to signed-out guests.

create or replace function public.get_similar_events(
  p_event_id uuid,
  p_limit integer default 3,
  p_locale text default 'en'
)
returns table (
  id uuid,
  host_id uuid,
  title text,
  description text,
  starts_at timestamptz,
  ends_at timestamptz,
  location extensions.geography,
  venue_name text,
  cohort_tags text[],
  max_attendees integer,
  status text,
  shared_tags integer,
  meters_away double precision
)
language sql
security definer
set search_path = pg_catalog, public, extensions
stable
as $$
  with src as (
    select e.cluster_id, e.location, e.cohort_tags, e.starts_at
    from public.events e
    where e.id = p_event_id
  )
  select
    e.id,
    e.host_id,
    coalesce(e.title_translations->>p_locale, e.title) as title,
    coalesce(e.description_translations->>p_locale, e.description) as description,
    e.starts_at,
    e.ends_at,
    e.location,
    e.venue_name,
    e.cohort_tags,
    e.max_attendees,
    e.status,
    cardinality(array(
      select unnest(e.cohort_tags) intersect select unnest(src.cohort_tags)
    )) as shared_tags,
    st_distance(e.location, src.location) as meters_away
  from public.events e
  cross join src
  where e.cluster_id = src.cluster_id
    and e.id <> p_event_id
    and e.status = 'open'
    and e.starts_at > now()
  order by
    shared_tags desc,
    meters_away asc nulls last,
    abs(extract(epoch from (e.starts_at - src.starts_at))) asc
  limit greatest(1, least(coalesce(p_limit, 3), 10));
$$;

revoke execute on function public.get_similar_events(uuid, integer, text) from public;
grant execute on function public.get_similar_events(uuid, integer, text) to anon, authenticated;
