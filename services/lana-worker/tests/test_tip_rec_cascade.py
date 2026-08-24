"""The recommendation cascade — the rec rides on the neighbor's row (frontend §12 / #68).

Four gaps this covers, all on the looking.tip turn:
  (a) the neighbor's ACTUAL recommendation, per row, with the tip it came from
  (b) an honest distance phrase, straight from the RPC — never the shared-thread label
  (c) a server-side re-rank that reaches past the page the user was shown
  (d) the seek-side ask draft, so the ask can be read back before anything is broadcast

The line that must not move: none of this writes. A search instruction ("find more
people") is not consent to post, and a draft confirmation is not consent either.
"""

from __future__ import annotations

import unittest
from unittest import mock
from unittest.mock import patch

from app.discovery_route import handle_discovery_turn
from app.peer_discovery_surface import stamp_peer_discovery_ctx
from app.tip_rec_cascade import (
    peer_rows_from_neighbor_tips,
    rerank_by_weights,
    tip_discovery_surface,
)


def _tip(
    name: str = "Marisol",
    *,
    text: str = "Dr. Reyes at Lake Nona Smiles — so gentle with the toddlers",
    peer_id: str = "peer-1",
    strength: float = 0.9,
    tags: list[str] | None = None,
    distance: str | None = "a few minutes away",
) -> dict:
    return {
        "signal_id": f"sig-{peer_id}",
        "detail_text": text,
        "category": "health",
        "match_strength": strength,
        "neighbor_label": name,
        "peer_user_id": peer_id,
        "avatar_url": f"https://img/{peer_id}.jpg",
        "affinity_tags": tags if tags is not None else ["Toddler stage"],
        "distance_meters": 400.0,
        "distance_text": distance,
        "created_at": "2026-08-01T00:00:00Z",
    }


def _tip_slots(detail: str = "gentle pediatric dentist") -> dict:
    return {
        "goal": "save_signal",
        "in_discovery": True,
        "confidence": 0.9,
        "linear_intent": "looking.tip",
        "signal_intent": "tip_seek",
        "signal_detail": detail,
        "signal_category": "health",
    }


def _turn(msg: str, session_ctx: dict | None = None):
    return handle_discovery_turn(
        msg,
        session_ctx=session_ctx if session_ctx is not None else {"routing_phase": "listening"},
        user_jwt="jwt",
        phone_verified=True,
        home_block_id="block-a",
        is_anonymous=False,
        history=[],
        user_id="user-1",
    )


class TestRowsCarryTheRec(unittest.TestCase):
    """(a)+(b): the quote is the point of the row, and distance is its own fact."""

    def test_row_carries_quote_signal_id_and_distance(self) -> None:
        rows = peer_rows_from_neighbor_tips([_tip()])

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["tip_text"], _tip()["detail_text"])
        self.assertEqual(row["tip_signal_id"], "sig-peer-1")
        self.assertEqual(row["distance_text"], "a few minutes away")
        self.assertEqual(row["peer_user_id"], "peer-1")
        self.assertEqual(row["trait_tags"], ["Toddler stage"])
        # A rec is not a claim affinity — never dress it as one.
        self.assertIsNone(row.get("match_stars"))
        self.assertIsNone(row.get("match_band"))
        self.assertIsNone(row.get("match_badge"))
        self.assertIsNone(row.get("similarity_score"))
        self.assertEqual([a["id"] for a in row["actions"]], ["peer_card_nudge"])

    def test_v1_rpc_shape_degrades_instead_of_inventing(self) -> None:
        """A DB still on find_neighbor_tips v1 returns no author — the row is dropped
        rather than rendered as an unattributable card."""
        legacy = {"detail_text": "Dr. Patel is great", "neighbor_label": "Marisol"}
        self.assertEqual(peer_rows_from_neighbor_tips([legacy]), [])

    def test_missing_distance_is_absent_not_guessed(self) -> None:
        rows = peer_rows_from_neighbor_tips([_tip(distance=None)])
        self.assertIsNone(rows[0]["distance_text"])

    def test_rows_are_ranked_by_match_strength(self) -> None:
        rows = peer_rows_from_neighbor_tips(
            [_tip("Ana", peer_id="p-2", strength=0.75), _tip("Bea", peer_id="p-3", strength=0.95)]
        )
        self.assertEqual([r["nickname"] for r in rows], ["Bea", "Ana"])

    def test_unverified_caller_gets_no_reach_out_pill(self) -> None:
        rows = peer_rows_from_neighbor_tips([_tip()], phone_verified=False)
        self.assertNotIn("actions", rows[0])


