import re
from datetime import datetime
from typing import Any

VALID_BUCKETS = frozenset(
    {"heritage", "stage", "vicinity", "faith", "activity", "interest", "general"}
)

EVENT_BUCKETS = frozenset(
    {"time", "venue", "audience", "activity", "constraint", "capacity", "purpose"}
)

EVENT_DRAFT_REQUIRED = ("title", "starts_at", "venue_name")

ALL_BUCKETS = VALID_BUCKETS | EVENT_BUCKETS

_UI_METADATA_LINE_RE = re.compile(
    r"^(?:heritage|stage|vicinity|faith|activity|interest|general)\s*·\s*.+$",
    re.I,
)


def sanitize_assistant_message(text: str) -> str:
    """Drop orchestrator UI metadata lines leaked into assistant_message."""
    raw = str(text or "").strip()
    if not raw:
        return raw
    lines = [ln for ln in raw.splitlines() if not _UI_METADATA_LINE_RE.match(ln.strip())]
    cleaned = "\n".join(lines).strip()
    return cleaned or raw


def event_draft_blockers(draft: dict[str, Any] | None) -> list[str]:
    data = draft or {}
    return [field for field in EVENT_DRAFT_REQUIRED if not str(data.get(field) or "").strip()]


def finalize_event_draft(draft: dict[str, Any]) -> dict[str, Any]:
    out = dict(draft)
    out["missing"] = event_draft_blockers(out)
    return out


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


def _clean_ui_phrase(raw: Any, *, max_len: int = 120) -> str | None:
    text = str(raw or "").strip()[:max_len]
    if not text or text.lower() in ("none", "null", "n/a"):
        return None
    return text


