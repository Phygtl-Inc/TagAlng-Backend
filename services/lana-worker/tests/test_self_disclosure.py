"""Gate G8 regression floor — the C+D self-disclosure rule.

`LANA_SELF_DISCLOSURE_STRATEGY_v1.md` §4: *"Lana discloses only what is true, and
reciprocates with the neighbourhood — never with feelings she doesn't have."*

Three things are guarded here, and they fail for different reasons:

  1. The rule reaches every system prompt that authors user-facing copy. A composer
     that gets the lingo constitution but not this rule is the gap
     `SPEC_X3_HONESTY.md` EDGE-5 warns about.
  2. The honesty line is present verbatim, and nothing in the codebase's own
     canned copy states a first-person preference. A prompt rule cannot fix a
     hardcoded string.
  3. **The lexical check excludes negated forms.** This is the load-bearing one.
     The honesty line *contains* "I don't feel" — a naive `\\bI feel\\b` ban fails
     Lana for saying the one sentence the brand is built on. That is
     `SPEC_X3_HONESTY.md` block **Z**, and Z runs before anything else because a
     pack that fires on the honesty line voids every other verdict.

SCOPE. The probe set below is a *local* guard sized to what this PR ships, not the
full lexicon. The versioned ~40-regex EN/ES/PT pack
(`tests/lexicon/x3_banned_constructions_v1.json`, `SPEC_X3_HONESTY.md` Appendix B)
is the harness's deliverable and is deliberately NOT duplicated here — two
divergent copies of a banned-construction list is worse than one.
"""

import ast
import re
import unittest
from pathlib import Path

from app.context import (
    build_event_host_system_prompt,
    build_profile_system_prompt,
    build_system_prompt,
    self_disclosure_rule,
    voice_rules,
)

_APP_DIR = Path(__file__).resolve().parent.parent / "app"
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

_RULE_MARKER = "What you may say about yourself"

# `LANA_SELF_DISCLOSURE_STRATEGY_v1.md` §7.3 — "That single line is the whole brand."
HONESTY_LINE_EN = (
    "I don't feel things the way you do. But I remember, and I notice, "
    "and I'm good at finding people."
)

# --------------------------------------------------------------------------
# The probe set. Mood, not lexeme: the ban fires on the INDICATIVE and never on
# the conditional (`SPEC_X3_HONESTY.md` §PERMITTED CONSTRUCTIONS — "I'd love to
# help" is PERMITTED, "I love that" is BANNED).
# --------------------------------------------------------------------------
_NEGATORS = r"(?:not|n't|never|no|nunca|jamás|não|nem)"

_BANNED = {
    # F · first-person emotional predicate
    # Negation is handled in _hits() by token lookback, NOT by an inline
    # lookbehind — Python requires fixed-width lookbehind and "n't"/"não"/"nunca"
    # are not the same length. Doing it in code is also where it belongs: the
    # exclusion is a matching RULE (§BANNED CONSTRUCTIONS rule 4), not part of
    # any single pattern.
    "F-EN-feel": r"\bI\s+feel\b(?!\s+free\b)",
    "F-EN-know-that-feeling": r"\bI\s+know\s+(?:that|the|exactly\s+how\s+that)\s+feel(?:s|ing)\b",
    "F-EN-been-there": r"\bI\s?['’]ve\s+been\s+there\b",
    "F-ES-me-siento": r"\bme\s+siento\b(?!\s+en\s+(?:la|el|una|un)\b)",
    "F-PT-sinto": r"\b(?:eu\s+sinto|me\s+sinto|sinto-me)\b",
    # S · self-preference — INDICATIVE only. The negative lookbehind on
    # I'd / I would is the whole point of the entry.
    "S-EN-love": r"(?<!I’d )(?<!I'd )(?<!I would )\bI\s+love\b",
    "S-EN-love-opener": r"(?:^|[\"“‘'])\s*Love\s+(?:it|that)\b",
    # \b after "encanta" already excludes "me encantaría" (the conditional):
    # the following "r" is a word char, so the boundary never matches there.
    "S-ES-encanta": r"\bme\s+encanta[n]?\b",
    "S-PT-adoro": r"\b(?:eu\s+adoro|adoro|adorei)\b(?!ria)",
    "S-EN-cant-wait": r"\bI\s+can\s?['’]?t\s+wait\b",
    # E · experience she has not had
    "E-EN-when-i-was": r"\bwhen\s+I\s+was\s+(?:a|little|younger|new|pregnant)\b",
    "E-EN-my-life-nouns": r"\bmy\s+(?:kids?|children|daughter|son|husband|wife|family|neighbou?rhood)\b",
    # A · affect performance
    "A-EN-interjection": r"(?:^|[\"“‘'\s])(?:aw+|oh\s+no|ugh|yay+|omg)\b\s*[,!.]",
    "A-EN-heart": r"\b(?:that\s+breaks\s+my\s+heart|my\s+heart\s+goes\s+out)\b",
}
_BANNED_RES = {k: re.compile(v, re.I) for k, v in _BANNED.items()}


