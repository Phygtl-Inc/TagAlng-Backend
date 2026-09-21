"""
d11_label_assist.py — model-assisted first pass on the D11 / F-RESOLVE labelling pool, plus a
blind audit sample so a human can measure whether the model's labels are trustworthy before
anyone relies on them.

    cd services/lana-worker/simulations/subject_eval
    python d11_label_assist.py                          # labels all 204 pairs, writes the audit sample
    python d11_label_assist.py --score audit_filled.csv  # after a human fills in the audit sample

WHY MODEL-ASSISTED, NOT HAND-LABELLED FROM SCRATCH
----------------------------------------------------
SUBJECT_GRAPH_EVAL_PLAN.md's own decision #5 ("Model-assisted labelling, with a human audit
slice as control") already settled this. Hand-labelling 204 pairs is ~1.5 days per the plan's
own fixture-budget math (§5.1: ~40s/pair x 300+). A model can do a first pass on 204 short pairs
in a couple of minutes, real cost. What it CANNOT do is grade its own homework — that would make
D3/D11's whole calibration circular, exactly the failure mode this suite exists to catch in
other systems.

So the design is two files, generated together, that must stay independent of each other:
  1. out/d11_model_labels.json    every pair + the model's label + its reasoning.
  2. out/d11_audit_sample.csv     a BLIND stratified sample (~30 pairs, no model label, no
                                  resolver decision, no reasoning) for a human to fill in.

`--score` compares a filled-in audit_sample against the model's labels for the SAME pairs and
reports the agreement rate. High agreement (the plan doesn't fix a number, but >85-90% is the
usual bar for trusting model labels as ground truth in eval work) means the other ~174 pairs can
be trusted from the model pass alone. Low agreement means full hand-labelling is still needed —
and this will have cost only a few minutes and a few dollars to learn, not 1.5 days.

STRATIFICATION
---------------
Sampling uniformly at random would over-represent the huge "obviously different" tail and under-
represent the boundary cases the audit actually needs to stress-test. The sample is drawn evenly
across the resolver's own three buckets (merge / adjudicate / new) so the audit exercises exactly
the decision boundary D11 cares about, not just the easy cases.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_HERE = Path(__file__).resolve().parent
CANDIDATES = _HERE / "out" / "d3_variant_candidates.json"
MODEL_LABELS_OUT = _HERE / "out" / "d11_model_labels.json"
AUDIT_SAMPLE_OUT = _HERE / "out" / "d11_audit_sample.csv"
AUDIT_KEY_OUT = _HERE / "out" / "d11_audit_answer_key.json"   # not for the auditor's eyes first

AUDIT_SAMPLE_SIZE = 30

_SYSTEM = """You are labelling pairs of short phrases mentioned by users of a neighbourhood app \
(TagAlng), each describing something they mentioned in passing — an activity, interest, need, \
or place. The question for EACH pair: would a reasonable neighbour reading both phrases agree \
they refer to the SAME underlying thing (the same specific activity, the same specific need, \
the same specific place/person), just worded differently — or are they DIFFERENT things that \
happen to share a word?

Judge on MEANING, not surface overlap. "husband travels for work" and "wife travels" share a \
word but are about two different people. "pre-k" and "son jake in pre-k" are the same thing \
described at different levels of detail.

Output ONLY valid JSON: {"label": "same"|"different"|"ambiguous", "reason": "one short clause"}
"ambiguous" is for genuine 50/50 cases only — a busy neighbour would still call most pairs one \
way or the other. Do not hedge into "ambiguous" just because you're not fully certain."""


def _label_pair(a: str, b: str, llm_json, model: str) -> dict:
    payload = json.dumps({"phrase_a": a, "phrase_b": b}, ensure_ascii=False)
    try:
        raw = llm_json(model=model, system=_SYSTEM, user_payload=payload,
                        max_tokens=120, temperature=0.0)
        label = str(raw.get("label") or "").strip().lower()
        if label not in ("same", "different", "ambiguous"):
            label = "ambiguous"
        return {"label": label, "reason": str(raw.get("reason") or "").strip()[:200]}
    except Exception as exc:  # noqa: BLE001 — one bad call must not kill the whole pass
        return {"label": "error", "reason": f"{type(exc).__name__}: {exc}"[:200]}


