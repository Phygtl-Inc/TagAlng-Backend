# PR7 · Geolocation-Aware Search

**Status:** ready for review · SQL verified against PROD in a rolled-back transaction
**Migration:** `supabase/migrations/20260917120000_geolocation_aware_search.sql` (staged here as `PR7_geolocation_aware_search.sql`)
**Repos touched:** `Phygtl-Inc/TagAlng-Backend` (SQL + worker) · `Phygtl-Inc/tagalng-pwa` (frontend)
**Supabase project verified:** `kmetmatfxdkrialwrnzj`
**Date:** 2026-07-30

---

## 0. The complaint

From the 2026-07-30 standup:

> **Tommaso:** "if my profile is set to my Orlando home, but I'm in Silicon Valley and I say I want to hit the gym, it would show me gyms in Orlando."
>
> **Asjid:** "it doesn't take user's location, it takes the block location."

Asjid is right, and the situation is slightly worse than described. There are **two** distinct home-block assumptions (places search and activity search), and a **third** latent bug (no radius cap) that makes the failure mode visibly absurd rather than merely wrong.

---

## 1. Root cause

### 1.1 The RPC layer is NOT the problem

`public.get_nearby_activities` already accepts coordinates and only falls back to the ZIP centroid when they are absent:

```sql
CREATE OR REPLACE FUNCTION public.get_nearby_activities(
  p_lat double precision DEFAULT NULL::double precision,
  p_lng double precision DEFAULT NULL::double precision,
  p_zip text DEFAULT NULL::text, ...)
...
declare
  v_lat double precision := p_lat;
  v_lng double precision := p_lng;
begin
  if v_lat is null or v_lng is null then
    v_zip5 := public.normalize_zip5(p_zip);
    ...
    select z.lat, z.lng into v_lat, v_lng
    from public.zip_centroids z where z.zip5 = v_zip5;
```

**The gap is entirely caller-side.** Confirmed by repo-wide search:

| RPC | Application call sites |
|---|---|
| `get_nearby_activities` | **exactly one** — `services/lana-worker/app/look_meet.py:456` |
| `get_nearby_activities_authed` | **zero** — SQL migrations only. Dead code. |
| `resolve_nearest_block_id` | **zero** — only invoked from inside other SQL functions |
| `get_blocks_near_zip` | `discovery_route.py:4876`, `apps/admin/lib/demo-user.ts:487` |

### 1.2 Failure #1 — Places search has no location input at all

`services/lana-worker/app/main.py` :: `search_places_endpoint`:

```python
auth = verify_auth(authorization)
from app.places import search_places

rows = search_places(query=body.q, block_id=auth.home_block_id, user_id=auth.user_id)
```

`services/lana-worker/app/models.py`:

```python
class PlaceSearchRequest(BaseModel):
    q: str
```

**One field. There is no lat/lng on the request, so there is nothing for the handler to pass.** The location bias is derived 100% server-side from `auth.home_block_id`.

That flows into `services/lana-worker/app/places.py` :: `_centroid`:

```python
def _centroid(zip_code, block_id, user_id=None):
    """Map center to bias the search. Tries (1) a passed ZIP, (2) a dev block, then
    (3) the user's home location ..."""
    if block_id and block_id in _BLOCK_FALLBACK:
        return _BLOCK_FALLBACK[block_id]
    z = _zip_centroid(service_client(), _normalize_zip5(zip_code))
    if z: return z
    if user_id:
        lat, lng, _ = resolve_event_location(user_id, None)
        return (lat, lng)
    return None
```

Every branch is home-derived. `_BLOCK_FALLBACK` in `services/lana-worker/app/event_location.py` is literally hard-coded Lake Nona:

```python
_BLOCK_FALLBACK: dict[str, tuple[float, float]] = {
    "8a2a1072b59ffff": (28.3647, -81.2568),
    "8a2a1072b5affff": (28.3689, -81.2621),
}
```

and `resolve_event_location` terminates in:

```python
    return 28.3647, -81.2568, block_id
```

That coordinate pair is Lake Nona, Orlando. **This is the literal "gyms in Orlando" line in the code.** The result is passed to Google Places as `locationBias.circle.center` with an 16 km radius (`search_places`) — so Google is explicitly *told* to look in Orlando.

### 1.3 Failure #2 — Activity search filters by block equality

