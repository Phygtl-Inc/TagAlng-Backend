-- Community invite links land on the community page · joins attributed to the invite
--
-- WHY (Asjid, 2026-10-09)
--   1. An invite minted for a community (/i/<token>, circle_invites.place_ref set) opened
--      a generic redeem card. The community already has a public page
--      (get.lana.help/{handle}, or {parent}/{chapter} for a chapter) — the invitee should
--      land there, and the client has to learn that link BEFORE sign-in, so the read is
--      anon-callable. It discloses only what the public page itself discloses: the place,
--      its link and its name. Never who sent the invite.
--   2. A redemption recorded that someone OPENED a link, not that the link produced a
--      member. /lana/circles/join now carries the invite token and the worker stamps the
--      redemption with joined_at / joined_place_ref, so "which invites actually grow a
--      community" is a query, not a guess.
--
-- WHAT
--   invite_destination(token) → {placeId, link, displayName} | null. Anon + authenticated.
--     Null unless the invite exists, is not revoked, carries a place_ref, and that place
--     resolves through the SAME public reads its page uses: resolve_chapter_handle
--     (20270131120000) for a chapter whose parent has a handle, else resolve_place_handle
--     (operator_verified + handle). The resolved placeId must be the invite's place, so a
--     retired alias that now points elsewhere never redirects anyone.
--   circle_invite_redemptions.joined_at / joined_place_ref: set by the worker
--     (circle_invites.attribute_join) when the invitee joins the invite's community.
--   community_invite_joins: service_role-only view of redemptions that produced a join,
--     for admin/analytics. No anon/authenticated grants.
--
-- DEPENDS ON 20270131120000 (resolve_chapter_handle, places.chapter_handle).
-- DEPLOY ORDER: this migration, then the worker. The worker's attribute_join writes the
-- two new columns; before they exist the write fails, is logged and swallowed (a join
-- never fails on attribution), so an early worker loses attribution, never joins.

-- ── 1 · redemption columns ──────────────────────────────────────────────────────

alter table public.circle_invite_redemptions
  add column if not exists joined_at        timestamptz,
  add column if not exists joined_place_ref uuid references public.places (id) on delete set null;

comment on column public.circle_invite_redemptions.joined_at is
  'When this invitee joined the invite''s community via /lana/circles/join with the token. '
  'First join wins; null = opened, never joined (from this link).';
comment on column public.circle_invite_redemptions.joined_place_ref is
  'The community joined — always the invite''s place_ref at the time of the join.';

create index if not exists circle_invite_redemptions_joined_idx
  on public.circle_invite_redemptions (invite_id)
  where joined_at is not null;

-- ── 2 · invite_destination · where /i/<token> should land ───────────────────────

create or replace function public.invite_destination(p_token text)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public
as $$
declare
  v_place_ref uuid;
  v_p         record;
  v_parent    text;
  v_j         jsonb;
begin
  select i.place_ref into v_place_ref
    from public.circle_invites i
   where i.token = btrim(coalesce(p_token, ''))
     and i.revoked_at is null;
  if v_place_ref is null then
    return null;
  end if;

  select p.id, p.handle, p.parent_place_ref, p.chapter_handle
    into v_p
    from public.places p where p.id = v_place_ref;
  if v_p.id is null then
    return null;
  end if;

  -- A chapter under a parent with a link: get.lana.help/{parent}/{chapter}.
  if v_p.parent_place_ref is not null and v_p.chapter_handle is not null then
    select pp.handle into v_parent from public.places pp where pp.id = v_p.parent_place_ref;
    if v_parent is not null then
      v_j := public.resolve_chapter_handle(v_parent, v_p.chapter_handle);
      if v_j is not null and (v_j->>'placeId')::uuid = v_p.id then
        return jsonb_build_object(
          'placeId',     v_p.id,
          'link',        v_j->>'link',
          'displayName', v_j->>'displayName');
      end if;
    end if;
  end if;

  -- A community with its own public page: get.lana.help/{handle}.
  if v_p.handle is not null then
    v_j := public.resolve_place_handle(v_p.handle);
    if v_j is not null and (v_j->>'placeId')::uuid = v_p.id then
      return jsonb_build_object(
        'placeId',     v_p.id,
        'link',        v_j->>'handle',
        'displayName', v_j->>'displayName');
    end if;
  end if;

  return null;
end;
$$;

-- revoke from public alone leaves anon's explicit default grant in place — name each role.
revoke all on function public.invite_destination(text) from public, anon, authenticated;
grant execute on function public.invite_destination(text) to anon, authenticated, service_role;

comment on function public.invite_destination(text) is
  'Where an invite link (/i/<token>) lands: {placeId, link, displayName} of the invite''s '
  'community when it has a public page (resolve_chapter_handle / resolve_place_handle), '
  'else null. Revoked or unlabeled invites → null. Never discloses the inviter.';

-- ── 3 · community_invite_joins · admin/analytics, service_role only ─────────────

create or replace view public.community_invite_joins
with (security_invoker = true)
as
select r.invite_id,
       i.token,
       i.owner_user_id    as inviter_user_id,
       r.user_id          as invitee_user_id,
       coalesce(r.joined_place_ref, i.place_ref) as place_ref,
       r.joined_at
  from public.circle_invite_redemptions r
  join public.circle_invites i on i.id = r.invite_id
 where r.joined_at is not null;

revoke all on table public.community_invite_joins from public, anon, authenticated;
grant select on table public.community_invite_joins to service_role;

comment on view public.community_invite_joins is
  'Invite redemptions that produced a join of the invite''s community (joined_at set by '
  'the worker''s attribute_join). service_role only — inviter/invitee ids are not public.';

-- ============================================================================
-- ROLLBACK
--   drop view if exists public.community_invite_joins;
--   drop function if exists public.invite_destination(text);
--   drop index if exists public.circle_invite_redemptions_joined_idx;
--   alter table public.circle_invite_redemptions
--     drop column if exists joined_place_ref,
--     drop column if exists joined_at;
--   Deploy the previous worker first (or accept logged, swallowed attribution failures).
--   Invite links fall back to the generic redeem card; joins are no longer attributed.
-- ============================================================================
