"""/lana/fellows serves the SAME matches as the conversation.

Before this endpoint the fellows screen called find_my_fellows directly, which reads
match_peers_by_claim_vectors_for_user and nothing else — no onion arm (shared place,
exact concepts) and no public+mutual disclosure split. So a faith or sobriety overlap
ranked in Lana's chat cards and was invisible on the screen: one user, two lists. The
endpoint exists to make that impossible, by reusing the chat lane's own fetch + shaper.
"""

import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app.auth import AuthSession
from app.layer1_handlers import peers_to_match_rows
from app.main import FellowsBody, post_fellows

AUTH = "Bearer test-token"


def _auth(*, verified: bool = True, block: str | None = "block-a") -> AuthSession:
    return AuthSession(
        user_id="u-caller", is_anonymous=False, phone_verified=verified, home_block_id=block
    )


def _peer(n: int, *, shared: list[str] | None = None) -> dict:
    return {
        "peer_user_id": f"p-{n}",
        "nickname": f"Peer{n}",
        "avatar_url": None,
        "similarity_score": 0.42,
        "matching_peer_label": "You both: things",
        "shared_labels": shared or [],
        "has_exact_concept_match": bool(shared),
    }


class TestFellowsUsesTheChatLane(unittest.TestCase):
    def _call(self, peers: list[dict], *, auth: AuthSession, limit: int = 12):
        with (
            patch("app.main.verify_auth", return_value=auth),
            patch(
                "app.discovery_route._fetch_verified_peer_matches", return_value=peers
            ) as fetch,
        ):
            res = post_fellows(FellowsBody(limit=limit), authorization=AUTH)
        return res, fetch

    def test_reads_the_blended_fetch_not_a_second_matcher(self) -> None:
        # The whole point: this is the onion-blended fetch the chat lane calls. If this
        # ever routes to find_my_fellows again, the two surfaces diverge in silence.
        res, fetch = self._call([_peer(1)], auth=_auth())
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(fetch.call_args.kwargs["user_id"], "u-caller")
        self.assertEqual(len(res.fellows), 1)

    def test_proven_overlap_badge_rides_out(self) -> None:
        # Three shared claims on a 0.42 cosine: PERFECT FIT is unreachable from the
        # score, so a badge computed anywhere but the shared ladder gets this wrong.
        res, _ = self._call(
            [_peer(1, shared=["Family oriented", "Author talk", "Interactive programs"])],
            auth=_auth(),
        )
        self.assertEqual(res.fellows[0].match_badge, "PERFECT FIT")

    def test_a_full_list_is_not_silently_cut_to_a_chat_card(self) -> None:
        # peers_to_match_rows defaults to 8 (a card's worth). A drawer asking for 12 and
        # receiving 8 reads as "that's everyone", which is a different claim.
        res, _ = self._call([_peer(i) for i in range(12)], auth=_auth(), limit=12)
        self.assertEqual(len(res.fellows), 12)

    def test_limit_is_clamped(self) -> None:
        res, fetch = self._call([_peer(i) for i in range(60)], auth=_auth(), limit=999)
        self.assertEqual(fetch.call_args.kwargs["limit"], 40)
        self.assertEqual(len(res.fellows), 40)

    def test_unverified_caller_is_flagged_not_stonewalled(self) -> None:
        # Everyone has a session (guests are signed in anonymously), so the gate on this
        # surface is verification, not auth. The rows are REAL matches with identities
        # withheld — the caller needs to know that, or the card can only guess from row
        # shape whether it is looking at "no matches" or "matches you may not see".
        res, _ = self._call([_peer(1)], auth=_auth(verified=False))
        self.assertTrue(res.requires_phone_verification)
        self.assertEqual(len(res.fellows), 1)

    def test_verified_caller_is_not_flagged(self) -> None:
        res, _ = self._call([_peer(1)], auth=_auth())
        self.assertFalse(res.requires_phone_verification)

    def test_unverified_caller_gets_no_identities(self) -> None:
        # The verify gate lives in the shaper, so the endpoint inherits it rather than
        # re-deciding — the RPC had its own copy of this rule.
        res, _ = self._call([_peer(1)], auth=_auth(verified=False))
        self.assertIsNone(res.fellows[0].nickname)
        self.assertIsNone(res.fellows[0].peer_user_id)
        self.assertTrue(res.fellows[0].preview)

    def test_no_home_block_is_an_error_not_an_empty_list(self) -> None:
        # The UI has a card for this that asks for their area; [] would read as
        # "no neighbours near you" and send them nowhere.
        with (
            patch("app.main.verify_auth", return_value=_auth(block=None)),
            patch("app.discovery_route._fetch_verified_peer_matches") as fetch,
        ):
            with self.assertRaises(HTTPException) as caught:
                post_fellows(FellowsBody(), authorization=AUTH)
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(caught.exception.detail, "home_block_missing")
        fetch.assert_not_called()


