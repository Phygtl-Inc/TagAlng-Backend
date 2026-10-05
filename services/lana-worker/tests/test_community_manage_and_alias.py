"""Pouya's SJSU QA (2026-10-04): the ZIP loop, the dead-end "I can't update it", and
"SJSU" not finding San Jose State University."""

import unittest
from unittest.mock import patch

from app.ui_actions import derive_ui_actions

_SJSU = {
    "id": "aff-1",
    "place_id": "pSJSU",
    "place_name": "San Jose State University",
    "place_address": "1 Washington Sq, San Jose, CA",
    "member_count": 1,
}
_GYM = {"id": "aff-2", "place_id": "pGym", "place_name": "Lp Fit", "member_count": 4}


class AliasResolutionTests(unittest.TestCase):
    @patch("app.community_discovery._community_about_turn", return_value="ABOUT")
    @patch("app.community_discovery.discover_communities", return_value=[])
    @patch("app.community_discovery._my_communities", return_value=[_GYM, _SJSU])
    @patch("app.community_discovery._ai_alias_match")
    def test_an_abbreviation_reaches_the_community_it_names(
        self, alias, _mine, _disc, about
    ) -> None:
        from app.community_discovery import communities_chat_turn

        alias.return_value = _SJSU
        out = communities_chat_turn(
            "u1",
            message="what's the status of my SJSU community",
            session_ctx={},
            community_name="SJSU",
            community_ask="about",
        )
        self.assertEqual(out, "ABOUT")
        alias.assert_called_once()
        self.assertEqual(about.call_args.kwargs["community"]["place_id"], "pSJSU")
        # Resolved by meaning, so it is not flagged as a guess.
        self.assertIsNone(about.call_args.kwargs["inexact"])

    @patch("app.community_discovery._community_about_turn", return_value="ABOUT")
    @patch("app.community_discovery.discover_communities", return_value=[])
    @patch("app.community_discovery._my_communities", return_value=[_SJSU])
    @patch("app.community_discovery._ai_alias_match")
    def test_an_exact_name_never_spends_a_model_call(self, alias, _mine, _disc, _about) -> None:
        from app.community_discovery import communities_chat_turn

        communities_chat_turn(
            "u1", message="x", session_ctx={}, community_name="San Jose State", community_ask="about"
        )
        alias.assert_not_called()

    def _ask_model(self, answer):
        from app.community_discovery import _ai_alias_match

        with patch("app.orchestrator.llm.llm_configured", return_value=True), patch(
            "app.orchestrator.llm.llm_json", return_value=answer
        ) as llm, patch("app.orchestrator.llm.router_model", return_value="m"):
            return _ai_alias_match("SJSU", [[_GYM, _SJSU], [dict(_SJSU)]]), llm

    def test_the_model_index_picks_the_row(self) -> None:
        hit, llm = self._ask_model({"match": 1})
        self.assertEqual(hit["place_id"], "pSJSU")
        # The duplicate from the nearby pool is offered once.
        self.assertEqual(len(__import__("json").loads(llm.call_args.kwargs["user_payload"])["communities"]), 2)

    def test_unsure_or_junk_answers_are_a_miss(self) -> None:
        for answer in ({"match": None}, {"match": 7}, {"match": -1}, {"match": True}, {"match": "1"}, None):
            hit, _ = self._ask_model(answer)
            self.assertIsNone(hit, answer)

    def test_no_model_is_a_miss_not_a_crash(self) -> None:
        from app.community_discovery import _ai_alias_match

        with patch("app.orchestrator.llm.llm_configured", return_value=False):
            self.assertIsNone(_ai_alias_match("SJSU", [[_SJSU]]))


