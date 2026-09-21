"""
validators.py — the three AnswerValidatorPorts of Arm B.

  shipped    what the product does TODAY. The measurement.
  reference  a spec-faithful validator, so the fixtures can be shown to be scoreable at all.
  dry        deterministic, no key, no repo import — for --dry-run.

WHY `shipped` IS A MIRROR AND NOT AN IMPORT
-------------------------------------------
Two of the three rules it models live inline in a FastAPI endpoint body
(app/main.py:2540-2551) and cannot be imported without standing up the app. The third,
`missing_required`, IS imported and called for real.

Mirroring carries drift risk, and this suite has been burnt by drift before — a stale caveat
printed into every circles_zip report for weeks. So the mirror is handled the way
policy_eval/lingo_guardrail.py handles the same problem: the mirrored lines carry their
citation, and selftest.py asserts the mirror against the real endpoint whenever a worker is
reachable. When the two disagree, the harness says so instead of scoring.

WHY `reference` EXISTS
----------------------
Not as a proposed patch — this suite evaluates and does not fix (CLAUDE.md). It is here for
two reasons:

  1. A fixture file where NOTHING can score well measures nothing. If `shipped` accepts every
     junk answer and no validator can do better, the honest conclusion is that the labels are
     unreasonable, not that the product is broken. `reference` is the control that rules that
     out.
  2. §12.3 and the standup's "Add AI Validator" action item both describe a validator that
     does not exist yet. Having one here that scores against hand-labels gives that work a
     target it can be measured against on day one, instead of shipping and hoping.

It is AI-first with a deterministic floor, which is the convention the product's own readers
already follow (app/tip_ask_ai.py, [[no-new-regex-use-ai-signals]]): an "is this a real
answer" lexicon is exactly the kind of phrase list that mis-fires across languages and
paraphrases, and half these fixtures are about paraphrase.
"""

from __future__ import annotations

import json
import os
from typing import Any

from ports import AnswerDecision, AnswerFixture

# The storage transform, MIRRORED from app/main.py:2543:
#     text = " ".join(str(value or "").split())[:280]
# It is a property of the FLOW, not of any validator, so every port applies it and reports the
# same `stored` — otherwise `reference` and `shipped` would disagree about truncation, which
# is not a thing they disagree about.
_MAX_ANSWER_CHARS = 280


def store_as(raw: Any) -> str | None:
    """What actually lands in `draft["answers"]`. None when the flow drops it.

    Whitespace-collapsed then hard-truncated with no ellipsis and no signal to the user —
    checks.check_truncation scores that separately, because a silently halved answer is a
    defect even when the answer was a good one.
    """
    text = " ".join(str(raw or "").split())[:_MAX_ANSWER_CHARS]
    return text or None


class ShippedValidator:
    """Every check a recommendation answer passes through today, and nothing else.

    1. the field must belong to the session's own generated step set   main.py:2546
       Always true for a fixture (the field is named by the step), so it is modelled and
       reported rather than exercised — it is an anti-injection guard, not a quality one.
    2. whitespace-collapse, drop-if-empty, truncate at 280               main.py:2543
    3. required fields must be non-blank before the ready card           REAL CALL to
       app.reco_question_sets.missing_required

    Nothing here looks at what the answer MEANS, because nothing in the product does.
    """

    name = "shipped"

    def __init__(self) -> None:
        self._missing_required = None
        try:
            from app.reco_question_sets import missing_required
            self._missing_required = missing_required
        except Exception:  # noqa: BLE001 — mirrored fallback below; reported in `reason`
            pass

    def validate(self, fx: AnswerFixture) -> AnswerDecision:
        stored = store_as(fx.answer)
        if stored is None:
            return AnswerDecision(
                accepted=False, stored=None,
                reason="dropped: collapses to empty, so the endpoint never writes the key "
                       "(main.py:2543-2546)",
            )
        # The real function, on a one-step set shaped like the product's own.
        step = {
            "field": fx.field_name,
            "label": fx.field_name.replace("_", " ").title()[:24],
            "question": fx.question,
            "required": bool(fx.required),
        }
        if self._missing_required is not None:
            blocked = self._missing_required([step], {fx.field_name: stored})
            real = " (missing_required called for real)"
        else:
            blocked = [] if stored else [fx.field_name]
            real = " (missing_required unavailable — mirrored)"
        if blocked:
            return AnswerDecision(accepted=False, stored=stored,
                                  reason=f"required field still missing{real}")
        return AnswerDecision(
            accepted=True, stored=stored,
            reason=f"field known, {len(stored)} chars <= {_MAX_ANSWER_CHARS}, non-blank{real} "
                   f"— nothing checks what it means",
        )


class DryValidator:
    """The shipped rules with no imports and no key, for --dry-run. Proves the plumbing, not
    the product: it reaches the same decisions as ShippedValidator by construction, so a
    --dry-run report is a shape check and must never be read as a result."""

    name = "dry"

    def validate(self, fx: AnswerFixture) -> AnswerDecision:
        stored = store_as(fx.answer)
        if stored is None:
            return AnswerDecision(accepted=False, stored=None, reason="dry: empty after collapse")
        return AnswerDecision(accepted=True, stored=stored,
                              reason="dry: non-blank, so today's flow accepts it")


