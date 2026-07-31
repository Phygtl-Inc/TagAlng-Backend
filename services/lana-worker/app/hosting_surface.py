"""C-4-EVENT-P3: structured hosting card for unified chat when host_meet is saved."""

from __future__ import annotations

import re
from typing import Any

from app.signal_capture import _WHEN_HINT

_VENUE_AT_RE = re.compile(
    r"\bat\s+([A-Za-z0-9][\w\s'.&·-]{1,48}?)(?:\s*[—,\.]|$)",
    re.I,
)
_WHO_FOR_RE = re.compile(
    r"\b(?:for|with|open to)\s+([\w\s'-]{3,40}?(?:moms?|parents?|families|neighbors?|adults?))\b",
    re.I,
)
_MOM_AUDIENCE_RE = re.compile(r"\b(?:moms?|parents?|families)\b", re.I)


def _extract_when(text: str) -> str | None:
    m = _WHEN_HINT.search(str(text or ""))
    return m.group(0).strip() if m else None


def _extract_venue(text: str) -> str | None:
    m = _VENUE_AT_RE.search(str(text or ""))
    if not m:
        return None
    venue = str(m.group(1) or "").strip(" .,-")
    return venue or None


def _title_from_detail(detail: str, *, when: str | None, venue: str | None) -> str:
    text = str(detail or "").strip()
    if not text:
        return "Your meetup"
    if " — " in text:
        head = text.split(" — ", 1)[0].strip()
        if len(head) >= 4:
            text = head
    scrub = text
    if when:
        scrub = re.sub(re.escape(when), "", scrub, flags=re.I)
    if venue:
        scrub = re.sub(r"\bat\s+" + re.escape(venue), "", scrub, flags=re.I)
    scrub = re.sub(r"\s+", " ", scrub).strip(" —-,.")
    if len(scrub) >= 3:
        return scrub[:120]
    return text[:120]


def _who_label_from_detail(detail: str) -> str | None:
    m = _WHO_FOR_RE.search(str(detail or ""))
    if m:
        return str(m.group(1) or "").strip()[:80] or None
    if _MOM_AUDIENCE_RE.search(str(detail or "")):
        return "Parents near you"
    return None


def _who_label_from_ctx(ctx: dict[str, Any]) -> str:
    explicit = _who_label_from_detail(str(ctx.get("signal_detail") or ""))
    if explicit:
        return explicit
    profile = ctx.get("identity_profile")
    if isinstance(profile, dict):
        claims = profile.get("claims")
        if isinstance(claims, list):
            bits: list[str] = []
            for row in claims[:4]:
                if not isinstance(row, dict):
                    continue
                label = str(row.get("label") or "").strip()
                if label and label.lower() not in bits:
                    bits.append(label)
            if bits:
                return " · ".join(bits[:3])[:80]
    return "Neighbors near you"


def trait_tags_from_hosting(
    *,
    title: str,
    when_label: str | None,
    where_label: str | None,
    who_label: str | None,
) -> list[str]:
    out: list[str] = []
    for part in (title, when_label, where_label, who_label):
        bit = str(part or "").strip()
        if not bit or len(bit) < 2:
            continue
        if bit.lower() in {"your meetup", "neighbors on your block", "neighbors near you"}:
            continue
        if bit not in out:
            out.append(bit)
        if len(out) >= 5:
            break
    return out


def build_hosting_draft(
    *,
    detail_text: str,
    when_hint: str | None = None,
    ctx: dict[str, Any] | None = None,
    matches_created: int = 0,
    block_name: str | None = None,
) -> dict[str, Any]:
    detail = str(detail_text or "").strip()
    when_label = str(when_hint or "").strip() or _extract_when(detail)
    where_label = _extract_venue(detail)
    if where_label and block_name and block_name.lower() not in where_label.lower():
        where_label = f"{where_label} · {block_name}"
    title = _title_from_detail(detail, when=when_label, venue=_extract_venue(detail))
    who_label = _who_label_from_ctx({**(ctx or {}), "signal_detail": detail})
    trait_tags = trait_tags_from_hosting(
        title=title,
        when_label=when_label,
        where_label=where_label,
        who_label=who_label,
    )
    nearby = max(0, int(matches_created or 0))
    if nearby > 0:
        outreach = f"I'll text the {nearby} closest fit{'s' if nearby != 1 else ''} near you."
    else:
        outreach = "I'll let neighbors nearby know when there's a fit."
    return {
        "title": title,
        "headline": f"Heard you — {title.rstrip('.')}.",
        "when_label": when_label,
        "where_label": where_label,
        "who_label": who_label,
        "trait_tags": trait_tags,
        "status_label": "Ready to open it up",
        "outreach_copy": outreach,
    }


def attach_hosting_to_signal_saved(
    saved: dict[str, Any],
    ctx: dict[str, Any],
    *,
    when_hint: str | None = None,
    block_name: str | None = None,
) -> None:
    if str(saved.get("intent") or "") != "host_meet":
        return
    saved["hosting"] = build_hosting_draft(
        detail_text=str(saved.get("detail_text") or ""),
        when_hint=when_hint,
        ctx=ctx,
        matches_created=int(saved.get("matches_created") or 0),
        block_name=block_name,
    )
