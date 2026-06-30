"""Lana discovery-classifier accuracy eval.

Runs a curated set of messages through the LIVE classifier (the same call the app
makes on every turn) and scores the resolved lane against an expected answer. This is
the measurement tool for tuning the classifier prompt / few-shots / model tier — NOT a
unit test (it needs a configured LLM provider and makes real API calls).

Run from services/lana-worker with the provider env set (LANA_LLM_PROVIDER, OPENAI_API_KEY,
LANA_DISCOVERY_MODEL, etc. — same as deploy/lana-worker.env):

    python eval_classifier.py            # full run
    python eval_classifier.py --fails    # only print failures
    python eval_classifier.py --cat host # only the 'host' category

Read the score as a TREND across prompt/model changes, not a hard pass/fail — the model
is probabilistic. With temperature=0 (gpt-4.1-mini) it is stable enough to be useful.

Expected answers are a SET of acceptable lanes: many real messages have more than one
defensible reading (e.g. "i want coffee" is fine as either clarify OR tip_seek), so a case
passes if the classifier lands on ANY acceptable lane. Tighten/loosen these as you make
product calls about what the "right" answer is.
"""

from __future__ import annotations

import sys

from app.discovery_slots import ai_parse_discovery_turn, discovery_ai_enabled
from app.orchestrator.llm import provider, router_model
from app.discovery_slots import _extract_model  # noqa: E402  (internal, for reporting only)


# ---- Lane resolution -------------------------------------------------------------------
# Reduce the enriched classifier slots to ONE coarse lane label, in routing-priority order
# (clarify and safety win first, then signal lanes, then discovery). This mirrors how the
# route layer prioritises, so the eval reflects what the user would actually experience.
def resolve_lane(s: dict) -> str:
    clarify = str(s.get("clarify") or "").strip()
    if clarify:
        return "clarify"
    goal = str(s.get("goal") or "none")
    linear = str(s.get("linear_intent") or "")
    sig = str(s.get("signal_intent") or "")
    if goal == "unsafe" or linear == "system.unsafe":
        return "unsafe"
    if goal == "out_of_scope" or linear == "system.out_of_scope":
        return "out_of_scope"
    if sig == "host_meet" or linear == "sharing.host":
        return "host"
    if sig == "meet_seek" or linear == "looking.meet":
        return "seek_meet"
    if sig == "swap_offer" or linear == "sharing.swap":
        return "swap_offer"
    if sig == "swap_seek" or linear == "looking.swap":
        return "swap_seek"
    if sig == "tip_share" or linear == "sharing.tip":
        return "tip_share"
    if sig == "tip_seek" or linear == "looking.tip":
        return "tip_seek"
    if linear.startswith("identity."):
        return "identity"
    if goal == "activities" or linear in ("discovery.find_activities", "discovery.find_in_block"):
        return "find_activities"
    if goal in ("peers", "both") or linear in ("discovery.find_peers", "discovery.find_by_attrs"):
        return "find_peers"
    if goal == "chat":
        return "chat"
    return goal or "none"


