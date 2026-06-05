import json
import os
import re
from typing import Any

_client: Any = None


def claude_configured() -> bool:
    return bool(os.environ.get("GCP_VERTEX_PROJECT", "").strip())


def router_model() -> str:
    return os.environ.get(
        "VERTEX_CLAUDE_ROUTER_MODEL",
        "claude-haiku-4-5@20251001",
    )


def synthesizer_model() -> str:
    return os.environ.get(
        "VERTEX_CLAUDE_SYNTH_MODEL",
        "claude-sonnet-4-6",
    )


def claude_region() -> str:
    return os.environ.get("VERTEX_CLAUDE_REGION", os.environ.get("GCP_VERTEX_LOCATION", "us-east1"))


def _client_instance():
    global _client
    if _client is not None:
        return _client
    project = os.environ.get("GCP_VERTEX_PROJECT", "")
    if not project:
        raise RuntimeError("GCP_VERTEX_PROJECT not set")
    from anthropic import AnthropicVertex

    _client = AnthropicVertex(project_id=project, region=claude_region())
    return _client


def _strip_json_fence(text: str) -> str:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    return raw.strip()


def claude_json(
    *,
    model: str,
    system: str,
    user_payload: str,
    max_tokens: int = 1024,
    temperature: float = 0.2,
) -> dict[str, Any]:
    client = _client_instance()
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
    text = _strip_json_fence("".join(parts))
    data = json.loads(text or "{}")
    if not isinstance(data, dict):
        raise ValueError("claude_json_not_object")
    return data
