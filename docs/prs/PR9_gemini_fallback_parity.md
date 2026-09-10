# PR9 · Gemini fallback parity

**Status:** ready for review · code-only (no migration)
**Repo:** `Phygtl-Inc/TagAlng-Backend` · path `services/lana-worker`
**Worker version:** 0.5.4 (OpenAPI) / 0.5.3 (root literal — see §1.5)
**Feature flag:** `LANA_LLM_FALLBACK=1|0` (rollback = `0`, one Cloud Run env var, no redeploy of code)
**Date:** 2026-07-30
**Verified against:** `Phygtl-Inc/TagAlng-Backend@main`, cloned and read in full. Every code block in §1 is verbatim from that tree.

---

## 0. The complaint

Asjid, 2026-07-30 standup, verbatim:

> "that fallback was added because initially we were on Gemini and then when we moved away from it to OpenAI, I implemented it in a way that Gemini remained just for fallback. But it does make sense that we have changed few things like a token max amount and even a timeout limit which doesn't really apply on Gemini. So what we can do here is make sure those functions also fall back when we have something on OpenAI."

He is right, and the situation is worse than he described in three specific ways. The audit below found **four** defects, one of which is a live 502.

---

## 1. Root cause

### 1.1 There is no failover. The "fallback" is a *configuration* branch, not an *error* branch.

`services/lana-worker/app/orchestrator/llm.py` :: `llm_json()` — the single entry point every composer uses:

```python
def llm_json(
    *,
    model: str,
    system: str,
    user_payload: str,
    max_tokens: int = 1024,
    temperature: float = 0.2,
    llm_attempts: list[int] | None = None,
) -> dict[str, Any]:
    p = provider()
    if p == "openai":
        data, attempts = _openai_json(
            model=model,
            system=system,
            user_payload=user_payload,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        if llm_attempts is not None:
            llm_attempts[:] = [attempts]
        return data
    if p == "claude":
        ...
    data, attempts = _gemini_json(...)
```

`provider()` is read from `LANA_LLM_PROVIDER`. In prod it is `openai`. **There is no `try` around `_openai_json`, and no path from the OpenAI branch to the Gemini branch.** A transport failure on OpenAI does not reach Gemini — ever.

The only retry inside `_openai_json` catches JSON problems, not transport problems:

```python
    try:
        return parse_json_object(text), attempts
    except (json.JSONDecodeError, ValueError):
        attempts = 2
        retry_text = _openai_generate(...)
```

`openai.APITimeoutError`, `openai.RateLimitError` (429), `openai.InternalServerError` (5xx) and `openai.APIConnectionError` are **none of** `JSONDecodeError` or `ValueError`. They propagate out of `llm_json` → out of `run_turn` / `run_lana_unified_pipeline` → into the blanket handler in `app/main.py`:

```python
    except Exception as exc:
        # Log the full traceback to the console — otherwise the caller only sees a
        # truncated 502 string and the real stack (where it actually broke) is lost.
        _LOG.exception(
            "lana_message_failed (purpose=%s session=%s)", purpose, session_id
        )
        raise HTTPException(
            status_code=502,
            detail=_vertex_error_detail("lana_message_failed", exc),
        ) from exc
```

with

```python
def _vertex_error_detail(prefix: str, exc: Exception) -> str:
    msg = str(exc).replace("\n", " ")[:500]
    return f"{prefix}:{type(exc).__name__}:{msg}"
```

**Answering the three questions asked:**

| Trigger | Does the fallback fire? | Silent to the user? | Logged? |
|---|---|---|---|
| OpenAI timeout (`APITimeoutError`) | **No** — on the orchestrator hot path | **No** — HTTP 502, body contains the exception class name and 500 chars of the OpenAI error string | Yes, `_LOG.exception("lana_message_failed …")` — but as a *crash*, not as a fallback event |
| OpenAI 429 (`RateLimitError`) | **No** | **No** — same 502 | same |
| OpenAI 5xx (`InternalServerError`) | **No** | **No** — same 502 | same |
| OpenAI returns unparseable JSON | Partially — retries twice on OpenAI, then downshifts to `router_model()`, still OpenAI | Yes | attempt count only |
| `llm_configured()` false (no `OPENAI_API_KEY`) | Yes, on ~6 helper composers only | Yes | Yes |

The **helper** composers do have an error branch, and those are the ones Asjid remembers writing. Example, `app/vertex_extract.py` :: `incremental_claims_from_utterance`:

```python
    try:
        from app.orchestrator.llm import llm_configured, llm_json, router_model

        if llm_configured():
            return llm_json(
                model=router_model(),
                system=system,
                user_payload=text,
                max_tokens=512,
                temperature=0.2,
            )
    except Exception:
        log.exception("llm_incremental_claim_extract_failed")
    return vertex_extract_claims_from_utterance(text, existing_labels, recent_questions)
```

and `app/rapport_synth.py` :: `_generate`:

```python
    except Exception:
        logger.exception("rapport-synth: orchestrator llm failed")

    # Vertex Gemini fallback.
    try:
        ...
        client = _vertex_client()
        model = os.environ.get("VERTEX_EXTRACT_MODEL", "gemini-2.5-flash")
        response = client.models.generate_content(
```

and `app/latent_extract.py` :: `extract_entities_from_message`:

```python
    # Fallback: direct Vertex Flash call (same model the claim extractor falls back to).
```

So: **the fallback exists on four low-value background composers and on zero hot-path calls.** The router, the synthesizer, the discovery classifier, the policy decider, the lingo guard and every `compose_reply` have no fallback at all.

