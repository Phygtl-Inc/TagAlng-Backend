"""Profile intake fast path — single LLM call, short heritage-focused chat."""

import json
import os
from typing import Any

from app.event_context import format_chat_history, host_display_name
from app.lana_ui import parse_turn_ui
from app.turn_timing import TurnTimer

PROFILE_HISTORY_MAX = 8
PROFILE_MAX_OUTPUT_TOKENS = 1024

_vertex_client_instance: Any = None

HERITAGE_SIGNALS = frozenset({"heritage"})
SUBSTANCE_BUCKETS = frozenset(
    {"heritage", "stage", "vicinity", "faith", "activity", "interest"}
)

BUCKET_GUIDE = """
UI buckets (highlights — pick one per focus):
- heritage — culture, ethnicity vibe, where family is from (NOT race taxonomy)
- stage — parenting, relationship, life phase, kids
- vicinity — new to area, block, neighborhood
- faith — religion, church, worship
- activity — sports, running, hobbies, food interests
- interest — social style, what they want to find on the block
- general — only if nothing else fits
"""

PROFILE_TURN_SUFFIX = f"""
You are in a live **profile intake** chat. Keep it short: heritage + one more thread, then wrap up.

{BUCKET_GUIDE}

When asking a follow-up, QUOTE a short phrase from what the user said (`focus_phrase`).
Warm tone; **at most one question per turn**. Do not repeat questions they already answered.

Output ONLY valid JSON (no markdown):
{{
  "assistant_message": "Your reply (include quoted focus phrase when clarifying)",
  "status": "continue" | "ready_to_complete",
  "topics_covered": ["heritage", "activity", ...],
  "topics_to_explore": [],
  "ui": {{
    "bucket": "heritage" | "stage" | "vicinity" | "faith" | "activity" | "interest" | "general",
    "focus_phrase": "short exact quote from USER text (null when wrapping up)",
    "highlights": [
      {{ "text": "phrase from user story", "bucket": "heritage" }}
    ]
  }}
}}

Rules:
- status "continue" — still missing heritage OR a second thread; set ui.focus_phrase when clarifying.
- status "ready_to_complete" — heritage + at least one other bucket are clear; invite tap Complete.
- highlights: 1-4 short phrases from the USER's words, each with a bucket.
- Never ask about race, exact age, sex, or street address.
"""

PROFILE_OPENING = """
The user just started profile intake. No prior chat.
Welcome them to TagAlng on their block. In ONE message, greet them and ask:
where their family heritage traces back to, and what they hope to find or do on the block.

Output ONLY valid JSON:
{
  "assistant_message": "...",
  "status": "continue",
  "topics_covered": [],
  "topics_to_explore": ["heritage", "interest"],
  "ui": {
    "bucket": null,
    "focus_phrase": null,
    "highlights": []
  }
}
"""


def format_profile_intake_context(ctx: dict[str, Any]) -> str:
    lines = ["HOST CONTEXT (minimal — profile intake only):"]
    name = host_display_name(ctx)
    if name:
        lines.append(f"- Neighbor name (use in greeting): {name}")
    if ctx.get("block_display_name"):
        lines.append(f"- Block: {ctx['block_display_name']}")
    elif ctx.get("home_block_id"):
        lines.append("- Block: assigned")
    return "\n".join(lines)


def count_user_turns(history: list[dict[str, Any]]) -> int:
    return sum(1 for m in history if m.get("role") == "user")


def _normalize_topic(topic: str) -> str:
    return str(topic).strip().lower()[:64]


def collect_profile_buckets(
    *,
    history: list[dict[str, Any]],
    ui: dict[str, Any],
    topics_covered: list[str],
) -> set[str]:
    buckets: set[str] = set()
    for topic in topics_covered:
        t = _normalize_topic(topic)
        if t in SUBSTANCE_BUCKETS:
            buckets.add(t)
    for item in ui.get("highlights") or []:
        b = str(item.get("bucket") or "").strip().lower()
        if b in SUBSTANCE_BUCKETS:
            buckets.add(b)
    for msg in history:
        meta = msg.get("metadata") if isinstance(msg.get("metadata"), dict) else {}
        past_ui = meta.get("ui") if isinstance(meta.get("ui"), dict) else {}
        for item in past_ui.get("highlights") or []:
            b = str(item.get("bucket") or "").strip().lower()
            if b in SUBSTANCE_BUCKETS:
                buckets.add(b)
    return buckets


def apply_profile_stop_rules(
    status: str,
    assistant_message: str,
    *,
    history: list[dict[str, Any]],
    ui: dict[str, Any],
    topics_covered: list[str],
) -> tuple[str, str]:
    """Code enforcement: 2–3 user turns then ready_to_complete."""
    user_turns = count_user_turns(history)
    buckets = collect_profile_buckets(history=history, ui=ui, topics_covered=topics_covered)
    has_heritage = bool(buckets & HERITAGE_SIGNALS) or any(
        _normalize_topic(t) == "heritage" for t in topics_covered
    )
    has_other = bool(buckets - HERITAGE_SIGNALS)
    highlight_count = len(ui.get("highlights") or [])

    force = False
    if user_turns >= 4:
        force = True
    elif user_turns >= 3:
        force = True
    elif user_turns >= 2 and has_heritage and (has_other or highlight_count >= 2):
        force = True

    if not force:
        return assistant_message, status

    status = "ready_to_complete"
    lower = assistant_message.lower()
    if "complete" not in lower and "tap" not in lower and "save" not in lower:
        assistant_message = (
            assistant_message.rstrip()
            + " When you're ready, tap Complete to save your profile."
        )
    return assistant_message[:1200], status


