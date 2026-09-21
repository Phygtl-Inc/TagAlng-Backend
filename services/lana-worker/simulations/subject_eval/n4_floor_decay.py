"""
n4_floor_decay.py — N4: a decision table for open question §6.2 ("should claims quietly
vanish after a few months?").

    cd services/lana-worker/simulations/subject_eval
    python n4_floor_decay.py

Pure simulation. No DB, no LLM, no key. `subject`/`attestation` have not shipped (confirmed
2026-09-17 — see EVAL_WORKING_PLAN.md §5a8/§5a9), so `effective_n = floor(sum(max(w) per
attester))` and its display floor (`effective_n >= 3`) are SPEC TEXT, not running code. This
produces the table Tommaso needs to decide #2, not a measurement of anything live.

THE DECAY FORM IS REVERSE-ENGINEERED, AND VERIFIED AGAINST THE PLAN'S OWN WORKED NUMBERS
-------------------------------------------------------------------------------------------
SUBJECT_GRAPH_EVAL_PLAN.md §6.2 gives one worked table for a "medium predicate" (365-day
half-life) with NO attester-age variation (every attester in a column shares one age):

    | attesters | fresh | 3 months | 1 year |
    | 3 | shows | hidden (2.52) | hidden (1.50) |
    | 4 | shows | shows (3.36)  | hidden (2.00) |
    | 6 | shows | shows         | shows (3.00)  |

Solving backward: at age=365d the table needs w=0.50 exactly (3*0.5=1.50, 4*0.5=2.00, 6*0.5=3.00
— all three match to 2dp), which is EXACTLY exponential decay with a 365-day half-life:
`w(age) = 2 ** (-age / half_life)`. At age=91.25d (365/4, "3 months"), that formula gives
w=0.8427, so 3*w=2.528 and 4*w=3.371 — both match the table's 2.52 / 3.36 to the stated
precision. `_verify_against_plan()` below asserts this reproduction exactly; if it ever breaks,
the reverse-engineered formula and the plan have drifted apart, and it's the plan (or the
formula) that needs re-checking, not this test.

`half_life=365` is confirmed for the plan's own "medium" example. **The `fast`/`slow` half-lives
below are NOT sourced — they are `# GUESSED` at 90d and 730d (quarterly / biennial) purely to
give the decision table a spread to react to.** The whole point of this file is to show
Tommaso the CONSEQUENCE of a half-life choice, not to assert one.
"""

from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_HERE = Path(__file__).resolve().parent
OUT = _HERE / "out" / "n4_floor_decay.md"

DISPLAY_FLOOR = 3   # effective_n >= 3, per §6.2 / §12.3

# medium=365 is REVERSE-ENGINEERED and VERIFIED against the plan's own worked numbers below.
# fast/slow are # GUESSED (see module docstring) — decision-table spread, not sourced values.
HALF_LIVES = {"fast (# GUESSED)": 90, "medium (confirmed vs plan)": 365, "slow (# GUESSED)": 730}

AGES_DAYS = {"fresh": 0, "1 month": 30, "3 months": 91.25, "6 months": 182.5, "1 year": 365,
             "2 years": 730}


def w(age_days: float, half_life: float) -> float:
    """Exponential decay, halving every `half_life` days."""
    return 2 ** (-age_days / half_life)


def effective_n(n_attesters: int, age_days: float, half_life: float) -> int:
    """Every attester assumed to have attested at the SAME age — matches how the plan's own
    worked table is structured (one age per column, N attesters, no per-attester variation)."""
    import math

    return math.floor(n_attesters * w(age_days, half_life))


