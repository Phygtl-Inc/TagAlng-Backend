"""Backward-compatible re-exports — prefer app.orchestrator.llm."""

from app.orchestrator.llm import (
    llm_configured,
    llm_json,
    provider,
    router_model,
    synthesizer_model,
)

claude_configured = llm_configured
claude_json = llm_json

__all__ = [
    "claude_configured",
    "claude_json",
    "llm_configured",
    "llm_json",
    "provider",
    "router_model",
    "synthesizer_model",
]
