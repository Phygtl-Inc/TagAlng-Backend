"""A chat INSIDE a community answers questions about that community.

Everyone arriving from a creator's link is a guest. Before this, the router was never told
the chat was inside a community, so "what is this community?" / "what do people do here?"
were filed as "show me communities near me", and the unverified-guest gate answered all of
them with "verify your email to see neighbours' spots" (2026-09-30).
"""

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from app.community_scope import CTX_KEY

PLACE = "cd75a194-df47-41c2-92a2-ad885dbd358a"
FACTS = {
    "place_id": PLACE,
    "name": "Tommaso",
    "kind": "creator community",
    "about": "a community for tech founders swapping notes on building products",
    "creator_wants": "Share what you're building this month",
    "creator": "Tommaso",
    "at": datetime.now(timezone.utc).isoformat(),
}


def _ctx() -> dict:
    return {CTX_KEY: {"place_id": PLACE, "name": "Tommaso"}, "_active_community_facts": dict(FACTS)}


class GateTests(unittest.TestCase):
    """_try_layer1_intent_turn's discovery.communities branch, for an unverified guest."""

    def _turn(self, *, name, ask, verified=False, ctx=None):
        from app.discovery_route import _try_layer1_intent_turn

        with patch(
            "app.discovery_route.slots_linear_intent", return_value="discovery.communities"
        ), patch("app.discovery_route.intent_confidence_met", return_value=True), patch(
            "app.discovery_slots.slots_community_name", return_value=name
        ), patch(
            "app.discovery_slots.slots_community_ask", return_value=ask
        ), patch(
            "app.community_discovery.communities_chat_turn", return_value="ENGINE"
        ) as engine, patch(
            "app.discovery_route.compose_reply", side_effect=lambda **kw: "GATE:" + kw["goal"]
        ):
            out = _try_layer1_intent_turn(
                msg="what do people do here?",
                slots={},
                session_ctx=_ctx() if ctx is None else ctx,
                user_jwt="jwt",
                phone_verified=verified,
                home_block_id=None,
                phase="listening",
                user_id="guest-1",
            )
        return out[0], out[2], engine

    def test_guest_asking_about_the_community_they_are_in_gets_an_answer(self) -> None:
        reply, routing, engine = self._turn(name="Tommaso", ask="about")
        self.assertEqual(reply, "ENGINE")
        engine.assert_called_once()
        self.assertEqual(engine.call_args.kwargs["community_name"], "Tommaso")

    def test_guest_asking_who_is_in_it_is_told_honestly_not_about_neighbours_spots(self) -> None:
        reply, routing, engine = self._turn(name="Tommaso", ask="people")
        engine.assert_not_called()
        self.assertTrue(reply.startswith("GATE:"))
        self.assertIn("member list", reply)
        self.assertNotIn("neighbours' spots", reply)
        self.assertIn("community_roster_need_verify", str(routing))

    def test_guest_browsing_other_communities_keeps_the_verify_gate(self) -> None:
        reply, _routing, engine = self._turn(name=None, ask=None)
        engine.assert_not_called()
        self.assertIn("neighbours' spots", reply)

    def test_guest_naming_a_different_community_keeps_the_verify_gate(self) -> None:
        reply, _routing, engine = self._turn(name="Mizu Sushi", ask="about")
        engine.assert_not_called()
        self.assertIn("neighbours' spots", reply)

    def test_not_inside_any_community_keeps_the_verify_gate(self) -> None:
        reply, _routing, engine = self._turn(name="Tommaso", ask="about", ctx={})
        engine.assert_not_called()
        self.assertIn("neighbours' spots", reply)

    def test_verified_member_is_unchanged(self) -> None:
        reply, _routing, engine = self._turn(name="Tommaso", ask="people", verified=True)
        self.assertEqual(reply, "ENGINE")


