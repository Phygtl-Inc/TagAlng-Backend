"""Posting the finished recommendation must be survivable and must actually reach a block.

Dev QA 2026-09-03: a full six-step recommendation (Dr. Thomas, pediatrician) reached the
ready card, "Pass the tip along" answered "I couldn't post that just now" — and the draft
was dropped in the same breath, so there was nothing left to retry and the next message
fell into the old signal cascade. Two defects behind it: the save used the raw home block
instead of resolving the session's, and the failure branch nulled the draft.
"""

from __future__ import annotations

import unittest
from unittest import mock

_DRAFT = {
    "name": "Dr. Thomas",
    "category": "pediatrician",
    "trait": "very good with kids",
    "reco_type": "professional",
    "step_set": [
        {"field": "profession", "label": "Profession", "question": "What do they do?", "kind": "text", "required": True},
        {"field": "helped_with", "label": "Helped with", "question": "What with?", "kind": "text", "required": True},
        {"field": "where", "label": "Where", "question": "Where are they?", "kind": "place", "required": False},
        {"field": "ask_ok", "label": "Neighbours", "question": "Can neighbours ask you more?",
         "kind": "toggle", "options": ["Let them ask", "Keep it to the card"], "required": False},
    ],
    "answers": {
        "profession": "pediatrician",
        "helped_with": "treatment of my kids",
        "where": "lake nona",
        "ask_ok": "Keep it to the card",
    },
}


def _run(ctx, msg="Pass the tip along", *, home_block_id=None, jwt="jwt"):
    from app.tip_share import run_tip_share_turn

    return run_tip_share_turn(
        user_message=msg,
        session_ctx=ctx,
        history=[],
        user_jwt=jwt,
        home_block_id=home_block_id,
    )


class TestPostFailureKeepsTheCard(unittest.TestCase):
    def _ctx(self):
        return {"tip_share_active": True, "tip_ready": True, "tip_draft": dict(_DRAFT)}

    def test_failed_post_keeps_the_draft_and_the_ready_card(self) -> None:
        ctx = self._ctx()
        with mock.patch("app.tip_share._save_tip", return_value=(None, "boom")):
            reply = _run(ctx, home_block_id="blk-1")
        self.assertTrue(ctx["tip_draft"], "the answered draft must survive a failed post")
        self.assertTrue(ctx["tip_ready"])
        self.assertTrue(ctx["tip_share_active"])
        self.assertFalse(ctx.get("tip_listed_now"))
        self.assertIn("Pass the tip along", reply)

    def test_block_required_reassigns_the_home_block_and_retries(self) -> None:
        ctx = self._ctx()
        calls: list[str | None] = []

        def save(*, draft, user_jwt, block_id, zip_code):
            calls.append(block_id)
            if block_id is None:
                return None, "block_required"
            return {"signal_id": "sig-1", "matches_created": 0}, ""

        with mock.patch("app.tip_share._save_tip", side_effect=save), mock.patch(
            "app.discovery_route.ensure_home_block_for_verified_user", return_value="blk-9"
        ):
            _run(ctx)
        self.assertEqual(calls, [None, "blk-9"])
        self.assertTrue(ctx["tip_listed_now"])

    def test_session_block_is_used_when_the_home_block_is_not_loaded(self) -> None:
        ctx = self._ctx()
        ctx["preview_block_id"] = "blk-session"
        seen: list[str | None] = []

        def save(*, draft, user_jwt, block_id, zip_code):
            seen.append(block_id)
            return {"signal_id": "sig-1", "matches_created": 0}, ""

        with mock.patch("app.tip_share._save_tip", side_effect=save):
            _run(ctx)
        self.assertEqual(seen, ["blk-session"])


class TestZipRecovery(unittest.TestCase):
    """block_required is the failure this flow actually hits: an account with no home
    block. The ZIP answer has to assign one and post, not land in the card."""

    def test_zip_answer_assigns_the_block_and_posts_without_a_second_tap(self) -> None:
        ctx = {
            "tip_share_active": True,
            "tip_ready": True,
            "tip_draft": dict(_DRAFT),
            "tip_need_zip": True,
        }
        seen: list[str | None] = []

        def save(*, draft, user_jwt, block_id, zip_code):
            seen.append(block_id)
            if block_id is None:
                return None, "block_required"
            return {"signal_id": "sig-1", "matches_created": 0}, ""

        with mock.patch("app.tip_share._save_tip", side_effect=save), mock.patch(
            "app.discovery_route.ensure_home_block_for_verified_user", return_value="blk-32827"
        ):
            _run(ctx, "32827")
        self.assertEqual(seen, ["blk-32827"])
        self.assertTrue(ctx["tip_listed_now"])
        self.assertEqual(ctx["zip_code"], "32827")
        self.assertIsNone(ctx["tip_need_zip"])

    def test_block_required_arms_the_zip_ask(self) -> None:
        ctx = {"tip_share_active": True, "tip_ready": True, "tip_draft": dict(_DRAFT)}
        with mock.patch(
            "app.tip_share._save_tip", return_value=(None, "block_required")
        ), mock.patch(
            "app.discovery_route.ensure_home_block_for_verified_user", return_value=None
        ):
            _run(ctx)
        self.assertTrue(ctx["tip_need_zip"])
        self.assertTrue(ctx["tip_draft"])


class TestClosingToggle(unittest.TestCase):
    def test_a_no_to_the_consent_step_does_not_release_the_lane(self) -> None:
        from app.tip_share import tip_share_should_release

        ctx = {"tip_share_active": True, "tip_draft": dict(_DRAFT), "tip_pending_ask": "ask_ok"}
        self.assertFalse(
            tip_share_should_release(
                "naah i dont want them to ask me", ctx, {"abandon": True}
            )
        )

    def test_a_real_abandon_on_a_text_step_still_releases(self) -> None:
        from app.tip_share import tip_share_should_release

        ctx = {"tip_share_active": True, "tip_draft": dict(_DRAFT), "tip_pending_ask": "where"}
        self.assertTrue(
            tip_share_should_release("actually find me a plumber", ctx, {"abandon": True})
        )


class TestNeighbourFacingText(unittest.TestCase):
    def test_the_consent_answer_is_not_part_of_what_neighbours_read(self) -> None:
        from app.tip_share import _detail_text

        text = _detail_text(dict(_DRAFT))
        self.assertIn("pediatrician", text)
        self.assertNotIn("Keep it to the card", text)


if __name__ == "__main__":
    unittest.main()
