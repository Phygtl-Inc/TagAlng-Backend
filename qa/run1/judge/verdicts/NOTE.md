The three judge verdicts (full JSON: per-conversation scores, systemic_issues with quotes,
best_moments, verdict paragraphs) are preserved verbatim in the QA session transcript and
summarized in the report. The headline numbers used in the report:

- find paths (32 convs): 0/32 ended with a secured next step; scores 1-3 on goal_progress;
  systemic: out-of-coverage dead end (20/32), ZIP amnesia loop (7/12 metro),
  verify-wall misfire on refinements (6/20 fl), refinements never filter (~18/20),
  dirty event data (every list with results), off-persona copy ("just cricket").
- host path (16 convs): 0/16 posted; "yes that works" verbatim confirm-loop;
  one-shot drafts faithful on rich asks (host_0/1/8/11 draft_quality 4-5);
  date grounding bugs; ZIP-as-venue. (Cross-conversation "state bleed" finding
  excluded post-hoc as a harness artifact — see judge_host_path.md caveat.)
- edge cases (24 convs): canned opener fired on 15/24 incl. babysitter request,
  loneliness disclosure, "not creeps?" trust question; 4/4 direct questions unanswered;
  EN-only replies to ES/PT; guardrails held (no prompt leak, no fabricated recs,
  calm under insults); best turn = the overwhelmed-mom empathic reply.

Deterministic (non-LLM) metric definitions used alongside the judges are in
harness/analyze.mjs — e.g. verify_walls = assistant text contains "verify your email";
latency percentiles; leak regexes (raw slugs, "placeholder", null/undefined);
ny_bleed; zip_dead_ends; kid-name echo; language mismatch heuristics.
"Secured next step" for the future gate should use the server-side definition added in
PR feature/loop-telemetry-enums: north_star_secured(turn) ∈
{rsvp, published, intro_sent, signal_saved, waitlist} — one shared helper, tested.
