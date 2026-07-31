"""Unit floor for the decide_turn policy plumbing — the pure logic only
(guardrail regex + fallback, NextAction parsing, defer bookkeeping, summary
stride math). LLM and DB paths are exercised by shadow-run diffing, not here."""

import unittest

from app.lingo_guard import GuardResult, find_violations, naive_clean
from app.policy.decide import (
    NextAction, apply_defer, ask_streak, note_ask_streak, parse_next_action,
)


class TestLingoGuardRegex(unittest.TestCase):
    def test_clean_text_has_no_violations(self) -> None:
        self.assertEqual(find_violations("Want me to set up a Sunday badminton meet?"), [])

    def test_banned_lexicon_detected(self) -> None:
        hits = find_violations("There are a couple of moms on your block in your circle")
        self.assertIn("moms", hits)
        self.assertIn("block", hits)
        self.assertIn("circle", hits)

    def test_person_match_detected_but_verb_match_legal(self) -> None:
        self.assertTrue(find_violations("I found a match for you"))
        self.assertTrue(find_violations("I matched you with Jess"))
        self.assertEqual(find_violations("a time that matches your schedule"), [])

    def test_gamification_words(self) -> None:
        self.assertTrue(find_violations("You're #2 on the leaderboard — keep your streak!"))

    def test_multilingual_mom_forms(self) -> None:
        self.assertTrue(find_violations("¡Hola mamá!"))
        self.assertTrue(find_violations("uma mamãe perto de você"))

    def test_naive_clean_removes_every_banned_word(self) -> None:
        dirty = "There are moms on your block — join the circle, level up your streak!"
        self.assertEqual(find_violations(naive_clean(dirty)), [])

    def test_guard_result_audit_shapes(self) -> None:
        self.assertEqual(GuardResult(text="hi").audit_dict(), {"rail": "clean"})
        bad = GuardResult(text="hi", ok=False, hits=["moms"], rewritten=True)
        self.assertEqual(bad.audit_dict()["rail"], "violation")
        self.assertEqual(bad.audit_dict()["hits"], ["moms"])


class TestNextActionParse(unittest.TestCase):
    def test_valid_action_parses(self) -> None:
        action = parse_next_action(
            {
                "kind": "bridge_offer",
                "utterance": "Want me to set something up?",
                "chips": [{"label": "Yes, set it up", "send": "Yes, set it up"}],
                "goal_id": "cap:sharing.host",
                "why": "interest stated; hosting available",
            }
        )
        self.assertIsNotNone(action)
        assert action is not None
        self.assertEqual(action.kind, "bridge_offer")
        self.assertEqual(len(action.chips), 1)
        self.assertEqual(action.goal_id, "cap:sharing.host")

    def test_handoff_allows_empty_utterance(self) -> None:
        action = parse_next_action({"kind": "handoff", "utterance": "", "why": "peer search"})
        self.assertIsNotNone(action)
        assert action is not None
        self.assertEqual(action.kind, "handoff")

    def test_non_handoff_requires_utterance(self) -> None:
        self.assertIsNone(parse_next_action({"kind": "reply", "utterance": ""}))

    def test_unknown_kind_rejected(self) -> None:
        self.assertIsNone(parse_next_action({"kind": "explode", "utterance": "hi"}))
        self.assertIsNone(parse_next_action("not a dict"))

    def test_chips_capped_at_three_and_labels_required(self) -> None:
        action = parse_next_action(
            {
                "kind": "reply",
                "utterance": "hi",
                "chips": [
                    {"label": f"c{i}", "send": f"s{i}"} for i in range(5)
                ] + [{"label": ""}],
            }
        )
        assert action is not None
        self.assertEqual(len(action.chips), 3)

    def test_routing_dict_carries_why(self) -> None:
        action = NextAction(kind="reply", utterance="hi", why="low signal")
        routing = action.routing_dict()
        self.assertEqual(routing["outcome"], "decide_turn")
        self.assertEqual(routing["why"], "low signal")

    def test_pending_action_kept_on_ground_place(self) -> None:
        action = parse_next_action(
            {
                "kind": "ground_place",
                "utterance": "Which club do they play at?",
                "pending_action": "host_meet",
            }
        )
        assert action is not None
        self.assertEqual(action.pending_action, "host_meet")
        self.assertEqual(action.routing_dict()["pending_action"], "host_meet")

    def test_pending_action_dropped_on_other_kinds_and_noise(self) -> None:
        # Only a grounding ask may carry a live intent…
        action = parse_next_action(
            {"kind": "bridge_offer", "utterance": "hi", "pending_action": "host_meet"}
        )
        assert action is not None
        self.assertIsNone(action.pending_action)
        # …and only the dispatchable kinds count.
        action = parse_next_action(
            {"kind": "ground_place", "utterance": "which?", "pending_action": "explode"}
        )
        assert action is not None
        self.assertIsNone(action.pending_action)


