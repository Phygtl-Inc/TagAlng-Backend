#!/usr/bin/env python3
"""How sure is Lana, really? — a summary of the `intent_decision` audit rows.

The baseline for removing the 0.85 confidence bump (app/layer1_intents.py). Read-only:
it selects `lana_audit_log` rows written with LANA_INTENT_AUDIT=on and prints

    - how often the classifier ran, FAILED (bad key, outage), was skipped, or was replaced
      by a chip/CTA
    - the spread of the model's REAL confidence (before any bump)
    - how often each keyword rule rewrote the model's pick, and from what to what
    - how many turns sat under the 0.85 pass mark and were bumped over it anyway

USAGE (SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY set, as for the worker)

    python -m scripts.intent_audit_report               # last 7 days
    python -m scripts.intent_audit_report --days 1
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

_BANDS = ((0.0, 0.4), (0.4, 0.6), (0.6, 0.85), (0.85, 1.01))


def _fetch(days: int, limit: int) -> list[dict[str, Any]]:
    from app.auth import service_client

    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    res = (
        service_client()
        .table("lana_audit_log")
        .select("routing, created_at")
        .eq("event_type", "intent_decision")
        .gte("created_at", since)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return [r.get("routing") or {} for r in (res.data or [])]


def _band(conf: float) -> str:
    for lo, hi in _BANDS:
        if lo <= conf < hi:
            return f"{lo:.2f}-{min(hi, 1.0):.2f}"
    return "?"


def summarize(records: list[dict[str, Any]]) -> str:
    n = len(records)
    if not n:
        return "No intent_decision rows in range — is LANA_INTENT_AUDIT=on?"
    ran = forced = skipped = clarified = bumped_over = 0
    failed: Counter[str] = Counter()
    bands: Counter[str] = Counter()
    rules: Counter[str] = Counter()
    rewrites: Counter[str] = Counter()
    for rec in records:
        cls = rec.get("classifier") or {}
        if cls.get("error"):
            failed[cls["error"]] += 1
        elif cls.get("forced_kind"):
            forced += 1
        elif not cls.get("ran"):
            skipped += 1
        else:
            ran += 1
            conf = cls.get("confidence")
            if isinstance(conf, (int, float)):
                bands[_band(float(conf))] += 1
            if cls.get("clarify"):
                clarified += 1
        overrides = rec.get("overrides") or []
        for o in overrides:
            rules[o.get("rule")] += 1
            rewrites[f"{o.get('rule')}: {o.get('from')} -> {o.get('to')}"] += 1
        if any((o.get("from_conf") or 0) < 0.85 <= (o.get("to_conf") or 0) for o in overrides):
            bumped_over += 1

    pct = lambda k: f"{k} ({100 * k / n:.0f}%)"  # noqa: E731
    lines = [
        f"turns: {n}",
        f"  classifier ran:        {pct(ran)}",
        f"  classifier FAILED:     {pct(sum(failed.values()))}"
        + (f"  {dict(failed)}" if failed else ""),
        f"  chip/CTA forced:       {pct(forced)}",
        f"  classifier skipped:    {pct(skipped)}",
        f"  model asked to clarify:{pct(clarified)}",
        f"  bumped over 0.85:      {pct(bumped_over)}",
        "",
        "model confidence (turns where it ran):",
        *(f"  {b}: {bands[b]}" for b in sorted(bands)),
        "",
        "keyword overrides by rule:",
        *(f"  {r}: {c}" for r, c in rules.most_common()),
        "",
        "top rewrites:",
        *(f"  {c:>4}  {w}" for w, c in rewrites.most_common(15)),
    ]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--limit", type=int, default=5000)
    args = ap.parse_args()
    print(summarize(_fetch(args.days, args.limit)))


if __name__ == "__main__":
    main()
