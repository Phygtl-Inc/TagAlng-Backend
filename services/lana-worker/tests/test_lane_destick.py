"""De-stickying: the capture lanes (look_meet, activity_browse, event_host) must
re-decide intent every turn — continue ONLY on a genuine answer/refine/confirm, and
release (never trap) on a pivot, an abandon, or a low-confidence read."""

import unittest

from app.activity_browse import activity_browse_should_release as browse_release
from app.discovery_route import _is_host_answer
from app.lane_decision import is_confident_foreign, lane_should_continue
from app.look_meet import look_meet_should_release as look_release


def _slots(goal="", linear=None, signal=None, conf=0.0, abandon=False):
    return {
        "goal": goal,
        "linear_intent": linear,
        "signal_intent": signal,
        "confidence": conf,
        "abandon": abandon,
    }


class LaneShouldContinueTests(unittest.TestCase):
    def test_empty_and_cancel_stay(self):
        self.assertTrue(lane_should_continue("", {}, None, is_valid_answer=lambda *a: False))
        self.assertTrue(lane_should_continue("nvm", {}, None, is_valid_answer=lambda *a: False))

    def test_abandon_releases_even_if_answer_shaped(self):
        self.assertFalse(
            lane_should_continue(
                "maybe later", {}, _slots(abandon=True), is_valid_answer=lambda *a: True
            )
        )

    def test_default_is_release(self):
        # Not empty, not cancel, not abandon, not a valid answer -> RELEASE (the inversion).
        self.assertFalse(
            lane_should_continue("who lives near me", {}, _slots("peers", conf=0.9),
                                 is_valid_answer=lambda *a: False)
        )

    def test_valid_answer_stays(self):
        self.assertTrue(
            lane_should_continue("sports", {}, _slots(conf=0.2), is_valid_answer=lambda *a: True)
        )

    def test_confident_foreign_requires_confidence_and_concrete_goal(self):
        self.assertFalse(is_confident_foreign(_slots("peers", conf=0.3), foreign_goals=frozenset({"peers"})))
        self.assertFalse(is_confident_foreign(_slots("continue", conf=0.9), foreign_goals=frozenset({"peers"})))
        self.assertTrue(is_confident_foreign(_slots("peers", conf=0.9), foreign_goals=frozenset({"peers"})))


class ActivityBrowseReleaseTests(unittest.TestCase):
    def test_refine_stays(self):
        # "show me cricket instead" is a re-filter, not a pivot.
        self.assertFalse(
            browse_release("show me cricket instead", {"browse_draft": {"interest": "any"}},
                           _slots("activities", "discovery.find_activities", conf=0.9))
        )

    def test_vague_low_confidence_answer_stays(self):
        self.assertFalse(
            browse_release("any", {"browse_draft": {"_asked": True}}, _slots("continue", conf=0.3))
        )

    def test_confident_pivot_to_peers_releases(self):
        self.assertTrue(
            browse_release("actually who lives near me", {"browse_draft": {"interest": "any"}},
                           _slots("peers", "discovery.find_peers", conf=0.9))
        )

    def test_switch_to_being_matched_releases(self):
        # meet_seek is foreign to browse so the user can leave browsing to be matched.
        self.assertTrue(
            browse_release("find me a tennis partner", {"browse_draft": {"interest": "any"}},
                           _slots("save_signal", signal="meet_seek", conf=0.9))
        )


