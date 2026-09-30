"""A chat that opens inside a community greets them inside it (app/community_opening.py).

The bug: POST /lana/sessions composed the opening BEFORE apply_community_selection ran,
so a follower arriving from a creator's link was greeted like any neighbour — and a
resumed thread ignored the selection altogether. These tests drive the real endpoint
function with the database and auth patched out, so the ORDER inside main.py is what is
under test, not a helper in isolation.
"""

import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from app import community_opening as co
from app.community_scope import CTX_KEY

PLACE = "10000000-0000-0000-0000-000000000001"
USER = "00000000-0000-0000-0000-00000000000c"
ROW = {
    "name": "Etiqueta do Reino",
    "place_type": "creator",
    "blurb": "a spot focused on social and dining etiquette",
    "first_action": "Share the table rule you wish everyone knew",
}


def _recent() -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()


def _old() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()


class CommunityOpeningTests(unittest.TestCase):
    def _run(self, *, joined_at: str, row: dict | None = None) -> tuple[str | None, dict]:
        seen: dict = {}

        def fake_compose(**kw):
            seen.update(kw)
            return "LLM:" + kw["fallback"]

        with patch.object(co, "_community_row", return_value=row or ROW), patch.object(
            co, "_creator_name", return_value="Zenaide"
        ), patch(
            "app.community_surface.caller_affiliation_at",
            return_value={"created_at": joined_at},
        ), patch.object(co, "compose_reply", side_effect=fake_compose):
            line = co.community_opening({"place_id": PLACE, "name": "x"}, user_id=USER)
        return line, seen

    def test_grounded_in_the_communitys_own_words(self) -> None:
        line, seen = self._run(joined_at=_recent())
        self.assertTrue(line and line.startswith("LLM:"))
        facts = "\n".join(seen["facts"])
        self.assertIn("Etiqueta do Reino", facts)
        self.assertIn("dining etiquette", facts)
        self.assertIn("table rule", facts)
        self.assertIn("Zenaide", facts)
        self.assertIn("just joined", facts)
        # The whole point: never the ZIP / tell-me-about-yourself opener.
        self.assertIn("Do not ask for their ZIP", seen["goal"])
        self.assertIn("SUBJECT", seen["goal"])

    def test_existing_member_is_greeted_not_welcomed(self) -> None:
        _, seen = self._run(joined_at=_old())
        self.assertIn("already a member", "\n".join(seen["facts"]))
        self.assertNotIn("Welcome them", seen["goal"])
        self.assertIn("returning member", seen["goal"])

    def test_undescribed_community_is_never_guessed_from_its_name(self) -> None:
        _, seen = self._run(joined_at=_recent(), row={"name": "Big Bros", "place_type": "creator"})
        self.assertIn("never guess what it is about from its name", "\n".join(seen["facts"]))

    def test_no_place_or_no_name_keeps_the_generic_opening(self) -> None:
        self.assertIsNone(co.community_opening(None, user_id=USER))
        self.assertIsNone(co.community_opening({"place_id": ""}, user_id=USER))
        with patch.object(co, "_community_row", return_value={}):
            self.assertIsNone(co.community_opening({"place_id": PLACE}, user_id=USER))

    def test_a_read_failure_never_breaks_the_session(self) -> None:
        with patch.object(co, "_community_row", side_effect=RuntimeError("db down")):
            self.assertIsNone(co.community_opening({"place_id": PLACE}, user_id=USER))


class CreateSessionOrderingTests(unittest.TestCase):
    """The endpoint itself: the opening must be composed WITH the community."""

    def _call(
        self,
        *,
        community_id: str | None,
        member: bool = True,
        resumed: bool = False,
        stored_ctx: dict | None = None,
        is_anonymous: bool = True,
    ):
        from app import main
        from app.main import CreateSessionRequest

        inserted: list[str] = []
        auth = SimpleNamespace(
            user_id=USER,
            is_anonymous=is_anonymous,
            home_block_id=None,
            phone_verified=False,
            role=None,
            grammatical_gender=None,
        )
        session = {"id": "s1", "context": dict(stored_ctx or {})}
        history = [{"role": "assistant", "content": "earlier line", "id": "m0", "metadata": {}}]

        def fake_insert(_sid, _role, content, _meta):
            inserted.append(content)
            return f"m{len(inserted)}"

        with ExitStack() as st:
            p = lambda *a, **k: st.enter_context(patch(*a, **k))  # noqa: E731
            p("app.main._vertex_required")
            p("app.main.verify_auth", return_value=auth)
            p("app.main.require_home_block_for_purpose")
            p("app.main.create_session", return_value=(session, resumed))
            p("app.main.list_messages", return_value=history)
            p("app.main.insert_message", side_effect=fake_insert)
            p("app.main.update_session_context")
            p("app.main.get_user_preferred_language", return_value=None)
            p("app.main.user_needs_display_name", return_value=False)
            p("app.main._onboarding_fields", return_value={})
            p("app.main._offered_chip_messages", return_value=[])
            p("app.lana_dispatch.compose_reply", side_effect=lambda **kw: kw["fallback"])
            p(
                "app.community_surface.caller_affiliation_at",
                return_value={"created_at": _recent()} if member else None,
            )
            p("app.community_surface._place_row", return_value={"name": ROW["name"]})
            p("app.community_opening._community_row", return_value=ROW)
            p("app.community_opening._creator_name", return_value="Zenaide")
            p(
                "app.community_opening.compose_reply",
                side_effect=lambda **kw: "COMMUNITY:" + kw["fallback"],
            )
            p("app.db.pop_login_carry", return_value=None)
            p("app.tip_ask_route.opening_for_pending_ask", return_value=None)
            body = CreateSessionRequest(purpose="lana", community_id=community_id)
            resp = main.create_lana_session(body, authorization="Bearer x", accept_language=None)
        return resp, inserted

    def test_new_session_from_a_creator_link_opens_inside_the_community(self) -> None:
        resp, inserted = self._call(community_id=PLACE)
        self.assertTrue(resp.assistant_message.startswith("COMMUNITY:"), resp.assistant_message)
        self.assertIn("Etiqueta do Reino", resp.assistant_message)
        self.assertEqual(inserted, [resp.assistant_message])

    def test_no_community_keeps_the_generic_opening(self) -> None:
        resp, _ = self._call(community_id=None)
        self.assertFalse(resp.assistant_message.startswith("COMMUNITY:"))

    def test_not_a_member_is_never_scoped_or_greeted_as_one(self) -> None:
        resp, _ = self._call(community_id=PLACE, member=False)
        self.assertFalse(resp.assistant_message.startswith("COMMUNITY:"))

    def test_resumed_thread_that_enters_a_new_community_says_so(self) -> None:
        resp, inserted = self._call(community_id=PLACE, resumed=True, stored_ctx={})
        self.assertTrue(resp.assistant_message.startswith("COMMUNITY:"))
        self.assertEqual(inserted, [resp.assistant_message])

    def test_resumed_thread_already_in_that_community_just_resumes(self) -> None:
        resp, inserted = self._call(
            community_id=PLACE,
            resumed=True,
            stored_ctx={CTX_KEY: {"place_id": PLACE, "name": ROW["name"]}},
        )
        self.assertEqual(resp.assistant_message, "earlier line")
        self.assertEqual(inserted, [])


if __name__ == "__main__":
    unittest.main()
