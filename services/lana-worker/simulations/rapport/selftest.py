"""selftest.py — offline negative-control suite for the rapport eval.

Mirrors policy_eval/selftest.py: a green eval run is meaningless if the checks CANNOT fail,
so every mechanical gate here is exercised in BOTH directions (clean -> pass, planted
violation -> fail). Run: python selftest.py  (exit 0 = every detection fires).

NO network / NO API calls: the extractor entry point is monkeypatched with a stub that
returns the raw dict shape parse_incremental_claims_data expects. Every gate is exercised
in BOTH directions (clean -> pass, planted violation -> fail).
"""
from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

RAPPORT = Path(__file__).resolve().parent   # this file now lives INSIDE rapport/
SCRATCH = Path(__file__).resolve().parent
sys.path.insert(0, str(RAPPORT))

import run_eval  # noqa: E402
from app.models import ExtractedClaim  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra=None) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{(' — ' + str(extra)) if extra else ''}")
    if not cond:
        FAILS.append(name)


def claim(**kw) -> ExtractedClaim:
    base = dict(concept="c", label="L", confidence=0.9, bucket="activity")
    base.update(kw)
    return ExtractedClaim(**base)


# ---------------------------------------------------------------------------
print("\n== 1. _match_claims: one-to-one MAXIMUM bipartite matching ==")

# a) ONE claim can no longer satisfy TWO expectations (old any() scan reported 2).
c = claim(label="Karate and Brazilian", bucket="activity")
exps = [{"bucket": "activity", "label_has": "karate"},
        {"bucket": "activity", "label_has": "brazilian"}]
n, un = run_eval._match_claims([c], exps)
check("one claim satisfies at most ONE expectation", n == 1 and un == [], f"n={n} unmatched={un}")

# b) DUPLICATE claims are now a precision miss (old code never penalised them).
dup = [claim(concept="karate", label="Karate"), claim(concept="karate_practice", label="Karate")]
n, un = run_eval._match_claims(dup, [{"bucket": "activity", "label_has": "karate"}])
check("duplicate claim becomes an unmatched (precision) miss", n == 1 and un == [1],
      f"n={n} unmatched={un}")

# c) MAXIMUM, not first-fit greedy. exp0 accepts claim0+claim1; exp1 accepts ONLY claim0.
#    First-fit gives claim0 to exp0 and strands exp1 (n=1); Kuhn augments to n=2.
claims_c = [claim(label="Karate and swimming"), claim(label="Swimming")]
exps_c = [{"bucket": "activity", "label_has": "swimming"},
          {"bucket": "activity", "label_has": "karate"}]
n, un = run_eval._match_claims(claims_c, exps_c)
check("MAXIMUM matching beats first-fit greedy (2, not 1)", n == 2 and un == [], f"n={n}")

# d) genuine miss still reported
n, un = run_eval._match_claims([claim(label="Karate")], [{"bucket": "faith", "label_has": "church"}])
check("non-matching claim -> 0 matched, 1 unmatched", n == 0 and un == [0], f"n={n} un={un}")

# e) label_has_any
lha = {"bucket": "vicinity", "label_has_any": ["moved", "arrived", "relocat", "new to"]}
check("label_has_any matches an alternative",
      run_eval._claim_matches(claim(bucket="vicinity", label="Arrived here last week"), lha))
check("label_has_any rejects an unrelated label",
      not run_eval._claim_matches(claim(bucket="vicinity", label="Feeling tired"), lha))

# ---------------------------------------------------------------------------
print("\n== 2. _check_anonymization: per-field + normalised, fails CLOSED ==")

clean = [claim(label="Karate", source_quote="my son does karate", synonyms=["martial arts"])]
check("clean claims -> no leak", run_eval._check_anonymization(clean, "What belt?", ["Sara"], None) == [])

# every field is scanned, separately
check("leak in details is caught",
      run_eval._check_anonymization([claim(details=["Sara started in May"])], None, ["Sara"]) == ["Sara"])
check("leak in nickname is caught",
      run_eval._check_anonymization([], None, ["Sara"], "Sara") == ["Sara"])
