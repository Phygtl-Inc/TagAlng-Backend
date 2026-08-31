#!/usr/bin/env python3
"""Both forks of the recommendation capture, with NO LLM key and NO database.

    ./scripts/try_reco_carousel.py            # chat fork, then carousel fork
    ./scripts/try_reco_carousel.py chat       # just the chat fork
    ./scripts/try_reco_carousel.py cards      # just the carousel fork

The extractor is stubbed with what a real model would return for the mock's own examples,
so what you are watching is the part that is ours: the guards in validate_steps, the walk,
the early finish, the tallies step, the batch endpoint, and what finally gets posted.

For the real thing — a set actually WRITTEN by the model — use try_reco_capture.py, which
needs a working LLM key.
"""

from __future__ import annotations

import pathlib
import sys
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services" / "lana-worker"))

import app.tip_share as ts  # noqa: E402
from app.auth import AuthSession  # noqa: E402
from app.main import TipSetupRequest, set_tip_setup  # noqa: E402
from app.reco_question_sets import validate_steps  # noqa: E402

# What the extractor returns on the opening turn — including `steps_raw`, the set the model
# proposed. Deliberately imperfect: the dentist set forgets `contact` (the floor puts it
# back), sneaks in a private ask and a statement (both dropped), and the recipe set is the
# mock's own 10-step spread.
DENTIST = {
    "name": "Dr. Sarah", "category": "pediatric dentist", "locality": "Lake Nona",
    "trait": "gentle with toddlers", "reco_type": "professional",
    "answers": {"profession": "Pediatric dentist"},
    "steps_raw": [
        {"field": "profession", "label": "Profession", "question": "What does she do?",
         "placeholder": "Pediatric dentist"},
        {"field": "helped_with", "label": "Helped with", "question": "What did she help with?",
         "placeholder": "Kids' dental care"},
        {"field": "home_address", "question": "What is her home address?"},   # dropped: private
        {"field": "blurb", "question": "Tell me more about her."},            # dropped: not a question
        {"field": "liked", "label": "What you liked", "question": "What did you like about her?",
         "placeholder": "Gentle with the little ones"},
        {"field": "recommend_to", "label": "Recommend to",
         "question": "Who would you recommend her to?", "placeholder": "New parents, nervous kids"},
        {"field": "good_to_know", "label": "Good to know",
         "question": "Anything to know before choosing her?", "placeholder": "Takes insurance"},
    ],
}

RECIPE = {
    "name": "Feijoada", "category": "family recipe", "reco_type": "recipe",
    "answers": {"recipe": "Feijoada — Brazilian black bean stew"},
    "steps_raw": [
        {"field": "recipe", "label": "Recipe", "question": "What is the recipe?"},
        {"field": "ingredients", "label": "Ingredients", "question": "What do you need to make it?",
         "placeholder": "Black beans, pork, sausage, garlic, rice"},
        {"field": "taste", "label": "Taste", "question": "How does it taste?",
         "placeholder": "Rich, smoky, deeply savoury"},
        {"field": "difficulty", "label": "Difficulty", "question": "How difficult is it to make?",
         "options": ["Easy — mostly hands-off", "Some patience", "A project"]},
        {"field": "time", "label": "Time", "question": "How long does it take?",
         "placeholder": "About 3 hours"},
        {"field": "tips", "label": "Tips", "question": "Any tips before making it?",
         "placeholder": "Soak the beans overnight"},
    ],
}

# Neighbours who already logged something about Dr. Sarah — what the closing agree row is
# built from. Empty for the recipe, so that set is one step shorter.
TALLIES = {"dr. sarah": [{"attr": "easy parking", "n": 2}, {"attr": "books online", "n": 1}]}

POSTED: dict = {}


def _install_stubs(opening: dict) -> None:
    """One-shot extractor: the opening message returns everything, later turns nothing."""
    turns = iter([(dict(opening), None)])
    ts._extract_tip_fields = lambda **kw: next(turns, ({}, None))
    ts._reco_tallies = lambda *, user_jwt, block_id, name: TALLIES.get(
        str(name or "").strip().lower(), []
    )
    ts.compose_reply = lambda **kw: kw["fallback"]
    ts._save_tip = lambda **kw: (
        POSTED.update(
            {"reco_fields": ts._reco_fields(kw["draft"]), "detail_text": ts._detail_text(kw["draft"])}
        ),
        {"signal_id": "sig-local", "matches_created": 2},
    )[1]


