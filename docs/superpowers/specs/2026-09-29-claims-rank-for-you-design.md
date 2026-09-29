# Claims rank results, "For you" explains why

2026-09-29 · approved in chat by Asjid.

## Problem

"can u find good italian restaurants?" returned cards that explain the PLACE ("reviewers
mention the crispy crust") but never the READER. The reader's claims were used only as hard
Google filters (dropping good places Google had not tagged), sometimes behind an extra
"pick an angle first" turn, and not at all for neighbour recommendations.

## Decisions

| Question | Decision |
|---|---|
| Role of claims | **Rank only, answer now.** The ask decides what is in the list; claims only order it. A claim never drops a place and never costs a turn. Only what the ASK says can filter. |
| Named claims | **Everyday only.** Diet, kids/family, pets, hobbies, how they go out may be named ("You've mentioned you eat out with your kids"). Faith, heritage and health are QUIET: they may reorder, but the line speaks about the place ("Reviewers note it's halal"). Private claims are never used. |
| Proof | A "For you" line needs a claim AND a quote that supports it, verified verbatim. A claim alone gets no line. |

## Reader claims — `app/reader_claims.py`

The reader's own non-dismissed, non-transient, non-private claims (household included —
"my kids"), each `{id, label, bucket, sayable}`. `sayable` is false for buckets `heritage`
and `faith`; the composer is additionally told never to name health, religion, ethnicity
or anything intimate. `claim_label` reaches the wire only when `sayable`.

## Google fallback

- `discovery_route._tip_seek_fallback_core`: personalizer CLAIM filters no longer filter or
  ask; they become refine chips after the answer. REQUEST filters ("kid-friendly steak")
  still filter, as before.
- A pool of 6 places (`ctx["google_place_pool"]`); `place_suggestions` stays 3 for old clients.
- `google_reco_cards`: details for the pool; the one model call also gets the reader's
  claims and returns `for_you: [{claim, line, quotes: [{review, excerpt}]}]` per place,
  grounded like the aspects. Order: places with a proven `for_you` first, then Google's
  order; top 3 shown.

## Neighbour recommendations — `reco_fit`

The page's one fit call also gets the reader's claims and each card's own words
(contributor descriptions + aspect quotes), and returns `for_you` per card, each quote
verified verbatim against that card's words. `finish_fit` then reorders the page: within the
same standing tier and group (circle / block / nearby), cards with a proven `for_you` move up.

## Wire

`RecoCardRow.for_you: [{line, claim_label, quotes: [str], review_quotes: [{text, author,
author_url}]}]`. PWA: a "For you" block first in "Why Lana sees a fit" (line + quote
carousel + "Based on what you've told me"), and Best fit sorts by it after standing.

## Testing

Unit + mutation (grounding, sayable gate, ranking, no-claims = unchanged, claim filters no
longer drop). Live: a vegetarian reader's "good restaurants"; "good steak places" stays all
steak; a heritage claim is never named; a reader with no claims sees today's result.
