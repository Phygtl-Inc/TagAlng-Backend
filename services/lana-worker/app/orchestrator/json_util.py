"""Parse/repair LLM JSON without pulling the full orchestrator stack."""

import json
import re
from typing import Any


def strip_json_fence(text: str) -> str:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    return raw.strip()


def repair_json_text(text: str) -> str:
    s = text.strip()
    s = re.sub(
        r':\s*"([^"]*?)"\s*(?:\|\s*"[^"]*?")+',
        r': "\1"',
        s,
    )
    s = re.sub(
        r'"bucket"\s*:\s*"([^"]+)"\s*(?:\|\s*"[^"]+")+',
        r'"bucket": "\1"',
        s,
    )
    s = re.sub(
        r'"disclosure"\s*:\s*"([^"]+)"\s*(?:\|\s*"[^"]+")+',
        r'"disclosure": "\1"',
        s,
    )
    s = re.sub(r",\s*}", "}", s)
    s = re.sub(r",\s*]", "]", s)
    return s


def _extract_json_string(raw: str, field: str) -> str | None:
    m = re.search(rf'"{field}"\s*:\s*"((?:[^"\\]|\\.)*)"', raw, re.DOTALL)
    if not m:
        return None
    return m.group(1).replace("\\n", " ").replace("\n", " ")


def _salvage_claims(raw: str) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _append_claim(block: str, concept: str) -> None:
        if concept in seen:
            return
        seen.add(concept)
        label = re.search(r'"label"\s*:\s*"((?:[^"\\]|\\.)*)"', block)
        conf = re.search(r'"confidence"\s*:\s*([0-9.]+)', block)
        disc = re.search(r'"disclosure"\s*:\s*"(public|mutual|private)"', block)
        bucket = re.search(r'"bucket"\s*:\s*"([a-z_]+)"', block)
        quote = re.search(r'"source_quote"\s*:\s*"((?:[^"\\]|\\.)*)"', block)
        tone = re.search(r'"tone"\s*:\s*"((?:[^"\\]|\\.)*)"', block)
        claims.append(
            {
                "concept": concept,
                "label": label.group(1) if label else concept.replace("_", " "),
                "tone": tone.group(1) if tone else None,
                "confidence": float(conf.group(1)) if conf else 0.7,
                "disclosure": disc.group(1) if disc else "public",
                "synonyms": [],
                "source_quote": quote.group(1) if quote else "",
                "bucket": bucket.group(1) if bucket else "general",
            }
        )

    for m in re.finditer(r'"concept"\s*:\s*"([a-z][a-z0-9_]*)"', raw):
        concept = m.group(1)
        start = raw.rfind("{", 0, m.start())
        if start < 0:
            start = max(0, m.start() - 1)
        end = raw.find("}", m.end())
        block = raw[start : end + 1] if end >= 0 else raw[start:]
        _append_claim(block, concept)

    return claims


def salvage_extract_object(text: str) -> dict[str, Any] | None:
    raw = strip_json_fence(text)
    if not raw or ('"mapped_summary"' not in raw and '"claims"' not in raw):
        return None

    mapped = _extract_json_string(raw, "mapped_summary")
    assistant = _extract_json_string(raw, "assistant_message")
    claims = _salvage_claims(raw)
    if not mapped and not claims and not assistant:
        return None

    out: dict[str, Any] = {
        "mapped_summary": mapped or "Here's what I heard from our chat.",
        "spans": [],
        "claims": claims,
        "assistant_message": assistant or "I think I have a great picture of you now!",
    }
    return out


def _close_truncated_json(text: str) -> str:
    """Best-effort close a truncated JSON object."""
    s = text.rstrip()
    if not s.startswith("{"):
        return s
    in_string = False
    escape = False
    for ch in s:
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
    if in_string:
        s += '"'
    open_braces = s.count("{") - s.count("}")
    open_brackets = s.count("[") - s.count("]")
    s += "]" * max(0, open_brackets)
    s += "}" * max(0, open_braces)
    return repair_json_text(s)


def salvage_json_object(text: str) -> dict[str, Any] | None:
    raw = strip_json_fence(text)
    if not raw:
        return None

    assistant = re.search(
        r'"assistant_message"\s*:\s*"((?:[^"\\]|\\.)*)"',
        raw,
        re.DOTALL,
    )
    if not assistant:
        partial = re.search(r'"assistant_message"\s*:\s*"(.+)', raw, re.DOTALL)
        if partial:
            msg = partial.group(1)
            msg = re.split(r'"\s*,\s*"', msg)[0]
            msg = msg.rstrip('"').replace("\\n", " ").replace("\n", " ")
            if msg.strip():
                out: dict[str, Any] = {
                    "assistant_message": msg.strip()[:1200],
                    "status": "continue",
                    "topics_covered": [],
                    "topics_to_explore": [],
                    "ui": {"bucket": None, "focus_phrase": None, "highlights": []},
                }
                status = re.search(r'"status"\s*:\s*"(continue|ready_to_complete)"', raw)
                if status:
                    out["status"] = status.group(1)
                return out

    outcome = re.search(r'"outcome"\s*:\s*"([RATC])"', raw)
    if outcome:
        conf = re.search(r'"confidence"\s*:\s*([0-9.]+)', raw)
        return {
            "outcome": outcome.group(1),
            "intent_class": "companionship",
            "confidence": float(conf.group(1)) if conf else 0.7,
            "tool_to_call": None,
            "tool_args": None,
            "missing_slots": [],
            "sentiment": "neutral",
            "needs_confirmation": False,
            "thinking": "salvaged",
        }

    if assistant:
        out = {
            "assistant_message": assistant.group(1).replace("\\n", " ").replace("\n", " ")[:1200],
            "status": "continue",
            "topics_covered": [],
            "topics_to_explore": [],
            "ui": {"bucket": None, "focus_phrase": None, "highlights": []},
        }
        status = re.search(r'"status"\s*:\s*"(continue|ready_to_complete)"', raw)
        if status:
            out["status"] = status.group(1)
        return out

    extract = salvage_extract_object(raw)
    if extract:
        return extract

    return None


def parse_json_object(text: str) -> dict[str, Any]:
    raw = strip_json_fence(text or "")
    if not raw:
        raise json.JSONDecodeError("empty llm response", raw, 0)

    candidates = [raw, repair_json_text(raw), _close_truncated_json(raw)]
    match = re.search(r"\{.*", raw, re.DOTALL)
    if match:
        blob = match.group(0)
        candidates.extend(
            [
                blob,
                repair_json_text(blob),
                _close_truncated_json(blob),
            ]
        )

    last_err: json.JSONDecodeError | None = None
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_err = exc
            continue
        if not isinstance(data, dict):
            raise ValueError("llm_json_not_object")
        return data

    salvaged = salvage_json_object(raw)
    if salvaged:
        return salvaged

    raise last_err or json.JSONDecodeError("llm_json_parse_failed", raw, 0)
