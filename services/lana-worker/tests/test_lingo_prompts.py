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


class TestAddressGuidance(unittest.TestCase):
    """§3.3/§4: role/gender stamped per turn reach every constitution-bearing prompt."""

    def tearDown(self) -> None:
        from app.context import set_address_context

        set_address_context(None, None)

    def test_neutral_by_default(self) -> None:
        from app.context import address_guidance, set_address_context

        set_address_context(None, None)
        self.assertEqual(address_guidance(), "")
        # The constitution md mentions "USER CONTEXT" in its rules; the appended
        # per-user guidance lines must be absent when nothing is known.
        self.assertNotIn("USER CONTEXT — household role", lingo_constitution())
        self.assertNotIn("USER CONTEXT — grammatical gender", lingo_constitution())

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
        self.assertEqual(address_guidance(), "")

    def test_caregiver_framing_never_parent_label(self) -> None:
        from app.context import address_guidance, set_address_context

        set_address_context("caregiver", None)
        guidance = address_guidance()
        self.assertIn("the family you care for", guidance)
        self.assertIn("never a parent label", guidance)


if __name__ == "__main__":
    unittest.main()
