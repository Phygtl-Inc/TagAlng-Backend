"""The community filter at the top of the app (app/community_scope.py).

One selection scopes what the user is shown and what they create. The rules that
matter and are easy to break:

  · a place the caller doesn't belong to never becomes the filter (§F);
  · `None` from the client means "unchanged", `""` means "back to my ZIP";
  · a scoped read that finds nothing WIDENS, and says which community was empty —
    it never dead-ends and never widens silently ([[far-supply-honest-empty]]).
"""

import unittest
from unittest.mock import patch

from app.activity_browse import run_activity_browse_turn
from app.community_scope import (
    CTX_KEY,
    active_community_id,
    apply_community_selection,
    events_in_community,
    peers_in_community,
    rows_by_members,
    widen_chip,
)

PLACE = "11111111-1111-1111-1111-111111111111"


class SelectionTests(unittest.TestCase):
    def test_none_keeps_absent_selection_and_empty_string_clears_it(self) -> None:
        ctx: dict = {CTX_KEY: {"place_id": PLACE, "name": "CF Fitness"}}
        # The client said nothing about the filter — the session keeps what it had.
        self.assertEqual(apply_community_selection(ctx, None, user_id="u1")["name"], "CF Fitness")
        self.assertEqual(active_community_id(ctx), PLACE)
        # "" is the explicit "no community" pick: back to the ZIP default.
        self.assertIsNone(apply_community_selection(ctx, "", user_id="u1"))
        self.assertIsNone(active_community_id(ctx))
        # None, not popped — a popped key gets resurrected by the ctx merge.
        self.assertIn(CTX_KEY, ctx)

    def test_membership_is_rechecked_server_side(self) -> None:
        ctx: dict = {}
        with patch("app.community_surface.caller_affiliation_at", return_value=None):
            self.assertIsNone(apply_community_selection(ctx, PLACE, user_id="u1"))
        self.assertIsNone(active_community_id(ctx))
        with patch("app.community_surface.caller_affiliation_at", return_value={"id": "a1"}), patch(
            "app.community_surface._place_row", return_value={"name": "CF Fitness"}
        ):
            comm = apply_community_selection(ctx, PLACE, user_id="u1")
        self.assertEqual(comm, {"place_id": PLACE, "name": "CF Fitness"})


class FilterTests(unittest.TestCase):
    def test_events_belong_by_tag_or_by_venue(self) -> None:
        rows = [
            {"id": "e1", "circle_place_ref": PLACE},
            {"id": "e2", "place_ref": PLACE},  # held there, never tagged
            {"id": "e3", "circle_place_ref": "other"},
            {"id": "e4"},
        ]
        self.assertEqual(
            [e["id"] for e in events_in_community(rows, PLACE)], ["e1", "e2"]
        )

    def test_rows_are_kept_by_author_membership(self) -> None:
        rows = [{"peer_user_id": "u1"}, {"peer_user_id": "u2"}, {}]
        with patch("app.community_surface._member_rows", return_value=[{"user_id": "u2"}]):
            self.assertEqual(rows_by_members(rows, PLACE), [{"peer_user_id": "u2"}])
        # An unreadable / empty roster filters to nothing rather than to everything.
        with patch("app.community_surface._member_rows", return_value=[]):
            self.assertEqual(rows_by_members(rows, PLACE), [])


