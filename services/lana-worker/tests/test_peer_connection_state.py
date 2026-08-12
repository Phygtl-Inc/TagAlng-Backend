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


if __name__ == "__main__":
    unittest.main()
