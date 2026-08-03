"""Circles · grounding questions on the rapport tile.

Covers the three moving parts:
  * ensure_grounding_gaps — capped, idempotent synthesis from ungrounded affiliations
  * handle_grounding_answer / _confirmation — tap grounds, free text confirms via
    chips, unmatchable answers persist as detail (never auto-ground from text)
  * ranker cadence — a grounding ask is suppressed when one was served within the
    last N-1 tile questions, unless it is the only thing on the plate
"""

import unittest
from unittest.mock import MagicMock, patch

from app.circles_flow import (
    _ESCAPE_SEND,
    ensure_grounding_gaps,
    handle_grounding_answer,
    handle_grounding_confirmation,
    match_grounding_candidate,
)


class _Q:
    """Chainable query stub: any attribute/call returns self; execute() too, so the
    trailing .execute().data reads the seeded payload (supports .not_.is_ chains)."""

    def __init__(self, data=None, count=None):
        self.data = data or []
        self.count = count

    def __getattr__(self, _name):
        return self

    def __call__(self, *args, **kwargs):
        return self


def _sb(tables: dict) -> MagicMock:
    sb = MagicMock()
    sb.table.side_effect = lambda name: tables[name]
    return sb


_ECHO_FALLBACK = lambda goal, facts, fallback, session_ctx: fallback  # noqa: E731


class TestEnsureGroundingGaps(unittest.TestCase):
    @patch("app.circles_flow._grounding_question", return_value=("Which gym?", "about your gym…"))
    @patch("app.rapport_gaps.open_semantic_gap", return_value=True)
    @patch("app.circles_flow.service_client")
    def test_opens_for_ungrounded_skips_already_asked(self, sb, open_gap, _q) -> None:
        sb.return_value = _sb(
            {
                # a1 already has a gap (answered) → never re-asked; nothing open now.
                "rapport_gaps": _Q([{"gap_row_id": "g1", "status": "answered", "affiliation_ref": "a1"}]),
                "circle_affiliations": _Q(
                    [
                        {"id": "a1", "circle_type": "fitness", "detail": "my gym"},
                        {"id": "a2", "circle_type": "hobby", "detail": "book club"},
                        {"id": "a3", "circle_type": "faith", "detail": "our church"},
                    ]
                ),
            }
        )
        opened = ensure_grounding_gaps("u1")
        self.assertEqual(opened, 2)  # a2 + a3, capped at the default max_open=2
        asked_affs = [c.kwargs["affiliation_ref"] for c in open_gap.call_args_list]
        self.assertEqual(asked_affs, ["a2", "a3"])
        self.assertEqual(open_gap.call_args_list[0].kwargs["gap_id"], "ground:a2")
        # Above the 0.8 semantic default — grounding outranks the extractor's
        # same-topic follow-up (which is suppressed on capture turns anyway).
        self.assertEqual(open_gap.call_args_list[0].kwargs["unlock_score"], 0.85)

    @patch("app.circles_flow.service_client")
    def test_cap_already_reached_is_a_noop(self, sb) -> None:
        # Two open/asked grounding gaps → need<=0 → circle_affiliations never queried
        # (the _sb dict would KeyError if it were).
        sb.return_value = _sb(
            {
                "rapport_gaps": _Q(
                    [
                        {"gap_row_id": "g1", "status": "open", "affiliation_ref": "a1"},
                        {"gap_row_id": "g2", "status": "asked", "affiliation_ref": "a2"},
                    ]
                )
            }
        )
        self.assertEqual(ensure_grounding_gaps("u1"), 0)


class TestGroundingQuestionLexicon(unittest.TestCase):
    def _authored(self, question):
        llm = MagicMock()
        llm.llm_configured.return_value = True
        llm.router_model.return_value = "m"
        llm.llm_json.return_value = {"question": question, "teaser": "about your gym…"}
        with patch.dict("sys.modules", {"app.orchestrator.llm": llm}):
            from app.circles_flow import _grounding_question

            return _grounding_question("fitness", "my gym")

    def test_lexicon_leak_falls_back_to_template(self) -> None:
        # Seen in dev: the model leaked "block" despite the prompt ban.
        q, _t = self._authored("Which spot hosts the cycling on your block?")
        self.assertEqual(q, "You mentioned your gym — which one is it, exactly?")

    def test_clean_question_passes(self) -> None:
        q, _t = self._authored("Which gym do you go to?")
        self.assertEqual(q, "Which gym do you go to?")


