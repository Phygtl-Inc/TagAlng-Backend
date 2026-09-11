"""
eval_intent.py
Classifier eval for the collapsed intent taxonomy (intent_test_set.json).

For each utterance:
  1. POST /lana/sessions                   — open a fresh cold session
  2. POST /lana/sessions/{id}/messages     — send the utterance (single turn)
  3. GET  /lana/sessions/{id}              — read context._discovery_slots.linear_intent
  4. Compare predicted linear_intent to the accepted set for this test intent

The accepted set is built directly from the test set:
  - If the intent block has "collapsed_from", accept any value in that list.
  - Otherwise accept the intent_id itself (with name overrides for codebase mismatches).

Usage:
  python eval_intent.py                             # all intents, all difficulties
  python eval_intent.py --intent discovery.find     # one intent
  python eval_intent.py --difficulty hard           # filter by difficulty
  python eval_intent.py --intent identity.update --difficulty easy,medium
  python eval_intent.py --dry-run                   # print matrix, no API calls
"""

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# Config — mirrors simulation.py env var conventions
# ---------------------------------------------------------------------------

LANA_BASE_URL = os.environ.get("LANA_BASE_URL", "http://localhost:8000")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
SIM_EMAIL = os.environ.get("SIM_EMAIL", "p1-sim@phygtl.dev")
SIM_PASSWORD = os.environ.get("SIM_PASSWORD", "")

TEST_SET_PATH = Path(__file__).parent.parent.parent.parent / "scratch" / "intent_test_set.json"

# Timeout per HTTP call — Flash classification can be slow under load
HTTP_TIMEOUT = 45

# ---------------------------------------------------------------------------
# Auth — Supabase password-grant JWT
# ---------------------------------------------------------------------------

