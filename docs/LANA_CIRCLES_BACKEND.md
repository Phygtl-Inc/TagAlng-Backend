# Lana · Circles + Place Profile · Backend (Phase 1)

Implements the backend of `LANA_CIRCLES_ZIP_MASTER_v1` + the Place Profile extension
(place as a canonical entity). **Scope note:** the onion matcher (§C) is owned by a
separate developer and is NOT in this drop — everything else backend is. The word
"circle" is internal-only (§A.4 M7): user-facing copy names the place, never the term.
Persona-neutral throughout: the specs say "mom", the product is for any user.

## A community's place is MANDATORY (2026-07-28 product decision)

Supersedes the "grounding optional" stance below wherever they conflict. A
**community** — anything user-visible, matchable, or counted — is a
`circle_affiliations` row with `status='confirmed'`, and confirmation requires a
canonical place (DB check `circle_affiliations_confirmed_has_place`, migration
`20260916`). Consequences:

- `/lana/circles/add` **requires** `google_place_id` (400 `place_required`
  otherwise) and creates the community grounded + confirmed in one step.
- `/lana/circles/mine` returns **grounded rows only**. Ungrounded rows are
  internal grounding candidates — chat-captured mentions and invite
  self-confirms — that surface exclusively through Lana's "which spot is it?"
  ask, never as communities. (FE note: the Communities panel's "pick the spot"
  rows stop appearing; the panel shows real communities only.)
- Chat capture therefore never silently creates a community: a mention parks a
  candidate, Lana asks for the location (grounding ask), and only the user's
  answer creates the community — with its place, announced.
