"""A confident "start a community" pick is never rewritten into hosting a meet.

Prod 2026-10-09: "I want to start a community: Podcasters Orlando … who meet at the
Orlando Public Library" — the model said sharing.community but also set the legacy
signal_intent=host_meet, and the hosting reconciler (plus its "meet at" regex) turned the
turn into a host-a-meet. Measured live: 0/3 → 3/3 after this fix.
"""

from __future__ import annotations

from app.layer1_intents import enrich_slots

MSG = (
    "I want to start a community: Podcasters Orlando, part of Podcaster. Local podcasters "
    "who meet at the Orlando Public Library"
)


def _ai(**kw):
    base = {"linear_intent": "sharing.community", "goal": "save_signal",
            "signal_intent": "host_meet", "confidence": 0.95}
    base.update(kw)
    return base


def test_confident_community_pick_survives_a_stray_host_meet() -> None:
    out = enrich_slots(_ai(), msg=MSG)
    assert out["linear_intent"] == "sharing.community"
    assert out["goal"] == "create_community"
    assert not out.get("signal_intent")


def test_a_hosting_utterance_alone_does_not_flip_it() -> None:
    # This sentence trips the hosting regex (want-verb + "meetup"); the model's confident
    # community pick still wins.
    out = enrich_slots(_ai(signal_intent=None, goal=None),
                       msg="I want to create a community for our weekly book club meetup")
    assert out["linear_intent"] == "sharing.community"
    assert out["goal"] == "create_community"


def test_a_real_host_pick_is_still_hosting() -> None:
    out = enrich_slots({"linear_intent": "sharing.host", "goal": "save_signal",
                        "signal_intent": "host_meet", "confidence": 0.95},
                       msg="I want to host a podcast recording meetup at the library on Saturday")
    assert out["linear_intent"] == "sharing.host"
    assert out["signal_intent"] == "host_meet"


def test_an_unsure_community_pick_still_gets_the_fallback() -> None:
    # Below the confidence bar the reconcilers are a fallback, as before.
    out = enrich_slots(_ai(confidence=0.4), msg="let's host a coffee morning on Sunday")
    assert out["linear_intent"] == "sharing.host"
