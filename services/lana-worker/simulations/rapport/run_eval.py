"""
run_eval.py — rapport claim-extraction fixture harness.

Runs tests/rapport/fixtures.yaml against the REAL extraction path
(app.vertex_extract.incremental_claims_from_utterance -> parse_incremental_claims_data),
then the same PII-redaction pass every real write goes through
(app.claims_persist.clean_claims_for_persist), and scores:

  - facet precision / recall  (mechanical diff against expect_claims)
  - anonymization             (mechanical must_not_contain check, post-redaction)
  - kids_count correctness    (mechanical)
  - followup presence         (mechanical — expect_followup bool)
  - gap quality               (LLM judge, 3-point scale — the one subjective axis;
                                only run when --judge is passed, since it costs a call)

No Supabase involved — nothing here writes to the DB, so nothing needs mocking beyond
what clean_claims_for_persist already is (a pure function of the claims list).

NOTE ON PROVIDER: incremental_claims_from_utterance routes through orchestrator/llm.py,
which honors LANA_LLM_PROVIDER. In this repo's .env.local that's set to "openai" (same as
the rest of the sim suite), so locally this exercises the OpenAI path, not the Vertex
Gemini path production actually runs — that's expected and matches how the rest of your
sim harness already runs against OpenAI locally regardless of Lana's prod provider.

Usage:
    cd services/lana-worker
    python tests/rapport/run_eval.py                 # all fixtures, mechanical axes only
    python tests/rapport/run_eval.py --id rapport_006 # one fixture
    python tests/rapport/run_eval.py --judge          # also run the gap-quality LLM judge
    python tests/rapport/run_eval.py --dry-run        # validate fixture shapes, no API calls
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parents[2]  # services/lana-worker
sys.path.insert(0, str(_ROOT))
load_dotenv(_ROOT.parents[1] / ".env.local", override=True)

from app.models import ExtractedClaim  # noqa: E402

# Import app.orchestrator (the PACKAGE) before anything imports app.vertex_extract
# directly. vertex_extract.py's own top-level import pulls in app.orchestrator.json_util,
# which (if orchestrator hasn't been touched yet) runs orchestrator/__init__ ->
# pipeline -> recall -> `from app.vertex_extract import vertex_embed` while vertex_extract
# is still mid-import — a circular ImportError. Triggering the orchestrator package import
# first, on its own, breaks the cycle the same way the real app's main.py import order does.
import app.orchestrator  # noqa: E402,F401

from app.vertex_extract import (  # noqa: E402
    incremental_claims_from_utterance,
    parse_incremental_claims_data,
)
from app.claims_persist import clean_claims_for_persist  # noqa: E402

FIXTURES_PATH = Path(__file__).parent / "fixtures.yaml"

_GAP_QUALITY_MODEL = "gpt-4o"


def _load_fixtures() -> list[dict[str, Any]]:
    with open(FIXTURES_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data["fixtures"]


def _claim_matches(claim: ExtractedClaim, exp: dict[str, Any]) -> bool:
    # concept is NOT matched exactly: only its regex shape is fixed by the extractor
    # (^[a-z][a-z0-9_]{1,63}$), the exact slug is the model's free choice ("karate_practice"
    # vs "karate" are both valid for the same thread) — bucket + label_has carry the real
    # matching signal, concept is recorded in fixtures for readability only.
    if claim.bucket != exp["bucket"]:
        return False
    label_has = str(exp.get("label_has", "")).strip()
    if label_has and label_has.lower() not in claim.label.lower():
        return False
    if claim.confidence < float(exp.get("confidence_min", 0.0)):
        return False
    if "disclosure" in exp and claim.disclosure != exp["disclosure"]:
        return False
    if "transient" in exp and claim.transient != bool(exp["transient"]):
        return False
    if "vague" in exp and claim.vague != bool(exp["vague"]):
        return False
    return True


def _check_anonymization(
    claims: list[ExtractedClaim], followup: str | None, must_not_contain: list[str]
) -> list[str]:
    """Returns list of leaked strings found (empty = clean).

    Checks claim fields (post clean_claims_for_persist redaction) AND the raw
    followup_question — redact_pii is only ever applied to claim label/source_quote/
    synonyms (see claims_persist.clean_claims_for_persist), the followup question text is
    NOT redacted anywhere in the pipeline, so it is a real, separate leak vector.
    """
    leaks = []
    haystacks = []
    for c in claims:
        haystacks.append(c.label or "")
        haystacks.append(c.source_quote or "")
        haystacks.extend(c.synonyms or [])
    haystacks.append(followup or "")
    blob = " ".join(haystacks).lower()
    for needle in must_not_contain:
        if needle.lower() in blob:
            leaks.append(needle)
    return leaks


def _judge_gap_quality(question: str, client) -> tuple[str, str]:
    """Rates a followup question too-vague / just-right / too-narrow. Returns (verdict, reasoning)."""
    prompt = (
        "You are rating ONE follow-up question a concierge AI ('Lana') asked a mom after she "
        "shared something about herself, on a 3-point scale:\n"
        "- too-vague: generic, could apply to anyone, doesn't build on what she said\n"
        "- just-right: warm, specific, clearly grounded in what she just said, opens a natural "
        "next thread without being invasive\n"
        "- too-narrow: overly specific/leading, forces a narrow answer, or feels invasive/sensitive "
        "for a first follow-up\n\n"
        f"The question: \"{question}\"\n\n"
        "Return JSON: {\"verdict\": \"too-vague|just-right|too-narrow\", \"reasoning\": \"one sentence\"}"
    )
    completion = client.chat.completions.create(
        model=_GAP_QUALITY_MODEL,
        messages=[
            {"role": "system", "content": "You are a precise evaluation judge."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        response_format={"type": "json_object"},
    )
    import json

    data = json.loads(completion.choices[0].message.content)
    return data.get("verdict", "unknown"), data.get("reasoning", "")


def run(fixture_id: str | None, dry_run: bool, judge: bool) -> None:
    fixtures = _load_fixtures()
    if fixture_id:
        fixtures = [f for f in fixtures if f["id"] == fixture_id]
        if not fixtures:
            raise SystemExit(f"No fixture with id {fixture_id!r}")

    print(f"[rapport-eval] {len(fixtures)} fixture(s) loaded from {FIXTURES_PATH.name}")
    if dry_run:
        for fx in fixtures:
            n_exp = len(fx.get("expect_claims", []))
            print(f"  [dry-run] {fx['id']}: input={fx['input']!r} expect_claims={n_exp}")
        return

    judge_client = None
    if judge:
        from openai import OpenAI

        judge_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))

    total_returned = 0
    total_returned_matched = 0
    total_expected = 0
    total_expected_matched = 0
    anonymization_fails: list[tuple[str, list[str]]] = []
    kids_count_fails: list[str] = []
    followup_fails: list[str] = []
    no_claims_fails: list[str] = []
    gap_quality_results: list[tuple[str, str, str]] = []

    for fx in fixtures:
        fx_id = fx["id"]
        raw = incremental_claims_from_utterance(fx["input"], fx.get("existing_labels"))
        _nickname, claims, kids_count, followup = parse_incremental_claims_data(raw)
        cleaned = clean_claims_for_persist(list(claims))

        expect_no_claims = bool(fx.get("expect_no_claims", False))
        expected = fx.get("expect_claims", [])
        fixture_failed = False

        if expect_no_claims:
            if cleaned:
                no_claims_fails.append(fx_id)
                fixture_failed = True
            total_returned += len(cleaned)
            # any returned claim is a false positive; none is a clean pass, no precision/recall math needed
        else:
            total_returned += len(cleaned)
            for c in cleaned:
                if any(_claim_matches(c, exp) for exp in expected):
                    total_returned_matched += 1
            total_expected += len(expected)
            for exp in expected:
                if any(_claim_matches(c, exp) for c in cleaned):
                    total_expected_matched += 1
                else:
                    fixture_failed = True

        if "expect_kids_count" in fx and kids_count != fx["expect_kids_count"]:
            kids_count_fails.append(f"{fx_id} (got {kids_count}, expected {fx['expect_kids_count']})")
            fixture_failed = True

        if "expect_followup" in fx:
            got_followup = followup is not None
            if got_followup != bool(fx["expect_followup"]):
                followup_fails.append(f"{fx_id} (got followup={got_followup}, expected {fx['expect_followup']})")
                fixture_failed = True

        must_not_contain = fx.get("must_not_contain", [])
        if must_not_contain:
            leaks = _check_anonymization(cleaned, followup, must_not_contain)
            if leaks:
                anonymization_fails.append((fx_id, leaks))
                fixture_failed = True

        status = "FAIL" if fixture_failed else "ok"
        print(f"  [{status}] {fx_id}: {len(cleaned)} claim(s) returned, {len(expected)} expected")

        if judge and followup:
            verdict, reasoning = _judge_gap_quality(followup, judge_client)
            gap_quality_results.append((fx_id, verdict, reasoning))
            print(f"       gap_quality={verdict} — {reasoning}")

    precision = (total_returned_matched / total_returned) if total_returned else 1.0
    recall = (total_expected_matched / total_expected) if total_expected else 1.0
    anonymization_pass_rate = 1.0 - (len(anonymization_fails) / max(1, sum(1 for f in fixtures if f.get("must_not_contain"))))

    print("\n[rapport-eval] summary")
    print(f"  facet precision:        {precision:.2f}  ({total_returned_matched}/{total_returned})")
    print(f"  facet recall:           {recall:.2f}  ({total_expected_matched}/{total_expected})")
    print(f"  anonymization pass rate:{anonymization_pass_rate:.2f}  ({len(anonymization_fails)} leak case(s))")
    print(f"  expect_no_claims fails: {len(no_claims_fails)}  {no_claims_fails}")
    print(f"  kids_count fails:       {len(kids_count_fails)}  {kids_count_fails}")
    print(f"  followup fails:         {len(followup_fails)}  {followup_fails}")

    if anonymization_fails:
        print("\n[rapport-eval] ANONYMIZATION LEAKS (hard fail)")
        for fx_id, leaks in anonymization_fails:
            print(f"  {fx_id}: leaked {leaks}")

    if judge and gap_quality_results:
        just_right = sum(1 for _, v, _ in gap_quality_results if v == "just-right")
        print(f"\n[rapport-eval] gap quality: {just_right}/{len(gap_quality_results)} just-right")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", dest="fixture_id", help="Run only this fixture id")
    parser.add_argument("--dry-run", action="store_true", help="Validate fixtures, no API calls")
    parser.add_argument("--judge", action="store_true", help="Also run the gap-quality LLM judge")
    args = parser.parse_args()
    run(args.fixture_id, args.dry_run, args.judge)
