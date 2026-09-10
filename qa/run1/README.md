# Lana QA run #1 · raw materials bundle
Tested 2026-07-08 against production get.lana.help (worker: tagalng-lana-worker on Cloud Run).
Run in-house by Tommaso's AI QA agent (Claude, agentic session on Tommaso's machine) — no external firm.
This bundle IS the raw data; nothing lives in scenarios.json or the simulations table because the
harness was standalone, driving the prod API directly.

## Contents
- harness/sim.mjs — THE SCENARIO SOURCE OF TRUTH: all 72 scenarios with exact opening lines,
  personas, turn plans, intent_hints, the find_fl ZIP list (32827/32832/32812/34786/32801),
  metro ZIPs (10025/78704/60614/94110/98115/02139), all 16 hosting phrasings verbatim
  ("yes that works", "MWF 7am before preschool dropoff", …), and all 24 edge scenarios.
  Buckets: find_fl_0..19, find_metro_0..11, host_0..15, edge_* (named).
- harness/sim2.mjs — the rerun variant (adds signup throttling + pooled-account fallback after
  Supabase's ~30/hr/IP anon-signup limit). READ THE CAVEAT below before reusing pooling.
- harness/probe.mjs — minimal API contract probe (anon signup → session → SSE parse).
- harness/analyze.mjs — deterministic metric definitions (verify-wall, leaks, latency, loops).
- harness/prep_judge_batches.mjs — how transcripts were compacted for the judges.
- transcripts/ — all 72 conversation JSONs (+ _summary.json, _analysis.json). Each turn carries:
  sent text, intent_hint, HTTP status, latency ms, assistant_message, routing
  {outcome, intent_class, confidence, tool_called, capture_fired}, ui_actions labels,
  activity_previews, event_draft, peer_matches count, joint_moment, signal_saved,
  identity_profile excerpt, requires_phone_verification, leak flags, SSE status labels.
  This is the full 233-turn corpus with per-turn routing metadata.
- judge/*.md — the exact compacted transcript batches the judges read.
- judge/prompts/ — the three judge prompts VERBATIM (rubrics: goal_progress/warmth/trust;
  draft_quality/flow/warmth; handling/safety_trust/warmth) incl. post-hoc caveats.
- judge/verdicts/NOTE.md — headline verdict numbers + where full verdicts live.

## Known harness caveats (bake these into the regression gate)
1. SESSION POOLING ARTIFACT: after the anon-signup rate limit, later scenarios shared accounts —
   and POST /lana/sessions resumes the active session per user, so pooled conversations
   interleaved into shared sessions (sessions listed in each transcript's "session" field;
   34 scenarios have clean dedicated sessions: all 32 find_* + host_0 + host_3).
   Fix for the gate: one account per scenario, or {"fresh": true} on session create
   (added in PR feature/session-scoped-drafts).
2. Host sims stop at draft stage by design (never publish to prod). Use is_test accounts/events
   (PR feature/event-data-purge) or a staging worker for full-loop coverage.
3. Conversation-level sim latency ≠ user-perceived latency once SSE deltas land
   (PR feature/sse-token-streaming) — keep measuring time-to-first-delta separately.

## Suggested gate assertions (from the report, with baselines)
canned-opener rate ≤2/24 (was 15/24) · verify-wall ≤5% of turns (was 17%) ·
host completion ≥14/16 (was 0/16) · zero dirty-data patterns · routing.outcome never 1 char ·
ES/PT replies language-match (was 0/2) · p90 turn latency <8s (was 13.2s) ·
find_metro: zero "try 32827", zero ZIP re-asks, waitlist join offered.
The 20 draft PRs on TagAlng-Backend/tagalng-pwa each carry a "post-deploy" test-plan checkbox
mapping to one of these.

## Production residue from run #1 (cleanup owner needed)
- 1 published meet: "Pre-K Playground Meetup" Fri 2026-07-10 9:30 AM, Laureate Park Zipline
  Playground, host t+lanaqa1@phygtl.com (visible to real 32827 users).
- 1 RSVP request to "Playdate at the park" from the same account.
- ~45 anonymous auth users + their sessions/signals (created 2026-07-08 ~00:35–01:30 UTC).
The feature/event-data-purge PR's migration quarantines the meet via the +qa email pattern
(is_test=true) and hard-deletes the pre-existing junk archetypes; the anon users can be bulk
deleted in Supabase (auth.users where is_anonymous and created_at in that window). Recommend
running that migration BEFORE adding new simulated traffic, and using +qa-suffixed accounts
going forward so the fence catches them automatically.
