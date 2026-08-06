# Place grounding — what changed on the backend + answers to your asks

Date: 2026-08-03 · Repo: `TagAlng-Backend` → `services/lana-worker`

Two parts:

1. **A bug we fixed** in the grounding flow (found in QA — the "Fitness CF" screenshot). Read this if you want to know why the offered spots will look different now.
2. **Answers to your five asks** from *Backend Asks* (issues #63), plus a status check on the older backlog.

**Nothing here needs a frontend change.** Every new field is additive and optional, and your current fallbacks stay correct. There are two behaviour changes on the tile you should know about, both explained below.

---

## Part 1 — the bug we fixed

### What was wrong

A user said *"I go to the gym at Fitness CF."* Lana's question said **"which Fitness CF location do you go to?"** — correct. But the three spots offered under it were **Crunch Fitness, EōS Fitness, and Lake Nona Performance Club**. None of them is a Fitness CF.

Why: the backend searched Google with the user's whole sentence, which matched nothing, then quietly fell back to *"just show me any gym near this person"* — and served those as if they were her answer. Nothing ever checked whether the results were the place she named.

A second problem: if we couldn't find her place, the conversation just ended (*"I'll remember that one"*). No next step.

### What we changed

| Before | Now |
|---|---|
| Searched the whole sentence ("I go to the gym at Fitness CF") | Searches only the **venue name** ("Fitness CF"), which the AI now pulls out when the user says it |
| Any nearby gym could be offered as her spot | A result is only offered as a **match** if its name actually contains the name she said |
| Not found nearby → gave up | Not found nearby → **searches a wider area** (a chain two towns over is normal), then says plainly that we couldn't find it |
| A typed search could return unrelated places | A typed search with no name match returns **nothing** — we never answer a specific search with random spots |
| Dead end when unpinned | Still offers a next step: *"want me to look for neighbours into the gym too?"* |

Truthfulness rule we now follow everywhere: **we only call something a match if it carries the name the user said.** Anything else is either offered as a question ("which of these is it?") or not offered at all.

### What this means for your card

Two behaviour changes, no code needed from you:

1. **When we can't find the name she said, the ask now arrives with `options: []`.** Your card already opens straight into its search box when there are no options (`useState(offered.length === 0)`) — that's exactly the behaviour we want. We'd rather show your search box than three wrong gyms.

2. **A single "guess" is never offered.** Your card renders one option as *"the place she mentioned — Pin it?"*, which would be a claim about her. So a lone unverified suggestion is dropped (→ search box). Two or three still come through as a pick-one grid, which reads as a question and is fine.

You will **not** see an escape/"none of these" chip in `options`. We added one for the chat surface (it has no search UI), but deliberately kept it off the tile, because your card already has "Search another" and a skip, and an option without a `google_place_id` would render as a place tile.

### New fields on each option (optional to use)

```jsonc
{
  "label": "Fitness CF Lake Nona",
  "address": "…",
  "google_place_id": "…",
  "send": "It's Fitness CF Lake Nona",
  "suggested": false,          // NEW — see below
  "unmatched_name": "Fitness CF" // NEW — only present in the case described below
}
```

- `suggested: false` → this place **carries the name the user said**. Safe to present as a match.
- `suggested: true` → the user never named a venue ("my gym"), so this is just a nearby place of the right kind. Fine as *"which one is it?"*, never as *"your gym"*.
- `unmatched_name` → she named a place, we **couldn't find it**. You will not receive these on the tile (we drop them), so you can ignore this field; it exists for chat, where the copy can say *"I couldn't find Fitness CF nearby…"*.

Ignoring both fields is safe.

---

## Part 2 — your five asks

### 1 · Send `circle_type` and the user's own phrase — **done**

`POST /lana/rapport/next-ask`, a `place_grounding` ask now includes:

```jsonc
{
  "kind": "place_grounding",
  "affiliation_id": "…",
  "circle_type": "fitness",              // for your glyph + noun
  "detail": "gym at Fitness CF",         // their words, feature notes stripped
  "place_name": "Fitness CF",            // NEW, extra: their name for the spot
  "options": [ … ]
}
```

`place_name` is a bonus we added while we were in there — it's the venue name the user actually said, which is usually what copy wants to name. `detail` is the fuller phrase.

All three are **omitted** when the affiliation doesn't have them, so your neutral-pin fallback stays correct. They're also served on cached re-shows, not just the first serve.

### 2 · Send the place an enrichment ask is about — **done**

Any rapport gap with a `place_ref` now serves:

```jsonc
{ "kind": "place_affinity", "place_name": "OrangeTheory Narcoossee", "place_type": "fitness" }
```

Read from the `places` row. If the gap has no `place_ref`, or the place row is gone, nothing is added and the ask serves exactly as before — so your plain-rapport-card path still works.

**The optional answer chips are not done.** Synthesising 2–3 chips from `place_features` is doable; free text remains the only answer path for now. Tell us if you want it and we'll add `options: [{label, send}]`.

### 3 · One call to finish a searched place — **done, option (b)**

`/lana/circles/ground` now closes the affiliation's open/asked grounding gap itself, so a place pinned by id will not re-show as an ask.

We put it one level deeper than you asked: the gap is closed inside `ground_affiliation`, the function *every* grounding path goes through — so the endpoint, the chat flow, and anything added later all get it. It's idempotent, so the chat path closing the same gap afterwards is harmless.

**What you can do now:** the second call is no longer needed. A searched pick is one call — `/lana/circles/ground {affiliation_id, google_place_id}`. Nothing breaks if you keep the answer turn, it just isn't required.

We did **not** add `google_place_id` to `record-answer` (option a). Ask if you'd still like it.

### 4 · Confirm the offered-spot branch — **not verified yet**

We couldn't run it either: it needs a real verified account against dev, which we don't have credentials for in this environment. It's on us — we'll report back with a yes or no.

One note on your finding: an **open** (never-served) gap routing as ordinary chat is expected behaviour, not the bug. That path needs the gap in `status='asked'` **with options cached**, which only a served ask produces. So the measurement doesn't tell us the offered path is broken — it tells us the setup wasn't reachable from a probe account.

Keep your fallback plan ready (route offered taps through ground-by-id) but don't switch yet.

### 5 · Guests and the "By the way…" tile — **product decision, not ours to close**

The current rule stands as coded: guests get `{ask: null}`, so no tile and no place pinning until they verify. The candidate is still captured and carries over on verify.

We've flagged it for a product call. Our own read: relaxing the gate for `kind='place_grounding'` only is defensible — pinning a gym isn't "profile deepening" — but it's not a change we'll make unilaterally. We'll come back with a decision.

---

## Part 3 — the older backlog

We checked these against the repo rather than the list. **Five look already shipped:**

| Ask | Issue | Status in repo |
|---|---|---|
| Host needs an `event_requests` row | #50 | **Shipped** — `20260911120000_host_rsvp.sql` |
| Cancel a pending co-host invite | #52 | **Shipped** — `20260902120000_revoke_pending_cohost_invite.sql` |
| Cancelled meets 404 in preview | #53 | **Fixed in repo** — `20260831120000_event_preview_cancelled.sql`. You report it still failing *in prod*, which points to the migration not being applied there. We're checking what's actually applied on dev vs prod. |
| Leave an active meet / `uncancel_event` | #55 | **Partly** — `20260826120000_not_going_keeps_chat.sql` landed; no withdraw RPC and no `uncancel_event` yet |
| Three-roster `get_event_preview_authed` | #50 | **Open** — no `maybe_participants` anywhere |
| `affinities[]` on look-meet events | #54 | **Open** |
| `cohost_id` / `viewer_can_manage` on preview | #56 | **Open** |
| Matched affinity tag ids, not just count | #59 | **Open** |
| Per-claim match intensity | #60 | **Open** |
| What closes `await_profile_photo` | #61 | **Open** |
| Seed `users.locale` in `handle_new_user()` | #45 | **Open** |
| Silent/restate mode | — | **Open** |

If any of the "shipped" rows still misbehave for you, it's most likely an unapplied migration on the environment you're testing rather than missing code — worth re-testing once we confirm what's applied where.

---

## One thing to wait for

The grounding fix ships with a migration (`20260927120000_circle_place_name.sql`). Until it's applied, the new `place_name` field won't be populated. It also clears the cached options on asks that are still open, so anyone who already saw a wrong list gets a fresh, correct one instead of the cached one.

**Timeline:** migration first, worker second. We'll confirm here when both are on dev and prod.
