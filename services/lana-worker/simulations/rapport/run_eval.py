"""
run_eval.py — rapport claim-extraction fixture harness.

Runs simulations/rapport/fixtures.yaml against the REAL extraction path
(app.vertex_extract.incremental_claims_from_utterance -> parse_incremental_claims_data),
then the same PII-redaction pass every real write goes through
(app.claims_persist.clean_claims_for_persist), and scores:

  - facet precision / recall  (mechanical — ONE-TO-ONE matching against expect_claims)
  - anonymization             (mechanical must_not_contain check, post-redaction)
  - kids_count correctness    (mechanical)
  - followup presence         (mechanical — expect_followup bool)
  - followup not-a-repeat     (mechanical — only for fixtures that set recent_questions)
  - gap quality               (LLM judge, 3-point scale — the one subjective axis;
                                only run when --judge is passed, since it costs a call.
                                ADVISORY: it never touches the process exit code.)

No Supabase involved — nothing here writes to the DB, so nothing needs mocking beyond
what clean_claims_for_persist already is (a pure function of the claims list).

NOTE ON PROVIDER: incremental_claims_from_utterance routes through orchestrator/llm.py,
which honors LANA_LLM_PROVIDER. In this repo's .env.local that's set to "openai" (same as
the rest of the sim suite), so locally this exercises the OpenAI path, not the Vertex
Gemini path production actually runs — that's expected and matches how the rest of your
sim harness already runs against OpenAI locally regardless of Lana's prod provider.

Usage (run from this directory — the script puts services/lana-worker on sys.path itself):
    cd services/lana-worker/simulations/rapport
    python run_eval.py                  # all fixtures, mechanical axes only
    python run_eval.py --id rapport_006 # one fixture
    python run_eval.py --judge          # also run the gap-quality LLM judge (costs a call)
    python run_eval.py --dry-run        # validate fixture shapes, no API calls

Writes out/report.md. That artifact contains PRE-redaction claim text, i.e. the fixtures'
planted PII — `simulations/*/out/` is gitignored; keep it that way.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# Fixtures include ES/PT inputs ("estou cansada", "meu kid"), and extracted claim text is echoed
# to stdout — so the default Windows console codepage can kill a run mid-suite. See the same
# guard in policy_eval/run_eval.py.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_ROOT = Path(__file__).resolve().parents[2]  # services/lana-worker
sys.path.insert(0, str(_ROOT))
load_dotenv(_ROOT.parents[1] / ".env.local", override=True)

from app.models import ExtractedClaim  # noqa: E402

# Defensive import-order guard, NOT currently load-bearing: app/orchestrator/__init__.py
# resolves its exports lazily (PEP 562), so importing app.vertex_extract first no longer
# closes the old vertex_extract -> json_util -> __init__ -> pipeline -> ... cycle. Touching
# the package here first is free and keeps this harness working if that laziness is ever
# reverted, which is why it stays.
import app.orchestrator  # noqa: E402,F401

from app.vertex_extract import (  # noqa: E402
    incremental_claims_from_utterance,
    parse_incremental_claims_data,
)
from app.claims_persist import clean_claims_for_persist  # noqa: E402

FIXTURES_PATH = Path(__file__).parent / "fixtures.yaml"
OUT_DIR = Path(__file__).parent / "out"

_GAP_QUALITY_MODEL = "gpt-4o"
# "unscored" is part of the vocabulary on purpose: a judge that errored/timed out/returned
# garbage must be SURFACED, never folded into the just-right ratio in either direction.
_GAP_VERDICTS = ("too-vague", "just-right", "too-narrow")
_UNSCORED = "unscored"


def _load_fixtures() -> list[dict[str, Any]]:
    with open(FIXTURES_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data["fixtures"]


# ---------------------------------------------------------------------------
# Fixture validation (real work behind --dry-run)
# ---------------------------------------------------------------------------

def validate_fixtures(fixtures: list[dict[str, Any]]) -> list[str]:
    """Load-time shape validation. Returns a list of human-readable errors (empty = valid).

    Every rule here is one the harness would otherwise hit as a KeyError, a silently inert
    matcher, or a self-contradicting expectation. Rules are deliberately narrow so they do
    not false-positive on the current fixture set (a validator that cries wolf gets disabled).
    """
    errors: list[str] = []
    seen_ids: set[str] = set()
    for i, fx in enumerate(fixtures):
        fx_id = str(fx.get("id") or "").strip()
        where = fx_id or f"<fixture #{i}>"
        if not fx_id:
            errors.append(f"{where}: missing/empty `id`")
        elif fx_id in seen_ids:
            errors.append(f"{where}: duplicate `id` (ids must be unique — --id and the report key on it)")
        else:
            seen_ids.add(fx_id)

        if not str(fx.get("input") or "").strip():
            errors.append(f"{where}: missing/empty `input` (nothing to send the extractor)")

        expected = fx.get("expect_claims") or []
        # Only the CONTRADICTION is an error: expect_no_claims TRUE while also listing
        # expectations. rapport_006 legitimately carries `expect_no_claims: false` next to
        # expect_claims, so the mere presence of both keys must NOT trip this.
        if bool(fx.get("expect_no_claims", False)) and expected:
            errors.append(
                f"{where}: expect_no_claims is true but expect_claims lists "
                f"{len(expected)} expectation(s) — contradictory"
            )

        if not isinstance(expected, list):
            errors.append(f"{where}: expect_claims must be a list")
            continue
        for j, exp in enumerate(expected):
            ewhere = f"{where}.expect_claims[{j}]"
            if not isinstance(exp, dict):
                errors.append(f"{ewhere}: must be a mapping")
                continue
            # run() indexes exp["bucket"] unguarded, and bucket is the primary match signal.
            if not str(exp.get("bucket") or "").strip():
                errors.append(f"{ewhere}: missing `bucket` (the primary match key)")
            label_has = str(exp.get("label_has", "") or "").strip()
            alts = exp.get("label_has_any") or []
            if not isinstance(alts, list):
                errors.append(f"{ewhere}: label_has_any must be a list")
                alts = []
            alts = [str(a).strip() for a in alts if str(a).strip()]
            # An expectation with an empty label_has and no alternatives degenerates to
            # "any claim in this bucket" — an inert matcher that scores a vacuous pass.
            # rapport_012 used to be exactly this; the guard stops it recurring silently.
            if not label_has and not alts:
                errors.append(
                    f"{ewhere}: label_has is empty and no label_has_any given — inert matcher "
                    f"(would match ANY claim in bucket {exp.get('bucket')!r})"
                )
    return errors


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def _claim_matches(claim: ExtractedClaim, exp: dict[str, Any]) -> bool:
    # concept is NOT matched exactly: only its regex shape is fixed by the extractor
    # (^[a-z][a-z0-9_]{1,63}$), the exact slug is the model's free choice ("karate_practice"
    # vs "karate" are both valid for the same thread) — bucket + label_has carry the real
    # matching signal, concept is recorded in fixtures for readability only.
    if claim.bucket != exp["bucket"]:
        return False
    label = (claim.label or "").lower()
    label_has = str(exp.get("label_has", "") or "").strip()
    if label_has and label_has.lower() not in label:
        return False
    # label_has_any: for inputs whose English canonical label genuinely has several valid
    # phrasings (a PT-only utterance whose label is still English). ANY alternative hitting
    # is enough; combined with label_has it is an AND (both constraints must hold).
    alts = [str(a).strip().lower() for a in (exp.get("label_has_any") or []) if str(a).strip()]
    if alts and not any(a in label for a in alts):
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


def _match_claims(
    claims: list[ExtractedClaim], expected: list[dict[str, Any]]
) -> tuple[int, list[int]]:
    """One-to-one MAXIMUM bipartite matching between returned claims and expectations.

    Returns (n_matched, unmatched_claim_indices).

    Why not two independent any() scans (what this replaced): those let ONE returned claim
    satisfy N expectations (recall counted N hits off a single claim) and let N returned
    claims all satisfy the SAME expectation (duplicates were never a precision penalty).
    A claim may now satisfy at most one expectation and vice versa, and BOTH ratios are fed
    from this single assignment, so the two axes can never disagree about what matched.

    Kuhn's algorithm (augmenting paths) — MAXIMUM matching, not first-fit greedy. Greedy
    under-counts: if expectation A accepts only claim 0 and expectation B accepts claims 0
    and 1, greedy handing claim 0 to B strands A even though a perfect assignment exists.
    Fixture sizes are tiny (<=3 expectations), so the recursion and the O(V*E) cost are free.
    """
    adj: list[list[int]] = [
        [ci for ci, c in enumerate(claims) if _claim_matches(c, exp)] for exp in expected
    ]
    exp_for_claim: list[int | None] = [None] * len(claims)

    def _augment(ei: int, seen: list[bool]) -> bool:
        for ci in adj[ei]:
            if seen[ci]:
                continue
            seen[ci] = True
            holder = exp_for_claim[ci]
            if holder is None or _augment(holder, seen):
                exp_for_claim[ci] = ei
                return True
        return False

    n_matched = 0
    for ei in range(len(expected)):
        if _augment(ei, [False] * len(claims)):
            n_matched += 1
    unmatched = [ci for ci, owner in enumerate(exp_for_claim) if owner is None]
    return n_matched, unmatched


# ---------------------------------------------------------------------------
# Anonymization
# ---------------------------------------------------------------------------

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _normalize(s: str) -> str:
    """Case-fold and flatten every non-alphanumeric run to a single space."""
    return _NON_ALNUM.sub(" ", str(s or "").lower()).strip()


def _needle_in_field(field_text: str, needle: str) -> bool:
    """True when `needle` leaks into `field_text`.

    Deliberately MORE sensitive than a plain case-folded substring test, because this is the
    PII axis and it must fail CLOSED: it matches on the raw case-folded text OR on the
    normalised text, so punctuation/whitespace/casing tricks ("j a n e@example.com",
    "Lincoln-Elementary", "123  Maple  Street") cannot hide a leak. NO word boundaries —
    boundaries would make this LESS sensitive, which is the wrong direction for PII.
    The raw test is kept as an OR arm so no needle that worked before can stop working.
    """
    if not field_text:
        return False
    if needle.lower() in field_text.lower():
        return True
    n_norm = _normalize(needle)
    return bool(n_norm) and n_norm in _normalize(field_text)


def _claim_fields(c: ExtractedClaim) -> list[str]:
    """Every text field of one claim, kept SEPARATE (see _check_anonymization)."""
    out = [c.label or "", c.source_quote or ""]
    out.extend(str(s) for s in (c.synonyms or []))
    # `details` is redacted downstream by clean_claims_for_persist but was once omitted from
    # this scan, so a leak living only in `details` was invisible to this axis.
    det = getattr(c, "details", None)
    if isinstance(det, str):
        out.append(det)
    elif isinstance(det, (list, tuple)):
        out.extend(str(d) for d in det)
    elif isinstance(det, dict):
        out.extend(str(v) for v in det.values())
    return out


def _check_anonymization(
    claims: list[ExtractedClaim], followup: str | None, must_not_contain: list[str],
    nickname: str | None = None,
) -> list[str]:
    """Returns list of leaked needles found (empty = clean).

    Scans EVERY field a leak could ride out on, each one SEPARATELY:
      * label / source_quote / each synonym / details — redacted by clean_claims_for_persist.
      * the extracted nickname — free text taken from the user's own words.
      * followup_question — NEVER redacted anywhere in the real pipeline, so it is the most
        exposed vector of all.

    Per-field, not one space-joined blob: joining invents matches that span a field boundary
    (needle "Sara Lincoln" "found" across synonyms[0]+synonyms[1] is not a leak of that
    string) — a false positive on a HARD_FAIL axis. Sensitivity is recovered, and then some,
    by the normalisation in _needle_in_field rather than by concatenation.
    """
    fields: list[str] = []
    for c in claims:
        fields.extend(_claim_fields(c))
    fields.append(followup or "")
    fields.append(nickname or "")

    leaks: list[str] = []
    for needle in must_not_contain:
        if any(_needle_in_field(f, str(needle)) for f in fields):
            leaks.append(str(needle))
    return leaks


# ---------------------------------------------------------------------------
# Judge (advisory axis — total function, never raises, never gates)
# ---------------------------------------------------------------------------

def _judge_gap_quality(question: str, client) -> tuple[str, str]:
    """Rates a followup question too-vague / just-right / too-narrow.

    TOTAL by construction: any transport error, timeout, malformed JSON, missing client or
    unrecognised verdict returns ("unscored", why). The gap-quality axis is OPTIONAL and
    ADVISORY — it must never take down the authoritative mechanical run, which is exactly
    what an uncaught OpenAI error used to do (the process died before the summary printed).
    """
    if client is None:
        return _UNSCORED, "no judge client (OpenAI client unavailable)"
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
    try:
        completion = client.chat.completions.create(
            model=_GAP_QUALITY_MODEL,
            messages=[
                {"role": "system", "content": "You are a precise evaluation judge."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        data = json.loads(completion.choices[0].message.content or "")
        verdict = str(data.get("verdict", "")).strip().lower()
        if verdict not in _GAP_VERDICTS:
            return _UNSCORED, f"judge returned unrecognised verdict {verdict!r}"
        return verdict, str(data.get("reasoning", ""))
    except Exception as e:  # noqa: BLE001 — advisory axis: degrade to unscored, never raise
        return _UNSCORED, f"judge call failed: {type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Per-fixture record + report artifact
# ---------------------------------------------------------------------------

@dataclass
class FixtureResult:
    fx_id: str
    text: str
    n_expected: int = 0
    n_returned: int = 0
    n_matched: int = 0
    axes_failed: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)
    # PRE-redaction claim text — this is why out/ is gitignored (fixtures plant real-looking PII).
    claims_repr: list[str] = field(default_factory=list)
    followup: str | None = None
    nickname: str | None = None
    error: str | None = None
    gap_verdict: str | None = None
    gap_reasoning: str | None = None

    @property
    def status(self) -> str:
        if self.error is not None:
            return "ERROR"
        return "FAIL" if self.axes_failed else "ok"


def _fmt_ratio(num: int, den: int) -> str:
    """A zero denominator is 'n/a', NEVER 1.00 — see the summary comment in run()."""
    return f"{num / den:.2f}  ({num}/{den})" if den else "n/a   (0/0)"


def render_report(results: list[FixtureResult], *, judged: bool, vacuous: bool,
                  totals: dict[str, int], axis_failures: list[tuple[str, str]]) -> str:
    L: list[str] = []
    L.append("# Rapport claim-extraction eval — report")
    L.append("")
    L.append("> ⚠️ Contains **pre-redaction** extractor output, i.e. the fixtures' planted PII. "
             "`simulations/*/out/` is gitignored — do not commit or paste this file.")
    L.append("")
    n_err = sum(1 for r in results if r.error)
    n_fail = sum(1 for r in results if r.status == "FAIL")
    L.append(f"- fixtures: {len(results)}  ·  ok: {len(results) - n_fail - n_err}  ·  "
             f"FAIL: {n_fail}  ·  ERROR: {n_err}  ·  judged axis: `{judged}`")
    L.append(f"- facet precision: {_fmt_ratio(totals['returned_matched'], totals['returned'])}"
             f"  ·  facet recall: {_fmt_ratio(totals['expected_matched'], totals['expected'])}")
    L.append("- precision/recall come from ONE one-to-one maximum matching, so a duplicate "
             "claim is a precision miss and one claim can never satisfy two expectations.")
    if vacuous:
        L.append("- ❌ **VACUOUS RUN** — nothing was actually scored; treated as a failure.")
    L.append("")

    L.append("## Failures by axis")
    L.append("")
    if axis_failures:
        by_axis: dict[str, list[str]] = {}
        for fx_id, axis in axis_failures:
            by_axis.setdefault(axis, []).append(fx_id)
        L.append("| axis | n | fixtures |")
        L.append("|---|---|---|")
        for axis, ids in sorted(by_axis.items(), key=lambda kv: -len(kv[1])):
            L.append(f"| `{axis}` | {len(ids)} | {', '.join(ids)} |")
    else:
        L.append("_No axis failed._")
    L.append("")

    L.append("## Per-fixture detail")
    L.append("")
    for r in results:
        L.append(f"### `{r.fx_id}` — {r.status}")
        L.append(f"- input: {r.text!r}")
        if r.error:
            L.append(f"- **extractor error**: {r.error}")
        else:
            L.append(f"- returned {r.n_returned} claim(s), expected {r.n_expected}, "
                     f"matched {r.n_matched}")
            for c in r.claims_repr:
                L.append(f"  - {c}")
            L.append(f"- followup: {r.followup!r}  ·  nickname: {r.nickname!r}")
        for d in r.details:
            L.append(f"- **{d}**")
        if r.gap_verdict:
            L.append(f"- gap_quality (advisory): `{r.gap_verdict}` — {r.gap_reasoning}")
        L.append("")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def run(fixture_id: str | None, dry_run: bool, judge: bool,
        out_path: Path | None = None) -> int:
    all_fixtures = _load_fixtures()

    # Load-time validation runs over the FULL set, not the --id selection: a broken sibling
    # fixture is a harness bug whichever one you asked for.
    errors = validate_fixtures(all_fixtures)
    if errors:
        print(f"[rapport-eval] fixture validation FAIL — {len(errors)} problem(s):")
        for e in errors:
            print(f"  - {e}")
        return 2
    print(f"[rapport-eval] fixture validation PASS — {len(all_fixtures)} fixture(s) checked")

    fixtures = all_fixtures
    if fixture_id:
        fixtures = [f for f in fixtures if f["id"] == fixture_id]
        if not fixtures:
            raise SystemExit(f"No fixture with id {fixture_id!r}")

    print(f"[rapport-eval] {len(fixtures)} fixture(s) loaded from {FIXTURES_PATH.name}")
    if dry_run:
        for fx in fixtures:
            n_exp = len(fx.get("expect_claims", []))
            print(f"  [dry-run] {fx['id']}: input={fx['input']!r} expect_claims={n_exp}")
        print("\n[rapport-eval] dry-run PASS — fixtures valid, no API calls made")
        return 0

    judge_client = None
    if judge:
        # Constructing the client must not abort the mechanical run either (missing package,
        # bad key). A None client makes every judge call return "unscored", which is honest.
        try:
            from openai import OpenAI

            judge_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] judge client unavailable ({type(e).__name__}: {e}) — "
                  f"gap quality will report unscored")

    totals = {"returned": 0, "returned_matched": 0, "expected": 0, "expected_matched": 0}
    anonymization_fails: list[tuple[str, list[str]]] = []
    kids_count_fails: list[str] = []
    followup_fails: list[str] = []
    repeat_fails: list[str] = []      # followup echoed a question already asked
    no_claims_fails: list[str] = []
    precision_fails: list[str] = []   # returned claims matching no expectation (false positives)
    recall_fails: list[str] = []      # expectations no returned claim satisfied (misses)
    error_fails: list[str] = []       # extractor blew up on this fixture
    gap_quality_results: list[tuple[str, str, str]] = []
    axis_failures: list[tuple[str, str]] = []   # (fixture_id, axis) — drives the summary + report
    results: list[FixtureResult] = []
    n_scored_fixtures = 0             # fixtures that produced a real comparison (non-vacuity)

    for fx in fixtures:
        fx_id = fx["id"]
        res = FixtureResult(fx_id=fx_id, text=str(fx["input"]))
        results.append(res)

        # One flaky API call must not abort the other 26 fixtures with no summary at all.
        # Mirrors policy_eval/run_eval.py: a backend failure is recorded, counted as a HARD
        # failure, and the run continues — an error is never a silent PASS.
        try:
            raw = incremental_claims_from_utterance(
                fx["input"], fx.get("existing_labels"), fx.get("recent_questions")
            )
            # NB: no assertion that `raw` is a non-empty dict — an empty {} is a legitimate
            # "no identity content" result (rapport_008/015/016/017/022 expect exactly that).
            nickname, claims, kids_count, followup = parse_incremental_claims_data(raw)
            cleaned = clean_claims_for_persist(list(claims))
        except Exception as e:  # noqa: BLE001
            res.error = f"{type(e).__name__}: {e}"
            error_fails.append(f"{fx_id} ({res.error})")
            axis_failures.append((fx_id, "extractor_error"))
            res.details.append(f"extractor_error: {res.error}")
            print(f"  [ERROR] {fx_id}: extractor call failed — {res.error}")
            continue

        expect_no_claims = bool(fx.get("expect_no_claims", False))
        expected = fx.get("expect_claims", [])
        fixture_failed = False
        n_scored_fixtures += 1

        res.n_expected = len(expected)
        res.n_returned = len(claims)
        res.followup = followup
        res.nickname = nickname
        res.claims_repr = [
            f"`{c.bucket}` / concept={c.concept!r} label={c.label!r} conf={c.confidence} "
            f"disclosure={c.disclosure} transient={c.transient} vague={c.vague}"
            for c in claims
        ]

        # FACET axes are scored on the RAW extractor output, NOT on `cleaned`. redact_pii runs
        # downstream and rewrites label/source_quote/synonyms, so scoring facets post-redaction
        # lets the backstop MASK an extraction error (a claim whose label was wrong can be
        # redacted into something that no longer looks wrong). Only the ANONYMIZATION axis is
        # scored post-redaction, because that axis is specifically about the backstop's output.
        if expect_no_claims:
            if claims:
                no_claims_fails.append(fx_id)
                axis_failures.append((fx_id, "expect_no_claims"))
                res.details.append(f"expect_no_claims: got {len(claims)} claim(s)")
                fixture_failed = True
            totals["returned"] += len(claims)
            # every returned claim here is a false positive by construction; it lands in the
            # precision denominator and is reported once, under expect_no_claims.
        else:
            totals["returned"] += len(claims)
            totals["expected"] += len(expected)
            n_matched, unmatched_claims = _match_claims(list(claims), expected)
            res.n_matched = n_matched
            # ONE assignment feeds BOTH ratios (see _match_claims).
            totals["returned_matched"] += n_matched
            totals["expected_matched"] += n_matched

            for ci in unmatched_claims:
                c = claims[ci]
                # PRECISION must gate too. Before, only the recall loop set fixture_failed, so a
                # claim the extractor invented (or duplicated) was counted in the precision ratio
                # but never failed the fixture — the axis was reported, not enforced.
                precision_fails.append(f"{fx_id} (unexpected claim: {c.concept!r}/{c.label!r})")
                axis_failures.append((fx_id, "precision"))
                res.details.append(f"precision: unmatched claim {c.concept!r}/{c.label!r}")
                fixture_failed = True
            if n_matched < len(expected):
                # Which expectations went unserved is not recoverable from the count alone, so
                # report (a) every expectation NO returned claim could satisfy, and (b) any
                # remaining shortfall, which under one-to-one matching means contention — two
                # expectations that both had candidates but only one distinct claim between
                # them (i.e. the extractor duplicated a thread). Without (b) that case would
                # fail the fixture with no printed reason.
                unserved = [exp for exp in expected
                            if not any(_claim_matches(c, exp) for c in claims)]
                for exp in unserved:
                    tag = exp.get("label_has") or exp.get("label_has_any")
                    recall_fails.append(f"{fx_id} (missed: {exp.get('bucket')}/{tag!r})")
                    res.details.append(f"recall: no claim matched {exp.get('bucket')}/{tag!r}")
                if len(unserved) < len(expected) - n_matched:
                    recall_fails.append(
                        f"{fx_id} (only {n_matched}/{len(expected)} expectation(s) could be "
                        f"assigned a DISTINCT claim — contended/duplicate matches)")
                    res.details.append(
                        f"recall: only {n_matched}/{len(expected)} expectation(s) could be "
                        f"assigned a distinct claim (contended/duplicate matches)")
                axis_failures.append((fx_id, "recall"))
                fixture_failed = True

        if "expect_kids_count" in fx and kids_count != fx["expect_kids_count"]:
            kids_count_fails.append(f"{fx_id} (got {kids_count}, expected {fx['expect_kids_count']})")
            axis_failures.append((fx_id, "kids_count"))
            res.details.append(f"kids_count: got {kids_count}, expected {fx['expect_kids_count']}")
            fixture_failed = True

        if "expect_followup" in fx:
            got_followup = followup is not None
            if got_followup != bool(fx["expect_followup"]):
                followup_fails.append(f"{fx_id} (got followup={got_followup}, expected {fx['expect_followup']})")
                axis_failures.append((fx_id, "followup"))
                res.details.append(f"followup: got {got_followup}, expected {fx['expect_followup']}")
                fixture_failed = True

        # recent_questions axis. The prompt tells the extractor its followup must not repeat or
        # near-duplicate anything already asked (_recent_questions_block in vertex_extract.py).
        # "Near-duplicate" needs judgment, so the ONLY thing asserted mechanically is an exact
        # repeat modulo case/punctuation — known by construction, zero false-positive surface.
        recent_questions = [str(q).strip() for q in (fx.get("recent_questions") or []) if str(q).strip()]
        if recent_questions and followup:
            fu_norm = _normalize(followup)
            for q in recent_questions:
                if fu_norm and fu_norm == _normalize(q):
                    repeat_fails.append(f"{fx_id} (followup repeats recent question: {q!r})")
                    axis_failures.append((fx_id, "followup_repeat"))
                    res.details.append(f"followup_repeat: {followup!r} repeats {q!r}")
                    fixture_failed = True
                    break

        must_not_contain = fx.get("must_not_contain", [])
        if must_not_contain:
            leaks = _check_anonymization(cleaned, followup, must_not_contain, nickname)
            if leaks:
                anonymization_fails.append((fx_id, leaks))
                axis_failures.append((fx_id, "anonymization"))
                res.details.append(f"anonymization: leaked {leaks}")
                fixture_failed = True

        res.axes_failed = [a for i, a in axis_failures if i == fx_id]
        status = "FAIL" if fixture_failed else "ok"
        print(f"  [{status}] {fx_id}: {len(claims)} claim(s) returned "
              f"({len(cleaned)} after redaction/dedupe), {len(expected)} expected")

        if judge and followup:
            verdict, reasoning = _judge_gap_quality(followup, judge_client)
            gap_quality_results.append((fx_id, verdict, reasoning))
            res.gap_verdict, res.gap_reasoning = verdict, reasoning
            print(f"       gap_quality={verdict} — {reasoning}")

    # RATIOS: a zero denominator reports "n/a", never a perfect 1.00. A run where the extractor
    # returned NOTHING used to print "facet precision: 1.00" — the most convincing possible green
    # for the most complete possible failure.
    precision_s = _fmt_ratio(totals["returned_matched"], totals["returned"])
    recall_s = _fmt_ratio(totals["expected_matched"], totals["expected"])
    n_pii_fixtures = sum(1 for f in fixtures if f.get("must_not_contain"))
    anonymization_s = (f"{1.0 - len(anonymization_fails) / n_pii_fixtures:.2f}"
                       if n_pii_fixtures else "n/a")

    # NON-VACUITY: "nothing was scored" must not read as "everything passed". A selection made
    # purely of expect_no_claims fixtures legitimately has both denominators at 0 but still
    # exercised the no_claims axis, so it counts as scored; a run where every extractor call
    # errored (or that scored nothing at all) does not.
    vacuous = n_scored_fixtures == 0

    print("\n[rapport-eval] summary")
    print(f"  facet precision:        {precision_s}")
    print(f"  facet recall:           {recall_s}")
    print(f"  anonymization pass rate:{anonymization_s}  ({len(anonymization_fails)} leak case(s))")
    print(f"  extractor errors:       {len(error_fails)}  {error_fails[:5]}")
    print(f"  expect_no_claims fails: {len(no_claims_fails)}  {no_claims_fails}")
    print(f"  kids_count fails:       {len(kids_count_fails)}  {kids_count_fails}")
    print(f"  followup fails:         {len(followup_fails)}  {followup_fails}")
    print(f"  followup repeat fails:  {len(repeat_fails)}  {repeat_fails}")
    print(f"  precision fails:        {len(precision_fails)}  {precision_fails[:5]}")
    print(f"  recall fails:           {len(recall_fails)}  {recall_fails[:5]}")

    if axis_failures:
        by_axis: dict[str, list[str]] = {}
        for fx_id, axis in axis_failures:
            by_axis.setdefault(axis, []).append(fx_id)
        print("\n[rapport-eval] failures by axis")
        for axis, ids in sorted(by_axis.items(), key=lambda kv: -len(kv[1])):
            print(f"  {axis:<18} {len(ids):>3}  {', '.join(sorted(set(ids)))}")

    if anonymization_fails:
        print("\n[rapport-eval] ANONYMIZATION LEAKS (hard fail)")
        for fx_id, leaks in anonymization_fails:
            print(f"  {fx_id}: leaked {leaks}")

    if judge and gap_quality_results:
        # Unscored is EXCLUDED from the denominator — folding it in either direction would
        # silently move the ratio for a reason that has nothing to do with question quality.
        scored = [(i, v, r) for i, v, r in gap_quality_results if v != _UNSCORED]
        n_unscored = len(gap_quality_results) - len(scored)
        just_right = sum(1 for _, v, _ in scored if v == "just-right")
        ratio = f"{just_right}/{len(scored)}" if scored else "n/a (0 scored)"
        print(f"\n[rapport-eval] gap quality: {ratio} just-right"
              f" · {n_unscored} unscored (advisory axis — does NOT affect exit code)")

    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        report = render_report(results, judged=judge, vacuous=vacuous, totals=totals,
                               axis_failures=axis_failures)
        out_path.write_text(report, encoding="utf-8")
        print(f"\n[rapport-eval] report -> {out_path}")

    # EXIT CODE: previously run() returned None and __main__ never called sys.exit, so every
    # failure path — anonymization leaks included — exited 0. A harness that cannot fail cannot
    # gate anything. Anonymization is a HARD fail (PII leak); the rest fail the run too. The
    # gap-quality judge is deliberately absent from this sum: it is advisory.
    failed = (len(anonymization_fails) + len(no_claims_fails) + len(kids_count_fails)
              + len(followup_fails) + len(repeat_fails) + len(precision_fails)
              + len(recall_fails) + len(error_fails))
    if vacuous:
        print("\n[rapport-eval] FAILED — VACUOUS RUN: no fixture was scored "
              "(every extractor call failed, or nothing was selected)")
        return 1
    if failed:
        print(f"\n[rapport-eval] FAILED — {failed} failing axis case(s)"
              + (" INCLUDING ANONYMIZATION LEAK(S)" if anonymization_fails else "")
              + (f" INCLUDING {len(error_fails)} EXTRACTOR ERROR(S)" if error_fails else ""))
        return 1
    print("\n[rapport-eval] all axes passed")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", dest="fixture_id", help="Run only this fixture id")
    parser.add_argument("--dry-run", action="store_true", help="Validate fixtures, no API calls")
    parser.add_argument("--judge", action="store_true", help="Also run the gap-quality LLM judge")
    parser.add_argument("--out", default=str(OUT_DIR / "report.md"),
                        help="Report artifact path (gitignored — contains pre-redaction PII)")
    args = parser.parse_args()
    raise SystemExit(run(args.fixture_id, args.dry_run, args.judge, Path(args.out)))
