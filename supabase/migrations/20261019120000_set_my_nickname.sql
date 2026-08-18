-- set_my_nickname — the write path for the OTHER pencil on the profile head
-- (backend asks §20 / issues #86).
--
-- 20261016 gave the public handle a setter; the real name it sits under had none, so the
-- client was writing users.nickname directly and could not tell "RLS refused" from "no
-- such row". This is the same SECURITY-boundary shape as set_my_handle so both pencils
-- fail the same way.
--
-- THE RULE, stated once so the client can mirror it: 1–30 characters after trimming.
-- 30 is not new — the worker has always truncated at 30 on the extraction path
-- (vertex_extract, claims_persist), so a longer name was already being cut silently.
-- Anything empty after the trim is nickname_invalid; so is anything over 30, rather
-- than a silent truncation.
--
-- NOT rate limited, deliberately: a rename costs nothing (no uniqueness, no index, no
-- notification) and the name is what Lana calls her, so getting it right matters more
-- than throttling. If a cooldown ever lands here it must raise
-- nickname_rename_too_soon:<seconds> so the UI can say WHEN, not just no.
--
-- ROLLBACK: drop function if exists public.set_my_nickname(text);

create or replace function public.set_my_nickname(p_nickname text)
returns jsonb
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_nickname text := btrim(coalesce(p_nickname, ''));
begin
  if auth.uid() is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;
  if char_length(v_nickname) < 1 or char_length(v_nickname) > 30 then
    raise exception 'nickname_invalid' using errcode = 'P0001';
  end if;

  update public.users set nickname = v_nickname where id = auth.uid();
  -- security invoker: the RLS update policy is the guard, so no row means refused.
  if not found then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;
  return jsonb_build_object('nickname', v_nickname);
end;
$$;

comment on function public.set_my_nickname(text) is
  'Rename the caller''s real first name — the one Lana speaks and neighbours see. '
  '1-30 characters after trimming, not unique. Raises nickname_invalid. Twin of '
  'set_my_handle, which sets the public @handle instead.';

revoke all on function public.set_my_nickname(text) from public, anon;
grant execute on function public.set_my_nickname(text) to authenticated;
