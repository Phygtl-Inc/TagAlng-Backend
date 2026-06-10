"""Profile intake fast path — single LLM call, short heritage-focused chat."""

import json
import os
import re
from typing import Any

from app.event_context import format_chat_history, host_display_name
from app.lana_ui import parse_turn_ui
from app.turn_timing import TurnTimer

PROFILE_HISTORY_MAX = 8
PROFILE_MAX_OUTPUT_TOKENS = 2048
ASSISTANT_MESSAGE_MAX_CHARS = 320

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
You are in a live **profile intake** chat. Keep it short: heritage + one more thread, then name (if missing), then wrap up.

{BUCKET_GUIDE}

When asking a follow-up, QUOTE a short phrase from what the user said (`focus_phrase`).
Warm tone; **at most one question per turn**. Do not repeat questions they already answered.

Output ONLY valid JSON (no markdown):
{{
  "assistant_message": "Your reply (include quoted focus phrase when clarifying)",
  "status": "continue" | "ready_to_complete",
  "topics_covered": ["heritage", "activity", ...],
  "topics_to_explore": [],
  "profile_patch": {{
    "nickname": "what neighbors should call them, or null",
    "full_name": "full name if they gave one, else null"
  }},
  "ui": {{
    "bucket": "heritage" | "stage" | "vicinity" | "faith" | "activity" | "interest" | "general",
    "focus_phrase": "short exact quote from USER text (null when wrapping up)",
    "highlights": [
      {{ "text": "phrase from user story", "bucket": "heritage" }}
    ]
  }}
}}

Rules:
- assistant_message: **max 2 short sentences, under 240 characters** — complete sentences only; never trail off mid-thought.
- status "continue" — still missing heritage, a second thread, display name (when HOST CONTEXT says so), or a gentle kids follow-up for moms.
- status "ready_to_complete" — story is enough AND display name is on file or in profile_patch; invite tap Complete.
- profile_patch: set nickname/full_name when the user tells you what to call them; otherwise null fields.
- highlights: 1-4 short phrases from the USER's words, each with a bucket.
- Keep JSON compact — short arrays, no commentary outside JSON.
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


_MOM_SIGNAL = re.compile(r"\b(mom|mother|parent|mama|kids?|toddler|baby|babies|children)\b", re.I)
_KIDS_DETAIL = re.compile(
    r"\b(\d+\s*(yo|yr|year|month|mo)s?|toddler|infant|newborn|twins|under\s+\d|"
    r"age\s+\d|ages?\s+\d|\d+\s+and\s+\d|two\s+kids|2\s+kids)\b",
    re.I,
)


def profile_intake_gaps(ctx: dict[str, Any]) -> dict[str, bool]:
    nick = str(ctx.get("nickname") or "").strip()
    full = str(ctx.get("full_name") or "").strip()
    return {
        "needs_display_name": not nick and not full,
    }


