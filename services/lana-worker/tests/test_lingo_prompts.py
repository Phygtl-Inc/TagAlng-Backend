"""LANA_LINGO §14.1 regression floor: the constitution is wired into every
reply-composing system prompt, and the prompt files never teach the model the
banned in-app lexicon again (mom/block-as-speech/circle). Backstage uses of
"block" (data model, memory block, recall scope) are allowed — this guards the
user-facing phrasings that were scrubbed, not the internal vocabulary."""

import re
import unittest
from pathlib import Path

from app.context import (
    build_event_host_system_prompt,
    build_profile_system_prompt,
    build_system_prompt,
    lingo_constitution,
)

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_CONSTITUTION_MARKER = "Lana lingo — how you choose words"

# User-facing phrasings that must never reappear in prompt files. Deliberately
# specific (not a bare \bblock\b) so backstage lines stay legal.
_BANNED_IN_PROMPTS = [
    r"\bmoms?\b",
    r"\bmamas?\b",
    r"\bmums?\b",
    r"\bmommy\b",
    r"block concierge",
    r"on (?:their|your|the) block\b",
    r"\bblock party\b",
    r"\bblock-level\b",
    r"\bcircles?\b",
]
_BANNED_RES = [re.compile(p, re.I) for p in _BANNED_IN_PROMPTS]


class TestConstitutionWired(unittest.TestCase):
    def test_constitution_file_loads(self) -> None:
        self.assertIn(_CONSTITUTION_MARKER, lingo_constitution())

    def test_all_reply_builders_include_constitution(self) -> None:
        for builder in (
            build_system_prompt,
            build_event_host_system_prompt,
            build_profile_system_prompt,
        ):
            self.assertIn(_CONSTITUTION_MARKER, builder(), builder.__name__)


class TestPromptLexicon(unittest.TestCase):
    def test_prompt_files_stay_lexicon_clean(self) -> None:
        violations: list[str] = []
        for path in sorted(_PROMPTS_DIR.glob("*.md")):
            # The constitution itself quotes banned words as negative examples.
            if path.name == "lana_lingo_constitution.md":
                continue
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for rx in _BANNED_RES:
                    if rx.search(line):
                        violations.append(f"{path.name}:{i}: {rx.pattern!r} in {line.strip()!r}")
        self.assertEqual(violations, [], "\n".join(violations))


class TestGuardCoversEveryHardRule(unittest.TestCase):
    def test_rule_three_bans_group_not_only_circle(self) -> None:
        """The constitution bans "circle(s)" AND "group" for a community; only "circle"
        was in the guard's pattern, so "I can add you to the group" shipped (QA
        2026-08-21)."""
        from app.lingo_guard import find_violations, naive_clean

        for text in (
            "I can add you to the group so you hear about future get-togethers.",
            "Te puedo agregar al grupo.",
        ):
            self.assertTrue(find_violations(text), text)
            cleaned = naive_clean(text)
            self.assertFalse(find_violations(cleaned), cleaned)
        self.assertIn("community", naive_clean("I can add you to the group."))

    def test_rule_six_bans_points_and_rank_in_the_score_frame(self) -> None:
        """Rule 6 names "points" and "rank"; the guard enforced only leaderboard/
        streak/level up, so asked "how many points do I have?" Lana replied "No
        points here — you're not being scored", speaking the frame she was
        denying (evals, 2026-08-25). Both words are ordinary English outside that
        frame, so the rule is scoped to the frame and the negatives matter as
        much as the positives."""
        from app.lingo_guard import find_violations, naive_clean

        for text in (
            "No points here, you're not being scored.",
            "You have 12 points so far.",
            "Want to see your rank?",
            "Nobody is ranking you against your neighbors.",
        ):
            self.assertTrue(find_violations(text), text)
            self.assertFalse(find_violations(naive_clean(text)), naive_clean(text))

        for legal in (
            "That points to the same spot you mentioned.",
            "The highest-ranked taco place near you is Rosa's.",
            "Meet them at the point by the lake.",
        ):
            self.assertFalse(find_violations(legal), legal)

    def test_the_banned_words_in_the_constitution_are_all_enforced(self) -> None:
        # Every word rule 3 names, checked against the pattern rather than trusting it.
        from app.lingo_guard import find_violations

        for word in ("circle", "circles", "group", "groups"):
            self.assertTrue(find_violations(f"your {word} nearby"), word)


class TestAddressGuidance(unittest.TestCase):
    """§3.3/§4: role/gender stamped per turn reach every constitution-bearing prompt."""

    def tearDown(self) -> None:
        from app.context import set_address_context

        set_address_context(None, None)

    def test_neutral_by_default(self) -> None:
        from app.context import address_guidance, set_address_context

        set_address_context(None, None)
        # No ROLE line when the role is unknown — that part is unchanged.
        self.assertNotIn("USER CONTEXT — household role", lingo_constitution())
        # But gender ALWAYS emits a line, including when unknown. Asserting "" here
        # is what encoded the defect: with no rule at all the composer had to pick
        # an agreement for words like bienvenido/bienvenida, and es/pt lean feminine
        # in a warm register — so unknown-gender users were greeted "¡Bienvenida!"
        # (eval 2026-09-01, lt_gender_es_unknown_neutral).
        self.assertIn("UNKNOWN", address_guidance())
        self.assertIn("NEVER default to the feminine form", address_guidance())

    def test_role_and_gender_reach_the_constitution(self) -> None:
        from app.context import address_guidance, set_address_context

        set_address_context("grandparent", "masculine")
        guidance = address_guidance()
        self.assertIn("your grandkids", guidance)
        self.assertIn("masculine", guidance)
        self.assertIn(guidance, lingo_constitution())
        self.assertIn(guidance, build_system_prompt())

    def test_unknown_role_stays_neutral(self) -> None:
        from app.context import address_guidance, set_address_context

        set_address_context("astronaut", None)
        # An unlisted role contributes nothing; the unknown-gender rule still rides.
        self.assertNotIn("USER CONTEXT — household role", address_guidance())
        self.assertNotIn("astronaut", address_guidance())

    def test_caregiver_framing_never_parent_label(self) -> None:
        from app.context import address_guidance, set_address_context

        set_address_context("caregiver", None)
        guidance = address_guidance()
        self.assertIn("the family you care for", guidance)
        self.assertIn("never a parent label", guidance)


if __name__ == "__main__":
    unittest.main()