class PeersTests(unittest.TestCase):
    def test_the_roster_is_the_peer_list_shared_threads_first(self) -> None:
        page = {
            "place_name": "CF Fitness",
            "members": [
                {"peer_user_id": "me", "nickname": "Maria", "me": True},
                {"peer_user_id": "u2", "nickname": "Sam", "trait_tags": []},
                {"peer_user_id": "u3", "nickname": "Ana", "trait_tags": ["running", "kids"]},
                {"peer_user_id": "u4", "nickname": None, "trait_tags": ["x"]},  # no name, no row
            ],
        }
        with patch("app.community_surface.community_members", return_value=page), patch(
            "app.onion_blend._caller_place_tags", return_value={PLACE: "your gym"}
        ):
            rows = peers_in_community("me", PLACE, limit=5)
        self.assertEqual([r["nickname"] for r in rows], ["Ana", "Sam"])
        # The proven fact reads first, and no cosine is invented for the pair.
        self.assertEqual(rows[0]["shared_labels"], ["your gym", "running", "kids"])
        self.assertIsNone(rows[0]["similarity_score"])
        self.assertEqual(rows[0]["community_name"], "CF Fitness")

    def test_a_failed_roster_read_is_empty_not_an_exception(self) -> None:
        with patch("app.community_surface.community_members", side_effect=ValueError("not_a_member")):
            self.assertEqual(peers_in_community("me", PLACE), [])


class BrowseScopeTests(unittest.TestCase):
    """The browse lane reads the community, not the block, when one is selected."""

    def _ctx(self) -> dict:
        return {
            "activity_browse_active": True,
            "browse_draft": None,
            "phone_verified": True,
            CTX_KEY: {"place_id": PLACE, "name": "CF Fitness"},
        }

    @patch("app.auth.jwt_user_id", return_value="me")
    @patch("app.activity_browse._fetch_block_events", return_value=[{"title": "Block BBQ"}])
    @patch(
        "app.community_scope.community_events",
        return_value=[
            {
                "id": "e1",
                "title": "FIFA watch party",
                "starts_at": "2026-06-27T18:00:00",
                "venue_name": "CF Fitness",
                "cohort_tags": [],
            }
        ],
    )
    def test_the_community_calendar_replaces_the_block_read(
        self, _comm_events, block_events, _uid
    ) -> None:
        ctx = self._ctx()
        run_activity_browse_turn(
            user_message="fifa",
            session_ctx=ctx,
            history=[],
            user_jwt="jwt",
            home_block_id="b1",
        )
        block_events.assert_not_called()
        self.assertEqual(
            [p["title"] for p in ctx.get("activity_previews") or []], ["FIFA watch party"]
        )

    @patch("app.auth.jwt_user_id", return_value="me")
    @patch(
        "app.community_scope.community_events",
        return_value=[
            {
                "id": "e1",
                "title": "FIFA watch party",
                "starts_at": "2026-06-27T18:00:00",
                "venue_name": "CF Fitness",
                "cohort_tags": [],
            }
        ],
    )
    def test_a_community_answers_without_a_zip(self, _comm_events, _uid) -> None:
        # The filter already says where. Asking "what's your ZIP?" of someone who just
        # picked a community is the dead end this whole path exists to avoid.
        ctx = self._ctx()
        reply = run_activity_browse_turn(
            user_message="fifa",
            session_ctx=ctx,
            history=[],
            user_jwt="jwt",
            home_block_id=None,
        )
        self.assertNotIn("zip", reply.lower())
        self.assertEqual(
            [p["title"] for p in ctx.get("activity_previews") or []], ["FIFA watch party"]
        )

    @patch("app.auth.jwt_user_id", return_value="me")
    @patch("app.community_scope.community_events", return_value=[])
    def test_an_empty_community_offers_the_way_out_and_the_tap_clears_the_filter(
        self, _comm_events, _uid
    ) -> None:
        ctx = self._ctx()
        reply = run_activity_browse_turn(
            user_message="any fifa nights?",
            session_ctx=ctx,
            history=[],
            user_jwt="jwt",
            home_block_id="b1",
        )
        chip = widen_chip("CF Fitness")
        self.assertIn(chip, (ctx.get("browse_draft") or {}).get("suggestions") or [])
        self.assertIn("cf fitness", reply.lower())
        # Tapping it drops the filter and re-runs the SAME search neighbourhood-wide.
        with patch(
            "app.activity_browse._fetch_block_events",
            return_value=[
                {
                    "title": "FIFA watch party",
                    "starts_at": "2026-06-27T18:00:00",
                    "venue_name": "The Pub",
                    "cohort_tags": [],
                }
            ],
        ) as block_events:
            run_activity_browse_turn(
                user_message=chip,
                session_ctx=ctx,
                history=[],
                user_jwt="jwt",
                home_block_id="b1",
            )
        self.assertIsNone(active_community_id(ctx))
        block_events.assert_called_once()


