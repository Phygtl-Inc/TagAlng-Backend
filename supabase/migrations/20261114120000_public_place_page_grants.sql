-- Lana · the public community page reads its own two RPCs ─────────────────────────
-- /c/{handle} in the PWA is a signed-out surface. It was reaching resolve_place_handle
-- and place_claim_card with a service-role key, which means shipping a full-database
-- secret into a public web app for two functions that were written to be safe in the
-- open: resolve_place_handle returns only operator_verified places and no address,
-- claimant or member list; place_claim_card returns name, type, zip, blurb, the member
-- noun and a count, and never a member name.
--
-- So they go to anon and the page reads them with the publishable key, the way
-- meet-meta already reads a public meet.
grant execute on function public.resolve_place_handle(text) to anon, authenticated;
grant execute on function public.place_claim_card(uuid)     to anon, authenticated;

-- ponytail: place_claim_card is keyed by uuid, so anon can card any place it can name a
-- uuid for, verified or not. Contents are non-private and uuids are unguessable; gate on
-- governance_state if a place's existence ever becomes sensitive.
comment on function public.place_claim_card(uuid) is
  'Community card: place, member noun/emoji, description, and the member count. No '
  'member names, ever. Readable by anon — it backs the public /c/{handle} page as well '
  'as the operator claim flow.';