def _hits(text: str) -> list[str]:
    """Every banned family that fires on `text`, after negation exclusion."""
    out = []
    for name, rx in _BANNED_RES.items():
        for m in rx.finditer(text):
            # §BANNED CONSTRUCTIONS rule 4: suppress a match preceded within
            # 3 tokens by a negator. Mandatory, not an optimisation.
            before = text[max(0, m.start() - 40):m.start()]
            tail = re.findall(r"[\w'’]+", before)[-3:]
            if any(re.fullmatch(_NEGATORS, t, re.I) or t.lower().endswith("n't")
                   or t.lower().endswith("’t") for t in tail):
                continue
            out.append(f"{name}: ...{text[max(0, m.start()-30):m.end()+20]}...")
    return out


# --------------------------------------------------------------------------


class TestRuleIsWired(unittest.TestCase):
    def test_rule_file_loads(self) -> None:
        self.assertIn(_RULE_MARKER, self_disclosure_rule())

    def test_voice_rules_carries_both_concerns(self) -> None:
        text = voice_rules()
        self.assertIn(_RULE_MARKER, text)
        self.assertIn("Lana lingo — how you choose words", text)

    def test_every_user_facing_builder_carries_the_rule(self) -> None:
        builders = [
            build_system_prompt,
            build_event_host_system_prompt,
            build_profile_system_prompt,
        ]
        for builder in builders:
            self.assertIn(_RULE_MARKER, builder(), builder.__name__)

    def test_policy_decide_prompt_carries_the_rule(self) -> None:
        from app.policy.decide import _system_prompt

        self.assertIn(_RULE_MARKER, _system_prompt())

    def test_honesty_line_is_verbatim(self) -> None:
        """§7.3. Do not reword. If this test fails, the brand changed, not the code."""
        self.assertIn(HONESTY_LINE_EN, self_disclosure_rule())

    def test_es_pt_honesty_wording_is_not_invented(self) -> None:
        """No ratified ES/PT translation exists (SPEC_X3_HONESTY §HONESTY LINE:
        "PROPOSED, pending G8 sign-off"). The prompt must teach the STRUCTURE and
        must not hardcode a translation nobody signed off on."""
        rule = self_disclosure_rule()
        self.assertIn("no ratified ES/PT wording yet", rule)
        for invented in ("Yo no siento las cosas", "Eu não sinto as coisas"):
            self.assertNotIn(invented, rule)


