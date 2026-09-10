# PR10 · Rate limiting + bot detection

**Status:** ready for review · SQL verified against PROD in a rolled-back transaction
**Migration:** `supabase/migrations/20260917140000_lana_rate_limiting.sql` (staged here as `PR10_rate_limiting.sql`)
**Repo:** `Phygtl-Inc/TagAlng-Backend` · SQL + `services/lana-worker`
**Supabase project verified:** `kmetmatfxdkrialwrnzj` (`tagalng-prod`, 76 public tables, Postgres 17.6)
**Feature flags:** `LANA_RATE_LIMIT=off|shadow|on` (ships **`shadow`**) · `LANA_BOT_DETECT=off|shadow|on` (ships **`shadow`**) · `LANA_TURNSTILE_ENABLED=0` (**off**)
**Date:** 2026-07-30

---

## 0. The complaint

Asjid, 2026-07-30: anonymous sign-in means anyone can script the backend thousands of times at our cost. No throttle, no captcha. The Google Maps-backed endpoints are reachable the same way.

He is right, and the exposure is total.

---

## 1. Root cause — verified, not assumed

### 1.1 There is no rate limiting anywhere in the worker

Repo-wide search of `services/lana-worker/app/**.py` for `rate_limit|ratelimit|throttle|turnstile|captcha|X-Forwarded-For|device_id` returns **six** hits, all unrelated:

```
app/supabase_rpc.py:51:    if "nudge_rate_limit" in lower:
app/supabase_rpc.py:52:        return {"status": 429, "detail": "nudge_rate_limit_daily"}
app/supabase_rpc.py:54:        return {"status": 429, "detail": "nudge_cooldown_pair"}
app/main.py:2515:        status = 429 if detail == "invite_rate_limited" else 404
app/circle_invites.py:86:def _rate_limited(invite_id: str) -> bool:
app/circle_invites.py:115:        if _rate_limited(str(invite["id"])):
```

Those are two *product* cooldowns implemented in Postgres (nudge frequency, invite resend). There is **no request-level throttle on any of the 34 endpoints**, no IP awareness, and no `Request` object is accepted by any handler — `app/main.py` imports `from fastapi import BackgroundTasks, FastAPI, File, Header, HTTPException, UploadFile`. `Request` is not imported, so the worker cannot currently see a client IP at all.

The only middleware is CORS, and it is wide open by default:

```python
_cors_raw = os.environ.get("CORS_ALLOW_ORIGINS", "*").strip()
_cors_origins = ["*"] if _cors_raw == "*" else [o.strip() for o in _cors_raw.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

(CORS is a browser policy; it does nothing against `curl`. Noted only because it is the only middleware present.)

### 1.2 Anonymous auth is a real, unlimited credential

`app/auth.py::verify_auth` accepts any valid Supabase JWT, including anonymous ones, and hands back:

```python
@dataclass(frozen=True)
class AuthSession:
    user_id: str
    is_anonymous: bool
    phone_verified: bool
    home_block_id: str | None
    role: str | None = None
    grammatical_gender: str | None = None
