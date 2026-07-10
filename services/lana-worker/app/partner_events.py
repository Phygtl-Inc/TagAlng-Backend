"""Partner-sourced events — import normalization + honest attribution.

Supply engine №1 for marketplace cold-start: recurring anchor events from local
institutions (library storytime, YMCA swim) are imported into the same `events` table
members use, marked `source='partner'` with `source_name` (e.g. "Lake Nona Library"),
and every surface that previews events appends a "via {source_name}" attribution so a
neighbor never mistakes an import for a member-hosted meet.

The import entrypoint is scripts/import_partner_events.py; everything data-shaped here
is pure so idempotency is testable without a database:

  normalize_partner_items(raw)          file rows → canonical event rows (deduped)
  partner_event_key(row)                identity = (source_name, title, starts_at)
  merge_partner_events(existing, new)   → (to_insert, to_update)  · idempotent
"""

from __future__ import annotations

from typing import Any

PARTNER_SOURCE = "partner"
MEMBER_SOURCE = "member"


# ── Attribution (read side) ──────────────────────────────────────────────────
def attribution_label(event: dict[str, Any] | None) -> str | None:
    """'via Lake Nona Library' for a partner event; None for member events (or a partner
    row missing its source_name — nothing honest to say, so say nothing)."""
    if not isinstance(event, dict):
        return None
    if str(event.get("source") or "").strip().lower() != PARTNER_SOURCE:
        return None
    name = str(event.get("source_name") or "").strip()
    return f"via {name}" if name else None


def with_attribution(text: str, event: dict[str, Any] | None) -> str:
    """Append ' · via {source_name}' to a preview line when the event is partner-sourced."""
    label = attribution_label(event)
    return f"{text} · {label}" if label else text


# ── Import normalization (pure) ──────────────────────────────────────────────
def _clean(raw: Any, limit: int) -> str | None:
    text = str(raw or "").strip()
    return text[:limit] or None


def _parse_starts(raw: Any) -> str | None:
    """Normalize a file timestamp to the stored UTC ISO instant. Naive wall-clock times
    mean the EVENT's local timezone (single-region today) — same rule as member events."""
    from app.event_publish import _parse_iso_ts

    return _parse_iso_ts(str(raw or "") or None)


def _split_tags(raw: Any) -> list[str]:
    if isinstance(raw, list):
        parts = [str(t) for t in raw]
    else:
        parts = str(raw or "").replace(";", "|").split("|")
    out: list[str] = []
    for p in parts:
        tag = p.strip()
        if tag and tag not in out:
            out.append(tag)
    return out[:6]


def partner_event_key(row: dict[str, Any]) -> tuple[str, str, str]:
    """Identity of a partner event: (source_name, title, starts_at), case/space-folded.
    A re-import of the same institution's schedule maps onto the same keys."""
    return (
        str(row.get("source_name") or "").strip().lower(),
        str(row.get("title") or "").strip().lower(),
        str(row.get("starts_at") or "").strip(),
    )


def normalize_partner_items(
    items: list[dict[str, Any]],
    *,
    default_block_id: str | None = None,
    default_cluster_id: str = "lake-nona",
) -> tuple[list[dict[str, Any]], list[str]]:
    """File rows → canonical `events` rows (source='partner'), deduped by key within the
    file. Returns (rows, problems); a row missing title/starts_at/source_name is reported,
    not silently dropped. host_id is stamped by the caller (schema requires a host user)."""
    rows: list[dict[str, Any]] = []
    problems: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    for i, item in enumerate(items or []):
        if not isinstance(item, dict):
            problems.append(f"row {i}: not an object")
            continue
        title = _clean(item.get("title"), 80)
        source_name = _clean(item.get("source_name"), 120)
        starts_at = _parse_starts(item.get("starts_at"))
        if not title or not source_name or not starts_at:
            problems.append(f"row {i}: needs title, source_name and a valid starts_at")
            continue
        row: dict[str, Any] = {
            "source": PARTNER_SOURCE,
            "source_name": source_name,
            "title": title,
            "starts_at": starts_at,
            "ends_at": _parse_starts(item.get("ends_at")),
            "description": _clean(item.get("description"), 500),
            "venue_name": _clean(item.get("venue_name"), 120) or source_name,
            "cohort_tags": _split_tags(item.get("cohort_tags")),
            "block_id": _clean(item.get("block_id"), 40) or default_block_id,
            "cluster_id": _clean(item.get("cluster_id"), 60) or default_cluster_id,
        }
        key = partner_event_key(row)
        if key in seen:
            continue  # duplicate line in the file — first one wins
        seen.add(key)
        rows.append(row)
    return rows, problems


