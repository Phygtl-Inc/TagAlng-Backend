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

import json
import os
import sys
import traceback

# Cross-provider failover would turn one failed call into a second call against the other
# vendor, breaking the 8-call accounting. This is the app's own documented kill switch
# (app/orchestrator/llm.py fallback_enabled), set before anything imports the module.
os.environ["LANA_LLM_FALLBACK"] = "0"

MAX_CALLS = 8
_calls = 0
# Raw model responses, newest last. Without these a failed call is INDISTINGUISHABLE
# from a bad judgement: both come out of _filter_events_by_query as all-zero scores and
# an empty label, because an unusable response falls silently into the keyword fallback.
# Telling those two apart cost a whole eval cycle on 2026-09-06.
_RAW: list = []


def _install_call_counter() -> None:
    """Wrap llm_json so the budget is enforced, not merely intended, and so the raw
    response is kept for printing.

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
        try:
            out = real(**kwargs)
        except Exception as exc:  # noqa: BLE001 - record, then let it propagate
            _RAW.append(f"RAISED {type(exc).__name__}: {exc}")
            raise
        _RAW.append(out)
        return out

    llm_mod.llm_json = counted


# ── The four cases ───────────────────────────────────────────────────────────────────
# starts_at is a plain future date: none of these requests mention a date, so the date
# constraint never engages — it just has to render.
_WHEN = "2026-09-19T18:00:00"


def _ev(
    title: str,
    description: str | None = None,
    *,
    expect_score: float | None = None,
    expect_match: bool = False,
) -> dict:
    """One candidate. The `_expect_*` keys are script-local — _filter_events_by_query
    builds its prompt from title/date/host/tags/description and ignores anything else."""
    return {
        "id": title[:12],
        "title": title,
        "description": description,
        "starts_at": _WHEN,
        "has_time": True,
        "cohort_tags": [],
        "host_name": "Sam",
        "_expect_score": expect_score,
        "_expect_match": expect_match,
    }


def cases() -> list[tuple[str, str, list[dict]]]:
    """Fresh dicts every call — _filter_events_by_query stamps rows IN PLACE, so reusing
    them across runs would carry run 1's scores into run 2.

    A, B and D are frozen ON PURPOSE: they are the membership regression check, and
    adding events to a case changes the context the model judges the existing ones in.
    Acceptance is that the violin recital, Sunday jam night and the FIFA watch party all
    come back RETURNED, as they were before scoring existed.

    C is the calibration probe, and its events are deliberately NOT the ladder's own
    rungs — see the note inside it.
    """
    return [
        (
            "A",
            "violin",
            [
                _ev("Violin recital at the community hall", expect_score=1.0, expect_match=True),
                # Live music, but participatory where a recital is listening.
                _ev("Guitar jam night", expect_score=0.4),
                # Both hands-on making: must clear 0.0, and must beat the pitch night.
                _ev("Beginner pottery workshop", expect_score=0.1),
                # The floor. Nothing in common.
                _ev("Startup pitch night", expect_score=0.0),
                _ev(
                    "Sunday jam night",
                    "bring your violin, guitar, or anything with strings",
                    expect_score=0.8,
                    expect_match=True,
                ),
            ],
        ),
        (
            "B",
            "soccer",
            [
                # Watching is a poor substitute for playing, so the score should fall hard
                # from the 1.0 it got before. Membership must NOT move — if this stops
                # matching, the reframe leaked into the decision and the change fails.
                _ev("FIFA watch party at the pub", expect_score=0.5, expect_match=True),
            ],
        ),
        (
            "C",
            "basketball",
            [
                # Deliberately NOT the ladder's own rungs. The previous version of this
                # case used indoor soccer / volleyball / ultimate / water polo / solo
                # weightlifting / book club — the exact items written in the prompt — so
                # reproducing them showed only that the model can copy a list. These are
                # different events in the same bands, which is a generalisation test.
                _ev("Sunday hoops run at the park", expect_score=0.9, expect_match=True),
                _ev("Co-ed dodgeball league night", expect_score=0.6),
                _ev("Badminton club night", expect_score=0.4),
                _ev("Rock climbing session", expect_score=0.2),
                _ev("Morning lap swim", expect_score=0.1),
                _ev("Watercolour painting class", expect_score=0.0),
            ],
        ),
        (
            "D",
            "running",
            [
                # False keyword hit: the word matches, the topic does not.
                _ev(
                    "Running a small business: a workshop for new founders",
                    expect_score=0.0,
                ),
            ],
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
        before = len(_RAW)
        try:
            matched, filter_label = _filter_events_by_query(events, request)
        except Exception as exc:  # noqa: BLE001 - print and move on, never retry
            print(f"    FAILED: {type(exc).__name__}: {exc}")
            traceback.print_exc(limit=3)
            continue

        returned_ids = {id(e) for e in matched}

        # The raw response, so a failure is READ rather than inferred.
        raw = _RAW[before] if len(_RAW) > before else "<no call made>"
        print(f"    raw: {json.dumps(raw, default=str)[:600]}")

        # Fallback signature: the model was never usable, so the keyword branch stamped
        # every row 0.0, returned "" for the label, and (finding no substring hit)
        # returned EVERY event. Any all-zero result must be checked against this before
        # being read as a judgement.
        scores = [e.get("topic_score") for e in events]
        if (
            not filter_label
            and all(s == 0.0 for s in scores)
            and len(matched) in (0, len(events))
        ):
            print(
                "    *** LOOKS LIKE THE KEYWORD FALLBACK — the model answer was "
                "unusable (truncated or malformed). These are not judgements. ***"
            )

        print(f"    filter label: {filter_label!r}")
        print(
            f"    {'title':52} | {'score':>5} | {'exp':>4} | {'':3} | "
            f"{'member':12} | {'exp':12} | mismatch"
        )
        off_score = off_member = 0
        for ev in events:
            title = str(ev.get("title") or "")
            # .get, not [...]: a missing key is itself the finding — every path through
            # the matcher is supposed to stamp every candidate.
            score = ev.get("topic_score", "MISSING")
            mismatch = ev.get("topic_mismatch", "MISSING")
            returned = id(ev) in returned_ids
            exp_score = ev.get("_expect_score")
            exp_match = bool(ev.get("_expect_match"))

            # ±0.2 — the bands are 0.3 wide, so anything inside that is the same band or
            # its neighbour. Wider than that is a real disagreement with the ladder.
            flag = ""
            if isinstance(score, float) and exp_score is not None:
                if abs(score - exp_score) > 0.2:
                    flag = "<<"
                    off_score += 1
            if returned != exp_match:
                flag = "<<!"
                off_member += 1

            seen[(request, title)] = (score, returned)
            print(
                f"    {title:52} | {score:>5} | "
                f"{'-' if exp_score is None else exp_score:>4} | {flag:3} | "
                f"{'RETURNED' if returned else 'not returned':12} | "
                f"{'RETURNED' if exp_match else 'not returned':12} | {mismatch!r}"
            )
        distinct = len({e.get("topic_score") for e in events})
        print(
            f"    ({len(events)} scored, {len(matched)} returned, "
            f"{distinct} distinct score{'' if distinct == 1 else 's'}; "
            f"{off_score} score off by >0.2, {off_member} membership surprise"
            f"{'' if off_member == 1 else 's'})"
        )

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