class RouterContextTests(unittest.TestCase):
    def test_router_is_told_which_community_the_chat_is_in(self) -> None:
        from app.discovery_slots import _discovery_slot_payload

        payload = _discovery_slot_payload(
            "what do people do here?",
            routing_phase="listening",
            history=[],
            has_block=False,
            has_identity=False,
            phone_verified=False,
            session_ctx=_ctx(),
        )
        line = next(l for l in payload.splitlines() if l.startswith("active_community:"))
        self.assertIn('"Tommaso" (creator community)', line)
        self.assertIn("tech founders", line)
        self.assertIn("Share what you're building", line)

    def test_no_community_says_none(self) -> None:
        from app.discovery_slots import _discovery_slot_payload

        payload = _discovery_slot_payload(
            "hi", routing_phase="listening", history=[], has_block=False,
            has_identity=False, phone_verified=False, session_ctx={},
        )
        self.assertIn("active_community: none", payload)

    def test_router_prompt_explains_what_here_means(self) -> None:
        import app.discovery_slots as ds

        prompt = " ".join(v for v in vars(ds).values() if isinstance(v, str))
        self.assertIn("ACTIVE COMMUNITY", prompt)
        self.assertIn("community_ask='about'", prompt)


class AboutFactsTests(unittest.TestCase):
    def test_about_answer_carries_the_creators_purpose(self) -> None:
        from app import community_discovery as cd

        prof = {
            "place_name": "Tommaso", "membership": "member", "member_count": 2,
            "relation": "community", "description": None, "features": [],
            "upcoming_events": [],
        }
        seen: dict = {}
        with patch("app.community_surface.community_profile", return_value=prof), patch(
            "app.reply_compose.compose_reply", side_effect=lambda **kw: seen.update(kw) or "ok"
        ):
            cd._community_about_turn(
                "guest-1",
                community={"place_id": PLACE, "place_name": "Tommaso"},
                message="what do people do here?",
                session_ctx=_ctx(),
            )
        facts = "\n".join(seen["facts"])
        self.assertIn("Share what you're building this month", facts)
        self.assertIn("run by Tommaso", facts)
        self.assertIn("tech founders", facts)
        self.assertIn("inside this community's chat", facts)
        # A creator's group is not a venue and its people are members, not neighbours.
        self.assertIn("Members: 2", facts)
        self.assertNotIn("People who go here", facts)
        self.assertIn('never "neighbors"', facts)
        # Never a guessed pronoun for the creator.
        self.assertIn("never with he/she", facts)
        # A creator community has no geography.
        self.assertNotIn("near you", seen["fallback"])


class PolicyPayloadTests(unittest.TestCase):
    def test_policy_is_told_it_is_inside_the_community(self) -> None:
        from app.policy.decide import _inside_community

        got = _inside_community(_ctx())
        self.assertEqual(got["name"], "Tommaso")
        self.assertEqual(got["creator_wants"], "Share what you're building this month")
        self.assertIsNone(_inside_community({}))

    def test_decide_turn_actually_sends_it_to_the_model(self) -> None:
        import json

        from app.policy import decide

        sent: dict = {}

        def fake_llm_json(**kw):
            sent.update(json.loads(kw["user_payload"]))
            return None  # unparseable -> decide_turn returns None, which is fine here

        with patch("app.orchestrator.llm.llm_configured", return_value=True), patch(
            "app.orchestrator.llm.llm_json", side_effect=fake_llm_json
        ), patch("app.policy.world.world_state", return_value={}), patch(
            "app.policy.goals.candidate_goals", return_value=[]
        ), patch.object(decide, "_claims", return_value=[]):
            decide.decide_turn(
                user_id="guest-1", session_ctx=_ctx(), history=[],
                user_message="what do people do here?",
            )
        self.assertEqual(sent["inside_community"]["name"], "Tommaso")
        self.assertEqual(sent["inside_community"]["kind"], "creator community")

    def test_policy_prompt_has_the_rule(self) -> None:
        from app.context import load_prompt

        text = load_prompt("lana_policy_decide.md")
        self.assertIn("inside_community", text)
        self.assertIn("Verification is never the answer", text)


