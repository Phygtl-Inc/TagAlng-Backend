# In-app community handle claim · design

*2026-10-06 · answers Tommaso's BUG_HANDLE_GUARD_ASJID.md (2026-10-05) · option A*

## Problem

A one-word community handle (`get.lana.help/sjsu`) must pass `places_single_token_handle_guard`.
The only proof that works today is **Proof B**: a verified `reservation_email` claim whose
reservation is exactly that handle. Three things stop communities from getting it:

1. **Proof B is creator-only.** The guard adds `new.place_type = 'creator'`, so a school, gym or
   church that went through the same reserve-and-email flow is refused on its type.
2. **Communities made in the app can never produce the proof.** The PWA "Create a community"
   button goes through Lana's `community_capture` lane → `add_circle`. That writes a `places`
   row with no handle, no reservation and no claim. The app has no handle screen at all
   (`rename_community_handle` has no caller). SJSU was made in-app, then hand-verified with a
   `manual_founder` claim, so even with (1) fixed, `sjsu` is still refused.
3. **The email claim and admin approval paths fail outright.** `complete_place_claim_by_email`
   and `approve_place_claim` set `governance_state = 'operator_verified'` *before* resolving the
   claim to `verified`. `places_verified_needs_claim_trg` (20261228120005) then raises
   `operator_verified_requires_verified_claim`, for **every** handle, dashed or not, unless the
   place already had some other verified claim. (Tommaso's doc also says a bare reservation
   passes the shape check here. Per the migrations it doesn't: see 1d.)

## Decisions (agreed 2026-10-06)

- **Email proof = the signed-in account.** lana.help's "confirm by email" is a Supabase email-OTP
  sign-in; the reservation is bound to that account. A PWA user who signed in with an email code
  has the same proof. Guests (anonymous auth, no email) must sign in first. No extra code.
- **All community types** may hold a one-word handle with this proof (Tommaso's ask). This is
  first come, first served; `protected_handles` and the member-handle check remain the only
  defence for famous names. Accepted knowingly.
- **Two entry points:** a step at the end of Lana's create-community chat, and a "Claim your
  link" action on the community page for its operator (covers SJSU and existing communities).
- **Who may claim (2026-10-06):** a community's existing operator, OR, for a name-only
  community (`google_place_id like 'creator:%'`, `governance_state = 'community_started'`), the
  user who created it (`places.created_by`). The latter becomes its verified operator on claim,
  exactly as lana.help self-verifies creators. Members of a community on a real Google place
  (gym, store) cannot self-verify; that still goes through lana.help's location claim.
- **Proof B stays `reservation_email` only.** Accepting `domain_email` too would let anyone who
  claimed a location by email rename it to a bare brand word (`safeway`). Location claims keep
  locality handles, which is the behaviour Tommaso's own `safeway-foster-city` example describes.
- **Setting a first handle is free.** `handle_renamed_at` is not stamped; the one rename stays
  available for a later change.

## Design

### 1 · Database · one migration `20270108120000_in_app_handle_claim.sql`

**a. `_place_handle_proven(p_place_id uuid, p_handle text) returns boolean`**, the single
definition of "this place may hold this bare handle": Proof A (verified external identity with
that username) or Proof B (verified `reservation_email` claim whose reservation's `normalized_handle` is the
handle). **No place-type condition.**

**b. `places_single_token_handle_guard()`** keeps its order and its unconditional checks (compound
passes; member handle → `handle_taken_by_user`; protected → `handle_protected`) and replaces the
inline proofs with `_place_handle_proven(new.id, new.handle)`.

**c. `claim_community_handle_for(p_user_id uuid, p_place_id uuid, p_handle text) returns jsonb`**,
security definer, **service_role only** (the worker talks to Supabase with the service role, so it
has no user JWT). **`claim_community_handle(p_place_id, p_handle)`**, granted to `authenticated`,
is a thin wrapper that passes `auth.uid()`. The PWA calls the wrapper; the worker calls `_for`
with the session's user id. In one transaction:

1. Caller: user id not null, `auth.users.is_anonymous` false, `email_confirmed_at` not null →
   else `{status:'sign_in_required'}`.
2. Eligibility, `_community_handle_claim_status(uid, place)`: operator, or creator of a
   name-only `community_started` place → else `{status:'not_eligible'}`.
3. Place already has a handle → `{status:'already_has_handle', handle}` (changes go through
   `rename_community_handle`).
4. Normalise; shape check with `_place_handle_shape_error(v, false)` (bare allowed) →
   `{status:'invalid', reason}`.
5. Availability: `_place_handle_taken(v)` (places, protected, live holds), plus `users.handle`
   and `place_handle_aliases` → `{status:'unavailable', reason, suggestions}`. Suggestions come
   from `suggest_place_handle(p_place_id, v)` plus `v-2…v-9`, as in `check_place_handle`.
6. Insert a `place_handle_reservations` row (`status 'consumed'`, `user_id`, `place_id`,
   `source 'in_app'`, random `token_hash`), then a `place_claims` row
   (`status 'verified'`, `verification_method 'reservation_email'`, `reservation_id`,
   `requested_by uid`, `review_notes 'In-app handle claim by signed-in, email-confirmed operator.'`).
7. `update places set handle = v` (the guard now passes). A unique violation from a race →
   `{status:'unavailable', reason:'taken'}`.
   The same update sets `governance_state = 'operator_verified'`, `claimed_by`/`claimed_at`
   (coalesced, so an existing operator is untouched); `places_sync_operator_trg` then writes
   `place_managers`. The claim row is inserted first, so `places_verified_needs_claim_trg` passes.
8. Return `{status:'claimed', handle, placeId}`.

**c2. `community_handle_offer_for(uid, place)` / `community_handle_offer(place)`**: eligibility
plus a suggested handle (the normalised community name, else `suggest_place_handle`, else
`name-2…9`). Used by the worker after publish and by the PWA to decide whether to show the button.

Dashed handles go through the same function. They don't need the proof, but recording the
claim keeps one trail for every in-app handle.

**d. `complete_place_claim_by_email` and `approve_place_claim`**, re-emitted with one change:
the claim is resolved to `verified` **before** the `places` update, so
`places_verified_needs_claim_trg` stops raising. Handle choice is unchanged. Per the migrations,
`_place_handle_shape_error(v)` defaults to `p_require_locality = true`, so a bare reservation
already falls back to `suggest_place_handle` (`safeway` → `safeway-foster-city`). Tommaso's doc
says live prod lets `sjsu` through the shape check. If prod differs from the migrations, that is
drift to investigate separately; it could not be checked from here (no prod read access).

### 2 · Worker · `community_capture.py`

On publish, the worker calls `community_handle_offer_for(user_id, place_id)` (service role). If
eligible, it attaches `handle_offer = {place_id, suggestion}` to the published `CommunityDraft`.
No new chat lane and no parsing of the next turn: the offer is a **rendered control**, so the PWA
shows it as a button and the claim itself always goes through the PWA sheet (one claim path).
The celebration line gets the fact "they can claim a short link get.lana.help/<suggestion>" so
the AI-written reply can mention it.

### 3 · PWA · community page

Two entry points, one `ClaimLinkSheet`: the published community card in chat (from
`handle_offer`), and the community's edit drawer (`CommunityEditDrawer`, which calls
`community_handle_offer` and shows the section only when eligible). It opens a small sheet:
`get.lana.help/[____]` prefilled with a suggestion, live availability, Claim button. It calls
`supabase.rpc('claim_community_handle', …)` and renders each status. A guest sees "Sign in to
claim" and goes through the existing sign-in. On success the page shows the link with copy/share.
Separate PR on tagalng-pwa (asjid9 token).

### 4 · Docs (Tommaso item 3)

Remove the stale "write both `claimed_by` and `place_managers`" warnings.
`places_sync_operator_trg` does it now.

## SJSU after this ships

Pouya (SJSU's operator, signed in with email) opens SJSU → "Claim your link" → `sjsu` → claimed.
No hand-written SQL.

## Errors

| Status | Meaning | Shown as |
|---|---|---|
| `sign_in_required` | guest or unconfirmed email | sign-in prompt |
| `not_eligible` | not operator / not the name-only creator | hidden (button only shows when eligible) |
| `already_has_handle` | handle set already | the existing link |
| `invalid` | bad shape/length | inline hint |
| `unavailable` | taken / protected / held / member / retired | suggestions |
| `claimed` | done | link + share |

## Testing

- **SQL**, in the local validation container: a school place gets `sjsu` via
  `claim_community_handle`; a guest gets `sign_in_required`; a non-operator gets `not_eligible`; a member of a real-place community gets `not_eligible`; the creator of a name-only community gets the handle and becomes operator;
  `nike` (protected) gets `unavailable`; a member's handle gets `unavailable`; a taken name gets
  suggestions; a second call gets `already_has_handle`; a direct `update places set handle='x'`
  with no proof is still refused; `complete_place_claim_by_email` with `safeway` and no proof gets
  `safeway-<city>`; `approve_place_claim` succeeds
  (it currently raises); creators (`mrbeast`) still pass.
- **Worker** unit tests for the offer/claim/skip/suggestion turns, plus mutation tests run in a
  copy (never in place: the live `--reload` worker).
- **Real scenario** on dev: create a school community in the PWA, claim a one-word link from chat;
  claim from the community page for an existing community.
- Suite compared against the known baseline failures.
- Rollout: dev → your OK → prod. Then Pouya claims `sjsu`.

## Out of scope

- Changing an existing handle (`rename_community_handle` already covers it).
- Proving the claimant represents the institution (e.g. an `@sjsu.edu` address).
- Backfilling handles for existing in-app communities. They claim through the new action.