check("leak in followup is caught",
      run_eval._check_anonymization([], "How is Sara doing?", ["Sara"]) == ["Sara"])
check("leak in synonyms is caught",
      run_eval._check_anonymization([claim(synonyms=["Lincoln Elementary"])], None, ["Lincoln"]) == ["Lincoln"])
check("leak in source_quote is caught",
      run_eval._check_anonymization([claim(source_quote="at Lincoln")], None, ["Lincoln"]) == ["Lincoln"])

# normalisation makes it MORE sensitive than the old plain substring test
check("punctuation/spacing cannot hide a leak (jane@example.com -> 'Jane @ Example . COM')",
      run_eval._check_anonymization([claim(label="mail Jane @ Example . COM")], None,
                                    ["jane@example.com"]) == ["jane@example.com"])
check("  (control) an inserted token is NOT a match — normalisation, not fuzzy matching",
      run_eval._check_anonymization([claim(label="jane (at) example.com")], None,
                                    ["jane@example.com"]) == [])
check("hyphen/whitespace cannot hide a leak (123  Maple--Street)",
      run_eval._check_anonymization([claim(label="123  Maple--Street")], None,
                                    ["123 Maple Street"]) == ["123 Maple Street"])

# per-field, so a needle straddling two fields is NOT a false positive
straddle = [claim(synonyms=["daughter Sara", "Lincoln school"])]
check("needle spanning two fields is NOT reported (no cross-field false positive)",
      run_eval._check_anonymization(straddle, None, ["Sara Lincoln"]) == [],
      "old joined-blob check would have flagged this")
# ...but each real needle in that same claim IS still reported
check("both real needles in that claim still caught",
      run_eval._check_anonymization(straddle, None, ["Sara", "Lincoln"]) == ["Sara", "Lincoln"])

# every needle in the live fixture set still works when planted
import yaml  # noqa: E402

fixtures = yaml.safe_load((RAPPORT / "fixtures.yaml").read_text(encoding="utf-8"))["fixtures"]
needles = [n for f in fixtures for n in (f.get("must_not_contain") or [])]
missed = [n for n in needles
          if run_eval._check_anonymization([claim(label=f"prefix {n} suffix")], None, [n]) != [n]]
check(f"all {len(needles)} live fixture needles still detected when planted", not missed, str(missed))

# ---------------------------------------------------------------------------
print("\n== 3. validate_fixtures: every rule fires, none false-positives ==")

check("current 27 fixtures validate clean", run_eval.validate_fixtures(fixtures) == [],
      str(run_eval.validate_fixtures(fixtures)))

ok_exp = [{"bucket": "activity", "label_has": "x"}]
cases = [
    ("duplicate id", [{"id": "a", "input": "i", "expect_claims": ok_exp},
                      {"id": "a", "input": "i", "expect_claims": ok_exp}], "duplicate"),
    ("empty input", [{"id": "a", "input": "  ", "expect_claims": ok_exp}], "input"),
    ("missing id", [{"input": "i", "expect_claims": ok_exp}], "id"),
    ("expect_no_claims TRUE + expect_claims", [{"id": "a", "input": "i",
                                                "expect_no_claims": True,
                                                "expect_claims": ok_exp}], "contradictory"),
    ("expectation missing bucket", [{"id": "a", "input": "i",
                                     "expect_claims": [{"label_has": "x"}]}], "bucket"),
    ("inert matcher (empty label_has, no label_has_any)",
     [{"id": "a", "input": "i", "expect_claims": [{"bucket": "vicinity", "label_has": ""}]}],
     "inert"),
    ("inert matcher (no label keys at all)",
     [{"id": "a", "input": "i", "expect_claims": [{"bucket": "vicinity"}]}], "inert"),
]
for name, fx, needle in cases:
    errs = run_eval.validate_fixtures(fx)
    check(f"rejects: {name}", any(needle in e for e in errs), str(errs))

# the rapport_006 shape (expect_no_claims FALSE + expect_claims) must NOT trip
check("accepts expect_no_claims:false alongside expect_claims (rapport_006 shape)",
      run_eval.validate_fixtures([{"id": "a", "input": "i", "expect_no_claims": False,
                                   "expect_claims": ok_exp}]) == [])

