-- Revert the 20260625 send-gateway lock. We are pausing after feature #5 and deferring
-- the worker safety gateway (#7), so the worker is NOT deployed to mediate sends. Restore
-- the direct send_message RPC so the communication model (features 1-5) keeps working.
--
-- The #7 / #6 objects (lana_message_holds, worker_send_message, create_message_hold,
-- override_held_message, lana_inline_hints, create_lana_hint, etc.) remain in place but
-- DORMANT — nothing calls them until we deploy the worker and re-lock send_message later.

grant execute on function public.send_message(uuid, text, uuid, uuid) to authenticated;
