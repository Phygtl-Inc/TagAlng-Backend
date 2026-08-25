"""The 2026-08-21 turn where one message caused two bugs at once.

Tim typed "sign me up for the badminton". It matched neither _RSVP_RE (which
lacked the "me" slot) nor _SIGNUP_INTENT_RE (whose lookahead excludes "... for"),
so it fell through to the AI slots, came back activity_browse, and he got the
browse list — no RSVP, no failure signalled. The same message then reached the
background claim extractor, which read the command as a self-description and
wrote a durable `Plays badminton` claim, which minted a rapport gap, which became
a tile question about a sport he had never claimed.
"""

import re
import unittest
from pathlib import Path

from app.discovery_route import _SIGNUP_INTENT_RE, wants_rsvp_intent, wants_verify_help


class TestRsvpCommandRouting(unittest.TestCase):
    def test_sign_me_up_for_is_rsvp(self) -> None:
        for msg in (
            "sign me up for the badminton",
            "Sign me up for the pickleball meetup",
            "sign us up for the tennis rally",
            "sign up for the badminton",
        ):
            with self.subTest(msg=msg):
                self.assertTrue(wants_rsvp_intent(msg))

    def test_account_signup_is_still_not_rsvp(self) -> None:
        """The two matchers must stay disjoint — "sign me up" with no object is
        account creation, and routing it to RSVP would break the signup funnel."""
        for msg in ("sign me up", "sign me up!", "create an account"):
            with self.subTest(msg=msg):
                self.assertFalse(wants_rsvp_intent(msg))
        self.assertTrue(_SIGNUP_INTENT_RE.search("sign me up"))
        self.assertIsNone(_SIGNUP_INTENT_RE.search("sign me up for the badminton"))


class TestVerifyHelpRouting(unittest.TestCase):
    """Tim asked to be verified three times and got "follow the app's prompts"
    three times. Two of the three phrasings matched this matcher and still never
    reached the gate (the PHASE_PREVIEW branch claimed the turn first, classified
    find_peers); the third — the imperative — did not match at all."""

    def test_imperative_and_interrogative_both_match(self) -> None:
        for msg in (
            "get me verified",
            "get us verified",
            "how do I get verified? then how did I found Pouya earlier?",
            "so...how do I get verified?",
            "I need to verify",
        ):
            with self.subTest(msg=msg):
                self.assertTrue(wants_verify_help(msg))

    def test_verify_gate_is_hoisted_above_the_preview_branch(self) -> None:
        """Structural: the explicit check must appear BEFORE `if phase == PHASE_PREVIEW`
        in handle_discovery_turn, or the peers handler swallows the turn again."""
        src = Path(__file__).resolve().parent.parent / "app" / "discovery_route.py"
        lines = src.read_text(encoding="utf-8").splitlines()
        preview = next(
            i for i, ln in enumerate(lines) if ln.strip() == "if phase == PHASE_PREVIEW:"
        )
        gate = next(
            i for i, ln in enumerate(lines)
            if "wants_verify_help(msg) or goal ==" in ln
        )
        self.assertLess(gate, preview)


class TestCommandIsNotAClaim(unittest.TestCase):
    """The extractor is an LLM, so the rule lives in its prompt. Assert the rule is
    present and reachable — a silent deletion is how the claim leak returns."""

    def test_prompt_excludes_commanded_targets(self) -> None:
        src = Path(__file__).resolve().parent.parent / "app" / "vertex_extract.py"
        text = src.read_text(encoding="utf-8")
        self.assertIn("A COMMAND is a request, not a self-description", text)
        # The exact turn that shipped the bad claim, and the carve-out that keeps
        # volunteered background capturable.
        self.assertIn("sign me up for the badminton", text)
        self.assertIn("I've played since college", text)

    def test_prompt_still_attributes_a_kids_interest_to_the_kid(self) -> None:
        """The other way a claim lands on the wrong person, and the one that put
        live rows in prod: on 2026-08-03 "The younger one loves Lego" and "The
        older one is getting into basketball" were both stored subject_kind=self,
        so a parent matched neighbours on their children's hobbies. Re-extracted
        on 2026-08-21 from near-identical wording, both came back subject=child.
        The rule that fixed it is prompt-side, so it can be deleted silently.
        """
        src = Path(__file__).resolve().parent.parent / "app" / "vertex_extract.py"
        text = src.read_text(encoding="utf-8")
        self.assertIn('Use "child" for anything the user says about their kid', text)
        self.assertIn("a fact about anyone else (spouse, parent, neighbour)", text)


if __name__ == "__main__":
    unittest.main()


class TestUnverifiedPeerDisclosure(unittest.TestCase):
    """Lana told Tim "I keep details private until verification" while the payload
    carried the nickname and the card rendered it (prod 2026-08-21 14:57). The gate
    lives in peers_to_match_rows so all call sites inherit it; the prose gate in
    format_preview_message must agree, or the copy is false either way.
    """

    _PEER = {
        "peer_user_id": "abc",
        "nickname": "Pouya",
        "avatar_url": "u",
        "matching_peer_label": "badminton",
    }

    def test_unverified_viewer_gets_no_identity(self) -> None:
        from app.layer1_handlers import peers_to_match_rows

        row = peers_to_match_rows([self._PEER], phone_verified=False)[0]
        self.assertIsNone(row["nickname"])
        self.assertIsNone(row["peer_user_id"])
        self.assertIsNone(row["avatar_url"])
        self.assertTrue(row["preview"])

    def test_verified_viewer_still_gets_everything(self) -> None:
        from app.layer1_handlers import peers_to_match_rows

        row = peers_to_match_rows([self._PEER], phone_verified=True)[0]
        self.assertEqual(row["nickname"], "Pouya")
        self.assertEqual(row["peer_user_id"], "abc")
        self.assertFalse(row["preview"])

    def test_prose_never_names_a_peer_to_an_unverified_reader(self) -> None:
        from app.discovery_route import format_preview_message

        msg = format_preview_message([self._PEER], "Lake Nona", phone_verified=False)
        self.assertNotIn("Pouya", msg)
        # Tim's #9: the header's colon promised a list that redaction removes.
        self.assertNotIn(":", msg.splitlines()[0])

    def test_prose_still_names_peers_to_a_verified_reader(self) -> None:
        from app.discovery_route import format_preview_message

        msg = format_preview_message([self._PEER], "Lake Nona", phone_verified=True)
        self.assertIn("• Pouya", msg)
        self.assertTrue(msg.splitlines()[0].endswith(":"))