# ---------------------------------------------------------------------------
print("\n== 4. _judge_gap_quality is TOTAL (never raises, never gates) ==")


class Boom:
    class chat:
        class completions:
            @staticmethod
            def create(**kw):
                raise RuntimeError("simulated OpenAI 500")


class Garbage:
    class chat:
        class completions:
            @staticmethod
            def create(**kw):
                class M:
                    content = "not json at all"
                return type("C", (), {"choices": [type("X", (), {"message": M})]})


class BadVerdict:
    class chat:
        class completions:
            @staticmethod
            def create(**kw):
                class M:
                    content = '{"verdict": "excellent", "reasoning": "n"}'
                return type("C", (), {"choices": [type("X", (), {"message": M})]})


class Good:
    class chat:
        class completions:
            @staticmethod
            def create(**kw):
                class M:
                    content = '{"verdict": "just-right", "reasoning": "grounded"}'
                return type("C", (), {"choices": [type("X", (), {"message": M})]})


check("API error -> unscored", run_eval._judge_gap_quality("q?", Boom)[0] == "unscored")
check("bad JSON -> unscored", run_eval._judge_gap_quality("q?", Garbage)[0] == "unscored")
check("unknown verdict -> unscored", run_eval._judge_gap_quality("q?", BadVerdict)[0] == "unscored")
check("missing client -> unscored", run_eval._judge_gap_quality("q?", None)[0] == "unscored")
check("healthy judge -> just-right", run_eval._judge_gap_quality("q?", Good)[0] == "just-right")

# ---------------------------------------------------------------------------
print("\n== 5. end-to-end run() with a STUBBED extractor (no API calls) ==")

_real = run_eval.incremental_claims_from_utterance
SEEN_ARGS: list[tuple] = []


def stub(payload):
    def _f(message, existing_labels=None, recent_questions=None):
        SEEN_ARGS.append((message, existing_labels, recent_questions))
        if isinstance(payload, Exception):
            raise payload
        return payload
    return _f


def run_with(payload, fixture_id, judge=False):
    run_eval.incremental_claims_from_utterance = stub(payload)
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            rc = run_eval.run(fixture_id, False, judge, SCRATCH / "out" / "report.md")
    finally:
        run_eval.incremental_claims_from_utterance = _real
    return rc, buf.getvalue()


TECH_OK = {"claims": [{"concept": "tech_worker", "label": "Works in tech", "bucket": "activity",
                       "confidence": 0.9, "vague": True}],
           "followup_question": "What kind of tech?"}

rc, out = run_with(TECH_OK, "rapport_009")
check("CLEAN rapport_009 -> exit 0", rc == 0, out.strip().splitlines()[-1])

# recall violation: extractor returns nothing
rc, out = run_with({"claims": [], "followup_question": "hm?"}, "rapport_009")
check("PLANTED recall miss -> exit 1", rc == 1)
check("  ...and precision reads n/a, NOT 1.00 (zero denominator)", "facet precision:        n/a" in out,
      [ln for ln in out.splitlines() if "precision:" in ln][0])
check("  ...and the failures-by-axis block tags it as `recall`",
      any(ln.strip().startswith("recall") and "rapport_009" in ln for ln in out.splitlines()),
      [ln for ln in out.splitlines() if "recall" in ln])

# precision violation: correct claim PLUS an invented one
rc, out = run_with({"claims": TECH_OK["claims"] + [{"concept": "invented", "label": "Invented",
                                                    "bucket": "faith", "confidence": 0.9}],
                    "followup_question": "What kind of tech?"}, "rapport_009")
check("PLANTED false-positive claim -> exit 1", rc == 1)
check("  ...precision axis named in failures-by-axis",
      any(ln.strip().startswith("precision") and "rapport_009" in ln for ln in out.splitlines()))

