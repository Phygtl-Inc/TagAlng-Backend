-- Share on a digital (creator) community made no link.
--
-- 20261207120000 added 'creator' to places.place_type and circle_affiliations.circle_type,
-- but there is a third copy of the same list: circle_invites.circle_type (20260906120000).
-- mint_invite copies the caller's affiliation circle_type onto the invite row, so every
-- invite for a creator community violated circle_invites_circle_type_check, the mint 500'd,
-- and the share sheet showed "Couldn't make a link just now".
--
-- Same ten values plus 'creator', matching the other two lists.

alter table public.circle_invites
  drop constraint if exists circle_invites_circle_type_check;
alter table public.circle_invites
  add constraint circle_invites_circle_type_check check (
    circle_type is null or circle_type in (
      'school','faith','fitness','kids_activity','neighborhood',
      'hobby','support','heritage','friends','other','creator'
    )
  );
