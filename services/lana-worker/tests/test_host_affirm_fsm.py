"""QA 2026-07-08 regression pack — the conversational host flow (0/16 sims ever posted).

Fixtures are the production findings, frozen on Wednesday 2026-07-08:
- "yes that works" on the confirm card publishes (never the "Updated **X** — does this
  look right?" forever-loop); a yes with a missing blocker asks for exactly ONE field.
- "tomorrow" grounds to 2026-07-09 and "thursday" to 2026-07-09 (host-local day, not
  the server's UTC day which is already Thursday every Wednesday evening).
- starts_at is strict ISO or null — prose like "next Wednesday 16:00:00" is dropped.
- Recurrence ("MWF 7am", "wednesdays 4pm") grounds the draft on the FIRST occurrence
  instead of nulling it.
- A bare 5-digit token ("34786") is a ZIP, never a venue.
"""

import unittest
from datetime import date, datetime, timezone
from unittest import mock

from app.event_when import event_local_now, resolve_event_when
from app.lana_ui import parse_event_draft
from app.lana_unified_pipeline import (
    _apply_host_brain,
    _classify_host_reply,
    _detect_recurrence,
    _first_recurrence_date,
    _friendly_when,
    _is_host_affirm,
    _is_zip_token,
    _resolve_event_date,
    _resolve_event_time,
    run_lana_unified_pipeline,
)

QA_TODAY = date(2026, 7, 8)  # Wednesday
QA_NOW = datetime(2026, 7, 8, 9, 0, 0)


class TestAffirmClassifier(unittest.TestCase):
    """Deterministic affirmation read — BEFORE any extraction (task rule #1)."""

    def test_clear_affirmations_match(self) -> None:
        for msg in (
            "yes that works", "yes", "Yes!", "looks good", "perfect", "that works",
            "sounds good", "yep", "sure, that works for me", "okay great", "all set",
        ):
            self.assertTrue(_is_host_affirm(msg), msg)
            self.assertEqual(_classify_host_reply(msg), "affirm", msg)

    def test_info_carrying_or_negative_replies_do_not_match(self) -> None:
        # Anything carrying a value / objection must flow through extraction instead.
        for msg in (
            "yes at 4pm", "no", "not quite", "change the time to 5",
            "make it saturday instead", "yes but at my place", "what time works?",
            "I'll use 34786 as the meeting spot", "wednesdays 4pm",
        ):
            self.assertFalse(_is_host_affirm(msg), msg)
        self.assertEqual(_classify_host_reply("change the time to 5"), "other")

    def test_cta_buttons_still_classify(self) -> None:
        self.assertEqual(_classify_host_reply("Drop the meet up"), "drop")
        self.assertEqual(_classify_host_reply("Let me tweak"), "tweak")
        self.assertEqual(_classify_host_reply("Looks good · next"), "affirm")


class _PipelineHarness(unittest.TestCase):
    """Drive the real host stage machine with the LLM seams stubbed out."""

    FULL_DRAFT = {
        "title": "Street Moms Coffee Morning",
        "description": "Morning coffee on the block.",
        "venue_name": "Foxtail Coffee",
        "venue_address": "123 Ave",
        "place_id": "pid_1",
        "venue_lat": 28.36,
        "venue_lng": -81.25,
        "starts_at": "2026-07-11T09:00:00",
        "event_setup": {"capacity_label": "How many moms?"},
    }

    def _ctx(self, stage: str, draft: dict) -> dict:
        return {
            "event_host_active": True,
            "host_stage": stage,
            "event_draft": dict(draft),
            "event_place_asked": True,
            "event_when_date": "2026-07-11" if draft.get("starts_at") else None,
            "event_when_time": "09:00" if draft.get("starts_at") else None,
        }

    def _run(self, message: str, session_ctx: dict, *, phone_verified: bool = True):
        def fake_run_turn(**kw):
            ctx = dict(kw["session_ctx"])
            ui = {"bucket": None, "focus_phrase": None, "highlights": []}
            return "", "continue", ctx, ui, ctx.get("event_draft")

        with mock.patch(
            "app.lana_unified_pipeline.unified_rules_first_enabled", return_value=False
        ), mock.patch(
            "app.lana_unified_pipeline.run_turn", side_effect=fake_run_turn
        ), mock.patch(
            "app.lana_unified_pipeline._auto_publish_event", return_value=("evt-1", None)
        ) as publish, mock.patch(
            "app.event_when.resolve_event_when", return_value={}
        ), mock.patch(
            "app.event_when.event_local_now", return_value=QA_NOW
        ), mock.patch(
            "app.host_turn.host_turn_brain", return_value=None
        ):
            reply, status, ctx, ui, draft = run_lana_unified_pipeline(
                user_id="u1",
                session_id="s1",
                history=[],
                user_message=message,
                session_ctx=session_ctx,
                user_jwt="jwt",
                phone_verified=phone_verified,
                home_block_id="blk",
                is_anonymous=False,
            )
        return reply, ctx, draft, publish