class TestDeferBookkeeping(unittest.TestCase):
    def test_capture_defer_parks_goal(self) -> None:
        ctx: dict = {}
        action = NextAction(kind="capture_defer", utterance="Got it — weekdays.",
                            defer_goal_id="circle:gym")
        apply_defer(ctx, action)
        self.assertEqual(ctx["deferred_goal_ids"], ["circle:gym"])
        apply_defer(ctx, action)  # idempotent
        self.assertEqual(ctx["deferred_goal_ids"], ["circle:gym"])

    def test_non_defer_kinds_do_nothing(self) -> None:
        ctx: dict = {}
        apply_defer(ctx, NextAction(kind="reply", utterance="hi"))
        self.assertNotIn("deferred_goal_ids", ctx)

    def test_deferred_list_capped(self) -> None:
        ctx = {"deferred_goal_ids": [f"g{i}" for i in range(10)]}
        apply_defer(ctx, NextAction(kind="capture_defer", utterance="ok",
                                    defer_goal_id="g_new"))
        self.assertEqual(len(ctx["deferred_goal_ids"]), 10)
        self.assertEqual(ctx["deferred_goal_ids"][-1], "g_new")


class TestAskStreak(unittest.TestCase):
    """note_ask_streak — the don't-stack-asks annoyance-guard input."""

    def test_ask_kinds_extend_the_streak(self) -> None:
        ctx: dict = {}
        note_ask_streak(ctx, NextAction(kind="ask_gap", utterance="Mornings or evenings?"))
        self.assertEqual(ask_streak(ctx), 1)
        note_ask_streak(ctx, NextAction(kind="ground_place", utterance="Which spot?"))
        self.assertEqual(ask_streak(ctx), 2)

    def test_non_ask_kinds_clear_with_none_not_pop(self) -> None:
        ctx: dict = {"policy_ask_streak": 3}
        note_ask_streak(ctx, NextAction(kind="reply", utterance="No worries."))
        # cleared with None (never popped) — the session merge resurrects popped keys
        self.assertIn("policy_ask_streak", ctx)
        self.assertIsNone(ctx["policy_ask_streak"])
        self.assertEqual(ask_streak(ctx), 0)

    def test_ask_streak_survives_garbage(self) -> None:
        self.assertEqual(ask_streak({"policy_ask_streak": "junk"}), 0)
        self.assertEqual(ask_streak({"policy_ask_streak": -4}), 0)
        self.assertEqual(ask_streak({}), 0)


