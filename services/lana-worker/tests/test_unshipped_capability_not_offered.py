"""A switched-off capability never reaches the policy.

Lana offered "I can help you swap or pass along kids gear with other families
nearby" with a "See who's swapping nearby" chip. Swap is not shipped. Three
things had to be true for that to happen, and all three are covered here:

  1. capability_index had both swap rows is_active true (fixed in
     20261006120000 — a data change, nothing to assert here).
  2. suggestion_queue -> _offer_goals never checked is_active, so a retired
     capability could still be pitched from the latent queue.
  3. the policy prompt named swapping as core to Lana, so she improvised it even
     with no capability at all (prompt rule; the live check is
     scratchpad/probe_noswap.py, not runnable in CI without an LLM).
"""

from __future__ import annotations

from app.policy import goals


def _world() -> dict[str, object]:
    return {"states": ["verified", "has_home_zip"], "user": {}, "circles": []}


def test_inactive_capability_is_filtered_from_every_goal_source(monkeypatch) -> None:
    """A latent suggestion for a retired capability must not survive the merge."""
    monkeypatch.setattr(
        goals, "capabilities_available",
        lambda _w: [{"capability_id": "sharing.host"}],
    )
    monkeypatch.setattr(goals, "_rapport_goals", lambda _u: [])
    monkeypatch.setattr(goals, "_grounding_goals", lambda _w: [])
    monkeypatch.setattr(goals, "_circle_offer_goals", lambda _w: [])
    monkeypatch.setattr(goals, "_pending_ask_goals", lambda _u: [])
    monkeypatch.setattr(
        goals, "_offer_goals",
        lambda _u: [
            {"id": "offer:1", "kind": "pending_offer", "summary": "swap the stroller",
             "value_hint": 0.9, "context": {"capability_id": "looking.swap"}},
            {"id": "offer:2", "kind": "pending_offer", "summary": "host something",
             "value_hint": 0.5, "context": {"capability_id": "sharing.host"}},
        ],
    )
    monkeypatch.setattr(
        goals, "_capability_goals",
        lambda _w: [
            {"id": "cap:sharing.host", "kind": "capability", "summary": "host",
             "value_hint": 0.6, "context": {"capability_id": "sharing.host"}},
        ],
    )

    out = goals.candidate_goals("u1", _world())
    ids = {g["id"] for g in out}
    assert "offer:1" not in ids, "a retired capability was still offered from the latent queue"
    assert ids == {"offer:2", "cap:sharing.host"}


def test_goals_without_a_capability_are_untouched(monkeypatch) -> None:
    """Rapport / grounding / pending-ask goals carry no capability_id and must
    pass through — the filter keys on the field being present, not truthy."""
    monkeypatch.setattr(goals, "capabilities_available", lambda _w: [])
    monkeypatch.setattr(
        goals, "_rapport_goals",
        lambda _u: [{"id": "gap:1", "kind": "rapport", "summary": "ask about running",
                     "value_hint": 0.5, "context": {}}],
    )
    for name in ("_grounding_goals", "_circle_offer_goals", "_capability_goals"):
        monkeypatch.setattr(goals, name, lambda _w: [])
    monkeypatch.setattr(goals, "_offer_goals", lambda _u: [])
    monkeypatch.setattr(
        goals, "_pending_ask_goals",
        lambda _u: [{"id": "pending_ask", "kind": "pending_offer", "summary": "x",
                     "value_hint": 0.7, "context": {"ask": {"intent": "tip_seek"}}}],
    )

    ids = {g["id"] for g in goals.candidate_goals("u1", _world())}
    assert ids == {"gap:1", "pending_ask"}


def test_prompt_does_not_advertise_swapping() -> None:
    """The prompt used to open with "you help people meet, host, and swap" — the
    policy pitched swapping from that alone, with no capability listed."""
    from pathlib import Path

    text = Path(__file__).resolve().parents[1].joinpath(
        "prompts", "lana_policy_decide.md"
    ).read_text()
    identity = text.split("## ", 2)[0] + text.split("## ", 2)[1]
    assert "swap" not in identity.lower(), (
        "swapping is advertised in the prompt's identity section but is not shipped"
    )