class TestYesAdvancesTheDraft(_PipelineHarness):
    """QA: 'yes that works' after a complete draft looped verbatim forever."""

    def test_yes_on_confirm_publishes(self) -> None:
        reply, ctx, _, publish = self._run(
            "yes that works", self._ctx("confirm", self.FULL_DRAFT)
        )
        publish.assert_called_once()
        self.assertEqual(ctx.get("event_id"), "evt-1")
        self.assertTrue(ctx.get("event_published_now"))
        self.assertIn("live", reply.lower())
        self.assertNotIn("does this look right", reply.lower())

    def test_yes_on_review_advances_never_loops(self) -> None:
        reply, ctx, _, publish = self._run(
            "yes that works", self._ctx("review", self.FULL_DRAFT)
        )
        self.assertEqual(ctx.get("host_stage"), "setup")
        self.assertNotIn("Updated", reply)
        self.assertNotIn("does this look right", reply.lower())
        publish.assert_not_called()

    def test_yes_on_setup_advances_to_confirm(self) -> None:
        reply, ctx, _, publish = self._run(
            "looks good", self._ctx("setup", self.FULL_DRAFT)
        )
        self.assertEqual(ctx.get("host_stage"), "confirm")
        publish.assert_not_called()

    def test_yes_with_missing_field_asks_exactly_one(self) -> None:
        draft = {k: v for k, v in self.FULL_DRAFT.items() if not k.startswith("venue")}
        draft.pop("place_id", None)
        draft.pop("venue_lat", None)
        draft.pop("venue_lng", None)
        reply, ctx, _, publish = self._run("yes that works", self._ctx("setup", draft))
        publish.assert_not_called()
        self.assertEqual(ctx.get("host_stage"), "setup")
        # Exactly ONE ask (the missing place) — never the multi-field " · " list and
        # never the unchanged card (host_aside surfaces the text, not the carousel).
        self.assertIn("where should we meet", reply.lower())
        self.assertNotIn(" · ", reply)
        self.assertNotIn("a name", reply)
        self.assertNotIn("date & time", reply)
        self.assertTrue(ctx.get("host_aside"))


class TestDateGrounding(unittest.TestCase):
    """QA: on Wed 2026-07-08, 'tomorrow' drafted 07-10 and 'thursday' drafted 07-16 —
    the resolver was anchored on the server's UTC day (already Thursday that evening)."""

    def test_event_local_now_uses_event_tz_not_utc(self) -> None:
        with mock.patch.dict("os.environ", {"EVENT_DEFAULT_TZ": "America/New_York"}):
            # Wed 2026-07-08 21:30 in Orlando is ALREADY Thu 2026-07-09 01:30 UTC.
            local = event_local_now(datetime(2026, 7, 9, 1, 30, tzinfo=timezone.utc))
        self.assertEqual(local.date(), QA_TODAY)
        self.assertEqual(local.strftime("%A"), "Wednesday")

    def test_tomorrow_is_july_9(self) -> None:
        self.assertEqual(
            _resolve_event_date("tomorrow at sunrise", today=QA_TODAY), "2026-07-09"
        )

    def test_bare_thursday_is_july_9_not_16(self) -> None:
        self.assertEqual(
            _resolve_event_date("thursday morning", today=QA_TODAY), "2026-07-09"
        )

    def test_llm_resolver_is_anchored_on_local_wednesday(self) -> None:
        import app.orchestrator.llm as llm

        captured: dict = {}

        def fake_llm_json(**kwargs):
            captured.update(kwargs)
            return {"date": "2026-07-09", "time": "06:30"}

        orig = (llm.llm_configured, llm.llm_json, llm.synthesizer_model)
        llm.llm_configured = lambda: True
        llm.llm_json = fake_llm_json
        llm.synthesizer_model = lambda: "test-model"
        try:
            out = resolve_event_when(
                history=[], user_message="tomorrow at sunrise", draft={}, now=QA_NOW
            )
        finally:
            llm.llm_configured, llm.llm_json, llm.synthesizer_model = orig
        self.assertIn("TODAY: Wednesday, 2026-07-08", captured["user_payload"])
        self.assertEqual(out, {"date": "2026-07-09", "time": "06:30"})


class TestStartsAtIsoOrNull(unittest.TestCase):
    """QA: 'wednesdays 4pm' produced starts_at 'next Wednesday 16:00:00' (non-ISO)."""

    def test_prose_starts_at_is_dropped(self) -> None:
        parsed = parse_event_draft({"starts_at": "next Wednesday 16:00:00"})
        self.assertIsNone(parsed["starts_at"])

    def test_iso_values_pass_through(self) -> None:
        for iso in ("2026-07-11T09:00:00", "2026-07-11T09:00:00Z", "2026-07-11 09:00:00"):
            self.assertEqual(parse_event_draft({"starts_at": iso})["starts_at"], iso, iso)

    def test_prose_ends_at_is_dropped_too(self) -> None:
        self.assertIsNone(parse_event_draft({"ends_at": "after an hour or so"})["ends_at"])


