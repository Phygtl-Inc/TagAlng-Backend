"""Don't offer a nudge to someone the user already knows.

The tier lives in user_relationships and is promoted on nudge/accept, but no peer-search
path reads it — they filter blocked users only. So Lana re-offered an intro to Tim days
after Pouya had nudged, connected and messaged him, and the send then bounced off the
7-day per-pair cooldown with a bare "try again".
"""

import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service")

from app.peer_discovery_surface import (  # noqa: E402
    attach_peer_card_actions,
    drop_connected_peers,
    stamp_connection_state,
)

_ME = "me-1"


def _rows():
    return [
        {"peer_user_id": "tim", "nickname": "Tim"},
        {"peer_user_id": "ana", "nickname": "Ana"},
        {"peer_user_id": "bo", "nickname": "Bo"},
    ]


def _sb_with_tiers(tiers: dict[str, str]):
    sb = MagicMock()
    sb.rpc.return_value.execute.return_value = MagicMock(
        data=[{"other_user_id": k, "tier": v} for k, v in tiers.items()]
    )
    return sb


class TestPeerConnectionState(unittest.TestCase):
    def _stamp(self, tiers):
        rows = _rows()
        with patch("app.auth.service_client", return_value=_sb_with_tiers(tiers)):
            stamp_connection_state(rows, user_id=_ME)
        return {r["peer_user_id"]: r.get("connection") for r in rows}

    def test_connected_tiers_are_marked(self):
        got = self._stamp({"tim": "acquaintance", "ana": "irl_peer", "bo": "stranger"})
        self.assertEqual(got["tim"], "connected")
        self.assertEqual(got["ana"], "connected")
        self.assertIsNone(got["bo"])

    def test_a_pending_nudge_reads_as_intro_sent(self):
        self.assertEqual(self._stamp({"tim": "nudge"})["tim"], "intro_sent")

    def test_a_just_sent_intro_is_not_overwritten(self):
        rows = [{"peer_user_id": "tim", "nickname": "Tim", "connection": "intro_sent"}]
        with patch("app.auth.service_client", return_value=_sb_with_tiers({"tim": "acquaintance"})):
            stamp_connection_state(rows, user_id=_ME)
        self.assertEqual(rows[0]["connection"], "intro_sent")

    def test_connected_rows_get_no_nudge_action(self):
        rows = [
            {"peer_user_id": "tim", "nickname": "Tim", "connection": "connected"},
            {"peer_user_id": "bo", "nickname": "Bo"},
        ]
        out = attach_peer_card_actions(rows, phone_verified=True)
        self.assertEqual(out[0].get("actions"), None)
        self.assertTrue(out[1]["actions"], "an unconnected neighbour still gets a nudge")

    def test_lookup_failure_leaves_cards_untouched(self):
        rows = _rows()
        sb = MagicMock()
        sb.rpc.side_effect = RuntimeError("postgrest down")
        with patch("app.auth.service_client", return_value=sb):
            stamp_connection_state(rows, user_id=_ME)
        self.assertTrue(all(r.get("connection") is None for r in rows))


class TestDropConnectedPeers(unittest.TestCase):
    """Marking the card was not enough: the row still reached Lana's prose, so she
    pitched an intro to someone already connected. Candidates are filtered at source."""

    def _drop(self, tiers):
        with patch("app.auth.service_client", return_value=_sb_with_tiers(tiers)):
            out = drop_connected_peers(_rows(), user_id=_ME)
        return [r["peer_user_id"] for r in out]

    def test_connected_peers_are_not_candidates(self):
        got = self._drop({"tim": "acquaintance", "ana": "irl_peer", "bo": "stranger"})
        self.assertEqual(got, ["bo"])

    def test_a_pending_nudge_still_shows(self):
        # One is out awaiting a reply — the card labels it intro_sent rather than hiding
        # the neighbour, so the user can see the ball is in the other court.
        self.assertIn("tim", self._drop({"tim": "nudge", "ana": "stranger", "bo": "stranger"}))

    def test_an_exhausted_search_goes_empty_rather_than_offering_a_known_peer(self):
        # Pouya's case: the only candidate was Tim, already connected. Returning [] lets
        # the real "nobody new yet" branch fire instead of an intro offer that can't send.
        with patch("app.auth.service_client", return_value=_sb_with_tiers({"tim": "acquaintance"})):
            self.assertEqual(
                drop_connected_peers([{"peer_user_id": "tim", "nickname": "Tim"}], user_id=_ME), []
            )

    def test_fails_open_when_the_lookup_breaks(self):
        sb = MagicMock()
        sb.rpc.side_effect = RuntimeError("postgrest down")
        with patch("app.auth.service_client", return_value=sb):
            out = drop_connected_peers(_rows(), user_id=_ME)
        self.assertEqual(len(out), 3, "a tier blip must not hide neighbours")

    def test_no_user_id_is_a_passthrough(self):
        self.assertEqual(len(drop_connected_peers(_rows(), user_id=None)), 3)


class TestIntroSendPreflight(unittest.TestCase):
    def test_intro_to_a_connected_peer_never_reaches_the_rpc(self):
        """The RPC would nudge (still strangers to it) and bounce off the 7-day pair
        cooldown, which reads to the user as 'try again in a moment' — for a week."""
        from app.intro_proposal import propose_neighbor_intro

        with (
            patch("app.auth.service_client", return_value=_sb_with_tiers({"tim": "acquaintance"})),
            patch("app.auth.jwt_user_id", return_value=_ME),
            patch("app.intro_proposal.call_rpc") as rpc,
        ):
            out = propose_neighbor_intro(
                "jwt", candidate_user_id="tim", match_reason="You both play badminton nearby."
            )
        rpc.assert_not_called()
        self.assertEqual(out["status"], "duplicate")

    def test_a_stranger_still_gets_the_intro(self):
        from app.intro_proposal import propose_neighbor_intro

        with (
            patch("app.auth.service_client", return_value=_sb_with_tiers({"bo": "stranger"})),
            patch("app.auth.jwt_user_id", return_value=_ME),
            patch("app.intro_proposal.call_rpc", return_value={"intro_id": "i-1"}) as rpc,
        ):
            out = propose_neighbor_intro(
                "jwt", candidate_user_id="bo", match_reason="You both play badminton nearby."
            )
        rpc.assert_called_once()
        self.assertEqual(out["intro_id"], "i-1")


if __name__ == "__main__":
    unittest.main()
