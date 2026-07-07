-- Admin: read a user's rapport gaps with provenance, for the Lana inbox "Profile Extraction"
-- panel. Mirrors admin_get_lana_conversation (security definer + is_tagalng_admin gate) —
-- needed because rapport_gaps has RLS (owner-only select), so the admin can't read other
-- users' rows directly.
--
-- Each gap carries its "why": the source message that opened it (opened_from_message_id →
-- lana_messages) and the claim its answer produced (answer_claim_id → user_identity_claims).
-- LEFT joins, so gaps with no recorded source / no answer still return (nulls).

create or replace function public.admin_get_rapport_gaps(p_user_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = public
as $$
declare
  result jsonb;
begin
  if not public.is_tagalng_admin() then
    raise exception 'admin_forbidden' using errcode = '42501';
  end if;

  select coalesce(
    jsonb_agg(
      jsonb_build_object(
        'gap_id', g.gap_id,
        'question', g.question,
        'why_frame', g.why_frame,
        'parent_bucket', g.parent_bucket,
        'status', g.status,
        'unlock_score', g.unlock_score,
        'skipped_count', g.skipped_count,
        'opened_at', g.opened_at,
        'asked_at', g.asked_at,
        'answered_at', g.answered_at,
        'source_message', src.content,     -- the message that triggered this gap ("why")
        'answer_label', ac.label,          -- what the answer became, if answered
        'answer_concept', ac.concept
      )
      order by g.opened_at desc
    ),
    '[]'::jsonb
  )
  into result
  from public.rapport_gaps g
  left join public.lana_messages src on src.id = g.opened_from_message_id
  left join public.user_identity_claims ac on ac.id = g.answer_claim_id
  where g.user_id = p_user_id;

  return result;
end;
$$;

comment on function public.admin_get_rapport_gaps(uuid) is
  'Admin-only: rapport gaps for a user with source-message + answer-claim provenance. '
  'Powers the Lana inbox rapport panel. is_tagalng_admin gated.';

revoke all on function public.admin_get_rapport_gaps(uuid) from public, anon;
grant execute on function public.admin_get_rapport_gaps(uuid) to authenticated;