class OrchestratorToolTests(unittest.TestCase):
    def test_the_find_peers_tool_honours_the_filter_too(self) -> None:
        # QA 2026-09-01: which router won the turn decided whether the filter applied —
        # the orchestrator's tool called the bare vector RPC, so picking Fitness CF still
        # answered neighbourhood-wide. Both roads now go through the same fetch.
        from app.orchestrator.tools import execute_tool

        roster = [{"peer_user_id": "u3", "nickname": "Ana", "community_name": "CF Fitness"}]
        with patch("app.community_scope.peers_in_community", return_value=roster) as scoped, patch(
            "app.discovery_route.kick_claim_embedding_backfill"
        ), patch("app.guest_capabilities.compose_reply", return_value="ok"):
            result = execute_tool(
                tool_name="find_peers",
                tool_args={},
                user_id="me",
                user_jwt="jwt",
                session_id="s1",
                block_id="b1",
                purpose="lana",
                session_ctx={
                    "phone_verified": True,
                    "identity_snippet": "Runner",
                    CTX_KEY: {"place_id": PLACE, "name": "CF Fitness"},
                },
                source_module="orchestrator",
            )
        scoped.assert_called_once()
        self.assertEqual(result["peer_matches"], roster)


class HereMeansTheCommunityTests(unittest.TestCase):
    """"The swimming pool here is too good" while scoped to Fitness CF is a fact ABOUT
    Fitness CF. Without the block below the extractor sees an unnamed place, emits no
    feature, and the fact is lost (QA 2026-09-01: nothing was written at all)."""

    def test_here_resolves_to_the_caller_own_affiliation_key(self) -> None:
        from app.community_scope import here_place

        ctx: dict = {CTX_KEY: {"place_id": PLACE, "name": "Fitness CF"}}
        with patch(
            "app.community_surface.caller_affiliation_at",
            return_value={"id": "a1", "circle_key": "fitness_cf"},
        ):
            self.assertEqual(
                here_place(ctx, "me"),
                {"place_id": PLACE, "name": "Fitness CF", "circle_key": "fitness_cf"},
            )
        # No filter, no user, or an affiliation with no key → nothing claimed.
        self.assertIsNone(here_place({}, "me"))
        self.assertIsNone(here_place(ctx, None))
        with patch("app.community_surface.caller_affiliation_at", return_value={"id": "a1"}):
            self.assertIsNone(here_place(ctx, "me"))

    def test_the_extractor_is_told_what_here_means(self) -> None:
        from app.vertex_extract import _here_block

        block = _here_block({"name": "Fitness CF", "circle_key": "fitness_cf"})
        self.assertIn("Fitness CF", block)
        self.assertIn("fitness_cf", block)  # the key the feature router matches on
        # Both halves: the place got a pool AND the speaker swims. Emitting only the
        # feature loses the person (QA 2026-09-01: has_pool landed, no claim did).
        self.assertIn("BOTH SIDES", block)
        # "I like the juice corner here, it's good" is a fact + an opinion, not an
        # opinion — the model dropped the feature until the block said so.
        self.assertIn("LIKING IT DOES NOT MAKE IT AN OPINION", block)
        # at_here has to be named as a field ON the claim, or it comes back unset.
        self.assertIn("at_here", block)
        # Absent context adds nothing to the prompt — no empty scaffolding.
        self.assertEqual(_here_block(None), "")
        self.assertEqual(_here_block({"name": "Fitness CF"}), "")

    def test_what_they_do_here_becomes_the_place_activity_edge(self) -> None:
        # The claim is the interest; the EDGE is what makes the community roster say
        # "Swimming" under this member. It is not a place_ref on the claim — a claim
        # holds one, and the same activity happens at two communities (20261010120000).
        from app.claims_persist import try_upsert_claims_from_message
        from app.models import ExtractedClaim

        swims = ExtractedClaim(
            concept="swimmer", label="Swims", confidence=0.8, bucket="interest", at_here=True
        )
        guitar = ExtractedClaim(  # true of them, not something they do at the gym
            concept="plays_guitar", label="Plays guitar", confidence=0.8, bucket="interest"
        )
        with patch("app.claims_persist.current_nickname", return_value="Asjid"), patch(
            "app.claims_persist.persist_nickname_if_stated", return_value=None
        ), patch("app.claims_persist.fetch_active_claim_threads", return_value=[]), patch(
            "app.claims_persist.incremental_claims_from_utterance", return_value={}
        ), patch(
            "app.claims_persist.parse_incremental_claims_data",
            return_value=(None, [swims, guitar], None, None),
        ), patch(
            "app.claims_persist.upsert_claims", return_value=2
        ), patch(
            "app.place_activities.link_activity_from_claim"
        ) as link:
            try_upsert_claims_from_message(
                "me",
                "i like the swimming pool here very much",
                allow_rapport_gap=False,
                here_place={"place_id": PLACE, "name": "Fitness CF", "circle_key": "gym"},
            )
        link.assert_called_once_with("me", PLACE, "Swims")

    def test_the_block_reaches_the_extractor_call(self) -> None:
        from app.claims_persist import try_upsert_claims_from_message

        here = {"name": "Fitness CF", "circle_key": "fitness_cf"}
        with patch("app.claims_persist.current_nickname", return_value="Asjid"), patch(
            "app.claims_persist.persist_nickname_if_stated", return_value=None
        ), patch("app.claims_persist.fetch_active_claim_threads", return_value=[]), patch(
            "app.claims_persist.incremental_claims_from_utterance", return_value={}
        ) as extract:
            try_upsert_claims_from_message(
                "me",
                "i enjoyed the swimming pool here its too good",
                allow_rapport_gap=False,
                here_place=here,
            )
        self.assertEqual(extract.call_args.kwargs.get("here_place"), here)


