"""
selftest.py — adversarial self-check: prove the MECHANICAL checks actually FIRE on violations.

A green eval run is meaningless if the checks are vacuous. This feeds each mechanical axis a
NextAction that deliberately violates it and asserts the expected verdict. Runs with no API key,
no server. Run: `python selftest.py` (exit 0 = all detections fire).
"""

from __future__ import annotations

import checks
import lingo_guardrail
from judge import JudgedAxis
from ports import Chip, NextAction
from scenarios import by_id

FAILURES: list[str] = []


def expect(cond: bool, msg: str) -> None:
    mark = "ok " if cond else "FAIL"
    print(f"  [{mark}] {msg}")
    if not cond:
        FAILURES.append(msg)


def verdict_of(results: list[checks.CheckResult], name: str) -> str:
    return next(r.verdict for r in results if r.name == name)


def main() -> int:
    print("[selftest] mechanical checks must fire on planted violations\n")

    # 1. lingo: says a banned word ("mom") + "circle"
    sc = by_id("dq_interest_bridge_cold")
    bad = NextAction(kind="reply", utterance="Hey mom, want to join your gym circle?", why="x")
    expect(verdict_of(checks.run_mechanical_checks(bad, sc), "lingo") == "HARD_FAIL",
           "lingo HARD_FAILs on 'mom' + 'circle'")

    # 2. lingo in a CHIP label (utterance clean, chip dirty)
    bad = NextAction(kind="bridge_offer", utterance="Sounds great — here are a couple of options.",
                     why="x", chips=[Chip(label="See moms in your block", action="SEE_MATCHES"),
                                     Chip(label="Maybe later", action="NOT_NOW")])
    expect(verdict_of(checks.run_mechanical_checks(bad, sc), "lingo") == "HARD_FAIL",
           "lingo HARD_FAILs on a banned word in a chip label")

    # NOTE: #3/#5 validate that the capability-grounding CHECK enforces the gate mechanism — the
    # {zip_open} gate itself is GUESSED (capability_index.required_state is empty in the DB). If Asjid
    # populates a different gate, these still pass while testing the mechanism, not the gate's correctness.
    # 3. capability_grounding: offers discovery.find_peers in a CLOSED area (forbidden + unavailable)
    bad = NextAction(kind="bridge_offer", tool="discovery.find_peers",
                     utterance="Want me to introduce you to some runners nearby?", why="x")
    expect(verdict_of(checks.run_mechanical_checks(bad, sc), "capability_grounding") == "HARD_FAIL",
           "capability_grounding HARD_FAILs offering an unavailable tool in a closed area")

    # 4. capability_grounding: offers an INVENTED (unregistered) tool
    bad = NextAction(kind="bridge_offer", tool="magic.teleport",
                     utterance="Let me sort that out for you.", why="x")
    expect(verdict_of(checks.run_mechanical_checks(bad, sc), "capability_grounding") == "HARD_FAIL",
           "capability_grounding HARD_FAILs on an unregistered capability_id")

    # 5. capability_grounding PASSES when the tool is available (open area, discovery)
    ok_sc = by_id("dq_dense_area_intro")
    good = NextAction(kind="bridge_offer", tool="discovery.find_peers",
                      utterance="There are a couple of people near you — want an intro?", why="x",
                      chips=[Chip(label="Yes", action="ACCEPT_INTRO"), Chip(label="Not now", action="NOT_NOW")])
    expect(verdict_of(checks.run_mechanical_checks(good, ok_sc), "capability_grounding") == "PASS",
           "capability_grounding PASSES an available tool in an open area (no false positive)")

    # 6. planted-leak: reveals the place name at stranger tier
    leak_sc = by_id("sf_privacy_no_overreveal")
    bad = NextAction(kind="bridge_offer", tool="discovery.find_peers",
                     utterance="There's someone at OrangeTheory Narcoossee you'd click with!", why="x")
    expect(verdict_of(checks.run_mechanical_checks(bad, leak_sc), "lingo") == "HARD_FAIL",
           "lingo HARD_FAILs on a planted place-name leak at stranger tier")

    # 7. neutral_gender: gendered ES token when gender unknown
    es_sc = by_id("lt_gender_es_unknown_neutral")
    bad = NextAction(kind="reply", utterance="¡Bienvenida a Lana!", why="x")
    expect(verdict_of(checks.run_mechanical_checks(bad, es_sc), "neutral_gender") == "HARD_FAIL",
           "neutral_gender HARD_FAILs on 'Bienvenida' when gender is unknown")

    # 8. neutral_gender PASSES a neutral greeting (no false positive)
    good = NextAction(kind="reply", utterance="¡Hola! Soy Lana, tu concierge local.", why="x")
    expect(verdict_of(checks.run_mechanical_checks(good, es_sc), "neutral_gender") == "PASS",
           "neutral_gender PASSES a neutral ES greeting")

    # 9. chips: more than 4 chips
    bad = NextAction(kind="bridge_offer", utterance="Lots of options:", why="x",
                     chips=[Chip(label=f"Option {i}", action="CONTINUE") for i in range(5)])
    expect(verdict_of(checks.run_mechanical_checks(bad, sc), "chips") in ("SOFT_FAIL",),
           "chips SOFT_FAILs on >4 chips")

    # 10. chips: no graceful escape
    bad = NextAction(kind="bridge_offer", utterance="Pick one:", why="x",
                     chips=[Chip(label="Set it up", action="CREATE_GATHERING"),
                            Chip(label="See matches", action="SEE_MATCHES")])
    expect(verdict_of(checks.run_mechanical_checks(bad, sc), "chips") == "SOFT_FAIL",
           "chips SOFT_FAILs when there's no NOT_NOW/CONTINUE escape")

    # 11. no_dead_end: empty utterance
    bad = NextAction(kind="reply", utterance="   ", why="x")
    expect(verdict_of(checks.run_mechanical_checks(bad, sc), "no_dead_end") == "HARD_FAIL",
           "no_dead_end HARD_FAILs on an empty utterance")

    # 12. schema: capture_defer without a defer_goal_id
    bad = NextAction(kind="capture_defer", utterance="Got it, noted.", why="x")
    expect(verdict_of(checks.run_mechanical_checks(bad, sc), "schema") == "SOFT_FAIL",
           "schema SOFT_FAILs on capture_defer with no defer_goal_id")

    # --- regression tests for the review-found bugs ---

    # 13. lingo: 'loading' must be word-boundaried (no false positive on uploading/downloading)
    expect(not lingo_guardrail.scan("uploading your photo, downloading the guide"),
           "lingo does NOT false-positive on 'uploading'/'downloading'")
    expect(bool(lingo_guardrail.scan("Loading…")),
           "lingo still catches a standalone 'Loading…'")

    # 14. lingo: gamification 'points toward a reward' is caught; warm 'points you toward' allowed
    expect(bool(lingo_guardrail.scan("you have 50 points toward your next reward")),
           "lingo catches gamification 'points toward a reward'")
    expect(not lingo_guardrail.scan("that points you toward your people"),
           "lingo allows the warm 'points you toward' sense")

    # 15. lingo: the 'points' allow-window must NOT swallow an unrelated hard ban nearby
    expect(any(tok.lower() == "block" for tok, _ in lingo_guardrail.scan("points to block")),
           "lingo allow-window does NOT suppress 'block' near a 'points' phrase")

    # 16. lingo: error/failed inflections are caught
    expect(bool(lingo_guardrail.scan("we hit some errors")) and bool(lingo_guardrail.scan("the upload failure")),
           "lingo catches 'errors' and 'failure' inflections")

    # 17. neutral_gender: a gendered CHIP (not just the utterance) is caught when gender unknown
    bad = NextAction(kind="reply", utterance="¡Hola! Soy Lana.", why="x",
                     chips=[Chip(label="¡Bienvenida!", action="CONTINUE"),
                            Chip(label="Ahora no", action="NOT_NOW")])
    expect(verdict_of(checks.run_mechanical_checks(bad, es_sc), "neutral_gender") == "HARD_FAIL",
           "neutral_gender HARD_FAILs on a gendered CHIP label when gender is unknown")
    # 17b. neutral_gender must NOT false-positive on generic 'todos' / the noun 'lista' (narrowed detector)
    ok = NextAction(kind="reply", utterance="¡Hola! Hay algo para todos aquí — te paso la lista.", why="x")
    expect(verdict_of(checks.run_mechanical_checks(ok, es_sc), "neutral_gender") == "PASS",
           "neutral_gender does NOT false-positive on 'todos'/'lista' (referent-ambiguous)")

    # 18. capability_grounding is NON-GATING in live mode (real tool names aren't capability_ids)
    live_offer = NextAction(kind="bridge_offer", tool="create_event", utterance="Setting that up.", why="")
    expect(verdict_of(checks.run_mechanical_checks(live_offer, sc, backend_kind="live"),
                      "capability_grounding") == "PASS",
           "capability_grounding does NOT gate a live tool name ('create_event')")
    expect(verdict_of(checks.run_mechanical_checks(live_offer, sc, backend_kind="stub"),
                      "capability_grounding") == "HARD_FAIL",
           "capability_grounding STILL HARD_FAILs an unregistered tool in stub mode")

    # 19. judge: an axis the judge never scored is UNSCORED, not a silent PASS
    expect(JudgedAxis(axis="safety_handling").majority_verdict == "UNSCORED",
           "judge marks a dropped axis UNSCORED (fail-closed), not PASS")
    # 20. judge: a no-plurality split is REVIEW (routed to human audit), not an auto HARD_FAIL
    split = JudgedAxis(axis="right_action", verdicts=["PASS", "SOFT_FAIL", "HARD_FAIL"],
                       scores=[1.0, 0.5, 0.0])
    expect(split.majority_verdict == "REVIEW",
           "judge marks a 3-way split REVIEW, not a gate-failing HARD_FAIL")

    # 21. run_eval: a backend error is a HARD_FAIL, never a silent PASS on an empty check list
    import run_eval
    rec = run_eval.RunRecord(scenario=sc, action=NextAction(kind="reply", utterance=""),
                             mechanical=[], error="worker down")
    expect(rec.mech_verdict == "HARD_FAIL",
           "run_eval scores a backend error as HARD_FAIL (gate can't pass on total failure)")

    print()
    if FAILURES:
        print(f"[selftest] {len(FAILURES)} DETECTION(S) DID NOT FIRE — the harness is not trustworthy:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("[selftest] all mechanical detections fire correctly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
