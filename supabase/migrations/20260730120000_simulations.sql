-- Nightly LANA simulation runs + HITL review queue.
-- Each row is one persona×seed transcript scored by the judge; Tim reviews in
-- /admin/sims and marks SFT-eligible ones. Written by the nightly cron
-- (service role) and the admin UI.

create table if not exists public.simulations (
  id              uuid primary key default gen_random_uuid(),
  run_id          text not null unique,
  git_sha         text,
  persona_id      text not null,
  seed_label      text not null,
  bucket          text not null,
  transcript_json jsonb not null,
  scores_json     jsonb,
  weighted_score  real,
  judge_summary   text,
  hitl_status     text not null default 'pending',
  tim_verdict     text,
  tim_note        text,
  sft_eligible    boolean not null default false,
  sft_messages    jsonb,
  model_versions  jsonb,
  created_at      timestamptz default now()
);

create index if not exists simulations_weighted_score_idx on public.simulations (weighted_score asc);
create index if not exists simulations_hitl_status_idx on public.simulations (hitl_status);
create index if not exists simulations_sft_eligible_idx on public.simulations (sft_eligible) where sft_eligible = true;
