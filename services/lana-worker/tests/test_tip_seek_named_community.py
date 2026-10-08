"""A recommendation ask that NAMES a community is read inside it.

Prod 2026-10-07 (Pouya, SJSU): "any recommendations for a beginner friendly project program
at San Jose State University?" from the main chat (no active community) searched the asker's
Minneapolis ZIP — where a community's tips never appear — and answered from Google, though
the asker was a confirmed SJSU member and a member had shared exactly that.
"""

import unittest
from unittest.mock import patch

SJSU = "95c614e8-749b-484e-bd33-9bf9bc005908"


class NamedMemberCommunityTests(unittest.TestCase):
    def _named(self, *, hit, member, said="San Jose State"):
        from app.community_scope import named_member_community

        with patch(
            "app.community_discovery.resolve_community_name", return_value={"hit": hit}
        ) as resolve, patch(
            "app.community_surface.caller_affiliation_at",
            return_value={"id": "a"} if member else None,
        ) as aff:
            out = named_member_community("u1", said)
        return out, resolve, aff

    def test_a_confirmed_member_gets_the_named_community_as_scope(self) -> None:
        out, resolve, aff = self._named(
            hit={"place_id": SJSU, "place_name": "San Jose State University"}, member=True
        )
        self.assertEqual(out, {"place_id": SJSU, "name": "San Jose State University"})
        resolve.assert_called_once_with("u1", "San Jose State")
        # Confirmed only: that is who find_neighbor_tips lets read a community's tips.
        self.assertEqual(aff.call_args.kwargs["statuses"], ("confirmed",))

    def test_a_non_member_is_not_scoped_there(self) -> None:
        out, _r, _a = self._named(
            hit={"place_id": SJSU, "place_name": "San Jose State University"}, member=False
        )
        self.assertIsNone(out)

    def test_an_unresolved_name_or_a_failure_is_no_scope(self) -> None:
        self.assertIsNone(self._named(hit=None, member=True)[0])
        from app.community_scope import named_member_community

        with patch(
            "app.community_discovery.resolve_community_name", side_effect=RuntimeError("db")
        ):
            self.assertIsNone(named_member_community("u1", "San Jose State"))
        self.assertIsNone(named_member_community("u1", None))


class SaveSignalPassesTheNamedScopeTests(unittest.TestCase):
    def _turn(self, slots, scope):
        from app import discovery_route as dr

        with patch("app.lana_paths.tip_ask_consent_enabled", return_value=True), patch(
            "app.community_scope.named_member_community", return_value=scope
        ) as named, patch.object(
            dr, "_tip_seek_answer_turn", return_value=("R", {}, {}, [])
        ) as ans:
            dr._try_save_signal_turn(
                msg="any beginner friendly project program at San Jose State?",
                slots=slots, session_ctx={}, user_jwt="jwt", phone_verified=True,
                home_block_id="blk1", phase="listening", user_id="u1",
            )
        return named, ans

    def _slots(self, community_name):
        return {
            "goal": "save_signal", "linear_intent": "looking.tip", "confidence": 0.95,
            "signal_intent": "tip_seek", "signal_detail": "beginner friendly project program",
            "community_name": community_name,
        }

    def test_the_named_community_reaches_the_answer_turn(self) -> None:
        scope = {"place_id": SJSU, "name": "San Jose State University"}
        named, ans = self._turn(self._slots("San Jose State"), scope)
        named.assert_called_once_with("u1", "San Jose State")
        self.assertEqual(ans.call_args.kwargs["community"], scope)

    def test_no_name_means_no_lookup_and_no_scope(self) -> None:
        named, ans = self._turn(self._slots(None), None)
        named.assert_not_called()
        self.assertIsNone(ans.call_args.kwargs["community"])


class AnswerTurnSearchesTheNamedCommunityTests(unittest.TestCase):
    def test_the_search_is_scoped_to_it_without_an_active_community(self) -> None:
        from app import discovery_route as dr

        with patch.object(dr, "_resolve_block_id_for_turn", return_value="blk-mpls"), \
                patch.object(dr, "find_neighbor_tips", return_value=[]) as find, \
                patch.object(dr, "compose_reply", side_effect=lambda **kw: "REPLY"), \
                patch.object(dr, "_stamp_tip_ask_draft"), \
                patch.object(dr, "_tip_seek_fallback_reply", return_value="GOOGLE") as google:
            _r, ctx, routing, _p = dr._tip_seek_answer_turn(
                msg="any beginner friendly project program at San Jose State?",
                detail="beginner friendly project program", category=None,
                session_ctx={}, user_jwt="jwt", phone_verified=True,
                home_block_id="blk-mpls", phase="listening", user_id="u1",
                active_intent="looking.tip",
                community={"place_id": SJSU, "name": "San Jose State University"},
            )
        self.assertEqual(find.call_args_list[0].kwargs["circle_place_id"], SJSU)
        # Empty inside the named community asks before looking past it — never Google.
        google.assert_not_called()
        self.assertIn("tip_seek_community_widen_offer", str(routing))
        self.assertEqual(ctx["tip_community_chip"], "Look beyond San Jose State University")
        # Scope for this search only: the session's community is not changed.
        self.assertFalse(ctx.get("active_community"))


if __name__ == "__main__":
    unittest.main()