class TestGroundOptionsNameGate(unittest.TestCase):
    """The user's own name for the place decides what may be offered (2026-08-03:
    "Fitness CF" was answered with Crunch / EoS / Lake Nona Performance Club)."""

    @patch("app.places.search_places")
    def test_no_name_searches_keyword_only_and_flags_suggestions(self, sp) -> None:
        # They named only the activity ("we go to church on sundays"), so there is
        # nothing to name-match: one keyword search, offered as suggestions.
        from app.circles_flow import ground_options

        sp.return_value = [{"name": "First Baptist", "address": "1 Main St", "place_id": "gp1"}]
        got = ground_options(
            "u1",
            {"circle_type": "faith", "detail": "We go to church on sundays", "place_name": ""},
            block_id=None,
        )
        self.assertEqual(sp.call_count, 1)
        self.assertEqual(sp.call_args.kwargs["query"], "church mosque synagogue temple")
        self.assertEqual(got[0]["google_place_id"], "gp1")
        self.assertTrue(got[0]["suggested"])

    @patch("app.places.search_places")
    def test_named_place_drops_results_that_lack_the_name(self, sp) -> None:
        from app.circles_flow import ground_options

        others = [
            {"name": "Crunch Fitness - Lake Nona", "address": "a", "place_id": "gp1"},
            {"name": "EoS Fitness", "address": "b", "place_id": "gp2"},
        ]
        # Block-biased search, then the widened retry — neither carries the name.
        sp.side_effect = [list(others), list(others), []]
        got = ground_options(
            "u1",
            {"circle_type": "fitness", "detail": "gym at Fitness CF", "place_name": "Fitness CF"},
            block_id="b1",
        )
        self.assertEqual(sp.call_args_list[0].kwargs["query"], "Fitness CF")
        self.assertGreater(sp.call_args_list[1].kwargs["radius"], 16000.0)
        # Falls through to keyword suggestions — flagged, never called a match.
        self.assertTrue(all(o["suggested"] for o in got))

    @patch("app.places.search_places")
    def test_named_place_kept_when_the_name_is_there(self, sp) -> None:
        from app.circles_flow import ground_options

        sp.return_value = [
            {"name": "Fitness CF Lake Nona", "address": "a", "place_id": "gp9"},
            {"name": "Crunch Fitness", "address": "b", "place_id": "gp1"},
        ]
        got = ground_options(
            "u1",
            {"circle_type": "fitness", "detail": "Fitness CF", "place_name": "Fitness CF"},
            block_id="b1",
        )
        self.assertEqual([o["google_place_id"] for o in got], ["gp9"])
        self.assertFalse(got[0]["suggested"])
        self.assertEqual(sp.call_count, 1)

    @patch("app.places.search_places")
    def test_widened_retry_finds_the_named_place_a_town_over(self, sp) -> None:
        from app.circles_flow import ground_options

        sp.side_effect = [
            [{"name": "Crunch Fitness", "address": "a", "place_id": "gp1"}],
            [{"name": "Fitness CF Kissimmee", "address": "c", "place_id": "gp7"}],
        ]
        got = ground_options(
            "u1",
            {"circle_type": "fitness", "detail": "Fitness CF", "place_name": "Fitness CF"},
            block_id="b1",
        )
        self.assertEqual([o["google_place_id"] for o in got], ["gp7"])

    @patch("app.places.search_places")
    def test_typed_search_never_falls_back_to_nearby_spots(self, sp) -> None:
        from app.circles_flow import ground_options

        sp.return_value = [{"name": "Crunch Fitness", "address": "a", "place_id": "gp1"}]
        got = ground_options(
            "u1",
            {"circle_type": "fitness", "detail": "my gym", "place_name": ""},
            block_id="b1",
            query="Fitness CF",
        )
        self.assertEqual(got, [])


class TestResolvePlaceName(unittest.TestCase):
    """Rows captured before place_name existed get it resolved from their own
    phrase, once, by the model that already reads these sentences."""

    @patch("app.circles_flow.service_client")
    @patch("app.orchestrator.llm.llm_json", return_value={"place_name": "Fitness CF"})
    @patch("app.orchestrator.llm.router_model", return_value="m")
    @patch("app.orchestrator.llm.llm_configured", return_value=True)
    def test_resolves_and_persists(self, _cfg, _rm, llm, sb) -> None:
        from app.circles_flow import _resolve_place_name

        aff = {"id": "a1", "detail": "I go to the gym at Fitness CF; has_sauna=true"}
        self.assertEqual(_resolve_place_name("u1", aff), "Fitness CF")
        # The parked feature note never reaches the model.
        self.assertNotIn("has_sauna", llm.call_args.kwargs["user_payload"])
        self.assertEqual(
            sb.return_value.table.return_value.update.call_args[0][0], {"place_name": "Fitness CF"}
        )

    @patch("app.circles_flow.service_client")
    @patch("app.orchestrator.llm.llm_json", return_value={"place_name": None})
    @patch("app.orchestrator.llm.router_model", return_value="m")
    @patch("app.orchestrator.llm.llm_configured", return_value=True)
    def test_activity_only_persists_the_empty_answer(self, _cfg, _rm, _llm, sb) -> None:
        from app.circles_flow import _resolve_place_name

        aff = {"id": "a1", "detail": "I go to the gym every weekend"}
        self.assertEqual(_resolve_place_name("u1", aff), "")
        # '' is a real answer ("they named no venue") — stored so we never re-ask.
        self.assertEqual(
            sb.return_value.table.return_value.update.call_args[0][0], {"place_name": ""}
        )

    def test_stored_value_short_circuits(self) -> None:
        from app.circles_flow import _resolve_place_name

        self.assertEqual(
            _resolve_place_name("u1", {"id": "a1", "place_name": "St. Luke's"}), "St. Luke's"
        )


