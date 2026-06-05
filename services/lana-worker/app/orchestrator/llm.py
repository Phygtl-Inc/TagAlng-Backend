"""Orchestrator LLM calls — Vertex Gemini (default) or Claude via env."""

import json
import os
import re
from typing import Any

_gemini_client: Any = None
_claude_client: Any = None


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


def _strip_json_fence(text: str) -> str:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    return raw.strip()


def _parse_json_object(text: str) -> dict[str, Any]:
    data = json.loads(_strip_json_fence(text) or "{}")
    if not isinstance(data, dict):
        raise ValueError("llm_json_not_object")
    return data


def _gemini_client():
    global _gemini_client
    if _gemini_client is not None:
        return _gemini_client
    project = os.environ.get("GCP_VERTEX_PROJECT", "")
    location = os.environ.get("GCP_VERTEX_LOCATION", "us-central1")
    if not project:
        raise RuntimeError("GCP_VERTEX_PROJECT not set")
    from google import genai

    _gemini_client = genai.Client(vertexai=True, project=project, location=location)
    return _gemini_client


def _claude_client():
    global _claude_client
    if _claude_client is not None:
        return _claude_client
    project = os.environ.get("GCP_VERTEX_PROJECT", "")
    if not project:
        raise RuntimeError("GCP_VERTEX_PROJECT not set")
    from anthropic import AnthropicVertex

    region = os.environ.get(
        "VERTEX_CLAUDE_REGION",
        os.environ.get("GCP_VERTEX_LOCATION", "us-east1"),
    )
    _claude_client = AnthropicVertex(project_id=project, region=region)
    return _claude_client


def _gemini_json(
    *,
    model: str,
    system: str,
    user_payload: str,
    max_tokens: int,
    temperature: float,
) -> dict[str, Any]:
    from google.genai import types

    client = _gemini_client()
    response = client.models.generate_content(
        model=model,
        contents=user_payload,
        config=types.GenerateContentConfig(
            temperature=temperature,
            max_output_tokens=max_tokens,
            response_mime_type="application/json",
            system_instruction=system,
        ),
    )
    return _parse_json_object(response.text or "")


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
        system=system,
        messages=[{"role": "user", "content": user_payload}],
    )
    parts: list[str] = []
    for block in response.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return _parse_json_object("".join(parts))


def llm_json(
    *,
    model: str,
    system: str,
    user_payload: str,
    max_tokens: int = 1024,
    temperature: float = 0.2,
) -> dict[str, Any]:
    if provider() == "claude":
        return _claude_json(
            model=model,
            system=system,
            user_payload=user_payload,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    return _gemini_json(
        model=model,
        system=system,
        user_payload=user_payload,
        max_tokens=max_tokens,
        temperature=temperature,
    )
