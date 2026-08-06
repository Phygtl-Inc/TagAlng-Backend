-- Circles grounding · the venue name the user actually said.
--
-- Bug (2026-08-03): a user said "I go to the gym at Fitness CF"; Lana's question
-- echoed the name correctly, but the chips under it were Crunch / EoS / Lake Nona
-- Performance Club — three unrelated gyms, offered as if they were the answer.
-- Cause: grounding fed the WHOLE raw phrase into Google text search, matched
-- nothing, and silently fell back to "any gym near this user".
--
-- The name now travels as its own field (extractor-provided, AI-resolved for rows
-- captured before this migration), so the search looks for the venue they named
-- and the results can be name-checked before being shown as matches.
--
-- NULL  = never resolved (grounding will resolve it once, from the phrase).
-- ''    = resolved: they named only an activity ("my gym") — do not ask the AI again.

alter table public.circle_affiliations
  add column if not exists place_name text;

comment on column public.circle_affiliations.place_name is
  'Venue name the user themselves said for this community ("Fitness CF"), the search text for grounding. '''' = resolved, no name given (they said only "my gym"); null = not resolved yet. Never inferred from nearby places — an invented name pins the user to the wrong spot.';

-- Chips are cached on the rapport gap the first time an ask is served
-- (rapport_gaps.grounding_options), so every user who already saw the wrong trio
-- would keep seeing it. Drop the cache for asks that are still open — the next
-- serve refetches through the fixed, name-gated search.
update public.rapport_gaps
   set grounding_options = null
 where affiliation_ref is not null
   and grounding_options is not null
   and status in ('open', 'asked');