`services/lana-worker/app/discovery_route.py` :: `fetch_preview_events_on_block` (line 5073) — the function behind the whole "what's happening" browse:

```python
res = (
    sb.table("events")
    .select("id, title, starts_at, has_time, venue_name, cohort_tags, host_id")
    .eq("block_id", block_id)
    .eq("status", "open")
    .gte("starts_at", now_iso)
    .order("starts_at")
    .limit(fetch_n)
    .execute()
)
```

`.eq("block_id", block_id)` — a plain PostgREST equality filter. No lat/lng, no distance, no PostGIS. And `block_id` comes from `resolve_block_id` (line 4844):

```python
def resolve_block_id(session_ctx, home_block_id):
    if home_block_id:
        return home_block_id
    bid = session_ctx.get("preview_block_id") or session_ctx.get("home_block_id")
    return str(bid) if bid else None
```

`home_block_id` wins unconditionally. **This is exactly Asjid's "it takes the block location."**

Two consequences: (a) travelling users get home events; (b) even at home, an event on the adjacent block that is physically closer than one on your own block is *invisible*. Equality is not proximity.

### 1.4 Failure #3 — `get_nearby_activities` has no radius cap

It orders the entire open-event set by distance and returns the top N. There is no `st_dwithin`, no bound. And the distance label is hard-coded to a walking speed:

```sql
concat(
  greatest(1, round(extensions.st_distance(e.location, v_point) / 80)::int),
  ' min walk'
) as distance_text,
```

**Verified live on PROD** — calling it from Silicon Valley coordinates:

```
get_nearby_activities(37.4419, -122.1430)
  → "Coffee & Catch-up"        3,914 km    label: "48930 min walk"
  → "Under 8s Pizza Playdate" 16,950 km    label: "211881 min walk"
  → "Under 8s Pizza Playdate" 16,950 km    label: "211881 min walk"
```

So even the one code path that *does* accept coordinates would present a Johannesburg event to a Palo Alto user as a 211,881-minute walk. Fixing the caller without fixing this would trade "wrong city" for "wrong hemisphere."

### 1.5 Also confirmed (context, not fixed here)

- `score_onion_candidates_for_user` contains **no geographic term whatsoever** — place `+3`, type `+1`, `+1` per shared concept. The peer matcher is geo-blind, as `_CODE_TRUTH_2026-07-30.md` states. Out of scope for PR7.
- `resolve_nearest_block_id(p_lat, p_lng, p_cluster_id DEFAULT 'lake-nona')` is **hard-pinned to one cluster**. For a device in California it returns an Orlando block. Currently harmless because all 7 rows in `blocks` are `cluster_id = 'lake-nona'`, but it is a landmine the moment a second cluster exists. PR7 deliberately does **not** use it (see `resolve_search_origin` comments).

---

## 2. Data anomaly found during verification

Two of the three upcoming open events are geographically inconsistent with their assigned block:

| id | title | block_id | lat | lng |
|---|---|---|---|---|
| `83d5b8e1-…dc17` | Under 8s Pizza Playdate | `8a2a1072b59ffff` (Lake Nona) | **-26.1834** | **27.9988** |
| `a700dd06-…234f` | Under 8s Pizza Playdate | `8a2a1072b59ffff` (Lake Nona) | **-26.1834** | **27.9988** |
| `0cef19da-…838a` | Coffee & Catch-up | `8a2a1072b59ffff` | 28.4753 | -81.2892 |

`-26.18, 27.99` is **Johannesburg, South Africa**. These rows claim a Lake Nona `block_id` while their `location` geography sits on another continent. They are also exact duplicates.

This matters for rollout: under today's `.eq("block_id")` browse they render as local Lake Nona activities. Under any geo-aware search they correctly vanish. **Expect the Lake Nona activity count to drop from 3 to 1 when PR7's worker change lands** — that is the fix working, not a regression. Someone should decide whether these are test rows to delete or real rows with a broken geocode.

---

## 3. Design

### 3.1 Principles (from the product constraints)

