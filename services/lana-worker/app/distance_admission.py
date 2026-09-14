"""C3 — distance admission.

A meet is admitted when its topic similarity clears a floor that rises with distance:

    similarity >= floor_base + k * ln(distance_m / radius_base_m)

So a far meet has to be a better topic match than a near one.
"""

from __future__ import annotations

import math
from typing import Any

# ln(0) is undefined, so anything at or below a metre reads as a metre.
_MIN_DISTANCE_M = 1.0


def _admission_floor(
    distance_m: float,
    *,
    floor_base: float = 0.55,
    k: float = 0.08,
    radius_base_m: float = 8000.0,
    clamp_ratio: float = 0.9,
) -> float:
    """Minimum topic similarity a meet at `distance_m` must have to be admitted."""
    try:
        d = float(distance_m)
    except (TypeError, ValueError):
        d = _MIN_DISTANCE_M
    if d != d or d < _MIN_DISTANCE_M:  # NaN, zero, negative
        d = _MIN_DISTANCE_M
    return max(floor_base * clamp_ratio, floor_base + k * math.log(d / radius_base_m))


def _similarity(row: dict[str, Any]) -> float | None:
    """The row's topic similarity, or None when the meet has no vector yet.
    Missing, null and unreadable all mean the topic is unknown, not bad."""
    value = row.get("similarity")
    if value is None:
        return None
    try:
        sim = float(value)
    except (TypeError, ValueError):
        return None
    return None if sim != sim else sim


def _distance(row: dict[str, Any]) -> float:
    """Metres from the seeker, or zero when the row carries no distance."""
    try:
        return float(row.get("distance_meters") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _admit(
    rows: list[dict[str, Any]],
    *,
    floor_base: float = 0.55,
    k: float = 0.08,
    radius_base_m: float = 8000.0,
    clamp_ratio: float = 0.9,
) -> list[dict[str, Any]]:
    """The rows that clear the floor for their distance, in input order, embedded rows
    first. Rows with no similarity are admitted and placed last: a meet posted a minute
    ago has no vector yet, and that is not a reason to hide it. Rows are returned untouched."""
    embedded: list[dict[str, Any]] = []
    unembedded: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        sim = _similarity(row)
        if sim is None:
            unembedded.append(row)
            continue
        floor = _admission_floor(
            _distance(row),
            floor_base=floor_base,
            k=k,
            radius_base_m=radius_base_m,
            clamp_ratio=clamp_ratio,
        )
        if sim >= floor:
            embedded.append(row)
    return embedded + unembedded