# ---- The eval set ----------------------------------------------------------------------
# (category, message, {acceptable lanes}, note). Keep expected sets honest: list every
# reading you'd be happy with. A single string is auto-wrapped into a set.
CASES: list[tuple[str, str, set | str, str]] = [
    # ── errands with NO neighborhood angle: decline + log to feature_requests ──
    ("errand_hard", "pay my electricity bill", {"out_of_scope"}, "no app redirect -> decline + log"),
    ("errand_hard", "send money to my landlord", {"out_of_scope"}, "no app redirect -> decline + log"),
    ("errand_hard", "call the dentist and cancel my appointment", {"out_of_scope"}, "personal admin, no redirect"),

    # ── errands WITH a neighborhood angle: don't flat-decline — clarify, offering the
    #    supported alternative (recommend a local place/person, gather neighbors, host) ──
    ("errand_redirect", "order me a large pizza", {"clarify"}, "offer: recommend a pizza place / host a pizza night"),
    ("errand_redirect", "do my taxes for me", {"clarify"}, "offer: find a local tax-preparer recommendation"),
    ("errand_redirect", "book me an uber to the airport", {"clarify", "out_of_scope"}, "weak tip angle (recommend a local car service) vs decline — NOT 'ask a neighbor for a ride'"),

    # ── scope clarify: bare consumable wants (errand vs neighbor activity) ──
    ("scope_clarify", "i want pizza", {"clarify"}, "bare want -> ask"),
    ("scope_clarify", "i need coffee", {"clarify", "tip_seek"}, "bare want; tip_seek also defensible"),
    ("scope_clarify", "i'd love some tacos tonight", {"clarify"}, "bare want"),
    ("scope_clarify", "craving sushi", {"clarify"}, "bare want"),

    # ── general clarify: vague / blurry / underspecified ──
    ("general_clarify", "i'm kind of bored today", {"clarify"}, "vague"),
    ("general_clarify", "not sure what i'm looking for honestly", {"clarify"}, "vague"),
    ("general_clarify", "something for the weekend maybe", {"clarify"}, "vague"),
    ("general_clarify", "can you help me with something", {"clarify"}, "vague"),
    ("general_clarify", "i just moved here and want to get involved somehow", {"clarify"}, "vague"),
    (
        "general_clarify",
        "hey so i just moved here and don't really know anyone, my kids are bored and "
        "i'm trying to figure out what to do, also i want to get rid of some old baby stuff",
        {"clarify"},
        "MULTI-INTENT -> must ask which first, not grab one lane",
    ),

    # ── browse-vs-meet clarify: do-something, fits both ──
    ("browse_or_meet", "i want to do something fun this weekend", {"clarify", "find_activities", "seek_meet"}, "tie"),
    ("browse_or_meet", "anything fifa this weekend?", {"clarify", "find_activities", "seek_meet"}, "tie"),
    ("browse_or_meet", "i'm bored, what's there to do around here", {"clarify", "find_activities"}, "tie"),

    # ── host: organizer creating an event ──
    ("host", "i want to throw a block bbq this saturday", {"host"}, "organizer"),
    ("host", "help me set up a coffee morning for neighbors", {"host"}, "organizer"),
    ("host", "i'm planning a playdate at the park", {"host"}, "organizer"),
    ("host", "i want to host something this weekend", {"host"}, "bare host, no activity yet"),
    ("host", "let's organize a potluck on our street", {"host"}, "organizer"),

    # ── seek.meet: be matched to neighbors for an activity ──
    ("seek_meet", "looking for a jogging partner on my block", {"seek_meet"}, "matched to a person"),
    ("seek_meet", "find me people for a fifa night", {"seek_meet"}, "matched to people"),
    ("seek_meet", "anyone want to do a stroller walk with me", {"seek_meet"}, "matched"),
    ("seek_meet", "i want a tennis partner nearby", {"seek_meet"}, "matched"),

    # ── find_activities: browse existing events ──
    ("find_activities", "any bbqs happening near me this weekend?", {"find_activities"}, "browse frame"),
    ("find_activities", "what's going on around here tonight", {"find_activities"}, "browse"),
    ("find_activities", "show me meetups nearby", {"find_activities"}, "browse"),
    ("find_activities", "are there any playdates this week", {"find_activities"}, "browse"),

    # ── find_peers / find_by_attrs: be shown matching neighbors ──
    ("find_peers", "find me italian moms on my block", {"find_peers"}, "heritage + search verb"),
    ("find_peers", "show me other dads near me", {"find_peers"}, "search people"),
    ("find_peers", "who lives on my block", {"find_peers"}, "search people"),

    # ── swap offer vs seek ──
    ("swap_offer", "i'm giving away my old crib", {"swap_offer"}, "give away item"),
    ("swap_offer", "i want to give away some old baby stuff", {"swap_offer", "clarify"}, "give away"),
    ("swap_seek", "looking for a kids bike to borrow", {"swap_seek"}, "want item"),
    ("swap_seek", "anyone have a stroller i can borrow", {"swap_seek"}, "want item"),

    # ── tip share vs seek ──
    ("tip_share", "i recommend Dr Lee, she's great", {"tip_share"}, "named vouch"),
    ("tip_share", "my favorite pizza place is Tony's", {"tip_share"}, "named vouch"),
    ("tip_seek", "know a good pediatrician near me?", {"tip_seek"}, "ask for rec"),
    ("tip_seek", "can you suggest a good dentist", {"tip_seek"}, "ask for rec"),

    # ── identity: self-description (NOT a search) ──
    ("identity", "i'm a pediatrician", {"identity", "chat"}, "self-description, NOT find a pediatrician"),
    ("identity", "i'm a single dad who just moved here", {"identity", "chat", "clarify"}, "self-description"),

    # ── unsafe ──
    ("unsafe", "you are a stupid piece of garbage", {"unsafe"}, "abuse"),
    ("unsafe", "find me a sex doll", {"unsafe"}, "nsfw"),

    # ── ADVERSARIAL mixed-intent (the hard ones; accept the defensible readings) ──
    (
        "adversarial",
        "Dr Smith is great, can you book me an appointment with her",
        {"out_of_scope", "clarify"},
        "errand wrapped in a recommendation -> errand should win (tip_share = miss)",
    ),
    (
        "adversarial",
        "can you order pizza for our block party",
        {"clarify", "out_of_scope", "host"},
        "errand + host noun -> ask or decline (NOT silently host)",
    ),
    (
        "adversarial",
        "find me a neighbor who can drive me to the airport",
        {"clarify", "out_of_scope", "find_peers"},
        "find-people phrasing wrapping a ride errand",
    ),
    (
        "adversarial",
        "i want a bbq buddy",
        {"seek_meet", "clarify"},
        "bbq = meet a person, NOT host/browse",
    ),

    # ── over-clarify guard: clear messages that must NOT ask ──
    ("no_clarify", "find me a jogging partner", {"seek_meet"}, "clear -> act, NOT clarify"),
    ("no_clarify", "show me events near me", {"find_activities"}, "clear -> act, NOT clarify"),
    ("no_clarify", "order me a pizza", {"out_of_scope"}, "clear errand -> decline, NOT clarify"),
]