| Constraint | How it is enforced |
|---|---|
| Home block stays the DEFAULT | `resolve_search_origin` returns `origin_source='home'` unless explicitly overridden |
| Device location is an OVERRIDE only when meaningfully far | `p_away_threshold_meters` default **40 000 m (~25 mi)** |
| Explicit and warm, never silent teleporting | The function **cannot** switch on its own. It returns `should_offer_away=true` and the labels for the ask. Only a caller passing `p_use_device=true` — i.e. after the user consented — gets a device origin. Teleporting is not expressible in the API. |
| Never dead-end | `home_label` + `away_label` are returned so Lana always has copy; when `away_label` is null she says "where you are" rather than inventing a place name |
| Ephemeral, never a new home | Both functions are `STABLE`. Zero writes. Nothing touches `users.home_zip` / `home_block_id`. |
| Degrade gracefully on permission denied | No coords → `device_fix_usable=false`, `should_offer_away=false`, `origin_source='home'`. Silent. No prompt. |

### 3.2 The accuracy guard — the important subtlety

Browser geolocation silently falls back to **IP-based lookup** when GPS/wifi is unavailable. Those fixes are routinely 10–50 km off and frequently resolve to an ISP datacenter in a different metro.

Acting on such a fix would fire *"You're away from Lake Nona — want me to look around where you are instead?"* at a user sitting on their own sofa. That is the single worst failure mode for this feature: it makes Lana look like she doesn't know where you live.

`resolve_search_origin` therefore takes `p_device_accuracy_meters` (from `GeolocationCoordinates.accuracy`) and **discards any fix worse than `p_max_accuracy_meters` (default 25 000 m)**, treating it exactly as if permission had been denied. The frontend must pass `coords.accuracy`; if it doesn't, the guard is skipped and the risk returns.

### 3.3 New SQL objects (all additive — no existing function altered)

**`public.humanize_distance_text(p_meters, p_locale)`** → `text`
Honest label at any range. Walking only under 1 600 m; then miles (EN) or km (PT/ES). Replaces the uncapped `' min walk'` concatenation.

**`public.resolve_search_origin(p_user_id, p_device_lat, p_device_lng, p_device_accuracy_meters, p_use_device, p_away_threshold_meters, p_max_accuracy_meters, p_locale)`**
Returns exactly one row, always. Never raises for a missing fix or missing home. Key outputs:

| Column | Meaning |
|---|---|
| `origin_lat` / `origin_lng` | the point to actually search around |
| `origin_source` | `'home'` \| `'device'` \| `'none'` |
| `should_offer_away` | **true ⇒ Lana must ask before switching.** The only route to a device origin. |
| `is_away` | raw distance verdict, independent of consent |
| `distance_from_home_meters` / `_text` | for the ask copy |
| `home_label` / `away_label` | e.g. `"Edgewood (32839)"` / `"Foster City (94404)"` |
| `device_fix_usable` | false when absent, out of range, null-island, or too imprecise |
| `device_block_id` / `device_cluster_id` | nearest block to the device, searched **globally** (not cluster-pinned — see §1.5) |

Security: `SECURITY DEFINER` with an IDOR guard — when called with a user JWT (`auth.uid()` non-null) the `p_user_id` must match; the worker's service role (`auth.uid()` null) may pass any id. Granted to `authenticated, service_role` only.

**`public.get_activities_near_point(p_lat, p_lng, p_radius_meters, p_window, p_locale, p_limit)`**
Radius-capped, **block-agnostic** activity search. Uses `st_dwithin` (GiST-index-assisted) with a radius clamped to 100 m – 200 km, default 40 km. Emits `distance_text` via `humanize_distance_text`. Returns `block_id` so the caller can still reason about blocks. Granted to `anon, authenticated, service_role`.

`get_nearby_activities` is left **untouched** so its existing caller (`look_meet.py:456`) keeps working unchanged.

### 3.4 Conversation shape

```
User (device 3 900 km from home):  "I want to hit the gym"

  worker → resolve_search_origin(user, lat, lng, accuracy)
         → should_offer_away = true
           home_label = "Edgewood (32839)"
           away_label = "Foster City (94404)"
           distance_from_home_text = "2425 mi away"

Lana:  "You're a long way from Edgewood right now — want me to look around
        Foster City instead?"
        [ Yes, look here ]  [ No, keep it home ]

  "Yes"  → worker re-runs with p_use_device=true → origin_source='device'
           Sets an EPHEMERAL session_ctx flag. Never written to users.
  "No"   → nothing changes. Home search proceeds. Flag set so we don't re-ask
           this session.
```

Copy rules: never the word "block" (backstage vocabulary, per the existing `_compose_zip_ask` system prompt). If `away_label` is null, use "where you are". Never assert a place name we cannot substantiate.

---

## 4. Worker-side change required