class TestNameHit(unittest.TestCase):
    def test_the_screenshot_case(self) -> None:
        from app.circles_flow import _name_hit

        self.assertFalse(_name_hit("Fitness CF", "Crunch Fitness - Lake Nona"))
        self.assertFalse(_name_hit("Fitness CF", "EōS Fitness"))
        self.assertFalse(_name_hit("Fitness CF", "Lake Nona Performance Club"))
        self.assertTrue(_name_hit("Fitness CF", "Fitness CF Lake Nona"))

    def test_accents_and_punctuation_fold(self) -> None:
        from app.circles_flow import _name_hit

        self.assertTrue(_name_hit("eos fitness", "EōS Fitness!"))
        self.assertTrue(_name_hit("orangetheory", "OrangeTheory Narcoossee"))
        self.assertTrue(_name_hit("St. Luke's", "St Lukes Church"))

    def test_too_short_never_hits(self) -> None:
        from app.circles_flow import _name_hit

        self.assertFalse(_name_hit("Y", "YMCA Lake Nona"))


class TestOfferedListsAlwaysHaveAWayOut(unittest.TestCase):
    """A wrong list must never be a dead end — the tile's only affordance is these
    chips, so one of them has to say "not these" (2026-08-03)."""

    def test_escape_chip_appended_but_never_grounds(self) -> None:
        from app.circles_flow import _ESCAPE_SEND, _with_escape, match_grounding_candidate

        chips = _with_escape([{"label": "A gym", "send": "It's A gym", "google_place_id": "gp1"}])
        self.assertEqual(chips[-1]["send"], _ESCAPE_SEND)
        self.assertIsNone(chips[-1]["google_place_id"])
        # It can never be mistaken for a place the user picked.
        self.assertIsNone(match_grounding_candidate(chips, _ESCAPE_SEND))

    def test_no_escape_on_an_empty_list(self) -> None:
        from app.circles_flow import _with_escape

        self.assertEqual(_with_escape([]), [])

    @patch("app.circles_flow.service_client")
    @patch("app.circles_flow._home_block_id", return_value="b1")
    @patch("app.circles_flow._own_affiliation",
           return_value={"id": "a1", "circle_type": "fitness", "place_name": "Fitness CF",
                         "detail": "gym at Fitness CF; has_sauna=true"})
    @patch("app.circles_flow.ground_options", return_value=[])
    def test_payload_names_the_kind_and_their_own_words(self, _go, _own, _blk, _sb) -> None:
        # The card needs the category for its glyph and their phrase for the noun
        # ("your gym") — FE ask #1, issues #63.
        from app.circles_flow import grounding_payload_for_gap

        payload = grounding_payload_for_gap(
            "u1", {"gap_row_id": "g1", "affiliation_ref": "a1", "grounding_options": None}
        )
        self.assertEqual(payload["circle_type"], "fitness")
        self.assertEqual(payload["detail"], "gym at Fitness CF")  # feature note stripped
        self.assertEqual(payload["place_name"], "Fitness CF")

    @patch("app.circles_flow.service_client")
    @patch("app.circles_flow._own_affiliation", return_value={"id": "a1"})
    def test_payload_omits_what_the_affiliation_lacks(self, _own, _sb) -> None:
        # Absent values must simply be absent — the card falls back to its neutral pin.
        from app.circles_flow import grounding_payload_for_gap

        payload = grounding_payload_for_gap(
            "u1", {"gap_row_id": "g1", "affiliation_ref": "a1", "grounding_options": []}
        )
        self.assertNotIn("circle_type", payload)
        self.assertNotIn("detail", payload)
        self.assertNotIn("place_name", payload)

    @patch("app.circles_flow.service_client")
    @patch("app.circles_flow._home_block_id", return_value="b1")
    @patch("app.circles_flow._own_affiliation",
           return_value={"id": "a1", "circle_type": "fitness", "place_name": "Fitness CF"})
    @patch("app.circles_flow.ground_options",
           return_value=[{"name": "Fitness CF Lake Nona", "address": "a",
                          "google_place_id": "gp9", "suggested": False}])
    def test_tile_offers_matches_without_an_escape_chip(self, _go, _own, _blk, _sb) -> None:
        # PlaceGroundingCard ships its own "Search another" + skip, and renders
        # every option as a place tile — an id-less escape chip would look like one.
        from app.circles_flow import _ESCAPE_SEND, grounding_payload_for_gap

        payload = grounding_payload_for_gap(
            "u1", {"gap_row_id": "g1", "affiliation_ref": "a1", "grounding_options": None}
        )
        sends = [o["send"] for o in payload["options"]]
        self.assertEqual(sends, ["It's Fitness CF Lake Nona"])
        self.assertNotIn(_ESCAPE_SEND, sends)

    @patch("app.circles_flow.service_client")
    @patch("app.circles_flow._home_block_id", return_value="b1")
    @patch("app.circles_flow._own_affiliation",
           return_value={"id": "a1", "circle_type": "fitness", "place_name": "Fitness CF"})
    @patch("app.circles_flow.ground_options",
           return_value=[{"name": "Crunch Fitness", "address": "a", "google_place_id": "gp1",
                          "suggested": True, "unmatched_name": "Fitness CF"},
                         {"name": "EoS Fitness", "address": "b", "google_place_id": "gp2",
                          "suggested": True, "unmatched_name": "Fitness CF"}])
    def test_tile_drops_consolations_for_a_name_it_couldnt_find(
        self, _go, _own, _blk, _sb
    ) -> None:
        # The card's question names the place; tiles that don't bear that name are
        # the whole bug. Zero options opens its own search box instead.
        from app.circles_flow import grounding_payload_for_gap

        payload = grounding_payload_for_gap(
            "u1", {"gap_row_id": "g1", "affiliation_ref": "a1", "grounding_options": None}
        )
        self.assertEqual(payload["options"], [])

    @patch("app.circles_flow.service_client")
    @patch("app.circles_flow._home_block_id", return_value="b1")
    @patch("app.circles_flow._own_affiliation",
           return_value={"id": "a1", "circle_type": "fitness", "place_name": ""})
    @patch("app.circles_flow.ground_options",
           return_value=[{"name": "Crunch Fitness", "address": "a",
                          "google_place_id": "gp1", "suggested": True},
                         {"name": "EoS Fitness", "address": "b",
                          "google_place_id": "gp2", "suggested": True}])
    def test_tile_still_offers_a_choice_for_an_unnamed_circle(
        self, _go, _own, _blk, _sb
    ) -> None:
        # "Which gym do you go to?" answered with two nearby gyms is a choice, not
        # a claim — this is what the pick-one grid is for.
        from app.circles_flow import grounding_payload_for_gap

        payload = grounding_payload_for_gap(
            "u1", {"gap_row_id": "g1", "affiliation_ref": "a1", "grounding_options": None}
        )
        self.assertEqual(len(payload["options"]), 2)

    @patch("app.circles_flow.service_client")
    @patch("app.circles_flow._home_block_id", return_value="b1")
    @patch("app.circles_flow._own_affiliation",
           return_value={"id": "a1", "circle_type": "fitness", "place_name": ""})
    @patch("app.circles_flow.ground_options",
           return_value=[{"name": "Crunch Fitness", "address": "a",
                          "google_place_id": "gp1", "suggested": True}])
    def test_tile_drops_a_lone_suggestion(self, _go, _own, _blk, _sb) -> None:
        # A single option renders as "the place she mentioned — pin it?", which a
        # guess must never claim.
        from app.circles_flow import grounding_payload_for_gap

        payload = grounding_payload_for_gap(
            "u1", {"gap_row_id": "g1", "affiliation_ref": "a1", "grounding_options": None}
        )
        self.assertEqual(payload["options"], [])


