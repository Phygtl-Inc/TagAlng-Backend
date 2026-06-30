-- LANA Layer 3 · Latent Intent Engine · Phase 1 scaffolding (collect, don't surface).
-- Spec: LANA_LATENT_INTENT_ENGINE_v1 §4, §6. This migration ships the data substrate only;
-- the entity extractor, capability matcher, timing engine and ranker are app-side / later phases.
--
-- Deviations from the spec's §6 schema (intentional, see comments):
--   * vector(768) not vector(1536) -> reuse existing text-embedding-005 (vertex_embed), no new provider.
--   * hnsw + vector_cosine_ops not ivfflat -> matches repo convention (claims, inquiry_signals, facts).
--   * embedding columns nullable + partial index -> seed rows / firehose rows insert first, embed in background.
--   * latent_signals gains subject + attributes + block_id -> entity attribution ("my kid does karate"
--     vs "I do karate") and block-density analysis, which §6.1 omitted.
--   * entity_type left as free text (no CHECK) -> the entity-type vocabulary is open question §10-Q1
--     (Yunchao). Add a CHECK in a follow-up migration once the extractor's output contract is frozen.

-- ============================================================================
-- §1 · capability_index — "what Lana can do," made matchable (spec §4)
-- Reference data. One row per capability, sourced from the layer1 intent enum.
-- embedding backfilled by a one-off script (vertex_embed of `description`).
-- ============================================================================
create table if not exists public.capability_index (
  capability_id text primary key,                       -- e.g. 'looking.meet' (matches layer1_intents)
  capability_name text not null,
  description text not null,                             -- semantic text that gets embedded
  embedding extensions.vector(768),                     -- nullable; backfilled post-migration
  entity_triggers text[] not null default '{}',         -- coarse pre-filter, e.g. {activity,sport,hobby}
  identity_claim_triggers text[] not null default '{}', -- gate, e.g. {has_kid}
  required_state text[] not null default '{}',          -- gate, e.g. {phone_verified}
  surface_priority int not null default 5 check (surface_priority between 1 and 10),
  is_active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

comment on table public.capability_index is
  'Layer 3 capability catalog. Entities extracted from turns are cosine-matched against these rows.';

create index if not exists capability_index_embedding_hnsw_idx
  on public.capability_index
  using hnsw (embedding extensions.vector_cosine_ops)
  where embedding is not null;

create index if not exists capability_index_entity_triggers_idx
  on public.capability_index
  using gin (entity_triggers);

alter table public.capability_index enable row level security;

-- Reference config: readable by authenticated clients (like reason_codes), service-role writes only.
create policy capability_index_read_authenticated on public.capability_index
  for select to authenticated using (is_active);

create policy capability_index_no_client_write on public.capability_index
  for all to authenticated using (false) with check (false);

-- Seed the initial catalog from the layer1 intent enum. embedding stays null until backfilled.
insert into public.capability_index
  (capability_id, capability_name, description, entity_triggers, identity_claim_triggers, surface_priority)
values
  ('looking.meet', 'Find a meet or playgroup',
   'Find a mom-with-similar-kids meet, playgroup, or activity group',
   '{activity,sport,hobby,class,lesson,playgroup}', '{has_kid}', 8),
  ('looking.swap', 'Find a gear/clothes swap',
   'Find someone in your block willing to swap kids gear, clothes, or equipment',
   '{gear,equipment,clothes,item,outgrew,size,stroller,toys}', '{has_kid}', 7),
  ('looking.tip', 'Find a mom-tested recommendation',
   'Find a mom-tested recommendation for a service, professional, or place',
   '{recommendation,dentist,doctor,pediatrician,tutor,gym,restaurant,service}', '{}', 6),
  ('sharing.host', 'Host a meet or playgroup',
   'Host or organize a meet, playdate, or activity for nearby families',
   '{activity,playgroup,event,meetup,host}', '{has_kid}', 6),
  ('sharing.swap', 'Offer gear/clothes to swap',
   'Offer kids gear, clothes, or equipment for a neighbor to take or swap',
   '{gear,clothes,outgrew,item,toys,donate}', '{has_kid}', 5),
  ('sharing.tip', 'Share a recommendation',
   'Share a mom-tested tip or recommendation with the block',
   '{recommendation,tip,review,place,service}', '{}', 5),
  ('discovery.find_peers', 'Find similar neighbors',
   'Find nearby moms with similar life stage, kids, or interests',
   '{neighbor,mom,parent,friend}', '{}', 7),
  ('discovery.find_activities', 'Find local activities',
   'Find events, classes, or activities happening nearby',
   '{activity,event,class,festival,camp}', '{}', 6)
on conflict (capability_id) do nothing;

-- ============================================================================
-- §2 · latent_signals — the raw firehose of extracted entities (spec §6.1)
-- One row per entity per turn. Append-only, never deduped. Independent of intent.
-- This is the Phase 1 data-collection deliverable.
-- ============================================================================
create table if not exists public.latent_signals (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.users (id) on delete cascade,
  session_id uuid not null references public.lana_sessions (id) on delete cascade,
  turn_id uuid references public.lana_messages (id) on delete set null,
  block_id text references public.blocks (id) on delete set null,
  utterance_excerpt text not null,            -- raw phrase; the attribution safety-net
  entity_text text not null,                  -- 'karate'
  entity_type text not null,                  -- 'activity' (free text pending §10-Q1 vocab)
  subject text not null default 'unknown'     -- whose: resolves "my kid does karate" vs "I do karate"
    check (subject in ('self', 'child', 'partner', 'household', 'other', 'unknown')),
  attributes jsonb not null default '{}'::jsonb,  -- escape hatch: {child_age:5, frequency:'weekly', ...}
  entity_confidence real not null,
  embedding extensions.vector(768),           -- nullable; embedded in background like lana_messages
  extracted_at timestamptz not null default now(),
  constraint latent_signals_attributes_is_object check (jsonb_typeof(attributes) = 'object')
);

comment on table public.latent_signals is
  'Layer 3 raw entity stream. Every entity from every turn, regardless of classified intent. '
  'Distinct from user_identity_claims (deduped durable facts) and local_signals (explicit requests).';

create index if not exists latent_signals_user_time_idx
  on public.latent_signals (user_id, extracted_at desc);

create index if not exists latent_signals_entity_type_idx
  on public.latent_signals (entity_type, extracted_at desc);

create index if not exists latent_signals_block_type_idx
  on public.latent_signals (block_id, entity_type)
  where block_id is not null;

create index if not exists latent_signals_embedding_hnsw_idx
  on public.latent_signals
  using hnsw (embedding extensions.vector_cosine_ops)
  where embedding is not null;

alter table public.latent_signals enable row level security;

create policy latent_signals_select_own on public.latent_signals
  for select to authenticated
  using (user_id = auth.uid());

create policy latent_signals_no_client_write on public.latent_signals
  for all to authenticated using (false) with check (false);

-- ============================================================================
-- §3 · suggestion_queue — candidate suggestions, timing-aware (spec §6.2)
-- Populated in Phase 1; NOTHING reads it yet. Gives the v0.3 timing engine + ranker a target.
-- ============================================================================
create table if not exists public.suggestion_queue (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.users (id) on delete cascade,
  trigger_layer text not null check (trigger_layer in ('3a', '3b')),
  trigger_context jsonb not null default '{}'::jsonb,   -- {entity:'karate', utterance:'...', signal_id:...}
  capability_id text references public.capability_index (capability_id) on delete set null,
  suggestion_text text,                                 -- pre-templated Lana voice line (optional)
  confidence real not null,
  surface_when text not null
    check (surface_when in ('now', 'end_of_turn', 'next_break', 'next_session')),
  expires_at timestamptz,
  surfaced_at timestamptz,                              -- null until actually shown
  user_action text check (user_action in ('accepted', 'dismissed', 'ignored', 'converted')),
  created_at timestamptz not null default now(),
  constraint suggestion_queue_trigger_context_is_object check (jsonb_typeof(trigger_context) = 'object')
);

comment on table public.suggestion_queue is
  'Layer 3 output buffer. Phase 1 populates only; surfacing + ranking arrive in v0.3.';

-- Partial index: the future "what should I surface to this mom now?" query touches only live rows.
create index if not exists suggestion_queue_user_unsurfaced_idx
  on public.suggestion_queue (user_id, created_at desc)
  where surfaced_at is null;

alter table public.suggestion_queue enable row level security;

create policy suggestion_queue_select_own on public.suggestion_queue
  for select to authenticated
  using (user_id = auth.uid());

create policy suggestion_queue_no_client_write on public.suggestion_queue
  for all to authenticated using (false) with check (false);

-- ============================================================================
-- §4 · recommendation_impressions — the outcome ledger / ranker training data (spec §6.3)
-- feature_vector is the ranker's input features (NOT a text embedding) -> no similarity index.
-- ============================================================================
create table if not exists public.recommendation_impressions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.users (id) on delete cascade,
  suggestion_id uuid references public.suggestion_queue (id) on delete set null,
  surfaced_at timestamptz not null default now(),
  user_action text check (user_action in ('accepted', 'dismissed', 'ignored', 'converted')),
  action_at timestamptz,
  context jsonb not null default '{}'::jsonb,           -- session state, trust tier, time-of-day, ...
  ranker_version text,
  ranker_score real,
  feature_vector extensions.vector(64),                 -- ranker inputs for replay/training (not embedded)
  constraint recommendation_impressions_context_is_object check (jsonb_typeof(context) = 'object')
);

comment on table public.recommendation_impressions is
  'Layer 3 labeled training data. One row per surfaced suggestion; user_action is the label (§7).';

create index if not exists recommendation_impressions_user_time_idx
  on public.recommendation_impressions (user_id, surfaced_at desc);

create index if not exists recommendation_impressions_suggestion_idx
  on public.recommendation_impressions (suggestion_id);

alter table public.recommendation_impressions enable row level security;

-- Pure telemetry/training data: service-role only, no client access (like lana_audit_log).
create policy recommendation_impressions_no_client_access on public.recommendation_impressions
  for all to authenticated using (false) with check (false);
