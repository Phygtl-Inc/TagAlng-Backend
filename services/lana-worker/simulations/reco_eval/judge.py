"""
judge.py — the JUDGED axes of Arm A.

Only three questions go to a model, and each is here because its ground truth genuinely needs
judgment (CLAUDE.md: don't make something mechanical if its ground truth needs judgment; don't
burn a judge call on something checkable):

  filterable        §6: "at least three questions a neighbour would later FILTER or SEARCH on".
                    Whether a neighbour would filter on "how long's the line at lunch?" is not
                    derivable from the string. The structural half — did the set offer anything
                    TAPPABLE at all — is mechanical and lives in checks.check_tappable.
  lazy_vs_good      §6's lazy-vs-good table. The pattern it describes ("answerable in a few
                    words, differs from one subject to the next, and could only be answered by
                    someone who actually went") is a judgment about the world, not about text.
                    The one slice that IS mechanical — a question Google's listing answers —
                    is checks.check_google_answerable, and this axis is told to ignore it so
                    the two do not double-count the same defect.
  subject_tailored  §2: "Lana writes a fresh set of questions for every single recommendation.
                    Not per category — per SUBJECT." A set can be well-formed, unbanned and
                    filterable and still be boilerplate for the type.

JUDGE MODEL IS INDEPENDENT of whatever model Lana runs (gpt-4o), so the judge never grades its
own family by construction — the same rule the rest of the suite follows.

UNSCORED IS PART OF THE VOCABULARY. A judge that errors, times out or returns something
unparseable is surfaced, never folded into a pass. It fails `--gate`.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from ports import GeneratedSet, QuestionFixture

JUDGE_MODEL = os.environ.get("RECO_JUDGE_MODEL", "gpt-4o")

_AXES = ("filterable", "lazy_vs_good", "subject_tailored")

_SYSTEM = """You are grading ONE set of questions a neighbourhood app wrote to capture ONE
recommendation. A neighbour is vouching for something; the app asks them these questions, and
the answers become a card OTHER neighbours read and filter.

The product's own rules, which you are grading against:

- The whole point is what a listing CANNOT tell you. A neighbour's recommendation is valuable
  because it carries what only someone who went knows: how long the wait really is, whether
  the staff are patient with kids, whether the quote held, whether there is shade.