- The grounding confirm **always announces the save** ("{place} is saved to your
  communities now") in every register variant — see the confirm table below.

## Deploy ordering (matters)

1. `supabase/migrations/20260906120000_circles_places_phase_a.sql` — additive only
   (tables `places`, `place_features`, `circle_affiliations`, `zip_unlock`,
   `circle_invites`, `circle_invite_redemptions`; columns on `users`,
   `user_identity_claims`, `rapport_gaps`, `events`; `recount_zip_unlock()` fn;
   events→places backfill). Nothing reads it until the worker deploys.
2. `supabase/migrations/20260907120000_rapport_circle_grounding.sql` — additive
   (`rapport_gaps.affiliation_ref` + `rapport_gaps.grounding_options`; see the
   grounding-questions section below).
3. Worker deploy (all changes below). The worker reads `rapport_gaps.place_ref`
   and `affiliation_ref`, so **migrations first**.

All new tables have RLS enabled with **no client policies** — PostgREST hands out
nothing; every read goes through the worker. That is the §F disclosure guarantee
at the schema layer.

## What happens without any FE change

- **Capture (§H.1):** the per-turn extractor now also emits `circle_candidates` +
  `place_feature_candidates`; they persist as `status='suggested'` affiliations /
  parked feature notes. Never interrupts a turn.
- **§4.3 enrichment:** grounding a place queues one AI-authored question on the
  existing "By the way…" tile ("What do you enjoy most at {place}?"); the answer
  becomes a place-tagged identity claim (`user_identity_claims.place_ref`) via the
  existing record-answer path. Zero FE work.
- **Event anchoring:** publish now stamps `events.place_ref` (background, dual-write
  with legacy `events.place_id` text; historical events backfilled by the migration).

## Grounding questions on the rapport tile (conversion step of the funnel)

A suggested affiliation with no `place_ref` is invisible to the onion matcher —
only confirmed + grounded rows match. The "By the way…" tile is the surface that
reliably reaches every user, so each ungrounded affiliation now opens ONE tile
question ("You mentioned a gym — which spot is it?"), interleaved with normal
rapport questions. All backend; the tile chips are the only optional FE upgrade.

- **Synthesis** (`circles_flow.ensure_grounding_gaps`): runs after circle capture
  (fresh mentions get asked while they're warm) and inside the tile's buffer
  refill. Capped at `LANA_CIRCLE_GAP_MAX_OPEN` (default 2) open grounding asks;
  keyed `ground:<affiliation_id>` so an asked/answered/skipped affiliation never
  re-opens. Question + teaser are AI-authored (lingo-clean), template fallback.
- **Cadence** (`rapport_ranker`): at most one grounding ask per
  `LANA_RAPPORT_CIRCLE_EVERY_N` (default 3) tile questions — suppress-only; with
  nothing else open the grounding ask still serves. Skip decay + 3-skip expiry
  apply unchanged (a skipped question sinks like any other).
- **Serve payload** (FE contract, additive): a grounding ask returned by
  `/lana/rapport/next-ask` also carries
  `kind: "place_grounding"`, `affiliation_id`, and
  `options: [{label, address, google_place_id, send}]` (2-3 nearby places,
  fetched from Google once and cached on the row). A chip tap should POST
  `/lana/circles/ground {affiliation_id, google_place_id}` — one-tap grounding.
  Free text keeps working with **zero FE change** (below). `options` may be `[]`.
- **Answer path** (chat, zero FE): a free-text answer runs a Places search with
  the user's own words and Lana replies with 2-3 confirmation chips through the
  existing rapport-concierge options; the tap (or typing the name) grounds via
  `ground_affiliation`, which also queues the §4.3 enrichment question. NEVER
  auto-grounds from a fuzzy text match — a wrong canonical place silently
  attached to a user is the §F trust failure. Un-matchable answers persist onto
  `circle_affiliations.detail`; "neither"/abandon closes warmly keeping their
  words; a confident pivot releases to normal routing like every rapport turn.
- `/lana/rapport/record-answer` on a grounding gap: matches a cached option →
  grounds; otherwise stores the text as detail. Never feeds the claims extractor
  (a place name is not an identity fact); returns `{ok, grounded}`.

### Grounding confirm register (the reply after a place is pinned)

The confirm is a **bridge, never a bare acknowledgement**
(LANA_RAPPORT_BRIDGE_SPEC_v1 §1/AC-1): one warm sentence confirming the place,
then exactly ONE state-aware offer chip. Implemented in
`circles_flow.ground_and_confirm`; chat path only (the tile endpoint returns
`{ok, grounded}` and renders natively).

| State (real reads, no extra LLM call) | Reply shape | Offer chip |
|---|---|---|
| ≥1 other confirmed member at the place | "Done — {place} is saved to your communities now. N of your neighbors call it their spot too — want an intro?" | `find_neighbors` (bridge rule 4 — intro only on a REAL count) |
| nobody else confirmed yet (default) | "Done — {place} is saved to your communities now. Want to set up a {topic} get-together there you can share with your group?" | `host_meet` (rule 5/6 — create+invite is always-on, §D.2) |
| offer already made this session | "Done — {place} is saved to your communities now." | none (annoyance guard) |

Every variant states the save — grounding is the moment the community is created
(place mandatory), and a community must never be created silently.

Hard register rules: never "on my radar" / "noted in my system" (vague promises);
never claim people are waiting when the count is 0; approved forward-looking idiom
is "I'll keep an ear out" (lingo constitution's own example). Accept/decline rides
the existing rapport offer rails — a tap or typed "sure" dispatches
deterministically, a decline closes warmly with no re-pitch.

## Worker endpoints (all POST, Bearer auth — same conventions as /lana/rapport/*)

### Circles profile surface (§G)
- `POST /lana/circles/mine` → `{circles:[{id, circle_type, status, grounded,
  place_name, place_address, detail, member_count, active, added_at}]}`
  (own circles — always fully visible to the owner)
- `POST /lana/circles/add` `{circle_type, detail?, google_place_id}` — place
  REQUIRED (400 `place_required` without it); creates grounded + confirmed
- `POST /lana/circles/update` `{affiliation_id, detail?}`
- `POST /lana/circles/remove` `{affiliation_id}` — soft-delete; drops from matching immediately
- `POST /lana/circles/ground-options` `{affiliation_id, query?}` →
  `{options:[{name, address, google_place_id}]}` — 2-3 real nearby places for the
  "which spot — X, or somewhere else?" chips (`query` = "search for another")
- `POST /lana/circles/ground` `{affiliation_id, google_place_id}` →
  `{place_id, place_name, status:'confirmed'}` — **only the tapped id crosses the
  wire**; name/geo/address are fetched server-side from Google (a client can never
  mint or rename a canonical place)

### Invites + ZIP unlock (§A.2 · §D · §E)
- `POST /lana/invites/mint` `{circle_key?}` → `{token, url:/i/<token>}`
- `POST /lana/invites/redeem` `{token}` → `{ok, confirm_prompt, circle_type}` —
  records growth (`users.invited_by`, set once) + recounts the ZIP. Errors:
  `invite_not_found` (404), `invite_rate_limited` (429). **Invite ≠ membership**:
  the prompt hint is the TYPE only, never the inviter's place.
- `POST /lana/invites/self-confirm` `{token, circle_type, detail?}` — the joiner's
  own (ungrounded) affiliation, `source='invite_confirmed'`; she confirms it by
  grounding HER OWN place via ground-options → ground.
- `POST /lana/area/progress` `{zip?}` → `{zip5, state, count, threshold,
  is_founding_eligible, founding_earned, founding_area}` — recounts on read;
  powers the "7 of 10 to come alive" pill. Copy is FE-owned, warm,
  **no leaderboard anywhere** (§E.4), "Founding member" never "Founding Mom".

### Events (§5.2)
- `POST /lana/events/invite-suggestions` `{event_id}` → `{count, members:[{user_id,
  nickname}]}` — host-only; first names only (Stranger-tier safe per §F.1).

## State machine (§D)

`zip_unlock` rows are created/updated by `recount_zip_unlock(zip5)` (SQL,
service-role only), called from redeem + area-progress (read-repair; no cron).
"Verified active" (U2): verified by any method (`phone_verified_at`, which email
verify also stamps) + a Lana session ≤30d + ≥1 confirmed thing (circle or accepted
intro). `warming→open` stamps founding (U4, quality-gated) and fires one push to
all members. **Unlock gates consumption of others' supply only — no create/host/
invite path checks `unlock_state`, ever (§D.2).**

### Discovery gating (§D.2 consumption side) — wired, mode-flagged

`LANA_ZIP_UNLOCK_GATE = off | soft | hard` (default **soft**), enforced by
`zip_unlock.discovery_zip_gate` + hooks in browse and find-peers. Deliberately
**supply-aware**: an area that already has events keeps them fully visible in any
mode — hiding a host's event from neighbors starves the meets that make the area
come alive. What changes per mode:

- **soft** (default): EMPTY discovery states in a not-open area get the
  seed-forward framing instead of a bare "nothing found" — AI-authored from true
  facts ("7 of 10 neighbors so far"), with the seek-offer pills kept on an
  interest search and a "Host a meet" pill on a generic browse. Nothing is
  blocked.
- **hard**: soft, plus **find-peers introductions require an OPEN area** — the
  peers turn returns the exemplar-#7 seed reply ("your area's just getting
  started — want to host something and bring your people in?") instead of
  running the match. Sparse-area intros are junk-quality and privacy-risky, so
  peers is the one surface that truly locks.
- Fail-open everywhere: any gate lookup error proceeds ungated — a gating bug
  must never lock discovery. State reads are the stored `zip_unlock` row (no
  recount on the hot path; missing rows recount once, read-repair style).
- `capability_index.required_state` is now populated to match (migration
  `20260908`): `looking.meet` + `discovery.find_peers` → `{zip_open}`,
  `sharing.*` → `{}` explicitly, `discovery.find_activities` stays `{}` on
  purpose (browse availability is never state-gated; only its empty copy is).

## Contract for the onion matcher (other dev)

- Candidates: `circle_affiliations` where `status='confirmed' and dismissed_at is
  null` (partial index `circle_affiliations_place_confirmed_idx` matches exactly
  this predicate). `place_ref` → `places.id`; score on FK equality.
- Shared affinities: existing `user_identity_claims` vectors, unchanged shape;
  place-tagged rows now carry `place_ref` (bonus signal: same-place affinity).
- Disclosure (§F): strip `places.*` below Direct — recommend simply not joining
  `places` for below-Direct results. Open question O7 (what "Direct with a place"
  means for multi-member places) still needs locking with Tommaso.

## Deliberate deviations from the spec docs

- No `zips`/`waitlist_invites` tables exist in this repo → `zip_unlock` +
  `circle_invites`(+redemptions) instead.
- `circle_affiliations` is place_ref-native (no legacy inline place columns, no
  §8 Phase B/C dual-write — the table is net-new). The only backfilled legacy is
  `events.place_id`.
- Embeddings are 768-dim (text-embedding-005), not the spec's 1536.
- `places.place_type` is advisory + nullable (O6): Google's type wins, else the
  first grounder's circle_type; never overwritten on re-grounding.
- `place_features` conflict policy: overwrite only at ≥ stored confidence;
  `source='owner'` immune to rapport writes (Phase 2 correction path).
