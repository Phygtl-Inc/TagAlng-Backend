"""
stub_impl.py — the two non-LLM QuestionSetPorts.

  DryQuestionSets     a clean canned set, run through the REAL validate_steps. --dry-run.
  StaticQuestionSets  the type's static fallback, deliberately. The negative control.

Neither needs an API key. Both need `app.reco_question_sets` importable, which needs no key,
no DB and no server — it is a pure module.
"""

from __future__ import annotations

from typing import Any

from ports import GeneratedSet, QuestionFixture

# The floor per type, for the dry set to supply itself. Kept here (not imported from checks)
# so a dry run is not scored against a set built from the same table the check reads — that
# would make check_floor_present tautological on the one backend everybody runs first.
_DRY_FLOOR: dict[str, tuple[tuple[str, str, str], ...]] = {
    # type: ((field, question, placeholder), ...)
    "professional": (
        ("profession", "What do they practise?", "paediatric dentistry"),
        ("helped_with", "What did they help you with?", "my toddler's first filling"),
        ("contact", "How do neighbours reach them?", "the front desk on 555-0134"),
    ),
    "service": (
        ("service", "What do they do?", "AC repair"),
        ("helped_with", "What did they do for you?", "replaced our blower in August"),
        ("contact", "How do neighbours reach them?", "his cell, 555-0134"),
    ),
    # No `where`: for restaurant/location the subject step carries the map pin and
    # validate_steps drops a second place step (reco_question_sets.py, drop_place=). Supplying
    # one here would be silently removed and the dry set would fall below the size floor.
    "restaurant": (
        ("dish", "What should they order?", "the al pastor tacos"),
        ("cuisine", "What kind of food is it?", "northern Mexican"),
    ),
    "recipe": (
        ("recipe", "What do you call it?", "sheet-pan chicken and veg"),
        ("ingredients", "What goes into it?", "chicken thighs, peppers, red onion"),
    ),
    "product": (
        ("used_for", "What is it used for?", "pet hair on rugs"),
        ("where_to_buy", "Where can neighbours get it?", "Costco, about $60"),
    ),
    "location": (
        ("known_for", "What is it known for?", "a shaded dog trail"),
        ("shade", "How much shade is there?", "most of the loop"),
    ),
    "diy": (
        ("fixes", "What problem does it solve?", "a messy caulk line"),
        ("how", "How do you do it?", "tape both edges before you run the bead"),
    ),
}

# Two extras every dry set gets, on top of the floor. Chosen so a dry run passes every
# mechanical axis: neither is on §6's banned list, neither asks anything Google knows, one is
# tappable so `tappable` passes, and both carry an example so `placeholders` passes.
_DRY_EXTRAS: tuple[dict[str, Any], ...] = (
    {
        "field": "best_for",
        "label": "Best for",
        "question": "Who would you send to it?",
        "options": ["Families", "Kids", "Groups", "Anyone"],
        "placeholder": "families with toddlers",
    },
    {
        "field": "good_to_know",
        "label": "Good to know",
        "question": "What would you want to know before you went the first time?",
        "placeholder": "the lot fills up by noon",
    },
)


# Canned translations of the fields a `lang` fixture touches. Without these a dry run
# SOFT_FAILs `language` on the Spanish fixture — correctly, since the dry set really is in
# English — and a plumbing smoke test that is never green is one nobody can read. Adding them
# also exercises the PASS branch of check_language, which otherwise never runs offline.
_DRY_TRANSLATIONS: dict[str, dict[str, tuple[str, str]]] = {
    "spanish": {
        # field: (question, placeholder)
        "profession": ("¿Qué especialidad tiene?", "pediatría"),
        "helped_with": ("¿Con qué les ayudó?", "la primera revisión de mi hija"),
        "contact": ("¿Cómo pueden contactarla los vecinos?", "el 555-0134 de recepción"),
        "best_for": ("¿Para quién la recomendarías?", "familias con niños pequeños"),
        "good_to_know": ("¿Qué te habría gustado saber la primera vez?",
                         "el estacionamiento se llena temprano"),
    },
}