class TestGoalNormalization(unittest.TestCase):
    def test_deferred_goals_marked(self) -> None:
        from unittest.mock import patch

        world = {"circles": [{"key": "gym", "type": "fitness", "grounded": False,
                              "confirmed": True}], "states": []}
        with patch("app.policy.goals._rapport_goals", return_value=[]), \
             patch("app.policy.goals._offer_goals", return_value=[]), \
             patch("app.policy.goals._pending_ask_goals", return_value=[]), \
             patch("app.policy.goals._capability_goals", return_value=[]):
            from app.policy.goals import candidate_goals

            goals = candidate_goals("u1", world, deferred_goal_ids=["circle:gym"])
        self.assertEqual(len(goals), 1)
        self.assertEqual(goals[0]["kind"], "ungrounded_circle")
        self.assertTrue(goals[0]["context"]["deferred_earlier"])

    def test_grounded_circle_becomes_an_offerable_goal(self) -> None:
        """A pinned community must stay in the goal list as something to OFFER —
        when it dropped out, a vague "I'm bored" got the capability catalog
        instead of an offer (QA 2026-07-31)."""
        from app.policy.goals import _circle_offer_goals

        goals = _circle_offer_goals(
            {
                "circles": [
                    {"key": "grandkids", "type": "family", "grounded": True,
                     "confirmed": True, "place": "Lake Nona Park"},
                    # ungrounded → the grounding ask owns it, not an offer
                    {"key": "gym_friends", "type": "fitness", "grounded": False,
                     "confirmed": True, "place": None},
                    # suggested-but-unconfirmed → not a community yet
                    {"key": "squash_group", "type": "sport", "grounded": True,
                     "confirmed": False, "place": "Life Time"},
                ]
            }
        )
        self.assertEqual([g["id"] for g in goals], ["circle_offer:grandkids"])
        goal = goals[0]
        self.assertEqual(goal["kind"], "circle_offer")
        # Their own word for the group, and the real place, both reach the policy.
        self.assertIn("grandkids", goal["summary"])
        self.assertIn("Lake Nona Park", goal["summary"])
        # The accept-send must stand alone: the host engine re-reads it cold.
        self.assertEqual(
            goal["context"]["send"],
            "help me host a get-together for my grandkids at Lake Nona Park",
        )

    def test_circle_offer_send_survives_a_missing_place_name(self) -> None:
        from app.policy.goals import _circle_offer_goals

        goals = _circle_offer_goals(
            {"circles": [{"key": "squash_group", "grounded": True,
                          "confirmed": True, "place": None}]}
        )
        self.assertEqual(
            goals[0]["context"]["send"], "help me host a get-together for my squash group"
        )
        self.assertIsNone(goals[0]["context"]["place_name"])

    def test_place_names_batches_one_read_and_tolerates_gaps(self) -> None:
        from unittest.mock import patch

        seen: dict = {}

        class _Res:
            data = [{"id": "p1", "name": "Lake Nona Park"}, {"id": "p2", "name": None}]

        class _Table:
            def select(self, *_a, **_k):
                return self

            def in_(self, _col, ids):
                seen["ids"] = ids
                return self

            def execute(self):
                return _Res()

        class _Client:
            def table(self, *_a, **_k):
                return _Table()

        rows = [{"place_ref": "p1"}, {"place_ref": "p2"}, {"place_ref": None}, {}]
        with patch("app.policy.world.service_client", return_value=_Client()):
            from app.policy.world import _place_names

            names = _place_names(rows)
        self.assertEqual(seen["ids"], ["p1", "p2"])  # one batched read, no None
        self.assertEqual(names["p1"], "Lake Nona Park")
        self.assertEqual(names["p2"], "")  # falsy → world_state maps it to None

    def test_capability_containment(self) -> None:
        from unittest.mock import patch

        rows = [
            {"capability_id": "sharing.host", "capability_name": "Host",
             "description": "Host a meet", "required_state": [], "surface_priority": 6},
            {"capability_id": "discovery.find_peers", "capability_name": "Find",
             "description": "Find people", "required_state": ["zip_open"],
             "surface_priority": 7},
        ]

        class _Res:
            data = rows

        class _Table:
            def select(self, *_a, **_k):
                return self

            def eq(self, *_a, **_k):
                return self

            def execute(self):
                return _Res()

        class _Client:
            def table(self, *_a, **_k):
                return _Table()

        with patch("app.policy.world.service_client", return_value=_Client()):
            from app.policy.world import capabilities_available

            closed = capabilities_available({"states": ["verified"]})
            open_ = capabilities_available({"states": ["verified", "zip_open"]})
        self.assertEqual([c["capability_id"] for c in closed], ["sharing.host"])
        self.assertEqual(
            sorted(c["capability_id"] for c in open_),
            ["discovery.find_peers", "sharing.host"],
        )


if __name__ == "__main__":
    unittest.main()
