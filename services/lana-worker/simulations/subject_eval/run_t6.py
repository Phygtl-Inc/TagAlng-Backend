"""run_t6.py — T6 honest-empty gate. Offline, deterministic, no LLM, no DB."""
from __future__ import annotations
import argparse, sys
from pathlib import Path
import yaml
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import honest_empty as he  # noqa: E402

FIX = _HERE / "honest_empty_fixtures.yaml"
OUT = _HERE / "out" / "t6_honest_empty.md"

def main() -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("--gate", action="store_true")
    args = ap.parse_args()
    cases = (yaml.safe_load(FIX.read_text(encoding="utf-8")) or {}).get("cases") or []
    wrong, rows = [], []
    for c in cases:
        want = str(c.get("expect")) == "violation"
        fired, detail = he.scan(he.from_case(c))
        got = bool(fired)
        rows.append((c, fired, detail, want))
        if got != want:
            wrong.append((c, fired, detail, want))
        print(f"  {'ok ' if got == want else 'XX '} {c['id']:<44} "
              f"want={'violation' if want else 'clean':<9} fired={fired or '-'}")
    covered = {n for _, f, _, _ in rows for n in f}
    uncovered = [n for n, _ in he.RULES if n not in covered]
    L = ["# T6 — honest empty", "",
         f"- {len(cases)} fixtures · {len(he.RULES)} rules · offline, no LLM, no DB",
         "- Asserted against the rules `app/activity_browse.py` already ships, so the browse "
         "path and the authority filter speak with one voice.", "",
         f"**{len(cases)-len(wrong)}/{len(cases)} agree with the hand-label.**", ""]
    if wrong:
        L += ["## Disagreements", "", "| fixture | wanted | fired |", "|---|---|---|"]
        L += [f"| `{c['id']}` | {'violation' if w else 'clean'} | {', '.join(f) or '—'} |"
              for c, f, _, w in wrong]
        L.append("")
    if uncovered:
        L += ["## Rules no fixture fires", "", *[f"- `{n}`" for n in uncovered], ""]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(L), encoding="utf-8")
    print(f"\n  agree {len(cases)-len(wrong)}/{len(cases)}  ·  uncovered rules: {uncovered or 'none'}")
    print(f"  report -> {OUT}")
    return 1 if (args.gate and (wrong or uncovered)) else 0

if __name__ == "__main__":
    raise SystemExit(main())
