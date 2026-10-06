# In-app community handle claim · implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Steps use `- [ ]`.

**Goal:** Any community can hold a one-word handle with proof; communities made in the app can
claim one (name-only creators self-verify, real places need an existing operator); the email
claim and admin approval paths stop failing.

**Architecture:** One migration owns all rules (proof helper, eligibility helper, claim and offer
RPCs, guard, reordered claim completion). The worker only attaches an offer to the published
draft; the PWA renders it and is the single place a claim is made.

**Tech stack:** Postgres/Supabase migrations, FastAPI worker (Python, pytest), Next.js PWA (zod,
next-intl, supabase-js).

## Global constraints

- Spec: `docs/superpowers/specs/2026-10-06-in-app-community-handle-claim-design.md`.
- Proof B accepts `reservation_email` only. Member-handle and `protected_handles` checks stay unconditional.
- A first handle never stamps `handle_renamed_at`.
- All Lana copy is AI-composed (`compose_reply`), no canned lines. No new regex matchers.
- Mutation tests run in a copy, never the live tree (`uvicorn --reload` serves it).
- Prove SQL in the local validation container before any push. No prod access from this session.

---

### Task 1: Migration `supabase/migrations/20270108120000_in_app_handle_claim.sql`

**Produces (SQL):**
- `_place_handle_proven(uuid, text) → boolean` (internal)
- `_place_handle_unavailable(text) → text|null` (internal: taken/protected/held/member/retired)
- `_community_handle_claim_status(uuid user, uuid place) → text|null` (internal: null = may claim)
- `_community_handle_suggestions(uuid place, text wanted) → text[]` (internal)
- `community_handle_offer_for(uuid user, uuid place) → jsonb {eligible, reason?, suggestion?}` (service_role)
- `community_handle_offer(uuid place) → jsonb` (authenticated; wraps with `auth.uid()`)
- `claim_community_handle_for(uuid user, uuid place, text handle) → jsonb {status, …}` (service_role)
- `claim_community_handle(uuid place, text handle) → jsonb` (authenticated)
- re-emitted `places_single_token_handle_guard`, `complete_place_claim_by_email`, `approve_place_claim`

- [ ] Write the SQL test script `supabase/tests/in_app_handle_claim.sql` (fixtures in a
      transaction, `do $$ … assert … $$` blocks, rolled back) covering every case in the spec's
      Testing section.
- [ ] Run it in the container on the clean tree: it must fail (functions missing).
- [ ] Write the migration.
- [ ] Apply all migrations in the container: exactly the 4 known storage failures, nothing else.
- [ ] Run the test script: all asserts pass.
- [ ] Mutation check: drop the `place_type = 'creator'` removal, the claim-first reorder, and the
      creator-only eligibility branch, one at a time, in a copy. A test must fail for each.
- [ ] Commit.

### Task 2: Worker offer on publish

**Files:** `services/lana-worker/app/models.py` (`CommunityDraft.handle_offer`, new
`HandleOffer{place_id, suggestion}`), `services/lana-worker/app/community_capture.py`
(publish branch), `services/lana-worker/tests/test_community_handle_offer.py`.

**Consumes:** `community_handle_offer_for(p_user_id, p_place_id)`.

- [ ] Failing tests: published + eligible → `draft["handle_offer"] == {"place_id", "suggestion"}`
      and the compose facts mention the link; ineligible or RPC error → no `handle_offer`, publish
      still succeeds; guest (no user id) → RPC not called.
- [ ] Implement `_handle_offer(place_id, user_id)` (service client, best effort, logs on error) and
      stamp it in the publish branch.
- [ ] Tests pass; mutation in a copy; full suite vs baseline (diff FAILED lines).
- [ ] Commit.

### Task 3: Docs (Tommaso item 3)

- [ ] Find docs that tell people to write `place_managers` alongside `claimed_by` by hand; replace
      with a note that `places_sync_operator_trg` does it. Commit.

### Task 4: PWA (separate repo/PR, branch off `main`)

**Files:** `src/lib/lana.schema.ts` + `src/lib/lana.ts` (`handle_offer`), new
`src/lib/community-handle.ts` (`fetchHandleOffer`, `claimCommunityHandle`), new
`src/features/circles/components/claim-link-sheet.tsx`, `community-draft-card.tsx` (button),
`community-edit-drawer.tsx` (section), `messages/{en,es,pt-BR}.json`.

- [ ] Schema + client functions; sheet with live input, Claim, per-status rendering, success with copy/share.
- [ ] Wire both entry points. `npm run lint && npx tsc --noEmit && npm test`.
- [ ] Commit; open PR with asjid9's token.

### Task 5: Real scenario on dev (after the user OKs a dev push)

- [ ] Push the migration to dev. Create a name-only community in the PWA → claim a one-word link
      from the chat card. Open SJSU-like operator place → claim from the edit drawer.