### 1.2 The Vertex path honours none of the OpenAI-tuned limits — this is Asjid's actual point

`_openai_client()` sets a timeout on the client:

```python
def _openai_timeout_sec() -> float:
    raw = os.environ.get("OPENAI_TIMEOUT_SEC", "60").strip()
    try:
        return max(5.0, float(raw))
    except ValueError:
        return 60.0


def _openai_client():
    global _openai_client_instance
    if _openai_client_instance is not None:
        return _openai_client_instance
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set")
    from openai import OpenAI

    _openai_client_instance = OpenAI(api_key=api_key, timeout=_openai_timeout_sec())
    return _openai_client_instance
```

Prod sets `OPENAI_TIMEOUT_SEC=15`.

`_gemini_client()` sets **nothing**:

```python
def _gemini_client():
    global _gemini_client_instance
    if _gemini_client_instance is not None:
        return _gemini_client_instance
    project = os.environ.get("GCP_VERTEX_PROJECT", "")
    location = os.environ.get("GCP_VERTEX_LOCATION", "us-central1")
    if not project:
        raise RuntimeError("GCP_VERTEX_PROJECT not set")
    from google import genai

    _gemini_client_instance = genai.Client(vertexai=True, project=project, location=location)
    return _gemini_client_instance
```

`google-genai` accepts `http_options=types.HttpOptions(timeout=<milliseconds>, retry_options=types.HttpRetryOptions(...))` on both the `Client` and per-request `GenerateContentConfig`. **Verified against the installed SDK** (`google-genai 1.75.0`): `HttpOptions.model_fields` = `['base_url', 'base_url_resource_scope', 'api_version', 'headers', 'timeout', 'client_args', 'async_client_args', 'extra_body', 'retry_options', 'httpx_client', 'httpx_async_client', 'aiohttp_client']`, `timeout` documented as *"Timeout for the request in milliseconds"*, and `GenerateContentConfig` does accept `http_options`. **Neither is used anywhere in the repo.** A hung Vertex call blocks a Cloud Run request thread until the platform's own 300 s cap.

Full inventory of every `client.models.generate_content(` call in the worker (10 sites) and what each one sets:

| File · function | `max_output_tokens` | timeout | retry on bad JSON | JSON parser |
|---|---|---|---|---|
| `orchestrator/llm.py:243` `_gemini_generate` | ✅ `max_tokens` | ❌ | ✅ 3-step (`_gemini_json`) | `parse_json_object` |
| `vertex_lana.py:117` `_call_lana` | ❌ | ❌ | ❌ | **`json.loads`** |
| `vertex_extract.py:337` `vertex_extract_claims_from_utterance` | ❌ | ❌ | ❌ | `parse_json_object` |
| `vertex_extract.py:394` `vertex_extract_from_transcript` | ❌ | ❌ | ❌ | `parse_json_object` |
| `vertex_event_extract.py:56` `vertex_extract_event_from_transcript` | ❌ | ❌ | ❌ | `parse_json_object` |
| `vertex_event.py:290` `_call_event_lana` (vertex branch) | ✅ `1024` | ❌ | ✅ 2-step | `parse_json_object` |
| `profile_intake.py:474` `_call_profile_lana` | ✅ `2048` | ❌ | ✅ 2-step + truncation check | `parse_json_object` |
| `rapport_reply.py:284` `_vertex_concierge_reply` | ❌ | ❌ | ❌ | `parse_json_object` |
| `rapport_synth.py:281` `_generate` (vertex fallback) | ❌ | ❌ | ❌ | `parse_json_object` |
| `latent_extract.py:160` `extract_entities_from_message` (fallback) | ❌ | ❌ | ❌ | `parse_json_object` |

**7 of 10 have no token cap. 10 of 10 have no timeout.** `vertex_extract.py::vertex_embed` (the embedding call, hit on every message via `embed_message_by_id`) also has no timeout.

The token-cap gap is not cosmetic. `_openai_json` is called with `max_tokens=512` from `rapport_reply`, `rapport_synth`, `latent_extract` and `vertex_extract`. On the Gemini fallback for those same functions the model is uncapped, so a Gemini 2.5 model with thinking enabled can emit thousands of tokens for a call budgeted at 512 — we pay for it, and the extra latency lands on a user's turn.

There is also a **second, uncached client**. `app/vertex_extract.py`:

```python
def _vertex_client():
    project = os.environ.get("GCP_VERTEX_PROJECT", "")
    location = os.environ.get("GCP_VERTEX_LOCATION", "us-central1")
    if not project:
        raise RuntimeError("GCP_VERTEX_PROJECT not set")
    from google import genai

    return genai.Client(vertexai=True, project=project, location=location)
```

No module-global cache, unlike `orchestrator/llm.py::_gemini_client()`. Every fallback call constructs a fresh client (fresh ADC credential resolution, fresh httpx pool). `rapport_reply`, `rapport_synth`, `latent_extract`, `profile_intake`, `vertex_event`, `vertex_lana` all import *this* one, so **the cached client in `llm.py` is used by exactly one call site and the uncached one by nine.**

### 1.3 LIVE BUG — the `/complete` extract sends a Gemini model id to OpenAI

`app/orchestrator/extract.py`:

```python
def _extract_model() -> str:
    """Flash is more reliable for large structured JSON than Pro."""
    return os.environ.get("VERTEX_EXTRACT_MODEL", "gemini-2.5-flash")


def claude_extract_profile_from_transcript(
    transcript: str,
) -> tuple[list[ExtractedClaim], str, str | None, list[MappedSpan]]:
    data = llm_json(
        model=_extract_model(),
        ...
```

