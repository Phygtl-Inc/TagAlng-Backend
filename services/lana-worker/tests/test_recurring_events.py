"""Recurring meets: what the AI resolver is allowed to hand us, and what reaches publish.

The recurrence DATE MATH lives in SQL (public.next_occurrence) because both the /meet
preview RPC and the worker's read-repair need one copy of it; what's covered here is the
capture path — the two places a bad cadence would either break a publish or silently turn
a one-off into a standing weekly commitment.
"""

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from app.event_publish import _parse_until_date, build_create_event_fields, event_tz
from app.event_when import resolve_event_when
from app.models import EventDraft


class ResolveRecurrenceTests(unittest.TestCase):
    NOW = datetime(2026, 8, 10, 9, 0, 0)  # a Monday

    def _resolve(self, payload):
        import app.orchestrator.llm as llm

        orig = (llm.llm_configured, llm.llm_json, llm.synthesizer_model)
        llm.llm_configured = lambda: True
        llm.synthesizer_model = lambda: "test-model"
        llm.llm_json = lambda **kwargs: payload
        self.addCleanup(lambda: setattr_all(llm, orig))
        return resolve_event_when(history=[], user_message="x", draft={}, now=self.NOW)

    def test_weekly_cadence_and_end_date_pass_through(self) -> None:
        out = self._resolve(
            {"date": "2026-08-14", "time": "18:00", "repeats": "weekly", "until": "2026-08-31"}
        )
        self.assertEqual(out["repeats"], "weekly")
        self.assertEqual(out["until"], "2026-08-31")
        self.assertEqual(out["date"], "2026-08-14")

    def test_unknown_cadence_is_dropped(self) -> None:
        # "daily" would fail the events_recurrence_valid check constraint at publish; a
        # dropped cadence just publishes a normal one-off meet.
        out = self._resolve({"date": "2026-08-14", "time": "18:00", "repeats": "daily"})
        self.assertNotIn("repeats", out)

    def test_one_off_stays_one_off(self) -> None:
        out = self._resolve({"date": "2026-08-14", "time": "18:00", "repeats": None})
        self.assertNotIn("repeats", out)
        self.assertNotIn("until", out)

    def test_past_end_date_is_dropped(self) -> None:
        # A past "until" would end the series on its very first roll. _snap_future bumps a
        # bare month/day to next year; anything still behind us is dropped.
        out = self._resolve({"repeats": "weekly", "until": "not-a-date"})
        self.assertNotIn("until", out)


def setattr_all(llm, orig) -> None:
    llm.llm_configured, llm.llm_json, llm.synthesizer_model = orig


class PublishFieldsTests(unittest.TestCase):
    def _fields(self, draft: EventDraft) -> dict:
        with patch(
            "app.event_publish.resolve_event_location", return_value=(1.0, 2.0, "blk-1")
        ), patch("app.event_publish._valid_purpose_ids", return_value=set()), patch(
            "app.event_publish._ai_event_description", return_value=None
        ), patch("app.event_publish._ai_cover_emoji", return_value=None):
            return build_create_event_fields("u-1", draft)

    def test_recurrence_reaches_create_event(self) -> None:
        until = (datetime.now(event_tz()).date() + timedelta(days=30)).isoformat()
        fields = self._fields(
            EventDraft(title="Badminton", recurrence="weekly", recurrence_until=until)
        )
        self.assertEqual(fields["recurrence"], "weekly")
        self.assertEqual(fields["recurrence_until"], until)

    def test_one_off_sends_no_recurrence_keys(self) -> None:
        fields = self._fields(EventDraft(title="Coffee"))
        self.assertNotIn("recurrence", fields)
        self.assertNotIn("recurrence_until", fields)

    def test_bogus_cadence_publishes_as_one_off(self) -> None:
        fields = self._fields(EventDraft(title="Coffee", recurrence="daily"))
        self.assertNotIn("recurrence", fields)

    def test_until_without_cadence_is_ignored(self) -> None:
        # An end date on a non-recurring meet is meaningless — never store it.
        fields = self._fields(EventDraft(title="Coffee", recurrence_until="2027-01-01"))
        self.assertNotIn("recurrence_until", fields)

    def test_past_until_dropped_but_series_survives(self) -> None:
        yesterday = (datetime.now(event_tz()).date() - timedelta(days=1)).isoformat()
        fields = self._fields(
            EventDraft(title="Badminton", recurrence="weekly", recurrence_until=yesterday)
        )
        self.assertEqual(fields["recurrence"], "weekly")
        # No end date → the DB's 180-day default, not a series that ends on its first roll.
        self.assertNotIn("recurrence_until", fields)


