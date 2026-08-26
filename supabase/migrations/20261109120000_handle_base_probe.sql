-- Lana · handle derivation as its own testable function ─────────────────────────────
-- 20261108 folded the "do not repeat a locality the name already carries" rule inside
-- suggest_place_handle, where the only way to exercise it is to have a places row with
-- the right address. Dev kept returning orlando-public-library-orlando after that
-- migration applied, and with the rule buried in a row lookup there was no way to tell a
-- stale function body from a logic bug.
--
-- So the rule moves into _place_handle_base(name, locality): immutable, no table access,
-- callable with literals. It can be asserted directly and probed on a live database to
-- confirm which version is actually deployed.

create or replace function public._place_handle_base(
  p_name     text,
  p_locality text
)
returns text
language plpgsql
immutable
set search_path = pg_catalog, public
as $$
declare
  v_slug text := public.normalize_place_handle(p_name);
  v_loc  text := public.normalize_place_handle(p_locality);
  v_base text;
begin
  if v_slug is null then
    return null;
  end if;
  if v_loc is null then
    -- No locality to add: only usable if the name already reads as a handle.
    return case when v_slug ~ '-' then left(v_slug, 44) else null end;
  end if;

  -- Compare token by token rather than with a LIKE pattern: "orlando" must match the
  -- whole segment, so "north-orlando" does not count as already containing "orlando",
  -- and a locality that is a substring of one word ("york" in "yorkshire") does not
  -- suppress a suffix that is still needed.
  if v_slug ~ ('(^|-)' || v_loc || '($|-)') and v_slug ~ '-' then
    v_base := v_slug;
  else
    v_base := v_slug || '-' || v_loc;
  end if;

  return btrim(regexp_replace(left(v_base, 44), '-+', '-', 'g'), '-');
end;
$$;

comment on function public._place_handle_base(text, text) is
  'slug(name) plus slug(locality), skipping the locality when the name already carries it '
  'as a whole token. Immutable and table-free so it can be asserted with literals and '
  'probed against a live database to confirm which version is deployed.';

create or replace function public.suggest_place_handle(
  p_place_id      uuid,
  p_ignore_handle text default null
)
returns text
language plpgsql
stable
security definer
set search_path = pg_catalog, public
as $$
declare
  v_name  text;
  v_addr  text;
  v_zip   text;
  v_parts text[];
  v_loc   text;
  v_base  text;
  v_try   text;
  i       int := 1;
begin
  select p.name, p.address, p.zip
    into v_name, v_addr, v_zip
    from public.places p
   where p.id = p_place_id;

  if v_name is null then
    return null;
  end if;

  -- Google formatted address: "<street>, <city>, <region> <postal>, <country>".
  v_parts := string_to_array(coalesce(v_addr, ''), ',');
  if array_length(v_parts, 1) >= 3 then
    v_loc := v_parts[array_length(v_parts, 1) - 2];
  end if;

  v_base := public._place_handle_base(v_name, coalesce(v_loc, v_zip));

  if v_base is null or public._place_handle_shape_error(v_base) is not null then
    return null;
  end if;

  v_try := v_base;
  while v_try is distinct from p_ignore_handle
    and public._place_handle_taken(v_try) is not null loop
    i := i + 1;
    if i > 9 then
      return null;
    end if;
    v_try := v_base || '-' || i::text;
  end loop;

  return v_try;
end;
$$;

revoke execute on function public._place_handle_base(text, text) from public, anon;
revoke execute on function public.suggest_place_handle(uuid, text)
  from public, anon, authenticated;
