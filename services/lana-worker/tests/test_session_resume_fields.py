"""POST /lana/sessions says whether the thread is resumed and how far in it is (§21).

A cold open of a mid-flow session replays the last assistant line with its ui, which is
byte-identical in shape to a fresh greeting. `resumed` + `user_turn_count` let the client
tell "never spoken into" from "fifteen turns deep" without a second history read. These
tests drive the real endpoint function with the database and auth patched out.
"""

import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

USER = "00000000-0000-0000-0000-00000000000c"


def _msg(role: str, i: int) -> dict:
    return {"role": role, "content": f"{role} {i}", "id": f"m{i}", "metadata": {}}


class SessionResumeFieldsTests(unittest.TestCase):
    def _call(self, *, resumed: bool, history: list[dict], force_new: bool = False):
        from app import main
        from app.main import CreateSessionRequest

        auth = SimpleNamespace(
            user_id=USER,
            is_anonymous=True,
            home_block_id=None,
            phone_verified=False,
            role=None,
            grammatical_gender=None,
        )
        session = {"id": "s1", "context": {}}
        with ExitStack() as st:
            p = lambda *a, **k: st.enter_context(patch(*a, **k))  # noqa: E731
            p("app.main._vertex_required")
            p("app.main.verify_auth", return_value=auth)
            p("app.main.require_home_block_for_purpose")
            create = p("app.main.create_session", return_value=(session, resumed))
            p("app.main.list_messages", return_value=history)
            p("app.main.insert_message", return_value="m-new")
            p("app.main.update_session_context")
            p("app.main.get_user_preferred_language", return_value=None)
            p("app.main.user_needs_display_name", return_value=False)
            p("app.main._onboarding_fields", return_value={})
            p("app.main._offered_chip_messages", return_value=[])
            p("app.lana_dispatch.compose_reply", side_effect=lambda **kw: kw["fallback"])
            p("app.db.pop_login_carry", return_value=None)
            p("app.tip_ask_route.opening_for_pending_ask", return_value=None)
            body = CreateSessionRequest(purpose="lana", force_new=force_new)
            resp = main.create_lana_session(body, authorization="Bearer x", accept_language=None)
            self.assertEqual(create.call_args.kwargs.get("force_new"), force_new)
        return resp

    def test_cold_open_of_a_mid_flow_session_reports_its_turns(self) -> None:
        history = []
        for i in range(3):
            history += [_msg("assistant", 2 * i), _msg("user", 2 * i + 1)]
        history.append(_msg("assistant", 99))
        resp = self._call(resumed=True, history=history)
        self.assertTrue(resp.resumed)
        self.assertEqual(resp.user_turn_count, 3)
        self.assertEqual(resp.assistant_message, "assistant 99")

    def test_resumed_greeting_nobody_answered_is_zero_turns(self) -> None:
        resp = self._call(resumed=True, history=[_msg("assistant", 0)])
        self.assertTrue(resp.resumed)
        self.assertEqual(resp.user_turn_count, 0)

    def test_force_new_is_a_fresh_zero_turn_session(self) -> None:
        resp = self._call(resumed=False, history=[], force_new=True)
        self.assertFalse(resp.resumed)
        self.assertEqual(resp.user_turn_count, 0)

    def test_fields_are_on_the_wire(self) -> None:
        resp = self._call(resumed=True, history=[_msg("user", 1), _msg("assistant", 2)])
        wire = resp.model_dump(mode="json")
        self.assertIs(wire["resumed"], True)
        self.assertEqual(wire["user_turn_count"], 1)


if __name__ == "__main__":
    unittest.main()
