-- One comparison, one answer, forever: cache what the adjudicator decides about a PAIR.
--
-- The Stage 2 resolver sends the 0.80-0.93 band to a model. Measured on a real stack
-- (scripts/eval_reco_resolution.py --trials 5, 2026-09-23), the same pair at the same
-- score came back MERGED on 2 of 5 runs and REFUSED on the other 3 — identical input,
-- opposite verdicts, at temperature 0. Temperature 0 lowers variance; it does not remove
-- it.
--
-- Why that matters more here than in most LLM calls: the verdict is written into
-- local_signals.subject_ref at capture and then persisted. So it is sticky per tip and
-- arbitrary across tips — two neighbours recommending "Mike Plumber" and "Mike the
-- Plumber" share a card or do not depending on which way the coin landed the moment each
-- of them posted. A count a stranger is meant to trust cannot be decided by a coin.
--
-- The fix is the one peer_rec_lines and reco_subject_digests already use: decide once,
-- store it, and read it back. The model stays nondeterministic; the PRODUCT stops being.
--
-- WHAT IS KEYED. The question "are these two the same real-world thing" is symmetric, so
-- the two sides are sorted before fingerprinting — otherwise A-vs-B and B-vs-A would be
-- two cache entries free to disagree, which is the same bug wearing a hat. Category is
-- part of the key because it is part of the question: "Mike the Plumber"/plumber against
-- "Mike the Plumber"/barber is a different comparison from the same names in one trade.
--
-- 'unsure' IS CACHED TOO. Re-asking a question the model could not answer is how a
-- refusal quietly becomes a merge on the third try. If it could not tell once, that is
-- the answer until the inputs change.

create table if not exists public.reco_adjudications (
  pair_sig    text primary key,
  verdict     text not null check (verdict in ('same', 'different', 'unsure')),
  -- The compared sides, denormalised for auditing. Nothing reads them to make a decision;
  -- they exist so a human reviewing a bad merge can see what was actually asked.
  a_key       text not null,
  a_category  text,
  b_key       text not null,
  b_category  text,
  created_at  timestamptz not null default now()
);

comment on table public.reco_adjudications is
  'Cached verdicts for the Stage 2 adjudication band. The model is nondeterministic even '
  'at temperature 0 (measured: same pair merged 2/5 runs), and the verdict is persisted '
  'into subject_ref at capture — so without this, whether two recommendations share a '
  'card is decided by chance. Keyed on a SORTED pair fingerprint: the question is '
  'symmetric, and A-vs-B must never disagree with B-vs-A.';
comment on column public.reco_adjudications.verdict is
  '"unsure" is cached like any other answer — re-asking a question the model could not '
  'answer is how a refusal becomes a merge on the third attempt.';

alter table public.reco_adjudications enable row level security;
-- No policies: the worker (service_role) is the only reader/writer, as with peer_rec_lines
-- and reco_subject_digests. No client has any business reading how a merge was decided.
revoke all on public.reco_adjudications from anon, authenticated;