`llm_json` routes **by `provider()`, not by the model string** — the docstring in `discovery_slots.py` says so explicitly:

```python
    The override MUST match the active provider() — llm_json routes by provider,
    not by the model string, so an OpenAI model id requires provider=openai
```

Prod env is `LANA_LLM_PROVIDER=openai` and `VERTEX_EXTRACT_MODEL=gemini-2.5-flash`. So `POST /lana/sessions/{id}/complete` calls OpenAI's `chat.completions.create(model="gemini-2.5-flash")` → `NotFoundError: model_not_found` → caught here:

```python
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=_vertex_error_detail("lana_extract_failed", exc),
        ) from exc
```

Both `claude_extract_profile_from_transcript` (profile) and `claude_extract_event_from_transcript` (event) are affected, and both are only reachable when `_use_orchestrator()` is true — which it is in prod. This is exactly the failure class Asjid described ("we moved away from Gemini and left things behind"), except it is not a fallback, it is the primary path.

The repo's own README still documents the pre-move behaviour:

> Profile/event **complete** extract still uses Vertex (`VERTEX_EXTRACT_MODEL`) unless migrated.

That statement was true when `_use_orchestrator()` was false. It is now false, and the code was never updated.

### 1.4 `vertex_lana._call_lana` is the weakest link of all

```python
def _call_lana(payload: str) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    client = _vertex_client()
    model = os.environ.get("VERTEX_LANA_MODEL", os.environ.get("VERTEX_EXTRACT_MODEL", "gemini-2.5-flash"))
    from google.genai import types

    system = build_system_prompt()
    response = client.models.generate_content(
        model=model,
        contents=payload,
        config=types.GenerateContentConfig(
            temperature=0.55,
            response_mime_type="application/json",
            system_instruction=system + "\n\n" + LANA_TURN_SUFFIX,
        ),
    )
    data = json.loads((response.text or "{}").strip())
    return _parse_turn(data)
```

No cap, no timeout, no retry, and it uses raw `json.loads` instead of the repo's own tolerant `parse_json_object`. `parse_json_object` exists precisely because models emit fenced/near-JSON; this call site opted out of it. One stray markdown fence and the whole turn 502s.

### 1.5 `/health` misreports the extract model

```python
        "extract_model": (
            synthesizer_model()
            if _use_orchestrator() and llm_configured()
            else os.environ.get("VERTEX_EXTRACT_MODEL", "gemini-2.5-flash")
        ),
```

Reports `gpt-4.1` (the synthesizer), while the code path in §1.3 actually uses `VERTEX_EXTRACT_MODEL`. That is why the 2026-07-30 code-truth audit recorded `extract_model gpt-4.1` and did not catch the 502. Root reports `"version": "0.5.3"` while `FastAPI(..., version="0.5.4")` — the same class of drift.

### 1.6 Bonus: `OPENAI_TIMEOUT_SEC=15` is a **per-attempt** timeout

`openai>=1.55` defaults `max_retries=2` (verified: `openai._constants.DEFAULT_MAX_RETRIES == 2`) and retries 408/409/429/5xx with backoff. `_openai_client()` never sets `max_retries`, so the real worst-case wall clock for one `llm_json` call is `3 × 15 s + backoff ≈ 50 s`, and `_openai_json` can then make **two more** `_openai_generate` calls on bad JSON — a ~150 s theoretical worst case on a single turn, behind a Cloud Run request. Making the budget explicit is part of this PR.

---

## 2. The fix

Design rule: **one place decides the limits, both providers read them.** Parity by construction, not by two lists that drift.

### 2.1 `app/orchestrator/llm.py` — new settings + shared Gemini config + real failover

```python
# ── ADD near the other env readers ────────────────────────────────────────────

def _openai_max_retries() -> int:
    """Explicit, so the worst-case wall clock of one llm_json call is knowable.
    The SDK default is 2 (openai._constants.DEFAULT_MAX_RETRIES); leaving it
    implicit meant OPENAI_TIMEOUT_SEC=15 was a PER-ATTEMPT budget nobody had
    multiplied out."""
    raw = os.environ.get("OPENAI_MAX_RETRIES", "2").strip()
    try:
        return max(0, min(5, int(raw)))
    except ValueError:
        return 2


def _vertex_timeout_sec() -> float:
    """Vertex timeout DEFAULTS TO THE OPENAI ONE. Parity by construction: tuning
    OPENAI_TIMEOUT_SEC moves both paths unless VERTEX_TIMEOUT_SEC overrides it.
    This is the specific gap Asjid called out on 2026-07-30."""
    raw = os.environ.get("VERTEX_TIMEOUT_SEC", "").strip()
    if not raw:
        return _openai_timeout_sec()
    try:
        return max(5.0, float(raw))
    except ValueError:
        return _openai_timeout_sec()


def _vertex_max_output_tokens(max_tokens: int | None) -> int:
    """Hard ceiling for any Gemini call. Gemini 2.5 spends THINKING tokens
    against this budget, so a caller's 512 needs headroom the same way the
    GPT-5/o-series branch above gives itself 4096 — but it must still be a
    cap, not open-ended."""
    ceiling_raw = os.environ.get("VERTEX_MAX_OUTPUT_TOKENS", "4096").strip()
    try:
        ceiling = max(256, int(ceiling_raw))
    except ValueError:
        ceiling = 4096
    if not max_tokens or int(max_tokens) <= 0:
        return ceiling
    return max(512, min(int(max_tokens) * 2, ceiling))


def fallback_enabled() -> bool:
    """Kill switch. LANA_LLM_FALLBACK=0 restores today's behaviour exactly."""
    return os.environ.get("LANA_LLM_FALLBACK", "1").strip().lower() not in ("0", "false", "off")
```

