import unittest
from datetime import date, datetime

import app.event_when as ew
from app.event_when import _snap_future, resolve_event_when


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