def _get_jwt() -> str:
    if not SUPABASE_URL or not SUPABASE_ANON_KEY or not SIM_PASSWORD:
        raise RuntimeError(
            "Missing Supabase credentials. Set SUPABASE_URL, SUPABASE_ANON_KEY, and SIM_PASSWORD "
            "(e.g. load .env.local before running)."
        )
    resp = httpx.post(
        f"{SUPABASE_URL}/auth/v1/token?grant_type=password",
        headers={"apikey": SUPABASE_ANON_KEY, "Content-Type": "application/json"},
        json={"email": SIM_EMAIL, "password": SIM_PASSWORD},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# Name overrides: test-set intent_id → codebase linear_intent
# (only needed where they differ and collapsed_from is absent)
# ---------------------------------------------------------------------------

_CODEBASE_NAME: dict[str, str] = {
    "identity.show": "identity.show_my_profile",
}

# discovery.find_in_block also covers discovery.block_log in the codebase
_EXTRA_ACCEPTS: dict[str, set[str]] = {
    "discovery.find_in_block": {"discovery.block_log"},
}

# fallback.* intents in the test taxonomy all route to system.out_of_scope in the codebase.
# The classifier fires goal=out_of_scope / linear_intent=system.out_of_scope for anything
# Lana can't handle. None covers cases where _discovery_slots wasn't written (e.g. fast paths).
_FALLBACK_ACCEPTS: set[str] = {"system.out_of_scope", None}  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Build the accept map from the test set
# ---------------------------------------------------------------------------

def _build_accept_map(intents: list[dict[str, Any]]) -> dict[str, set[str]]:
    """Maps each test intent_id to the set of raw linear_intent values that count as a pass."""
    result: dict[str, set[str]] = {}
    for block in intents:
        intent_id: str = block["intent_id"]
        collapsed_from: list[str] = block.get("collapsed_from", [])

        if collapsed_from:
            accept = set(collapsed_from)
        else:
            raw = _CODEBASE_NAME.get(intent_id, intent_id)
            accept = {raw}

        accept |= _EXTRA_ACCEPTS.get(intent_id, set())
        result[intent_id] = accept
    return result


# ---------------------------------------------------------------------------
# Lana API helpers
# ---------------------------------------------------------------------------

def _create_session(jwt: str, client: httpx.Client) -> str:
    resp = client.post(
        f"{LANA_BASE_URL}/lana/sessions",
        json={"purpose": "lana", "force_new": True},
        headers={"Authorization": f"Bearer {jwt}"},
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["session_id"]


def _send_message(session_id: str, message: str, jwt: str, client: httpx.Client) -> None:
    resp = client.post(
        f"{LANA_BASE_URL}/lana/sessions/{session_id}/messages",
        json={"message": message},
        headers={"Authorization": f"Bearer {jwt}"},
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()


def _get_linear_intent(session_id: str, jwt: str, client: httpx.Client) -> tuple[str | None, float]:
    """Returns (linear_intent, confidence) from session context._discovery_slots."""
    resp = client.get(
        f"{LANA_BASE_URL}/lana/sessions/{session_id}",
        headers={"Authorization": f"Bearer {jwt}"},
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    ctx: dict[str, Any] = resp.json().get("context") or {}
    slots: dict[str, Any] = ctx.get("_discovery_slots") or {}
    linear_intent: str | None = slots.get("linear_intent") or None
    confidence: float = float(slots.get("confidence") or 0.0)
    return linear_intent, confidence


# ---------------------------------------------------------------------------
# Single utterance evaluation
# ---------------------------------------------------------------------------

def _classify_utterance(utterance: str, jwt: str, client: httpx.Client) -> tuple[str | None, float]:
    session_id = _create_session(jwt, client)
    _send_message(session_id, utterance, jwt, client)
    return _get_linear_intent(session_id, jwt, client)


def _is_pass(
    intent_id: str,
    predicted: str | None,
    accept_map: dict[str, set[str]],
) -> bool:
    if intent_id.startswith("fallback."):
        return predicted in _FALLBACK_ACCEPTS
    return predicted in accept_map.get(intent_id, set())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Intent classifier eval against intent_test_set.json")
    parser.add_argument("--intent", help="Comma-separated intent_ids to run (default: all)")
    parser.add_argument(
        "--difficulty",
        help="Comma-separated difficulties to include: easy, medium, hard (default: all)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print the run matrix without calling any API")
    parser.add_argument(
        "--out",
        default="scratch/eval_intent_{ts}.json",
        help="Output file path relative to repo root (default: scratch/eval_intent_{ts}.json)",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = _parse_args()

    if not TEST_SET_PATH.exists():
        print(f"[eval] test set not found at {TEST_SET_PATH}")
        sys.exit(1)

    data = json.loads(TEST_SET_PATH.read_text(encoding="utf-8"))
    intents: list[dict[str, Any]] = data["intents"]
    accept_map = _build_accept_map(intents)

    intent_filter = {i.strip() for i in args.intent.split(",")} if args.intent else None
    difficulty_filter = {d.strip() for d in args.difficulty.split(",")} if args.difficulty else None

    # Build run matrix: list of (intent_id, utterance, difficulty, notes)
    matrix: list[tuple[str, str, str, str]] = []
    for block in intents:
        intent_id = block["intent_id"]
        if intent_filter and intent_id not in intent_filter:
            continue
        for u in block["utterances"]:
            diff = u.get("difficulty", "")
            if difficulty_filter and diff not in difficulty_filter:
                continue
            matrix.append((intent_id, u["utterance"], diff, u.get("notes", "")))

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    print(f"[eval] {ts} — {len(matrix)} utterances queued across {len({m[0] for m in matrix})} intents")

    if args.dry_run:
        print(f"\n{'INTENT':40s}  {'DIFF':6}  {'EXPECTS':30s}  UTTERANCE")
        print("-" * 120)
        for intent_id, utterance, diff, _ in matrix:
            expects = ", ".join(sorted(accept_map.get(intent_id, {"system.out_of_scope"}) if not intent_id.startswith("fallback.") else {"system.out_of_scope"}))
            print(f"  {intent_id:40s}  [{diff:6s}]  {expects:30s}  {utterance[:60]}")
        print(f"\n[eval] dry-run — {len(matrix)} utterances, no API calls")
        return

    # ---------------------------------------------------------------------------
    # Run
    # ---------------------------------------------------------------------------

    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    out_path_str = args.out.replace("{ts}", ts)
    out_path = Path(out_path_str)
    if not out_path.is_absolute():
        out_path = Path(__file__).parent.parent.parent.parent / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)

    def _flush() -> None:
        out_path.write_text(
            json.dumps({"meta": {"ts": ts, "total": len(matrix), "completed": len(results) + len(errors), "passed": sum(1 for r in results if r["passed"]), "errors": len(errors), "intent_filter": args.intent, "difficulty_filter": args.difficulty}, "results": results, "errors": errors}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    print(f"[eval] logging in as {SIM_EMAIL} ...")
    jwt = _get_jwt()
    jwt_fetched_at = datetime.now(timezone.utc)
    print(f"[eval] authenticated — saving progress to {out_path}")

    with httpx.Client() as client:
        for i, (intent_id, utterance, difficulty, notes) in enumerate(matrix, 1):
            # Refresh JWT every 45 minutes (Supabase tokens expire after 1 hour)
            age_minutes = (datetime.now(timezone.utc) - jwt_fetched_at).total_seconds() / 60
            if age_minutes > 45:
                print(f"  [auth] refreshing JWT (age={age_minutes:.0f}m)")
                jwt = _get_jwt()
                jwt_fetched_at = datetime.now(timezone.utc)

            run_id = str(uuid.uuid4())[:8]
            print(f"  [{i}/{len(matrix)}] {intent_id} [{difficulty}] — {utterance[:70]}")
            try:
                predicted, confidence = _classify_utterance(utterance, jwt, client)
                passed = _is_pass(intent_id, predicted, accept_map)
                marker = "PASS" if passed else "FAIL"
                print(f"    → {marker}  predicted={predicted}  conf={confidence:.2f}")
                results.append({
                    "run_id": run_id,
                    "intent_id": intent_id,
                    "utterance": utterance,
                    "difficulty": difficulty,
                    "notes": notes,
                    "predicted": predicted,
                    "confidence": confidence,
                    "passed": passed,
                })
            except Exception as exc:
                print(f"    → ERROR: {exc}")
                errors.append({
                    "run_id": run_id,
                    "intent_id": intent_id,
                    "utterance": utterance,
                    "difficulty": difficulty,
                    "error": str(exc),
                })
            _flush()

    # ---------------------------------------------------------------------------
    # Report
    # ---------------------------------------------------------------------------

    print(f"\n{'=' * 60}")
    print(f"{'INTENT':40s}  {'PASS':>6}  {'TOTAL':>6}  {'PCT':>6}")
    print(f"{'-' * 60}")

    by_intent: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        by_intent.setdefault(r["intent_id"], []).append(r)

    for intent_id in sorted(by_intent):
        rs = by_intent[intent_id]
        n_pass = sum(1 for r in rs if r["passed"])
        n_total = len(rs)
        pct = n_pass / n_total * 100 if n_total else 0
        print(f"{intent_id:40s}  {n_pass:>6}  {n_total:>6}  {pct:>5.0f}%")

    print(f"{'-' * 60}")
    n_pass_total = sum(1 for r in results if r["passed"])
    n_total = len(results)
    pct_total = n_pass_total / n_total * 100 if n_total else 0
    print(f"{'TOTAL':40s}  {n_pass_total:>6}  {n_total:>6}  {pct_total:>5.0f}%")

    if errors:
        print(f"\n  Errors ({len(errors)}):")
        for e in errors:
            print(f"    {e['intent_id']} — {e['utterance'][:60]}: {e['error']}")

    # Per-difficulty breakdown
    print(f"\n{'DIFFICULTY BREAKDOWN':40s}")
    for diff in ("easy", "medium", "hard"):
        rs = [r for r in results if r["difficulty"] == diff]
        if not rs:
            continue
        n = sum(1 for r in rs if r["passed"])
        print(f"  {diff:10s}  {n}/{len(rs)}  ({n / len(rs) * 100:.0f}%)")

    # Failures for inspection
    failures = [r for r in results if not r["passed"]]
    if failures:
        print(f"\n  Failures ({len(failures)}):")
        for r in failures:
            expects = ", ".join(sorted(accept_map.get(r["intent_id"], {"system.out_of_scope"})))
            print(f"    [{r['difficulty']:6s}] {r['intent_id']:40s}  expected={expects}  got={r['predicted']}  \"{r['utterance'][:60]}\"")

    # Confusion matrix — only for failed rows, predicted vs expected intent
    if failures:
        print(f"\n  CONFUSION (expected → predicted, failures only):")
        confusion: dict[str, dict[str, int]] = {}
        for r in failures:
            exp = r["intent_id"]
            pred = r["predicted"] or "None"
            confusion.setdefault(exp, {}).setdefault(pred, 0)
            confusion[exp][pred] += 1
        for exp in sorted(confusion):
            for pred, count in sorted(confusion[exp].items(), key=lambda x: -x[1]):
                print(f"    {exp:40s} → {pred:40s}  ×{count}")

    _flush()
    print(f"\n[eval] output → {out_path}")


if __name__ == "__main__":
    main()
