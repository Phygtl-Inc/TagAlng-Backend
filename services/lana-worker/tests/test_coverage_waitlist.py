"""Out-of-coverage state + waitlist capture (QA 2026-07-08: 12/12 metro moms dead-ended).

A valid ZIP with no block must get the honest out-of-coverage reply (never "try 32827",
never a ZIP re-ask — the given ZIP persists on the session), offer the waitlist, and a
join must persist the row with the ZIP + what she was looking for.
"""

import unittest
from unittest.mock import patch

from app.activity_browse import (
    activity_browse_should_release,
    run_activity_browse_turn,
)
from app.ui_actions import activity_browse_actions


class TestOutOfCoverage(unittest.TestCase):
    @patch("app.discovery_route.fetch_blocks_for_zip", return_value=[])
    def test_unresolved_zip_gets_out_of_coverage_not_try_32827(self, _fetch) -> None:
        ctx: dict = {
            "activity_browse_active": True,
            "browse_draft": {"_asked": True, "interest": "walk buddy", "_need_zip": True},
        }
        reply = run_activity_browse_turn(
            user_message="10025",
            session_ctx=ctx,
            history=[],
            user_jwt="jwt",
            home_block_id=None,
        )
        self.assertIn("not on your block yet", reply.lower())
        self.assertNotIn("32827", reply)  # never point her at someone else's ZIP
        self.assertNotIn("couldn't find a block", reply.lower())
        # The ZIP she gave is persisted — the amnesia fix.
        self.assertEqual(ctx.get("coverage_zip"), "10025")
        self.assertTrue((ctx.get("browse_draft") or {}).get("_coverage_offer"))

    @patch("app.discovery_route.fetch_blocks_for_zip", return_value=[])
    def test_following_turn_never_reasks_zip(self, _fetch) -> None:
        # Offer pending, she replies with something that's neither a tap nor a ZIP —
        # the session keeps the ZIP and she is NOT asked for it again.
        ctx: dict = {
            "activity_browse_active": True,
            "coverage_zip": "10025",
            "browse_draft": {"_asked": True, "interest": "walk buddy", "_coverage_offer": True},
        }
        reply = run_activity_browse_turn(
            user_message="that's disappointing",
            session_ctx=ctx,
            history=[],
            user_jwt="jwt",
            home_block_id=None,
        )
        self.assertNotIn("what's your zip", reply.lower())
        self.assertEqual(ctx.get("coverage_zip"), "10025")

    @patch("app.discovery_route.fetch_blocks_for_zip", return_value=[])
    def test_lane_reentry_uses_remembered_zip_no_reask(self, _fetch) -> None:
        # She comes back to browsing later in the session: the remembered (uncovered) ZIP
        # resolves straight to the out-of-coverage state — never "What's your ZIP?" again.
        ctx: dict = {
            "activity_browse_active": True,
            "coverage_zip": "10025",
            "browse_draft": {"_asked": True},
        }
        reply = run_activity_browse_turn(
            user_message="story time",
            session_ctx=ctx,
            history=[],
            user_jwt="jwt",
            home_block_id=None,
        )
        self.assertNotIn("what's your zip", reply.lower())
        self.assertIn("not on your block yet", reply.lower())
        self.assertNotIn("32827", reply)

    def test_offer_reply_never_releases_the_lane(self) -> None:
        # "Join the waitlist" is a tap on OUR pill — it must stay in-flow even when the
        # classifier reads it off-lane.
        self.assertFalse(
            activity_browse_should_release(
                "Join the waitlist",
                {"activity_browse_active": True, "browse_draft": {"_coverage_offer": True}},
                {"goal": "peers", "confidence": 0.9},
            )
        )


class TestWaitlistJoin(unittest.TestCase):
    @patch("app.db.save_coverage_waitlist", return_value=True)
    def test_join_persists_zip_and_looking_for(self, mock_save) -> None:
        ctx: dict = {
            "activity_browse_active": True,
            "coverage_zip": "10025",
            "browse_draft": {"_asked": True, "interest": "walk buddy", "_coverage_offer": True},
        }
        reply = run_activity_browse_turn(
            user_message="Join the waitlist",
            session_ctx=ctx,
            history=[],
            user_jwt="jwt",
            home_block_id=None,
            user_id="u-1",
        )
        mock_save.assert_called_once_with(
            user_id="u-1", zip_code="10025", looking_for="walk buddy"
        )
        self.assertIn("on the list for 10025", reply)
        self.assertTrue(ctx.get("coverage_waitlisted"))
        self.assertFalse(ctx.get("activity_browse_active"))

    @patch("app.discovery_route.fetch_blocks_for_zip", return_value=[])
    @patch("app.db.save_coverage_waitlist", return_value=True)
    def test_already_waitlisted_gets_status_not_a_second_offer(self, _save, _fetch) -> None:
        ctx: dict = {
            "activity_browse_active": True,
            "coverage_zip": "10025",
            "coverage_waitlisted": True,
            "browse_draft": {"_asked": True},
        }
        reply = run_activity_browse_turn(
            user_message="anything for kids?",
            session_ctx=ctx,
            history=[],
            user_jwt="jwt",
            home_block_id=None,
        )
        self.assertIn("already on the waitlist", reply.lower())
        self.assertNotIn("what's your zip", reply.lower())


class TestZipValidation(unittest.TestCase):
    @patch("app.discovery_route.fetch_blocks_for_zip")
    def test_99999_rejected_before_lookup(self, mock_fetch) -> None:
        ctx: dict = {
            "activity_browse_active": True,
            "browse_draft": {"_asked": True, "interest": "sports", "_need_zip": True},
        }
        reply = run_activity_browse_turn(
            user_message="99999",
            session_ctx=ctx,
            history=[],
            user_jwt="jwt",
            home_block_id=None,
        )
        self.assertIn("typo", reply.lower())
        mock_fetch.assert_not_called()
        # Not treated as out-of-coverage — she's re-asked for a real ZIP.
        self.assertIsNone(ctx.get("coverage_zip"))
        self.assertTrue((ctx.get("browse_draft") or {}).get("_need_zip"))

    @patch("app.discovery_route.fetch_blocks_for_zip")
    def test_00000_rejected_before_lookup(self, mock_fetch) -> None:
        ctx: dict = {
            "activity_browse_active": True,
            "browse_draft": {"_asked": True, "interest": "sports", "_need_zip": True},
        }
        reply = run_activity_browse_turn(
            user_message="00000",
            session_ctx=ctx,
            history=[],
            user_jwt="jwt",
            home_block_id=None,
        )
        self.assertIn("typo", reply.lower())
        mock_fetch.assert_not_called()


class TestCoverageUiActions(unittest.TestCase):
    def test_coverage_offer_pills(self) -> None:
        rows = activity_browse_actions(
            {"browse_draft": {"_coverage_offer": True}, "activity_browse_active": True}
        )
        labels = [r["label"] for r in rows]
        self.assertEqual(labels, ["Join the waitlist", "Keep looking around"])


if __name__ == "__main__":
    unittest.main()