Client construction gains the settings:

```python
    _openai_client_instance = OpenAI(
        api_key=api_key,
        timeout=_openai_timeout_sec(),
        max_retries=_openai_max_retries(),
    )
```

```python
def gemini_http_options():
    """Timeout + transport retries for EVERY Vertex call in the worker.
    timeout is MILLISECONDS (google-genai HttpOptions contract)."""
    from google.genai import types

    return types.HttpOptions(
        timeout=int(_vertex_timeout_sec() * 1000),
        retry_options=types.HttpRetryOptions(
            attempts=_openai_max_retries() + 1,
            http_status_codes=[408, 429, 500, 502, 503, 504],
        ),
    )


def gemini_config(*, system: str | None, temperature: float, max_tokens: int | None):
    """THE single GenerateContentConfig builder. Every direct-Vertex call site in
    the worker must go through this so a change to the limits lands everywhere."""
    from google.genai import types

    kwargs: dict[str, Any] = {
        "temperature": temperature,
        "max_output_tokens": _vertex_max_output_tokens(max_tokens),
        "response_mime_type": "application/json",
        "http_options": gemini_http_options(),
    }
    if system:
        kwargs["system_instruction"] = system
    return types.GenerateContentConfig(**kwargs)


def _gemini_client():
    global _gemini_client_instance
    if _gemini_client_instance is not None:
        return _gemini_client_instance
    project = os.environ.get("GCP_VERTEX_PROJECT", "")
    location = os.environ.get("GCP_VERTEX_LOCATION", "us-central1")
    if not project:
        raise RuntimeError("GCP_VERTEX_PROJECT not set")
    from google import genai

    _gemini_client_instance = genai.Client(
        vertexai=True,
        project=project,
        location=location,
        http_options=gemini_http_options(),   # client-level floor
    )
    return _gemini_client_instance
```

`_gemini_generate` now uses the shared builder instead of hand-rolling the config:

```python
def _gemini_generate(*, model: str, system: str, user_payload: str, max_tokens: int, temperature: float) -> str:
    client = _gemini_client()
    response = client.models.generate_content(
        model=model,
        contents=user_payload,
        config=gemini_config(
            system=system + _JSON_RULES,
            temperature=temperature,
            max_tokens=max_tokens,
        ),
    )
    return response.text or ""
```

Error classification, shared by both providers:

```python
_RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


def _is_retryable(exc: BaseException) -> tuple[bool, str]:
    """True when the OTHER provider is worth trying: timeout, rate limit, 5xx,
    connection reset. NOT for auth errors, bad requests, or model_not_found —
    those are our bugs and must stay loud."""
    name = type(exc).__name__
    # OpenAI SDK
    status = getattr(exc, "status_code", None)
    if name in ("APITimeoutError", "APIConnectionError"):
        return True, name
    if isinstance(status, int) and status in _RETRYABLE_STATUS:
        return True, f"http_{status}"
    # google-genai (errors.APIError carries .code)
    code = getattr(exc, "code", None)
    if isinstance(code, int) and code in _RETRYABLE_STATUS:
        return True, f"http_{code}"
    if name in ("ServerError", "DeadlineExceeded", "ResourceExhausted", "ServiceUnavailable"):
        return True, name
    if isinstance(exc, TimeoutError):
        return True, "timeout"
    return False, name
```

And the failover itself — the whole point of the PR:

```python
def _fallback_target(p: str) -> str | None:
    """The other configured provider, or None. Never returns the same provider."""
    if p == "openai" and vertex_configured():
        return "gemini"
    if p in ("gemini", "claude") and openai_configured():
        return "openai"
    return None


def _model_for(target: str, *, synth: bool) -> str:
    """Translate a model slot across providers — a gpt-4.1 request must become a
    gemini-2.5-pro request, not a literal 'gpt-4.1' sent to Vertex. Reads the
    same env vars the primary path reads (VERTEX_LANA_ROUTER_MODEL /
    VERTEX_LANA_SYNTH_MODEL / OPENAI_ROUTER_MODEL / OPENAI_SYNTH_MODEL)."""
    if target == "openai":
        return (
            (os.environ.get("OPENAI_SYNTH_MODEL", "").strip() or "gpt-4o")
            if synth
            else (os.environ.get("OPENAI_ROUTER_MODEL", "").strip() or "gpt-4o-mini")
        )
    return (
        (os.environ.get("VERTEX_LANA_SYNTH_MODEL", "").strip() or "gemini-2.5-pro")
        if synth
        else (os.environ.get("VERTEX_LANA_ROUTER_MODEL", "").strip() or "gemini-2.5-flash")
    )


def llm_json(
    *,
    model: str,
    system: str,
    user_payload: str,
    max_tokens: int = 1024,
    temperature: float = 0.2,
    llm_attempts: list[int] | None = None,
    allow_fallback: bool = True,
) -> dict[str, Any]:
    p = provider()
    # Remember which slot the caller asked for BEFORE we cross providers, so the
    # fallback picks the equivalent tier rather than a literal model string that
    # only exists on the other vendor.
    is_synth = (model == synthesizer_model())
    try:
        return _dispatch(
            p, model=model, system=system, user_payload=user_payload,
            max_tokens=max_tokens, temperature=temperature, llm_attempts=llm_attempts,
        )
    except Exception as exc:  # noqa: BLE001 — classified immediately below
        retryable, reason = _is_retryable(exc)
        target = _fallback_target(p) if (allow_fallback and fallback_enabled()) else None
        if not retryable or target is None:
            raise
        fb_model = _model_for(target, synth=is_synth)
        _log.warning(
            "llm_fallback from=%s to=%s reason=%s primary_model=%s fallback_model=%s max_tokens=%s",
            p, target, reason, model, fb_model, max_tokens,
        )
        try:
            data = _dispatch(
                target, model=fb_model, system=system, user_payload=user_payload,
                max_tokens=max_tokens, temperature=temperature, llm_attempts=llm_attempts,
            )
        except Exception:
            _log.exception("llm_fallback_also_failed from=%s to=%s", p, target)
            raise exc from None   # surface the ORIGINAL error, not the second one
        if llm_attempts is not None and llm_attempts:
            # Mark the turn as fallback-served so TurnRouting/telemetry can count it.
            llm_attempts[:] = [llm_attempts[0] + 10]
        _log.info("llm_fallback_ok from=%s to=%s model=%s", p, target, fb_model)
        return data


def _dispatch(
    p: str, *, model: str, system: str, user_payload: str,
    max_tokens: int, temperature: float, llm_attempts: list[int] | None,
) -> dict[str, Any]:
    """Today's llm_json body, extracted verbatim so both the primary and the
    fallback attempt run identical code with identical limits."""
    if p == "openai":
        data, attempts = _openai_json(
            model=model, system=system, user_payload=user_payload,
            max_tokens=max_tokens, temperature=temperature,
        )
    elif p == "claude":
        data = _claude_json(
            model=model, system=system, user_payload=user_payload,
            max_tokens=max_tokens, temperature=temperature,
        )
        attempts = 1
    else:
        data, attempts = _gemini_json(
            model=model, system=system, user_payload=user_payload,
            max_tokens=max_tokens, temperature=temperature,
        )
    if llm_attempts is not None:
        llm_attempts[:] = [attempts]
    return data
```

Note `llm_attempts[:] = [attempts + 10]` on a fallback-served turn: `TurnRouting`/`timing_ms` already carries `*_attempts` keys (`_timing_total_ms` skips them), so `attempts >= 11` becomes a countable "this turn was served by the backup provider" signal with no schema change.

### 2.2 One public helper for the direct-Vertex call sites

New in `app/orchestrator/llm.py`:

```python
def vertex_generate_json(
    *,
    model: str | None = None,
    system: str | None,
    user_payload: str,
    max_tokens: int,
    temperature: float,
    retry_suffix: str | None = None,
    attempts_out: list[int] | None = None,
) -> dict[str, Any]:
    """THE supported way to make a direct Vertex call outside the orchestrator.

    Applies the shared timeout, the shared token ceiling, transport retries, the
    tolerant JSON parser, and the one-shot bad-JSON re-ask that the OpenAI path
    has always had. Replaces nine hand-rolled generate_content() blocks."""
    from app.orchestrator.json_util import parse_json_object

    client = _gemini_client()
    mdl = model or os.environ.get(
        "VERTEX_LANA_MODEL", os.environ.get("VERTEX_EXTRACT_MODEL", "gemini-2.5-flash")
    )
    cfg = gemini_config(system=system, temperature=temperature, max_tokens=max_tokens)

    def _gen(payload: str, cfg_override=None) -> str:
        resp = client.models.generate_content(
            model=mdl, contents=payload, config=cfg_override or cfg
        )
        return resp.text or ""

    attempts = 1
    text = _gen(user_payload)
    try:
        data = parse_json_object(text)
    except (json.JSONDecodeError, ValueError):
        attempts = 2
        suffix = retry_suffix or (
            "\n\nYour previous reply was invalid JSON. Return ONE compact JSON "
            "object. assistant_message must be a single line string."
        )
        data = parse_json_object(
            _gen(
                user_payload + suffix,
                gemini_config(system=system, temperature=0.1, max_tokens=max_tokens),
            )
        )
    if attempts_out is not None:
        attempts_out[:] = [attempts]
    return data
```

### 2.3 Rewrite the nine hand-rolled call sites

Mechanical, one shape. Example — `app/rapport_reply.py`:

```python
-def _vertex_concierge_reply(user_payload: str) -> Any:
-    """Direct Vertex Gemini fallback when the orchestrator LLM isn't configured."""
-    from app.orchestrator.json_util import parse_json_object
-    from app.vertex_extract import _vertex_client
-
-    client = _vertex_client()
-    model = os.environ.get("VERTEX_EXTRACT_MODEL", "gemini-2.5-flash")
-    from google.genai import types
-
-    response = client.models.generate_content(
-        model=model,
-        contents=CONCIERGE_PROMPT + "\n\n" + user_payload,
-        config=types.GenerateContentConfig(
-            temperature=0.5,
-            response_mime_type="application/json",
-        ),
-    )
-    return parse_json_object(response.text or "")
+def _vertex_concierge_reply(user_payload: str) -> Any:
+    """Direct Vertex Gemini fallback. Same token budget (512) and same timeout
+    the OpenAI path uses — see llm.gemini_config()."""
+    from app.orchestrator.llm import vertex_generate_json
+
+    return vertex_generate_json(
+        model=os.environ.get("VERTEX_EXTRACT_MODEL", "gemini-2.5-flash"),
+        system=CONCIERGE_PROMPT,
+        user_payload=user_payload,
+        max_tokens=512,       # parity with the llm_json call directly above
+        temperature=0.5,
+    )
```