class ManageTurnTests(unittest.TestCase):
    def _turn(self, *, name, mine, nearby=(), ctx=None):
        from app.community_discovery import communities_chat_turn

        ctx = {} if ctx is None else ctx
        with patch("app.community_discovery._my_communities", return_value=list(mine)), patch(
            "app.community_discovery.discover_communities", return_value=list(nearby)
        ), patch("app.community_discovery._ai_alias_match", return_value=None), patch(
            "app.reply_compose.compose_reply", side_effect=lambda **kw: kw
        ):
            out = communities_chat_turn(
                "u1",
                message="I want to update the location of my community",
                session_ctx=ctx,
                community_name=name,
                community_ask="manage",
            )
        return out, ctx

    def test_their_community_gets_a_button_to_its_edit_screen(self) -> None:
        out, ctx = self._turn(name="San Jose State University", mine=[_GYM, _SJSU])
        chip = ctx["policy_chips"][0]
        self.assertEqual(chip["open_panel"], "communities")
        self.assertEqual(chip["affiliation_id"], "aff-1")
        facts = " ".join(out["facts"])
        self.assertIn("San Jose State University", facts)
        self.assertIn("location changes", facts)
        # Never a bare refusal: the goal makes the button the answer.
        self.assertIn("button", out["goal"])

    def test_the_button_survives_into_ui_actions(self) -> None:
        _, ctx = self._turn(name="San Jose State University", mine=[_SJSU])
        rows = derive_ui_actions(ctx, "chat")
        self.assertEqual(rows[0]["open_panel"], "communities")
        self.assertEqual(rows[0]["affiliation_id"], "aff-1")
        # Older clients post the message, which must be harmless.
        self.assertEqual(rows[0]["message"], "show my communities")

    def test_the_button_survives_the_response_model(self) -> None:
        # main.py rebuilds every row field by field; the first real run lost both fields
        # there while every test above was green.
        from app.main import _ui_action_rows_from_raw, _ui_actions_from_ctx

        _, ctx = self._turn(name="San Jose State University", mine=[_SJSU])
        with patch("app.main.localize_labels", side_effect=lambda labels, _lang: labels):
            row = _ui_actions_from_ctx(ctx, "chat")[0]
        self.assertEqual((row.open_panel, row.affiliation_id), ("communities", "aff-1"))
        replayed = _ui_action_rows_from_raw([row.model_dump()])[0]
        self.assertEqual((replayed.open_panel, replayed.affiliation_id), ("communities", "aff-1"))

    def test_unnamed_with_one_community_picks_it(self) -> None:
        _, ctx = self._turn(name=None, mine=[_SJSU])
        self.assertEqual(ctx["policy_chips"][0]["affiliation_id"], "aff-1")

    def test_unnamed_with_several_asks_which(self) -> None:
        out, ctx = self._turn(name=None, mine=[_GYM, _SJSU])
        labels = [c["label"] for c in ctx["policy_chips"]]
        self.assertEqual(labels, ["Lp Fit", "San Jose State University"])
        self.assertNotIn("open_panel", ctx["policy_chips"][0])
        self.assertIn("which", out["goal"].lower())

    def test_unnamed_inside_a_community_picks_that_one(self) -> None:
        from app.community_scope import CTX_KEY

        ctx = {CTX_KEY: {"place_id": "pSJSU", "name": "San Jose State University"}}
        _, ctx = self._turn(name=None, mine=[_GYM, _SJSU], ctx=ctx)
        self.assertEqual(ctx["policy_chips"][0]["affiliation_id"], "aff-1")

    def test_a_community_they_are_not_in_offers_the_join(self) -> None:
        out, ctx = self._turn(
            name="Lp Fit", mine=[_SJSU], nearby=[dict(_GYM, is_member=False)]
        )
        self.assertIn("NOT a member", " ".join(out["facts"]))
        self.assertEqual(ctx["community_join_pending"]["places"][0]["place_id"], "pGym")

    def test_no_communities_at_all_is_said_plainly(self) -> None:
        out, ctx = self._turn(name=None, mine=[])
        self.assertIn("not in any", out["goal"])
        self.assertFalse(ctx.get("policy_chips"))


class SlotsTests(unittest.TestCase):
    def test_manage_survives_the_slot_reader(self) -> None:
        from app.discovery_slots import slots_community_ask

        self.assertEqual(slots_community_ask({"community_ask": "manage"}), "manage")
        self.assertEqual(slots_community_ask({"community_ask": "people"}), "people")
        self.assertEqual(slots_community_ask({"community_ask": "junk"}), "about")

    def test_a_possessive_bare_noun_names_nothing(self) -> None:
        from app.discovery_slots import slots_community_name

        for said in ("my community", "our group", "the club", "community"):
            self.assertIsNone(slots_community_name({"community_name": said}), said)
        self.assertEqual(slots_community_name({"community_name": "my gym crew"}), "my gym crew")
        self.assertEqual(slots_community_name({"community_name": "SJSU"}), "SJSU")


class ChangeZipTests(unittest.TestCase):
    def _turn(self, *, slots, phase="listening", ctx=None):
        from app.discovery_route import _try_layer1_intent_turn

        with patch(
            "app.discovery_route.slots_linear_intent", return_value="settings.change_zip"
        ), patch("app.discovery_route.intent_confidence_met", return_value=True), patch(
            "app.discovery_route.compose_reply", side_effect=lambda **kw: "COMPOSED:" + kw["goal"]
        ):
            return _try_layer1_intent_turn(
                msg="I don't want to enter my ZIP right now",
                slots=slots,
                session_ctx=ctx or {},
                user_jwt="jwt",
                phone_verified=True,
                home_block_id="b1",
                phase=phase,
                user_id="u1",
            )

    def test_a_refusal_lets_go_instead_of_re_asking(self) -> None:
        reply, ctx, routing, _ = self._turn(slots={"declined_slot": "zip"}, phase="need_zip")
        self.assertTrue(reply.startswith("COMPOSED:"))
        self.assertIn("Do not ask for the ZIP again", reply)
        self.assertEqual(ctx["routing_phase"], "listening")
        self.assertIn("settings_change_zip_declined", str(routing))

    def test_the_first_ask_is_unchanged(self) -> None:
        reply, ctx, _, _ = self._turn(slots={})
        self.assertEqual(reply, "Sure — what's your new ZIP code?")
        self.assertEqual(ctx["routing_phase"], "need_zip")

    def test_a_second_ask_is_never_the_same_line(self) -> None:
        reply, _, _, _ = self._turn(
            slots={}, phase="need_zip", ctx={"active_intent": "settings.change_zip"}
        )
        self.assertTrue(reply.startswith("COMPOSED:"))


if __name__ == "__main__":
    unittest.main()
