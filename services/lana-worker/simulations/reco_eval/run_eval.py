"""
run_eval.py — entry point for the recommendation-quality eval harness.

    cd services/lana-worker/simulations/reco_eval
    python run_eval.py --dry-run                     # no key, no server — smoke-test everything
    python run_eval.py --arm answers                 # THE HEADLINE RUN: no key needed, no server.
                                                     #   what the shipped flow accepts today
    python run_eval.py --arm answers --validator reference   # ...and what a §12.3 validator would
    python run_eval.py --arm questions --backend inproc      # the REAL generator (OPENAI_API_KEY)
    python run_eval.py --arm questions --backend inproc --judge
    python run_eval.py --arm questions --backend static      # the negative control / fallback cost
    python run_eval.py --backend live --arm questions        # the whole shipped flow over HTTP
    python run_eval.py --id q06_barnes_noble         # one fixture

Writes out/report.md and prints a summary. Mechanical axes always run; the judged axes of arm A
run only with --judge. --gate exits 1 on any HARD_FAIL or UNSCORED (fail closed).

WHAT THIS MEASURES, IN ONE LINE EACH
  arm A (questions)  the set Lana writes for a subject — §§2, 5, 6, 9 of the capture spec
  arm B (answers)    what the flow accepts into a card — §11, the check the product never built
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

# Fixtures include Spanish input and the report echoes generated questions verbatim, so the
# default Windows console codepage can kill a run mid-suite on a print(). Same guard as
# policy_eval/run_eval.py and rapport/run_eval.py.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))                    # bare intra-package imports
sys.path.insert(0, str(_HERE.parents[0]))         # simulations/ -> `import provenance`
sys.path.insert(0, str(_HERE.parents[1]))         # services/lana-worker -> `import app.*`
load_dotenv(_HERE.parents[3] / ".env.local", override=True)

import checks  # noqa: E402
import provenance as _prov_mod  # noqa: E402
import judge as judge_mod  # noqa: E402
from backend import get_question_backend, get_validator  # noqa: E402
from ports import (  # noqa: E402
    AnswerDecision,
    AnswerFixture,
    CheckResult,
    GeneratedSet,
    QuestionFixture,
    worst,
)

FIXTURES_PATH = _HERE / "fixtures.yaml"
OUT_DIR = _HERE / "out"

_VALID_TYPES = {"professional", "restaurant", "recipe", "product", "location", "service", "diy"}
_VALID_KINDS = {"text", "choice", "place", "toggle", "agree"}


# ---------------------------------------------------------------------------
# Fixture loading + validation (the real work behind --dry-run)
# ---------------------------------------------------------------------------

def validate_fixtures(raw: dict) -> list[str]:
    """Load-time shape validation. Returns human-readable errors; empty means valid.

    Every rule here is one the harness would otherwise hit as a KeyError, a silently inert
    matcher, or a self-contradicting expectation. Rules are deliberately narrow: a validator
    that cries wolf gets disabled, and then it validates nothing.
    """
    errs: list[str] = []
    seen: set[str] = set()

    for i, q in enumerate(raw.get("questions") or []):
        tag = f"questions[{i}] id={q.get('id')!r}"
        qid = str(q.get("id") or "")
        if not qid:
            errs.append(f"{tag}: missing id")
        elif qid in seen:
            errs.append(f"{tag}: duplicate id")
        seen.add(qid)
        if not str(q.get("opening_line") or "").strip():
            errs.append(f"{tag}: empty opening_line — there is nothing to generate a set from")
        rt = q.get("expect_reco_type")
        if rt is not None and rt not in _VALID_TYPES:
            errs.append(f"{tag}: expect_reco_type={rt!r} is not one of the seven the DB accepts")
        if q.get("lookup_able") and rt is None:
            # google_answerable exempts the type's own floor fields; with no type it cannot
            # tell a banned question from a required one and would fire on `contact`.
            errs.append(f"{tag}: lookup_able is set but expect_reco_type is not — "
                        f"check_google_answerable needs the type to know the floor exemption")
        pd = q.get("prior_draft") or {}
        if "step_set" in pd:
            errs.append(f"{tag}: prior_draft carries `step_set`, which sets want_steps=False "
                        f"(tip_share.py:130) — no set would ever be generated")
        if pd.get("reco_type") and q.get("expect_generated", True):
            errs.append(f"{tag}: prior_draft pins reco_type, which suppresses generation "
                        f"(tip_share.py:78), but expect_generated is not false")
        for j, fact in enumerate(list(q.get("stated_facts") or []) + list(q.get("forbidden_topics") or [])):
            if not [x for x in (fact.get("question_any_of") or []) if str(x).strip()]:
                errs.append(f"{tag}: fact[{j}] has no question_any_of — an inert matcher that "
                            f"would score a vacuous pass")

    for i, a in enumerate(raw.get("answers") or []):
        tag = f"answers[{i}] id={a.get('id')!r}"
        aid = str(a.get("id") or "")
        if not aid:
            errs.append(f"{tag}: missing id")
        elif aid in seen:
            errs.append(f"{tag}: duplicate id")
        seen.add(aid)
        if a.get("verdict") not in ("accept", "reject"):
            errs.append(f"{tag}: verdict={a.get('verdict')!r}, expected 'accept' or 'reject'")
        if a.get("reco_type") not in _VALID_TYPES:
            errs.append(f"{tag}: reco_type={a.get('reco_type')!r} is not one of the seven")
        if a.get("kind") not in _VALID_KINDS:
            errs.append(f"{tag}: kind={a.get('kind')!r} is not a RecoStep kind")
        if not str(a.get("field") or "").strip():
            errs.append(f"{tag}: missing field")
        if not str(a.get("question") or "").strip():
            errs.append(f"{tag}: missing question — a validator has to know what was asked")
        if not str(a.get("reason") or "").strip():
            errs.append(f"{tag}: no reason — a hand-label nobody can audit is not ground truth")

    labels = [a.get("verdict") for a in (raw.get("answers") or [])]
    if labels and ("accept" not in labels or "reject" not in labels):
        errs.append("answers: the file has only one label. Without both, 'accept everything' "
                    "or 'reject everything' scores perfectly and the metric is meaningless.")
    return errs


def load_fixtures() -> tuple[list[QuestionFixture], list[AnswerFixture], dict]:
    with open(FIXTURES_PATH, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    qs = [
        QuestionFixture(
            id=str(q["id"]),
            opening_line=str(q["opening_line"]),
            prior_draft=dict(q.get("prior_draft") or {}),
            expect_reco_type=q.get("expect_reco_type"),
            lookup_able=bool(q.get("lookup_able")),
            stated_facts=list(q.get("stated_facts") or []),
            forbidden_topics=list(q.get("forbidden_topics") or []),
            lang=q.get("lang"),
            expect_generated=bool(q.get("expect_generated", True)),
            probes=str(q.get("probes") or "").strip(),
        )
        for q in (raw.get("questions") or [])
    ]
    ans = [
        AnswerFixture(
            id=str(a["id"]),
            reco_type=str(a["reco_type"]),
            field_name=str(a["field"]),
            question=str(a["question"]),
            kind=str(a.get("kind") or "text"),  # type: ignore[arg-type]
            required=bool(a.get("required")),
            answer=str(a.get("answer") or ""),
            verdict=str(a["verdict"]),  # type: ignore[arg-type]
            reason=str(a.get("reason") or "").strip(),
        )
        for a in (raw.get("answers") or [])
    ]
    return qs, ans, raw


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

@dataclass
class QRecord:
    fx: QuestionFixture
    gen: GeneratedSet
    mechanical: list[CheckResult]
    judged: judge_mod.JudgeResult | None = None
    trial: int = 0

    @property
    def verdict(self) -> str:
        return worst(self.mechanical) if self.mechanical else "UNSCORED"


@dataclass
class ARecord:
    fx: AnswerFixture
    decision: AnswerDecision
    mechanical: list[CheckResult] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        return worst(self.mechanical) if self.mechanical else "UNSCORED"


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def run_questions(fixtures: list[QuestionFixture], *, backend_kind: str, do_judge: bool,
                  n_judges: int, trials: int = 1) -> list[QRecord]:
    """Generate each fixture's set `trials` times.

    ONE TRIAL PER FIXTURE MEASURES SAMPLING NOISE, NOT LANA. The generator is severely
    nondeterministic: seven identical calls (same opener, prev={}, temperature 0.2, model
    pinned, LANA_LLM_FALLBACK=0) returned `steps_raw` of 6, 6, 6, 3 and EMPTY three times. An
    empty proposal sends validate_steps down `if not middle:` (reco_question_sets.py:338-344)
    and ships the type's STATIC set — so a single-trial run stands a large chance of scoring
    reco_question_sets.py's hand-written tables as if Lana had authored them, in either
    direction. Every Arm A number is therefore a RATE over trials, never a verdict.

    The judge runs on the FIRST successfully GENERATED trial per fixture only. Judging every
    trial would multiply the cost by `trials` to re-answer a question about the set, not about
    the sampling — and judging a fallback trial would grade reco_question_sets.py.
    """
    backend = get_question_backend(backend_kind)
    client = None
    if do_judge:
        from openai import OpenAI
        key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not key:
            raise RuntimeError("--judge needs OPENAI_API_KEY")
        client = OpenAI(api_key=key)

    out: list[QRecord] = []
    for fx in fixtures:
        judged_once = False
        for trial in range(max(1, trials)):
            suffix = f"  (trial {trial + 1}/{trials})" if trials > 1 else ""
            print("")
            print(f"[reco-eval:A] {fx.id}{suffix}")
            gen = backend.generate(fx)
            for f in gen.flags:
                print(f"  [flag] {f}")
            if gen.error:
                print(f"  [error] {gen.error}")
            else:
                print(f"  type={gen.reco_type} generated={gen.generated} "
                      f"steps={len(gen.middle())}(+{len(gen.tail())} tail) "
                      f"prefilled={sorted(gen.prefilled)}")
                for i, st in enumerate(gen.middle()):
                    opts = f" [{'/'.join(st.get('options') or [])}]" if st.get("options") else ""
                    print(f"    {i + 1}. {st.get('question')}{opts}")
            mech = checks.run_question_checks(gen, fx)
            for r in mech:
                print(f"  [mech] {r.name}: {r.verdict}"
                      + ("" if r.verdict == "PASS" else f"  <-- {r.detail}"))
            judged = None
            if do_judge and not judged_once and not gen.error and gen.steps and gen.generated:
                judged = judge_mod.score_set(gen, fx, n_judges=n_judges, client=client)
                judged_once = True
                for a in judged.axes:
                    print(f"  [judge] {a.axis}: {a.verdict} ({a.score})"
                          + (f"  <-- {a.reasoning}" if a.verdict != "PASS" else ""))
            out.append(QRecord(fx=fx, gen=gen, mechanical=mech, judged=judged, trial=trial))
    return out


def run_provenance() -> dict[str, str]:
    """What actually produced these numbers — provider, model, fallback state.

    WHY THIS EXISTS. Pouya lost days tuning prompts against gpt-4o-mini before finding
    production ran 4.1-mini; the scores were fine on the right model. Nothing surfaced which
    path was running. This harness had the same hole: the model under test was named only in a
    docstring, so a reader of out/report.md could not tell what the 45% fallback rate described.

    A number without the model that produced it is not a measurement, it is an anecdote. Every
    report now carries this header, and any later run can be compared to it honestly.
    """
    prov: dict[str, str] = {
        "provider": os.environ.get("LANA_LLM_PROVIDER", "(unset)"),
        "fallback": os.environ.get("LANA_LLM_FALLBACK", "(unset — cross-provider retry POSSIBLE)"),
        "judge_model": os.environ.get("RECO_JUDGE_MODEL", "gpt-4o"),
        "validator_model": os.environ.get("RECO_VALIDATOR_MODEL", "gpt-4o"),
        # WHICH CREDENTIAL, not just which model. The model answers "what produced this"; the key
        # answers "whose account, and was it even alive". On 2026-09-15/16 a personal key exported
        # in a shell shadowed the repo key from .env.local, was out of credits, and every call
        # 429'd — but `extract_entities_from_message` swallows both its OpenAI and its Vertex
        # failure, so the reports read as "the extractor found nothing" for a day.
        # Last four characters only: enough to tell two keys apart, not enough to be a secret.
        "api_key": _prov_mod.key_fingerprint(),
    }
    try:
        from app.orchestrator.llm import synthesizer_model

        prov["generator_model"] = synthesizer_model()
    except Exception as exc:  # noqa: BLE001 — dry/static need no LLM config at all
        prov["generator_model"] = f"(not resolvable: {exc})"
    return prov


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a proportion.

    Wilson and not the normal approximation: at n=30 with p near 0.4 the normal interval is
    both too narrow and can run past 0 or 1. This is the interval on the headline Arm A
    number, so it decides whether a reader is entitled to quote a point.

    Why it is here at all: two runs of 30 trials returned 13/30 and 11/30 for the same
    quantity — 43% and 37%. Nothing changed between them but sampling. Printing a bare "43%"
    invites someone to repeat it as a measured constant, and then to read the next run's 37%
    as an improvement. It is not; it is noise.
    """
    if n <= 0:
        return (0.0, 0.0)
    p = k / n
    den = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / den
    return (max(0.0, centre - half), min(1.0, centre + half))