class FactsCacheTests(unittest.TestCase):
    def test_facts_are_read_once_and_reread_when_the_community_changes(self) -> None:
        from app import community_opening as co

        ctx = {CTX_KEY: {"place_id": PLACE, "name": "Tommaso"}}
        with patch.object(co, "_community_row", return_value={
            "name": "Tommaso", "place_type": "creator", "blurb": "b", "first_action": "f",
        }) as row, patch.object(co, "_creator_name", return_value="Tommaso"):
            co.active_community_facts(ctx)
            co.active_community_facts(ctx)
            self.assertEqual(row.call_count, 1)
            ctx[CTX_KEY] = {"place_id": "other", "name": "Other"}
            co.active_community_facts(ctx)
            self.assertEqual(row.call_count, 2)


if __name__ == "__main__":
    unittest.main()


class HumanDescriptionIsNeverRegeneratedTests(unittest.TestCase):
    """The profile's description writer overwrote a creator's own words with a generated
    "a spot known to neighbors…" line on the first profile open (2026-10-01)."""

    def setUp(self) -> None:
        from app import community_surface as cs

        # Module-global: a mocked pool never runs the job that would discard the key.
        cs._BLURB_INFLIGHT.clear()

    def _blurb(self, **kw):
        from app import community_surface as cs

        with patch.object(cs, "_BLURB_POOL") as pool:
            out = cs._blurb(
                place_name="Founders Table", relation="community", area=None,
                features=[], members=2, place_id=PLACE, **kw,
            )
        return out, pool.submit.called

    def test_a_person_written_description_is_served_and_never_rewritten(self) -> None:
        mine = "a community for early-stage founders swapping honest notes"
        out, rewrote = self._blurb(stored=mine, stored_key=None)
        self.assertEqual(out, mine)
        self.assertFalse(rewrote)

    def test_a_generated_line_whose_facts_moved_is_still_refreshed(self) -> None:
        out, rewrote = self._blurb(stored="old generated line", stored_key="stale-key")
        self.assertTrue(rewrote)

    def test_creator_community_with_nothing_written_gets_nothing_invented(self) -> None:
        out, rewrote = self._blurb(stored=None, stored_key=None, generate=False)
        self.assertIsNone(out)
        self.assertFalse(rewrote)


class RosterNeverInventsNamesTests(unittest.TestCase):
    """Guests from a creator link have no name. Told to "anchor with two names" and
    handed none, the model wrote "names like Alex and Jordan" (2026-10-01)."""

    def _roster(self, members):
        from app import community_discovery as cd

        seen: dict = {}
        roster = {"members": members, "member_count": len(members), "curious_count": 0}
        with patch("app.community_surface.community_members", return_value=roster), patch(
            "app.reply_compose.compose_reply", side_effect=lambda **kw: seen.update(kw) or "ok"
        ):
            cd._roster_chat_turn(
                "u-me", community={"place_id": PLACE, "place_name": "Founders Table"},
                message="who else is in here?", session_ctx={},
            )
        return seen

    def test_nameless_members_are_never_named(self) -> None:
        seen = self._roster([{"peer_user_id": "g1", "nickname": None}, {"peer_user_id": "u-me", "me": True}])
        self.assertIn("never invent a name", "\n".join(seen["facts"]))
        self.assertNotIn("TWO names", seen["goal"])

    def test_named_members_can_still_be_named(self) -> None:
        seen = self._roster([{"peer_user_id": "g1", "nickname": "Ana"}, {"peer_user_id": "u-me", "me": True}])
        self.assertIn("Ana", "\n".join(seen["facts"]))
        self.assertIn("TWO names", seen["goal"])