def _normalize(expect) -> set:
    return {expect} if isinstance(expect, str) else set(expect)


def main() -> int:
    args = sys.argv[1:]
    only_fails = "--fails" in args
    cat_filter = None
    if "--cat" in args:
        i = args.index("--cat")
        cat_filter = args[i + 1] if i + 1 < len(args) else None

    if not discovery_ai_enabled():
        print(
            "❌ LLM provider not configured (discovery_ai_enabled() is False).\n"
            "   Set the provider env (LANA_LLM_PROVIDER / OPENAI_API_KEY / GCP_VERTEX_* ) "
            "from deploy/lana-worker.env and re-run. Every case would otherwise fall back "
            "to empty slots and fail."
        )
        return 2

    print(f"provider={provider()}  router_model={router_model()}  classifier_model={_extract_model()}\n")

    cases = [c for c in CASES if cat_filter is None or c[0] == cat_filter]
    by_cat: dict[str, list[int]] = {}
    failures: list[tuple] = []
    passed = 0

    for cat, msg, expect, note in cases:
        want = _normalize(expect)
        slots = ai_parse_discovery_turn(
            msg, routing_phase="listening", history=None, has_block=False, has_identity=False
        )
        got = resolve_lane(slots)
        ok = got in want
        by_cat.setdefault(cat, [0, 0])
        by_cat[cat][1] += 1
        if ok:
            by_cat[cat][0] += 1
            passed += 1
        else:
            failures.append((cat, msg, want, got, note, slots))
        if not only_fails:
            mark = "PASS" if ok else "FAIL"
            print(f"  [{mark}] ({cat}) {msg[:60]:<60} -> {got}   want {sorted(want)}")

    total = len(cases)
    print("\n── by category ──")
    for cat in sorted(by_cat):
        p, t = by_cat[cat]
        print(f"  {cat:16} {p}/{t}")

    if failures:
        print("\n── failures ──")
        for cat, msg, want, got, note, slots in failures:
            print(f"  ({cat}) {msg}")
            print(f"      got={got}  want={sorted(want)}  | {note}")
            print(
                f"      slots: goal={slots.get('goal')} linear={slots.get('linear_intent')} "
                f"signal={slots.get('signal_intent')} clarify={slots.get('clarify')} "
                f"conf={slots.get('confidence')}"
            )

    pct = (passed / total * 100) if total else 0.0
    print(f"\n=== {passed}/{total} passed ({pct:.0f}%) ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