class TestRecurrenceFirstOccurrence(_PipelineHarness):
    """QA: 'MWF 7am' nulled the whole draft; 'wednesdays 4pm' emitted prose starts_at."""

    def test_detects_cadences(self) -> None:
        self.assertEqual(_detect_recurrence("MWF 7am before preschool dropoff"), [0, 2, 4])
        self.assertEqual(_detect_recurrence("wednesdays 4pm"), [2])
        self.assertEqual(_detect_recurrence("every tuesday after school"), [1])
        self.assertEqual(_detect_recurrence("mon/wed/fri mornings"), [0, 2, 4])
        self.assertEqual(_detect_recurrence("a weekly walk"), [])
        self.assertIsNone(_detect_recurrence("wednesday 4pm"))  # single date, no cadence
        self.assertIsNone(_detect_recurrence("yes that works"))

    def test_first_occurrence_from_frozen_wednesday(self) -> None:
        # Wed 2026-07-08 → MWF starts this Friday; "wednesdays" starts NEXT Wednesday.
        self.assertEqual(_first_recurrence_date([0, 2, 4], QA_TODAY), "2026-07-10")
        self.assertEqual(_first_recurrence_date([2], QA_TODAY), "2026-07-15")
        self.assertIsNone(_first_recurrence_date([], QA_TODAY))

    def test_mwf_7am_keeps_the_draft_and_grounds_first_meet(self) -> None:
        draft = {
            "title": "Preschool Walk Crew",
            "venue_name": "Foxtail Coffee",
            "place_id": "pid_1",
            "venue_lat": 28.36,
            "venue_lng": -81.25,
            "event_setup": {"capacity_label": "How many moms?"},
        }
        reply, ctx, out, publish = self._run(
            "MWF 7am before preschool dropoff", self._ctx("setup", draft)
        )
        publish.assert_not_called()
        # The rest of the draft survives, and the first occurrence is grounded:
        # Wed 2026-07-08 → Fri 2026-07-10 at 07:00 (never a null or prose starts_at).
        self.assertEqual(out["title"], "Preschool Walk Crew")
        self.assertEqual(out["starts_at"], "2026-07-10T07:00:00")
        self.assertIn("weekly meets are coming", reply.lower())
        self.assertIn("first one", reply.lower())

    def test_wednesdays_4pm_first_occurrence_phrase(self) -> None:
        self.assertEqual(_resolve_event_time("wednesdays 4pm"), "16:00")
        self.assertEqual(_friendly_when("2026-07-15", "16:00"), "Wed Jul 15, 4 PM")


class TestZipIsNeverAVenue(_PipelineHarness):
    """QA: 'I'll use 34786 as the meeting spot' pinned a bare ZIP as the venue."""

    def test_zip_token_detection(self) -> None:
        self.assertTrue(_is_zip_token("34786"))
        self.assertTrue(_is_zip_token(" 34786 "))
        self.assertTrue(_is_zip_token("34786-1234"))
        self.assertFalse(_is_zip_token("Foxtail Coffee"))
        self.assertFalse(_is_zip_token("Suite 34786 Cafe"))
        self.assertFalse(_is_zip_token("123 Main St"))

    def test_extractor_venue_zip_is_dropped(self) -> None:
        self.assertIsNone(parse_event_draft({"venue_name": "34786"})["venue_name"])
        self.assertEqual(
            parse_event_draft({"venue_name": "Foxtail Coffee"})["venue_name"],
            "Foxtail Coffee",
        )

    def test_host_brain_zip_place_is_rejected(self) -> None:
        ed: dict = {}
        _apply_host_brain(
            {"place": "34786", "reply": "x"}, ed, {}, {}, {}, existing_title=""
        )
        self.assertNotIn("venue_name", ed)

    def test_pipeline_keeps_venue_empty_and_asks_for_a_place(self) -> None:
        draft = {
            "title": "Street Moms Coffee Morning",
            "venue_name": "34786",  # a stale extractor mistake persisted on the draft
            "starts_at": "2026-07-11T09:00:00",
            "event_setup": {"capacity_label": "How many moms?"},
        }
        reply, ctx, out, publish = self._run(
            "I'll use 34786 as the meeting spot", self._ctx("setup", draft)
        )
        publish.assert_not_called()
        self.assertFalse(str(out.get("venue_name") or "").strip())
        self.assertIn("a place", reply)


if __name__ == "__main__":
    unittest.main()
