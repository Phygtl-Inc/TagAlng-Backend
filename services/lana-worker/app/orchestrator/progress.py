"""Human-readable progress labels for streaming turns.

The streaming endpoint surfaces these as `{"type":"status","label":...}` SSE frames
while a turn is being computed. The first label is generic (we don't know intent yet);
every label after routing reflects the router's real decision, so a user hosting a meet
and a user hunting for neighbors see genuinely different words.
"""

from __future__ import annotations

from typing import Any

# Shown before we know anything — the router LLM call hasn't returned yet.
READING = "Reading your message…"
# Shown just before the synthesizer LLM call composes the reply.
COMPOSING = "Composing…"

# intent_class → label. Keep in sync with the router's intent taxonomy
# (see orchestrator/router.py). Unknown intents fall back to COMPOSING.
_BY_INTENT: dict[str, str] = {
    "discovery": "Finding people near you…",
    "activity": "Setting up your meet…",
    "swap": "Listing your item…",
    "marketplace": "Listing your item…",
    "identity": "Getting to know you…",
    "companionship": "Composing…",
    "meta": "Composing…",
}

# tool_to_call → label. Wins over intent when a specific tool is about to run, so the
# label names the actual work ("Finding neighbors…") rather than the broad category.
_BY_TOOL: dict[str, str] = {
    "find_peers": "Finding people near you…",
    "publish_activity": "Publishing your meet…",
    "update_event_draft": "Setting up your meet…",
    "propose_intro": "Introducing you…",
    "propose_cohost": "Setting up your meet…",
    "list_my_intros": "Checking your intros…",
    "save_local_signal": "Saving that…",
    "capture_inquiry": "Noting that down…",
}


def label_for_routing(routing: dict[str, Any] | None) -> str:
    """Pick the most specific honest label for a decided routing."""
    if not isinstance(routing, dict):
        return COMPOSING
    tool = routing.get("tool_to_call")
    if isinstance(tool, str) and tool in _BY_TOOL:
        return _BY_TOOL[tool]
    intent = routing.get("intent_class")
    if isinstance(intent, str) and intent in _BY_INTENT:
        return _BY_INTENT[intent]
    return COMPOSING
