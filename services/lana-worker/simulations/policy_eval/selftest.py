"""
selftest.py — adversarial self-check: prove the MECHANICAL checks actually FIRE on violations.

A green eval run is meaningless if the checks are vacuous. This feeds each mechanical axis a
NextAction that deliberately violates it and asserts the expected verdict. Runs with no API key,
no server. Run: `python selftest.py` (exit 0 = all detections fire).
"""

from __future__ import annotations

import pathlib

import checks
import lingo_guardrail
from judge import JudgedAxis
from live_policy import map_real_action, world_dict_from
from ports import Chip, Community, NextAction, TurnNote, WorldState
from scenarios import by_id
from world_state import (
    ALWAYS_ON, INACTIVE_CAPABILITIES, available_capabilities, cold_area, live_area,
    rootless_user, unverified_user, warming_area, with_confirmed_circle,
)

FAILURES: list[str] = []


def expect(cond: bool, msg: str) -> None:
    mark = "ok " if cond else "FAIL"
    print(f"  [{mark}] {msg}")
    if not cond:
        FAILURES.append(msg)


def note(msg: str) -> None:
    """Print something that is neither a pass nor a failure but MUST be visible.

    Used for the drift check's blind spots: "I could not verify these N statements" is an
    honest result and has to appear in the output, but it is not a detection that failed.
    """
    print(f"  [note] {msg}")


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

    # 3. THE REGRESSION THIS SUITE EXISTED TO PREVENT AND INSTEAD CAUSED.
    # discovery.find_peers in a CLOSED area used to HARD_FAIL here, because the harness encoded a
    # pre-20261005 capability map. 20261005120000_ungate_discovery_pre_open.sql cleared the
    # {zip_open} gate on looking.meet / discovery.find_peers / discovery.find_activities — so this
    # is CORRECT behaviour and the check was failing Lana for doing the right thing. It must PASS.
    now_ok = NextAction(kind="bridge_offer", tool="discovery.find_peers",
                        utterance="Want me to introduce you to some runners nearby?", why="x")
    expect(verdict_of(checks.run_mechanical_checks(now_ok, sc), "capability_grounding") == "PASS",
           "capability_grounding PASSES discovery.find_peers in a CLOSED area "
           "(post-20261005 the zip_open gate is gone — this used to be a false HARD_FAIL)")
    for cid in ("looking.meet", "discovery.find_activities"):
        act = NextAction(kind="bridge_offer", tool=cid, utterance="Here's one nearby.", why="x")
        expect(verdict_of(checks.run_mechanical_checks(act, sc), "capability_grounding") == "PASS",
               f"capability_grounding PASSES {cid} in a CLOSED area (ungated by 20261005120000)")

    # 3b. ...but the axis is NOT vacuous: an is_active=false capability is still a HARD_FAIL.
    # 20261006120000 switched both swap rows off ("Swap is not shipped. Stop offering it.") after
    # the policy pitched swap in prod. This is the planted violation for that new arm.
    bad = NextAction(kind="bridge_offer", tool="looking.swap",
                     utterance="Want to see who's swapping gear nearby?", why="x")
    expect(verdict_of(checks.run_mechanical_checks(bad, sc), "capability_grounding") == "HARD_FAIL",
           "capability_grounding HARD_FAILs an is_active=false capability (looking.swap)")
    expect(INACTIVE_CAPABILITIES == {"looking.swap", "sharing.swap"}
           and not (ALWAYS_ON & INACTIVE_CAPABILITIES),
           "an inactive capability is never in ALWAYS_ON / available_capabilities")

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

    # 5b. THE COMMUNITY-ASK PAIR (added 2026-08-25). Until scenarios.py grew a community ask,
    # NOTHING in the suite ever offered discovery.communities, so the only live required_state
    # gate in the whole registry was never reached through checks.py — only the availability set
    # was inspected directly. This walks the gate end-to-end, in both directions.
    comm = NextAction(kind="bridge_offer", tool="discovery.communities", why="x",
                      utterance="There are a few spots near you that people here belong to — "
                                "want to see them?",
                      chips=[Chip(label="Show me", action="SEE_MATCHES"),
                             Chip(label="Not now", action="NOT_NOW")])
    expect(verdict_of(checks.run_mechanical_checks(comm, by_id("dq_community_ask_unverified")),
                      "capability_grounding") == "HARD_FAIL",
           "capability_grounding HARD_FAILs discovery.communities for an UNVERIFIED user "
           "(20261028120000's {verified} gate, provoked by a real scenario)")
    for _sid in ("dq_community_ask_verified", "dq_community_ask_warming_zip"):
        expect(verdict_of(checks.run_mechanical_checks(comm, by_id(_sid)),
                          "capability_grounding") == "PASS",
               f"capability_grounding PASSES discovery.communities in {_sid} — no false positive, "
               f"and a warming ZIP must NOT suppress it (communities are never area-gated)")

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
           "chips SOFT_FAILs on >3 chips")

    # The range is 0-3, not §5.2's 2-4 (Asjid ruled 2026-08-25). decide.py:272 truncates at 3, so
    # 4 is unreachable by construction and §5.2 describes a system that cannot exist.
    four = NextAction(kind="bridge_offer", utterance="Lots of options:", why="x",
                      chips=[Chip(label=f"Option {i}", action="CONTINUE") for i in range(4)])
    expect(verdict_of(checks.run_mechanical_checks(four, sc), "chips") == "SOFT_FAIL",
           "chips SOFT_FAILs on exactly 4 — the parser truncates at 3, so 4 can never ship")

    # THE CHANGE THAT WAS COSTING 13 OF 23 LIVE SCENARIOS: §5.2's FLOOR of 2 is gone, so a
    # one-chip offer is no longer failed FOR ITS COUNT. The escape rule is untouched and still
    # applies, so this particular action still SOFT_FAILs — but for the escape, never the count.
    # Asserting on the REASON is the point: a PASS here would silently retire the escape rule,
    # which Asjid explicitly wants kept until typed chip actions ship.
    one = NextAction(kind="bridge_offer", utterance="Want me to set that up?", why="x",
                     chips=[Chip(label="Yes, set it up", action="CREATE_GATHERING")])
    _one_res = next(r for r in checks.run_mechanical_checks(one, sc) if r.name == "chips")
    expect("escape" in _one_res.detail and "2-4" not in _one_res.detail
           and "expects 2" not in _one_res.detail,
           f"a one-chip offer is failed for the missing ESCAPE, not for its count "
           f"(§5.2's floor is gone) — reason: {_one_res.detail!r}")

    # ...and with an escape present, a two-chip offer is clean.
    two = NextAction(kind="bridge_offer", utterance="Want me to set that up?", why="x",
                     chips=[Chip(label="Yes, set it up", action="CREATE_GATHERING"),
                            Chip(label="Not now", action="NOT_NOW")])
    expect(verdict_of(checks.run_mechanical_checks(two, sc), "chips") == "PASS",
           "chips PASSES an offer with an accept chip and a graceful out")

    # ...but §5.2's SUBSTANCE survives: an offer with no chip has no way to accept it.
    none_offer = NextAction(kind="bridge_offer", utterance="Want me to set that up?", why="x")
    expect(verdict_of(checks.run_mechanical_checks(none_offer, sc), "chips") == "SOFT_FAIL",
           "chips SOFT_FAILs an offer carrying NO chip to accept it (the rule that survives)")

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

    # 18. capability_grounding is NON-GATING in live (HTTP) mode (engine tool names aren't capability_ids)
    live_offer = NextAction(kind="bridge_offer", tool="create_event", utterance="Setting that up.", why="")
    expect(verdict_of(checks.run_mechanical_checks(live_offer, sc, backend_kind="live"),
                      "capability_grounding") == "PASS",
           "capability_grounding does NOT gate a live tool name ('create_event')")
    expect(verdict_of(checks.run_mechanical_checks(live_offer, sc, backend_kind="stub"),
                      "capability_grounding") == "HARD_FAIL",
           "capability_grounding STILL HARD_FAILs an unregistered tool in stub mode")

    # --- state-token vocabulary (app/policy/world.py:124-134) ------------------------------
    # 22. Each REAL token is emitted under the REAL condition, and the invented one is gone.
    expect(sorted(live_area().current_state_tokens()) == ["has_home_zip", "verified", "zip_open"],
           "state tokens: an open, verified, ZIP-having user emits verified/has_home_zip/zip_open")
    expect("verified" not in unverified_user().current_state_tokens(),
           "state tokens: no phone AND no email verification -> no `verified` token")
    expect("has_home_zip" not in rootless_user().current_state_tokens(),
           "state tokens: no users.home_zip -> no `has_home_zip` token")
    expect("zip_open" not in cold_area().current_state_tokens()
           and "zip_open" in live_area().current_state_tokens(),
           "state tokens: `zip_open` tracks zip_unlock.unlock_state == 'open'")
    expect("has_circle" not in cold_area().current_state_tokens()
           and "has_circle" in with_confirmed_circle(cold_area()).current_state_tokens(),
           "state tokens: `has_circle` needs a circle_affiliations row with status=='confirmed'")
    # A merely GROUNDED-but-unconfirmed circle must NOT grant has_circle (world.py:134 keys on
    # status, not on the `grounded` boolean) — the near-miss that a lazier check would pass.
    near_miss = cold_area()
    near_miss.communities.append(
        Community(circle_type="gym", place_name="X", grounded=True, confirmed=False))
    expect("has_circle" not in near_miss.current_state_tokens(),
           "state tokens: grounded-but-unconfirmed does NOT grant `has_circle`")
    expect(all("phone_verified" not in w.current_state_tokens()
               for w in (cold_area(), live_area(), unverified_user())),
           "state tokens: the invented `phone_verified` token is gone from every world")
    # THE AVAILABILITY PIN. What used to stand here was
    #     available_capabilities(cold_area()) == available_capabilities(live_area())
    # advertised as catching "a future gate migration". It could not, and 20261028120000 proved
    # it: both fixtures are verified=True and differ ONLY in zip_unlock_state, so a {verified}
    # gate sits on BOTH sides of the equality and cancels out. An availability pin has to vary
    # ONE token at a time. (Case 3b above already covers a hypothetical {has_circle} gate by
    # accident — cold_area() has no confirmed circle. {verified} and {has_home_zip} were the
    # blind spots; {verified} is now a real gate.)
    _COMM = "discovery.communities"
    expect(_COMM not in available_capabilities(unverified_user()),
           "availability EXCLUDES discovery.communities for an UNVERIFIED user "
           "(required_state={verified}, 20261028120000:41) — the containment arm has teeth")
    expect(all(_COMM in available_capabilities(w)
               for w in (live_area(), cold_area(), warming_area())),
           "availability INCLUDES discovery.communities for a VERIFIED user in an open, a CLOSED "
           "and a WARMING ZIP alike — communities are never area-gated (20261028120000:25)")
    expect(available_capabilities(unverified_user())
           == available_capabilities(live_area()) - {_COMM},
           "discovery.communities is the ONLY offer verification changes today — one row, so the "
           "day a second one gains a {verified} gate this line moves")
    # ...and the two dimensions that are still ungated, pinned deliberately instead of implied:
    expect(available_capabilities(cold_area()) == available_capabilities(live_area()),
           "no capability gates on `zip_open` today (20261005120000 cleared all three, and "
           "20261028120000 deliberately did not re-add it) — closed and open availability match")
    expect(available_capabilities(rootless_user()) == available_capabilities(live_area()),
           "no capability gates on `has_home_zip` today — a user with no home ZIP loses no offer")

    # 23. SQL-NULL gender/role semantics: no third enum value, and the legacy spelling degrades
    #     to the NEUTRAL branch rather than asserting a gender the DB check would reject.
    w = WorldState(user_id="u", grammatical_gender="unknown", role="unspecified")  # type: ignore[arg-type]
    expect(w.grammatical_gender is None and w.role is None and w.gender_is_null,
           "ports: legacy 'unknown'/'unspecified' normalise to None (SQL NULL semantics)")
    expect(WorldState(user_id="u", grammatical_gender="feminine").gender_is_null is False,
           "ports: a pinned gender is NOT null (the agree-when-known direction still works)")

    # --- distress: the chip exemption, and the control that proves chips are still checked ---
    # 24. _apply_distress_gate (decide.py:300) CLEARS chips on a distress turn by design, so the
    #     chip rules must not fire there. distress_turn is only observable now that it is real
    #     (decide.py:55) — this is the exemption being planted.
    distress_no_chips = NextAction(kind="reply", distress_turn=True, why="distress; drop the task",
                                   utterance="That sounds really rough. I'm glad you said something.")
    expect(verdict_of(checks.run_mechanical_checks(distress_no_chips, sc), "chips") == "PASS",
           "chips: a chipless DISTRESS turn PASSES (the gate clears chips on purpose)")
    distress_offer = NextAction(kind="bridge_offer", distress_turn=True, why="x",
                                utterance="Take care of yourself first.",
                                chips=[Chip(label=f"Option {i}", action="CREATE_GATHERING")
                                       for i in range(5)])
    expect(verdict_of(checks.run_mechanical_checks(distress_offer, sc), "chips") == "PASS",
           "chips: even a chip-heavy DISTRESS turn is exempt (the whole rule is waived, not part of it)")
    # 25. THE CONTROL. The exact same shapes on a NON-distress turn must still be caught —
    #     otherwise the exemption above would have quietly disabled the chip axis for everyone.
    control_empty = NextAction(kind="bridge_offer", distress_turn=False, why="x",
                               utterance="Want me to set something up?")
    expect(verdict_of(checks.run_mechanical_checks(control_empty, sc), "chips") == "SOFT_FAIL",
           "CONTROL: a chipless OFFER on a non-distress turn still SOFT_FAILs")
    control_many = NextAction(kind="bridge_offer", distress_turn=False, why="x",
                              utterance="Lots of options:",
                              chips=[Chip(label=f"Option {i}", action="CREATE_GATHERING")
                                     for i in range(5)])
    expect(verdict_of(checks.run_mechanical_checks(control_many, sc), "chips") == "SOFT_FAIL",
           "CONTROL: >4 chips with no escape on a non-distress turn still SOFT_FAILs")
    # A distress turn must not become a blanket amnesty either: lingo is still absolute.
    distress_dirty = NextAction(kind="reply", distress_turn=True, why="x",
                                utterance="Oh mom, that sounds hard.")
    expect(verdict_of(checks.run_mechanical_checks(distress_dirty, sc), "lingo") == "HARD_FAIL",
           "CONTROL: a distress turn is exempt from CHIP rules only — lingo still HARD_FAILs")

    # --- fail-closed behaviour of the inproc world seam --------------------------------------
    # 26. World NOT honoured (the default inproc path) -> UNSCORED, and the SCENARIO cannot pass.
    clean = NextAction(kind="bridge_offer", tool="sharing.host", why="x",
                       utterance="Want to set something up nearby?",
                       chips=[Chip(label="Set it up", action="CREATE_GATHERING"),
                              Chip(label="Not now", action="NOT_NOW")])
    unhonoured = TurnNote(world_source="account", observed_state_tokens={"verified", "zip_open"})
    res = checks.run_mechanical_checks(clean, sc, backend_kind="inproc", note=unhonoured)
    expect(verdict_of(res, "world_fidelity") == "UNSCORED",
           "world_fidelity is UNSCORED when decide_turn ran in the account's world, not the scenario's")
    expect(checks.overall_verdict(res) == "UNSCORED",
           "FAIL-CLOSED: an otherwise-perfect turn whose world wasn't honoured never scores PASS")
    # 27. Injected world -> PASS, but loudly labelled harness-supplied.
    injected = TurnNote(world_source="injected",
                        observed_state_tokens=sc.world.current_state_tokens())
    res = checks.run_mechanical_checks(clean, sc, backend_kind="inproc", note=injected)
    expect(verdict_of(res, "world_fidelity") == "PASS"
           and "HARNESS-SUPPLIED" in next(r.detail for r in res if r.name == "world_fidelity"),
           "world_fidelity PASSES an injected world but says HARNESS-SUPPLIED in the detail")
    # 27b. SEEDED world (local stacks only — local_world.apply_world). PASS requires the pin to
    #      be READ BACK successfully through world_state(); a seed that silently did not take
    #      must not present as control, so the mismatch case is UNSCORED, never PASS.
    seeded_ok = TurnNote(world_source="seeded",
                         observed_state_tokens=sc.world.current_state_tokens())
    res = checks.run_mechanical_checks(clean, sc, backend_kind="inproc", note=seeded_ok)
    expect(verdict_of(res, "world_fidelity") == "PASS"
           and "SEEDED" in next(r.detail for r in res if r.name == "world_fidelity"),
           "world_fidelity PASSES a SEEDED world that read back as pinned")
    seeded_bad = TurnNote(world_source="seeded",
                          observed_state_tokens=sc.world.current_state_tokens() | {"zip_open"})
    res = checks.run_mechanical_checks(clean, sc, backend_kind="inproc", note=seeded_bad)
    expect(verdict_of(res, "world_fidelity") == "UNSCORED",
           "world_fidelity is UNSCORED when the seed did NOT take (read-back != pinned)")
    seeded_blind = TurnNote(world_source="seeded", observed_state_tokens=None)
    res = checks.run_mechanical_checks(clean, sc, backend_kind="inproc", note=seeded_blind)
    expect(verdict_of(res, "world_fidelity") == "UNSCORED",
           "world_fidelity is UNSCORED when a seeded world could not be read back at all")
    # 28. decide_turn returned None -> the whole turn is UNSCORED: not PASS, and not a HARD_FAIL
    #     blaming Lana for a turn the legacy path answered out of this harness's sight.
    declined = TurnNote(world_source="account", decision_declined=True)
    res = checks.run_mechanical_checks(NextAction(kind="handoff", utterance=""), sc,
                                       backend_kind="inproc", note=declined)
    expect([r.name for r in res] == ["decision_observable"] and res[0].verdict == "UNSCORED",
           "a declined decision (decide_turn -> None) is a single UNSCORED axis, not an empty-utterance HARD_FAIL")
    # 29. Verdict ordering: UNSCORED must outrank SOFT_FAIL, and HARD_FAIL must outrank UNSCORED.
    cr = checks.CheckResult
    expect(checks.overall_verdict([cr("a", "PASS", ""), cr("b", "SOFT_FAIL", ""),
                                   cr("c", "UNSCORED", "")]) == "UNSCORED",
           "_worst ranks UNSCORED above SOFT_FAIL (an unmeasured axis must not hide in a soft verdict)")
    expect(checks.overall_verdict([cr("a", "UNSCORED", ""), cr("b", "HARD_FAIL", "")]) == "HARD_FAIL",
           "_worst still ranks HARD_FAIL above UNSCORED (a real finding outranks a blind spot)")

    # --- the inproc mapping, exercised without a DB or an LLM --------------------------------
    class _RealAction:  # stand-in for app.policy.decide.NextAction (same attribute names)
        kind = "bridge_offer"
        utterance = "There's a run on Saturday morning — want me to introduce you?"
        chips = [{"label": "Yes, introduce me", "send": "yes please introduce me"},
                 {"label": "", "send": "dropped"}]
        goal_id = "cap:discovery.find_peers"
        defer_goal_id = None
        pending_action = None
        distress_turn = True
        why = "area has supply; an intro is the one best next step"

    mapped = map_real_action(_RealAction())
    expect(mapped.kind == "bridge_offer" and mapped.why.startswith("area has supply"),
           "inproc mapping: `kind` and `why` are REAL (no heuristic, no auto-exemption)")
    expect(mapped.tool == "discovery.find_peers",
           "inproc mapping: tool is recovered from goal_id's 'cap:' prefix (goals.py:381)")
    expect(mapped.distress_turn is True and len(mapped.chips) == 1,
           "inproc mapping: distress_turn carries through; empty-label chips are dropped")

    class _NoGoal(_RealAction):
        goal_id = "rapport:kids"

    expect(map_real_action(_NoGoal()).tool is None,
           "inproc mapping: a non-capability goal_id is tool=None, NOT an invented tool")

    # 30. An injected world must describe the SAME world the checks compare against, or injection
    #     would silently test a different situation than the report claims.
    for w in (cold_area(), live_area(), unverified_user(), rootless_user(),
              with_confirmed_circle(cold_area())):
        expect(set(world_dict_from(w)["states"]) == w.current_state_tokens(),
               f"world_dict_from({w.user_id}).states == the WorldState's own token set")
    expect(world_dict_from(rootless_user())["area"]["state"] is None,
           "world_dict_from: no home ZIP -> no area at all (world.py:113 reads it off users.home_zip)")

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
    # ...and an empty check list with NO error is UNSCORED, not PASS: "we never looked" is not
    # "we looked and it was fine".
    rec = run_eval.RunRecord(scenario=sc, action=NextAction(kind="reply", utterance="hi"),
                             mechanical=[])
    expect(rec.mech_verdict == "UNSCORED",
           "run_eval scores an empty mechanical list as UNSCORED, never PASS")


    # --- capability registry drift vs the MIGRATIONS -------------------------------------------
    # Added 2026-08-24 after `discovery.communities` (20261028120000) landed and every selftest
    # stayed green while capability_grounding would have HARD_FAILed real Lana for offering it.
    # Pinning the harness's own dict only proved the dict equalled itself. The migrations are in
    # the repo, so the real registry is checkable offline.
    import world_state as _ws

    drift = _ws.capability_drift()
    expect(not drift["missing"] and not drift["extra"],
           f"capability registry IDS match the migrations (missing={sorted(drift['missing'])}, "
           f"extra={sorted(drift['extra'])})")

    # THE VALUE ARM (added 2026-08-25). The id arm compares ids ONLY, so the hand-transcribed
    # {"verified"} at world_state.py:91 was load-bearing with nothing checking it — and a wrong
    # value there does not read as an error, it silently mis-gates a real capability.
    expect(not drift["value_mismatch"],
           f"required_state VALUES match the migrations for every id the replay could verify "
           f"(mismatch={drift['value_mismatch']})")
    expect(not drift["deleted_but_registered"],
           f"no registered capability has been DELETED by a migration "
           f"(deleted_but_registered={sorted(drift['deleted_but_registered'])})")
    _replay_real = _ws.capability_required_state_replay()
    expect(_replay_real["verifiable"].get("discovery.communities") == {"verified"},
           "the value arm VERIFIES discovery.communities={verified} straight out of "
           "20261028120000 — the one row in the registry with a live gate")
    expect(set(drift["unverified_values"])
           == set(_ws.REGISTERED_CAPABILITIES) - {"discovery.communities"},
           "exactly the eight pre-20261005 rows are reported UNVERIFIABLE (their last parseable "
           "assignment predates statements the replay refuses to guess at)")
    expect(any("20261005120000" in u for u in drift["unparsed"]),
           "the UNPARSED list names 20261005120000 — its UPDATE keys on capability_name, so the "
           "replay cannot know which ids it touched, and says so instead of guessing")

    # FAIL LOUD, NOT SILENT. Everything the replay could not verify is printed. Swallowing it
    # would turn "8 of 9 values unchecked" into an indistinguishable green.
    note(f"required_state VERIFIED for {len(_replay_real['verifiable'])}/"
         f"{len(_ws.REGISTERED_CAPABILITIES)} ids: "
         + ", ".join(f"{k}={sorted(v)}" for k, v in sorted(_replay_real["verifiable"].items())))
    note(f"required_state NOT verifiable for {len(drift['unverified_values'])} ids "
         f"(hand-replayed only): {', '.join(drift['unverified_values'])}")
    note(f"{len(drift['unparsed'])} migration statement(s) UNPARSED — reported, never assumed clean:")
    for _u in drift["unparsed"]:
        note("      " + _u)

    # Non-vacuity, BOTH directions — a drift check that cannot fail is worse than none.
    _saved = dict(_ws.REGISTERED_CAPABILITIES)
    try:
        _ws.REGISTERED_CAPABILITIES.pop("discovery.communities", None)
        expect("discovery.communities" in _ws.capability_drift()["missing"],
               "...and it FIRES when a real capability is missing from the harness")
        _ws.REGISTERED_CAPABILITIES.update(_saved)
        _ws.REGISTERED_CAPABILITIES["magic.teleport"] = set()
        expect("magic.teleport" in _ws.capability_drift()["extra"],
               "...and it FIRES on an invented capability the migrations never mention")
    finally:
        _ws.REGISTERED_CAPABILITIES.clear()
        _ws.REGISTERED_CAPABILITIES.update(_saved)

    # Non-vacuity of the VALUE arm, both flavours of wrong.
    _saved = dict(_ws.REGISTERED_CAPABILITIES)
    try:
        _ws.REGISTERED_CAPABILITIES["discovery.communities"] = set()
        expect("discovery.communities" in _ws.capability_drift()["value_mismatch"],
               "value arm FIRES when the mirror CLEARS a gate the migration set ({verified} -> {})")
        _ws.REGISTERED_CAPABILITIES["discovery.communities"] = {"zip_open"}
        expect(_ws.capability_drift()["value_mismatch"].get("discovery.communities")
               == (["verified"], ["zip_open"]),
               "value arm FIRES on the WRONG token and reports both sides (real vs ours)")
    finally:
        _ws.REGISTERED_CAPABILITIES.clear()
        _ws.REGISTERED_CAPABILITIES.update(_saved)
    expect(not _ws.capability_drift()["value_mismatch"],
           "...and the real migration set is clean again once the mirror is restored "
           "(the planted violations did not leak)")

    # A rollback recipe inside a SQL COMMENT must not count as a definition — 20261028120000:53
    # contains `--   delete from public.capability_index where capability_id = '...';`
    import tempfile
    with tempfile.TemporaryDirectory() as _d:
        _p = pathlib.Path(_d) / "x.sql"
        _p.write_text(chr(10).join([
            "-- delete from public.capability_index where capability_id = 'ghost.cap';",
            "insert into public.capability_index (capability_id) values ('real.cap');",
        ]), encoding="utf-8")
        _ids = _ws.capability_ids_in_migrations(pathlib.Path(_d))
        expect(_ids == {"real.cap"},
               "commented-out capability ids are NOT read as real (no false drift)")

    # --- the strict parser, on synthetic migration sets -----------------------------------
    # These pin the FP/blind-spot trade-off directly: what it parses, and what it refuses to.
    def _replay(*bodies):
        with tempfile.TemporaryDirectory() as _dd:
            for _i, _b in enumerate(bodies, 1):
                (pathlib.Path(_dd) / f"{_i:03d}_m.sql").write_text(_b, encoding="utf-8")
            return _ws.capability_required_state_replay(pathlib.Path(_dd))

    _SEED = ("insert into public.capability_index "
             "(capability_id, capability_name, description, required_state) "
             "values ('a.one', 'n', 'd', array['verified']::text[]);")

    _r = _replay(_SEED)
    expect(_r["verifiable"] == {"a.one": {"verified"}} and not _r["unparsed"],
           "parser: a plain INSERT with an array['...']::text[] required_state is replayed exactly")

    _r = _replay(_SEED, "update public.capability_index set required_state = '{}'::text[] "
                        "where capability_id = 'a.one';")
    expect(_r["verifiable"] == {"a.one": set()} and not _r["unparsed"],
           "parser: a bare `where capability_id = '...'` UPDATE is replayed (gate cleared)")

    _r = _replay(_SEED, "update public.capability_index set required_state = '{}'::text[] "
                        "where capability_id = 'a.one' "
                        "and (required_state is null or required_state = '{}');")
    expect(not _r["verifiable"] and "a.one" in _r["unverifiable"] and len(_r["unparsed"]) == 1,
           "parser: a GUARDED update is UNPARSED and POISONS the value it could have changed "
           "(the guard may not have fired; guessing either way is the false positive)")

    _r = _replay(_SEED, "update public.capability_index set required_state = '{}'::text[] "
                        "where capability_name in ('n');")
    expect(not _r["verifiable"] and len(_r["unparsed"]) == 1,
           "parser: an update keyed on capability_name is UNPARSED (the id is not in the "
           "statement at all) — this is 20261005120000's shape")

    _r = _replay(_SEED, "delete from public.capability_index where capability_id = 'a.one';")
    expect(_r["deleted"] == {"a.one": 2} and "a.one" not in _r["values"] and not _r["unparsed"],
           "parser: a strict-form DELETE removes the row from the replay")

    # THE DELETION BLIND SPOT, at drift level: the id LITERAL survives inside the DELETE
    # statement, so the loose id scan still calls the registry clean. Only the replay sees it.
    with tempfile.TemporaryDirectory() as _dd:
        (pathlib.Path(_dd) / "001_m.sql").write_text(_SEED, encoding="utf-8")
        (pathlib.Path(_dd) / "002_m.sql").write_text(
            "delete from public.capability_index where capability_id = 'a.one';", encoding="utf-8")
        _saved = dict(_ws.REGISTERED_CAPABILITIES)
        try:
            _ws.REGISTERED_CAPABILITIES.clear()
            _ws.REGISTERED_CAPABILITIES["a.one"] = {"verified"}
            _d2 = _ws.capability_drift(pathlib.Path(_dd))
            expect(not _d2["missing"] and not _d2["extra"]
                   and _d2["deleted_but_registered"] == {"a.one"},
                   "DELETION blindness is closed: the id arm still reads clean (the literal is in "
                   "the DELETE itself) while deleted_but_registered catches it")
        finally:
            _ws.REGISTERED_CAPABILITIES.clear()
            _ws.REGISTERED_CAPABILITIES.update(_saved)

    _r = _replay(_SEED, "create function f() returns int language sql as $$ "
                        "select count(*) from public.capability_index $$;")
    expect(not _r["unparsed"] and _r["verifiable"] == {"a.one": {"verified"}},
           "parser: a READ-ONLY $$ function body over capability_index is not flagged "
           "(20260729120000's matcher must not poison every value)")

    _r = _replay(_SEED, "create function f() returns void language plpgsql as $$ begin "
                        "update public.capability_index set required_state = array['x'] "
                        "where capability_id = 'a.one'; end $$;")
    expect(len(_r["unparsed"]) == 1 and not _r["verifiable"],
           "parser: a WRITE hidden inside a $$ body IS flagged UNPARSED and poisons the value")


    # --- follow_thread is a REAL kind (added 2026-08-25) ---------------------------------------
    # app/policy/decide.py:26 KINDS gained "follow_thread", and decide.py:487 makes it the
    # FIRST-choice revision when a turn would otherwise dead-end. ActionKind is a pydantic
    # Literal, so a kind the real policy emits but this harness lacks does NOT degrade
    # gracefully — construction raises, run_eval scores a backend error, and a CORRECT decision
    # takes a HARD_FAIL. Pin both that the kind is accepted and that expected_kind tolerates it
    # wherever a stored rapport question was already tolerated.
    ft = NextAction(kind="follow_thread", utterance="What got you into pottery?", why="x")
    expect(ft.kind == "follow_thread", "ActionKind accepts follow_thread (decide.py:26 KINDS)")

    _ft_sc = by_id("dq_low_signal_continue")
    expect("follow_thread" not in (_ft_sc.expect_kind or []),
           "a reply-only scenario is NOT widened to follow_thread "
           "(low-signal continue must not turn into a question)")

    _widened = [s for s in __import__("scenarios").ALL_SCENARIOS
                if s.expect_kind and "ask_gap" in s.expect_kind]
    expect(all("follow_thread" in s.expect_kind for s in _widened),
           f"every scenario accepting ask_gap also accepts follow_thread ({len(_widened)} scenario(s)) "
           f"— asking about what they JUST raised is strictly more responsive than a stored question")


    # --- lingo: points/rank scoped to the SCORE FRAME, not banned as bare words ---------------
    # Mirrors app/lingo_guard.py:52-55 (shipped 2026-08-25). Asjid: "the tests assert the
    # negatives as hard as the positives" — a bare-word ban false-positives on ordinary English,
    # and a false positive on a gating axis is the expensive kind.
    for _txt in ("No points here — you're not being scored",
                 "how many points do I have?",
                 "you earned 12 points",
                 "your rank is 4th",
                 "we ranked you higher than your neighbours"):
        _a = NextAction(kind="reply", utterance=_txt, why="x")
        expect(verdict_of(checks.run_mechanical_checks(_a, sc), "lingo") == "HARD_FAIL",
               f"lingo HARD_FAILs the score frame: {_txt!r}")

    for _txt in ("That points to the same spot on the map.",
                 "It's the highest-ranked taco place near you.",
                 "This points you toward your people.",
                 "At this point it's easier to just host one."):
        _a = NextAction(kind="reply", utterance=_txt, why="x")
        expect(verdict_of(checks.run_mechanical_checks(_a, sc), "lingo") == "PASS",
               f"lingo does NOT fire on ordinary English: {_txt!r}")


    # --- empty utterance: HARD_FAIL, except for handoff -----------------------------------------
    # decide.py:267 is explicit: `if kind != "handoff" and not utterance: return None`. So an
    # empty handoff is legal product output. The first inproc run (2026-09-01) HARD_FAILed a real
    # decide_turn handoff on schema AND no_dead_end for exactly this — a false positive against
    # correct behaviour, which is the class this suite exists to avoid.
    _empty_reply = NextAction(kind="reply", utterance="", why="x")
    expect(verdict_of(checks.run_mechanical_checks(_empty_reply, sc), "schema") == "HARD_FAIL",
           "schema still HARD_FAILs an empty utterance on a NON-handoff kind")
    expect(verdict_of(checks.run_mechanical_checks(_empty_reply, sc), "no_dead_end") == "HARD_FAIL",
           "no_dead_end still HARD_FAILs an empty utterance on a NON-handoff kind")

    _empty_handoff = NextAction(kind="handoff", utterance="", why="x")
    expect(verdict_of(checks.run_mechanical_checks(_empty_handoff, sc), "schema") == "PASS",
           "schema PASSES an empty utterance when kind=handoff (decide.py:267 permits it)")
    expect(verdict_of(checks.run_mechanical_checks(_empty_handoff, sc), "no_dead_end") == "PASS",
           "no_dead_end PASSES a handoff — a transfer is not a dead end")

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
