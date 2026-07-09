import unittest
from datetime import date, datetime
from unittest.mock import patch

import app.event_when as ew
from app.event_when import (
    _snap_future,
    event_local_dt,
    format_event_when,
    resolve_event_when,
)


class FormatEventWhenTests(unittest.TestCase):
    """THE tz-aware formatter — QA 2026-07-08: the same stored instant rendered as
    three different datetimes because each surface strftime'd the raw UTC value."""

    # QA fixture 1: stored UTC instant = Mon Jul 13, 8:30 PM America/New_York.
    QA_EVENING = "2026-07-14T00:30:00+00:00"
    # QA fixture 2: = Wed Aug 12, 12:00 AM (midnight) EDT. Correct DATA — the formatter
    # must say midnight truthfully; the midnight-party bug itself is a different PR.
    QA_MIDNIGHT = "2026-08-12T04:00Z"

    def setUp(self) -> None:
        # Pin the default tz so an ambient EVENT_DEFAULT_TZ can't skew assertions.
        patcher = patch.dict("os.environ", {"EVENT_DEFAULT_TZ": "America/New_York"})
        patcher.start()
        self.addCleanup(patcher.stop)

    # ── The two QA fixtures ──
    def test_qa_evening_card(self) -> None:
        self.assertEqual(format_event_when(self.QA_EVENING, style="card"), "Mon, Jul 13 · 8:30 PM")

    def test_qa_evening_inline(self) -> None:
        self.assertEqual(format_event_when(self.QA_EVENING, style="inline"), "Mon Jul 13, 8:30 PM")

    def test_qa_evening_never_shows_utc_date(self) -> None:
        for style in ("card", "inline"):
            self.assertNotIn("14", format_event_when(self.QA_EVENING, style=style))

    def test_qa_midnight_card(self) -> None:
        self.assertEqual(format_event_when(self.QA_MIDNIGHT, style="card"), "Wed, Aug 12 · 12:00 AM")

    def test_qa_midnight_inline(self) -> None:
        self.assertEqual(format_event_when(self.QA_MIDNIGHT, style="inline"), "Wed Aug 12, 12:00 AM")

    # ── DST boundaries (America/New_York: 2026 DST ends Nov 1, 2:00 AM) ──
    def test_dst_fall_back_before_transition(self) -> None:
        # 05:30 UTC = 1:30 AM EDT (UTC-4), still daylight time.
        self.assertEqual(
            format_event_when("2026-11-01T05:30:00+00:00", style="card"), "Sun, Nov 1 · 1:30 AM"
        )

    def test_dst_fall_back_after_transition(self) -> None:
        # 07:00 UTC = 2:00 AM EST (UTC-5) — same UTC instant math, new offset.
        self.assertEqual(
            format_event_when("2026-11-01T07:00:00Z", style="card"), "Sun, Nov 1 · 2:00 AM"
        )

    def test_winter_standard_time(self) -> None:
        # EST (UTC-5): 01:00 UTC Dec 20 = 8:00 PM Dec 19 local — date rolls back a day.
        self.assertEqual(
            format_event_when("2026-12-20T01:00:00Z", style="card"), "Sat, Dec 19 · 8:00 PM"
        )

    # ── Input shapes ──
    def test_naive_input_is_event_local_wall_clock_not_shifted(self) -> None:
        # In-flight drafts are naive event-local (see event_publish._parse_iso_ts).
        self.assertEqual(
            format_event_when("2026-07-13T20:30:00", style="card"), "Mon, Jul 13 · 8:30 PM"
        )

    def test_date_only_renders_without_clock(self) -> None:
        self.assertEqual(format_event_when("2026-07-13", style="card"), "Mon, Jul 13")
        self.assertEqual(format_event_when("2026-07-13", style="inline"), "Mon Jul 13")

    def test_tz_override(self) -> None:
        self.assertEqual(
            format_event_when(self.QA_EVENING, tz="America/Los_Angeles", style="card"),
            "Mon, Jul 13 · 5:30 PM",
        )

    def test_empty_is_none(self) -> None:
        self.assertIsNone(format_event_when(""))
        self.assertIsNone(format_event_when(None))

    def test_unparseable_degrades_to_prefix(self) -> None:
        self.assertEqual(format_event_when("not-a-date-at-all"), "not-a-date")

    def test_unknown_style_rejected(self) -> None:
        with self.assertRaises(ValueError):
            format_event_when(self.QA_EVENING, style="fancy")

    def test_event_local_dt_converts_aware_to_event_tz(self) -> None:
        dt = event_local_dt(self.QA_EVENING)
        assert dt is not None
        self.assertEqual((dt.year, dt.month, dt.day, dt.hour, dt.minute), (2026, 7, 13, 20, 30))