class TestMatchGroundingCandidate(unittest.TestCase):
    CANDS = [
        {"label": "OrangeTheory Narcoossee", "name": "OrangeTheory Narcoossee",
         "send": "It's OrangeTheory Narcoossee", "google_place_id": "gp1"},
        {"label": "LA Fitness Lake Nona", "name": "LA Fitness Lake Nona",
         "send": "It's LA Fitness Lake Nona", "google_place_id": "gp2"},
    ]

    def test_chip_tap_exact_send(self) -> None:
        got = match_grounding_candidate(self.CANDS, "It's LA Fitness Lake Nona")
        self.assertEqual(got["google_place_id"], "gp2")

    def test_name_containment(self) -> None:
        got = match_grounding_candidate(self.CANDS, "orangetheory narcoossee")
        self.assertEqual(got["google_place_id"], "gp1")

    def test_no_match_and_guards(self) -> None:
        self.assertIsNone(match_grounding_candidate(self.CANDS, "the one by Publix"))
        self.assertIsNone(match_grounding_candidate(self.CANDS, ""))
        self.assertIsNone(match_grounding_candidate(None, "anything"))
        # A candidate without a place id can never ground.
        self.assertIsNone(
            match_grounding_candidate([{"name": "X gym", "send": "It's X gym"}], "It's X gym")
        )