class TestReRank(unittest.TestCase):
    """(c): weights re-order, and only ever re-order."""

    def _rows(self) -> list[dict]:
        return peer_rows_from_neighbor_tips(
            [
                _tip("Ana", peer_id="p-1", strength=0.95, tags=["Toddler stage"]),
                _tip(
                    "Bea",
                    peer_id="p-2",
                    strength=0.80,
                    tags=["Same insurance", "Toddler stage"],
                ),
            ]
        )

    def test_weighted_thread_outranks_the_stronger_match(self) -> None:
        ranked = rerank_by_weights(self._rows(), ["Same insurance"])
        self.assertEqual([r["nickname"] for r in ranked], ["Bea", "Ana"])
        self.assertEqual(ranked[0]["weight_hits"], 1)

    def test_a_weight_can_be_satisfied_by_the_rec_text_itself(self) -> None:
        rows = peer_rows_from_neighbor_tips(
            [
                _tip("Ana", peer_id="p-1", strength=0.95, tags=[]),
                _tip("Bea", peer_id="p-2", strength=0.80, tags=[], text="Dr. Ruiz — free parking"),
            ]
        )
        self.assertEqual(
            [r["nickname"] for r in rerank_by_weights(rows, ["parking"])], ["Bea", "Ana"]
        )

    def test_an_unmatched_weight_drops_nobody(self) -> None:
        ranked = rerank_by_weights(self._rows(), ["Speaks Portuguese"])
        self.assertEqual([r["nickname"] for r in ranked], ["Ana", "Bea"])

    def test_no_weights_is_a_no_op(self) -> None:
        self.assertEqual(
            [r["nickname"] for r in rerank_by_weights(self._rows(), [])], ["Ana", "Bea"]
        )


class TestSurface(unittest.TestCase):
    def test_counts_only_rows_that_actually_carry_a_rec(self) -> None:
        surface = tip_discovery_surface(peer_rows_from_neighbor_tips([_tip(), _tip("Bea", peer_id="p-2")]))
        assert surface is not None
        self.assertEqual(surface["strong_count"], 2)
        self.assertEqual(surface["partial_count"], 0)
        self.assertIn("2 neighbor recs", surface["status_label"])
        self.assertIsNone(surface["weak_peer"])

    def test_no_rows_no_surface(self) -> None:
        self.assertIsNone(tip_discovery_surface([]))

    def test_peer_enrichment_leaves_cascade_rows_alone(self) -> None:
        """The enricher's unscored branch would wipe trait_tags off these rows, and its
        scored branch would invent a badge. Neither may touch them."""
        ctx = {"peer_matches": peer_rows_from_neighbor_tips([_tip()])}
        stamp_peer_discovery_ctx(ctx, phone_verified=True)

        row = ctx["peer_matches"][0]
        self.assertEqual(row["trait_tags"], ["Toddler stage"])
        self.assertEqual(row["tip_text"], _tip()["detail_text"])
        self.assertIsNone(row.get("match_badge"))


