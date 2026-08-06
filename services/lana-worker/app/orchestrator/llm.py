"""Orchestrator LLM — OpenAI (ATPR), Vertex Gemini, or Claude via env."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from app.orchestrator.json_util import parse_json_object

_log = logging.getLogger(__name__)

_gemini_client_instance: Any = None
_claude_client_instance: Any = None
_openai_client_instance: Any = None

_JSON_RULES = (
    "\n\nReturn strictly valid JSON. Double-quoted keys/strings only. "
    "No trailing commas. No | union syntax. "
    "Keep assistant_message on ONE line (no raw line breaks)."
)


def provider() -> str:
    explicit = os.environ.get("LANA_LLM_PROVIDER", "").strip().lower()
    if explicit in ("openai", "gpt"):
        return "openai"
    if explicit in ("claude", "anthropic"):
        return "claude"
    if explicit in ("gemini", "vertex", "google"):
        return "gemini"
    if os.environ.get("OPENAI_API_KEY", "").strip():
        return "openai"
    return "gemini"


def openai_configured() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY", "").strip())


def vertex_configured() -> bool:
    return bool(os.environ.get("GCP_VERTEX_PROJECT", "").strip())


def llm_configured() -> bool:
    if provider() == "openai":
        return openai_configured()
    return vertex_configured()


def router_model() -> str:
    if provider() == "openai":
        return (
            os.environ.get("OPENAI_ROUTER_MODEL", "").strip()
            or os.environ.get("OPENAI_LANA_ROUTER_MODEL", "").strip()
            or "gpt-4o-mini"
        )
    explicit = os.environ.get("VERTEX_LANA_ROUTER_MODEL", "").strip()
    if explicit:
        return explicit
    legacy = os.environ.get("VERTEX_CLAUDE_ROUTER_MODEL", "").strip()
    if legacy:
        return legacy
    return "claude-haiku-4-5@20251001" if provider() == "claude" else "gemini-2.5-flash"


def synthesizer_model() -> str:
    if provider() == "openai":
        return (
            os.environ.get("OPENAI_SYNTH_MODEL", "").strip()
            or os.environ.get("OPENAI_LANA_SYNTH_MODEL", "").strip()
            or "gpt-4o"
        )
    explicit = os.environ.get("VERTEX_LANA_SYNTH_MODEL", "").strip()
    if explicit:
        return explicit
    legacy = os.environ.get("VERTEX_CLAUDE_SYNTH_MODEL", "").strip()
    if legacy:
        return legacy
    return "claude-sonnet-4-6" if provider() == "claude" else "gemini-2.5-pro"


def extractor_model() -> str:
    """Provider-correct model for every structured EXTRACTION call.

    Deliberately NOT the router tier. Extraction is the most instruction-dense
    call in the system (six required output fields, ~4k tokens of rules), and a
    mini-tier model reliably nails the primary field while silently dropping the
    trailing ones: "i play pickleball regularly with friends" produced the
    plays_pickleball claim and circle_candidates [] on gpt-4.1-mini — twice in
    prod and once locally — losing the affiliation the onion matcher needs
    (2026-08-05).

    LANA_EXTRACT_MODEL overrides, but it MUST match the active provider:
    llm_json routes by provider(), not by the model string, so a Gemini id under
    LANA_LLM_PROVIDER=openai 502s the call (2026-07-30).
    """
    override = os.environ.get("LANA_EXTRACT_MODEL", "").strip()
    if override:
        return override
    if provider() in ("openai", "claude"):
        return synthesizer_model()
    return os.environ.get("VERTEX_EXTRACT_MODEL", "gemini-2.5-flash")


def _openai_timeout_sec() -> float:
    raw = os.environ.get("OPENAI_TIMEOUT_SEC", "60").strip()
    try:
        return max(5.0, float(raw))
    except ValueError:
        return 60.0


def _openai_max_retries() -> int:
    """Explicit, so the worst-case wall clock of one llm_json call is knowable.
    The SDK default is 2 (openai._constants.DEFAULT_MAX_RETRIES); leaving it
    implicit meant OPENAI_TIMEOUT_SEC was a PER-ATTEMPT budget nobody had
    multiplied out."""
    raw = os.environ.get("OPENAI_MAX_RETRIES", "2").strip()
    try:
        return max(0, min(5, int(raw)))
    except ValueError:
        return 2


def _vertex_timeout_sec() -> float:
    """Vertex timeout DEFAULTS TO THE OPENAI ONE. Parity by construction: tuning
    OPENAI_TIMEOUT_SEC moves both paths unless VERTEX_TIMEOUT_SEC overrides it."""
    raw = os.environ.get("VERTEX_TIMEOUT_SEC", "").strip()
    if not raw:
        return _openai_timeout_sec()
    try:
        return max(5.0, float(raw))
    except ValueError:
        return _openai_timeout_sec()


def _vertex_max_output_tokens(max_tokens: int | None) -> int:
    """Hard ceiling for any Gemini call. Gemini 2.5 spends THINKING tokens
    against this budget, so a caller's budget is doubled for headroom — and the
    ceiling must stay above 2x the largest caller budget (4096-token extracts),
    otherwise the doubling is a no-op exactly where truncation hurts most."""
    ceiling_raw = os.environ.get("VERTEX_MAX_OUTPUT_TOKENS", "8192").strip()
    try:
        ceiling = max(256, int(ceiling_raw))
    except ValueError:
        ceiling = 8192
    if not max_tokens or int(max_tokens) <= 0:
        return ceiling
    return max(512, min(int(max_tokens) * 2, ceiling))


def fallback_enabled() -> bool:
    """Kill switch. LANA_LLM_FALLBACK=0 restores the no-failover behaviour."""
    return os.environ.get("LANA_LLM_FALLBACK", "1").strip().lower() not in ("0", "false", "off")


def _openai_client():
    global _openai_client_instance
    if _openai_client_instance is not None:
        return _openai_client_instance
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set")
    from openai import OpenAI

    _openai_client_instance = OpenAI(
        api_key=api_key,
        timeout=_openai_timeout_sec(),
        max_retries=_openai_max_retries(),
    )
    return _openai_client_instance


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
        http_options=gemini_http_options(),  # client-level floor
    )
    return _gemini_client_instance


def _claude_client():
    global _claude_client_instance
    if _claude_client_instance is not None:
        return _claude_client_instance
    project = os.environ.get("GCP_VERTEX_PROJECT", "")
    if not project:
        raise RuntimeError("GCP_VERTEX_PROJECT not set")
    from anthropic import AnthropicVertex

    region = os.environ.get(
        "VERTEX_CLAUDE_REGION",
        os.environ.get("GCP_VERTEX_LOCATION", "us-east1"),
    )
    _claude_client_instance = AnthropicVertex(project_id=project, region=region)
    return _claude_client_instance


def _openai_uses_completion_tokens(model: str) -> bool:
    """GPT-5.x and the o-series reasoning models replaced `max_tokens` with
    `max_completion_tokens` and reject a custom `temperature` (only the default is
    allowed). Older gpt-4* models still take `max_tokens` + `temperature`."""
    m = str(model or "").lower()
    return m.startswith(("gpt-5", "o1", "o3", "o4", "o5"))


def _openai_generate(
    *,
    model: str,
    system: str,
    user_payload: str,
    max_tokens: int,
    temperature: float,
) -> str:
    client = _openai_client()
    params: dict[str, Any] = {
        "model": model,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system + _JSON_RULES},
            {"role": "user", "content": user_payload},
        ],
    }
    if _openai_uses_completion_tokens(model):
        # GPT-5.x / o-series spend hidden REASONING tokens against this budget BEFORE any
        # visible output, so a small cap (e.g. the classifier's 512) can be fully consumed
        # by reasoning, returning EMPTY content — which silently degrades every downstream
        # read (abandon, clarify, out_of_scope). Give generous headroom. temperature is
        # fixed at the model default on these models, so it is not sent.
        params["max_completion_tokens"] = max(int(max_tokens), 4096)
    else:
        params["max_tokens"] = max_tokens
        params["temperature"] = temperature
    import time as _time

    t0 = _time.monotonic()
    response = client.chat.completions.create(**params)
    elapsed_ms = int((_time.monotonic() - t0) * 1000)
    # Latency diagnostics: cached=0 on a >1k-token static system prompt means OpenAI's
    # automatic prefix caching is NOT engaging (attack the input); cached≈prompt with a
    # slow call means the time is OUTPUT generation (attack completion length instead).
    try:
        usage = getattr(response, "usage", None)
        details = getattr(usage, "prompt_tokens_details", None)
        logging.getLogger(__name__).info(
            "llm_usage model=%s prompt=%s cached=%s completion=%s ms=%d",
            model,
            getattr(usage, "prompt_tokens", None),
            getattr(details, "cached_tokens", None),
            getattr(usage, "completion_tokens", None),
            elapsed_ms,
        )
    except Exception:  # noqa: BLE001 - diagnostics must never break a call
        pass
    choice = response.choices[0].message.content if response.choices else None
    return choice or ""


def _openai_json(
    *,
    model: str,
    system: str,
    user_payload: str,
    max_tokens: int,
    temperature: float,
    downshift_model: str | None = None,
) -> tuple[dict[str, Any], int]:
    # downshift_model MUST be an OpenAI model id. router_model() consults
    # provider(), which still names the PRIMARY provider during a cross-provider
    # fallback dispatch — using it here would send a Gemini id to OpenAI.
    downshift = downshift_model or router_model()
    attempts = 1
    text = _openai_generate(
        model=model,
        system=system,
        user_payload=user_payload,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    try:
        return parse_json_object(text), attempts
    except (json.JSONDecodeError, ValueError):
        attempts = 2
        retry_text = _openai_generate(
            model=model,
            system=system,
            user_payload=(
                user_payload
                + "\n\nYour previous reply was invalid JSON. "
                "Return ONE compact JSON object. assistant_message must be a single line string."
            ),
            max_tokens=max_tokens,
            temperature=0.1,
        )
        try:
            return parse_json_object(retry_text), attempts
        except (json.JSONDecodeError, ValueError):
            if model != downshift:
                attempts = 3
                mini_text = _openai_generate(
                    model=downshift,
                    system=system,
                    user_payload=user_payload,
                    max_tokens=max_tokens,
                    temperature=0.15,
                )
                return parse_json_object(mini_text), attempts
            raise


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


def _gemini_json(
    *,
    model: str,
    system: str,
    user_payload: str,
    max_tokens: int,
    temperature: float,
    downshift_model: str | None = None,
) -> tuple[dict[str, Any], int]:
    # downshift_model MUST be a Gemini model id — see the note in _openai_json.
    downshift = downshift_model or router_model()
    attempts = 1
    text = _gemini_generate(
        model=model,
        system=system,
        user_payload=user_payload,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    try:
        return parse_json_object(text), attempts
    except (json.JSONDecodeError, ValueError):
        attempts = 2
        retry_text = _gemini_generate(
            model=model,
            system=system,
            user_payload=(
                user_payload
                + "\n\nYour previous reply was invalid JSON. "
                "Return ONE compact JSON object. assistant_message must be a single line string."
            ),
            max_tokens=max_tokens,
            temperature=0.1,
        )
        try:
            return parse_json_object(retry_text), attempts
        except (json.JSONDecodeError, ValueError):
            if model != downshift:
                attempts = 3
                flash_text = _gemini_generate(
                    model=downshift,
                    system=system,
                    user_payload=user_payload,
                    max_tokens=max_tokens,
                    temperature=0.15,
                )
                return parse_json_object(flash_text), attempts
            raise


def _claude_json(
    *,
    model: str,
    system: str,
    user_payload: str,
    max_tokens: int,
    temperature: float,
) -> dict[str, Any]:
    client = _claude_client()
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system + _JSON_RULES,
        messages=[{"role": "user", "content": user_payload}],
    )
    parts: list[str] = []
    for block in response.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return parse_json_object("".join(parts))


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
    has always had. Replaces the hand-rolled generate_content() blocks."""
    client = _gemini_client()
    mdl = model or os.environ.get(
        "VERTEX_LANA_MODEL", os.environ.get("VERTEX_EXTRACT_MODEL", "gemini-2.5-flash")
    )
    cfg = gemini_config(system=system, temperature=temperature, max_tokens=max_tokens)

    def _gen(payload: str, cfg_override: Any = None) -> str:
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
    same env vars the primary path reads."""
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


def _dispatch(
    p: str,
    *,
    model: str,
    system: str,
    user_payload: str,
    max_tokens: int,
    temperature: float,
    llm_attempts: list[int] | None,
) -> dict[str, Any]:
    """One provider attempt. Both the primary and the fallback run this, so the
    limits and the bad-JSON retry ladder are identical either way.

    The bad-JSON downshift model is resolved for `p` explicitly: the helpers'
    default (router_model()) follows provider(), which is wrong mid-fallback."""
    downshift = _model_for("openai" if p == "openai" else "gemini", synth=False)
    if p == "openai":
        data, attempts = _openai_json(
            model=model,
            system=system,
            user_payload=user_payload,
            max_tokens=max_tokens,
            temperature=temperature,
            downshift_model=downshift,
        )
    elif p == "claude":
        data = _claude_json(
            model=model,
            system=system,
            user_payload=user_payload,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        attempts = 1
    else:
        data, attempts = _gemini_json(
            model=model,
            system=system,
            user_payload=user_payload,
            max_tokens=max_tokens,
            temperature=temperature,
            downshift_model=downshift,
        )
    if llm_attempts is not None:
        llm_attempts[:] = [attempts]
    return data


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
    # Which slot the caller asked for, resolved BEFORE crossing providers so the
    # fallback picks the equivalent tier rather than a literal model string that
    # only exists on the other vendor.
    is_synth = model == synthesizer_model()
    try:
        return _dispatch(
            p,
            model=model,
            system=system,
            user_payload=user_payload,
            max_tokens=max_tokens,
            temperature=temperature,
            llm_attempts=llm_attempts,
        )
    except Exception as exc:  # noqa: BLE001 - classified immediately below
        retryable, reason = _is_retryable(exc)
        target = _fallback_target(p) if (allow_fallback and fallback_enabled()) else None
        if not retryable or target is None:
            raise
        fb_model = _model_for(target, synth=is_synth)
        _log.warning(
            "llm_fallback from=%s to=%s reason=%s primary_model=%s fallback_model=%s max_tokens=%s",
            p,
            target,
            reason,
            model,
            fb_model,
            max_tokens,
        )
        try:
            data = _dispatch(
                target,
                model=fb_model,
                system=system,
                user_payload=user_payload,
                max_tokens=max_tokens,
                temperature=temperature,
                llm_attempts=llm_attempts,
            )
        except Exception:
            _log.exception("llm_fallback_also_failed from=%s to=%s", p, target)
            raise exc from None  # surface the ORIGINAL error, not the second one
        if llm_attempts is not None and llm_attempts:
            # attempts >= 11 marks a fallback-served turn for telemetry. timing_ms
            # already carries *_attempts keys, so this needs no schema change.
            llm_attempts[:] = [llm_attempts[0] + 10]
        _log.info("llm_fallback_ok from=%s to=%s model=%s", p, target, fb_model)
        return data