class LookMeetReleaseTests(unittest.TestCase):
    def test_p1_answer_stays(self):
        # No kind captured yet — an activity-shaped reply IS the "what kind?" answer.
        self.assertFalse(
            look_release("playground meet", {}, _slots("activities", "discovery.find_activities", conf=0.9))
        )

    def test_confident_pivot_releases(self):
        self.assertTrue(
            look_release("show me dads instead", {"look_draft": {"kind": "x"}},
                         _slots("peers", "discovery.find_peers", conf=0.9))
        )

    def test_new_kind_after_kind_set_releases(self):
        self.assertTrue(
            look_release("actually a coffee meetup", {"look_draft": {"kind": "playground"}},
                         _slots("activities", "discovery.find_activities", conf=0.9))
        )

    def test_detail_refine_stays(self):
        self.assertFalse(
            look_release("make it saturday", {"look_draft": {"kind": "playground"}},
                         _slots("continue", conf=0.4))
        )

    def test_abandon_releases(self):
        self.assertTrue(
            look_release("maybe later", {"look_draft": {"kind": "x"}}, _slots(abandon=True))
        )

    def test_seed_turn_never_releases(self):
        # The "A meet or playgroup" button just entered the flow (look_meet_skip_seed) with a
        # generic payload. The classifier mis-reads "looking for a meet" as find_peers, but
        # the button is an explicit lane choice — the seed turn must NOT release.
        self.assertFalse(
            look_release(
                "I'm looking for a meet or playgroup",
                {"look_meet_active": True, "look_meet_skip_seed": True},
                _slots("peers", "discovery.find_peers", conf=0.9),
            )
        )

    def test_ready_card_non_confirm_releases(self):
        self.assertTrue(
            look_release("what's the weather", {"look_ready": True, "look_draft": {"kind": "x"}},
                         _slots("chat", conf=0.2))
        )

    def test_ready_card_confirm_stays(self):
        self.assertFalse(
            look_release("start listening", {"look_ready": True, "look_draft": {"kind": "x"}}, _slots())
        )


class HostAnswerTests(unittest.TestCase):
    """The event-host lane must treat a step answer as an answer — not a pivot — even when
    the classifier confidently mis-reads a venue name as find_activities or a capacity chip
    as off-lane. Otherwise the flow releases mid-build and drops the resolved date/draft."""

    def test_venue_name_on_where_step_is_an_answer(self):
        # Asked "where?", no venue yet — a place name reads as find_activities but IS the
        # answer to the where-step, so it must not release host mode.
        ctx = {"event_draft": {"title": "Move night with fam"}, "event_place_asked": True}
        self.assertTrue(
            _is_host_answer(
                "South Econ Community Park", ctx,
                _slots("activities", "discovery.find_activities", conf=0.9),
            )
        )

    def test_capacity_chip_is_an_answer(self):
        ctx = {
            "event_draft": {"title": "Move night", "venue_name": "South Econ Park"},
            "event_place_asked": True,
            "event_cap_asked": True,
        }
        self.assertTrue(
            _is_host_answer("Open · no limit", ctx, _slots("activities", conf=0.9))
        )

    def test_title_step_reply_is_an_answer(self):
        # No title yet — a name that merely reads as goal="activities" (no concrete foreign
        # action) IS the title and must stay.
        self.assertTrue(_is_host_answer("Move night with fam", {}, _slots("activities", conf=0.9)))

    def test_title_step_pivot_to_search_releases(self):
        # No title yet, but the user confidently pivots to searching ("I wanna search a
        # meet" -> meet_seek / find_activities). The naming step must NOT swallow this as the
        # title — the AI's concrete-action read releases the lane (no-sticky-flows).
        self.assertFalse(
            _is_host_answer(
                "I wanna search a meet", {},
                _slots("activities", "discovery.find_activities", signal="meet_seek", conf=0.9),
            )
        )

    def test_title_step_abandon_releases(self):
        # No title yet, the AI flags abandon ("i dont wanna host anything", no replacement).
        # _is_host_answer itself doesn't read abandon — the release gate does — but the lane
        # decision must release on the AI's abandon flag, so it never traps at the name step.
        self.assertFalse(
            lane_should_continue(
                "i dont wanna host anything", {}, _slots(abandon=True),
                is_valid_answer=_is_host_answer,
            )
        )

    def test_meta_question_is_not_an_answer(self):
        ctx = {"event_draft": {"title": "Move night"}, "event_place_asked": True}
        self.assertFalse(_is_host_answer("what's my zip?", ctx, _slots("chat", conf=0.9)))

    def test_confident_pivot_on_when_step_releases(self):
        # Title set, not yet on the where-step (collecting when) — a confident pivot to
        # peers is NOT an answer, so the lane releases.
        ctx = {"event_draft": {"title": "Move night"}}
        self.assertFalse(
            _is_host_answer(
                "actually who lives near me", ctx,
                _slots("peers", "discovery.find_peers", conf=0.9),
            )
        )


if __name__ == "__main__":
    unittest.main()
