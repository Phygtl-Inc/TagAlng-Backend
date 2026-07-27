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


class TestGroundOptionsKeywordFallback(unittest.TestCase):
    @patch("app.places.search_places")
    def test_sentence_phrase_falls_back_to_type_keyword(self, sp) -> None:
        # "We go to church on sundays" matches nothing; retry uses the faith keyword.
        from app.circles_flow import ground_options

        sp.side_effect = [
            [],
            [{"name": "First Baptist", "address": "1 Main St", "place_id": "gp1"}],
        ]
        got = ground_options(
            "u1",
            {"circle_type": "faith", "detail": "We go to church on sundays"},
            block_id=None,
        )
        self.assertEqual(got[0]["google_place_id"], "gp1")
        self.assertEqual(sp.call_args_list[0].kwargs["query"], "We go to church on sundays")
        self.assertEqual(
            sp.call_args_list[1].kwargs["query"], "church mosque synagogue temple"
        )

    @patch("app.places.search_places")
    def test_no_retry_when_phrase_hits(self, sp) -> None:
        from app.circles_flow import ground_options

        sp.return_value = [{"name": "OrangeTheory", "address": "2 Elm", "place_id": "gp2"}]
        got = ground_options("u1", {"circle_type": "fitness", "detail": "my gym"}, block_id=None)
        self.assertEqual(len(got), 1)
        self.assertEqual(sp.call_count, 1)


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

    @patch("app.circles_flow._compose_grounding_reply", side_effect=_ECHO_FALLBACK)
    @patch("app.circles_flow.note_ungrounded_detail")
    @patch("app.circles_flow._home_block_id", return_value=None)
    @patch("app.circles_flow.ground_options", return_value=[])
    @patch("app.circles_flow._own_affiliation",
           return_value={"id": "a1", "circle_type": "fitness", "detail": "my gym", "place_ref": None})
    def test_unmatchable_answer_kept_as_detail(self, _own, _go, _blk, note, _cr) -> None:
        gap = {**self.GAP, "grounding_options": None}
        result = handle_grounding_answer("u1", gap, "the little studio by Publix")
        self.assertIsNone(result["pending"])
        self.assertFalse(result["grounded"])
        note.assert_called_once_with("u1", "a1", "the little studio by Publix")


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
        gac.assert_called_once_with("u1", "a1", "gp1", session_ctx=None)

    @patch("app.circles_flow._compose_grounding_reply", side_effect=_ECHO_FALLBACK)
    @patch("app.circles_flow.note_ungrounded_detail")
    def test_abandon_keeps_their_words_and_closes(self, note, _cr) -> None:
        result = handle_grounding_confirmation("u1", self.STATE, "neither", abandon=True)
        self.assertIsNone(result["pending"])
        note.assert_called_once_with("u1", "a1", "orange theory")

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
    def test_worn_out_after_three_attempts(self, note, _cr) -> None:
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