def question_metrics(records: list[QRecord]) -> dict[str, object]:
    """Rates across every trial. The fallback rate is the headline Arm A number."""
    ok = [r for r in records if not r.gen.error]
    # TWO DIFFERENT QUESTIONS, TWO DIFFERENT DENOMINATORS, both reported.
    #
    #  unintended fallback  — of the trials where a written set was EXPECTED, how often did the
    #                         generator fail to produce one? Excludes q11, which deliberately
    #                         pins the type-before-name hazard and is expected to fall back;
    #                         counting a pinned fallback as a generator failure double-counts a
    #                         behaviour we already know about.
    #  static delivered     — of ALL trials, how often did a NEIGHBOUR receive the static set?
    #                         Includes q11, because its trials ship the static questionnaire to
    #                         a real person exactly like any other fallback. This is the number
    #                         the sentence "N trials shipped the static set" is actually about.
    expected = [r for r in ok if r.fx.expect_generated]
    fallback = [r for r in expected if not r.gen.generated]
    delivered = [r for r in ok if not r.gen.generated]
    # UNSCORED IS NOT A FAILURE AND MUST NOT BE TALLIED AS ONE. ports.worst() ranks it above
    # SOFT_FAIL so it cannot carry a green gate, but that is about GATING. In a report it means
    # "we could not measure this", explicitly not a finding against Lana — and merging the two
    # under a heading that says "failures" is how an eval manufactures a finding. It showed:
    # on `--backend static`, `stated_facts` read as "9 of 11 failures" when all 9 were the
    # harness declining to score an axis that backend cannot measure.
    axis: dict[str, int] = {}
    unscored: dict[str, int] = {}
    for r in records:
        for c in r.mechanical:
            if c.verdict in ("SOFT_FAIL", "HARD_FAIL"):
                axis[c.name] = axis.get(c.name, 0) + 1
            elif c.verdict == "UNSCORED":
                unscored[c.name] = unscored.get(c.name, 0) + 1
    return {
        "trials": len(records),
        "errors": len(records) - len(ok),
        "fallback": len(fallback),
        "fallback_of": len(expected),
        "fallback_rate": round(len(fallback) / len(expected), 3) if expected else 0.0,
        "fallback_ci": wilson_ci(len(fallback), len(expected)),
        "delivered": len(delivered),
        "delivered_of": len(ok),
        "delivered_rate": round(len(delivered) / len(ok), 3) if ok else 0.0,
        "delivered_ci": wilson_ci(len(delivered), len(ok)),
        "axis_fail": axis,
        "axis_unscored": unscored,
    }


