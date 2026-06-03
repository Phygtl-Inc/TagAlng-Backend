import re
from typing import Any

VALID_BUCKETS = frozenset(
    {"heritage", "stage", "vicinity", "faith", "activity", "interest", "general"}
)


def normalize_bucket(raw: Any) -> str | None:
    if raw is None:
        return None
    b = str(raw).strip().lower()
    if b in VALID_BUCKETS:
        return b
    return "general"


def parse_highlights(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw[:6]:
        if isinstance(item, str):
            text = item.strip()[:120]
            if text:
                out.append({"text": text, "bucket": "general"})
            continue
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()[:120]
        if not text:
            continue
        out.append({"text": text, "bucket": normalize_bucket(item.get("bucket")) or "general"})
    return out


def parse_turn_ui(data: dict[str, Any]) -> dict[str, Any]:
    ui_raw = data.get("ui")
    if not isinstance(ui_raw, dict):
        ui_raw = {}
    focus = str(ui_raw.get("focus_phrase", data.get("focus_phrase", ""))).strip()[:120]
    bucket = normalize_bucket(ui_raw.get("bucket", data.get("bucket")))
    highlights = parse_highlights(ui_raw.get("highlights"))
    if focus and not highlights:
        highlights = [{"text": focus, "bucket": bucket or "general"}]
    if focus and bucket is None:
        bucket = highlights[0]["bucket"] if highlights else "general"
    return {
        "bucket": bucket,
        "focus_phrase": focus or None,
        "highlights": highlights,
    }


def parse_mapped_spans(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw[:12]:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()[:160]
        if not text:
            continue
        concept = str(item.get("claim_concept", item.get("concept", ""))).strip().lower()
        if concept and not re.match(r"^[a-z][a-z0-9_]{1,63}$", concept):
            concept = ""
        out.append(
            {
                "text": text,
                "bucket": normalize_bucket(item.get("bucket")) or "general",
                "claim_concept": concept or None,
            }
        )
    return out