def parse_highlights(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw[:6]:
        if isinstance(item, str):
            text = _clean_ui_phrase(item)
            if text:
                out.append({"text": text, "bucket": "general"})
            continue
        if not isinstance(item, dict):
            continue
        text = _clean_ui_phrase(item.get("text"))
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
            text = _clean_ui_phrase(item)
            if text:
                out.append({"text": text, "bucket": "activity"})
            continue
        if not isinstance(item, dict):
            continue
        text = _clean_ui_phrase(item.get("text"))
        if not text:
            continue
        out.append({"text": text, "bucket": normalize_event_bucket(item.get("bucket"))})
    return out


def parse_event_turn_ui(data: dict[str, Any]) -> dict[str, Any]:
    ui_raw = data.get("ui")
    if not isinstance(ui_raw, dict):
        ui_raw = {}
    focus = _clean_ui_phrase(ui_raw.get("focus_phrase", data.get("focus_phrase")))
    bucket = normalize_event_bucket(ui_raw.get("bucket", data.get("bucket")))
    highlights = parse_event_highlights(ui_raw.get("highlights"))
    if focus and not highlights:
        highlights = [{"text": focus, "bucket": bucket}]
    return {
        "bucket": bucket,
        "focus_phrase": focus,
        "highlights": highlights,
    }


# A bare ZIP (5 digits, optional +4) is an AREA, not a meetable venue. The extractor
# must never let "I'll use 34786 as the meeting spot" pin a ZIP code to the map.
_ZIP_TOKEN_RE = re.compile(r"^\d{5}(?:-\d{4})?$")


def _iso_ts_or_none(val: str | None) -> str | None:
    """starts_at / ends_at must be strict ISO-8601 or None. The extractor sometimes
    emits prose ("next Wednesday 16:00:00") which would poison the draft card, the
    step-gate's date recovery, and publish — drop anything fromisoformat can't parse."""
    if not val:
        return None
    text = val[:-1] + "+00:00" if val.endswith("Z") else val
    try:
        datetime.fromisoformat(text)
    except ValueError:
        return None
    return val


def parse_event_draft(raw: Any, *, valid_purpose_ids: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}

    def field(key: str, max_len: int) -> str | None:
        val = str(raw.get(key, "")).strip()[:max_len]
        if not val or val.lower() in ("none", "null", "n/a"):
            return None
        return val

    title = field("title", 80)
    description = field("description", 500)
    venue_name = field("venue_name", 120)
    if venue_name and _ZIP_TOKEN_RE.match(venue_name):
        venue_name = None  # a ZIP is never a venue — keep asking for a real place
    starts_at = _iso_ts_or_none(field("starts_at", 64))
    ends_at = _iso_ts_or_none(field("ends_at", 64))
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
    bring_raw = raw.get("bring_items") or []
    bring_items: list[str] = []
    if isinstance(bring_raw, list):
        for b in bring_raw[:12]:
            label = str(b).strip()[:60]
            if label and label not in bring_items:
                bring_items.append(label)
    # AI-tailored quick-setup card config (opaque dict) — passed through untouched.
    event_setup_raw = raw.get("event_setup")
    event_setup = event_setup_raw if isinstance(event_setup_raw, dict) else None
    missing_raw = raw.get("missing") or []
    missing: list[str] = []
    if isinstance(missing_raw, list):
        for m in missing_raw[:8]:
            s = str(m).strip()
            if s:
                missing.append(s[:64])
    # Per-event affinity prompt + quick-reply chips (display only; the user's choice
    # is folded into cohort_tags by the model, which already drives matching).
    affinity_prompt = field("affinity_prompt", 160)
    options_raw = raw.get("affinity_options") or []
    affinity_options: list[str] = []
    if isinstance(options_raw, list):
        for opt in options_raw[:3]:
            label = str(opt).strip()[:60]
            if label and label not in affinity_options:
                affinity_options.append(label)
    # Generic tappable quick-replies for whatever Lana is asking this turn
    # (place / time / title…), including a "decide later" where optional.
    suggestions_raw = raw.get("suggestions") or []
    suggestions: list[str] = []
    if isinstance(suggestions_raw, list):
        for opt in suggestions_raw[:4]:
            label = str(opt).strip()[:60]
            if label and label not in suggestions:
                suggestions.append(label)
    return {
        "title": title,
        "description": description,
        "venue_name": venue_name,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "duration_minutes": duration_minutes,
        "max_attendees": max_attendees,
        "cohort_tags": cohort_tags,
        "bring_items": bring_items,
        "event_setup": event_setup,
        "affinity_prompt": affinity_prompt,
        "affinity_options": affinity_options,
        "missing": missing,
    }


_CLEARABLE_EVENT_FIELDS = frozenset(
    {
        "title",
        "description",
        "venue_name",
        "starts_at",
        "ends_at",
        "duration_minutes",
        "max_attendees",
        "cohort_tags",
    }
)


def merge_event_drafts(
    previous: dict[str, Any] | None,
    incoming: dict[str, Any] | None,
    *,
    clear_fields: list[str] | None = None,
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
    if new.get("bring_items"):
        merged["bring_items"] = new["bring_items"]
    # event_setup is set once (when entering the setup stage) and then persists untouched.
    if new.get("event_setup"):
        merged["event_setup"] = new["event_setup"]
    elif base.get("event_setup"):
        merged["event_setup"] = base["event_setup"]
    # Honor explicit resets — the host wants to redo a slot they'd already filled
    # ("don't call it X", "change the time"). Slot-filling is otherwise monotonic, so
    # without this a rejected value sticks forever and the flow re-asks the NEXT slot
    # in a loop. Only clear when this turn didn't also supply a fresh value for the slot
    # (e.g. "rename it to Game Night" sets the new title instead of blanking it).
    for key in clear_fields or []:
        if key not in _CLEARABLE_EVENT_FIELDS:
            continue
        if new.get(key) in (None, "", []):
            merged[key] = [] if key == "cohort_tags" else None
    # Affinity prompt/options are transient per turn — take the latest (clears once
    # the host answers and the model stops re-asking).
    merged["affinity_prompt"] = new.get("affinity_prompt")
    merged["affinity_options"] = new.get("affinity_options") or []
    merged["suggestions"] = new.get("suggestions") or []
    if new.get("missing") is not None:
        merged["missing"] = new["missing"]
    return merged


def parse_turn_ui(data: dict[str, Any]) -> dict[str, Any]:
    ui_raw = data.get("ui")
    if not isinstance(ui_raw, dict):
        ui_raw = {}
    focus = _clean_ui_phrase(ui_raw.get("focus_phrase", data.get("focus_phrase")))
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