# Axes that short-circuit to PASS on fixtures they do not apply to. Their counts below are out
# of ALL trials, so dividing gives a rate that understates how often they fire where they are
# actually in force. Named rather than silently re-based: inventing a per-axis denominator
# would need every check to declare applicability, and a wrong denominator is worse than a
# stated caveat.
_CONDITIONAL_AXES = {
    "google_answerable": "only runs on `lookup_able` fixtures",
    "language": "only runs on fixtures with a `lang`",
    "stated_facts": "only runs on fixtures with `stated_facts`, and needs the prefill probe",
    "floor_present": "needs a `reco_type` and an up-to-date floor mirror",
}


def run_answers(fixtures: list[AnswerFixture], *, validator_kind: str) -> list[ARecord]:
    # The step's own chip options, so ReferenceValidator never spends a call rejecting a tap
    # the product itself offered. Only `price` has a closed set in the fixtures today.
    options_by_field = {"price": ["Cheap eats", "Mid-range", "A treat"]}
    validator = get_validator(validator_kind, options_by_field=options_by_field)
    out: list[ARecord] = []
    for fx in fixtures:
        dec = validator.validate(fx)
        mech = checks.run_answer_checks(dec, fx)
        mark = {"PASS": "  ok", "SOFT_FAIL": "SOFT", "HARD_FAIL": "HARD", "UNSCORED": " ???"}
        v = worst(mech)
        print(f"[reco-eval:B] {mark.get(v, v)}  {fx.id:<28} "
              f"{'ACCEPT' if dec.accepted else 'REJECT':<6} (want {fx.verdict.upper()})  "
              f"{fx.answer[:38]!r}")
        out.append(ARecord(fx=fx, decision=dec, mechanical=mech))
    return out


