"""
backend.py — the SIM_BACKEND switch (mirrors circles_zip/backend.py and policy_eval/backend.py).

run_eval.py never imports an implementation directly, only through these two factories.

  QUESTION SETS (arm A)                        needs
    inproc  the REAL _extract_tip_fields +     OPENAI_API_KEY. No DB, no server.
            validate_steps, in-process
    live    the whole shipped flow over HTTP   a running worker + a seeded sim account
    static  the type's fallback set            nothing (the negative control)
    dry     a clean canned set                 nothing (--dry-run)

  ANSWER VALIDATORS (arm B)                    needs
    shipped   what the product does today      nothing (imports missing_required)
    reference §12.3's AI validator, to spec    OPENAI_API_KEY
    dry       the shipped rules, no imports    nothing

The two arms have independent switches because they are independently useful: the cheap,
key-free `--validator shipped` run is the one that produces the headline junk-acceptance
number, and it does not need arm A to have run at all.
"""

from __future__ import annotations

import os

from ports import AnswerValidatorPort, QuestionSetPort

QUESTION_BACKENDS = ("inproc", "live", "static", "dry")
VALIDATORS = ("shipped", "reference", "dry")


def get_question_backend(kind: str | None = None) -> QuestionSetPort:
    kind = (kind or os.environ.get("SIM_BACKEND", "inproc")).strip().lower()
    if kind == "dry":
        from stub_impl import DryQuestionSets
        return DryQuestionSets()
    if kind == "static":
        from stub_impl import StaticQuestionSets
        return StaticQuestionSets()
    if kind == "inproc":
        from live_impl import InProcQuestionSets
        return InProcQuestionSets()
    if kind == "live":
        from live_impl import LiveQuestionSets
        return LiveQuestionSets()
    raise ValueError(f"Unknown backend={kind!r}, expected one of {QUESTION_BACKENDS}")


def get_validator(
    kind: str | None = None, *, options_by_field: dict[str, list[str]] | None = None
) -> AnswerValidatorPort:
    kind = (kind or os.environ.get("SIM_VALIDATOR", "shipped")).strip().lower()
    if kind == "dry":
        from validators import DryValidator
        return DryValidator()
    if kind == "shipped":
        from validators import ShippedValidator
        return ShippedValidator()
    if kind == "reference":
        from validators import ReferenceValidator
        return ReferenceValidator(options_by_field=options_by_field)
    raise ValueError(f"Unknown validator={kind!r}, expected one of {VALIDATORS}")
