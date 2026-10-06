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
   `operator_verified_requires_verified_claim`, for **every** handle, dashed or not. On top of
   that, both pick the handle by shape only (`_place_handle_shape_error`), so a one-word
   reservation never falls back to `suggest_place_handle` and would hit the guard. And the email
   path labels the claim `domain_email`, which Proof B doesn't accept.

## Decisions (agreed 2026-10-06)

- **Email proof = the signed-in account.** lana.help's "confirm by email" is a Supabase email-OTP
  sign-in; the reservation is bound to that account. A PWA user who signed in with an email code
  has the same proof. Guests (anonymous auth, no email) must sign in first. No extra code.
- **All community types** may hold a one-word handle with this proof (Tommaso's ask). This is
  first come, first served; `protected_handles` and the member-handle check remain the only
  defence for famous names. Accepted knowingly.
- **Two entry points:** a step at the end of Lana's create-community chat, and a "Claim your
  link" action on the community page for its operator (covers SJSU and existing communities).
- **Setting a first handle is free.** `handle_renamed_at` is not stamped; the one rename stays
  available for a later change.

## Design

### 1 · Database · one migration `20270108120000_in_app_handle_claim.sql`

**a. `_place_handle_proven(p_place_id uuid, p_handle text) returns boolean`**, the single
definition of "this place may hold this bare handle": Proof A (verified external identity with
that username) or Proof B (verified claim with `verification_method in ('reservation_email',
'domain_email')` whose reservation's `normalized_handle` is the handle). **No place-type
condition.** Both email methods mean the same evidence: this inbox confirmed this exact string.

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
2. `is_community_operator(p_place_id, uid)` → else `{status:'not_operator'}`.
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
8. Return `{status:'claimed', handle, url}`.

Dashed handles go through the same function. They don't need the proof, but recording the
claim keeps one trail for every in-app handle.

**d. `complete_place_claim_by_email` and `approve_place_claim`**, re-emitted with:
- the claim resolved to `verified` **before** the `places` update (the email path writes
  `reservation_email`, matching lana.help; approval keeps its reviewer fields);
- handle choice guard-aware: use the reserved string only if
  `_place_handle_shape_error(v) is null` **or** (bare and `_place_handle_proven` would hold after
  this claim, and not a member/protected handle); otherwise `suggest_place_handle`. Since the
  claim is now written first, a bare reservation that came through email *does* earn its bare
  handle, and anything refused degrades to `name-city` as the comment always promised.

### 2 · Worker · `community_capture.py`

After `publish_community` succeeds and the user is signed in (not a guest), Lana offers a short
link: the AI-written line plus a chip with the suggested handle (from `check_place_handle` on the
community's slug), and "Skip". A tapped chip or typed name calls `claim_community_handle_for`
with the session's user id. `unavailable` → offer the returned suggestions as
chips; `sign_in_required` → skip silently (guests get the community-page action later). One ask
only, never re-asked in the same session. All copy is AI-rendered at the final-mile choke point,
with no canned lines.

### 3 · PWA · community page

Operators of a community with `handle is null` see **"Claim your link"**. It opens a small sheet:
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
| `not_operator` | caller doesn't manage it | hidden (button only shows to operators) |
| `already_has_handle` | handle set already | the existing link |
| `invalid` | bad shape/length | inline hint |
| `unavailable` | taken / protected / held / member / retired | suggestions |
| `claimed` | done | link + share |

## Testing

- **SQL**, in the local validation container: a school place gets `sjsu` via
  `claim_community_handle`; a guest gets `sign_in_required`; a non-operator gets `not_operator`;
  `nike` (protected) gets `unavailable`; a member's handle gets `unavailable`; a taken name gets
  suggestions; a second call gets `already_has_handle`; a direct `update places set handle='x'`
  with no proof is still refused; `complete_place_claim_by_email` with `safeway` and no proof gets
  `safeway-<city>`; with a bare reservation it gets the bare handle; `approve_place_claim` succeeds
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
