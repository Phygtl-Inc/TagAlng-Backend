-- Two AI-authored reads a recommendation card needs, and the places to keep them.
--
-- 1. THE BODY — what one neighbour actually recommended, in prose.
--
--    A capture stores a one-line `reco_description` ("great prices and they deliver") and
--    `reco_fields`, the answered steps ("Delivery: On time", "Best for: Families"). The
--    card has been rendering the one-liner and hiding the rest behind a disclosure, which
--    is a list of key-values, not something a neighbour reads. Screens 11/12 want the
--    DESCRIPTION block — the recommendation as prose, from that author's own answers.
--
--    Per CONTRIBUTION, never per subject, and that is the whole safety of it: the body is
--    composed from ONE person's words and answers, so it can never blend two neighbours
--    into a description neither of them wrote. On a merged card each voice keeps its own.
--
-- 2. THE SYNTHESIS — "Six say it freezes well, two say it went watery — both froze it
--    cooked."
--
--    The themes already say what most people mentioned and how many. What they cannot say
--    is that some neighbours DISAGREED, or what the dissenters had in common. That line is
--    the most useful thing on the card and the easiest to get wrong: it makes a claim
--    about a disagreement, with counts, about identifiable people.
--
--    So it is stored with its evidence, not as a sentence. `synthesis` carries the line
--    AND the signal_ids behind each side, so a reader (or a reviewer) can always get back
--    to who actually said what — and so the writer can be checked against the data rather
--    than trusted. A line whose counts do not match its cited rows is dropped, not shown.
--
-- Both live beside the themes, keyed the same way (subject/contribution + language +
-- a fingerprint of what they were built from), for the reason peer_rec_lines gives: a
-- reload must not cost a model call, and a NEW contribution must author a NEW read rather
-- than serve one that cannot know about it.

-- ── 1. The per-contribution body ──────────────────────────────────────────────────────
create table if not exists public.reco_contribution_bodies (
  signal_id  uuid not null references public.local_signals (id) on delete cascade,
  lang       text not null default 'en',
  -- Fingerprint of the description + answered steps it was written from. An author
  -- editing their recommendation authors a new body instead of keeping a stale one.
  basis_sig  text not null,
  body       text not null,
  created_at timestamptz not null default now(),
  primary key (signal_id, lang, basis_sig)
);

comment on table public.reco_contribution_bodies is
  'One neighbour''s recommendation as prose, composed from THEIR OWN description and '
  'answered steps. Per contribution and never per subject: a merged card shows several of '
  'these side by side rather than one blended description nobody wrote.';

alter table public.reco_contribution_bodies enable row level security;
-- No policies: the worker (service_role) is the only reader/writer, as with peer_rec_lines.
revoke all on public.reco_contribution_bodies from anon, authenticated;


-- ── 2. The synthesis, beside the themes it is built from ──────────────────────────────
alter table public.reco_subject_digests
  add column if not exists synthesis jsonb;

comment on column public.reco_subject_digests.synthesis is
  'The agreement/disagreement line, WITH its evidence: '
  '{line, majority:{label,n,signal_ids[]}, minority:{label,n,signal_ids[]}, shared_trait}. '
  'Stored structured because the line makes a counted claim about identifiable people — '
  'the counts are checked against the cited rows before it is shown, and a line that '
  'cannot be reconciled with its own evidence is dropped rather than rendered.';