class HostBrainRepeatsTests(unittest.TestCase):
    """Mid-flow capture ("make it a weekly thing") is the host-turn AI's job, not a word
    list — this is the only gate the cadence passes through on those turns."""

    def _brain(self, payload):
        import app.orchestrator.llm as llm
        from app.host_turn import host_turn_brain

        orig = (llm.llm_configured, llm.llm_json, llm.synthesizer_model)
        llm.llm_configured = lambda: True
        llm.synthesizer_model = lambda: "test-model"
        llm.llm_json = lambda **kwargs: payload
        self.addCleanup(lambda: setattr_all(llm, orig))
        return host_turn_brain(history=[], user_message="x", draft={}, needed=[])

    def test_cadence_read_from_the_turn(self) -> None:
        out = self._brain({"reply": "Lovely — every Friday it is.", "repeats": "weekly"})
        self.assertEqual(out["repeats"], "weekly")

    def test_none_is_kept_as_an_instruction_not_dropped(self) -> None:
        # "actually just this once" has to reach the pipeline as a distinct value, since
        # that's what takes an already-set cadence back off.
        out = self._brain({"reply": "Got it, one-time.", "repeats": "none"})
        self.assertEqual(out["repeats"], "none")

    def test_silence_and_nonsense_leave_the_cadence_alone(self) -> None:
        for payload in ({"reply": "ok"}, {"reply": "ok", "repeats": "daily"}):
            self.assertIsNone(self._brain(payload)["repeats"], payload)


class DraftAcrossTurnsTests(unittest.TestCase):
    """merge_event_drafts REBUILDS the draft from parse_event_draft's key list every turn,
    so a cadence that isn't listed there is silently gone by the next chip tap — the host
    says "every Friday" and publishes a one-off. These are that regression."""

    def test_cadence_survives_a_later_turn(self) -> None:
        from app.lana_ui import merge_event_drafts

        prev = {
            "title": "Badminton",
            "starts_at": "2026-08-14T18:00:00",
            "recurrence": "weekly",
            "recurrence_until": "2026-09-30",
        }
        merged = merge_event_drafts(prev, {"max_attendees": 8})  # host taps a capacity chip
        self.assertEqual(merged["recurrence"], "weekly")
        self.assertEqual(merged["recurrence_until"], "2026-09-30")

    def test_later_turn_can_change_the_cadence(self) -> None:
        from app.lana_ui import merge_event_drafts

        merged = merge_event_drafts({"recurrence": "weekly"}, {"recurrence": "monthly"})
        self.assertEqual(merged["recurrence"], "monthly")

    def test_unknown_cadence_never_enters_the_draft(self) -> None:
        from app.lana_ui import merge_event_drafts

        self.assertIsNone(merge_event_drafts({}, {"recurrence": "daily"})["recurrence"])

    def test_clear_takes_the_cadence_back_off(self) -> None:
        from app.lana_ui import merge_event_drafts

        merged = merge_event_drafts(
            {"recurrence": "weekly"}, {}, clear_fields=["recurrence"]
        )
        self.assertIsNone(merged["recurrence"])


class ParseUntilTests(unittest.TestCase):
    def test_shapes(self) -> None:
        today = datetime.now(event_tz()).date()
        self.assertEqual(_parse_until_date(today.isoformat()), today.isoformat())
        self.assertIsNone(_parse_until_date(None))
        self.assertIsNone(_parse_until_date(""))
        self.assertIsNone(_parse_until_date("next friday"))
        self.assertIsNone(_parse_until_date("2020-01-01"))


if __name__ == "__main__":
    unittest.main()
