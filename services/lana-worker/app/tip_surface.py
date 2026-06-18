"""C-4-RECO-P3: structured tip card when tip_share is saved."""

from __future__ import annotations

import re
from typing import Any

_DR_NAME_RE = re.compile(
    r"\b(?:dr\.?\s+)?([\w'.-]+)\s+is\s+(?:a\s+)?"
    r"(?:great|good|wonderful|amazing|excellent)\s+(\w+)",
    re.I,
)
_TRAIT_SPLIT_RE = re.compile(r"\s*[·•,]\s*|\s+and\s+", re.I)
_CATEGORY_LABELS = {
    "health": "healthcare",
    "food": "food & drink",
    "home": "home services",
    "education": "education",
    "activities": "activities",
}


def _title_from_detail(detail: str, *, category: str | None = None) -> str:
    text = str(detail or "").strip()
    if not text:
        return "Your tip"
    m = _DR_NAME_RE.search(text)
    if m:
        name = str(m.group(1) or "").strip().title()
        role = str(m.group(2) or "").strip().lower()
        if not name.lower().startswith("dr"):
            name = f"Dr. {name}"
        return f"{name} · {role}"
    if " — " in text:
        head = text.split(" — ", 1)[0].strip()
        if len(head) >= 4:
            return head[:120]
    if " · " in text:
        return text.split(" · ", 1)[0].strip()[:120]
    cat = _CATEGORY_LABELS.get(str(category or "").strip().lower())
    if cat and cat not in text.lower():
        return f"{text[:80]} · {cat}"
    return text[:120]


def trait_tags_from_tip(*, title: str, detail: str, where_label: str | None) -> list[str]:
    out: list[str] = []
    text = str(detail or "").strip()
    title_bit = str(title or "").strip()
    for part in _TRAIT_SPLIT_RE.split(text):
        bit = str(part or "").strip(" .")
        if not bit or len(bit) < 3:
            continue
        if bit.lower() == title_bit.lower():
            continue
        if bit.lower() in {title_bit.split(" · ", 1)[0].lower(), "great doctor", "good doctor"}:
            continue
        if bit not in out:
            out.append(bit[:48])
        if len(out) >= 4:
            break
    where = str(where_label or "").strip()
    if where and where not in out and len(out) < 5:
        out.append(where[:48])
    return out[:5]


def build_tip_draft(
    *,
    detail_text: str,
    category: str | None = None,
    where_hint: str | None = None,
    matches_created: int = 0,
) -> dict[str, Any]:
    detail = str(detail_text or "").strip()
    where_label = str(where_hint or "").strip() or None
    if not where_label and " — " in detail:
        tail = detail.rsplit(" — ", 1)[-1].strip()
        if tail and len(tail.split()) <= 6:
            where_label = tail
    title = _title_from_detail(detail, category=category)
    trait_tags = trait_tags_from_tip(title=title, detail=detail, where_label=where_label)
    nearby = max(0, int(matches_created or 0))
    if nearby > 0:
        outreach = (
            f"I'll listen for {nearby} neighbor{'s' if nearby != 1 else ''} "
            "on your block who might want this."
        )
    else:
        outreach = "I'll listen for moms on your block who need this."
    return {
        "title": title,
        "headline": f"Heard you — {title.rstrip('.')}.",
        "where_label": where_label,
        "trait_tags": trait_tags,
        "status_label": "Ready to pass it along",
        "outreach_copy": outreach,
    }


def attach_tip_to_signal_saved(
    saved: dict[str, Any],
    *,
    where_hint: str | None = None,
) -> None:
    if str(saved.get("intent") or "") != "tip_share":
        return
    saved["tip"] = build_tip_draft(
        detail_text=str(saved.get("detail_text") or ""),
        category=str(saved.get("category") or "") or None,
        where_hint=where_hint,
        matches_created=int(saved.get("matches_created") or 0),
    )
