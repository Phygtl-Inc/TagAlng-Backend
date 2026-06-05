"""Orchestrator LLM calls — Vertex Gemini (default) or Claude via env."""

import json
import os
from typing import Any

from app.orchestrator.json_util import parse_json_object

_gemini_client_instance: Any = None
_claude_client_instance: Any = None

_JSON_RULES = (
    "\n\nReturn strictly valid JSON. Double-quoted keys/strings only. "
    "No trailing commas. No | union syntax. "
    "Keep assistant_message on ONE line (no raw line breaks)."
)


def provider() -> str:
    p = os.environ.get("LANA_LLM_PROVIDER", "gemini").strip().lower()
    if p in ("claude", "anthropic"):
        return "claude"
    return "gemini"


def llm_configured() -> bool:
    return bool(os.environ.get("GCP_VERTEX_PROJECT", "").strip())


def router_model() -> str:
    explicit = os.environ.get("VERTEX_LANA_ROUTER_MODEL", "").strip()
    if explicit:
        return explicit
    legacy = os.environ.get("VERTEX_CLAUDE_ROUTER_MODEL", "").strip()
    if legacy:
        return legacy
    return "claude-haiku-4-5@20251001" if provider() == "claude" else "gemini-2.5-flash"


def synthesizer_model() -> str:
    explicit = os.environ.get("VERTEX_LANA_SYNTH_MODEL", "").strip()
    if explicit:
        return explicit
    legacy = os.environ.get("VERTEX_CLAUDE_SYNTH_MODEL", "").strip()
    if legacy:
        return legacy
    return "claude-sonnet-4-6" if provider() == "claude" else "gemini-2.5-pro"


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
    if provider() == "claude":
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
