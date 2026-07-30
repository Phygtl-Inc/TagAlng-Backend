"""Empty peers search must offer tap-able pills, and the taps must work.

The bug: an attr search with zero matches replied in prose "I can keep an ear out
and text you — or try a broader description" with NO chips and no handler for a
yes. Mirror of the browse lane's seek offer (activity_browse._seek_offer).
"""

import unittest
from unittest.mock import patch

from app.discovery_route import (
    _stamp_peer_seek_offer,
    _try_peer_seek_offer_reply_turn,
)
from app.ui_actions import derive_ui_actions, peer_seek_offer_actions
from app.ui_intent import UI_INTENT_CHAT


class TestPeerSeekOfferChips(unittest.TestCase):
    def test_pills_render_on_chat_turn(self) -> None:
        ctx: dict = {}
        _stamp_peer_seek_offer(ctx, "hiking, photography, cafés")
        actions = derive_ui_actions(ctx, UI_INTENT_CHAT)
        self.assertEqual([a["id"] for a in actions], ["peer_seek_notify", "peer_seek_widen"])
        self.assertEqual(actions[0]["label"], "Yes, notify me")
        self.assertEqual(actions[1]["label"], "Show everyone nearby")

    def test_stamp_arms_pending(self) -> None:
        ctx: dict = {}
        _stamp_peer_seek_offer(ctx, "hikers")
        self.assertEqual(ctx["peer_seek_offer_pending"], {"filter": "hikers"})

    def test_labels_match_builder(self) -> None:
        # derive_ui_actions must return exactly the builder's pills.
        ctx = {"peer_seek_offer": {"filter": "x"}}
        self.assertEqual(derive_ui_actions(ctx, UI_INTENT_CHAT), peer_seek_offer_actions())


class TestPeerSeekOfferReplyTurn(unittest.TestCase):
    def _ctx(self) -> dict:
        return {
            "peer_seek_offer_pending": {"filter": "hiking, photography, cafés"},
            "phone_verified": True,
            "preview_block_id": "b-1",
            "preview_block_label": "Lake Nona",
        }

    def test_no_pending_returns_none(self) -> None:
        self.assertIsNone(
            _try_peer_seek_offer_reply_turn(
                msg="Yes, notify me",
                session_ctx={},
                user_jwt="jwt",
                phone_verified=True,
                home_block_id="b-1",
            )
        )

    def test_unrelated_reply_disarms_and_falls_through(self) -> None:
        ctx = self._ctx()
        out = _try_peer_seek_offer_reply_turn(
            msg="actually, help me host a bbq",
            session_ctx=ctx,
            user_jwt="jwt",
            phone_verified=True,
            home_block_id="b-1",
        )
        self.assertIsNone(out)
        # Disarmed with None (pop would be resurrected by the session-ctx merge).
        self.assertIn("peer_seek_offer_pending", ctx)
        self.assertIsNone(ctx["peer_seek_offer_pending"])

    def test_long_message_with_yes_is_not_swallowed(self) -> None:
        ctx = self._ctx()
        out = _try_peer_seek_offer_reply_turn(
            msg="yes I moved here recently and I also wanted to ask about hosting something",
            session_ctx=ctx,
            user_jwt="jwt",
            phone_verified=True,
            home_block_id="b-1",
        )
        self.assertIsNone(out)
        self.assertIsNone(ctx["peer_seek_offer_pending"])

    @patch("app.look_meet.start_meet_seek_from_interest")
    @patch("app.discovery_route._resolve_block_id_for_turn", return_value="b-1")
    def test_accept_tap_saves_seek(self, _blk, mock_seek) -> None:
        mock_seek.return_value = "✅ You're in — I'm listening."
        ctx = self._ctx()
        out = _try_peer_seek_offer_reply_turn(
            msg="Yes, notify me when someone like that joins",
            session_ctx=ctx,
            user_jwt="jwt",
            phone_verified=True,
            home_block_id="b-1",
        )
        self.assertIsNotNone(out)
        reply, turn_ctx, routing, peers = out
        self.assertEqual(reply, "✅ You're in — I'm listening.")
        self.assertEqual(routing["routing_phase"], "listening")
        self.assertIsNone(turn_ctx["peer_seek_offer_pending"])
        self.assertEqual(peers, [])
        kwargs = mock_seek.call_args.kwargs
        self.assertEqual(kwargs["interest"], "hiking, photography, cafés")
        self.assertEqual(kwargs["block_id"], "b-1")

    @patch("app.discovery_route.fetch_preview_peers_on_block", return_value=[])
    @patch("app.discovery_route._fetch_verified_peer_matches", return_value=[])
    @patch("app.discovery_route._resolve_block_id_for_turn", return_value="b-1")
    def test_widen_tap_shows_neighbors(self, _blk, mock_verified, mock_preview) -> None:
        ctx = self._ctx()
        out = _try_peer_seek_offer_reply_turn(
            msg="Show everyone nearby",
            session_ctx=ctx,
            user_jwt="jwt",
            phone_verified=True,
            home_block_id="b-1",
        )
        self.assertIsNotNone(out)
        reply, turn_ctx, routing, peers = out
        self.assertTrue(mock_verified.called)
        self.assertTrue(mock_preview.called)
        self.assertIsNone(turn_ctx["peer_seek_offer_pending"])
        self.assertEqual(routing["tool_to_call"], "peer_seek_widened")

    @patch("app.discovery_route._fetch_verified_peer_matches")
    @patch("app.discovery_route._resolve_block_id_for_turn", return_value="b-1")
    def test_widen_with_matches_lists_them(self, _blk, mock_verified) -> None:
        mock_verified.return_value = [{"nickname": "Maria", "similarity_score": 0.8}]
        ctx = self._ctx()
        out = _try_peer_seek_offer_reply_turn(
            msg="widen the search",
            session_ctx=ctx,
            user_jwt="jwt",
            phone_verified=True,
            home_block_id="b-1",
        )
        self.assertIsNotNone(out)
        _reply, _turn_ctx, _routing, peers = out
        self.assertEqual(len(peers), 1)


class TestEmptyAttrReplyMentionsPills(unittest.TestCase):
    def test_fallback_copy_offers_both_options(self) -> None:
        # No-LLM fallback must still promise exactly what the pills do.
        with patch(
            "app.layer1_handlers.compose_reply", side_effect=lambda **kw: kw["fallback"]
        ):
            from app.layer1_handlers import format_attr_peers_reply

            text = format_attr_peers_reply([], filter_text="hikers", partial_summary=None)
        self.assertIn("notify you", text)
        self.assertIn("widen the search", text)


if __name__ == "__main__":
    unittest.main()
