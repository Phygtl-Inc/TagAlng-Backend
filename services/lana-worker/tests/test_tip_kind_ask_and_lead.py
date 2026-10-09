"""A kind-less recommendation ask, and a Google lead-in that follows what renders.

Prod 2026-10-06, "do you have reccomendations at SJSU": Google was searched for the bare
place, the reply opened "here's what's nearby (from Google — not a personal vouch)" with
no Google list on screen, and the ask-neighbours offer that followed said "I couldn't find
any recommendations at SJSU yet". Three fixes, one test class each.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app import discovery_route as dr

PLACE = "place-sjsu"
_KINDS = [
    {"label": "Food", "ask": "good food near SJSU"},
    {"label": "Coffee", "ask": "good coffee near SJSU"},
    {"label": "Study spots", "ask": "quiet study spots near SJSU"},
]


class AskDraftKindTests(unittest.TestCase):
    """The draft model is the one that reads whether a kind was named."""

    def _draft(self, raw):
        from app.tip_ask_draft import build_ask_draft

        with patch("app.orchestrator.llm.llm_configured", return_value=True), patch(
            "app.orchestrator.llm.llm_json", return_value=raw
        ):
            return build_ask_draft(
                msg="do you have reccomendations at SJSU", detail="recommendations at SJSU"
            )

    def test_kind_less_ask_carries_the_offered_kinds(self) -> None:
        draft = self._draft(
            {"title": "Recommendations at SJSU", "category": "", "kind_named": False,
             "kind_options": _KINDS + [{"label": "", "ask": "x"}, "junk"]}
        )
        self.assertEqual(draft["kind_options"], _KINDS)

    def test_a_named_kind_carries_none(self) -> None:
        draft = self._draft(
            {"title": "Coffee near SJSU", "category": "coffee shop", "kind_named": True,
             "kind_options": _KINDS}
        )
        self.assertNotIn("kind_options", draft)

    def test_a_missing_verdict_is_not_kind_less(self) -> None:
        # Only an explicit False stops the search — a model that said nothing keeps the
        # straight-through answer.
        draft = self._draft({"title": "Coffee", "kind_options": _KINDS})
        self.assertNotIn("kind_options", draft)

    def test_the_prompt_asks_for_the_verdict(self) -> None:
        from app.tip_ask_draft import _SYSTEM

        self.assertIn("kind_named", _SYSTEM)
        self.assertIn("A place or area alone is not a kind", _SYSTEM)


class KindAskTurnTests(unittest.TestCase):
    def _ask(self, *, draft, tips=(), session_ctx=None, block="blk1", **kw):
        with patch.object(dr, "_resolve_block_id_for_turn", return_value=block), patch.object(
            dr, "find_neighbor_tips", return_value=list(tips)
        ), patch.object(
            dr, "compose_reply", side_effect=lambda **k: "REPLY:" + k["goal"]
        ), patch.object(dr, "_stamp_tip_ask_draft", return_value=draft), patch.object(
            dr, "_tip_seek_fallback_reply", return_value="GOOGLE"
        ) as google, patch.object(dr, "stamp_tip_peer_surface", return_value=[]), patch.object(
            dr, "_compose_neighbor_tip_reply", return_value="TIPS"
        ), patch("app.reco_fit.finish_fit"), patch(
            "app.reco_aspects.split_query_full", return_value={}
        ), patch("app.reco_kind_gate.keep_asked_kind", side_effect=lambda rows, *_a, **_k: rows), patch(
            "app.aspect_round.aspects_enabled", return_value=False
        ), patch("app.reco_authority.authority_enabled", return_value=False):
            out = dr._tip_seek_answer_turn(
                msg="do you have reccomendations at SJSU", detail="recommendations at SJSU",
                category=None, session_ctx=dict(session_ctx or {}), user_jwt="jwt",
                phone_verified=True, home_block_id=block, phase="listening", user_id="u1",
                active_intent="looking.tip", **kw,
            )
        return out, google

    def test_kind_less_ask_asks_what_kind_before_google(self) -> None:
        (reply, ctx, routing, peers), google = self._ask(
            draft={"title": "x", "kind_options": _KINDS}
        )
        google.assert_not_called()
        self.assertIn("tip_seek_kind_ask", str(routing))
        self.assertIn("what kind of recommendation", reply)
        self.assertEqual([c["message"] for c in ctx["rec_chips"]], [k["ask"] for k in _KINDS])
        self.assertEqual([c["label"] for c in ctx["rec_chips"]], ["Food", "Coffee", "Study spots"])
        # The question is the turn: no ask-neighbours offer, no receipt card under it.
        self.assertFalse(ctx.get("tip_ask_offer"))
        self.assertIsNone(ctx["ask_draft"])
        self.assertIsNone(ctx["ask_draft_pending"])
        self.assertEqual(peers, [])

    def test_the_chips_render_as_the_turns_actions(self) -> None:
        from app.ui_actions import derive_ui_actions

        (_r, ctx, _ro, _p), _g = self._ask(draft={"title": "x", "kind_options": _KINDS})
        rows = derive_ui_actions(ctx, "chat")
        self.assertEqual([r["message"] for r in rows], [k["ask"] for k in _KINDS])

    def test_a_specific_ask_goes_straight_through(self) -> None:
        (reply, ctx, routing, _p), google = self._ask(draft={"title": "Coffee near SJSU"})
        google.assert_called_once()
        self.assertIn("tip_seek_answered", str(routing))
        self.assertTrue(ctx.get("tip_ask_offer"))

    def test_neighbour_recs_still_answer_a_kind_less_ask(self) -> None:
        (reply, _c, routing, _p), google = self._ask(
            draft={"title": "x", "kind_options": _KINDS}, tips=[{"signal_id": "s1"}]
        )
        self.assertEqual(reply, "TIPS")
        google.assert_not_called()
        self.assertNotIn("tip_seek_kind_ask", str(routing))

    def test_a_picked_category_chip_is_the_kind(self) -> None:
        (_r, _c, routing, _p), google = self._ask(
            draft={"title": "x", "kind_options": _KINDS},
            session_ctx={"reco_type_filter": ["restaurant"]},
        )
        google.assert_called_once()
        self.assertNotIn("tip_seek_kind_ask", str(routing))

    def test_a_widen_is_never_stopped(self) -> None:
        (_r, _c, routing, _p), google = self._ask(
            draft={"title": "x", "kind_options": _KINDS}, widen=True
        )
        google.assert_called_once()

    def test_inside_a_community_it_asks_before_offering_to_look_beyond(self) -> None:
        comm = {"lana_community_scope": {"place_id": PLACE, "name": "SJSU"}}
        with patch.object(dr, "active_community", return_value={"place_id": PLACE, "name": "SJSU"}):
            (reply, ctx, routing, _p), google = self._ask(
                draft={"title": "x", "kind_options": _KINDS}, session_ctx=comm
            )
        google.assert_not_called()
        self.assertIn("tip_seek_kind_ask", str(routing))
        self.assertNotIn("tip_community_chip", ctx)

    def test_inside_a_community_with_no_location_it_still_asks(self) -> None:
        with patch.object(dr, "active_community", return_value={"place_id": PLACE, "name": "SJSU"}):
            (_r, _c, routing, _p), _g = self._ask(
                draft={"title": "x", "kind_options": _KINDS}, block=None
            )
        self.assertIn("tip_seek_kind_ask", str(routing))


def _row(name, pid, community=False):
    row = {"name": name, "address": "San Jose", "place_id": pid}
    if community:
        row["community"] = {"member_count": 4}
    return row


class LeadInFollowsWhatRendersTests(unittest.TestCase):
    """The core text is canned and names Google; it stands only over a Google list."""

    def _reply(self, rows, cards=None):
        def core(**kw):
            kw["ctx"]["google_place_suggestions"] = rows
            kw["ctx"]["rec_chips"] = [{"label": "Vegetarian", "message": "veg"}]
            return "No neighbor has recommended one yet, so here's what's nearby (from Google)."

        def stamp(ctx, **_kw):
            ctx["google_reco_cards"] = cards

        seen: dict = {}
        ctx: dict = {}
        with patch.object(dr, "_tip_seek_fallback_core", side_effect=core), patch(
            "app.google_reco_cards.stamp_google_cards", side_effect=stamp
        ), patch("app.places._centroid", return_value=None), patch(
            "app.reader_claims.load_reader_claims", return_value=[]
        ), patch.object(
            dr, "compose_reply", side_effect=lambda **k: seen.update(k) or "COMPOSED"
        ):
            out = dr._tip_seek_fallback_reply(
                ctx=ctx, msg="recs at SJSU", detail="SJSU", category=None, block_id="b1",
                session_ctx={}, user_id="u1", posted=False,
            )
        return out, ctx, seen

    def test_google_rows_keep_the_google_lead(self) -> None:
        out, ctx, seen = self._reply([_row("Peanuts Deluxe Cafe", "g1"), _row("SJSU", "c1", True)])
        self.assertIn("from Google", out)
        self.assertEqual(seen, {})

    def test_only_community_rows_never_say_google(self) -> None:
        out, ctx, seen = self._reply([_row("San José State University", "c1", True)])
        self.assertEqual(out, "COMPOSED")
        self.assertIn("not from Google", seen["goal"])
        self.assertIn("Places shown from their communities: 1", seen["facts"])

    def test_nothing_shown_is_the_honest_empty_path(self) -> None:
        out, ctx, _seen = self._reply([{"name": ""}])
        self.assertEqual(out, "")
        self.assertIsNone(ctx["google_place_suggestions"])
        self.assertIsNone(ctx["rec_chips"])

    def test_counting_mirrors_the_surface(self) -> None:
        rows = [_row("A", "g1"), _row("B", "g2"), _row("C", "c1", True)]
        cards = [{"subject_ref": "google:g1"}, {"subject_ref": "google:g9"}]
        ctx = {"google_place_suggestions": rows, "google_reco_cards": cards}
        # Two cards + B (A is covered by its card) — the community row on its own.
        self.assertEqual(dr.tip_fallback_shown(ctx), {"google": 3, "circles": 1})


class OfferLineKnowsWhatWasShownTests(unittest.TestCase):
    def test_offer_line_is_told_the_list_was_shown(self) -> None:
        calls: list[dict] = []

        def fallback(**kw):
            kw["ctx"]["google_place_suggestions"] = [_row("Peanuts", "g1"), _row("Dining", "g2")]
            return "LEAD"

        with patch.object(dr, "_resolve_block_id_for_turn", return_value="blk1"), patch.object(
            dr, "find_neighbor_tips", return_value=[]
        ), patch.object(
            dr, "compose_reply", side_effect=lambda **k: calls.append(k) or "OFFER"
        ), patch.object(dr, "_stamp_tip_ask_draft", return_value={}), patch.object(
            dr, "_tip_seek_fallback_reply", side_effect=fallback
        ), patch("app.aspect_round.aspects_enabled", return_value=False), patch(
            "app.reco_authority.authority_enabled", return_value=False
        ):
            reply, _c, _r, _p = dr._tip_seek_answer_turn(
                msg="good coffee near SJSU", detail="coffee near SJSU", category=None,
                session_ctx={}, user_jwt="jwt", phone_verified=True, home_block_id="blk1",
                phase="listening", user_id="u1", active_intent="looking.tip",
            )
        self.assertEqual(reply, "LEAD OFFER")
        facts = "\n".join(calls[-1]["facts"])
        self.assertIn("2 places from Google", facts)
        self.assertIn("never say you found nothing", facts)


if __name__ == "__main__":
    unittest.main()