class TestAnswerTurnStampsTheCascade(unittest.TestCase):
    def setUp(self) -> None:
        self.save = mock.patch("app.discovery_route.save_local_signal").start()
        self.tips = mock.patch(
            "app.discovery_route.find_neighbor_tips", return_value=[_tip()]
        ).start()
        mock.patch("app.discovery_route._search_tip_places", return_value=[]).start()
        mock.patch("app.discovery_route.discovery_ai_enabled", return_value=True).start()
        mock.patch(
            "app.discovery_route.discovery_slots_for_turn", return_value=_tip_slots()
        ).start()
        self.addCleanup(mock.patch.stopall)

    def test_neighbor_rows_and_ask_draft_ride_the_answer(self) -> None:
        reply, ctx, routing, peers = _turn("I'm looking for a pediatric dentist, gentle with toddlers")

        self.save.assert_not_called()
        self.assertEqual(routing.get("tool_to_call"), "tip_seek_neighbor_tip")
        self.assertEqual(ctx["peer_matches"][0]["tip_text"], _tip()["detail_text"])
        self.assertEqual(ctx["peer_matches"][0]["distance_text"], "a few minutes away")
        self.assertEqual(peers, ctx["peer_matches"])
        self.assertEqual(ctx["discovery_surface"]["strong_count"], 1)
        # The ask, read back — and the ask itself remembered for a re-rank.
        self.assertTrue(ctx["ask_draft"]["title"])
        self.assertTrue(ctx["ask_draft"]["ready"])
        self.assertEqual(ctx["ask_draft_pending"]["detail"], "gentle pediatric dentist")
        self.assertEqual(ctx["tip_last_ask"]["detail"], "gentle pediatric dentist")
        assert reply is not None
        self.assertIn("Reyes", reply)

    def test_a_plain_ask_reads_narrow(self) -> None:
        _turn("I'm looking for a pediatric dentist")
        self.assertEqual(self.tips.call_args.kwargs["limit"], 3)
        self.assertIsNone(self.tips.call_args.kwargs["radius_meters"])

    def test_a_places_only_answer_still_remembers_the_ask(self) -> None:
        """No neighbor had posted a matching tip, so the answer came from Places — a later
        "find more people" still has an ask to widen."""
        self.tips.return_value = []
        with patch(
            "app.discovery_route._search_tip_places",
            return_value=[{"name": "Lake Nona Smiles", "address": "1 Main St", "attrs": {}}],
        ):
            _reply, ctx, _routing, peers = _turn("I'm looking for a pediatric dentist")

        self.assertEqual(ctx["tip_last_ask"]["detail"], "gentle pediatric dentist")
        self.assertEqual(peers, [])

    def test_gated_guest_still_gets_the_ask_read_back(self) -> None:
        _reply, ctx, routing, _peers = handle_discovery_turn(
            "I'm looking for a pediatric dentist, gentle with toddlers",
            session_ctx={"routing_phase": "listening"},
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            is_anonymous=True,
            history=[],
            user_id=None,
        )
        self.save.assert_not_called()
        self.assertEqual(routing.get("tool_to_call"), "tip_seek_need_verify")
        self.assertTrue(ctx["ask_draft"]["title"])