> These are the changes PR7's SQL is designed to serve. **I have not written the Python** — this PR ships the migration only. Paths and function names below are verified against `Phygtl-Inc/TagAlng-Backend@main`.

### 4.1 Accept the device fix — `services/lana-worker/app/models.py`

```python
class DeviceLocation(BaseModel):
    lat: float
    lng: float
    accuracy_m: float | None = None   # GeolocationCoordinates.accuracy — REQUIRED for the guard
```

Add `device_location: DeviceLocation | None = None` to:
- `SendMessageRequest` (drives `/messages` and `/messages/stream`)
- `PlaceSearchRequest` — which today is only `q: str`

### 4.2 Thread it through — `services/lana-worker/app/main.py`

- `search_places_endpoint`: currently `search_places(query=body.q, block_id=auth.home_block_id, user_id=auth.user_id)`. Call `resolve_search_origin` first, then pass the resulting `origin_lat`/`origin_lng` down.
- `send_lana_message` / `stream_lana_message`: stash `body.device_location` on `session_ctx` under an **ephemeral** key (e.g. `device_fix`), alongside a `device_override_confirmed: bool` and `device_override_asked: bool`. These must be treated like the existing `preview_block_id` — session-scoped, never persisted to `users`.

### 4.3 Make the Places bias respect it — `services/lana-worker/app/places.py`

`_centroid(zip_code, block_id, user_id)` needs a new highest-precedence branch for a confirmed device fix:

```python
def _centroid(zip_code, block_id, user_id=None, device_latlng=None):
    if device_latlng:            # confirmed override wins
        return device_latlng
    ...                          # existing home-derived chain unchanged
```

Both `nearby_place_suggestions` and `search_places` forward the new argument. **Keep the existing "no centroid ⇒ return []" guard** — an unbiased Google Places search returns results near the *server*, which is worse than nothing.

### 4.4 Make activity browse geo-aware — `services/lana-worker/app/discovery_route.py`

Add a sibling to `fetch_preview_events_on_block` that calls the new RPC instead of `.eq("block_id", …)`:

```python
def fetch_preview_events_near(lat, lng, *, radius_m=40000, limit=5, pool=40):
    rows = call_rpc(user_jwt, "get_activities_near_point",
                    {"p_lat": lat, "p_lng": lng, "p_radius_meters": radius_m, "p_limit": pool})
```

`activity_browse.py::_fetch_block_events` (line ~205) then routes to the geo version when an origin is available, falling back to the block version otherwise. Note `activity_previews_from_events` currently emits no distance field — add `distance_text` so the FE can render it.

### 4.5 Ask, don't teleport — `services/lana-worker/app/activity_browse.py`

Add an away-offer branch mirroring the existing `_seek_offer` / `_expansion_offer` pattern (`draft["_away_offer"]`, `draft["suggestions"] = ["Yes, look here", "No, keep it home"]`), composed via `compose_reply` with `home_label` / `away_label` / `distance_from_home_text` as grounded facts. Ask **at most once per session**.

### 4.6 Don't forget `look_meet.py`

`_find_block_events` (line ~440) currently prefers `p_zip` over coordinates:

```python
args: dict[str, Any] = {"p_limit": 20}
if zip_code:
    args["p_zip"] = zip_code
else:
    loc = _centroid(zip_code, block_id)
    ...
    args["p_lat"], args["p_lng"] = loc[0], loc[1]
```

A confirmed device fix must take precedence over `zip_code` here, and this call should move to `get_activities_near_point` to inherit the radius cap.

---

## 5. Frontend change required

> Verified against `Phygtl-Inc/tagalng-pwa@main`.

**Good news:** `next.config.ts` already ships `Permissions-Policy: … geolocation=(self) …`, and CSP `connect-src` already includes the worker origin. No header work needed.

1. **New hook `src/hooks/use-geolocation.ts`** — does not exist today. Must:
   - use `navigator.permissions.query({name:'geolocation'})` to check state **without** triggering a prompt;
   - only call `getCurrentPosition` when already granted, or after an explicit user gesture;
   - return `{lat, lng, accuracy}` — **`accuracy` is mandatory**, it feeds the §3.2 guard;
   - never block a turn on the fix. Timeout ~8 s, `maximumAge` ~5 min, then proceed without it.

