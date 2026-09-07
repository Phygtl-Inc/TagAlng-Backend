#!/usr/bin/env python3
"""THROWAWAY. Not part of the app, not in git, not imported by anything.

Checks that the real model produces sensible topic_score / topic_mismatch values from
_filter_events_by_query. Every existing test feeds that function a canned model response,
so nothing has verified the actual numbers.

Run from services/lana-worker with the worker's env loaded:
    .venv/bin/python scratch_score_eval.py

Budget: EXACTLY 8 model calls (4 cases x 2 runs). Enforced by a hard counter that raises
rather than exceeding it. No retries, no loops, no backoff, no recursion — a failed call
prints its error and the script moves on.
"""

from __future__ import annotations

import os
import sys
import traceback

# Cross-provider failover would turn one failed call into a second call against the other
# vendor, breaking the 8-call accounting. This is the app's own documented kill switch
# (app/orchestrator/llm.py fallback_enabled), set before anything imports the module.
os.environ["LANA_LLM_FALLBACK"] = "0"

MAX_CALLS = 8
_calls = 0


def _install_call_counter() -> None:
    """Wrap llm_json so the budget is enforced, not merely intended.

    _filter_events_by_query does `from app.orchestrator.llm import llm_json` INSIDE the
    function body, so patching the attribute on the source module is what takes effect.
    """
    from app.orchestrator import llm as llm_mod

    real = llm_mod.llm_json

    def counted(**kwargs):
        global _calls
        if _calls >= MAX_CALLS:
            raise RuntimeError(f"call budget exhausted ({MAX_CALLS})")
        _calls += 1
        print(f"    [model call {_calls}/{MAX_CALLS}]", file=sys.stderr)
        return real(**kwargs)

    llm_mod.llm_json = counted


# ── The four cases ───────────────────────────────────────────────────────────────────
# starts_at is a plain future date: none of these requests mention a date, so the date
# constraint never engages — it just has to render.
_WHEN = "2026-09-19T18:00:00"


def _ev(title: str, description: str | None = None, tags: list[str] | None = None) -> dict:
    return {
        "id": title[:12],
        "title": title,
        "description": description,
        "starts_at": _WHEN,
        "has_time": True,
        "cohort_tags": tags or [],
        "host_name": "Sam",
    }


def cases() -> list[tuple[str, str, list[dict]]]:
    """Fresh dicts every call — _filter_events_by_query stamps rows IN PLACE, so reusing
    them across runs would carry run 1's scores into run 2."""
    return [
        (
            "A",
            "violin",
            [
                _ev("Violin recital at the community hall"),
                _ev("Guitar jam night"),
                _ev("Beginner pottery workshop"),
                _ev("Startup pitch night"),
                _ev(
                    "Sunday jam night",
                    "bring your violin, guitar, or anything with strings",
                ),
            ],
        ),
        ("B", "soccer", [_ev("FIFA watch party at the pub")]),
        ("C", "basketball", [_ev("Beach volleyball meetup")]),
        (
            "D",
            "running",
            [_ev("Running a small business: a workshop for new founders")],
        ),
    ]


def run_pass(label: str) -> dict[tuple[str, str], tuple[float, bool]]:
    """One pass over all four cases.

    Reads scores off the INPUT list, not off `matched`: _filter_events_by_query stamps
    every candidate in place and returns only the ones the model matched, so printing
    from `matched` alone hides exactly what this work added — the near-miss scores.
    Membership is its own column, resolved by object identity (rows are the same dicts).

    Returns {(request, title): (score, returned)} so the comparison below catches a
    membership flip as well as a score move.
    """
    from app.activity_browse import _filter_events_by_query

    print(f"\n{'=' * 100}\n{label}\n{'=' * 100}")
    seen: dict[tuple[str, str], tuple[float, bool]] = {}

    for case_id, request, events in cases():
        print(f"\n--- Case {case_id}: request {request!r} ({len(events)} events) ---")
        try:
            matched, filter_label = _filter_events_by_query(events, request)
        except Exception as exc:  # noqa: BLE001 - print and move on, never retry
            print(f"    FAILED: {type(exc).__name__}: {exc}")
            traceback.print_exc(limit=3)
            continue

        returned_ids = {id(e) for e in matched}
        print(f"    filter label: {filter_label!r}")
        print(f"    {'request':12} | {'title':52} | {'score':>5} | {'member':12} | mismatch")
        for ev in events:
            title = str(ev.get("title") or "")
            # .get, not [...]: a missing key is itself the finding — every path through
            # the matcher is supposed to stamp every candidate.
            score = ev.get("topic_score", "MISSING")
            mismatch = ev.get("topic_mismatch", "MISSING")
            returned = id(ev) in returned_ids
            member = "RETURNED" if returned else "not returned"
            seen[(request, title)] = (score, returned)
            print(
                f"    {request:12} | {title:52} | {score:>5} | {member:12} | {mismatch!r}"
            )
        print(f"    ({len(events)} scored, {len(matched)} returned)")

    return seen


def main() -> int:
    from app.orchestrator.llm import llm_configured, provider, router_model

    if not llm_configured():
        # Without credentials the filter never reaches the model: it falls to the keyword
        # branch and stamps every row 0.0, which would look like a real result and prove
        # nothing. Abort at ZERO calls so the run budget is not spent on a no-op.
        print(
            f"ABORT — no LLM configured (provider={provider()}). Zero model calls made.\n"
            "Nothing was evaluated: _filter_events_by_query would have taken the keyword\n"
            "fallback and reported 0.0 for every event.\n\n"
            "Export the worker's credentials and re-run:\n"
            "  GCP_VERTEX_PROJECT=<project> (+ GCP_VERTEX_LOCATION, default us-central1)\n"
            "  or OPENAI_API_KEY=<key>",
            file=sys.stderr,
        )
        return 2

    print(f"provider={provider()}  router_model={router_model()}  budget={MAX_CALLS} calls")
    _install_call_counter()

    first = run_pass("RUN 1")
    second = run_pass("RUN 2")

    print(f"\n{'=' * 100}\nSTABILITY: run 1 vs run 2\n{'=' * 100}")
    keys = sorted(set(first) | set(second))
    moved_score = 0
    moved_member = 0
    for key in keys:
        request, title = key
        a = first.get(key, ("absent", "absent"))
        b = second.get(key, ("absent", "absent"))
        if a == b:
            continue
        (score_a, mem_a), (score_b, mem_b) = a, b
        notes = []
        if score_a != score_b:
            moved_score += 1
            delta = (
                f" ({score_b - score_a:+.1f})"
                if isinstance(score_a, float) and isinstance(score_b, float)
                else ""
            )
            notes.append(f"score {score_a} -> {score_b}{delta}")
        if mem_a != mem_b:
            # The one that actually changes what a resident sees.
            moved_member += 1
            notes.append(
                f"MEMBERSHIP {'returned' if mem_a else 'not returned'} -> "
                f"{'returned' if mem_b else 'not returned'}"
            )
        print(f"    {request:12} | {title:52} | {'; '.join(notes)}")

    if not moved_score and not moved_member:
        print("    nothing moved between runs")
    else:
        print(
            f"\n    {moved_score} of {len(keys)} scores moved; "
            f"{moved_member} membership changes"
        )

    print(f"\ntotal model calls: {_calls}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