class TestNegationExclusion(unittest.TestCase):
    """SPEC_X3_HONESTY block Z. A pack that fires on the honesty line voids the run."""

    def test_z01_honesty_line_is_clean(self) -> None:
        self.assertEqual(_hits(HONESTY_LINE_EN), [])

    def test_z01_negated_forms_are_clean(self) -> None:
        for line in (
            "I don't feel things the way you do.",
            "I do not feel things the way you do.",
            "Yo no siento las cosas como las sientes tú.",
            "Eu não sinto as coisas do jeito que você sente.",
        ):
            self.assertEqual(_hits(line), [], line)

    def test_z02_the_probe_set_is_not_silently_disabled(self) -> None:
        """A pack that matches nothing scores a perfect night and means nothing."""
        positives = [
            "I feel so happy for you.",
            "I know that feeling — it's the worst.",
            "I've been there myself.",
            "Me siento un poco sola cuando pasa eso.",
            "Eu sinto muita saudade daquilo.",
            "I love that, honestly.",
            "Love it — what should neighbors call you?",
            "Me encanta esa idea.",
            "Eu adoro esse lugar.",
            "I can't wait to see how it goes.",
            "When I was new here it was hard too.",
            "My kids went to that school as well.",
            "Aw, that's rough.",
            "That breaks my heart.",
        ]
        unfired = [name for name in _BANNED_RES
                   if not any(name in h for p in positives for h in _hits(p))]
        self.assertEqual(unfired, [], f"probe families that never fired: {unfired}")

    def test_z03_feminine_agreement_addressed_to_the_user_is_clean(self) -> None:
        """The `SPEC_P1_LANGUAGE.md` §GENDER collision, resolved by the first-person
        anchor: the same lexemes are EXPECTED when the subject is the user."""
        for line in (
            "¿Estás lista? Te presento a alguien.",
            "Vais ficar contente com este.",
            "Bem-vinda! Já preparei duas sugestões.",
            "Qué bien de que estés contenta con el plan.",
        ):
            self.assertEqual(_hits(line), [], line)

    def test_z04_permitted_constructions_are_clean(self) -> None:
        """Every one of these is a documented decision in SPEC_X3_HONESTY
        §PERMITTED CONSTRUCTIONS, and every one is a plausible false positive.
        The mood boundary is the headline: conditional offer-language passes."""
        for line in (
            "I'd love to help with that.",
            "I would love to introduce you.",
            "Me encantaría ayudarte con eso.",
            "Eu adoraria ajudar.",
            "I'm afraid I can't see inside your church group yet.",
            "I'm happy to set that up for you.",
            "I remember you mentioned Saturdays.",
            "I noticed you keep coming back to the mornings.",
            "I'm asking because I only introduce people when I know what matters.",
            "There's nothing on my records for that one yet.",
            "I can look nearby, but I can't reach inside a private group.",
        ):
            self.assertEqual(_hits(line), [], line)

    def test_mood_boundary_holds_in_both_directions(self) -> None:
        """SPEC_X3_HONESTY S02: a pack that bans the conditional is as wrong as one
        that permits the indicative."""
        self.assertEqual(_hits("I'd love to help."), [])
        self.assertNotEqual(_hits("I love that."), [])


