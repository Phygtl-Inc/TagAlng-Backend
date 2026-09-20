"""Reach and the intro offer — prod 2026-09-16 (session 524eb2c3…).

Lana showed Tommaso five neighbours and offered to introduce him to one of them.
He said "Yes" twice and then "introduce me to Daniel". All three turns raised and
were never answered, and no intro was created. Three separate defects:

  1. the featured peer was the only one of the five the send RPC refused
     (15.5 km away, no shared place) — the card offered what the tap can't do;
  2. `user_not_on_same_block` had no handler, so the refusal killed the turn
     instead of costing a sentence;
  3. the offer armed `pending_intro_offer`, then the lane's full list overwrote
     ctx["peer_matches"] and drop_stale_intro_offer cleared the offer on its own
     seed turn — so the "yes" had no chip and nothing to bind to.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException

from app.peer_discovery_surface import (  # noqa: E402
    attach_peer_card_actions,
    drop_stale_intro_offer,
    stamp_reachability,
)

# The five rows as prod stored them, in render order.
PEERS = [
    {"peer_user_id": "409b42df", "nickname": "Asjid", "connection": "intro_sent"},
    {"peer_user_id": "c3411690", "nickname": "Daniel"},  # 15.5 km away, no shared place
    {"peer_user_id": "52b9d72d", "nickname": "Daniel"},  # shares a confirmed place
    {"peer_user_id": "92ec6af9", "nickname": "Dom", "connection": "intro_sent"},
    {"peer_user_id": "984f0a98", "nickname": "Jake"},
]
REACH = {
    "c3411690": False,
    "52b9d72d": True,
    "984f0a98": True,
    "409b42df": True,
    "92ec6af9": True,
}


def _rows() -> list[dict]:
    return [dict(p) for p in PEERS]


class ReachStampTest(unittest.TestCase):
    def test_button_only_where_the_send_would_work(self) -> None:
        rows = _rows()
        with mock.patch(
            "app.peer_discovery_surface.peers_can_reach", return_value=REACH
        ):
            stamp_reachability(rows, user_id="db98744e")
        carded = attach_peer_card_actions(rows, phone_verified=True)
        by_id = {r["peer_user_id"]: r for r in carded}
        self.assertNotIn("actions", by_id["c3411690"], "unreachable row kept its Nudge")
        self.assertTrue(by_id["52b9d72d"].get("actions"))
        self.assertTrue(by_id["984f0a98"].get("actions"))
        # Unchanged: already-connected rows never carried a button.
        self.assertNotIn("actions", by_id["409b42df"])

    def test_lookup_failure_leaves_buttons_alone(self) -> None:
        """Fail-open: an infra blip must not strip a list that would have worked."""
        rows = _rows()
        with mock.patch("app.peer_discovery_surface.peers_can_reach", return_value={}):
            stamp_reachability(rows, user_id="db98744e")
        carded = attach_peer_card_actions(rows, phone_verified=True)
        by_id = {r["peer_user_id"]: r for r in carded}
        self.assertTrue(by_id["c3411690"].get("actions"))

    def test_stamp_is_idempotent(self) -> None:
        rows = [{"peer_user_id": "c3411690", "nickname": "Daniel", "can_nudge": True}]
        with mock.patch(
            "app.peer_discovery_surface.peers_can_reach", return_value=REACH
        ) as lookup:
            stamp_reachability(rows, user_id="db98744e")
        lookup.assert_not_called()
        self.assertTrue(rows[0]["can_nudge"])


class FeaturedPeerTest(unittest.TestCase):
    def test_offer_features_someone_who_can_be_reached(self) -> None:
        from app.discovery_route import _maybe_attach_intro_offer

        rows = _rows()
        with mock.patch(
            "app.peer_discovery_surface.peers_can_reach", return_value=REACH
        ):
            stamp_reachability(rows, user_id="db98744e")
        ctx: dict = {}
        with mock.patch(
            "app.discovery_route.peer_matches_identity_snippet", return_value=True
        ), mock.patch(
            "app.discovery_route.build_match_reason", return_value="You both: Interested in sports"
        ), mock.patch(
            "app.intro_proposal.compose_reply", side_effect=lambda **kw: kw["fallback"]
        ):
            _maybe_attach_intro_offer(
                reply="…", peers=rows, ctx=ctx, identity_snippet="sports", msg="show me all"
            )
        offer = ctx.get("pending_intro_offer")
        self.assertIsInstance(offer, dict)
        self.assertEqual(
            offer["candidate_user_id"], "52b9d72d", "pitched the unreachable neighbour"
        )


class OfferSurvivesItsOwnTurnTest(unittest.TestCase):
    def test_offer_kept_when_armed_this_turn(self) -> None:
        ctx = {
            "pending_intro_offer": {"candidate_user_id": "52b9d72d"},
            "intro_offer_armed_now": True,
        }
        drop_stale_intro_offer(ctx, _rows())
        self.assertIsNotNone(ctx.get("pending_intro_offer"))

    def test_offer_from_an_earlier_turn_still_dropped(self) -> None:
        """The 2026-08-18 bug stays fixed: no armed-now flag, list moved on."""
        ctx = {"pending_intro_offer": {"candidate_user_id": "52b9d72d"}}
        drop_stale_intro_offer(ctx, _rows())
        self.assertIsNone(ctx.get("pending_intro_offer"))


class RefusalAnswersTest(unittest.TestCase):
    def test_unreachable_candidate_replies_instead_of_raising(self) -> None:
        import app.intro_proposal as ip

        def refuse(_jwt, _name, _payload):
            raise HTTPException(status_code=400, detail="user_not_on_same_block")

        for msg in ("yes", "introduce me to Daniel"):
            with self.subTest(msg=msg), mock.patch.object(
                ip, "call_rpc", refuse
            ), mock.patch(
                "app.auth.jwt_user_id", lambda *_: "db98744e"
            ), mock.patch(
                "app.peer_discovery_surface.peer_tiers", lambda *a, **k: {}
            ), mock.patch.object(
                ip, "compose_reply", side_effect=lambda **kw: kw["fallback"]
            ):
                out = ip.try_propose_intro_from_preview(
                    msg=msg,
                    session_ctx={},
                    user_jwt="jwt",
                    peers=[{"peer_user_id": "c3411690", "nickname": "Daniel"}],
                    identity_snippet=None,
                    force=True,
                )
            self.assertIsNotNone(out)
            reply, payload = out
            self.assertEqual(payload["status"], "unreachable")
            self.assertIn("Daniel", reply)

    def test_consent_withdrawn_also_answers(self) -> None:
        import app.intro_proposal as ip

        def refuse(_jwt, _name, _payload):
            raise HTTPException(status_code=400, detail="candidate_consent_missing")

        with mock.patch.object(ip, "call_rpc", refuse), mock.patch(
            "app.auth.jwt_user_id", lambda *_: "db98744e"
        ), mock.patch(
            "app.peer_discovery_surface.peer_tiers", lambda *a, **k: {}
        ), mock.patch.object(
            ip, "compose_reply", side_effect=lambda **kw: kw["fallback"]
        ):
            out = ip.try_propose_intro_from_preview(
                msg="yes",
                session_ctx={},
                user_jwt="jwt",
                peers=[{"peer_user_id": "c3411690", "nickname": "Daniel"}],
                identity_snippet=None,
                force=True,
            )
        self.assertIsNotNone(out)
        self.assertIn("introductions", out[0])


if __name__ == "__main__":
    unittest.main()