class TestCascadeControls(unittest.TestCase):
    """(c) on the wire: the re-rank and widen turns, and what they must never be read as."""

    def setUp(self) -> None:
        self.save = mock.patch("app.discovery_route.save_local_signal").start()
        self.tips = mock.patch(
            "app.discovery_route.find_neighbor_tips",
            return_value=[
                _tip("Ana", peer_id="p-1", strength=0.95, tags=["Toddler stage"]),
                _tip("Bea", peer_id="p-2", strength=0.80, tags=["Same insurance"]),
            ],
        ).start()
        mock.patch("app.discovery_route._search_tip_places", return_value=[]).start()
        mock.patch("app.discovery_route.discovery_ai_enabled", return_value=True).start()
        mock.patch(
            "app.discovery_route.discovery_slots_for_turn", return_value=_tip_slots()
        ).start()
        self.addCleanup(mock.patch.stopall)

    def _answered(self, **extra) -> dict:
        ctx = {
            "routing_phase": "listening",
            "tip_last_ask": {"detail": "gentle pediatric dentist", "category": "health"},
            "tip_ask_offer_pending": {"detail": "gentle pediatric dentist", "category": "health"},
        }
        ctx.update(extra)
        return ctx

    def test_rerank_reaches_past_the_page_and_reorders(self) -> None:
        _reply, ctx, routing, _peers = _turn(
            "Same insurance",
            self._answered(tip_rerank={"weights": ["Same insurance"], "widen": False}),
        )

        self.save.assert_not_called()
        self.assertEqual(routing.get("tool_to_call"), "tip_seek_reranked")
        # Wider fetch than the page she was shown — the part a client-side sort can't do.
        self.assertGreater(self.tips.call_args.kwargs["limit"], 3)
        self.assertEqual([r["nickname"] for r in ctx["peer_matches"]], ["Bea", "Ana"])
        self.assertIsNone(ctx.get("tip_rerank"))

    def test_find_more_people_widens_and_is_not_posting_consent(self) -> None:
        """The trap: the ask-neighbors offer is armed, and 'Find more people' arrives. It is
        a search instruction — reading it as a yes would post an ask the user never agreed
        to broadcast."""
        _reply, ctx, routing, _peers = _turn("Find more people", self._answered())

        self.save.assert_not_called()
        self.assertEqual(routing.get("tool_to_call"), "tip_seek_reranked")
        self.assertIsNotNone(self.tips.call_args.kwargs["radius_meters"])
        self.assertIsNone(ctx.get("signal_saved"))

    def test_keep_listening_is_the_accept(self) -> None:
        from app.discovery_route import _read_offer_reply

        with patch("app.tip_ask_ai.interpret_offer_reply", return_value=None):
            self.assertEqual(
                _read_offer_reply(
                    offer="ask_neighbors", detail="x", msg="Keep listening for me"
                ),
                "accept",
            )

    def test_a_control_without_a_prior_ask_falls_through(self) -> None:
        _reply, _ctx, routing, _peers = _turn("Find more people", {"routing_phase": "listening"})
        self.assertNotEqual(routing.get("tool_to_call"), "tip_seek_reranked")

    def test_a_rerank_with_nothing_to_rank_does_not_stay_armed(self) -> None:
        """A hint left armed would silently re-order some later, unrelated ask."""
        _reply, ctx, _routing, _peers = _turn(
            "Same insurance",
            {"routing_phase": "listening", "tip_rerank": {"weights": ["Same insurance"]}},
        )
        self.assertIsNone(ctx.get("tip_rerank"))