class TestHandleGroundingAnswer(unittest.TestCase):
    GAP = {"gap_row_id": "g1", "affiliation_ref": "a1",
           "grounding_options": [{"label": "OrangeTheory Narcoossee",
                                  "send": "It's OrangeTheory Narcoossee",
                                  "google_place_id": "gp1"}]}

    @patch("app.circles_flow.ground_and_confirm",
           return_value={"reply": "Locked in", "options": [], "pending": None, "grounded": True})
    def test_tile_chip_tap_grounds(self, gac) -> None:
        result = handle_grounding_answer("u1", self.GAP, "It's OrangeTheory Narcoossee")
        self.assertTrue(result["grounded"])
        gac.assert_called_once_with("u1", "a1", "gp1", session_ctx=None)

    @patch("app.circles_flow._compose_grounding_reply", side_effect=_ECHO_FALLBACK)
    @patch("app.circles_flow._home_block_id", return_value=None)
    @patch("app.circles_flow.ground_options",
           return_value=[{"name": "OrangeTheory Narcoossee", "address": "123", "google_place_id": "gp1"}])
    @patch("app.circles_flow._own_affiliation",
           return_value={"id": "a1", "circle_type": "fitness", "detail": "my gym", "place_ref": None})
    def test_free_text_offers_confirm_chips_never_autogrounds(self, _own, _go, _blk, _cr) -> None:
        gap = {**self.GAP, "grounding_options": None}
        result = handle_grounding_answer("u1", gap, "orange theory")
        self.assertFalse(result["grounded"])
        self.assertEqual(result["pending"]["affiliation_id"], "a1")
        self.assertEqual(result["pending"]["attempts"], 1)
        self.assertEqual(result["pending"]["candidates"][0]["google_place_id"], "gp1")
        self.assertEqual(result["options"][0]["send"], "It's OrangeTheory Narcoossee")

    @patch("app.circles_flow._compose_grounding_reply")
    @patch("app.circles_flow._home_block_id", return_value=None)
    @patch("app.circles_flow.ground_options",
           return_value=[{"name": "Crunch Fitness", "address": "1 Elm",
                          "google_place_id": "gp1", "suggested": True,
                          "unmatched_name": "Fitness CF"}])
    @patch("app.circles_flow._own_affiliation",
           return_value={"id": "a1", "circle_type": "fitness", "detail": "Fitness CF",
                         "place_ref": None})
    def test_consolations_are_never_presented_as_matches(self, _own, _go, _blk, cr) -> None:
        # The screenshot bug: three unrelated gyms offered as if they were the
        # user's own. Nearby same-kind places may still be OFFERED, but the reply
        # has to lead with not having found theirs, by name.
        cr.side_effect = _ECHO_FALLBACK
        gap = {**self.GAP, "grounding_options": None}
        result = handle_grounding_answer("u1", gap, "Fitness CF")
        goal = cr.call_args.kwargs["goal"]
        self.assertIn("could NOT find", goal)
        self.assertIn("never call them matches", goal)
        self.assertIn("Fitness CF", result["reply"])
        # Still offered, still escapable.
        self.assertEqual(result["options"][0]["send"], "It's Crunch Fitness")
        self.assertEqual(result["options"][-1]["send"], _ESCAPE_SEND)

    @patch("app.circles_flow._compose_grounding_reply")
    @patch("app.circles_flow._home_block_id", return_value=None)
    @patch("app.circles_flow.ground_options",
           return_value=[{"name": "Crunch Fitness", "address": "1 Elm",
                          "google_place_id": "gp1", "suggested": True}])
    @patch("app.circles_flow._own_affiliation",
           return_value={"id": "a1", "circle_type": "fitness", "detail": "my gym",
                         "place_ref": None})
    def test_plain_suggestions_dont_claim_a_failed_search(self, _own, _go, _blk, cr) -> None:
        # They never named a venue, so nothing failed — asking which one it is is
        # honest, and claiming "I couldn't find it" would be invented.
        cr.side_effect = _ECHO_FALLBACK
        gap = {**self.GAP, "grounding_options": None}
        result = handle_grounding_answer("u1", gap, "the gym near the school")
        goal = cr.call_args.kwargs["goal"]
        self.assertIn("haven't named their spot", goal)
        self.assertNotIn("couldn't find", result["reply"])

    @patch("app.circles_flow._compose_grounding_reply", side_effect=_ECHO_FALLBACK)
    @patch("app.circles_flow._own_affiliation",
           return_value={"id": "a1", "circle_type": "fitness", "circle_key": "gym",
                         "place_ref": None})
    def test_escape_tap_asks_what_its_called(self, _own, _cr) -> None:
        result = handle_grounding_answer("u1", self.GAP, _ESCAPE_SEND)
        # Thread stays open with a clean slate — their next words drive the search.
        self.assertEqual(result["pending"]["candidates"], [])
        self.assertEqual(result["options"], [])
        self.assertFalse(result["grounded"])

    @patch("app.circles_flow._compose_grounding_reply", side_effect=_ECHO_FALLBACK)
    @patch("app.circles_flow.note_ungrounded_detail")
    @patch("app.circles_flow._home_block_id", return_value=None)
    @patch("app.circles_flow.ground_options", return_value=[])
    @patch("app.circles_flow._own_affiliation",
           return_value={"id": "a1", "circle_type": "fitness", "circle_key": "gym",
                         "detail": "my gym", "place_ref": None})
    def test_unmatchable_answer_kept_as_detail(self, _own, _go, _blk, note, _cr) -> None:
        gap = {**self.GAP, "grounding_options": None}
        result = handle_grounding_answer("u1", gap, "the little studio by Publix")
        self.assertIsNone(result["pending"])
        self.assertFalse(result["grounded"])
        note.assert_called_once_with("u1", "a1", "the little studio by Publix")

    @patch("app.circles_flow._compose_grounding_reply", side_effect=_ECHO_FALLBACK)
    @patch("app.circles_flow.note_ungrounded_detail")
    @patch("app.circles_flow.ground_options")
    @patch("app.circles_flow._own_affiliation",
           return_value={"id": "a1", "circle_type": "fitness", "circle_key": "gym",
                         "place_ref": None})
    def test_abandon_never_searches_or_keeps_detail(self, _own, go, note, _cr) -> None:
        # "none of these" fed to Places search returns arbitrary nearby spots —
        # the abandon verdict must close the thread without a search and without
        # storing the rejection as detail.
        result = handle_grounding_answer("u1", self.GAP, "none of these", abandon=True)
        self.assertIsNone(result["pending"])
        self.assertFalse(result["grounded"])
        self.assertEqual(result["options"], [])
        go.assert_not_called()
        note.assert_not_called()

    @patch("app.circles_flow.ground_and_confirm",
           return_value={"reply": "Locked in", "options": [], "pending": None, "grounded": True})
    def test_chip_tap_wins_over_abandon(self, gac) -> None:
        # A tap is deterministic — even a stray abandon verdict never blocks it.
        result = handle_grounding_answer(
            "u1", self.GAP, "It's OrangeTheory Narcoossee", abandon=True
        )
        self.assertTrue(result["grounded"])
        gac.assert_called_once()


