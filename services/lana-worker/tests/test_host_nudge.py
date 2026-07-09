"""Demand-triggered host nudge — threshold, candidate pick, cap, copy, CTA seeding."""

from __future__ import annotations

import unittest

from app.host_nudge import (
    NEED_THRESHOLD,
    find_host_nudge_candidates,
    group_unmet_needs,
    host_nudge_actions,
    host_nudge_copy,
    pick_candidate_host,
)


def _sig(user_id: str, detail: str, *, category: str | None = None, intent: str = "meet_seek",
         status: str = "listening") -> dict:
    return {
        "user_id": user_id,
        "intent": intent,
        "category": category,
        "detail_text": detail,
        "status": status,
    }


_PARK_SIGNALS = [
    _sig("u1", "a weekday park morning", category="playdate"),
    _sig("u2", "A weekday PARK morning", category="playdate"),
    _sig("u3", "weekday park mornings with toddlers", category="playdate"),
]


class TestGrouping(unittest.TestCase):
    def test_threshold_needs_three_distinct_users(self) -> None:
        self.assertEqual(NEED_THRESHOLD, 3)
        needs = group_unmet_needs(_PARK_SIGNALS)
        self.assertEqual(len(needs), 1)
        self.assertEqual(needs[0]["count"], 3)
        self.assertEqual(needs[0]["user_ids"], ["u1", "u2", "u3"])

    def test_two_moms_is_not_demand(self) -> None:
        self.assertEqual(group_unmet_needs(_PARK_SIGNALS[:2]), [])

    def test_same_mom_three_times_is_not_demand(self) -> None:
        signals = [_sig("u1", "a weekday park morning", category="playdate") for _ in range(3)]
        self.assertEqual(group_unmet_needs(signals), [])

    def test_ignores_non_meet_seek_and_non_listening(self) -> None:
        signals = _PARK_SIGNALS[:2] + [
            _sig("u3", "a weekday park morning", category="playdate", intent="host_meet"),
            _sig("u4", "a weekday park morning", category="playdate", status="closed"),
        ]
        self.assertEqual(group_unmet_needs(signals), [])

    def test_groups_by_detail_when_no_category(self) -> None:
        signals = [_sig(f"u{i}", "  Saturday morning RUN ") for i in range(1, 4)]
        needs = group_unmet_needs(signals)
        self.assertEqual(len(needs), 1)
        self.assertEqual(needs[0]["need_key"], "detail:saturday morning run")


class TestCandidatePick(unittest.TestCase):
    def _need(self) -> dict:
        return {"need_key": "cat:playdate", "need_label": "a weekday park morning",
                "count": 3, "user_ids": ["u1", "u2", "u3"]}

    def test_prefers_matching_interest_then_activity(self) -> None:
        candidates = [
            {"user_id": "u9", "verified": True, "activity_count": 10},  # active, no interest
            {"user_id": "u2", "verified": True, "activity_count": 4},   # wants it herself
            {"user_id": "u1", "verified": True, "activity_count": 2},
        ]
        picked = pick_candidate_host(self._need(), candidates)
        self.assertEqual(picked["user_id"], "u2")

    def test_unverified_never_picked(self) -> None:
        candidates = [{"user_id": "u1", "verified": False, "activity_count": 99}]
        self.assertIsNone(pick_candidate_host(self._need(), candidates))

    def test_seven_day_cap_excludes_recently_nudged(self) -> None:
        candidates = [
            {"user_id": "u2", "verified": True, "activity_count": 4},
            {"user_id": "u1", "verified": True, "activity_count": 2},
        ]
        picked = pick_candidate_host(self._need(), candidates, exclude_user_ids={"u2"})
        self.assertEqual(picked["user_id"], "u1")

    def test_find_candidates_applies_cap_and_threshold(self) -> None:
        candidates = [
            {"user_id": "u1", "verified": True, "activity_count": 5, "nickname": "Marina"},
            {"user_id": "u2", "verified": True, "activity_count": 3, "nickname": "Beatriz"},
        ]
        out = find_host_nudge_candidates(
            "blk1",
            signals=_PARK_SIGNALS,
            candidates=candidates,
            recently_nudged_user_ids={"u1"},
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["host_user_id"], "u2")
        self.assertEqual(out[0]["host_nickname"], "Beatriz")
        self.assertEqual(out[0]["count"], 3)
        # Below threshold → nothing, regardless of candidates.
        self.assertEqual(
            find_host_nudge_candidates(
                "blk1", signals=_PARK_SIGNALS[:2], candidates=candidates,
                recently_nudged_user_ids=set(),
            ),
            [],
        )

    def test_one_nudge_per_host_per_pass(self) -> None:
        signals = _PARK_SIGNALS + [
            _sig(f"u{i}", "a stroller coffee walk", category="coffee") for i in range(4, 7)
        ]
        candidates = [{"user_id": "u1", "verified": True, "activity_count": 5}]
        out = find_host_nudge_candidates(
            "blk1", signals=signals, candidates=candidates, recently_nudged_user_ids=set()
        )
        # Only one candidate host exists — she gets ONE nudge, not one per need.
        self.assertEqual(len(out), 1)


class TestCopyAndCta(unittest.TestCase):
    def test_nudge_copy_matches_strategy_line(self) -> None:
        self.assertEqual(
            host_nudge_copy(3, "a weekday park morning"),
            "Three moms near you want a weekday park morning. Want to host it? "
            "I'll handle invites, RSVPs, and reminders.",
        )
        self.assertTrue(host_nudge_copy(4, "a stroller walk").startswith("Four moms"))
        self.assertTrue(host_nudge_copy(12, "a swim").startswith("12 moms"))

    def test_candidates_carry_the_copy(self) -> None:
        out = find_host_nudge_candidates(
            "blk1",
            signals=_PARK_SIGNALS,
            candidates=[{"user_id": "u1", "verified": True, "activity_count": 1}],
            recently_nudged_user_ids=set(),
        )
        self.assertIn("Three moms near you want", out[0]["copy"])

    def test_accept_cta_seeds_host_flow_with_need_prefilled(self) -> None:
        actions = host_nudge_actions("a weekday park morning")
        accept = actions[0]
        # Tapping posts a plain hosting utterance — the layer-1 classifier routes
        # "I want to host …" to sharing.host/host_meet, entering the existing host flow
        # with the need already in the message.
        self.assertEqual(accept["message"], "I want to host a weekday park morning")
        self.assertEqual(accept["style"], "primary")
        self.assertEqual(actions[1]["message"], "not now")


if __name__ == "__main__":
    unittest.main()
