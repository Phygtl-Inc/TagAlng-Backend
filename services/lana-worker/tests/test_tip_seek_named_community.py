"""A recommendation ask that NAMES a community is read inside it.

Prod 2026-10-07 (Pouya, SJSU): "any recommendations for a beginner friendly project program
at San Jose State University?" from the main chat (no active community) searched the asker's
Minneapolis ZIP and answered from Google, though a member had shared exactly that.
"""

import unittest
from unittest.mock import patch

SJSU = "95c614e8-749b-484e-bd33-9bf9bc005908"


class NamedCommunityScopeTests(unittest.TestCase):
    def _named(self, *, hit, said="San Jose State"):
        from app.community_scope import named_community_scope

        with patch(
            "app.community_discovery.resolve_community_name", return_value={"hit": hit}
        ) as resolve:
            out = named_community_scope("u1", said)
        return out, resolve

    def test_the_named_community_becomes_the_scope(self) -> None:
        out, resolve = self._named(hit={"place_id": SJSU, "place_name": "San Jose State University"})
        self.assertEqual(out, {"place_id": SJSU, "name": "San Jose State University"})
        resolve.assert_called_once_with("u1", "San Jose State")

    def test_membership_is_not_required(self) -> None:
        # Community recommendations are open to anyone (20270126120000): the scope is
        # granted without ever asking whether the caller belongs there.
        with patch("app.community_surface.caller_affiliation_at", return_value=None) as aff:
            out, _r = self._named(hit={"place_id": SJSU, "place_name": "San Jose State University"})
        self.assertEqual(out["place_id"], SJSU)
        aff.assert_not_called()

    def test_an_unresolved_name_or_a_failure_is_no_scope(self) -> None:
        self.assertIsNone(self._named(hit=None)[0])
        from app.community_scope import named_community_scope

        with patch(
            "app.community_discovery.resolve_community_name", side_effect=RuntimeError("db")
        ):
            self.assertIsNone(named_community_scope("u1", "San Jose State"))
        self.assertIsNone(named_community_scope("u1", None))


class SaveSignalPassesTheNamedScopeTests(unittest.TestCase):
    def _turn(self, slots, scope):
        from app import discovery_route as dr

        with patch("app.lana_paths.tip_ask_consent_enabled", return_value=True), patch(
            "app.community_scope.named_community_scope", return_value=scope
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
