-- Unified Lana chat session (no FE mode switch — routing decides per turn).

alter type public.lana_session_purpose add value if not exists 'lana';

comment on type public.lana_session_purpose is
  'profile_intake · event_draft · lana (unified concierge — internal routing per turn)';
