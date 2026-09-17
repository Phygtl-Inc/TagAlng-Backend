-- Close the loop: Lana raises the ask when the neighbor actually arrives.
--
-- The outreach email's "Share what you know" button opened the app into an ordinary chat.
-- Nothing there mentioned the ask, so the one person who agreed to help had to remember
-- what the mail said and type it unprompted — and from their side the button did nothing.
--
-- One column is all that is missing. The pending row already says who owes an answer; it
-- could not say whether Lana had already brought it up, and without that she would either
-- raise it once and forget, or raise it on every single session open until they answered,
-- which is nagging on behalf of a stranger.

alter table public.tip_ask_recipients
  add column if not exists surfaced_at timestamptz;

comment on column public.tip_ask_recipients.surfaced_at is
  'When Lana opened a session with this ask. Null = never raised in chat; set once, so an '
  'unanswered ask is mentioned a single time rather than every visit.';

-- The lookup is "my unanswered, unraised asks, newest first".
create index if not exists tip_ask_recipients_pending_surface_idx
  on public.tip_ask_recipients (recipient_user_id, created_at desc)
  where surfaced_at is null and status in ('queued', 'sent');
