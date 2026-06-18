-- Repair block-log display: drop stale low-quality swap rows; ensure get_my_block_log exposes peer signal text.

delete from public.match_notifications
where block_log_entry_id in (
  select id from public.block_log_entries
  where user_acted_at is null
    and match_type = 'inbound_for_my_seek'
    and (
      match_reasons is null
      or match_reasons = array['Same block neighbor']::text[]
      or exists (
        select 1 from unnest(coalesce(match_reasons, '{}'::text[])) r
        where r ilike '%you''re looking for%'
          and r not ilike '%offering%'
          and r not ilike '%matches your ask%'
      )
    )
);

delete from public.block_log_entries
where user_acted_at is null
  and match_type = 'inbound_for_my_seek'
  and (
    match_reasons is null
    or match_reasons = array['Same block neighbor']::text[]
    or exists (
      select 1 from unnest(coalesce(match_reasons, '{}'::text[])) r
      where r ilike '%you''re looking for%'
        and r not ilike '%offering%'
        and r not ilike '%matches your ask%'
    )
  );

comment on function public.get_my_block_log() is
  'Read-only pending block log rows with my/peer signal detail_text for display. Call refresh_my_signal_matches() first.';