class SnapFutureTests(unittest.TestCase):
    today = date(2026, 6, 26)

    def test_future_date_passes_through(self) -> None:
        self.assertEqual(_snap_future("2026-06-28", self.today), "2026-06-28")

    def test_today_passes_through(self) -> None:
        self.assertEqual(_snap_future("2026-06-26", self.today), "2026-06-26")

    def test_past_bare_date_bumps_to_next_year(self) -> None:
        # A month/day already gone by this year → next year's occurrence.
        self.assertEqual(_snap_future("2026-01-10", self.today), "2027-01-10")

    def test_unparseable_is_dropped(self) -> None:
        self.assertIsNone(_snap_future("not-a-date", self.today))


class ResolveEventWhenTests(unittest.TestCase):
    NOW = datetime(2026, 6, 26, 9, 0, 0)

    def _patch_llm(self, payload):
        """Stub the LLM layer so resolve_event_when runs deterministically."""
        import app.orchestrator.llm as llm

        self._orig = (llm.llm_configured, llm.llm_json, llm.synthesizer_model)
        llm.llm_configured = lambda: True
        llm.synthesizer_model = lambda: "test-model"
        llm.llm_json = lambda **kwargs: payload
        self.addCleanup(self._restore, llm)

    def _restore(self, llm) -> None:
        llm.llm_configured, llm.llm_json, llm.synthesizer_model = self._orig

    def test_ordinal_date_resolved(self) -> None:
        # "28th June" — the exact phrasing the old regex could not parse.
        self._patch_llm({"date": "2026-06-28", "time": None})
        out = resolve_event_when(
            history=[],
            user_message="i want it on 28th June, not on friday",
            draft={},
            now=self.NOW,
        )
        self.assertEqual(out, {"date": "2026-06-28"})

    def test_time_normalized(self) -> None:
        self._patch_llm({"date": None, "time": "21:00"})
        out = resolve_event_when(
            history=[], user_message="at 9pm", draft={}, now=self.NOW
        )
        self.assertEqual(out, {"time": "21:00"})

    def test_no_change_returns_empty_dict(self) -> None:
        # Model ran but saw no date/time — empty dict, NOT None (distinct from no-LLM).
        self._patch_llm({"date": None, "time": None})
        out = resolve_event_when(
            history=[], user_message="my place", draft={}, now=self.NOW
        )
        self.assertEqual(out, {})

    def test_past_date_from_model_snapped_forward(self) -> None:
        self._patch_llm({"date": "2026-01-05", "time": None})
        out = resolve_event_when(
            history=[], user_message="january 5th", draft={}, now=self.NOW
        )
        self.assertEqual(out, {"date": "2027-01-05"})

    def test_garbage_values_ignored(self) -> None:
        self._patch_llm({"date": "next week", "time": "9pm"})
        out = resolve_event_when(
            history=[], user_message="whenever", draft={}, now=self.NOW
        )
        self.assertEqual(out, {})

    def test_llm_unavailable_returns_none(self) -> None:
        # None signals the caller to fall back to the regex resolver.
        import app.orchestrator.llm as llm

        orig = llm.llm_configured
        llm.llm_configured = lambda: False
        self.addCleanup(setattr, llm, "llm_configured", orig)
        out = resolve_event_when(
            history=[], user_message="28th June", draft={}, now=self.NOW
        )
        self.assertIsNone(out)


if __name__ == "__main__":
    unittest.main()