def _turn(ctx: dict, msg: str) -> None:
    print(f"\n\033[36m> {msg}\033[0m")
    reply = ts.run_tip_share_turn(
        user_message=msg, session_ctx=ctx, history=[], user_jwt="local", home_block_id="b1"
    )
    print(f"Lana: {reply}")


def _show_posted() -> None:
    print("\n  WOULD POST:")
    for row in POSTED.get("reco_fields") or []:
        print(f"    {row['label']:<18} {row['answer']}   \033[90m({row['question']})\033[0m")
    print(f"\n  detail_text a neighbour reads:\n    {POSTED.get('detail_text')}")
    POSTED.clear()


def chat_fork() -> None:
    """The conversation fork: one question per turn, then an early finish."""
    print("\n\033[1m── CHAT FORK · Dr. Sarah ─────────────────────────────────────────\033[0m")
    _install_stubs(DENTIST)
    ctx: dict = {"zip_code": "32827"}
    _turn(ctx, "Dr. Sarah in Lake Nona — pediatric dentist, so gentle my toddler loves going")
    for answer in [
        "Kids' dental care",
        "(407) 555-0148",
        "Gentle with the little ones",
        "New parents and nervous kids",
        "Takes insurance · open Saturdays",
        "Let them ask",
        "easy parking ×2",
    ]:
        _turn(ctx, answer)
    _turn(ctx, "pass the tip along")
    _show_posted()


def cards_fork() -> None:
    """The carousel fork: the set is generated, then answered in ONE submission."""
    print("\n\033[1m── CARDS FORK · Feijoada ─────────────────────────────────────────\033[0m")
    _install_stubs(RECIPE)
    ctx: dict = {"zip_code": "32827"}
    _turn(ctx, "Feijoada — my Sunday-lunch staple, the whole block asks for the recipe")

    steps = (ctx.get("tip_draft") or {}).get("step_set") or []
    print("\n  the carousel the FE renders:")
    for i, step in enumerate(steps, 1):
        mark = "!" if step.get("required") else "·"
        extra = step.get("placeholder") or (" / ".join(step.get("options") or []))
        print(f"    {mark} {i}/{len(steps)} {step['label']:<18} {step['question']}"
              f"   \033[90m{step['kind']}{'  e.g. ' + extra if extra else ''}\033[0m")

    # What RecoSetupCarousel POSTs: every answer at once. `evil` is what a tampered client
    # would add — the endpoint drops it, because only this set's own fields are accepted.
    answers = {
        "ingredients": "Black beans, pork, sausage, garlic, rice",
        "taste": "Rich, smoky, deeply savoury",
        "difficulty": "Easy — mostly hands-off",
        "time": "About 3 hours",
        "tips": "Soak the beans overnight",
        "ask_ok": "Let them ask",
        "evil": "should not land",
    }
    auth = AuthSession(user_id="u1", is_anonymous=False, phone_verified=True, home_block_id="b1")
    with (
        patch("app.main.verify_auth", return_value=auth),
        patch("app.main.get_session_for_user", return_value={"context": ctx}),
        patch("app.main.update_session_context", side_effect=lambda sid, c: ctx.update(c)),
    ):
        res = set_tip_setup("s1", TipSetupRequest(answers=answers), authorization="Bearer local")
    print(f"\n  POST /lana/sessions/s1/tip-setup → {res}")
    print(f"  fields kept: {sorted((ctx['tip_draft'].get('answers') or {}))}")

    _turn(ctx, "Looks good")
    _turn(ctx, "pass the tip along")
    _show_posted()


if __name__ == "__main__":
    which = (sys.argv[1] if len(sys.argv) > 1 else "both").lower()
    if which in ("both", "chat"):
        chat_fork()
    if which in ("both", "cards"):
        cards_fork()