class TestAskDraftReply(unittest.TestCase):
    """(d): Looks good / Let me tweak that — and the consent line between them."""

    def setUp(self) -> None:
        self.save = mock.patch("app.discovery_route.save_local_signal").start()
        self.tips = mock.patch(
            "app.discovery_route.find_neighbor_tips", return_value=[_tip()]
        ).start()
        mock.patch("app.discovery_route._search_tip_places", return_value=[]).start()
        mock.patch("app.discovery_route.discovery_ai_enabled", return_value=True).start()
        mock.patch(
            "app.discovery_route.discovery_slots_for_turn", return_value=_tip_slots()
        ).start()
        self.addCleanup(mock.patch.stopall)

    def _drafted(self) -> dict:
        return {
            "routing_phase": "listening",
            "ask_draft_pending": {
                "title": "Gentle pediatric dentist",
                "detail": "gentle pediatric dentist",
                "category": "health",
            },
        }

    def test_looks_good_acknowledges_without_posting(self) -> None:
        _reply, ctx, routing, _peers = _turn("Looks good", self._drafted())

        self.save.assert_not_called()
        self.assertEqual(routing.get("tool_to_call"), "tip_ask_confirmed")
        self.assertIsNone(ctx.get("ask_draft_pending"))

    def test_the_spoken_offer_still_wins_over_the_card(self) -> None:
        """Both armed: "yes" is answering the question Lana actually asked out loud, so it
        is posting consent — not a draft confirmation that quietly swallows it."""
        ctx_in = self._drafted()
        ctx_in["tip_ask_offer_pending"] = {"detail": "gentle pediatric dentist", "category": "health"}
        self.save.return_value = {
            "signal_id": "sig-1",
            "intent": "tip_seek",
            "detail_text": "gentle pediatric dentist",
            "matches_created": 0,
        }
        with patch("app.discovery_route.fetch_my_block_log", return_value=[]):
            _reply, _ctx, routing, _peers = _turn("Yes, ask my neighbors", ctx_in)

        self.save.assert_called_once()
        self.assertEqual(routing.get("tool_to_call"), "tip_ask_posted")

    def test_tweak_asks_what_to_change_then_re_answers_the_merged_ask(self) -> None:
        _reply, ctx, routing, _peers = _turn("Let me tweak that", self._drafted())

        self.save.assert_not_called()
        self.assertEqual(routing.get("tool_to_call"), "tip_ask_tweak")
        self.assertEqual(ctx["tip_tweak_pending"]["detail"], "gentle pediatric dentist")

        # Their correction is folded into the ask, not routed as a brand-new seek.
        _reply2, _ctx2, routing2, _peers2 = _turn(
            "closer to home", {"routing_phase": "listening", **ctx}
        )
        self.assertEqual(routing2.get("tool_to_call"), "tip_seek_neighbor_tip")
        self.save.assert_not_called()
        self.assertIn("gentle pediatric dentist", self.tips.call_args.kwargs["query"])
        self.assertIn("closer to home", self.tips.call_args.kwargs["query"])

    def test_an_unrelated_reply_falls_through_to_normal_routing(self) -> None:
        with patch("app.tip_ask_ai.interpret_ask_draft_reply", return_value="other"):
            _reply, _ctx, routing, _peers = _turn("what about pizza places", self._drafted())
        self.assertNotIn(
            routing.get("tool_to_call"), ("tip_ask_confirmed", "tip_ask_tweak")
        )


class TestAskDraftShape(unittest.TestCase):
    """The card renders from real words even with no LLM available."""

    def test_fallback_echoes_their_ask_verbatim(self) -> None:
        from app.tip_ask_draft import build_ask_draft

        with patch("app.orchestrator.llm.llm_configured", return_value=False):
            draft = build_ask_draft(
                msg="know a gentle pediatric dentist?",
                detail="gentle pediatric dentist",
                category="health",
            )
        self.assertEqual(draft["title"], "Gentle pediatric dentist")
        self.assertTrue(draft["ready"])
        self.assertEqual([c["field"] for c in draft["chips"]], ["category"])

    def test_no_ask_no_card(self) -> None:
        from app.tip_ask_draft import build_ask_draft

        self.assertEqual(build_ask_draft(msg="hi", detail=""), {})


