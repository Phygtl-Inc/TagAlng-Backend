"""
run_safety.py — D10 (F-SAFETY), structural half.

    cd services/lana-worker/simulations/subject_eval
    python run_safety.py          # scan every fixture
    python run_safety.py --gate   # exit 1 on any disagreement with the hand-label

Offline, deterministic, no key, no DB. Scans payloads against the rules in safety.py.

D10's bar is ZERO violations, so this gates on AGREEMENT WITH THE LABEL, not on the raw count
of violations found — the fixture file is mostly planted violations on purpose. A pass means
every `expect: violation` case fired and every `expect: clean` case did not.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import safety  # noqa: E402

FIXTURES = _HERE / "safety_fixtures.yaml"
OUT = _HERE / "out" / "safety.md"


def main() -> int:
    ap = argparse.ArgumentParser(description="D10 F-SAFETY, structural half")
    ap.add_argument("--gate", action="store_true")
    args = ap.parse_args()

    cases = (yaml.safe_load(FIXTURES.read_text(encoding="utf-8")) or {}).get("cases") or []
    if not cases:
        print("no fixtures")
        return 2

    rows, wrong, unscored = [], [], []
    for c in cases:
        want = str(c.get("expect")) == "violation"
        r = safety.evaluate(c)
        rows.append((c, r, want))
        if r.unscorable:
            unscored.append(r)
        elif r.violated != want:
            wrong.append((c, r, want))

    print(f"[f-safety] {len(cases)} fixtures")
    for c, r, want in rows:
        mark = "???" if r.unscorable else ("ok " if r.violated == want else "XX ")
        print(f"  {mark} {r.case_id:<42} want={'violation' if want else 'clean':<9} "
              f"fired={r.rules_fired or '-'}")

    # Coverage: a rule with no planted violation is a rule nobody has proven fires.
    covered = {n for _, r, _ in rows for n in r.rules_fired}
    uncovered = [n for n, _ in safety.RULES if n not in covered]

    L = ["# F-SAFETY (D10) — structural half", "",
         f"- {len(cases)} fixtures · {len(safety.RULES)} rules · offline, no key, no DB",
         "- Payload shape is **provisional** — `[ASJID-1]` has not landed. An unrecognised "
         "shape is reported UNSCORED, never clean.", "",
         f"**{len(cases) - len(wrong) - len(unscored)}/{len(cases)} agree with the hand-label.**", ""]
    if wrong:
        L += ["## Disagreements", "", "| fixture | wanted | got | rules |", "|---|---|---|---|"]
        L += [f"| `{r.case_id}` | {'violation' if w else 'clean'} | "
              f"{'violation' if r.violated else 'clean'} | {', '.join(r.rules_fired) or '—'} |"
              for _, r, w in wrong]
        L.append("")
    if uncovered:
        L += ["## Rules with no planted violation", "",
              "_These fire on nothing in the fixture set, so nothing proves they work._", "",
              *[f"- `{n}`" for n in uncovered], ""]
    by_cat: dict[str, int] = {}
    for c, r, _ in rows:
        if r.violated:
            by_cat[r.category] = by_cat.get(r.category, 0) + 1
    if by_cat:
        L += ["## Violations caught, by category", "", "| category | n |", "|---|---|"]
        L += [f"| {k} | {v} |" for k, v in sorted(by_cat.items(), key=lambda kv: -kv[1])]
        L.append("")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(L), encoding="utf-8")

    print(f"\n  agree: {len(cases) - len(wrong) - len(unscored)}/{len(cases)}"
          f"  ·  disagree: {len(wrong)}  ·  unscorable: {len(unscored)}")
    if uncovered:
        print(f"  [!] rules never fired by any fixture: {uncovered}")
    print(f"  report -> {OUT}")

    if args.gate and (wrong or unscored or uncovered):
        print("[f-safety] GATE FAIL")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
