# Google fallback in the recommendation card template

2026-09-29 · approved in chat by Asjid (approach A).

## Problem

A recommendation ask with no neighbour recommendation ("find good turkish places near me")
falls back to Google Places and renders a bare list — name, address, "Open". Neighbour
results render as subject cards with "Why Lana sees a fit", evidence lines and quotes. The
fallback should use that same template, sourced from Google reviews, labelled
"From Google · not community-verified".

## Decisions

| Question | Decision |
|---|---|
| Evidence source | Google reviews via Place Details (≤5 per place). Not websites. |
| How many places | The top 3 Google places are enriched; the rest keep the plain row. |
| Time budget | None. Slow beats empty. Per-call network timeouts only (hung connections). |
| Counts | None. "Google reviewers mention the lamb adana" + quotes. Rating/total in the header. |
| Template | Approach A — the existing `RecoCard`, with `source: "google"`. |

## Backend

`app/google_reco_cards.py`, called once from the tip-seek Google fallback after
`ctx["google_place_suggestions"]` is set (plain, verified and widen paths).

1. First 3 Google places (rows from our own communities are excluded — they are ours).
2. Place Details in parallel, field mask `id,displayName,rating,userRatingCount,
   googleMapsUri,reviews`.
3. ONE model call for all places: per place a `fit_line` (reviews only) and up to 3
   `aspects` relevant to the ask, each `{label, headline, quotes: [{review, excerpt}]}`.
4. Grounding in code: an excerpt must appear verbatim in the review it cites, or it is
   dropped; an aspect with no surviving quote is dropped.
5. `ctx["google_reco_cards"]` — a separate key, because `reco_cards` is gated to
   peer-surface turns and the fallback is not one. Old clients ignore it and keep
   `place_suggestions`, which is unchanged.

Failure (Google error, model error, network timeout) → no Google cards that turn; the plain
list still renders. Review text is never written to the DB or the session (Google terms).

## Wire (`RecoCardRow` additions)

- `source: "neighbours" | "google"` (default neighbours).
- `google: {rating, rating_count, maps_url}`.
- `RecoAspectRow.review_quotes: [{text, author, author_url}]` — attribution Google requires;
  `quotes` (plain strings) stays for neighbour cards.
- Google cards: no contributors, vouch, cohorts, standing; `group_kind: "google"`;
  `fit_chips` = the ask's chips minus the recommender chip.

## PWA

- `google_reco_cards` → `RecoCards` in the recommendation flow, replacing the Google rows
  of `PlaceSuggestionsCard` for the places it covers.
- `RecoCard` with `source === "google"`: "From Google · not community-verified" header,
  "4.6★ · 1,240 Google reviews", fit panel as today, evidence quotes attributed to the
  reviewer with a link to Google, "Open in Google Maps". No vouched strip, no Nudge, no
  "Was this rec useful?".

## Testing

Unit (grounding, dropped aspects, failure → no cards, no DB write) + mutation; live Places +
live model on real asks; PWA in the browser against a fixture; full suites vs baseline.