class TestAffinitiesSurvive(unittest.TestCase):
    """The shared claims reach the caller as strings, and agree with the badge.

    The shaper called enrich_peer_match_row and read ONLY match_badge off it, so the
    composed label and the per-claim tags were computed and thrown away. Prod row,
    2026-08-28: badge "STRONG" (two shared claims) beside matching_peer_label
    "Enjoys sports" and trait_tags []. The badge and the label described different
    things, and a client wanting the affinities had nothing to read.
    """

    def test_shared_claims_come_back_as_strings(self) -> None:
        rows = peers_to_match_rows(
            [_peer(1, shared=["Enjoys sports", "Badminton every Tuesday"])],
            phone_verified=True,
        )
        self.assertEqual(rows[0]["trait_tags"], ["Enjoys sports", "Badminton every Tuesday"])

    def test_label_and_badge_describe_the_same_overlap(self) -> None:
        shared = ["Enjoys sports", "Badminton every Tuesday"]
        row = peers_to_match_rows([_peer(1, shared=shared)], phone_verified=True)[0]
        self.assertEqual(row["match_badge"], "STRONG")  # two shared claims
        # Both of them, not one — the label is the joined truth behind the badge.
        for claim in shared:
            self.assertIn(claim, row["matching_peer_label"])

    def test_a_row_with_nothing_shared_keeps_its_raw_label(self) -> None:
        # enrich leaves the label alone when it has no caller-side claim to stand on;
        # the fallback must not blank it out.
        row = peers_to_match_rows([_peer(1)], phone_verified=True)[0]
        self.assertEqual(row["matching_peer_label"], "You both: things")


class TestWireIsLean(unittest.TestCase):
    def _route(self):
        from app.main import app as fastapi_app

        return next(
            r for r in fastapi_app.routes if getattr(r, "path", "") == "/lana/fellows"
        )

    def test_unset_row_fields_are_not_serialized(self) -> None:
        # match_stars/match_band, the tip_* and group_* rec fields, distance_text and
        # membership are structurally unreachable here — they belong to other peer
        # surfaces sharing PeerMatchRow. They must not ship as ~12 nulls per row.
        self.assertTrue(self._route().response_model_exclude_none)

    def test_concept_slug_never_ships(self) -> None:
        # No client renders it, and the slug is the one field redaction doesn't cover.
        self.assertEqual(
            self._route().response_model_exclude,
            {"fellows": {"__all__": {"matching_peer_concept"}}},
        )


class TestShaperRowCap(unittest.TestCase):
    def test_default_still_a_card_worth(self) -> None:
        rows = peers_to_match_rows([_peer(i) for i in range(20)], phone_verified=True)
        self.assertEqual(len(rows), 8)

    def test_cap_is_raisable(self) -> None:
        rows = peers_to_match_rows(
            [_peer(i) for i in range(20)], phone_verified=True, max_rows=12
        )
        self.assertEqual(len(rows), 12)


if __name__ == "__main__":
    unittest.main()


class TestAttrSearchVerifyGateOpensTheEmailStep(unittest.TestCase):
    """An unverified trait search must ASK for the email, not just mention it.

    QA 2026-08-28: tapping the fellows card's "Tell Lana your ZIP" posted a peers ask
    that read as a trait search; Lana answered "Verify your email first" and the FE
    showed no email field, because the gate stamped phase="listening" — which derives
    ui_intent=chat. Every other verify gate sets a phase that maps to collect_email.
    """

    def test_gate_phase_derives_the_email_intent(self) -> None:
        from app.discovery_route import PHASE_GATE_VERIFY
        from app.ui_intent import UI_INTENT_COLLECT_EMAIL, _PHASE_TO_INTENT

        self.assertEqual(_PHASE_TO_INTENT.get(PHASE_GATE_VERIFY), UI_INTENT_COLLECT_EMAIL)

    def test_listening_would_not_have(self) -> None:
        # The bug, stated: the phase it used to send carries no auth step at all.
        from app.ui_intent import UI_INTENT_COLLECT_EMAIL, _PHASE_TO_INTENT

        self.assertNotEqual(_PHASE_TO_INTENT.get("listening"), UI_INTENT_COLLECT_EMAIL)


class TestFellowsCommunityFilter(unittest.TestCase):
    """`place_id` narrows the same list to one of the caller's communities."""

    def _call(self, peers, *, members, mine=True, place_id="pl-1"):
        with (
            patch("app.main.verify_auth", return_value=_auth()),
            patch("app.discovery_route._fetch_verified_peer_matches", return_value=peers) as fetch,
            patch(
                "app.community_surface.caller_affiliation_at",
                return_value={"id": "a-1"} if mine else None,
            ),
            patch(
                "app.community_surface._member_rows",
                return_value=[{"user_id": u, "status": "confirmed"} for u in members],
            ),
        ):
            return post_fellows(FellowsBody(place_id=place_id), authorization=AUTH), fetch

    def test_keeps_only_members_of_that_place(self) -> None:
        res, fetch = self._call([_peer(1), _peer(2), _peer(3)], members=["p-2", "u-caller"])
        self.assertEqual([f.peer_user_id for f in res.fellows], ["p-2"])
        # Filtering happens after ranking, so the fetch must go deeper than the page.
        self.assertGreater(fetch.call_args.kwargs["limit"], 12)

    def test_non_member_cannot_filter_by_a_place(self) -> None:
        # Otherwise the filter is a membership oracle for any place id (§F).
        with self.assertRaises(HTTPException) as caught:
            self._call([_peer(1)], members=["p-1"], mine=False)
        self.assertEqual(caught.exception.status_code, 403)

    def test_no_place_id_is_the_unfiltered_list(self) -> None:
        with (
            patch("app.main.verify_auth", return_value=_auth()),
            patch("app.discovery_route._fetch_verified_peer_matches", return_value=[_peer(1)]) as fetch,
            patch("app.community_surface._member_rows") as members,
        ):
            res = post_fellows(FellowsBody(), authorization=AUTH)
        self.assertEqual(len(res.fellows), 1)
        self.assertEqual(members.call_count, 0)
        self.assertEqual(fetch.call_args.kwargs["limit"], 12)