- At least THREE questions must be ones a reader would later filter or search on — the facts
  that decide whether this recommendation is for THEM. (which ages · walk-ins · insurance ·
  parking · kids' menu · does it fit a Civic trunk · emergency call-outs)
- Questions must be written for THIS SPECIFIC SUBJECT, not for its category. A specific
  pediatric dentist and a specific taco truck get different questions; so do two different
  taco trucks.
- Example answers under each question must be examples for THIS subject ("about 30 minutes"),
  not generic ("e.g. duration").
- Lazy questions read beautifully and tell a reader nothing: "Is the food good?", "Is she a
  good doctor?", "Are they reliable?", "Is it nice?", "Is it worth it?".

Grade THREE axes independently. For each: verdict "PASS", "SOFT_FAIL" or "HARD_FAIL", a score
0.0-1.0, one sentence of reasoning, and the `field` keys of any offending questions.

  filterable        Count the questions a reader would genuinely filter or search on and list
                    them. 3+ -> PASS. 1-2 -> SOFT_FAIL. 0 -> HARD_FAIL.
  lazy_vs_good      Are the questions answerable only by someone who actually went/used it?
                    IGNORE questions that a public listing would answer (hours, phone, address,
                    website, price band) — those are scored elsewhere and must not be counted
                    here. Judge only the "is it nice?" failure: vague, unanswerable-in-a-few-
                    words, or identical in shape for any subject.
  subject_tailored  Is this set about THIS subject, or could it be pasted onto any other
                    recommendation of the same type? Include the example answers in the
                    judgment.

Be a fair grader, not a harsh one. A set that is merely ORDINARY is a PASS. Reserve HARD_FAIL
for a set that would produce a card a reader cannot use.

Output only valid JSON:
{"axes":[{"axis":"filterable","verdict":"PASS","score":0.8,"reasoning":"...",
"offending_fields":["..."]}, ...]}"""


@dataclass
class AxisScore:
    axis: str
    verdict: str = "UNSCORED"
    score: float = 0.0
    reasoning: str = ""
    offending_fields: list[str] = field(default_factory=list)
    disagreement: bool = False


@dataclass
class JudgeResult:
    axes: list[AxisScore] = field(default_factory=list)
    error: str | None = None

    @property
    def any_hard_fail(self) -> bool:
        return any(a.verdict == "HARD_FAIL" for a in self.axes)

    @property
    def any_unscored(self) -> bool:
        return any(a.verdict == "UNSCORED" for a in self.axes)

    @property
    def any_disagreement(self) -> bool:
        return any(a.disagreement for a in self.axes)


def _payload(gen: GeneratedSet, fx: QuestionFixture) -> str:
    return json.dumps(
        {
            "what_the_neighbour_said": fx.opening_line,
            "kind_of_recommendation": gen.reco_type,
            "subject_is_listed_publicly": fx.lookup_able,
            "the_questions": [
                {
                    "field": s.get("field"),
                    "question": s.get("question"),
                    "example_answer": s.get("placeholder") or None,
                    "tappable_options": s.get("options") or None,
                    "must_have": bool(s.get("required")),
                }
                for s in gen.middle()
            ],
        },
        ensure_ascii=False,
        indent=1,
    )


def _one_pass(payload: str, client: Any) -> list[AxisScore] | str:
    try:
        resp = client.chat.completions.create(
            model=JUDGE_MODEL,
            messages=[{"role": "system", "content": _SYSTEM},
                      {"role": "user", "content": payload}],
            response_format={"type": "json_object"},
            temperature=0.0,
            max_tokens=700,
        )
        data = json.loads(resp.choices[0].message.content or "{}")
    except Exception as exc:  # noqa: BLE001
        return f"judge call failed: {exc}"
    rows = data.get("axes")
    if not isinstance(rows, list):
        return f"judge returned no axes: {str(data)[:160]}"
    out: list[AxisScore] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        axis = str(row.get("axis") or "")
        if axis not in _AXES:
            continue
        verdict = str(row.get("verdict") or "").upper()
        if verdict not in ("PASS", "SOFT_FAIL", "HARD_FAIL"):
            verdict = "UNSCORED"
        try:
            score = round(float(row.get("score") or 0.0), 3)
        except (TypeError, ValueError):
            score = 0.0
        out.append(AxisScore(
            axis=axis, verdict=verdict, score=score,
            reasoning=str(row.get("reasoning") or "")[:400],
            offending_fields=[str(f) for f in (row.get("offending_fields") or [])][:8],
        ))
    return out


def score_set(
    gen: GeneratedSet, fx: QuestionFixture, *, n_judges: int = 1, client: Any = None
) -> JudgeResult:
    """Judge one generated set. Every axis the judge dropped comes back UNSCORED — an axis
    nobody measured must never read as one that passed."""
    if gen.error or not gen.steps:
        return JudgeResult(error="nothing to judge — the backend returned no set")
    if client is None:
        from openai import OpenAI

        key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not key:
            return JudgeResult(error="--judge needs OPENAI_API_KEY")
        client = OpenAI(api_key=key)

    payload = _payload(gen, fx)
    passes: list[list[AxisScore]] = []
    errors: list[str] = []
    for _ in range(max(1, n_judges)):
        got = _one_pass(payload, client)
        if isinstance(got, str):
            errors.append(got)
        else:
            passes.append(got)
    if not passes:
        return JudgeResult(
            axes=[AxisScore(axis=a, verdict="UNSCORED", reasoning=errors[0] if errors else "")
                  for a in _AXES],
            error="; ".join(errors) or "judge produced nothing",
        )

    axes: list[AxisScore] = []
    for axis in _AXES:
        rows = [r for p in passes for r in p if r.axis == axis]
        if not rows:
            axes.append(AxisScore(axis=axis, verdict="UNSCORED",
                                  reasoning="judge omitted this axis"))
            continue
        verdicts = [r.verdict for r in rows]
        # Plurality. A tie across stances is a REVIEW, not a coin flip — the same triage
        # signal policy_eval uses: route it to a human rather than inventing a majority.
        top = max(set(verdicts), key=verdicts.count)
        tied = sum(1 for v in set(verdicts) if verdicts.count(v) == verdicts.count(top)) > 1
        axes.append(AxisScore(
            axis=axis,
            verdict="REVIEW" if (tied and len(rows) > 1) else top,
            score=round(sum(r.score for r in rows) / len(rows), 3),
            reasoning=rows[0].reasoning,
            offending_fields=sorted({f for r in rows for f in r.offending_fields}),
            disagreement=len(set(verdicts)) > 1,
        ))
    return JudgeResult(axes=axes)
