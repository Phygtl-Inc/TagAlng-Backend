-- Swap matches must share item words (bicycle≠boots). Base 0.72 matched everything on block.

create or replace function public._signal_match_strength(
  p_my_category text,
  p_my_detail text,
  p_peer_category text,
  p_peer_detail text
)
returns real
language plpgsql
immutable
as $$
declare
  v_strength real := 0.0;
  v_word text;
  v_overlap int := 0;
  v_stop text[] := array[
    'looking', 'for', 'have', 'want', 'wanna', 'swap', 'borrow', 'offer',
    'someone', 'good', 'know', 'block', 'neighbor', 'kids', 'kid', 'child',
    'the', 'and', 'with', 'from', 'that', 'this', 'your', 'my', 'our', 'are',
    'you', 'what', 'when', 'size', 'stage', 'also', 'need', 'like', 'just',
    'give', 'away', 'free', 'home', 'help', 'near', 'nearby'
  ];
begin
  if p_my_detail is not null and p_peer_detail is not null then
    foreach v_word in array regexp_split_to_array(lower(p_my_detail), '[^a-z0-9]+') loop
      if length(v_word) < 4 then
        continue;
      end if;
      if v_word = any (v_stop) then
        continue;
      end if;
      if position(v_word in lower(p_peer_detail)) > 0 then
        v_overlap := v_overlap + 1;
      end if;
    end loop;
  end if;

  if v_overlap > 0 then
    v_strength := 0.68 + least(v_overlap * 0.08, 0.27);
  end if;

  if p_my_category is not null
     and p_peer_category is not null
     and lower(trim(p_my_category)) = lower(trim(p_peer_category)) then
    v_strength := greatest(v_strength, 0.72);
  end if;

  return least(v_strength, 0.95);
end;
$$;

update public.reason_codes
set template = 'A neighbor is looking for: {peer_detail}'
where code = 'swap_seek_matches_offer';

-- Drop stale auto-matches built with the loose 0.72 floor; refresh on next block-log read.
delete from public.match_notifications
where block_log_entry_id in (
  select id from public.block_log_entries where user_acted_at is null
);

delete from public.block_log_entries
where user_acted_at is null;