def answer_metrics(records: list[ARecord]) -> dict[str, float | int]:
    scored = [r for r in records if not r.decision.unscorable]
    junk = [r for r in scored if r.fx.verdict == "reject"]
    good = [r for r in scored if r.fx.verdict == "accept"]
    junk_accepted = [r for r in junk if r.decision.accepted]
    good_rejected = [r for r in good if not r.decision.accepted]
    correct = len(junk) - len(junk_accepted) + len(good) - len(good_rejected)
    # SPLIT OUT THE JUNK THAT NEVER REACHES A VALIDATOR. `store_as` (the flow's own transform,
    # shared by every port) drops anything that collapses to empty, so a blank or whitespace
    # answer is refused before any validator has an opinion. Counting those in the denominator
    # flatters the shipped flow: it "detects 2 of 10" when it detects none of the junk that
    # arrives as text — the 2 are the flow dropping empties, not a check noticing anything.
    reaching = [r for r in junk if r.decision.stored is not None]
    reaching_accepted = [r for r in reaching if r.decision.accepted]
    return {
        "total": len(records),
        "unscorable": len(records) - len(scored),
        "junk": len(junk),
        "junk_accepted": len(junk_accepted),
        "junk_acceptance_rate": round(len(junk_accepted) / len(junk), 3) if junk else 0.0,
        "junk_reaching": len(reaching),
        "junk_reaching_accepted": len(reaching_accepted),
        "reaching_acceptance_rate": (round(len(reaching_accepted) / len(reaching), 3)
                                     if reaching else 0.0),
        "good": len(good),
        "good_rejected": len(good_rejected),
        "false_reject_rate": round(len(good_rejected) / len(good), 3) if good else 0.0,
        "accuracy": round(correct / len(scored), 3) if scored else 0.0,
    }


# ---------------------------------------------------------------------------
# Naive baselines — the honesty check on any high Arm B score
# ---------------------------------------------------------------------------
#
# A validator scoring 100% on 21 rows means nothing on its own: it could mean the validator is
# good, or it could mean the labels are trivially separable and a two-line heuristic would do
# just as well. These are the two-line heuristics, scored the same way, so the report can say
# which. They are deterministic, need no key and no calls, so they are always computed.
#
# The wordlist baseline is deliberately CHEATING — its words were read off this very fixture
# file, so it is tuned on its own test set and its score is an upper bound no honest wordlist
# could reach. It is here precisely because even that upper bound leaves most junk accepted:
# "hundred dollars", "I love pizza" and "the strip mall behind Publix" are not wordlist-able,
# and that is the argument that the fixture set is not gameable without understanding meaning.
_CHEAT_WORDLIST = frozenset({"idk", "ask me", "good", "stuff", "done", "n/a", "none", "nice"})

_BASELINES: tuple[tuple[str, Any], ...] = (
    ("accept everything", lambda t: True),
    ("reject everything", lambda t: False),
    ("non-blank only (= the shipped flow)", lambda t: bool(t)),
    ("reject under 5 chars", lambda t: len(t) >= 5),
    ("reject under 8 chars", lambda t: len(t) >= 8),
    ("reject under 12 chars", lambda t: len(t) >= 12),
    ("junk wordlist read off THIS file (cheating)", lambda t: bool(t) and t not in _CHEAT_WORDLIST),
)