# duplicate-claim violation (the whole point of one-to-one matching)
rc, out = run_with({"claims": TECH_OK["claims"] + [{"concept": "tech_worker2", "label": "Works in tech",
                                                    "bucket": "activity", "confidence": 0.9,
                                                    "vague": True}],
                    "followup_question": "What kind of tech?"}, "rapport_009")
check("PLANTED duplicate claim -> exit 1 (old greedy code passed this)", rc == 1)

# anonymization violation: PII in the followup, which is never redacted
rc, out = run_with({"claims": [{"concept": "kid_in_school", "label": "Kid in school",
                                "bucket": "general", "confidence": 0.9}],
                    "followup_question": "How does Sara like Lincoln Elementary?"}, "rapport_006")
check("PLANTED PII leak in followup -> exit 1", rc == 1)
check("  ...reported as an anonymization HARD fail", "ANONYMIZATION LEAKS" in out)

# expect_no_claims violation
rc, out = run_with({"claims": [{"concept": "pizza_fan", "label": "Likes pizza", "bucket": "interest",
                                "confidence": 0.9}]}, "rapport_015")
check("PLANTED claim on an expect_no_claims fixture -> exit 1", rc == 1)

# kids_count violation
rc, out = run_with({"claims": [], "kids_count": 5}, "rapport_017")
check("PLANTED wrong kids_count -> exit 1", rc == 1)

# followup-presence violation
rc, out = run_with({"claims": [], "followup_question": "So how old are they?"}, "rapport_016")
check("PLANTED unexpected followup -> exit 1", rc == 1)

# extractor error: recorded, counted, run does NOT abort before the summary
rc, out = run_with(RuntimeError("simulated 502 from provider"), "rapport_009")
check("PLANTED extractor error -> exit 1", rc == 1)
check("  ...ERROR verdict printed for the fixture", "[ERROR] rapport_009" in out)
check("  ...summary still printed (run not aborted)", "[rapport-eval] summary" in out)
check("  ...vacuity guard fires (nothing scored)", "VACUOUS RUN" in out)

# recent_questions passthrough + the not-a-repeat gate
SEEN_ARGS.clear()
POTTERY = {"claims": [{"concept": "pottery", "label": "Pottery", "bucket": "activity",
                       "confidence": 0.9}],
           "followup_question": "Wheel or hand-building?"}
rc, out = run_with(POTTERY, "rapport_027")
check("rapport_027 clean -> exit 0", rc == 0)
check("  ...recent_questions actually reached the extractor",
      bool(SEEN_ARGS) and SEEN_ARGS[0][2] == ["What got you into pottery?",
                                              "Do you have a favorite thing to make?"],
      str(SEEN_ARGS[:1]))
rc, out = run_with({**POTTERY, "followup_question": "  what got you INTO pottery??  "}, "rapport_027")
check("PLANTED repeated followup -> exit 1", rc == 1)
check("  ...followup_repeat axis named", "followup_repeat" in out)

# judge is ADVISORY: a broken judge must not change the exit code.
# NB: _judge_gap_quality is stubbed so this makes ZERO network calls.
_real_judge = run_eval._judge_gap_quality
run_eval._judge_gap_quality = lambda q, c: ("unscored", "simulated judge outage")
try:
    rc, out = run_with(TECH_OK, "rapport_009", judge=True)
finally:
    run_eval._judge_gap_quality = _real_judge
gap_line = [ln for ln in out.splitlines() if "gap quality" in ln]
check("judge outage on an otherwise-clean fixture -> STILL exit 0 (advisory axis)", rc == 0)
check("  ...unscored EXCLUDED from the just-right denominator (n/a, not 0/1 and not 1/1)",
      bool(gap_line) and "n/a (0 scored)" in gap_line[0] and "1 unscored" in gap_line[0],
      gap_line)

# artifact
report = SCRATCH / "out" / "report.md"
check("out/report.md artifact written", report.exists() and report.stat().st_size > 0)
check("  ...artifact carries the axis breakdown", "Failures by axis" in report.read_text(encoding="utf-8"))

print("\n" + "=" * 60)
print(f"{'ALL CHECKS PASSED' if not FAILS else 'FAILURES: ' + str(FAILS)}")
sys.exit(1 if FAILS else 0)