class CreatorNameFallbackTests(unittest.TestCase):
    """"who created this?" inside Tommaso's own community got "I don't have info on who
    created Tommaso" — no creator_name feature row, but an operator (2026-10-01)."""

    def test_operator_is_the_creator_when_the_claim_form_stored_none(self) -> None:
        from app import community_opening as co

        with patch.object(co, "_operator_name", return_value="Tommaso") as op, patch(
            "app.auth.service_client"
        ) as sc:
            sc.return_value.table.return_value.select.return_value.eq.return_value.eq.return_value \
                .order.return_value.order.return_value.limit.return_value.execute.return_value.data = []
            self.assertEqual(co._creator_name(PLACE), "Tommaso")
            op.assert_called_once_with(PLACE)

    def test_stored_creator_name_wins(self) -> None:
        from app import community_opening as co

        with patch.object(co, "_operator_name", return_value="Operator") as op, patch(
            "app.auth.service_client"
        ) as sc:
            sc.return_value.table.return_value.select.return_value.eq.return_value.eq.return_value \
                .order.return_value.order.return_value.limit.return_value.execute.return_value.data = [
                    {"value": "Zenaide"}
                ]
            self.assertEqual(co._creator_name(PLACE), "Zenaide")
            op.assert_not_called()


class CreatorRosterWordingTests(unittest.TestCase):
    def test_a_creators_group_has_members_not_neighbours_who_go_there(self) -> None:
        from app import community_discovery as cd

        seen: dict = {}
        roster = {
            "members": [{"peer_user_id": "g1", "nickname": None}, {"peer_user_id": "me", "me": True}],
            "member_count": 3, "curious_count": 0,
        }
        with patch("app.community_surface.community_members", return_value=roster), patch(
            "app.reply_compose.compose_reply", side_effect=lambda **kw: seen.update(kw) or "ok"
        ):
            ctx = _ctx()
            cd._roster_chat_turn(
                "me", community={"place_id": PLACE, "place_name": "Tommaso"},
                message="can you show me other people?", session_ctx=ctx,
            )
        facts = "\n".join(seen["facts"])
        self.assertIn("Members: 3", facts)
        self.assertNotIn("People who go here", facts)
        self.assertIn("never say nearby", facts)
        self.assertEqual(ctx["peer_matches"][0]["matching_peer_label"], "Member of Tommaso")


class RecommendationsInsideCommunityRouteTests(unittest.TestCase):
    def test_router_prompt_sends_recommendation_asks_to_the_recommendation_engine(self) -> None:
        import app.discovery_slots as ds

        prompt = " ".join(v for v in vars(ds).values() if isinstance(v, str))
        self.assertIn("does it have any recommendations?", prompt)
        self.assertIn("NEVER community_ask='about'", prompt)


class RecommendationInsideCommunityTests(unittest.TestCase):
    """A signed-in follower with no ZIP asked "any good dentist here?" inside Founders
    Table and was asked for a ZIP before anything was looked up (2026-10-01)."""

    def _ask(self, *, ctx, tips):
        from app import discovery_route as dr

        with patch.object(dr, "_resolve_block_id_for_turn", return_value=None), patch.object(
            dr, "find_neighbor_tips", return_value=tips
        ) as find, patch.object(
            dr, "compose_reply", side_effect=lambda **kw: "REPLY:" + kw["goal"]
        ), patch.object(dr, "_stamp_tip_ask_draft"):
            out = dr._tip_seek_answer_turn(
                msg="any good dentist here?", detail="dentist", category=None,
                session_ctx=ctx, user_jwt="jwt", phone_verified=True, home_block_id=None,
                phase="listening", user_id="u1", active_intent="looking.tip",
            )
        return out, find

    def test_inside_a_community_the_zip_is_an_offer_not_a_gate(self) -> None:
        (reply, _c, routing, _p), find = self._ask(ctx=_ctx(), tips=[])
        # The community was actually searched, with no location.
        # First read is the community's own (the second is the usual widen, which with no
        # location finds nothing).
        kw = find.call_args_list[0].kwargs
        self.assertEqual(kw["circle_place_id"], PLACE)
        self.assertIsNone(kw["block_id"])
        self.assertIn("tip_seek_community_empty", str(routing))
        self.assertIn("none of its members has shared one", reply)
        self.assertNotIn("tip_seek_need_zip", str(routing))

    def test_outside_a_community_the_zip_gate_is_unchanged(self) -> None:
        (reply, _c, routing, _p), find = self._ask(ctx={}, tips=[])
        find.assert_not_called()
        self.assertIn("tip_seek_need_zip", str(routing))