def baseline_table(fixtures: list[AnswerFixture]) -> list[dict[str, Any]]:
    """Score each naive heuristic against the same hand-labels the real validators face."""
    out: list[dict[str, Any]] = []
    for name, accepts in _BASELINES:
        junk = [f for f in fixtures if f.verdict == "reject"]
        good = [f for f in fixtures if f.verdict == "accept"]
        ja = sum(1 for f in junk if accepts(" ".join(f.answer.split()).lower()))
        gr = sum(1 for f in good if not accepts(" ".join(f.answer.split()).lower()))
        correct = (len(junk) - ja) + (len(good) - gr)
        out.append({
            "name": name,
            "junk_accepted": ja, "junk": len(junk),
            "good_rejected": gr, "good": len(good),
            "accuracy": round(correct / len(fixtures), 3) if fixtures else 0.0,
        })
    return out


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def render_report(qrecs: list[QRecord], arecs: list[ARecord], *, backend_kind: str,
                  validator_kind: str, judged: bool, arm: str, trials: int = 1) -> str:
    L: list[str] = []
    L.append("# Recommendation-quality eval — report")
    L.append("")
    L.append(f"- arm: `{arm}`  ·  question backend: `{backend_kind}`  ·  "
             f"answer validator: `{validator_kind}`  ·  judged axes: `{judged}`  ·  "
             f"trials per fixture: {trials}")
    L.append("- Scope: Asjid Malik, *How Lana decides what to ask about a recommendation* "
             "(2026-09-07) §§2, 5, 6, 9, 11, 12.")
    # Which path actually ran. A number without the model that produced it is an anecdote.
    _p = run_provenance()
    L.append(f"- **Ran against:** provider `{_p['provider']}` · generator "
             f"`{_p['generator_model']}` · judge `{_p['judge_model']}` · validator "
             f"`{_p['validator_model']}` · key `{_p['api_key']}` · "
             f"`LANA_LLM_FALLBACK={_p['fallback']}`")
    if _p["fallback"] not in ("0", "false", "False"):
        L.append("  - ⚠️ **Cross-provider fallback is not disabled.** A retryable failure can be "
                 "silently re-served by a different provider, so the model named above may not "
                 "be the model that produced every result. Set `LANA_LLM_FALLBACK=0`.")
    _shadow = _prov_mod.shadowed_key_warning()
    if _shadow:
        L.append(f"  - ⚠️ **Wrong credential:** {_shadow}. These numbers may describe a dead or "
                 f"unrelated account rather than the project's — `unset OPENAI_API_KEY`.")
    if backend_kind == "dry":
        L.append("- **dry** — canned question sets. Proves the pipeline, never the product.")
    if backend_kind == "static":
        L.append("- **static** — the type's FALLBACK sets, on purpose. This is the negative "
                 "control: `banned_generic` and `google_answerable` MUST fire here (the static "
                 "sets contain \"What stood out for you?\" and, for `location`, an opening-hours "
                 "question). A clean run against this backend means those checks have gone "
                 "vacuous. It is also the honest answer to \"what does a neighbour get when "
                 "generation doesn't run?\"")
    if backend_kind == "inproc":
        L.append("- **inproc** — the REAL `_extract_tip_fields` + `validate_steps`, in-process. "
                 "The model's raw proposal is visible here, so a floor field the model FORGOT "
                 "and the guards repaired in is distinguishable from one it wrote. No DB, so "
                 "the `others_also_said` tally row is never exercised.")
    if backend_kind == "live":
        L.append("- **live (HTTP)** — the whole shipped flow: router, entry backstop, name and "
                 "category gates, Places, real tallies. The raw model proposal is NOT "
                 "observable (validate_steps has consumed it), and a fixture's `prior_draft` "
                 "is not honoured — the flow builds its own.")
    if validator_kind == "shipped":
        L.append("- **shipped validator** — every check a recommendation answer passes through "
                 "today and nothing else: field-is-known (`main.py:2534, 2539`), "
                 "whitespace-collapse and 280-char truncation (`main.py:2538`), required-non-blank "
                 "(`missing_required`, called for real). Nothing looks at meaning.")
    if validator_kind == "reference":
        L.append("- **reference validator** — §12.3's AI validator built to spec. NOT a proposed "
                 "patch: it exists to prove the fixtures are scoreable at all, and to give the "
                 "'Add AI Validator' action item a target it can be measured against.")
    L.append("")

    # ── ARM B first: it is the finding ──────────────────────────────────────────────────
    if arecs:
        m = answer_metrics(arecs)
        L.append("## Arm B — what the flow ACCEPTS (§11)")
        L.append("")
        L.append(f"**{m['junk_reaching_accepted']} of {m['junk_reaching']} unusable answers "
                 f"that actually reach a validator were accepted "
                 f"({m['reaching_acceptance_rate']:.0%}).**")
        L.append("")
        L.append(f"_Quote that figure, not {m['junk_accepted']}/{m['junk']}. "
                 f"{m['junk'] - m['junk_reaching']} of the {m['junk']} junk rows collapse to "
                 f"empty in `store_as` and are dropped by the FLOW before any validator has an "
                 f"opinion, so no validator in this harness can accept them. Counting them "
                 f"flatters the shipped path, which detects none of the junk that arrives as "
                 f"text._")
        L.append("")
        L.append("| metric | value | what it means |")
        L.append("|---|---|---|")
        L.append(f"| junk acceptance rate | **{m['junk_acceptance_rate']:.0%}** | "
                 f"unusable answers stored and displayed as facts |")
        L.append(f"| false reject rate | {m['false_reject_rate']:.0%} | "
                 f"good answers refused — §12.4 rules this out, so it must stay at 0 |")
        L.append(f"| accuracy | {m['accuracy']:.0%} | agreement with the hand-labels |")
        L.append(f"| unscorable | {m['unscorable']} | validator could not decide (fails `--gate`) |")
        L.append("")
        wrong = [r for r in arecs if r.verdict not in ("PASS",)]
        if wrong:
            L.append("| fixture | answer | question | wanted | got | axis |")
            L.append("|---|---|---|---|---|---|")
            for r in wrong:
                axes = ", ".join(f"`{c.name}`" for c in r.mechanical if c.verdict != "PASS")
                L.append(
                    f"| `{r.fx.id}` | {r.fx.answer[:44]!r} | {r.fx.question} | "
                    f"{r.fx.verdict} | {'accept' if r.decision.accepted else 'reject'} | {axes} |"
                )
            L.append("")
        for r in arecs:
            hard = [c for c in r.mechanical if c.verdict == "HARD_FAIL"]
            if hard:
                L.append(f"- **HARD** `{r.fx.id}`: " + "; ".join(c.detail for c in hard))
        L.append("")

        # What a two-line heuristic would score, so nobody has to take a high number on faith.
        L.append("#### Naive baselines on the same labels")
        L.append("")
        L.append("_A score is only meaningful next to what a trivial validator gets. If a "
                 "heuristic matches the real one, the labels are trivially separable and the "
                 "score says nothing about understanding meaning._")
        L.append("")
        L.append("| heuristic | junk accepted | good rejected | accuracy |")
        L.append("|---|---|---|---|")
        for b in baseline_table([r.fx for r in arecs]):
            L.append(f"| {b['name']} | {b['junk_accepted']}/{b['junk']} | "
                     f"{b['good_rejected']}/{b['good']} | {b['accuracy']:.0%} |")
        L.append("")
        L.append(f"_Measured now: **{validator_kind}** accepted {m['junk_accepted']}/{m['junk']} "
                 f"junk and rejected {m['good_rejected']}/{m['good']} good, "
                 f"{m['accuracy']:.0%} accuracy._")
        L.append("")

    # ── ARM A ───────────────────────────────────────────────────────────────────────────
    if qrecs:
        qm = question_metrics(qrecs)
        L.append("## Arm A — what Lana ASKS (§§2, 5, 6, 9)")
        L.append("")
        if trials > 1:
            lo, hi = qm["fallback_ci"]
            dlo, dhi = qm["delivered_ci"]
            L.append(f"**{qm['delivered']} of {qm['delivered_of']} trials "
                     f"({qm['delivered_rate']:.0%}, 95% CI {dlo:.0%}-{dhi:.0%}) delivered the "
                     f"type's STATIC question set to the neighbour instead of one written for "
                     f"the subject.**")
            L.append("")
            L.append(f"_Two denominators, because they answer different questions._ "
                     f"**{qm['delivered']}/{qm['delivered_of']}** counts every trial: what a "
                     f"neighbour actually receives. "
                     f"**{qm['fallback']}/{qm['fallback_of']}** "
                     f"({qm['fallback_rate']:.0%}, CI {lo:.0%}-{hi:.0%}) is the UNINTENDED "
                     f"failure rate — it drops the trials of fixtures that pin the fallback on "
                     f"purpose (`expect_generated: false`), where the static set is the "
                     f"documented behaviour rather than a generator failure.")
            L.append("")
            L.append(f"_Quote the interval, not the point. Two runs of 30 trials returned 43% "
                     f"and 37% for this same quantity with nothing changed between them but "
                     f"sampling. At n={qm['fallback_of']} the number carries roughly "
                     f"+/-{(hi - lo) / 2:.0%}; reading a movement inside that band as an "
                     f"improvement or a regression would be reading noise._")
            L.append("")
            L.append("The generator is nondeterministic: identical calls return a set of six "
                     "questions, or three, or none. An empty proposal sends `validate_steps` "
                     "down `if not middle:` (reco_question_sets.py:338-344) and the neighbour "
                     "silently gets §1's one-form-for-everything questionnaire — the thing §2 "
                     "says was replaced. Nothing anywhere records that it happened, which is "
                     "why every number below is a rate over "
                     f"{trials} trials per fixture rather than a verdict.")
            L.append("")
        L.append("| fixture | type | written for the subject | worst verdict | axes that failed |")
        L.append("|---|---|---|---|---|")
        for fid in dict.fromkeys(r.fx.id for r in qrecs):
            rs = [r for r in qrecs if r.fx.id == fid]
            gen_n = sum(1 for r in rs if r.gen.generated and not r.gen.error)
            worst_v = "PASS"
            for r in rs:
                if ["PASS", "SOFT_FAIL", "UNSCORED", "HARD_FAIL"].index(r.verdict) > \
                        ["PASS", "SOFT_FAIL", "UNSCORED", "HARD_FAIL"].index(worst_v):
                    worst_v = r.verdict
            per_axis: dict[str, int] = {}
            per_unsc: dict[str, int] = {}
            for r in rs:
                for c in r.mechanical:
                    if c.verdict in ("SOFT_FAIL", "HARD_FAIL"):
                        per_axis[c.name] = per_axis.get(c.name, 0) + 1
                    elif c.verdict == "UNSCORED":
                        per_unsc[c.name] = per_unsc.get(c.name, 0) + 1
            bits = [f"`{k}` {v}/{len(rs)}" for k, v in
                    sorted(per_axis.items(), key=lambda kv: -kv[1])]
            # Marked, not merged: "unmeasured" and "failed" are different claims about Lana.
            bits += [f"_{k} unscored {v}/{len(rs)}_" for k, v in
                     sorted(per_unsc.items(), key=lambda kv: -kv[1])]
            bad = ", ".join(bits) or "—"
            types = {r.gen.reco_type for r in rs if r.gen.reco_type}
            L.append(f"| `{fid}` | {'/'.join(sorted(types)) or '—'} | {gen_n}/{len(rs)} | "
                     f"{worst_v} | {bad} |")
        L.append("")

        axis_fail = dict(qm["axis_fail"])  # type: ignore[arg-type]
        axis_unscored = dict(qm["axis_unscored"])  # type: ignore[arg-type]
        if axis_fail:
            L.append("### Mechanical failures by axis")
            L.append("")
            L.append("_SOFT_FAIL and HARD_FAIL only. UNSCORED is listed separately below — it "
                     "means the axis could not be measured, which is not a finding against Lana._")
            L.append("")
            for name, n in sorted(axis_fail.items(), key=lambda kv: -kv[1]):
                note = f"  ·  _{_CONDITIONAL_AXES[name]}_" if name in _CONDITIONAL_AXES else ""
                L.append(f"- `{name}`: {n} of {qm['trials']} trials{note}")
            L.append("")
        if axis_unscored:
            L.append("### Axes that could not be measured")
            L.append("")
            for name, n in sorted(axis_unscored.items(), key=lambda kv: -kv[1]):
                L.append(f"- `{name}`: UNSCORED on {n} of {qm['trials']} trials")
            L.append("")

        if judged:
            agg: dict[str, dict[str, int]] = {}
            for r in qrecs:
                for a in (r.judged.axes if r.judged else []):
                    d = agg.setdefault(a.axis, {})
                    d["n"] = d.get("n", 0) + 1
                    d[a.verdict] = d.get(a.verdict, 0) + 1
            if agg:
                L.append("### Judged axes")
                L.append("")
                L.append("_UNSCORED = the judge dropped the axis (surfaced, never a silent PASS). "
                         "REVIEW = no plurality across stances → human spot-audit._")
                L.append("")
                L.append("_**The denominator differs from the mechanical axes on purpose.** The "
                         "judge runs on the FIRST GENERATED trial of each fixture only — never "
                         "on a fallback trial (grading the static set would grade our own "
                         "hand-written table, not Lana) and never more than once per fixture "
                         "(the question is about the set, not about sampling). So `scored` here "
                         "counts FIXTURES that produced at least one written set, not trials._")
                L.append("")
                L.append("| axis | scored | PASS | SOFT_FAIL | HARD_FAIL | UNSCORED | REVIEW |")
                L.append("|---|---|---|---|---|---|---|")
                for axis, d in sorted(agg.items()):
                    L.append(f"| {axis} | {d.get('n', 0)} | {d.get('PASS', 0)} | "
                             f"{d.get('SOFT_FAIL', 0)} | {d.get('HARD_FAIL', 0)} | "
                             f"{d.get('UNSCORED', 0)} | {d.get('REVIEW', 0)} |")
                L.append("")

        L.append("### Findings (non-PASS detail, with the questions that produced them)")
        L.append("")
        any_finding = False
        shown: dict[str, int] = {}
        _CAP = 2  # trials shown per fixture; the table above carries the full rates
        for r in qrecs:
            problems = [c for c in r.mechanical if c.verdict != "PASS"]
            jprob = [a for a in (r.judged.axes if r.judged else []) if a.verdict != "PASS"]
            if not problems and not jprob:
                continue
            shown[r.fx.id] = shown.get(r.fx.id, 0) + 1
            if shown[r.fx.id] > _CAP:
                # Not a silent cap: the rate is in the table above, and the last line of this
                # section says how many transcripts were elided.
                continue
            any_finding = True
            trial_tag = f"  ·  trial {r.trial + 1}" if trials > 1 else ""
            L.append(f"#### `{r.fx.id}`{trial_tag} — {r.fx.opening_line!r}")
            if r.fx.probes:
                L.append(f"> {' '.join(r.fx.probes.split())}")
            L.append("")
            for f in r.gen.flags:
                L.append(f"- _backend flag_: {f}")
            for c in problems:
                L.append(f"- **{c.verdict}** `{c.name}` — {c.detail}")
            for a in jprob:
                dis = " · JUDGES DISAGREE" if a.disagreement else ""
                fields = f" (fields: {', '.join(a.offending_fields)})" if a.offending_fields else ""
                L.append(f"- **{a.verdict}** (judged) `{a.axis}`{dis} — {a.reasoning}{fields}")
            L.append("")
            # Accurate per trial. On a fallback trial the model wrote NOTHING — every step came
            # out of reco_question_sets' static table — and heading it "the set Lana wrote"
            # attributes the team's own hand-written questions to the generator, which is the
            # opposite of what the trial found.
            L.append("<details><summary>" + (
                "the set Lana wrote" if r.gen.generated
                else "the STATIC set the neighbour received (the model wrote none of this)"
            ) + "</summary>")
            L.append("")
            for i, s in enumerate(r.gen.middle()):
                req = " **(must-have)**" if s.get("required") else ""
                opts = f" — taps: {', '.join(s.get('options') or [])}" if s.get("options") else ""
                ph = f" — e.g. *{s.get('placeholder')}*" if s.get("placeholder") else ""
                ans = f" — PRE-FILLED: {s.get('answer')!r}" if s.get("answer") else ""
                L.append(f"{i + 1}. `{s.get('field')}` {s.get('question')}{req}{opts}{ph}{ans}")
            L.append("")
            L.append("</details>")
            L.append("")
        elided = sum(max(0, n - _CAP) for n in shown.values())
        if elided:
            L.append(f"_{elided} further failing trial(s) not shown — at most {_CAP} transcripts "
                     f"per fixture. The rates in the table above count every trial._")
            L.append("")
        if not any_finding:
            L.append("_Every fixture passed every applicable axis on every trial._")
            L.append("")

    # ── Appendix: what makes each axis pass ────────────────────────────────────────────
    # Emitted from checks.PASS_RULES, which sits beside the implementations, so a reader
    # auditing a number can find the rule that produced it, and so the stated rule and the
    # code cannot drift apart unnoticed.
    shown = {c.name for r in qrecs for c in r.mechanical}
    shown |= {a.axis for r in qrecs for a in (r.judged.axes if r.judged else [])}
    shown |= {c.name for r in arecs for c in r.mechanical}
    rules = [(k, v) for k, v in checks.PASS_RULES.items() if k in shown]
    if rules:
        L.append("---")
        L.append("")
        L.append("## Appendix — evaluation criteria and pass rules")
        L.append("")
        L.append("_Generated from `checks.PASS_RULES`, which lives next to the implementations. "
                 "`selftest.py` fails if any axis is missing an entry, so a check cannot be "
                 "added without stating what makes it pass._")
        L.append("")
        L.append("| axis | what PASSES | how it is decided | on failure |")
        L.append("|---|---|---|---|")
        for name, (passes, how, sev) in rules:
            L.append(f"| `{name}` | {passes} | {how} | {sev} |")
        L.append("")

    return "\n".join(L)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Recommendation-quality eval harness")
    ap.add_argument("--arm", choices=["questions", "answers", "both"], default="both")
    ap.add_argument("--backend", choices=["inproc", "live", "static", "dry"], default=None)
    ap.add_argument("--validator", choices=["shipped", "reference", "dry"], default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="alias for --backend dry --validator dry, judge off")
    ap.add_argument("--judge", action="store_true", help="run arm A's judged axes (needs OPENAI_API_KEY)")
    ap.add_argument("--multi-judge", action="store_true", help="3 passes + disagreement flag")
    ap.add_argument("--trials", type=int, default=None,
                    help="arm A generations per fixture (default 3 for inproc/live, 1 for the "
                         "deterministic backends). The generator is nondeterministic — a "
                         "single trial measures sampling noise, so every Arm A number is a rate")
    ap.add_argument("--id", help="run one fixture by id (either arm)")
    ap.add_argument("--gate", action="store_true", help="exit 1 on any HARD_FAIL or UNSCORED")
    ap.add_argument("--out", default=str(OUT_DIR / "report.md"))
    args = ap.parse_args()

    backend_kind = "dry" if args.dry_run else (args.backend or "inproc")
    validator_kind = "dry" if args.dry_run else (args.validator or "shipped")
    do_judge = args.judge and not args.dry_run

    qs, ans, raw = load_fixtures()
    errs = validate_fixtures(raw)
    if errs:
        print(f"[reco-eval] {len(errs)} fixture problem(s):")
        for e in errs:
            print(f"  - {e}")
        return 2
    print(f"[reco-eval] fixtures OK: {len(qs)} question set(s), {len(ans)} answer(s)")

    if args.id:
        qs = [q for q in qs if q.id == args.id]
        ans = [a for a in ans if a.id == args.id]
        if not qs and not ans:
            print(f"no fixture with id={args.id!r}")
            return 2

    arm = args.arm
    # `dry` and `static` are deterministic by construction, so repeating them buys nothing.
    # `inproc` and `live` go through the model and need repetition to say anything at all.
    default_trials = 3 if backend_kind in ("inproc", "live") else 1
    trials = args.trials if args.trials and args.trials > 0 else default_trials
    qrecs = run_questions(qs, backend_kind=backend_kind, do_judge=do_judge,
                          n_judges=3 if args.multi_judge else 1,
                          trials=trials) if (arm in ("questions", "both") and qs) else []
    arecs = run_answers(ans, validator_kind=validator_kind) if (arm in ("answers", "both") and ans) else []

    report = render_report(qrecs, arecs, backend_kind=backend_kind,
                           validator_kind=validator_kind, judged=do_judge, arm=arm,
                           trials=trials)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")

    qhard = sum(r.verdict == "HARD_FAIL" for r in qrecs)
    qsoft = sum(r.verdict == "SOFT_FAIL" for r in qrecs)
    qunsc = sum(r.verdict == "UNSCORED" for r in qrecs)
    jhard = sum(1 for r in qrecs if r.judged and r.judged.any_hard_fail)
    junsc = sum(1 for r in qrecs if r.judged and r.judged.any_unscored)
    ahard = sum(r.verdict == "HARD_FAIL" for r in arecs)
    aunsc = sum(r.verdict == "UNSCORED" for r in arecs)

    print("\n" + "=" * 68)
    if qrecs:
        qm = question_metrics(qrecs)
        nfx = len({r.fx.id for r in qrecs})
        print(f"[reco-eval] arm A · {nfx} fixtures x {trials} trials = {qm['trials']} sets "
              f"· backend={backend_kind}")
        print(f"  mechanical: {qhard} HARD_FAIL, {qsoft} SOFT_FAIL, {qunsc} UNSCORED, "
              f"{qm['errors']} backend error")
        _lo, _hi = qm["fallback_ci"]
        _dlo, _dhi = qm["delivered_ci"]
        print(f"  STATIC SET DELIVERED:  {qm['delivered']}/{qm['delivered_of']} trials "
              f"({qm['delivered_rate']:.0%}, CI {_dlo:.0%}-{_dhi:.0%})"
              f"   <-- what a neighbour gets")
        print(f"  ...of which UNINTENDED: {qm['fallback']}/{qm['fallback_of']} "
              f"({qm['fallback_rate']:.0%}, CI {_lo:.0%}-{_hi:.0%})"
              f"   <-- excludes fixtures that pin the fallback")
        if do_judge:
            print(f"  judged: {jhard} HARD_FAIL, {junsc} UNSCORED")
    if arecs:
        m = answer_metrics(arecs)
        print(f"[reco-eval] arm B · {m['total']} answers · validator={validator_kind}")
        print(f"  junk accepted:     {m['junk_reaching_accepted']}/{m['junk_reaching']}  "
              f"({m['reaching_acceptance_rate']:.0%}) of junk that REACHES a validator"
              f"   <-- the §11 gap")
        print(f"                     ({m['junk_accepted']}/{m['junk']} counting the "
              f"{m['junk'] - m['junk_reaching']} rows the flow drops as empty before any "
              f"validator runs)")
        print(f"  good rejected:     {m['good_rejected']}/{m['good']}  "
              f"({m['false_reject_rate']:.0%})   <-- must stay 0 (§12.4)")
        print(f"  accuracy:          {m['accuracy']:.0%}")
        if m["unscorable"]:
            print(f"  unscorable:        {m['unscorable']}")
    print(f"  report -> {out_path}")

    # Fail closed: an axis nobody could measure must not carry a green gate.
    if args.gate and (qhard or qunsc or jhard or junsc or ahard or aunsc):
        print("[reco-eval] GATE FAIL")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
