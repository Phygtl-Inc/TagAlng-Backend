import re
from typing import Any

VALID_BUCKETS = frozenset(
    {"heritage", "stage", "vicinity", "faith", "activity", "interest", "general"}
)

EVENT_BUCKETS = frozenset(
    {"time", "venue", "audience", "activity", "constraint", "capacity", "purpose"}
)

ALL_BUCKETS = VALID_BUCKETS | EVENT_BUCKETS


def normalize_bucket(raw: Any) -> str | None:
    if raw is None:
        return None
    b = str(raw).strip().lower()
    if b in ALL_BUCKETS:
        return b
    return "general"


def normalize_event_bucket(raw: Any) -> str:
    b = normalize_bucket(raw)
    if b in EVENT_BUCKETS:
        return b
    return "activity"


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


def parse_event_highlights(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw[:8]:
        if isinstance(item, str):
            text = item.strip()[:120]
            if text:
                out.append({"text": text, "bucket": "activity"})
            continue
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()[:120]
        if not text:
            continue
        out.append({"text": text, "bucket": normalize_event_bucket(item.get("bucket"))})
    return out


def parse_event_turn_ui(data: dict[str, Any]) -> dict[str, Any]:
    ui_raw = data.get("ui")
    if not isinstance(ui_raw, dict):
        ui_raw = {}
    focus = str(ui_raw.get("focus_phrase", data.get("focus_phrase", ""))).strip()[:120]
    bucket = normalize_event_bucket(ui_raw.get("bucket", data.get("bucket")))
    highlights = parse_event_highlights(ui_raw.get("highlights"))
    if focus and not highlights:
        highlights = [{"text": focus, "bucket": bucket}]
    return {
        "bucket": bucket,
        "focus_phrase": focus or None,
        "highlights": highlights,
    }


def parse_event_draft(raw: Any, *, valid_purpose_ids: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    title = str(raw.get("title", "")).strip()[:80] or None
    description = str(raw.get("description", "")).strip()[:500] or None
    venue_name = str(raw.get("venue_name", "")).strip()[:120] or None
    starts_at = str(raw.get("starts_at", "")).strip()[:64] or None
    ends_at = str(raw.get("ends_at", "")).strip()[:64] or None
    duration = raw.get("duration_minutes")
    duration_minutes: int | None = None
    if duration is not None:
        try:
            duration_minutes = max(1, min(int(duration), 720))
        except (TypeError, ValueError):
            duration_minutes = None
    max_att = raw.get("max_attendees")
    max_attendees: int | None = None
    if max_att is not None:
        try:
            max_attendees = max(1, min(int(max_att), 200))
        except (TypeError, ValueError):
            max_attendees = None
    tags_raw = raw.get("cohort_tags") or []
    cohort_tags: list[str] = []
    if isinstance(tags_raw, list):
        allowed = valid_purpose_ids or set()
        for t in tags_raw[:6]:
            tid = str(t).strip()
            if not tid:
                continue
            if allowed and tid not in allowed:
                continue
            if tid not in cohort_tags:
                cohort_tags.append(tid)
    missing_raw = raw.get("missing") or []
    missing: list[str] = []
    if isinstance(missing_raw, list):
        for m in missing_raw[:8]:
            s = str(m).strip()
            if s:
                missing.append(s[:64])
    return {
        "title": title,
        "description": description,
        "venue_name": venue_name,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "duration_minutes": duration_minutes,
        "max_attendees": max_attendees,
        "cohort_tags": cohort_tags,
        "missing": missing,
    }


def merge_event_drafts(
    previous: dict[str, Any] | None,
    incoming: dict[str, Any] | None,
) -> dict[str, Any]:
    base = parse_event_draft(previous or {})
    new = parse_event_draft(incoming or {})
    merged = dict(base)
    for key in (
        "title",
        "description",
        "venue_name",
        "starts_at",
        "ends_at",
        "duration_minutes",
        "max_attendees",
    ):
        if new.get(key) not in (None, "", []):
            merged[key] = new[key]
    if new.get("cohort_tags"):
        merged["cohort_tags"] = new["cohort_tags"]
    if new.get("missing") is not None:
        merged["missing"] = new["missing"]
    return merged


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