def run_label_pass() -> list[dict]:
    from dotenv import load_dotenv

    load_dotenv(_HERE.parents[3] / ".env.local", override=True)
    import os
    os.environ.setdefault("LANA_LLM_FALLBACK", "0")

    sys.path.insert(0, str(_HERE.parents[1]))
    from app.orchestrator.llm import llm_configured, llm_json, synthesizer_model

    if not llm_configured():
        print("no LLM configured — set OPENAI_API_KEY"); return []

    model = synthesizer_model()
    pairs = json.loads(CANDIDATES.read_text(encoding="utf-8"))
    print(f"[D11-assist] labelling {len(pairs)} pairs with {model} (temperature=0)")

    out = []
    for i, p in enumerate(pairs, 1):
        result = _label_pair(p["a"], p["b"], llm_json, model)
        out.append({**p, **result})
        if i % 25 == 0 or i == len(pairs):
            print(f"    {i}/{len(pairs)}")

    errors = sum(1 for r in out if r["label"] == "error")
    if errors:
        print(f"  [!] {errors}/{len(pairs)} calls errored — see 'reason' field for each")

    MODEL_LABELS_OUT.parent.mkdir(parents=True, exist_ok=True)
    MODEL_LABELS_OUT.write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")

    dist = {}
    for r in out:
        dist[r["label"]] = dist.get(r["label"], 0) + 1
    print(f"  label distribution: {dist}")
    print(f"  -> {MODEL_LABELS_OUT}")
    return out


def build_audit_sample(labelled: list[dict]) -> None:
    """A BLIND stratified sample: no model label, no resolver decision, no reasoning. Just the
    two phrases and the cosine, so a human's judgment isn't anchored by seeing the model's guess
    first — the whole point of an audit is an INDEPENDENT read."""
    rng = random.Random("d11-audit-sample")
    by_bucket: dict[str, list[dict]] = {"merge": [], "adjudicate": [], "new": []}
    for r in labelled:
        by_bucket.setdefault(r["resolver"], []).append(r)

    per_bucket = max(1, AUDIT_SAMPLE_SIZE // 3)
    sample: list[dict] = []
    for bucket, rows in by_bucket.items():
        take = rng.sample(rows, min(per_bucket, len(rows)))
        sample.extend(take)
    rng.shuffle(sample)

    AUDIT_SAMPLE_OUT.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT_SAMPLE_OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "phrase_a", "phrase_b", "your_label (same / different / ambiguous)"])
        for i, r in enumerate(sample):
            w.writerow([i, r["a"], r["b"], ""])

    # The answer key — deliberately a SEPARATE file, not opened until after labelling.
    key = [{"id": i, **{k: r[k] for k in ("a", "b", "cosine", "resolver", "label", "reason")}}
           for i, r in enumerate(sample)]
    AUDIT_KEY_OUT.write_text(json.dumps(key, indent=1, ensure_ascii=False), encoding="utf-8")

    print(f"\n[D11-assist] blind audit sample ({len(sample)} pairs, stratified across "
          f"merge/adjudicate/new) -> {AUDIT_SAMPLE_OUT}")
    print(f"  Fill in the last column, then run:")
    print(f"    python d11_label_assist.py --score {AUDIT_SAMPLE_OUT}")
    print(f"  (the answer key at {AUDIT_KEY_OUT.name} stays unopened until then — that's what")
    print(f"   keeps this an honest audit instead of a self-check)")


def score_audit(filled_csv: Path) -> int:
    if not AUDIT_KEY_OUT.exists():
        print(f"no answer key at {AUDIT_KEY_OUT} — run without --score first"); return 2
    key = {row["id"]: row for row in json.loads(AUDIT_KEY_OUT.read_text(encoding="utf-8"))}

    agree = disagree = blank = 0
    disagreements = []
    with filled_csv.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            i = int(row["id"])
            human = row.get("your_label (same / different / ambiguous)", "").strip().lower()
            if not human:
                blank += 1
                continue
            model = key[i]["label"]
            # "ambiguous" from either side is treated as agreement with anything but a flat
            # opposite — auditing exists to catch confident-and-wrong, not to punish honesty
            # about a genuinely hard case.
            if human == model or "ambiguous" in (human, model):
                agree += 1
            else:
                disagree += 1
                disagreements.append((i, key[i]["a"], key[i]["b"], model, human))

    scored = agree + disagree
    if scored == 0:
        print("no rows filled in yet"); return 2
    rate = agree / scored
    print(f"\n[D11-assist] audit result: {agree}/{scored} agree ({rate:.0%}), {blank} left blank")
    if disagreements:
        print("\n  disagreements (model -> human):")
        for i, a, b, m, h in disagreements:
            print(f"    #{i}: {a!r} / {b!r}: model={m} human={h}")
    if rate >= 0.85:
        print(f"\n  >= 85% agreement — reasonable to trust the model's labels on the "
              f"remaining {204 - AUDIT_SAMPLE_SIZE} pairs for D11/F-RESOLVE.")
    else:
        print(f"\n  < 85% agreement — the model pass is not reliable enough on its own; "
              f"the disagreements above are where to start a fuller hand-review.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="D11 model-assisted labelling + human audit")
    ap.add_argument("--score", type=Path, default=None,
                     help="path to a filled-in audit CSV; scores it against the answer key")
    args = ap.parse_args()

    if args.score:
        return score_audit(args.score)

    labelled = run_label_pass()
    if not labelled:
        return 2
    build_audit_sample(labelled)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