`app/vertex_lana.py` — also fixes the raw `json.loads`:

```python
-def _call_lana(payload: str) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
-    client = _vertex_client()
-    model = os.environ.get("VERTEX_LANA_MODEL", os.environ.get("VERTEX_EXTRACT_MODEL", "gemini-2.5-flash"))
-    from google.genai import types
-
-    system = build_system_prompt()
-    response = client.models.generate_content(
-        model=model,
-        contents=payload,
-        config=types.GenerateContentConfig(
-            temperature=0.55,
-            response_mime_type="application/json",
-            system_instruction=system + "\n\n" + LANA_TURN_SUFFIX,
-        ),
-    )
-    data = json.loads((response.text or "{}").strip())
-    return _parse_turn(data)
+LANA_MAX_OUTPUT_TOKENS = 1024   # matches vertex_event.EVENT_MAX_OUTPUT_TOKENS
+
+
+def _call_lana(payload: str) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
+    from app.orchestrator.llm import vertex_generate_json
+
+    data = vertex_generate_json(
+        system=build_system_prompt() + "\n\n" + LANA_TURN_SUFFIX,
+        user_payload=payload,
+        max_tokens=LANA_MAX_OUTPUT_TOKENS,
+        temperature=0.55,
+    )
+    return _parse_turn(data)
```

Same treatment for: `vertex_extract.vertex_extract_claims_from_utterance` (512), `vertex_extract.vertex_extract_from_transcript` (4096), `vertex_event_extract.vertex_extract_event_from_transcript` (4096), `vertex_event._call_event_lana` vertex branch (`EVENT_MAX_OUTPUT_TOKENS`), `profile_intake._call_profile_lana` (`PROFILE_MAX_OUTPUT_TOKENS`, keeping its truncation re-ask via `retry_suffix=`), `rapport_synth._generate` (512), `latent_extract.extract_entities_from_message` (512).

`vertex_extract._vertex_client()` is then reduced to a delegating shim so the nine importers share the one cached, timeout-carrying client:

```python
def _vertex_client():
    """Deprecated shim — kept because nine modules import it by name. Delegates to
    the single cached client so every Vertex call inherits gemini_http_options()."""
    from app.orchestrator.llm import _gemini_client

    return _gemini_client()
```

and `vertex_embed` gains the timeout for free.

### 2.4 Fix the live 502 (`orchestrator/extract.py`)

```python
-def _extract_model() -> str:
-    """Flash is more reliable for large structured JSON than Pro."""
-    return os.environ.get("VERTEX_EXTRACT_MODEL", "gemini-2.5-flash")
+def _extract_model() -> str:
+    """Provider-correct model for the /complete extract.
+
+    llm_json routes by provider(), NOT by the model string, so returning
+    VERTEX_EXTRACT_MODEL under LANA_LLM_PROVIDER=openai sent "gemini-2.5-flash"
+    to OpenAI and 502'd every profile/event completion (found 2026-07-30).
+    LANA_EXTRACT_MODEL overrides, but it MUST match the active provider."""
+    override = os.environ.get("LANA_EXTRACT_MODEL", "").strip()
+    if override:
+        return override
+    if provider() == "openai":
+        # Large structured JSON — the synth tier, matching what /health reports.
+        return synthesizer_model()
+    if provider() == "claude":
+        return synthesizer_model()
+    return os.environ.get("VERTEX_EXTRACT_MODEL", "gemini-2.5-flash")
```

(`from app.orchestrator.llm import llm_json, provider, synthesizer_model`.)

### 2.5 Make `/health` honest

```python
-        "extract_model": (
-            synthesizer_model()
-            if _use_orchestrator() and llm_configured()
-            else os.environ.get("VERTEX_EXTRACT_MODEL", "gemini-2.5-flash")
-        ),
+        # Report what the /complete path ACTUALLY calls, not what we hope it calls.
+        "extract_model": (
+            orchestrator_extract_model()
+            if _use_orchestrator() and llm_configured()
+            else os.environ.get("VERTEX_EXTRACT_MODEL", "gemini-2.5-flash")
+        ),
+        "fallback_enabled": fallback_enabled(),
+        "fallback_target": _fallback_target(provider()),
+        "openai_timeout_sec": _openai_timeout_sec(),
+        "vertex_timeout_sec": _vertex_timeout_sec(),
+        "openai_max_retries": _openai_max_retries(),
```

(`orchestrator_extract_model` = `app.orchestrator.extract._extract_model` re-exported.) And bump the `root()` literal `"version": "0.5.3"` → `"0.5.5"` to match `FastAPI(version=...)`.

### 2.6 Env / flags

Add to `deploy/lana-worker.env.example`:

```bash
# ── LLM failover (PR9) ─────────────────────────────────────────────────────
# 1 = a retryable OpenAI failure (timeout / 429 / 5xx / connection) silently
# retries once on Vertex Gemini with the SAME token + timeout budget.
# 0 = today's behaviour (no failover) — the rollback switch.
LANA_LLM_FALLBACK=1

# Vertex timeout DEFAULTS to OPENAI_TIMEOUT_SEC. Set only to diverge them.
# VERTEX_TIMEOUT_SEC=15

# Ceiling for any Gemini call. A caller's max_tokens is doubled (Gemini 2.5
# spends thinking tokens against the budget) and then clamped to this.
VERTEX_MAX_OUTPUT_TOKENS=4096

# Explicit so OPENAI_TIMEOUT_SEC's real worst case is (retries+1) x timeout.
OPENAI_MAX_RETRIES=2

# Provider-correct /complete extract. Must match LANA_LLM_PROVIDER. Leave unset.
# LANA_EXTRACT_MODEL=
```