```

`is_anonymous` is **read and used for product branching** (guest intake, guest capabilities) but never for cost control. A script does `POST /auth/v1/signup` against Supabase with no credential, gets an anonymous JWT, and every `/lana/*` endpoint opens.

### 1.3 The three money endpoints, unprotected

**a) The turn.** `POST /lana/sessions/{id}/messages` and `/messages/stream` both funnel into `_run_lana_message`, which begins:

```python
    _vertex_required()
    timer = TurnTimer()
    if emit is not None:
        timer.set_emitter(emit)
    auth = verify_auth(authorization)

    with timer.stage("db_load_session"):
        session = get_session_for_user(session_id, auth.user_id)
    if session.get("status") != "active":
        raise HTTPException(status_code=400, detail="session_not_active")
    ...
    with timer.stage("db_save_user_message"):
        user_msg_id = insert_message(session_id, "user", body.message.strip(), {}, embed=False)
```

Everything after that is billable: the discovery classifier (`gpt-4.1-mini`), the synthesizer (`gpt-4.1`, `max_tokens=2048`), the lingo guard, `compose_reply`, plus a background embedding per message. Nothing counts, nothing caps.

**b) Session creation.** `POST /lana/sessions`:

```python
@app.post("/lana/sessions", response_model=CreateSessionResponse)
def create_lana_session(
    body: CreateSessionRequest,
    authorization: str | None = Header(default=None),
    accept_language: str | None = Header(default=None),
):
    _vertex_required()
    auth = verify_auth(authorization)
    require_home_block_for_purpose(auth, body.purpose)
```

`CreateSessionRequest.force_new: bool = False` — a script setting `force_new=true` gets a brand-new session **and a brand-new LLM opening** on every call. This is the cheapest possible way to burn our budget.

**c) Google Places.** Both handlers are one `verify_auth` away from Google's meter:

```python
@app.post("/lana/places/search", response_model=PlaceSearchResponse)
def search_places_endpoint(
    body: PlaceSearchRequest,
    authorization: str | None = Header(default=None),
):
    auth = verify_auth(authorization)
    from app.places import search_places

    rows = search_places(query=body.q, block_id=auth.home_block_id, user_id=auth.user_id)
    return PlaceSearchResponse(results=[PlaceResult(**r) for r in rows])


@app.post("/lana/places/reverse-geocode", response_model=PlaceSearchResponse)
def reverse_geocode_endpoint(
    body: ReverseGeocodeRequest,
    authorization: str | None = Header(default=None),
):
    verify_auth(authorization)
    from app.places import reverse_geocode
```

`app/places.py` calls `https://places.googleapis.com/v1/places:searchText` with `GOOGLE_MAPS_API_KEY`. Places API (New) Text Search is billed per request. `reverse_geocode_endpoint` does not even bind the auth result to a name — there is currently no per-user attribution to throttle on.

### 1.4 There is no counter store, and no Redis

Verified on `tagalng-prod` (`kmetmatfxdkrialwrnzj`):

```sql
select count(*) from information_schema.tables
where table_schema='public'
  and (table_name ilike '%rate%' or table_name ilike '%limit%'
    or table_name ilike '%quota%' or table_name ilike '%bot%'
    or table_name ilike '%abuse%' or table_name ilike '%device%');
-- → 0
```

76 public tables, none of them a counter. `pgcrypto` is installed.

---

## 2. Design rationale

### 2.1 Why Postgres and not memory

Cloud Run scales horizontally and recycles instances. An in-process dict is reset by every cold start and is per-instance, so a script that opens 8 connections gets 8 independent quotas. There is no Redis in this stack and adding one is a new dependency, a new secret, a new failure mode and a new bill for a pilot with 31 prod users.

Postgres already holds every other cooldown in this product (`nudge_rate_limit_daily`, `invite_rate_limited` — §1.1), so this is the house pattern, not a new one. Cost is one RPC per gated request; a turn already makes three Supabase round-trips before the first LLM token, so this is noise beside a 1-3 s model call.

**The counters are durable across deploys, restarts and instance count.** That was the requirement.

### 2.2 Why one generalised counter table, not three

`lana_rate_counters` is keyed by `(subject_kind, subject_id, metric, window_seconds, bucket_start)`. The bucket is derived:

```
bucket_start = to_timestamp(floor(epoch(now()) / window_seconds) * window_seconds)
```

For `window_seconds = 86400` that is exactly UTC midnight — the daily reset the tier table assumes. The same table therefore serves a 24 h turn quota, a 1 h session-create quota and a 1 h places quota with no schema branching and one index strategy. Adding a fourth metric later is an enum value, not a migration of substance.

### 2.3 Why the anonymous wall is a conversion, not an error

This is the product decision that shapes the whole PR. At 12 turns an anonymous user is **engaged** — they are the single best signup prospect we will see that day. Returning `429 Too Many Requests` to a person who is having a good conversation is throwing away the funnel to save a few cents.

So: anonymous at limit returns **HTTP 200 with a normal Lana turn** whose content is a warm email ask, and sets `requires_phone_verification=True` — the field the PWA already reads to render the email-verify button (the name is legacy; `app/auth.py` documents that the gate "is now fed by EMAIL verification (email OTP), not SMS"). The existing guest flow already speaks this language:

```python
        reply = (
            "Almost there — verify your email with the button below, "
            "then send me a quick message and we'll keep going."
        )
```

We reuse that machinery rather than invent a new dead-end error surface. **Lana never says no; she says "give me your email and we keep going."**

### 2.4 Why the signed-in tiers degrade instead of blocking

A verified user who hits 60 or 200 turns in a day is not an attacker, they are our best user. Blocking them is a product own-goal. Downgrading the synthesizer from `gpt-4.1` to `gpt-4.1-mini` cuts the marginal cost by roughly an order of magnitude, is invisible in a two-sentence reply, and preserves the relationship. If the same account then trips the *bot* signals, it gets flagged and blocked on behaviour — which is the correct reason to block, not on volume.

### 2.5 Why bot detection ships in shadow first

Every threshold in §4 is a false-positive risk against a real person on a slow phone. `LANA_BOT_DETECT=shadow` records verdicts to `lana_abuse_flags` with `blocked=false` and blocks nobody. We read the table for a week, tune, then flip to `on`. Same posture the repo already uses for `LANA_DECIDE_TURN=shadow`.

### 2.6 Tier mapping — a correction that must be reviewed

The agreed table says "Email signed-in / Phone-verified". The code cannot express that today: `AuthSession` collapses both into one boolean.

```python
    # Auth gate flag. Now fed by EMAIL verification (email OTP), not SMS. The name
    # is kept because ~50 downstream call sites in discovery_* read `phone_verified`
    # as the generic "is this a permanent, verified account?" signal.
    phone_verified: bool
```

and `_resolve_verified` returns true for **either** channel:

```python
    if profile.get("email_verified_at") or profile.get("phone_verified_at"):
        return True
```

`_load_user_profile` already selects both columns, and prod `public.users` has both `email_verified_at` and `phone_verified_at`, so a true three-way split is available — it just needs surfacing. This PR adds two **new** fields and does not touch `phone_verified` (50 call sites):

```python
@dataclass(frozen=True)
class AuthSession:
    ...
    # PR10: the tier ladder needs the CHANNEL, not just "verified". phone_verified
    # above stays exactly as-is — it means "permanent verified account" and ~50
    # discovery_* call sites depend on that meaning.
    email_verified: bool = False
    phone_channel_verified: bool = False
```

fed from the row `_load_user_profile` already fetches. Tier resolution:

| Auth state | Tier | Turns/day | At limit |
|---|---|---|---|
| `is_anonymous` | `anonymous` | **12** | Warm email ask (200, conversion) |
| `not is_anonymous`, `not phone_channel_verified` | `email` | **60** | Silent downgrade to `router_model()` |
| `phone_channel_verified` | `phone` | **200** | Silent downgrade to `router_model()` |
| any + active `blocked` flag | `blocked` | **0** | 429, no body detail, no LLM call, no DB write |

**Review question for Asjid:** with email OTP as the only live verification channel, the `phone` tier is currently unreachable except for legacy pre-migration accounts. Either (a) ship it anyway as forward-compat, or (b) collapse to 12 / 60 / 200-for-founders. This PR implements (a); the limits are env vars either way.

---

## 3. The migration

Full SQL in `PR10_rate_limiting.sql`. Three tables, five functions, all additive.

| Object | Purpose |
|---|---|
| `public.lana_rate_counters` | windowed hit counters, PK `(subject_kind, subject_id, metric, window_seconds, bucket_start)` |
| `public.lana_abuse_flags` | bot verdicts; `blocked=true` = tier 0; unique on `(subject_kind, subject_id, reason)` |
| `public.lana_turn_rhythm` | bounded ring buffer of inter-turn gaps + payload hashes (**no message content**, sha256 prefixes only) |
| `lana_rate_consume(kind, id, metric, limit, window, consume)` | the hot path — atomic increment + verdict, returns `{allowed, blocked, block_reason, used, limit, remaining, window_seconds, reset_at}` |
| `lana_rhythm_observe(kind, id, payload_hash, max_samples)` | records a turn, returns `{samples, cv, identical_ratio, burst, metronomic, templated, bot_like}` |
| `lana_abuse_flag_set(...)` / `lana_abuse_flag_clear(...)` | escalate / lift a flag (the operator escape hatch for a false positive) |
| `lana_rate_prune(interval)` | opportunistic GC — no cron at pilot scale, same posture as `/lana/area/progress` read-repair |

Atomicity matters and is handled in SQL, not Python:

```sql
    insert into public.lana_rate_counters as c
      (subject_kind, subject_id, metric, window_seconds, bucket_start, hits)
    values
      (p_subject_kind, p_subject_id, p_metric, p_window_seconds, v_bucket, 1)
    on conflict (subject_kind, subject_id, metric, window_seconds, bucket_start)
    do update set hits = c.hits + 1, last_at = now()
    returning c.hits into v_hits;
```

One statement, one row lock, post-increment value returned — two concurrent Cloud Run instances cannot both read "11 of 12".

A hard-blocked subject short-circuits **before** the counter write, so a blocked script cannot grow our tables:

```sql
  if coalesce(v_blocked, false) then
    return jsonb_build_object('allowed', false, 'blocked', true, ...);
  end if;
```

Privacy: `subject_id` for `subject_kind='ip'` is a truncated sha256, never a raw address — recorded in the column comment. RLS is enabled on all three tables with an explicit deny-all policy for `anon` and `authenticated`; only `service_role` is granted execute/DML. A client that could call `lana_rate_consume` could reset its own counter, so no client can.

---

## 4. Worker changes

### 4.1 New module `app/rate_limit.py`

```python
"""Request throttle + bot signals (PR10).

Counters live in Postgres (public.lana_rate_counters) because Cloud Run is
horizontally scaled and there is no Redis: an in-process counter resets on every
cold start and is per-instance, so N connections buy N quotas.

FAIL-OPEN by default. If the counter DB hiccups we serve the turn — a throttle
outage must never become a Lana outage. LANA_RATE_FAIL_OPEN=0 inverts that for
a live incident.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from typing import Any

from fastapi import Request

from app.auth import AuthSession, service_client

_log = logging.getLogger(__name__)

MODE_OFF, MODE_SHADOW, MODE_ON = "off", "shadow", "on"


def _mode(var: str, default: str) -> str:
    raw = os.environ.get(var, default).strip().lower()
    return raw if raw in (MODE_OFF, MODE_SHADOW, MODE_ON) else default


def rate_mode() -> str:
    return _mode("LANA_RATE_LIMIT", MODE_SHADOW)


def bot_mode() -> str:
    return _mode("LANA_BOT_DETECT", MODE_SHADOW)


def _fail_open() -> bool:
    return os.environ.get("LANA_RATE_FAIL_OPEN", "1").strip().lower() not in ("0", "false", "off")


def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, str(default)).strip()))
    except ValueError:
        return default


# ── subject identity ────────────────────────────────────────────────────────

def _hash(value: str, prefix: str) -> str:
    return f"{prefix}:{hashlib.sha256(value.encode('utf-8')).hexdigest()[:32]}"


def client_ip(request: Request | None) -> str | None:
    """Client IP from X-Forwarded-For.

    Cloud Run appends its own front-end hop, so the client is the value
    LANA_XFF_TRUSTED_HOPS from the right. XFF is attacker-controlled to the LEFT
    of our trusted hops, which is exactly why we count from the right.
    """
    if request is None:
        return None
    xff = request.headers.get("x-forwarded-for", "")
    parts = [p.strip() for p in xff.split(",") if p.strip()]
    if not parts:
        return request.client.host if request.client else None
    hops = _int_env("LANA_XFF_TRUSTED_HOPS", 1)
    idx = len(parts) - 1 - hops
    return parts[idx] if 0 <= idx < len(parts) else parts[0]


def ip_subject(request: Request | None) -> str | None:
    ip = client_ip(request)
    return _hash(ip, "ip") if ip else None


def device_subject(request: Request | None) -> str | None:
    """X-Lana-Device-Id when the PWA sends one; otherwise a UA+IP fingerprint so
    this ships with NO frontend change. Weak on purpose — it is one signal of
    three, never the sole basis for a block."""
    if request is None:
        return None
    explicit = (request.headers.get("x-lana-device-id") or "").strip()
    if explicit:
        return _hash(explicit[:128], "dev")
    ua = (request.headers.get("user-agent") or "")[:200]
    ip = client_ip(request) or ""
    return _hash(f"{ua}|{ip}", "fp") if (ua or ip) else None


def payload_hash(text: str) -> str:
    return hashlib.sha256((text or "").strip().lower().encode("utf-8")).hexdigest()[:32]


# ── tiers ───────────────────────────────────────────────────────────────

TIER_ANON, TIER_EMAIL, TIER_PHONE, TIER_BLOCKED = "anonymous", "email", "phone", "blocked"


def tier_for(auth: AuthSession) -> str:
    if auth.is_anonymous:
        return TIER_ANON
    if getattr(auth, "phone_channel_verified", False):
        return TIER_PHONE
    return TIER_EMAIL


def turn_limit(tier: str) -> int:
    return {
        TIER_ANON:  _int_env("LANA_TURNS_PER_DAY_ANON", 12),
        TIER_EMAIL: _int_env("LANA_TURNS_PER_DAY_EMAIL", 60),
        TIER_PHONE: _int_env("LANA_TURNS_PER_DAY_PHONE", 200),
    }.get(tier, 12)


@dataclass(frozen=True)
class Verdict:
    allowed: bool          # false only when the caller must be denied
    enforced: bool         # false in shadow mode — caller ignores `allowed`
    blocked: bool          # bot hard-block
    tier: str
    used: int
    limit: int
    remaining: int
    reset_at: str | None
    degrade: bool          # signed-in over quota → serve on the mini model


def _rpc(name: str, params: dict[str, Any]) -> dict[str, Any] | None:
    try:
        return service_client().rpc(name, params).execute().data
    except Exception:  # noqa: BLE001 — a throttle outage must not be a Lana outage
        _log.exception("rate_rpc_failed name=%s", name)
        return None


def consume(
    *, subject_kind: str, subject_id: str, metric: str,
    limit: int, window_seconds: int = 86400, tier: str = TIER_EMAIL,
) -> Verdict:
    mode = rate_mode()
    if mode == MODE_OFF or not subject_id:
        return Verdict(True, False, False, tier, 0, limit, limit, None, False)

    data = _rpc("lana_rate_consume", {
        "p_subject_kind": subject_kind,
        "p_subject_id": subject_id,
        "p_metric": metric,
        "p_limit": limit,
        "p_window_seconds": window_seconds,
        "p_consume": True,
    })
    if data is None:
        return Verdict(_fail_open(), False, False, tier, 0, limit, limit, None, False)

    allowed = bool(data.get("allowed"))
    blocked = bool(data.get("blocked"))
    used    = int(data.get("used") or 0)
    if not allowed:
        _log.warning(
            "rate_limit_hit mode=%s tier=%s kind=%s metric=%s used=%s limit=%s blocked=%s",
            mode, tier, subject_kind, metric, used, limit, blocked,
        )
    return Verdict(
        allowed=allowed,
        enforced=(mode == MODE_ON),
        blocked=blocked,
        tier=tier,
        used=used,
        limit=limit,
        remaining=int(data.get("remaining") or 0),
        reset_at=data.get("reset_at"),
        degrade=(not allowed and not blocked and tier in (TIER_EMAIL, TIER_PHONE)),
    )
```

### 4.2 Bot signals — cheapest first

```python
# Signal 1 (cheapest): session-creation rate per IP / device.
#   Handled entirely by consume(metric="session_create"). No extra call. A repeat
#   offender gets a session_flood flag.
#
# Signal 2: inter-message latency variance. Humans are irregular; scripts are
#   metronomic. Computed in SQL from a 16-sample ring buffer.
#
# Signal 3: identical / templated payloads. sha256 prefix only — no content.

def observe_turn(*, subject_kind: str, subject_id: str, message: str) -> dict[str, Any]:
    """Record the turn and return the behavioural verdict. Never raises, never
    blocks — the caller applies policy."""
    if bot_mode() == MODE_OFF or not subject_id:
        return {}
    data = _rpc("lana_rhythm_observe", {
        "p_subject_kind": subject_kind,
        "p_subject_id": subject_id,
        "p_payload_hash": payload_hash(message),
        "p_max_samples": 16,
    }) or {}

    if not data.get("bot_like"):
        return data

    reason = (
        "metronomic" if data.get("metronomic")
        else "templated_payload" if data.get("templated")
        else "burst"
    )
    enforce = (bot_mode() == MODE_ON)
    _rpc("lana_abuse_flag_set", {
        "p_subject_kind": subject_kind,
        "p_subject_id": subject_id,
        "p_reason": reason,
        "p_signal": data,
        # SHADOW: record the verdict, block nobody. Flip LANA_BOT_DETECT=on only
        # after a week of reading lana_abuse_flags for false positives.
        "p_blocked": enforce,
        "p_ttl": f"{_int_env('LANA_BOT_BLOCK_HOURS', 24)} hours",
    })
    _log.warning(
        "bot_signal mode=%s kind=%s reason=%s cv=%s identical=%s samples=%s",
        bot_mode(), subject_kind, reason,
        data.get("cv"), data.get("identical_ratio"), data.get("samples"),
    )
    return data
```

Thresholds (all in SQL so they are one place, and deliberately conservative):

| Signal | Rule | Min samples |
|---|---|---|
| `burst` | any gap `< 400 ms` | 6 |
| `metronomic` | coefficient of variation of gaps `< 0.15` | 6 |
| `templated` | `1 − distinct(hashes)/count(hashes) ≥ 0.60` | 5 |

Gaps over 10 minutes are discarded before the statistics — that is a new sitting, not a typing rhythm, and including it would make every real user look irregular in a way that masks nothing and helps nobody.

### 4.3 Enforcement point 1 — `POST /lana/sessions`

```python
@app.post("/lana/sessions", response_model=CreateSessionResponse)
def create_lana_session(
    body: CreateSessionRequest,
    request: Request,                                  # NEW — for the client IP
    authorization: str | None = Header(default=None),
    accept_language: str | None = Header(default=None),
    cf_turnstile_response: str | None = Header(default=None, alias="cf-turnstile-response"),
):
    _vertex_required()
    auth = verify_auth(authorization)

    # PR10 · session-creation throttle. force_new=true mints a brand-new LLM
    # opening on every call, so this is the cheapest way to burn budget.
    require_turnstile(request, cf_turnstile_response, auth)      # no-op unless flagged on
    guard_session_create(request, auth)

    require_home_block_for_purpose(auth, body.purpose)
```

```python
def guard_session_create(request: Request, auth: AuthSession) -> None:
    """Per-IP AND per-device, 1 h window. Deliberately not per-user: an attacker
    mints a fresh anonymous user for every request, so user_id is worthless here."""
    tier = tier_for(auth)
    for kind, subject, limit in (
        ("ip",     ip_subject(request),     _int_env("LANA_SESSIONS_PER_HOUR_IP", 10)),
        ("device", device_subject(request), _int_env("LANA_SESSIONS_PER_HOUR_DEVICE", 5)),
    ):
        if not subject:
            continue
        v = consume(subject_kind=kind, subject_id=subject, metric="session_create",
                    limit=limit, window_seconds=3600, tier=tier)
        if v.allowed:
            continue
        if not v.blocked and v.used >= limit * 3:
            # Persistently over 3x the ceiling is not a shared NAT, it is a script.
            _rpc("lana_abuse_flag_set", {
                "p_subject_kind": kind, "p_subject_id": subject,
                "p_reason": "session_flood",
                "p_signal": {"used": v.used, "limit": limit},
                "p_blocked": (bot_mode() == MODE_ON),
                "p_ttl": "24 hours",
            })
        if v.enforced or v.blocked:
            # Machine-facing surface: 429, no explanation, no Lana copy.
            raise HTTPException(status_code=429, detail="rate_limited")
```

### 4.4 Enforcement point 2 — the turn

The gate runs **before** `insert_message`, so a blocked script writes nothing to `lana_messages`.

```python
def _run_lana_message(
    session_id: str,
    body: SendMessageRequest,
    background_tasks: BackgroundTasks,
    authorization: str | None,
    emit: Callable[[str, str | None], None] | None = None,
    accept_language: str | None = None,
    request: Request | None = None,                    # NEW
) -> SendMessageResponse:
    _vertex_required()
    timer = TurnTimer()
    if emit is not None:
        timer.set_emitter(emit)
    auth = verify_auth(authorization)

    with timer.stage("db_load_session"):
        session = get_session_for_user(session_id, auth.user_id)
    if session.get("status") != "active":
        raise HTTPException(status_code=400, detail="session_not_active")

    purpose = str(session.get("purpose", "profile_intake"))
    session_ctx_in = dict(session.get("context") or {})

    # ── PR10 · turn quota + bot signals. BEFORE insert_message: a hard-blocked
    # script must not be able to grow lana_messages or trigger an embedding.
    with timer.stage("rate_gate"):
        gate = guard_turn(request, auth, body.message, session_ctx_in)
    if gate.blocked and gate.enforced:
        raise HTTPException(status_code=429, detail="rate_limited")   # silent
    if gate.wall:
        # Anonymous at the wall — a real 200 turn that converts, not an error.
        return _quota_conversion_turn(
            session_id=session_id, auth=auth, session_ctx=session_ctx_in,
            accept_language=accept_language,
        )
    if gate.degrade:
        # Signed-in over quota — silent mini-model turn. Nothing user-visible.
        set_degrade_to_mini(True)

    require_home_block_for_purpose(auth, purpose)
    with timer.stage("db_save_user_message"):
        user_msg_id = insert_message(session_id, "user", body.message.strip(), {}, embed=False)
```

`guard_turn` composes the pieces:

```python
def guard_turn(request, auth, message: str, session_ctx: dict) -> TurnGate:
    tier = tier_for(auth)
    v = consume(subject_kind="user", subject_id=auth.user_id, metric="turn",
                limit=turn_limit(tier), window_seconds=86400, tier=tier)

    # Behavioural signals on BOTH the user and the device: a script that mints a
    # fresh anonymous user per turn defeats the user counter but not the device.
    observe_turn(subject_kind="user", subject_id=auth.user_id, message=message)
    dev = device_subject(request)
    if dev:
        observe_turn(subject_kind="device", subject_id=dev, message=message)

    return TurnGate(
        blocked=v.blocked,
        enforced=v.enforced,
        # The anonymous wall only fires when enforcement is ON. In shadow we log
        # and serve, so we can size the conversion before we ship it.
        wall=(v.enforced and not v.allowed and not v.blocked and tier == TIER_ANON),
        degrade=(v.enforced and v.degrade),
    )
```

The silent downgrade is a context-local flag read by `synthesizer_model()`. It is set inside `_run_lana_message`, which is also the function the SSE endpoint runs on its worker thread, so it is correct for both transports (a `threading.Thread` does not inherit a parent's `contextvars`):

```python
# app/orchestrator/llm.py
_DEGRADE_TO_MINI: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "lana_degrade_to_mini", default=False
)


def set_degrade_to_mini(on: bool) -> None:
    _DEGRADE_TO_MINI.set(bool(on))


def synthesizer_model() -> str:
    # PR10: over-quota signed-in users are served on the router tier. Silent by
    # design — a two-sentence reply is indistinguishable, and blocking our most
    # engaged users to save cents is a product own-goal.
    if _DEGRADE_TO_MINI.get():
        return router_model()
    ...   # unchanged
```

### 4.5 The anonymous wall — copy

Constraints: `app/lingo_guard.py` bans `mom(s)/mommy/mama/mum`, `block(s)`, `circle(s)`, `leaderboard/streak/level up`, and person-noun "a match" from anything user-facing. Every line below is clean under `_BANNED_RE` and `_MATCH_PERSON_RE`, and **every line ends in an offered next step**.

```python
_QUOTA_ASK_FIRST = (
    "I want to keep everything you've told me — right now it only lives in this "
    "tab. Give me your email and I'll save all of it, then we pick up exactly "
    "here. What address should I use?"
)

_QUOTA_ASK_AGAIN = (
    "Still here with you — I just need an email before we go further, and "
    "everything we've talked about is saved the moment you give it. The button "
    "below takes about ten seconds."
)


def _quota_conversion_turn(*, session_id, auth, session_ctx, accept_language) -> SendMessageResponse:
    """Anonymous at the daily wall. HTTP 200, a real Lana turn, and the existing
    email-verify affordance. This is the signup conversion — NOT an error."""
    n = int(session_ctx.get("quota_ask_count") or 0)
    reply = compose_reply(
        goal=(
            "The person has been talking with you all day as a guest and their "
            "guest allowance is used up. Warmly ask for their email so you can "
            "save the conversation and carry on. Never say no, never end the "
            "conversation, always give them the next step."
        ),
        fallback=(_QUOTA_ASK_FIRST if n == 0 else _QUOTA_ASK_AGAIN),
        session_ctx=session_ctx,
        max_sentences=2,
    )
    ctx = {
        **session_ctx,
        "quota_ask_count": n + 1,
        "requires_phone_verification": True,   # the FE's email-verify button
        "guest_intake": True,
        "guest_step": GUEST_STEP_PHONE,        # hand off to the shipped signup lane
    }
    update_session_context(session_id, ctx)
    return SendMessageResponse(
        session_id=session_id,
        status="continue",
        assistant_message=finalize_reply_language(reply, ctx),
        requires_phone_verification=True,
        is_anonymous=True,
        ui_actions=[UiActionRow(
            id="quota_email", label="Use my email", message="I'll use my email",
            style="primary",
        )],
    )
```

`compose_reply` already returns the `fallback` verbatim when the LLM is unconfigured or fails, and the reply passes the same final-mile localizer + lingo guard as every other turn. **The wall turn itself makes no synthesizer call**, so the endpoint is genuinely cheap once the wall is up.

### 4.6 Enforcement point 3 — the Google-billed endpoints

```python
@app.post("/lana/places/search", response_model=PlaceSearchResponse)
def search_places_endpoint(
    body: PlaceSearchRequest,
    request: Request,                                  # NEW
    authorization: str | None = Header(default=None),
):
    auth = verify_auth(authorization)
    # PR10 · Places API (New) Text Search is billed per request. Over quota we
    # return [] — the SAME contract places.py already documents for a missing
    # key ("Returns [] when the key isn't configured"), which the tip-share flow
    # already degrades gracefully to free-type + AI chips. No new copy, no dead
    # end, no error surface.
    if not guard_places(request, auth):
        return PlaceSearchResponse(results=[])

    from app.places import search_places
    rows = search_places(query=body.q, block_id=auth.home_block_id, user_id=auth.user_id)
    return PlaceSearchResponse(results=[PlaceResult(**r) for r in rows])


@app.post("/lana/places/reverse-geocode", response_model=PlaceSearchResponse)
def reverse_geocode_endpoint(
    body: ReverseGeocodeRequest,
    request: Request,                                  # NEW
    authorization: str | None = Header(default=None),
):
    auth = verify_auth(authorization)   # was: verify_auth(authorization) — result discarded,
                                        # so there was no per-user attribution to throttle on
    if not guard_places(request, auth):
        return PlaceSearchResponse(results=[])
    ...
```

```python
def guard_places(request: Request, auth: AuthSession) -> bool:
    """Per-user daily + per-IP hourly. Returns False when the call must be skipped."""
    tier = tier_for(auth)
    per_user = _int_env(
        "LANA_PLACES_PER_DAY_ANON" if tier == TIER_ANON else "LANA_PLACES_PER_DAY_USER",
        20 if tier == TIER_ANON else 100,
    )
    v = consume(subject_kind="user", subject_id=auth.user_id, metric="places_call",
                limit=per_user, window_seconds=86400, tier=tier)
    if v.blocked:
        return False
    if v.enforced and not v.allowed:
        return False
    ip = ip_subject(request)
    if ip:
        vi = consume(subject_kind="ip", subject_id=ip, metric="places_call",
                     limit=_int_env("LANA_PLACES_PER_HOUR_IP", 200),
                     window_seconds=3600, tier=tier)
        if (vi.enforced and not vi.allowed) or vi.blocked:
            return False
    return True
```

### 4.7 Cloudflare Turnstile — wired, flagged, OFF

Designed for `POST /lana/sessions` and shipped inert, so no frontend change is required to merge this PR.

```python
def turnstile_enabled() -> bool:
    return (
        os.environ.get("LANA_TURNSTILE_ENABLED", "0").strip().lower() in ("1", "true", "on")
        and bool(os.environ.get("LANA_TURNSTILE_SECRET", "").strip())
    )


def require_turnstile(request: Request, token: str | None, auth: AuthSession) -> None:
    """No-op unless BOTH the flag and the secret are set. When live, only
    anonymous session creation is challenged — a verified account has already
    proved it is a person."""
    if not turnstile_enabled() or not auth.is_anonymous:
        return
    if not token:
        raise HTTPException(status_code=428, detail="turnstile_required")
    try:
        res = httpx.post(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data={
                "secret": os.environ["LANA_TURNSTILE_SECRET"],
                "response": token,
                "remoteip": client_ip(request) or "",
            },
            timeout=5.0,
        )
        ok = bool(res.json().get("success"))
    except Exception:  # noqa: BLE001
        _log.exception("turnstile_verify_failed")
        ok = _fail_open()      # a Cloudflare outage must not close signup
    if not ok:
        raise HTTPException(status_code=403, detail="turnstile_failed")
```

Frontend contract when we do turn it on: render the widget on first app open and send the token as the `cf-turnstile-response` header on `POST /lana/sessions`. `428 turnstile_required` is the signal to show the challenge. Nothing else changes.

### 4.8 Opportunistic GC

No cron exists at pilot scale — `/lana/area/progress` already documents that posture. Same trick:

```python
def maybe_prune() -> None:
    """~1 in 500 turns, in a BackgroundTask so it never touches turn latency."""
    if random.randint(1, _int_env("LANA_RATE_PRUNE_EVERY", 500)) != 1:
        return
    _rpc("lana_rate_prune", {"p_older_than": "7 days"})
```

### 4.9 `/health`

```python
        "rate_limit_mode": rate_mode(),
        "bot_detect_mode": bot_mode(),
        "turnstile_enabled": turnstile_enabled(),
        "turn_limits": {"anonymous": 12, "email": 60, "phone": 200},   # from env
```

### 4.10 Env additions (`deploy/lana-worker.env.example`)

```bash
# ── Rate limiting + bot detection (PR10) ───────────────────────────────────
# off    = no counting at all (full rollback)
# shadow = count + log + record verdicts, block NOBODY   ← ships as this
# on     = enforce
LANA_RATE_LIMIT=shadow
LANA_BOT_DETECT=shadow

# Daily turn quota per tier.
LANA_TURNS_PER_DAY_ANON=12
LANA_TURNS_PER_DAY_EMAIL=60
LANA_TURNS_PER_DAY_PHONE=200

# Session creation (1 h window). force_new=true mints a fresh LLM opening.
LANA_SESSIONS_PER_HOUR_IP=10
LANA_SESSIONS_PER_HOUR_DEVICE=5

# Google Places — billed per request.
LANA_PLACES_PER_DAY_USER=100
LANA_PLACES_PER_DAY_ANON=20
LANA_PLACES_PER_HOUR_IP=200

# A throttle outage must not be a Lana outage. 0 = fail closed (incident only).
LANA_RATE_FAIL_OPEN=1

# Trusted proxy hops in X-Forwarded-For. Cloud Run direct = 1.
LANA_XFF_TRUSTED_HOPS=1

# Bot block TTL, and the GC sampling rate.
LANA_BOT_BLOCK_HOURS=24
LANA_RATE_PRUNE_EVERY=500

# Cloudflare Turnstile on POST /lana/sessions. OFF — needs a frontend widget
# first. Both must be set for it to engage.
LANA_TURNSTILE_ENABLED=0
# LANA_TURNSTILE_SECRET=
```

---

## 5. Test plan

### 5.1 SQL — already run against PROD

Executed against `kmetmatfxdkrialwrnzj` inside `begin; … rollback;`, 13 assertion groups, all passed (`ALL PR10 ASSERTIONS PASSED`, final select `PR10 verified`, 4 counter rows / 1 rhythm row created and discarded). Post-check confirmed **0 leftover tables, 0 leftover functions, public table count back to 76**. Nothing was committed.

Covered:

1. 11 consumes under a limit of 12 → `allowed=true`, `used=11`
2. 12th allowed, 13th denied, `remaining=0`
3. `p_consume=false` peeks without incrementing
4. `reset_at` is in the future and window-aligned
5. a different `(kind, metric, window)` is an independent bucket
6. `p_limit=null` counts but always allows (shadow mode)
7. a `blocked` flag short-circuits, **and writes no counter row**
8. flag re-set escalates and increments `hits`; never de-escalates
9. `lana_abuse_flag_clear` lifts the block and traffic flows again
10. first rhythm sample yields no verdict (`samples=0`, `bot_like=false`)
11. 10 back-to-back calls → `burst=true`, `templated=true`, `bot_like=true`
12. 60 calls with unique hashes → ring buffers stay ≤ 16 and `templated=false` (the false-positive direction)
13. empty `subject_id` raises `P0001`

### 5.2 New worker tests — `tests/test_rate_limit.py`

`unittest`, matching the existing 75-file suite. All RPCs mocked; no network.

```python
class TestSubjectIdentity(unittest.TestCase):
    def test_xff_takes_the_trusted_hop_from_the_right(self):
        r = _req({"x-forwarded-for": "1.2.3.4, 10.0.0.1"})
        self.assertEqual(rl.client_ip(r), "1.2.3.4")

    def test_spoofed_left_hand_xff_is_ignored(self):
        r = _req({"x-forwarded-for": "9.9.9.9, 1.2.3.4, 10.0.0.1"})
        os.environ["LANA_XFF_TRUSTED_HOPS"] = "1"
        self.assertEqual(rl.client_ip(r), "1.2.3.4")   # NOT 9.9.9.9

    def test_ip_is_never_stored_raw(self):
        self.assertNotIn("1.2.3.4", rl.ip_subject(_req({"x-forwarded-for": "1.2.3.4, x"})))

    def test_device_falls_back_to_ua_fingerprint_without_a_header(self):
        self.assertTrue(rl.device_subject(_req({"user-agent": "Mozilla/5.0"})).startswith("fp:"))


class TestTiers(unittest.TestCase):
    def test_anonymous_is_12(self):        self.assertEqual(rl.turn_limit(rl.TIER_ANON), 12)
    def test_email_is_60(self):            self.assertEqual(rl.turn_limit(rl.TIER_EMAIL), 60)
    def test_phone_is_200(self):           self.assertEqual(rl.turn_limit(rl.TIER_PHONE), 200)
    def test_env_override(self):
        os.environ["LANA_TURNS_PER_DAY_ANON"] = "3"
        self.assertEqual(rl.turn_limit(rl.TIER_ANON), 3)


class TestModes(unittest.TestCase):
    def test_shadow_never_enforces(self):
        os.environ["LANA_RATE_LIMIT"] = "shadow"
        v = rl.consume(..., limit=1)   # rpc mocked to allowed=false
        self.assertFalse(v.enforced)   # caller must serve the turn

    def test_off_makes_no_rpc_call(self):
        os.environ["LANA_RATE_LIMIT"] = "off"
        with mock.patch.object(rl, "_rpc") as r:
            rl.consume(subject_kind="user", subject_id="u", metric="turn", limit=12)
        r.assert_not_called()

    def test_rpc_failure_fails_open(self):
        os.environ["LANA_RATE_LIMIT"] = "on"
        with mock.patch.object(rl, "_rpc", return_value=None):
            self.assertTrue(rl.consume(subject_kind="user", subject_id="u",
                                       metric="turn", limit=12).allowed)

    def test_rpc_failure_can_fail_closed(self):
        os.environ["LANA_RATE_FAIL_OPEN"] = "0"
        ...
        self.assertFalse(v.allowed)


class TestWallCopy(unittest.TestCase):
    """The wall is a conversion, and it must survive the lingo guard."""

    def test_copy_is_lingo_clean(self):
        from app.lingo_guard import enforce
        for line in (rl._QUOTA_ASK_FIRST, rl._QUOTA_ASK_AGAIN):
            self.assertEqual(enforce(line).text, line)   # unchanged = no violation

    def test_copy_never_says_mom_block_or_circle(self):
        from app.lingo_guard import find_violations
        for line in (rl._QUOTA_ASK_FIRST, rl._QUOTA_ASK_AGAIN, "Use my email"):
            self.assertEqual(find_violations(line), [])   # verified 2026-07-30

    def test_copy_always_offers_a_next_step(self):
        for line in (rl._QUOTA_ASK_FIRST, rl._QUOTA_ASK_AGAIN):
            self.assertTrue(any(w in line.lower() for w in ("email", "button", "address")))

    def test_wall_returns_200_not_429(self):
        """Anonymous at the limit is a signup moment, not an error."""
        resp = main._quota_conversion_turn(...)
        self.assertTrue(resp.requires_phone_verification)
        self.assertTrue(resp.assistant_message)
        self.assertEqual(len(resp.ui_actions), 1)

    def test_wall_makes_no_synthesizer_call(self):
        with mock.patch("app.orchestrator.llm.llm_json") as j:
            main._quota_conversion_turn(...)
        # compose_reply may call it; the ORCHESTRATOR pipeline must not.
        self.assertLessEqual(j.call_count, 1)


class TestDegrade(unittest.TestCase):
    def test_over_quota_signed_in_gets_the_mini_model(self):
        llm.set_degrade_to_mini(True)
        self.assertEqual(llm.synthesizer_model(), llm.router_model())
        llm.set_degrade_to_mini(False)

    def test_degrade_is_silent(self):
        """No copy, no field, no ui_action distinguishes a degraded turn."""
        ...
```

### 5.3 Integration, on staging

| # | Scenario | Expected |
|---|---|---|
| 1 | `LANA_RATE_LIMIT=shadow`, script 50 anonymous turns | All 50 served. `lana_rate_counters` shows `hits=50`. `rate_limit_hit` in logs from turn 13. **No user impact.** |
| 2 | `LANA_RATE_LIMIT=on`, anonymous, turns 1-12 | Normal turns |
| 3 | same, turn 13 | **HTTP 200**, `requires_phone_verification=true`, warm email ask, one `ui_action`. Not 429. |
| 4 | turn 14, 15 | The second variant, still 200, still offers the button. Never a dead end, never a repeat-verbatim loop. |
| 5 | supply the email → verify → turn 16 | Normal turn, tier now `email`, counter is a fresh `user` row against limit 60 |
| 6 | verified account, turn 61 | Normal 200 turn. `synth_model` in the timing log is the router model. Nothing user-visible. |
| 7 | 11 × `POST /lana/sessions` from one IP in an hour | 11th → `429 rate_limited`. A `session_flood` flag appears once past 3× |
| 8 | 10 turns at exactly 1 s intervals with an identical body | `lana_abuse_flags` gains `metronomic` + `templated_payload`. `blocked=false` in shadow |
| 9 | flip `LANA_BOT_DETECT=on`, repeat 8 | `blocked=true`; next request → `429 rate_limited` with no explanatory body, **no LLM call, no `lana_messages` row** |
| 10 | `lana_abuse_flag_clear('device', …)` | Traffic resumes immediately |
| 11 | 101 × `/lana/places/search` in a day | 101st returns `{"results": []}` — the flow degrades to free-type chips, exactly as it does with no Maps key |
| 12 | `LANA_TURNSTILE_ENABLED=0` (default) with no token | No effect at all — proves it ships inert |
| 13 | `LANA_TURNSTILE_ENABLED=1` + secret, anonymous, no token | `428 turnstile_required` |
| 14 | break the Supabase connection, `LANA_RATE_LIMIT=on` | Turns still serve (fail-open), `rate_rpc_failed` logged |
| 15 | two Cloud Run instances, concurrent turns 12 and 13 | Exactly one wall. No double-spend |

### 5.4 Rollout sequence

1. Merge SQL. Deploy worker with `LANA_RATE_LIMIT=shadow`, `LANA_BOT_DETECT=shadow`, `LANA_TURNSTILE_ENABLED=0`. **Zero user-visible change.**
2. Run 3-7 days. Query the real distribution:
   ```sql
   select metric, window_seconds,
          percentile_cont(0.50) within group (order by hits) as p50,
          percentile_cont(0.95) within group (order by hits) as p95,
          percentile_cont(0.99) within group (order by hits) as p99,
          max(hits)
   from public.lana_rate_counters group by 1, 2;

   select reason, count(*), min(created_at), max(updated_at)
   from public.lana_abuse_flags group by 1;
   ```
   If p99 anonymous turns/day is above 12, **raise the limit before enforcing** — the number in the agreed table is a guess and the data is not.
3. Flip `LANA_RATE_LIMIT=on`. Watch the anonymous→email conversion rate at the wall; that is the metric that justifies the design.
4. Flip `LANA_BOT_DETECT=on` only after the shadow flags contain zero recognisable humans.
5. Turnstile last, once the PWA renders the widget.

---

## 6. Rollback

| Level | Action | Effect | Time |
|---|---|---|---|
| 1 | `--update-env-vars LANA_RATE_LIMIT=off` | No counting, no gating, no RPC. Identical to pre-PR behaviour. | seconds |
| 2 | `--update-env-vars LANA_BOT_DETECT=off` | Bot signals off; quotas keep working. | seconds |
| 3 | `--update-env-vars LANA_RATE_LIMIT=shadow` | Keep the data, stop enforcing. The preferred "something is wrong" setting. | seconds |
| 4 | raise a single limit, e.g. `LANA_TURNS_PER_DAY_ANON=100` | Widen without a deploy. | seconds |
| 5 | `select public.lana_abuse_flag_clear('device','dev:…')` | Lift one false-positive block. | seconds |
| 6 | `delete from public.lana_abuse_flags where blocked;` | Lift every block at once. | seconds |
| 7 | previous Cloud Run revision | Full code rollback. Tables stay; nothing else reads them. | ~1 min |
| 8 | `drop table public.lana_rate_counters, public.lana_abuse_flags, public.lana_turn_rhythm cascade;` + `drop function` × 5 | Full schema rollback. **Only after level 7** — no other object depends on these. | ~1 min |

**No existing table, column, function, policy or grant is modified.** Levels 1-6 need no deploy.

---

## 7. Known gaps — stated, not hidden

1. **`/hooks/event-join`, `/hooks/event-decision`, `/hooks/event-cancel` are not throttled by this PR.** They fan out real push notifications and real emails to real people, which is arguably a worse abuse surface than LLM spend. They need a per-recipient throttle, not a per-caller one, and that is a different design. **Flagged for a follow-up PR.**
2. **Device identity is weak without a frontend change.** The UA+IP fingerprint collides across a household and changes on a browser update. It is deliberately one of three signals and never the sole basis for a block. `X-Lana-Device-Id` upgrades it with two lines of PWA code.
3. **XFF is spoofable to the left of our trusted hops.** Counting from the right is the correct mitigation for Cloud Run, but `LANA_XFF_TRUSTED_HOPS` must be re-checked if a load balancer or Cloudflare is ever put in front — a wrong value silently makes per-IP limiting useless.
4. **An attacker who mints a fresh anonymous user per turn defeats the per-user turn counter.** That is exactly why the session-create limit is per-IP/device and why the rhythm signals also run on the device subject. Turnstile is the real answer, and it is wired but off.
5. **The `phone` tier is currently unreachable** in a fresh account — see §2.6. Needs Asjid's decision.
6. **The 12/60/200 numbers are unvalidated.** Step 2 of §5.4 exists specifically to replace them with measured p99s before anything is enforced.