def parse_profile_patch(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, max_len in (("nickname", 30), ("full_name", 80)):
        val = str(raw.get(key) or "").strip()[:max_len]
        if val and val.lower() not in ("none", "null", "n/a"):
            out[key] = val
    return out


def _conversation_text(history: list[dict[str, Any]], ui: dict[str, Any]) -> str:
    parts: list[str] = []
    for msg in history:
        if msg.get("role") == "user":
            parts.append(str(msg.get("content") or ""))
    for item in ui.get("highlights") or []:
        parts.append(str(item.get("text") or ""))
    return " ".join(parts)


def needs_kids_followup(
    *,
    history: list[dict[str, Any]],
    ui: dict[str, Any],
    topics_covered: list[str],
) -> bool:
    text = _conversation_text(history, ui) + " " + " ".join(topics_covered)
    if not _MOM_SIGNAL.search(text):
        return False
    if _KIDS_DETAIL.search(text):
        return False
    if any(_normalize_topic(t) == "stage" for t in topics_covered):
        return "kid" not in text.lower() and "child" not in text.lower()
    return True


def format_profile_intake_context(ctx: dict[str, Any]) -> str:
    lines = ["HOST CONTEXT (minimal — profile intake only):"]
    guest_step = str(ctx.get("guest_step") or "")
    if ctx.get("guest_intake"):
        if guest_step == "post_verify":
            lines.append(
                "- Guest intake (phone verified): collect remaining profile details "
                "(kids ages, interests). Do not ask for phone. Intro to neighbor is queued."
            )
        elif guest_step == "intro_declined":
            lines.append(
                "- Guest intake: user skipped neighbor intro — finish heritage, interests, "
                "and display name. Do not ask for phone."
            )
        else:
            lines.append(
                "- Guest intake (anonymous): ask life stage and heritage first — "
                "one question per turn. Do not ask for phone, block, or display name yet "
                "(joint-moment intro handles name after they accept)."
            )
    gaps = profile_intake_gaps(ctx)
    name = host_display_name(ctx)
    if name:
        lines.append(f"- Neighbor name (use in greeting): {name}")
    elif gaps["needs_display_name"]:
        lines.append(
            "- Display name: MISSING — after heritage/interests, ask indirectly "
            'what neighbors should call them; save in profile_patch.nickname'
        )
    if ctx.get("block_display_name"):
        lines.append(f"- Block: {ctx['block_display_name']}")
    elif ctx.get("home_block_id"):
        lines.append("- Block: assigned")
    elif ctx.get("guest_intake"):
        lines.append("- Block: not assigned yet (normal for guest — do not ask)")
    return "\n".join(lines)


GUEST_PROFILE_OPENING = (
    "So — who are you, right now? "
    "Tell me your life stage and what you're hoping to find on the block."
)


def lana_profile_guest_opening() -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    """Instant opening for anonymous Meet-Lana flow (no LLM call)."""
    ui: dict[str, Any] = {
        "bucket": "stage",
        "focus_phrase": None,
        "highlights": [],
    }
    ctx: dict[str, Any] = {
        "topics_covered": [],
        "topics_to_explore": ["heritage", "stage"],
        "last_status": "continue",
        "last_ui": ui,
        "guest_intake": True,
        "guest_step": "early_chat",
        "last_routing": {
            "outcome": "R",
            "intent_class": "identity",
            "confidence": 1.0,
            "tool_to_call": None,
            "guest_fast_opening": True,
        },
    }
    return GUEST_PROFILE_OPENING, "continue", ctx, ui


def assistant_message_looks_truncated(msg: str) -> bool:
    """Detect mid-sentence cutoffs from truncated LLM JSON (e.g. ends with 'We've got a')."""
    s = str(msg or "").strip()
    if len(s) < 24:
        return False
    if s[-1] in ".!?":
        return False
    if s.endswith('"') and s.count('"') % 2 == 0:
        return False
    return s[-1].isalnum() or s[-1] in ",;:(-"


def _clamp_assistant_message(msg: str) -> str:
    text = str(msg or "").strip()
    if len(text) <= ASSISTANT_MESSAGE_MAX_CHARS:
        return text
    cut = text[:ASSISTANT_MESSAGE_MAX_CHARS]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(".,;:") + "."


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
    profile_gaps: dict[str, bool] | None = None,
    display_name_saved: bool = False,
    profile_patch: dict[str, str] | None = None,
    guest_step: str | None = None,
) -> tuple[str, str]:
    """Code enforcement: short intake, with optional name + mom/kids beats."""
    if guest_step and guest_step not in ("post_verify", "intro_declined"):
        if status == "ready_to_complete":
            status = "continue"
        return assistant_message, status
    user_turns = count_user_turns(history)
    buckets = collect_profile_buckets(history=history, ui=ui, topics_covered=topics_covered)
    has_heritage = bool(buckets & HERITAGE_SIGNALS) or any(
        _normalize_topic(t) == "heritage" for t in topics_covered
    )
    has_other = bool(buckets - HERITAGE_SIGNALS)
    highlight_count = len(ui.get("highlights") or [])
    gaps = profile_gaps or {}
    has_name = display_name_saved or bool(profile_patch) or not gaps.get("needs_display_name")
    kids_gap = needs_kids_followup(history=history, ui=ui, topics_covered=topics_covered)

    claims_ready = has_heritage and (has_other or highlight_count >= 2)
    force = False
    if user_turns >= 5:
        force = True
    elif claims_ready and has_name and not kids_gap and user_turns >= 2:
        force = True
    elif user_turns >= 4 and has_name:
        force = True
    elif user_turns >= 3 and claims_ready and has_name:
        force = True

    if claims_ready and gaps.get("needs_display_name") and not has_name and user_turns < 5:
        force = False
    if kids_gap and user_turns < 4 and claims_ready and has_name:
        force = False

    if not force:
        if status == "ready_to_complete":
            if gaps.get("needs_display_name") and not has_name:
                status = "continue"
            elif kids_gap:
                status = "continue"
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
    profile_gaps: dict[str, bool] | None = None,
    display_name_saved: bool = False,
    guest_step: str | None = None,
) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    if not isinstance(data, dict):
        raise ValueError("invalid_turn_json")
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
    assistant_message = _clamp_assistant_message(str(data.get("assistant_message", "")))
    if not assistant_message or assistant_message_looks_truncated(assistant_message):
        gaps = profile_gaps or {}
        if gaps.get("needs_display_name"):
            assistant_message = "Love that — what should neighbors call you on the block?"
        elif needs_kids_followup(history=history, ui=ui, topics_covered=covered):
            assistant_message = "Little ones at home, or mostly grown?"
        else:
            assistant_message = "What do you hope to find or do on the block?"
    profile_patch = parse_profile_patch(data.get("profile_patch"))
    assistant_message, status = apply_profile_stop_rules(
        status,
        assistant_message,
        history=history,
        ui=ui,
        topics_covered=covered,
        profile_gaps=profile_gaps,
        display_name_saved=display_name_saved,
        profile_patch=profile_patch,
        guest_step=guest_step,
    )
    ctx: dict[str, Any] = {
        "topics_covered": covered,
        "topics_to_explore": explore,
        "last_status": status,
        "last_ui": ui,
    }
    if profile_patch:
        ctx["profile_patch"] = profile_patch
        ctx["display_name_saved"] = True
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

    _RETRY_SUFFIX = (
        "\n\nYour previous reply was invalid or truncated. "
        "Return ONE compact JSON object. "
        "assistant_message: max 2 short sentences, under 240 characters, must end with . or ?"
    )

    attempts = 1
    text = _generate(payload)
    try:
        data = parse_json_object(text)
    except (json.JSONDecodeError, ValueError):
        attempts = 2
        text = _generate(payload + _RETRY_SUFFIX)
        data = parse_json_object(text)
    else:
        msg = str(data.get("assistant_message", ""))
        if assistant_message_looks_truncated(msg):
            attempts = 2
            text = _generate(payload + _RETRY_SUFFIX)
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
    ctx_pack: dict[str, Any] | None = None,
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
    gaps = profile_intake_gaps(ctx_pack or {})
    return _parse_profile_turn(data, history=[], profile_gaps=gaps)


def lana_profile_turn(
    user_context_block: str,
    history: list[dict[str, Any]],
    user_message: str,
    *,
    ctx_pack: dict[str, Any] | None = None,
    session_ctx: dict[str, Any] | None = None,
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
    gaps = profile_intake_gaps(ctx_pack or {})
    saved = bool((session_ctx or {}).get("display_name_saved"))
    guest_step = str((session_ctx or {}).get("guest_step") or "") or None
    return _parse_profile_turn(
        data,
        history=history,
        profile_gaps=gaps,
        display_name_saved=saved,
        guest_step=guest_step,
    )