2. **Wire into `src/features/voice/components/lana-conversation.tsx`.** `postToLana(text, applyUi, intentHint, extra, session)` already has an `extra?: Record<string, unknown>` parameter that is spread into the request body for **both** the SSE and blocking paths (`sendMessage` → `streamMessage` / `lanaFetch` in `src/lib/lana.ts`). Merge `device_location` there. **No change is needed to `sendMessage`, `streamMessage`, or `lanaFetch`.**

3. **`src/lib/lana.ts` :: `searchPlaces(query)`** currently posts `{ q }` only. Extend to `{ q, device_location }`. Two callers to update: `WherePicker` in `src/features/voice/components/host-setup-carousel.tsx`, and whatever supplies `search` to `src/features/voice/components/community-place-picker.tsx`.

4. **Permission UX.** The only existing `navigator.geolocation` call in the repo is `pinMyLocation()` in `host-setup-carousel.tsx` (`{ enableHighAccuracy: true, timeout: 10000 }`), and it discards `coords.accuracy`. Do **not** prompt for location on chat load. Attach coords only when already granted; otherwise let Lana offer it conversationally.

5. **Render `distance_text`** on activity preview cards once the worker returns it.

---

## 6. Verification performed

All SQL was executed against PROD `kmetmatfxdkrialwrnzj` inside `begin; … rollback;`. **Nothing was committed.** Confirmed pre-flight: zero name collisions for all three new functions.

Test user: `8d7ac59d-d17f-48ed-9c27-6f33d7792a29` (home ZIP 32839 → `28.4889, -81.4114`, block `zip-32839` "Edgewood (32839)").
Device fixture: `37.4419, -122.1430` (Palo Alto / Silicon Valley) — 3 902 927 m from home.

| # | Case | Result |
|---|---|---|
| T1 | **Baseline bug**: `get_nearby_activities(SV)` | 3 rows, nearest **3 914 km**, labels **"48930 min walk"**, **"211881 min walk"** ✗ bug reproduced |
| T2 | `get_activities_near_point(SV, 40 km)` | **`[]`** ✓ |
| T3 | `get_activities_near_point(Lake Nona, 40 km)` | 1 row — "Coffee & Catch-up", **"7.9 mi away"** ✓ (the 2 Johannesburg rows correctly excluded — see §2) |
| T4 | No device fix (permission denied) | `origin_source='home'`, `should_offer_away=false`, `device_fix_usable=false` ✓ silent degrade |
| T5 | Away in SV, **not** confirmed | `origin_source='home'` (**no teleport**), `should_offer_away=true`, `is_away=true`, `home_label="Edgewood (32839)"`, `away_label="Foster City (94404)"`, `distance="2425 mi away"` ✓ |
| T6 | Away in SV, `p_use_device=true` | `origin_source='device'`, origin = `37.4419,-122.143`, `should_offer_away=false` ✓ |
| T7 | Same coords, `accuracy=50 000 m` | `device_fix_usable=false`, `should_offer_away=false`, origin stays home ✓ IP-noise guard works |
| T8 | Device 19.2 km from home (under threshold) | `is_away=false`, `should_offer_away=false` ✓ no false prompt |
| T9 | Label formatting | `400m→"5 min walk"`, `5000m→"3.1 mi away"`, `19229m→"12 mi away"`, `3902927m→"2425 mi away"`, `pt 5000m→"a 5 km"`, `es 400m→"5 min caminando"`, `null→null` ✓ |

**Bug found and fixed during verification:** the first pass rendered `"2548. mi away"` / `"a 5. km"` — `to_char` with the `FM` modifier strips trailing zeros but leaves a dangling `.`. Fixed with `rtrim(…, '.')`; re-verified in T9 above. The committed `.sql` file carries the fix and a comment explaining it.

### Before / after summary

| Query from Silicon Valley | Before | After |
|---|---|---|
| Activity search | 3 Orlando/Johannesburg events, "48930 min walk" | 0 events (correct — nothing within 40 km) |
| Places search ("gym") | Google biased to `28.3647, -81.2568` (Lake Nona) | Biased to device coords **after the user consents**; home otherwise |
| User at home, 19 km out | n/a | No prompt, no behaviour change |
| Permission denied | n/a | Identical to today's behaviour |

---

## 7. Test plan for the full feature

