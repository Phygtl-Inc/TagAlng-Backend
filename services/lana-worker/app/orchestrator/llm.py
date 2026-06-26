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
    response = client.chat.completions.create(**params)
    choice = response.choices[0].message.content if response.choices else None
    return choice or ""


def _openai_json(
    *,
    model: str,
    system: str,
    user_payload: str,
    max_tokens: int,
    temperature: float,
) -> tuple[dict[str, Any], int]:
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
            if model != router_model():
                attempts = 3
                mini_text = _openai_generate(
                    model=router_model(),
                    system=system,
                    user_payload=user_payload,
                    max_tokens=max_tokens,
                    temperature=0.15,
                )
                return parse_json_object(mini_text), attempts
            raise


def _gemini_generate(*, model: str, system: str, user_payload: str, max_tokens: int, temperature: float) -> str:
    from google.genai import types

    client = _gemini_client()
    response = client.models.generate_content(
        model=model,
        contents=user_payload,
        config=types.GenerateContentConfig(
            temperature=temperature,
            max_output_tokens=max_tokens,
            response_mime_type="application/json",
            system_instruction=system + _JSON_RULES,
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
) -> tuple[dict[str, Any], int]:
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
            if model != router_model():
                attempts = 3
                flash_text = _gemini_generate(
                    model=router_model(),
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
        data = _claude_json(
            model=model,
            system=system,
            user_payload=user_payload,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        if llm_attempts is not None:
            llm_attempts[:] = [1]
        return data
    data, attempts = _gemini_json(
        model=model,
        system=system,
        user_payload=user_payload,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    if llm_attempts is not None:
        llm_attempts[:] = [attempts]
    return data