_REFERENCE_SYSTEM = (
    "You are the missing validator on a neighbourhood recommendation card. A neighbour was "
    "asked ONE question about something they are recommending, and typed ONE answer. Decide "
    "whether that answer is usable — whether ANOTHER neighbour reading the finished card "
    "could act on it.\n"
    'Output only valid JSON: {"usable": true|false, "why": "<12 words>"}.\n'
    "UNDERSTAND, DO NOT PATTERN-MATCH. Any language, any phrasing.\n"
    # EVERY EXAMPLE BELOW IS DELIBERATELY NOT A FIXTURE. An earlier version of this prompt
    # quoted eight of the twenty-one hand-labelled answers verbatim, which made the score a
    # lookup rather than a measurement — the validator was told the answers to its own test.
    # The rules are the same; the illustrations are now drawn from cases that appear nowhere
    # in fixtures.yaml. `selftest.py` asserts the separation so it cannot creep back.
    "usable = false ONLY when the answer fails to answer the question: it is a non-answer "
    "(a shrug, a refusal, a placeholder), it is contentless filler that would read as a fact "
    "on the card, it answers a DIFFERENT question than the one asked, or it is the wrong kind "
    "of value for what was asked (a single figure where a range was asked for, a feeling where "
    "a time was asked for).\n"
    "usable = true for everything else. Be generous. Specifically:\n"
    "- Informal, slangy, misspelled or joking answers are USABLE if a real answer is in there. "
    "'nah not really' answers a yes/no question. 'like twenty min' is a time.\n"
    "- Very short answers are USABLE when the question has a short answer. A bare number "
    "answers a how-many question; one word off a chip row answers a closed-set question.\n"
    "- An answer does not have to be detailed, well written, or interesting to be usable.\n"
    "- For a contact field, anything a neighbour could actually reach them by is usable — a "
    "phone number, a business name, a clinic, a website. An answer that routes the reader "
    "back to the author instead of to the subject is NOT: it reaches nobody who reads the card.\n"
    "- For a location field, a findable place is usable; a landmark only an existing local "
    "could resolve is not.\n"
    "Judge the answer against THIS question and THIS kind of recommendation. The same words "
    "can be usable for one and not the other: a single price answers 'what does it cost?' "
    "about a product, and does not answer 'what's the price range?' about a restaurant.\n"
    "When you genuinely cannot tell, answer usable=true. Wrongly rejecting a neighbour's real "
    "answer is worse than storing a weak one."
)

# Non-answers so clearly contentless that no judgment is involved. Deliberately TINY and
# exact-match-after-normalisation only: this is a floor under the LLM for the degenerate
# cases, not a lexicon. Anything requiring interpretation goes to the model.
_DEAD_ANSWERS = frozenset({
    "idk", "i dont know", "i don't know", "dunno", "no idea", "not sure",
    "n/a", "na", "none", "nothing", "?", "??", "-", "--", "tbd", "ask me", "ask",
})


class ReferenceValidator:
    """§12.3's AI validator, built to the spec so the fixtures have something to score against.

    AI-first with a deterministic floor:
      * an answer that is one of the step's own chips is valid by construction — never spend
        a call on it, and never let a model reject a tap the product itself offered.
      * an empty answer is refused by the flow before any validator sees it.
      * an exact dead non-answer ('idk', 'ask me') needs no judgment.
      * everything else goes to the model.

    NO KEY, NO VERDICT. It returns unscorable=True rather than falling back to a lexicon —
    fail closed. A guessed verdict here would silently become the number the report quotes.
    """

    name = "reference"

    def __init__(self, options_by_field: dict[str, list[str]] | None = None) -> None:
        self._options = options_by_field or {}

    def validate(self, fx: AnswerFixture) -> AnswerDecision:
        stored = store_as(fx.answer)
        if stored is None:
            return AnswerDecision(accepted=False, stored=None,
                                  reason="empty after whitespace collapse")

        norm = stored.strip().lower().rstrip(".!")
        opts = [o.strip().lower() for o in self._options.get(fx.field_name, [])]
        if opts and norm in opts:
            return AnswerDecision(accepted=True, stored=stored,
                                  reason="one of the step's own options — valid by construction")
        if norm in _DEAD_ANSWERS:
            return AnswerDecision(accepted=False, stored=stored,
                                  reason=f"{stored!r} is a non-answer, not an answer")

        if not os.environ.get("OPENAI_API_KEY", "").strip():
            return AnswerDecision(
                accepted=False, stored=stored, unscorable=True,
                reason="no OPENAI_API_KEY — the reference validator is AI-first and will not "
                       "guess a verdict from a phrase list",
            )
        payload = json.dumps(
            {
                "kind_of_recommendation": fx.reco_type,
                "question_asked": fx.question,
                "field": fx.field_name,
                "answer_control": fx.kind,
                "is_a_must_have_field": bool(fx.required),
                "the_neighbours_answer": stored,
            },
            ensure_ascii=False,
        )
        try:
            from openai import OpenAI

            client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
            resp = client.chat.completions.create(
                # Independent of whatever model Lana runs, for the reason CLAUDE.md gives:
                # a judge must never grade its own family by construction.
                model=os.environ.get("RECO_VALIDATOR_MODEL", "gpt-4o"),
                messages=[{"role": "system", "content": _REFERENCE_SYSTEM},
                          {"role": "user", "content": payload}],
                response_format={"type": "json_object"},
                temperature=0.0,
                max_tokens=64,
            )
            data = json.loads(resp.choices[0].message.content or "{}")
        except Exception as exc:  # noqa: BLE001
            return AnswerDecision(accepted=False, stored=stored, unscorable=True,
                                  reason=f"reference validator call failed: {exc}")
        if not isinstance(data, dict) or not isinstance(data.get("usable"), bool):
            return AnswerDecision(accepted=False, stored=stored, unscorable=True,
                                  reason=f"reference validator returned {data!r}")
        return AnswerDecision(accepted=bool(data["usable"]), stored=stored,
                              reason=str(data.get("why") or "")[:160])


__all__ = ["ShippedValidator", "ReferenceValidator", "DryValidator", "store_as"]