Prod values to set on the Cloud Run service: `LANA_LLM_FALLBACK=1`, `VERTEX_MAX_OUTPUT_TOKENS=4096`, `OPENAI_MAX_RETRIES=2`. Leave `VERTEX_TIMEOUT_SEC` unset so it inherits `OPENAI_TIMEOUT_SEC=15`.

---

## 3. Test plan

### 3.1 New unit tests — `services/lana-worker/tests/test_llm_fallback.py`

Follows the existing `unittest` + env-save/restore shape of `tests/test_llm_provider.py`.

```python
class TestFallbackParity(unittest.TestCase):
    def test_vertex_timeout_defaults_to_openai_timeout(self):
        os.environ["OPENAI_TIMEOUT_SEC"] = "15"
        os.environ.pop("VERTEX_TIMEOUT_SEC", None)
        self.assertEqual(llm._vertex_timeout_sec(), 15.0)

    def test_vertex_timeout_override_wins(self):
        os.environ["OPENAI_TIMEOUT_SEC"] = "15"
        os.environ["VERTEX_TIMEOUT_SEC"] = "30"
        self.assertEqual(llm._vertex_timeout_sec(), 30.0)

    def test_gemini_config_always_caps_tokens_and_sets_timeout(self):
        cfg = llm.gemini_config(system="s", temperature=0.2, max_tokens=512)
        self.assertIsNotNone(cfg.max_output_tokens)
        self.assertLessEqual(cfg.max_output_tokens, 4096)
        self.assertIsNotNone(cfg.http_options.timeout)     # milliseconds

    def test_token_ceiling_is_respected(self):
        os.environ["VERTEX_MAX_OUTPUT_TOKENS"] = "1000"
        self.assertEqual(llm._vertex_max_output_tokens(4096), 1000)
```

```python
class TestFallbackTrigger(unittest.TestCase):
    """Each of the three triggers Asjid asked about, with a fake client."""

    def _run(self, exc):
        os.environ["LANA_LLM_PROVIDER"] = "openai"
        os.environ["OPENAI_API_KEY"] = "sk-test"
        os.environ["GCP_VERTEX_PROJECT"] = "p"
        os.environ["LANA_LLM_FALLBACK"] = "1"
        with mock.patch.object(llm, "_openai_json", side_effect=exc), \
             mock.patch.object(llm, "_gemini_json", return_value=({"ok": True}, 1)) as gem:
            out = llm.llm_json(model="gpt-4.1", system="s", user_payload="u", max_tokens=512)
        return out, gem

    def test_timeout_falls_back(self):
        out, gem = self._run(openai.APITimeoutError(request=mock.Mock()))
        self.assertEqual(out, {"ok": True})
        self.assertEqual(gem.call_args.kwargs["max_tokens"], 512)   # SAME budget

    def test_rate_limit_429_falls_back(self):
        out, _ = self._run(_status_error(429))
        self.assertEqual(out, {"ok": True})

    def test_server_5xx_falls_back(self):
        out, _ = self._run(_status_error(503))
        self.assertEqual(out, {"ok": True})

    def test_model_slot_translates(self):
        """A synth-tier request must become the VERTEX synth model, never the
        literal 'gpt-4.1'."""
        os.environ["OPENAI_SYNTH_MODEL"] = "gpt-4.1"
        os.environ["VERTEX_LANA_SYNTH_MODEL"] = "gemini-2.5-pro"
        out, gem = self._run(_status_error(429))
        self.assertEqual(gem.call_args.kwargs["model"], "gemini-2.5-pro")

    def test_auth_error_does_NOT_fall_back(self):
        """401/400/model_not_found are our bugs — they must stay loud."""
        with self.assertRaises(openai.AuthenticationError):
            self._run(_status_error(401))

    def test_flag_off_restores_old_behaviour(self):
        os.environ["LANA_LLM_FALLBACK"] = "0"
        with self.assertRaises(openai.APITimeoutError):
            self._run(openai.APITimeoutError(request=mock.Mock()))

    def test_fallback_is_silent_to_the_caller_and_logged(self):
        with self.assertLogs("app.orchestrator.llm", level="WARNING") as cm:
            out, _ = self._run(_status_error(429))
        self.assertEqual(out, {"ok": True})            # silent: normal return
        self.assertTrue(any("llm_fallback" in m for m in cm.output))   # logged
```

```python
class TestExtractModelRegression(unittest.TestCase):
    def test_openai_provider_never_returns_a_gemini_model(self):
        os.environ["LANA_LLM_PROVIDER"] = "openai"
        os.environ["OPENAI_API_KEY"] = "sk-test"
        os.environ["OPENAI_SYNTH_MODEL"] = "gpt-4.1"
        os.environ["VERTEX_EXTRACT_MODEL"] = "gemini-2.5-flash"
        self.assertNotIn("gemini", extract._extract_model())
        self.assertEqual(extract._extract_model(), "gpt-4.1")

    def test_gemini_provider_still_uses_vertex_extract_model(self):
        os.environ["LANA_LLM_PROVIDER"] = "gemini"
        os.environ.pop("OPENAI_API_KEY", None)
        os.environ["VERTEX_EXTRACT_MODEL"] = "gemini-2.5-flash"
        self.assertEqual(extract._extract_model(), "gemini-2.5-flash")
```

Plus a guard test that stops this class of drift recurring:

```python
class TestNoUnboundedVertexCalls(unittest.TestCase):
    """Static guard: nobody may call generate_content() outside llm.py again."""

    def test_generate_content_only_in_llm_module(self):
        root = pathlib.Path(__file__).resolve().parents[1] / "app"
        offenders = [
            str(p.relative_to(root))
            for p in root.rglob("*.py")
            if "generate_content(" in p.read_text()
            and p.name != "llm.py"
        ]
        self.assertEqual(offenders, [], f"direct Vertex calls found: {offenders}")
```

Run: `cd services/lana-worker && python -m unittest discover -s tests -v`
(existing suite is 75 files, all `unittest`; no pytest dependency is added).

### 3.2 Manual / staging

1. **Extract regression (the live 502).** Against staging, create a `profile_intake` session, send 2-3 messages, then
   `POST /lana/sessions/{id}/complete` **with `{"publish": false}`** — `CompleteSessionRequest.publish` defaults `true` and will publish a real event otherwise. Before: 502 `lana_extract_failed:NotFoundError:...gemini-2.5-flash...`. After: 200 with claims.
2. **Timeout fallback.** Set `OPENAI_TIMEOUT_SEC=1` on a staging revision, send a normal turn. Expect: a 200 reply (served by Gemini) and one `llm_fallback from=openai to=gemini reason=APITimeoutError` line in Cloud Logging. Then set `LANA_LLM_FALLBACK=0`, repeat, expect the 502 back.
3. **429.** Point `OPENAI_API_KEY` at a key with a 0 RPM tier (or use an obviously invalid org project id that returns 429) — same expectation as 2.
4. **Token cap.** With `LANA_LLM_PROVIDER=gemini`, run a rapport reply turn and confirm the Vertex request logs `max_output_tokens=1024` (512 × 2), not unset.
5. **Health.** `curl $WORKER/health` — `extract_model` must equal `synth_model` when `llm_provider=openai`, and `fallback_enabled`, `vertex_timeout_sec`, `openai_max_retries` must appear.
6. **Cost sanity.** After 24 h on prod, `grep llm_fallback` in Cloud Logging. Expected steady state: near zero. A non-trivial rate means OpenAI is genuinely unstable for us and the tier/timeouts need revisiting — which is exactly the number we currently cannot see.

### 3.3 Explicitly out of scope

- Streaming (`/messages/stream`) is unchanged — it wraps the same `_run_lana_message`, so it inherits the fix without transport work.
- `app/orchestrator/claude.py` (the AnthropicVertex branch) gets the shared `_is_retryable` treatment via `llm_json`, but no timeout is added to `AnthropicVertex` — it is unused in prod (`LANA_LLM_PROVIDER=openai`) and adding it untested is risk without benefit. **Flagged as a known remaining gap.**

---

## 4. Rollback

| Level | Action | Effect |
|---|---|---|
| 1 (seconds, no deploy) | `gcloud run services update tagalng-lana-worker-prod --update-env-vars LANA_LLM_FALLBACK=0` | Failover off. `llm_json` behaves exactly as today (raise → 502). Token caps and timeouts stay — they are strictly safer than unbounded. |
| 2 | also `--update-env-vars VERTEX_MAX_OUTPUT_TOKENS=32768` | Effectively removes the Gemini token ceiling if a prompt is found that legitimately needs more, without a code change. |
| 3 | also `--update-env-vars VERTEX_TIMEOUT_SEC=300` | Effectively removes the Vertex timeout. |
| 4 | also `--update-env-vars LANA_EXTRACT_MODEL=gemini-2.5-flash` | Restores the pre-PR `/complete` model string (i.e. re-breaks it deliberately) if the new one misbehaves. |
| 5 (full) | `gcloud run services update-traffic ... --to-revisions PREV=100` | Previous revision. No data was written by this PR — nothing to unwind. |

**No migration, no schema change, no data written.** Rollback is env-var-only at levels 1-4.

---

## 5. What this PR does NOT fix (stated, not hidden)

1. **`AnthropicVertex` has no timeout.** Unused in prod; left alone deliberately (§3.3).
2. **The 502 body still leaks internals.** `_vertex_error_detail` puts the exception class and 500 chars of the vendor error into the HTTP response. That is a separate hardening PR; failover makes it far rarer but does not remove it.
3. **`OPENAI_TIMEOUT_SEC=15` may be too aggressive for `gpt-4.1` on the synth path.** This PR makes the real budget visible (`(retries+1) × timeout`) but does not retune it — that needs the latency data from `llm_usage model=... ms=...`, which is already being logged.
4. **Interaction with PR10.** PR10 makes `synthesizer_model()` return `router_model()` for an over-quota signed-in user. `llm_json` computes `is_synth = (model == synthesizer_model())`, so on a degraded turn a fallback also picks the router tier on the other provider. That is the correct behaviour (a degraded turn stays degraded across providers) but it is load-bearing and non-obvious — if the two PRs land in either order, keep this line in mind.
5. **`event_fast_path` / `profile_fast_path` bypass the orchestrator entirely** and go through `vertex_event` / `profile_intake`. Those two DO already cap tokens; this PR adds their timeouts. But their *primary* provider selection (`_call_event_lana` checks `provider() in ("openai","claude")`, `_call_profile_lana` does not) remains inconsistent — `_call_profile_lana` is **Vertex-only with no OpenAI branch at all**, meaning `profile_intake` turns hit Gemini even under `LANA_LLM_PROVIDER=openai`. That is the mirror image of §1.3 and deserves its own PR; flagged, not fixed here, because changing the model behind profile intake changes conversation quality and needs an eval pass.