class TestGroundAndConfirmAnnounces(unittest.TestCase):
    """Grounding is the moment the community is CREATED (place mandatory,
    2026-07-28) — every register variant must tell the user it's now saved on
    their profile, never a silent create."""

    def _run(self, others: int, session_ctx):
        from app.circles_flow import ground_and_confirm

        with patch(
            "app.circles_flow._own_affiliation",
            return_value={"id": "a1", "circle_key": "book_club"},
        ), patch(
            "app.circles_flow.ground_affiliation",
            return_value={
                "affiliation_id": "a1",
                "place_id": "p1",
                "place_name": "OrangeTheory",
                "status": "confirmed",
            },
        ), patch(
            "app.circles_flow._place_co_member_count", return_value=others
        ), patch(
            "app.circles_flow._compose_grounding_reply", side_effect=_ECHO_FALLBACK
        ) as compose:
            result = ground_and_confirm("u1", "a1", "gp1", session_ctx=session_ctx)
            return result, compose.call_args.kwargs

    def _assert_announced(self, result, kwargs) -> None:
        self.assertIn("saved to your communities", result["reply"])
        self.assertTrue(
            any("saved as one of their communities" in f for f in kwargs["facts"])
        )

    def test_intro_variant_announces_saved_community(self) -> None:
        result, kwargs = self._run(2, {})
        self._assert_announced(result, kwargs)
        self.assertEqual(result["offer"]["kind"], "find_neighbors")

    def test_host_variant_announces_saved_community(self) -> None:
        result, kwargs = self._run(0, {})
        self._assert_announced(result, kwargs)
        self.assertEqual(result["offer"]["kind"], "host_meet")

    def test_plain_close_still_announces(self) -> None:
        # Tile endpoint path (no chat ctx): no offer, but the save is still said.
        result, kwargs = self._run(0, None)
        self._assert_announced(result, kwargs)
        self.assertIsNone(result["offer"])


class TestGroundAndConfirmPendingAction(unittest.TestCase):
    """Grounding in service of an action the user ALREADY asked for (policy
    stamped pending_action): never re-offer their own request — announce the
    save and hand back an auto-dispatch offer with the place pre-filled
    (QA 2026-07-30, the squash/Life Time loop)."""

    def _run(self, pending_action: str, others: int = 0, session_ctx=None):
        from app.circles_flow import ground_and_confirm

        with patch(
            "app.circles_flow._own_affiliation",
            return_value={"id": "a1", "circle_key": "squash_group"},
        ), patch(
            "app.circles_flow.ground_affiliation",
            return_value={
                "affiliation_id": "a1",
                "place_id": "p1",
                "place_name": "Life Time",
                "status": "confirmed",
            },
        ), patch(
            "app.circles_flow._place_co_member_count", return_value=others
        ), patch(
            "app.circles_flow._compose_grounding_reply", side_effect=_ECHO_FALLBACK
        ) as compose:
            result = ground_and_confirm(
                "u1", "a1", "gp1",
                session_ctx=session_ctx if session_ctx is not None else {},
                pending_action=pending_action,
            )
            return result, compose.call_args.kwargs

    def test_host_intent_dispatches_with_place_prefilled(self) -> None:
        ctx: dict = {}
        result, kwargs = self._run("host_meet", session_ctx=ctx)
        offer = result["offer"]
        self.assertTrue(offer["auto"])
        self.assertEqual(offer["kind"], "host_meet")
        self.assertIn("Life Time", offer["send"])
        self.assertIn("squash group", offer["send"])
        self.assertTrue(result["grounded"])
        self.assertIsNone(result["pending"])
        # The save is still announced (never silent) but nothing is re-offered.
        self.assertIn("saved to your communities", result["reply"])
        self.assertNotIn("?", kwargs["goal"].split(" — ")[0])
        self.assertTrue(ctx["_grounding_offer_done"])

    def test_host_intent_wins_over_intro_offer(self) -> None:
        # Co-members at the place would normally flip to the intro offer — but
        # the user asked to organize, so organizing wins.
        result, _ = self._run("host_meet", others=3)
        self.assertEqual(result["offer"]["kind"], "host_meet")
        self.assertTrue(result["offer"]["auto"])

    def test_find_neighbors_intent_dispatches(self) -> None:
        result, _ = self._run("find_neighbors")
        offer = result["offer"]
        self.assertTrue(offer["auto"])
        self.assertEqual(offer["kind"], "find_neighbors")
        self.assertIn("squash group", offer["send"])