class RecommendationLookBeyondTests(unittest.TestCase):
    """Inside a community with a location, an empty community used to widen SILENTLY to the
    neighbourhood and then Google — "what to eat" in Pausa answered with places nobody there
    had shared (Tommaso, 2026-10-01). Now it asks first, the way meets do."""

    def _ask(self, *, ctx, tips, msg="what to eat"):
        from app import discovery_route as dr

        with patch.object(dr, "_resolve_block_id_for_turn", return_value="blk1"), patch.object(
            dr, "find_neighbor_tips", return_value=tips
        ) as find, patch.object(
            dr, "compose_reply", side_effect=lambda **kw: "REPLY:" + kw["goal"]
        ), patch.object(dr, "_stamp_tip_ask_draft"), patch.object(
            dr, "_tip_seek_fallback_reply", return_value="GOOGLE"
        ) as google:
            out = dr._tip_seek_answer_turn(
                msg=msg, detail="somewhere to eat", category=None,
                session_ctx=ctx, user_jwt="jwt", phone_verified=True, home_block_id="blk1",
                phase="listening", user_id="u1", active_intent="looking.tip",
            )
        return out, find, google

    def test_empty_community_asks_before_looking_beyond(self) -> None:
        (reply, ctx, routing, peers), find, google = self._ask(ctx=_ctx(), tips=[])
        # Only the community was read — no silent neighbourhood widen, no Google.
        self.assertEqual(find.call_count, 1)
        self.assertEqual(find.call_args.kwargs["circle_place_id"], PLACE)
        google.assert_not_called()
        self.assertIn("tip_seek_community_widen_offer", str(routing))
        self.assertIn("look beyond the community", reply)
        self.assertEqual(ctx["rec_chips"][0]["message"], "Look beyond Tommaso")
        self.assertEqual(ctx["tip_community_chip"], "Look beyond Tommaso")
        self.assertFalse(ctx.get("tip_ask_offer"))
        self.assertEqual(peers, [])

    def test_offer_renders_the_look_beyond_pill(self) -> None:
        from app.ui_actions import derive_ui_actions

        (_r, ctx, _rt, _p), _f, _g = self._ask(ctx=_ctx(), tips=[])
        actions = derive_ui_actions(ctx, "chat")
        self.assertEqual([a["message"] for a in actions], ["Look beyond Tommaso"])

    def test_community_with_a_rec_is_unchanged(self) -> None:
        from app import discovery_route as dr

        tip = {"detail_text": "Pausa's pasta", "user_id": "u2"}
        with patch.object(dr, "stamp_tip_peer_surface", return_value=[tip]), patch.object(
            dr, "_compose_neighbor_tip_reply", return_value="TIPS"
        ), patch("app.reco_fit.finish_fit"), patch(
            "app.reco_kind_gate.keep_asked_kind", side_effect=lambda rows, _k: rows
        ), patch("app.reco_aspects.split_query_full", return_value={}):
            (reply, ctx, routing, _p), find, google = self._ask(ctx=_ctx(), tips=[tip])
        self.assertEqual(reply, "TIPS")
        self.assertIsNone(ctx.get("tip_community_chip"))
        google.assert_not_called()

    def test_tapping_look_beyond_releases_the_community_and_searches_wider(self) -> None:
        from app import discovery_route as dr
        from app.community_scope import RELEASED_KEY

        ctx = _ctx()
        ctx["tip_last_ask"] = {"detail": "somewhere to eat", "category": None}
        ctx["tip_community_chip"] = "Look beyond Tommaso"
        with patch.object(dr, "_tip_seek_answer_turn", return_value=("R", {}, {}, [])) as ans:
            out = dr._try_tip_cascade_control_turn(
                msg="Look beyond Tommaso", session_ctx=ctx, user_jwt="jwt",
                phone_verified=True, home_block_id="blk1", phase="listening", user_id="u1",
            )
        self.assertIsNotNone(out)
        self.assertIsNone(ctx[CTX_KEY])
        self.assertEqual(ctx[RELEASED_KEY], PLACE)
        self.assertIsNone(ctx["tip_community_chip"])
        self.assertEqual(ans.call_args.kwargs["detail"], "somewhere to eat")

    def test_other_messages_disarm_the_pill_and_keep_the_community(self) -> None:
        from app import discovery_route as dr

        ctx = _ctx()
        ctx["tip_last_ask"] = {"detail": "somewhere to eat", "category": None}
        ctx["tip_community_chip"] = "Look beyond Tommaso"
        out = dr._try_tip_cascade_control_turn(
            msg="actually, any good dentist?", session_ctx=ctx, user_jwt="jwt",
            phone_verified=True, home_block_id="blk1", phase="listening", user_id="u1",
        )
        self.assertIsNone(out)
        self.assertEqual(ctx[CTX_KEY]["place_id"], PLACE)
        self.assertIsNone(ctx["tip_community_chip"])