class PeerReplyTests(unittest.TestCase):
    def test_an_empty_community_widens_but_says_so(self) -> None:
        from app.discovery_route import _fetch_verified_peer_matches

        ctx: dict = {CTX_KEY: {"place_id": PLACE, "name": "CF Fitness"}}
        with patch("app.community_scope.peers_in_community", return_value=[]), patch(
            "app.discovery_route.kick_claim_embedding_backfill"
        ), patch(
            "app.discovery_route.fetch_peer_matches_within_radius", return_value=None
        ), patch(
            "app.discovery_route.fetch_peer_matches",
            return_value=[{"peer_user_id": "u9", "nickname": "Wider"}],
        ), patch(
            "app.onion_blend.onion_blend_enabled", return_value=False
        ):
            peers = _fetch_verified_peer_matches(
                "jwt", user_id="me", block_id="b1", session_ctx=ctx
            )
        self.assertEqual([p["nickname"] for p in peers], ["Wider"])
        # The note the reply is composed from — without it the widen is silent.
        self.assertEqual(ctx.get("community_widened_from"), "CF Fitness")

        from app.guest_capabilities import format_peer_matches

        with patch("app.guest_capabilities.compose_reply", side_effect=lambda **kw: str(kw["facts"])):
            text = format_peer_matches(peers, ctx)
        self.assertIn("CF Fitness", text)
        self.assertIsNone(ctx.get("community_widened_from"))  # one turn only


if __name__ == "__main__":
    unittest.main()