def _vertex_client():
    global _vertex_client_instance
    if _vertex_client_instance is not None:
        return _vertex_client_instance
    project = os.environ.get("GCP_VERTEX_PROJECT", "")
    location = os.environ.get("GCP_VERTEX_LOCATION", "us-central1")
    if not project:
        raise RuntimeError("GCP_VERTEX_PROJECT not set")
    from google import genai

    _vertex_client_instance = genai.Client(vertexai=True, project=project, location=location)
    return _vertex_client_instance


def _parse_profile_turn(
    data: Any,
    *,
    history: list[dict[str, Any]],
) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    if not isinstance(data, dict):
        raise ValueError("invalid_turn_json")
    assistant_message = str(data.get("assistant_message", "")).strip()[:1200]
    if not assistant_message:
        assistant_message = "Tell me a bit about you — where does your family heritage trace back to?"
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
    assistant_message, status = apply_profile_stop_rules(
        status,
        assistant_message,
        history=history,
        ui=ui,
        topics_covered=covered,
    )
    ctx = {
        "topics_covered": covered,
        "topics_to_explore": explore,
        "last_status": status,
        "last_ui": ui,
    }
    return assistant_message, status, ctx, ui


def _call_profile_lana(
    payload: str,
    *,
    attempts_out: list[int] | None = None,
) -> dict[str, Any]:
    from app.context import build_profile_system_prompt
    from app.orchestrator.json_util import parse_json_object

    client = _vertex_client()
    model = os.environ.get("VERTEX_LANA_MODEL", os.environ.get("VERTEX_EXTRACT_MODEL", "gemini-2.5-flash"))
    from google.genai import types

    system = build_profile_system_prompt() + "\n\n---\n\n" + PROFILE_TURN_SUFFIX

    def _generate(user: str) -> str:
        response = client.models.generate_content(
            model=model,
            contents=user,
            config=types.GenerateContentConfig(
                temperature=0.45,
                max_output_tokens=PROFILE_MAX_OUTPUT_TOKENS,
                response_mime_type="application/json",
                system_instruction=system,
            ),
        )
        return response.text or ""

    attempts = 1
    text = _generate(payload)
    try:
        data = parse_json_object(text)
    except (json.JSONDecodeError, ValueError):
        attempts = 2
        text = _generate(
            payload
            + "\n\nYour previous reply was invalid JSON. "
            "Return ONE compact JSON object with assistant_message, status, and ui."
        )
        data = parse_json_object(text)
    if attempts_out is not None:
        attempts_out[:] = [attempts]
    return data


def _profile_opening_payload(user_context_block: str, host_name: str | None) -> str:
    if host_name:
        name_rule = (
            f'The neighbor\'s name is "{host_name}". '
            f'assistant_message MUST open with a greeting that uses their name '
            f'(e.g. "Hi {host_name}! I\'m Lana — where does your family heritage trace back to, '
            f'and what do you hope to find on the block?").'
        )
    else:
        name_rule = "No name on file — use a warm generic greeting."
    return "\n\n".join([user_context_block, PROFILE_OPENING.strip(), name_rule])


def lana_profile_opening(
    user_context_block: str,
    *,
    host_name: str | None = None,
    timer: TurnTimer | None = None,
) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    payload = _profile_opening_payload(user_context_block, host_name)
    attempts_box: list[int] = []
    if timer:
        with timer.stage("llm_profile_turn"):
            data = _call_profile_lana(payload, attempts_out=attempts_box)
        if attempts_box:
            timer.set_count("llm_profile_attempts", attempts_box[0])
    else:
        data = _call_profile_lana(payload)
    return _parse_profile_turn(data, history=[])


def lana_profile_turn(
    user_context_block: str,
    history: list[dict[str, Any]],
    user_message: str,
    *,
    timer: TurnTimer | None = None,
) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    payload = "\n\n".join(
        [
            user_context_block,
            "CONVERSATION SO FAR:\n"
            + format_chat_history(history, max_messages=PROFILE_HISTORY_MAX),
            f"USER'S NEW MESSAGE:\n{user_message.strip()}",
            "Reply as Lana. One question max. Set ui.highlights from the user's words.",
        ]
    )
    attempts_box: list[int] = []
    if timer:
        with timer.stage("llm_profile_turn"):
            data = _call_profile_lana(payload, attempts_out=attempts_box)
        if attempts_box:
            timer.set_count("llm_profile_attempts", attempts_box[0])
    else:
        data = _call_profile_lana(payload)
    return _parse_profile_turn(data, history=history)