class TestHandleGroundingConfirmation(unittest.TestCase):
    STATE = {
        "affiliation_id": "a1",
        "candidates": [{"name": "OrangeTheory Narcoossee",
                        "send": "It's OrangeTheory Narcoossee", "google_place_id": "gp1"}],
        "answer_text": "orange theory",
        "attempts": 1,
    }

    @patch("app.circles_flow.ground_and_confirm",
           return_value={"reply": "Locked in", "options": [], "pending": None, "grounded": True})
    def test_confirm_chip_grounds(self, gac) -> None:
        result = handle_grounding_confirmation("u1", self.STATE, "It's OrangeTheory Narcoossee")
        self.assertTrue(result["grounded"])
        gac.assert_called_once_with(
            "u1", "a1", "gp1", session_ctx=None, pending_action=None
        )

    @patch("app.circles_flow.ground_and_confirm",
           return_value={"reply": "Locked in", "options": [], "pending": None, "grounded": True})
    def test_confirm_chip_forwards_pending_action(self, gac) -> None:
        state = {**self.STATE, "pending_action": "host_meet"}
        handle_grounding_confirmation("u1", state, "It's OrangeTheory Narcoossee")
        gac.assert_called_once_with(
            "u1", "a1", "gp1", session_ctx=None, pending_action="host_meet"
        )

    @patch("app.circles_flow._compose_grounding_reply", side_effect=_ECHO_FALLBACK)
    @patch("app.circles_flow._home_block_id", return_value="b1")
    @patch("app.circles_flow.ground_options",
           return_value=[{"name": "LA Fitness", "label": "LA Fitness",
                          "send": "It's LA Fitness", "google_place_id": "gp2"}])
    @patch("app.circles_flow._own_affiliation",
           return_value={"id": "a1", "circle_key": "gym", "place_ref": None})
    def test_correction_keeps_pending_action_armed(self, _own, _go, _blk, _cr) -> None:
        # A re-search turn must not drop the live intent — the eventual tap
        # still has to dispatch it.
        state = {**self.STATE, "pending_action": "host_meet"}
        result = handle_grounding_confirmation("u1", state, "no, the LA Fitness one")
        self.assertEqual(result["pending"]["pending_action"], "host_meet")

    @patch("app.circles_flow._compose_grounding_reply", side_effect=_ECHO_FALLBACK)
    @patch("app.circles_flow.note_ungrounded_detail")
    @patch("app.circles_flow._own_affiliation",
           return_value={"id": "a1", "circle_type": "fitness", "circle_key": "gym",
                         "place_ref": None})
    def test_abandon_keeps_their_words_and_closes(self, _own, note, _cr) -> None:
        result = handle_grounding_confirmation("u1", self.STATE, "neither", abandon=True)
        self.assertIsNone(result["pending"])
        note.assert_called_once_with("u1", "a1", "orange theory")

    @patch("app.circles_flow._compose_grounding_reply", side_effect=_ECHO_FALLBACK)
    @patch("app.circles_flow.note_ungrounded_detail")
    @patch("app.circles_flow._own_affiliation",
           return_value={"id": "a1", "circle_type": "fitness", "circle_key": "gym",
                         "place_ref": None})
    def test_unpinned_close_still_offers_to_look_for_people(self, _own, _note, _cr) -> None:
        # No pin, but the community is known — the thread ends on an offer to look,
        # not a dead end (2026-08-03). It must not claim anyone is there.
        ctx: dict = {}
        result = handle_grounding_confirmation(
            "u1", self.STATE, "neither", session_ctx=ctx, abandon=True
        )
        offer = result["offer"]
        self.assertEqual(offer["kind"], "find_neighbors")
        self.assertIn("gym", offer["send"])
        self.assertTrue(ctx["_grounding_offer_done"])

    @patch("app.circles_flow._compose_grounding_reply", side_effect=_ECHO_FALLBACK)
    @patch("app.circles_flow._own_affiliation",
           return_value={"id": "a1", "circle_type": "fitness", "circle_key": "gym",
                         "place_ref": None})
    def test_escape_chip_asks_for_the_name_instead_of_closing(self, _own, _cr) -> None:
        from app.circles_flow import _ESCAPE_SEND

        result = handle_grounding_confirmation("u1", self.STATE, _ESCAPE_SEND)
        self.assertEqual(result["pending"]["attempts"], 2)
        # Candidates cleared so their next words drive a fresh search.
        self.assertEqual(result["pending"]["candidates"], [])
        self.assertIsNone(result.get("offer"))

    @patch("app.circles_flow._compose_grounding_reply", side_effect=_ECHO_FALLBACK)
    @patch("app.circles_flow._home_block_id", return_value=None)
    @patch("app.circles_flow.ground_options",
           return_value=[{"name": "Crunch Fitness", "address": "9 Elm", "google_place_id": "gp9"}])
    @patch("app.circles_flow._own_affiliation",
           return_value={"id": "a1", "circle_type": "fitness", "detail": "my gym", "place_ref": None})
    def test_correction_searches_once_more(self, _own, _go, _blk, _cr) -> None:
        result = handle_grounding_confirmation("u1", self.STATE, "no it's Crunch actually")
        self.assertEqual(result["pending"]["attempts"], 2)
        self.assertEqual(result["pending"]["candidates"][0]["google_place_id"], "gp9")

    @patch("app.circles_flow._compose_grounding_reply", side_effect=_ECHO_FALLBACK)
    @patch("app.circles_flow.note_ungrounded_detail")
    @patch("app.circles_flow._own_affiliation",
           return_value={"id": "a1", "circle_type": "fitness", "circle_key": "gym",
                         "place_ref": None})
    def test_worn_out_after_three_attempts(self, _own, note, _cr) -> None:
        state = {**self.STATE, "attempts": 3}
        result = handle_grounding_confirmation("u1", state, "some other gym")
        self.assertIsNone(result["pending"])
        note.assert_called_once()