def _translate(raw: list[dict[str, Any]], lang: str | None) -> list[dict[str, Any]]:
    table = _DRY_TRANSLATIONS.get(str(lang or "").strip().lower())
    if not table:
        return raw
    out = []
    for step in raw:
        s = dict(step)
        hit = table.get(str(s.get("field")))
        if hit:
            s["question"], s["placeholder"] = hit
        out.append(s)
    return out


def _validate(raw: list[dict[str, Any]], reco_type: str | None) -> list[dict[str, Any]]:
    from app.reco_question_sets import validate_steps

    return validate_steps(raw, reco_type, tallies=())


class DryQuestionSets:
    """A clean canned set for `--dry-run`: no OpenAI key, no server, no DB.

    It supplies the type's floor ITSELF rather than letting validate_steps repair it in, for a
    reason worth stating: a repaired floor field is copied from the static `_SETS` table,
    which carries no `placeholder`, so a repaired set can never pass `placeholders`. A dry run
    is meant to prove the pipeline is green when nothing is wrong — if the canned set tripped a
    real axis, nobody could tell a plumbing failure from a finding.

    It is NOT a quality reference. It proves the plumbing and nothing else.
    """

    name = "dry"

    def generate(self, fx: QuestionFixture) -> GeneratedSet:
        rtype = fx.expect_reco_type or "location"
        floor = _DRY_FLOOR.get(rtype, _DRY_FLOOR["location"])
        raw: list[dict[str, Any]] = [
            {
                "field": f,
                "label": f.replace("_", " ").title()[:24],
                "question": q,
                "placeholder": p,
            }
            for f, q, p in floor
        ]
        raw += [dict(e) for e in _DRY_EXTRAS]
        raw = _translate(raw, fx.lang)
        try:
            steps = _validate(raw, rtype)
        except Exception as exc:  # noqa: BLE001
            return GeneratedSet(error=f"dry backend could not reach validate_steps: {exc}")
        # Dry pre-fills every fact the fixture says the opening line already gave, which is
        # what a working extractor does. Without it a dry run would SOFT_FAIL `stated_facts`
        # on half the fixtures and read as a finding.
        prefilled = {
            str(s.get("field")): "(canned)"
            for s in steps
            for fact in fx.stated_facts
            if any(n.lower() in str(s.get("question") or "").lower()
                   for n in (fact.get("question_any_of") or []))
        }
        return GeneratedSet(
            steps=steps, reco_type=rtype, prefilled=prefilled, raw_steps=raw,
            generated=fx.expect_generated, prefill_measured=True,
            flags=["dry backend — canned questions. Proves the pipeline, never the product."],
        )


class StaticQuestionSets:
    """The type's STATIC fallback set, deliberately — reco_question_sets `_SETS`.

    This is the negative control, and it earns its place twice over:

      1. NON-VACUITY. The static sets literally contain "What stood out for you?" and "What
         did you like about them?" (§6's first two banned rows) and, for `location`, "When is
         it open — and the best time to go?" (§12.1's opening-hours ban). So a run against
         this backend MUST light up `banned_generic` and `google_answerable`. If it ever comes
         back clean, those checks have gone vacuous and every green run above them is worthless.
      2. It QUANTIFIES the fallback. §1 says the one-form-for-everything questionnaire was
         abandoned; fixtures.yaml q11 shows a live path that still reaches it. Running the
         whole fixture set through the static sets says exactly what a neighbour gets when
         that path is taken, in the same numbers as everything else in the report.
    """

    name = "static"

    def generate(self, fx: QuestionFixture) -> GeneratedSet:
        rtype = fx.expect_reco_type
        try:
            steps = _validate(None, rtype)  # raw=None -> validate_steps takes the fallback branch
        except Exception as exc:  # noqa: BLE001
            return GeneratedSet(error=f"static backend could not reach validate_steps: {exc}")
        return GeneratedSet(
            steps=steps, reco_type=rtype, prefilled={}, raw_steps=None, generated=False,
            flags=["static backend — the type's fallback set, on purpose. This is what a "
                   "neighbour gets whenever generation does not run."],
        )


__all__ = ["DryQuestionSets", "StaticQuestionSets"]