class TestWireProjection(unittest.TestCase):
    """The FE reads these off the response model, so the projection has to carry them."""

    def test_cascade_fields_reach_the_peer_row_model(self) -> None:
        from app.main import _peer_matches_from_ctx

        rows = _peer_matches_from_ctx({"peer_matches": peer_rows_from_neighbor_tips([_tip()])})
        self.assertEqual(rows[0].tip_text, _tip()["detail_text"])
        self.assertEqual(rows[0].tip_signal_id, "sig-peer-1")
        self.assertEqual(rows[0].distance_text, "a few minutes away")

    def test_rows_without_a_rec_carry_none(self) -> None:
        from app.main import _peer_matches_from_ctx

        rows = _peer_matches_from_ctx(
            {"peer_matches": [{"peer_user_id": "p", "nickname": "Ana", "similarity_score": 0.9}]}
        )
        self.assertIsNone(rows[0].tip_text)
        self.assertIsNone(rows[0].distance_text)

    def test_cascade_rows_survive_the_response_gate(self) -> None:
        """peer_matches are gated by ui_intent / active_intent on the way out — a looking.tip
        turn has to be on the allowed side of that gate, or the cards never ship."""
        from app.auth import AuthSession
        from app.main import _onboarding_fields

        payload = _onboarding_fields(
            {
                "routing_phase": "listening",
                "active_intent": "looking.tip",
                "peer_matches": peer_rows_from_neighbor_tips([_tip()]),
                "discovery_surface": tip_discovery_surface(peer_rows_from_neighbor_tips([_tip()])),
            },
            AuthSession(
                user_id="user-1",
                is_anonymous=False,
                phone_verified=True,
                home_block_id="block-a",
            ),
        )
        self.assertEqual(len(payload["peer_matches"]), 1)
        self.assertEqual(payload["peer_matches"][0].tip_text, _tip()["detail_text"])
        self.assertEqual(payload["discovery_surface"].strong_count, 1)

    def test_ask_draft_projection(self) -> None:
        from app.main import _ask_draft_from_ctx

        payload = _ask_draft_from_ctx(
            {
                "ask_draft": {
                    "title": "Gentle pediatric dentist",
                    "detail": "Gentle with toddlers · Lake Nona",
                    "category": "pediatric dentist",
                    "locality": "Lake Nona",
                    "chips": [{"label": "pediatric dentist", "tone": "sky", "field": "category"}],
                    "ready": True,
                }
            }
        )
        assert payload is not None
        self.assertEqual(payload.title, "Gentle pediatric dentist")
        self.assertEqual(payload.chips[0].label, "pediatric dentist")
        self.assertTrue(payload.ready)
        self.assertIsNone(_ask_draft_from_ctx({}))


if __name__ == "__main__":
    unittest.main()


class TestSharedCircleGrouping(unittest.TestCase):
    """C-FIND-V2 groups recs under the circle shared with the recommender, because "the
    grouping IS the explanation" — a stranger's rec and one from someone you sit next to
    at St Mary's are not the same claim, and the wire could not tell them apart."""

    def _rows(self):
        return peer_rows_from_neighbor_tips([
            {
                "signal_id": "s3", "detail_text": "Some place", "peer_user_id": "p3",
                "neighbor_label": "Zed", "match_strength": 0.95,
                "shared_circles": [], "same_block": False,
            },
            {
                "signal_id": "s2", "detail_text": "Walk-in friendly", "peer_user_id": "p2",
                "neighbor_label": "Rosa", "match_strength": 0.9,
                "shared_circles": [], "same_block": True,
            },
            {
                "signal_id": "s1", "detail_text": "Gentle, quick appts", "peer_user_id": "p1",
                "neighbor_label": "coral88", "match_strength": 0.8,
                "shared_circles": [
                    {"place_id": "pl-mary", "name": "St Mary's Church", "circle_type": "faith"}
                ],
                "same_block": False,
            },
        ])

    def test_a_shared_circle_names_the_group(self):
        row = next(r for r in self._rows() if r["nickname"] == "coral88")
        self.assertEqual(row["group_kind"], "circle")
        self.assertEqual(row["group_key"], "pl-mary")
        self.assertEqual(row["group_label"], "St Mary's Church")

    def test_no_shared_circle_falls_to_the_block(self):
        row = next(r for r in self._rows() if r["nickname"] == "Rosa")
        self.assertEqual(row["group_kind"], "block")
        # No copy from the backend: "Your block" is the surface's heading to translate.
        self.assertIsNone(row["group_label"])

    def test_circle_rows_outrank_a_stronger_stranger(self):
        # Zed matches best (0.95) but shares nothing — provenance leads the list, which is
        # the order the headings render in.
        self.assertEqual([r["group_kind"] for r in self._rows()], ["circle", "block", "nearby"])

    def test_malformed_circles_are_dropped_not_rendered_blank(self):
        rows = peer_rows_from_neighbor_tips([{
            "signal_id": "s", "detail_text": "x", "peer_user_id": "p", "neighbor_label": "n",
            "shared_circles": [{"name": "no id"}, {"place_id": "no-name"}, "junk"],
        }])
        self.assertEqual(rows[0]["shared_circles"], [])
        self.assertEqual(rows[0]["group_kind"], "nearby")