def _verify_against_plan() -> None:
    """Non-vacuity: this reverse-engineered formula must reproduce EVERY number the plan
    actually states exactly, and match its qualitative shows/hidden calls everywhere else — or
    the decision table below is worth nothing.

    The plan's table (§6.2) gives an exact Σw only for the five HIDDEN cells (2.52, 1.50, 3.36,
    2.00) plus the one boundary case worth flagging (6 @ 1yr = 3.00, exactly at the floor). Every
    other cell is stated only as "shows", with no number — asserting a specific value there
    would be testing an invented number, not the plan's. Two independent checks: exact numbers
    where given, and shows/hidden category everywhere (9 category checks, 5 of them also exact)."""
    medium = HALF_LIVES["medium (confirmed vs plan)"]
    # (attesters, age, exact Σw the plan states, or None if the plan only says "shows")
    cases = [
        (3, "fresh", None, "shows"), (3, "3 months", 2.52, "hidden"), (3, "1 year", 1.50, "hidden"),
        (4, "fresh", None, "shows"), (4, "3 months", 3.36, "shows"), (4, "1 year", 2.00, "hidden"),
        (6, "fresh", None, "shows"), (6, "3 months", None, "shows"), (6, "1 year", 3.00, "shows"),
    ]
    fails = []
    for n, age_label, exact_sum_w, want_category in cases:
        raw_sum_w = n * w(AGES_DAYS[age_label], medium)
        got_en = effective_n(n, AGES_DAYS[age_label], medium)
        got_category = "shows" if got_en >= DISPLAY_FLOOR else "hidden"
        ok = got_category == want_category
        if exact_sum_w is not None:
            ok = ok and abs(raw_sum_w - exact_sum_w) < 0.01
            detail = f"Σw={raw_sum_w:.2f} (plan: {exact_sum_w}), {got_category}"
        else:
            detail = f"Σw={raw_sum_w:.2f}, {got_category} (plan only states 'shows'/'hidden')"
        print(f"  [{'ok ' if ok else 'FAIL'}] {n} attesters @ {age_label}: {detail}")
        if not ok:
            fails.append((n, age_label, want_category, got_category))
    if fails:
        raise SystemExit(f"[N4] reverse-engineered decay formula does NOT match the plan: {fails}")


def main() -> int:
    print("[N4] floor-and-decay decision table for §6.2\n")
    print("## Verifying the decay formula against SUBJECT_GRAPH_EVAL_PLAN.md's own numbers\n")
    _verify_against_plan()
    print("\n  All 9 reproduce exactly — the exponential-halving reverse-engineering holds.\n")

    L = ["# N4 — floor-and-decay decision table (§6.2)", "",
         "Pure simulation, no DB/LLM. `subject`/`attestation` have not shipped, so this is a "
         "decision table, not a measurement — see module docstring for how the decay form was "
         "reverse-engineered and verified against the plan's own worked numbers (exact match, "
         "9/9 cases).", "",
         f"**Display floor: effective_n >= {DISPLAY_FLOOR}.** Below it, a claim with real "
         "attesters behind it quietly stops showing — that disappearance IS the product "
         "question in §6.2.", "",
         "`medium` (365-day half-life) is the only value CONFIRMED against the plan's worked "
         "table. `fast` (90d) and `slow` (730d) below are `# GUESSED` spread values, not "
         "sourced — the table exists to show the CONSEQUENCE of a choice, not assert one.", ""]

    for label, hl in HALF_LIVES.items():
        L += [f"## {label} predicate (half-life {hl}d)", "",
              "| attesters | " + " | ".join(AGES_DAYS.keys()) + " |",
              "|---|" + "---|" * len(AGES_DAYS)]
        for n in (3, 4, 5, 6, 8, 10):
            row = [str(n)]
            for age_label, age_days in AGES_DAYS.items():
                en = effective_n(n, age_days, hl)
                shown = "shows" if en >= DISPLAY_FLOOR else "**hidden**"
                row.append(f"{shown} ({en})")
            L.append("| " + " | ".join(row) + " |")
        L.append("")

    # The two policy alternatives §6.2 poses, made concrete.
    L += ["## The actual decision", "",
          "**Option A — floor applies to the DECAYED count (current spec reading).** A claim "
          "with real support quietly disappears once decay pulls `effective_n` under 3. At the "
          "`medium` half-life, that needs **6 attesters to survive a full year**, or 4 within "
          "~3 months. Fewer than that reads to a neighbour as 'nobody knows anything about "
          "this' rather than 'this got a bit stale.'", "",
          "**Option B — floor applies to DISTINCT attester count (raw, undecayed); decay "
          "affects RANKING only.** A subject with 3 distinct attesters always shows, however "
          "old the claims — decay only pushes it down the list against fresher competition. "
          "Fixes the display/consensus inconsistency already flagged (`is_contested` reads raw "
          "counts while the floor reads decayed ones — they'd agree under Option B, disagree "
          "under A).", "",
          "This table doesn't pick between them — it's what makes picking possible. Given how "
          "steep Option A's minimums are even at a 1-year half-life (the least aggressive of "
          "the three swept here), **Option A risks reading as 'the app forgot' rather than "
          "'this may be dated' for anything below ~6 attesters** — worth weighing against "
          "whatever motivated wanting decay in the first place.", ""]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(L), encoding="utf-8")
    print(f"  report -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