# Fields a re-import may refresh on an existing occurrence (identity fields excluded).
_UPDATABLE_FIELDS = ("description", "venue_name", "ends_at", "cohort_tags")


def merge_partner_events(
    existing: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Pure idempotency core: (to_insert, to_update) for an import batch.

    An incoming row whose (source_name, title, starts_at) key isn't in `existing` is an
    insert. A known key is an update ONLY when an updatable field actually changed (the
    update dict carries the existing row's id + changed fields). Re-running the same file
    against its own output yields ([], [])."""
    by_key = {partner_event_key(r): r for r in existing if isinstance(r, dict)}
    to_insert: list[dict[str, Any]] = []
    to_update: list[dict[str, Any]] = []
    for row in incoming:
        current = by_key.get(partner_event_key(row))
        if current is None:
            to_insert.append(row)
            continue
        patch: dict[str, Any] = {}
        for field in _UPDATABLE_FIELDS:
            new_val = row.get(field)
            old_val = current.get(field)
            if field == "cohort_tags":
                new_val = list(new_val or [])
                old_val = list(old_val or [])
            if new_val is not None and new_val != old_val:
                patch[field] = row.get(field)
        if patch and current.get("id"):
            patch["id"] = current["id"]
            to_update.append(patch)
    return to_insert, to_update


# ── DB side (service role) ───────────────────────────────────────────────────
def fetch_existing_partner_events(source_names: list[str]) -> list[dict[str, Any]]:
    """All partner events for the named institutions — the idempotency baseline."""
    names = sorted({str(n or "").strip() for n in source_names if str(n or "").strip()})
    if not names:
        return []
    from app.auth import service_client

    res = (
        service_client()
        .table("events")
        .select("id, source, source_name, title, starts_at, ends_at, description, venue_name, cohort_tags")
        .eq("source", PARTNER_SOURCE)
        .in_("source_name", names)
        .execute()
    )
    return [r for r in (res.data or []) if isinstance(r, dict)]


def upsert_partner_events(
    incoming: list[dict[str, Any]],
    *,
    host_id: str,
    dry_run: bool = False,
) -> dict[str, int]:
    """Idempotent import: insert new occurrences, patch changed ones, leave the rest.

    `host_id` is the house account that owns imports (events.host_id is not null) — it
    is stamped on inserts only, never rewritten on update. NOTE: this branch's events
    table has no is_test column, so none is set; if one lands later the importer should
    pass it through here."""
    if not host_id:
        raise ValueError("host_id required (events.host_id is not null)")
    existing = fetch_existing_partner_events([r["source_name"] for r in incoming])
    to_insert, to_update = merge_partner_events(existing, incoming)
    if dry_run:
        return {"inserted": len(to_insert), "updated": len(to_update),
                "unchanged": len(incoming) - len(to_insert) - len(to_update)}
    from app.auth import service_client

    sb = service_client()
    if to_insert:
        payload = [{**row, "host_id": host_id} for row in to_insert]
        sb.table("events").insert(payload).execute()
    for patch in to_update:
        row_id = patch.pop("id")
        sb.table("events").update(patch).eq("id", row_id).execute()
    return {"inserted": len(to_insert), "updated": len(to_update),
            "unchanged": len(incoming) - len(to_insert) - len(to_update)}