**SQL (add to `services/lana-worker/tests/`)**
- T1–T9 above as regression tests.
- Threshold boundary: 39 999 m → no offer; 40 001 m → offer.
- `p_user_id = null` → raises `user_id_required`.
- IDOR: user JWT for user A calling with user B's id → raises `forbidden`.
- Null island `(0,0)` → `device_fix_usable=false`.
- User with no `home_zip` **and** no `home_block_id`, no device → `origin_source='none'`.
- Same user **with** a device fix → `origin_source='device'`, `should_offer_away=false` (nothing to override).
- Radius clamp: `p_radius_meters=999999` → clamped to 200 km; `=1` → clamped to 100 m.

**Worker (drive the API directly — no browser needed, per `_CODE_TRUTH` §Testing implications)**
- Assert on `TurnRouting` (`outcome`, `intent_class`, `tool_called`) per turn.
- Away-user "find me a gym" → reply contains the offer, `tool_called` shows no place search executed yet.
- Accept → place search fires with device coords.
- Decline → place search fires with home coords; offer is **not** repeated later in the session.
- No `device_location` in body → byte-identical behaviour to today (the true regression gate).
- **Privacy assertion:** after a full away-mode session, re-read `users.home_zip` / `home_block_id` and assert unchanged.
- ⚠️ Use dedicated test accounts. **Never call `/complete` with default args** (`publish` defaults `true`). **Never call `/hooks/*`** (real push + email).

**Frontend**
- Permission denied / dismissed / granted paths.
- Low-accuracy fix (simulate `accuracy: 50000`) → no offer shown.
- Offline / geolocation timeout → turn still completes.
- `distance_text` renders on preview cards.

---

## 8. Rollout

1. Apply the migration. **Inert on its own** — nothing calls the new functions, so this step cannot change behaviour.
2. Deploy the worker behind an env flag (e.g. `GEO_AWARE_SEARCH=1`), default **off**.
3. Ship the frontend. With the flag off, `device_location` is accepted and ignored.
4. Flip the flag for internal accounts. Watch for false away-prompts (the §3.2 risk).
5. Flip on.

Decide before step 4 whether the two Johannesburg `Under 8s Pizza Playdate` rows (§2) are deleted or re-geocoded — otherwise Lake Nona's activity count visibly drops from 3 to 1 and will read as a regression.

---

## 9. Rollback

**SQL** — all three functions are new; zero collisions verified pre-flight. No existing object was altered, so a drop cannot clobber anything:

```sql
drop function if exists public.get_activities_near_point(
  double precision, double precision, double precision, interval, text, integer);
drop function if exists public.resolve_search_origin(
  uuid, double precision, double precision, double precision, boolean,
  double precision, double precision, text);
drop function if exists public.humanize_distance_text(double precision, text);
```

Drop in that order — the first two depend on `humanize_distance_text`.

**Worker** — independent of the SQL. Set `GEO_AWARE_SEARCH=0`, or revert the deploy. Home-block behaviour returns regardless of whether the functions exist, because nothing pre-existing calls them.

**Frontend** — independent of both. An extra `device_location` field on the request body is ignored by an older worker (Pydantic drops unknown fields by default).

The three tiers can be rolled back in any order.

---

## 10. What I could not determine

- **Which Supabase project is authoritative.** I verified against `kmetmatfxdkrialwrnzj` as instructed, but `_CODE_TRUTH_2026-07-30.md` names `rjlcyvwogmfmngemhbmn` as PROD. The two differ substantially: `kmetmatfxdkrialwrnzj` holds **7 blocks / 3 open events / 13 zip_centroids / 10 users with a home ZIP**, whereas `_CODE_TRUTH` reports **103 events** and 108 users with `home_zip`. The SQL is schema-compatible with both (identical function signatures), but **the before/after row counts in §6 apply only to `kmetmatfxdkrialwrnzj`.** Someone should confirm which ref the migration targets before applying.
- **The exact ask/decline copy.** I specified the shape and the grounded facts, not final wording — that should go through the normal Lana voice review, and §3.4's line is a placeholder.
- **Whether an away-mode session should also change peer/circle results.** `score_onion_candidates_for_user` is geo-blind (§1.5), so "who's around me" is unaffected by this PR either way. Deliberately out of scope, but it means away-mode is half a feature: Lana will find you a gym in Palo Alto but still show you Orlando neighbours.
- **Whether the Johannesburg events are test fixtures or real broken data.** I did not delete or modify them.
- **The `community-place-picker.tsx` caller.** The subagent confirmed the component takes a `search` prop but did not identify which parent supplies it, so §5.3's "two callers" may be off by one.