class TestFollowupYieldsToGrounding(unittest.TestCase):
    """A turn that captured a circle gives its tile slot to the grounding question:
    the extractor's same-topic follow-up ("what do you enjoy at book club?") is
    suppressed — the §4.3 place-tagged enrichment re-asks it after grounding."""

    def _run(self, circles: int):
        from types import SimpleNamespace

        import app.claims_persist as cp

        claim = SimpleNamespace(bucket="activity", confidence=0.9, label="Book club member")
        with patch.object(cp, "persist_nickname_if_stated", return_value=None), \
             patch.object(cp, "should_extract_claims_from_message", return_value=True), \
             patch.object(cp, "fetch_active_claim_threads", return_value=[]), \
             patch("app.rapport_gaps.recent_gap_questions", return_value=[]), \
             patch.object(cp, "incremental_claims_from_utterance", return_value={"followup_topic": "about your book club…"}), \
             patch.object(cp, "parse_incremental_claims_data",
                          return_value=(None, [claim], None, "What do you enjoy most at book club?")), \
             patch.object(cp, "filter_extracted_claims", side_effect=lambda _m, c: c), \
             patch.object(cp, "persist_kids_count"), \
             patch.object(cp, "upsert_claims", return_value=1), \
             patch("app.circles_capture.run_circle_capture",
                   return_value={"circles": circles, "features": 0}), \
             patch.object(cp, "_open_rapport_gap") as open_gap:
            res = cp.try_upsert_claims_from_message("u1", "I'm in a book club with friends")
        return res, open_gap

    def test_circle_capture_suppresses_same_turn_followup(self) -> None:
        res, open_gap = self._run(circles=1)
        self.assertEqual(res.saved, 1)
        open_gap.assert_not_called()

    def test_no_circle_keeps_followup(self) -> None:
        res, open_gap = self._run(circles=0)
        self.assertEqual(res.saved, 1)
        open_gap.assert_called_once()


class TestRankerCadence(unittest.TestCase):
    CIRCLE_ROW = {"gap_row_id": "g1", "gap_id": "ground:a1", "parent_bucket": "interest",
                  "why_frame": "about your gym…", "question": "Which gym?",
                  "unlock_score": 0.75, "skipped_count": 0, "affiliation_ref": "a1",
                  "opened_at": "2026-07-25T00:00:00+00:00", "asked_at": None}
    NORMAL_ROW = {"gap_row_id": "g2", "gap_id": "deepen:reading", "parent_bucket": "general",
                  "why_frame": "one quick thing…", "question": "What do you read?",
                  "unlock_score": 0.5, "skipped_count": 0, "affiliation_ref": None,
                  "opened_at": "2026-07-24T00:00:00+00:00", "asked_at": None}

    def _next_ask(self, rows, circle_recent):
        import app.rapport_ranker as rr

        with patch.object(rr, "_preferred_lang", return_value=None), \
             patch.object(rr, "_pending_ask", return_value=None), \
             patch.object(rr, "_recently_asked", return_value=False), \
             patch.object(rr, "_max_tier_rank", return_value=0), \
             patch.object(rr, "_load_open_rows", return_value=rows), \
             patch.object(rr, "_circle_asked_recently", return_value=circle_recent), \
             patch.object(rr, "_with_grounding", side_effect=lambda _u, _r, ask: ask), \
             patch.object(rr, "service_client", return_value=_sb({"rapport_gaps": _Q()})), \
             patch.object(rr, "track"):
            return rr.next_ask("u1")

    def test_circle_winner_suppressed_when_recently_asked(self) -> None:
        ask = self._next_ask([dict(self.CIRCLE_ROW), dict(self.NORMAL_ROW)], circle_recent=True)
        self.assertEqual(ask["gap_row_id"], "g2")

    def test_circle_winner_serves_when_cadence_clear(self) -> None:
        ask = self._next_ask([dict(self.CIRCLE_ROW), dict(self.NORMAL_ROW)], circle_recent=False)
        self.assertEqual(ask["gap_row_id"], "g1")

    def test_circle_only_plate_still_serves(self) -> None:
        # Suppress-only: with nothing else open the grounding ask still runs.
        ask = self._next_ask([dict(self.CIRCLE_ROW)], circle_recent=True)
        self.assertEqual(ask["gap_row_id"], "g1")


if __name__ == "__main__":
    unittest.main()
