from typing import Any

from app.context import build_system_prompt, format_user_context
from app.lana_ui import parse_turn_ui

BUCKET_GUIDE = """
UI buckets (for highlights and cards — pick one per focus):
- heritage — culture, ethnicity vibe, where family is from (NOT race taxonomy)
- stage — parenting, relationship, life phase, kids
- vicinity — new to area, block, neighborhood
- faith — religion, church, worship (mutual disclosure if saved as claim)
- activity — sports, running, hobbies, food interests
- interest — social style, what they want to find on the block
- general — only if nothing else fits
"""

LANA_TURN_SUFFIX = f"""
You are in a live chat. Use the conversation history and the user's latest message.

{BUCKET_GUIDE}

When asking a follow-up, QUOTE a short phrase from what the user said (focus_phrase) in assistant_message,
e.g. "Latino mom" — which corner? Mexican, Cuban, Puerto Rican...?
Warm tone; at most 2 questions per turn.

Output ONLY valid JSON (no markdown):
{{
  "assistant_message": "Your reply (include quoted focus phrase when clarifying)",
  "status": "continue" | "ready_to_complete",
  "topics_covered": ["heritage", "stage", ...],
  "topics_to_explore": ["optional tags still worth asking"],
  "ui": {{
    "bucket": "heritage" | "stage" | "vicinity" | "faith" | "activity" | "interest" | "general",
    "focus_phrase": "short exact quote from USER text you are asking about (null on welcome only)",
    "highlights": [
      {{ "text": "phrase from user story", "bucket": "heritage" }}
    ]
  }}
}}

Rules:
- status "continue" — still missing context; set ui.focus_phrase to the phrase you are clarifying.
- status "ready_to_complete" — enough for profile; ui.focus_phrase may be null; highlights may list key phrases from whole chat.
- highlights: 1-3 short phrases from the USER's words (latest message or earlier), each with a bucket.
- Never ask about race, exact age, sex, or street address.
"""

OPENING_INSTRUCTION = """
The user just started profile intake. No prior chat.
Welcome them to TagAlng on their block; invite them to share their story in their own words.

Output ONLY valid JSON:
{
  "assistant_message": "...",
  "status": "continue",
  "topics_covered": [],
  "topics_to_explore": ["opening"],
  "ui": {
    "bucket": null,
    "focus_phrase": null,
    "highlights": []
  }
}
"""


def _format_history(messages: list[dict[str, Any]]) -> str:
    if not messages:
        return "(no messages yet)"
    lines: list[str] = []
    for m in messages:
        role = m.get("role", "user")
        who = "User" if role == "user" else "Lana"
        lines.append(f"{who}: {m.get('content', '').strip()}")
    return "\n".join(lines)


def _parse_turn(data: Any) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    if not isinstance(data, dict):
        raise ValueError("invalid_turn_json")
    assistant_message = str(data.get("assistant_message", "")).strip()[:1200]
    if not assistant_message:
        assistant_message = "Tell me a bit about you — I'd love to hear your story."
    status = str(data.get("status", "continue")).lower()
    if status not in ("continue", "ready_to_complete"):
        status = "continue"
    covered = data.get("topics_covered", [])
    explore = data.get("topics_to_explore", [])
    if not isinstance(covered, list):
        covered = []
    if not isinstance(explore, list):
        explore = []
    covered = [str(x)[:64] for x in covered[:12]]
    explore = [str(x)[:64] for x in explore[:12]]
    ui = parse_turn_ui(data)
    ctx = {
        "topics_covered": covered,
        "topics_to_explore": explore,
        "last_status": status,
        "last_ui": ui,
    }
    return assistant_message, status, ctx, ui


LANA_MAX_OUTPUT_TOKENS = 1024  # matches vertex_event.EVENT_MAX_OUTPUT_TOKENS


def _call_lana(payload: str) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    from app.orchestrator.llm import vertex_generate_json

    # parse_json_object, not json.loads — models emit fenced/near-JSON and a
    # single stray markdown fence used to 502 the whole turn here.
    data = vertex_generate_json(
        system=build_system_prompt() + "\n\n" + LANA_TURN_SUFFIX,
        user_payload=payload,
        max_tokens=LANA_MAX_OUTPUT_TOKENS,
        temperature=0.55,
    )
    return _parse_turn(data)


def lana_opening(user_context_block: str, purpose: str) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    payload = "\n\n".join([user_context_block, OPENING_INSTRUCTION])
    return _call_lana(payload)


def lana_turn(
    user_context_block: str,
    purpose: str,
    history: list[dict[str, Any]],
    user_message: str,
) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    payload = "\n\n".join(
        [
            user_context_block,
            "CONVERSATION SO FAR:\n" + _format_history(history),
            f"USER'S NEW MESSAGE:\n{user_message.strip()}",
            "Reply as Lana. Set ui.focus_phrase to a phrase from the user's words when you ask a clarifying question.",
        ]
    )
    return _call_lana(payload)