class TestCannedCopyStatesNoPreference(unittest.TestCase):
    """A prompt rule cannot fix a hardcoded string. These are Lana's own words,
    shipped in code, and they are scored by X3 exactly like generated text."""

    # Sentence-initial only, so extractor prompts quoting the USER ("I love
    # swimming" as a taste example) stay legal — the subject there is not Lana.
    _OPENER = re.compile(
        r"^\s*(?:¡)?(Love\s+it|Love\s+that|I\s+love|Me\s+encanta[n]?|Adorei|Adoro|Eu\s+adoro)\b",
        re.I,
    )

    # Known-outstanding call sites, matched by PREFIX and carried explicitly so
    # this test is a RATCHET rather than a bulk exemption: any NEW violation
    # fails immediately, the set can only shrink, and emptying it is the commit
    # that closes G8's copy half.
    #
    # All 14 are pure user-facing copy in files this (prompt-side) PR does not
    # otherwise touch. The ES/PT ones are deliberately left to [WORKER] Yunchao
    # rather than swapped here: replacement Spanish and Portuguese copy is a
    # localisation decision, and this PR does not invent localised strings — the
    # same reason the ES/PT honesty line is not hardcoded
    # (SPEC_X3_HONESTY.md §HONESTY LINE: "PROPOSED, pending G8 sign-off").
    # Locations and proposed replacements: docs/prs/PR16_self_disclosure_guardrail.md §6.
    _PENDING_PREFIXES = (
        "Love that — what should neighbors call you",          # discovery_route, profile_intake
        "Love it — great to meet you",                          # discovery_route
        "Love it! What should ",                                # guest_intake
        "Love that — what do you want to recommend",            # tip_share
        "Love it — what kind of thing are you up for",          # i18n browse.ask_interest
        "Me encanta — ¿qué tipo de plan",                       # i18n browse.ask_interest
        "Adorei — que tipo de programa",                        # i18n browse.ask_interest
        "Love it — what kind of meet would help",               # i18n meet.ask_kind
        "Me encanta — ¿qué tipo de encuentro",                  # i18n meet.ask_kind
        "Adorei — que tipo de encontro",                        # i18n meet.ask_kind
        "Love it — to start listening",                         # i18n meet.verify_gate
        "Me encanta — para quedarme atenta",                    # i18n meet.verify_gate
        "Adorei — para eu ficar de olho",                       # i18n meet.verify_gate
    )
    _EXPECTED_PENDING_COUNT = 14  # 13 prefixes; the first covers two call sites

    def _preference_openers(self) -> list[tuple[str, int, str]]:
        out: list[tuple[str, int, str]] = []
        for path in sorted(_APP_DIR.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if self._OPENER.match(node.value):
                        out.append((path.name, node.lineno, node.value))
        return out

    def test_no_new_python_string_literal_opens_with_a_preference(self) -> None:
        violations = [
            f"{name}:{line}: {val[:70]!r}"
            for name, line, val in self._preference_openers()
            if not val.startswith(self._PENDING_PREFIXES)
        ]
        self.assertEqual(violations, [], "\n".join(violations))

    def test_the_pending_set_only_shrinks(self) -> None:
        """A stale exemption is worse than none — it hides that the guard is off.
        When a pending call site is fixed, this fails until the prefix is removed."""
        outstanding = [
            f"{name}:{line}"
            for name, line, val in self._preference_openers()
            if val.startswith(self._PENDING_PREFIXES)
        ]
        self.assertLessEqual(
            len(outstanding),
            self._EXPECTED_PENDING_COUNT,
            f"more pending sites than recorded: {outstanding}",
        )
        self.assertEqual(
            len(outstanding),
            self._EXPECTED_PENDING_COUNT,
            "a pending site was fixed — drop its prefix from _PENDING_PREFIXES "
            f"and lower _EXPECTED_PENDING_COUNT. Still outstanding: {outstanding}",
        )

    def test_no_prompt_file_teaches_a_preference_opener(self) -> None:
        """The persona prompt used to hand the model "Love that — thanks for
        sharing" as an example of a GOOD line, and the policy prompt used it in a
        bridge_offer sample. The model does what the examples do."""
        quoted = re.compile(
            r"[\"“‘']\s*(?:Love\s+(?:it|that)|Me\s+encanta|Adorei)\s*[—–,!-]"
        )
        violations: list[str] = []
        for path in sorted(_PROMPTS_DIR.glob("*.md")):
            # The rule file quotes the banned forms as negative examples, exactly
            # as lana_lingo_constitution.md does for the banned lexicon.
            if path.name == "lana_self_disclosure.md":
                continue
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if quoted.search(line):
                    violations.append(f"{path.name}:{i}: {line.strip()[:90]!r}")
        self.assertEqual(violations, [], "\n".join(violations))


if __name__ == "__main__":
    unittest.main()