class CreatorFormAnswersTests(unittest.TestCase):
    """lana.help's "Community fit" answers live in place_features, not places.blurb. A new
    creator community therefore had the creator's own description and Lana read none of
    it (2026-10-01)."""

    ROW = {"name": "Beast Bros", "place_type": "creator", "blurb": "a spot known to neighbors",
           "first_action": "What's your all-time favourite MrBeast video?"}
    ANSWERS = {"shared_context": "Fans of MrBeast's big stunts, giveaways and philanthropy",
               "member_value": "Video ideas, challenge tips and premiere watch-alongs"}

    def test_facts_prefer_the_creators_words_over_a_stored_blurb(self) -> None:
        from app import community_opening as co

        ctx = {CTX_KEY: {"place_id": PLACE, "name": "Beast Bros"}}
        with patch.object(co, "_community_row", return_value=self.ROW), patch.object(
            co, "_creator_answers", return_value=self.ANSWERS
        ), patch.object(co, "_creator_name", return_value="Jimmy"):
            facts = co.active_community_facts(ctx)
            line = co.active_community_prompt_line(ctx)
        self.assertEqual(facts["about"], self.ANSWERS["shared_context"])
        self.assertEqual(facts["members_help"], self.ANSWERS["member_value"])
        self.assertIn("members help each other with: Video ideas", line)
        self.assertIn("a first question its creator expects", line)
        self.assertNotIn("spot known to neighbors", line)

    def test_opening_is_grounded_in_the_form_answers(self) -> None:
        from app import community_opening as co

        seen: dict = {}
        with patch.object(co, "_community_row", return_value=self.ROW), patch.object(
            co, "_creator_answers", return_value=self.ANSWERS
        ), patch.object(co, "_creator_name", return_value="Jimmy"), patch(
            "app.community_surface.caller_affiliation_at", return_value={"created_at": ""}
        ), patch.object(co, "compose_reply", side_effect=lambda **kw: seen.update(kw) or "ok"):
            co.community_opening({"place_id": PLACE}, user_id="u1")
        facts = "\n".join(seen["facts"])
        self.assertIn("MrBeast's big stunts", facts)
        self.assertIn("Video ideas, challenge tips", facts)
        self.assertIn("the card below asks it, never you", facts)

    def test_policy_sees_what_members_help_with(self) -> None:
        from app.policy.decide import _inside_community

        ctx = _ctx()
        ctx["_active_community_facts"]["members_help"] = "honest product feedback"
        self.assertEqual(_inside_community(ctx)["members_help"], "honest product feedback")
