-- Optional free-text follow-up on a rating — the "tell us more" box the PWA reveals
-- after a thumbs-down. It rides the same lana_feedback row as the thumb: submitting
-- the box updates the row's comment, flipping the thumb without a new comment clears
-- it (a stale 👎 explanation must not survive onto a 👍), and un-thumbing deletes the
-- whole row, comment included.
alter table public.lana_feedback
  add column if not exists comment text
  check (comment is null or char_length(comment) <= 2000);

comment on column public.lana_feedback.comment is
  'Optional free-text the user attached to their rating (the PWA offers the box on a thumbs-down). Overwritten on every re-rate; null when the user rated without elaborating.';
