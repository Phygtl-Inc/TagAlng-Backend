"""Constraint memory (QA 2026-07-08): availability/need constraints are captured,
acknowledged, applied to every event result set, and multi-need asks never silently
drop a need."""

import unittest
from unittest.mock import patch

from app.constraints import (
    ACK_CTX_KEY,
    CONSTRAINT_CTX_KEY,
    KID_AGE_KIND,
    MULTI_NEED_STACK_KEY,
    PARKED_NEEDS_KEY,
    TIME_KIND,
    capture_constraints_for_turn,
    constraint_ack_line,
    constraints_all_filtered_note,
    detect_multi_needs,
    extract_constraints_from_message,
    filter_events_by_constraints,
    finalize_constraint_turn,
)
from app.db import merge_session_context

_QA_LINE = "single working mom — I can only do evenings after 6 or weekends"


def _evening_weekend_constraints() -> dict:
    return extract_constraints_from_message(_QA_LINE)


class TestExtraction(unittest.TestCase):
    def test_evenings_after_6_or_weekends(self) -> None:
        out = _evening_weekend_constraints()
        self.assertIn(TIME_KIND, out)
        windows = out[TIME_KIND]["windows"]
        # One evening window starting 6 PM + one all-day weekend window (OR semantics).
        evening = [w for w in windows if w.get("start_minute") == 18 * 60]
        weekend = [w for w in windows if w.get("days") == "weekend"]
        self.assertTrue(evening)
        self.assertTrue(weekend)
        label = out[TIME_KIND]["label"].lower()
        self.assertIn("evening", label)
        self.assertIn("weekend", label)

    def test_weekday_evenings_is_one_combined_window(self) -> None:
        out = extract_constraints_from_message("we're only free weekday evenings")
        windows = out[TIME_KIND]["windows"]
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0]["days"], "weekday")
        self.assertEqual(windows[0]["start_minute"], 17 * 60)

    def test_mornings_only(self) -> None:
        out = extract_constraints_from_message("mornings only for me please")
        windows = out[TIME_KIND]["windows"]
        self.assertEqual(windows[0]["end_minute"], 12 * 60)

    def test_cant_do_weekdays(self) -> None:
        out = extract_constraints_from_message("I can't do weekdays")
        self.assertEqual(out[TIME_KIND]["windows"][0]["days"], "weekend")

    def test_browse_query_is_not_a_constraint(self) -> None:
        # "this weekend?" is a search filter, not a durable availability statement.
        self.assertEqual(extract_constraints_from_message("anything fun this weekend?"), {})
        self.assertEqual(extract_constraints_from_message("show me morning meetups"), {})

    def test_kid_age_band(self) -> None:
        out = extract_constraints_from_message("my 3 year old needs playmates")
        self.assertEqual(out[KID_AGE_KIND]["min_years"], 3)
        out = extract_constraints_from_message("I have a toddler at home")
        self.assertEqual(out[KID_AGE_KIND]["min_years"], 1)
        self.assertEqual(out[KID_AGE_KIND]["max_years"], 3)

    def test_ack_line(self) -> None:
        ack = constraint_ack_line(_evening_weekend_constraints())
        self.assertTrue(ack.endswith("— noted."))
        self.assertIn("evenings", ack.lower())
        self.assertIn("weekends", ack.lower())


class TestEventFilter(unittest.TestCase):
    def test_evening_only_filters_10am_event_out(self) -> None:
        constraints = _evening_weekend_constraints()
        events = [
            # Wednesday 10 AM — the QA "coffee morning" that must never be shown.
            {"title": "Coffee morning", "starts_at": "2026-07-15T10:00:00"},
            # Wednesday 6:30 PM — inside the evening window.
            {"title": "Evening stroll", "starts_at": "2026-07-15T18:30:00"},
            # Saturday 10 AM — weekends are fully open.
            {"title": "Saturday picnic", "starts_at": "2026-07-18T10:00:00"},
        ]
        kept, dropped = filter_events_by_constraints(events, constraints)
        titles = [e["title"] for e in kept]
        self.assertEqual(dropped, 1)
        self.assertNotIn("Coffee morning", titles)
        self.assertIn("Evening stroll", titles)
        self.assertIn("Saturday picnic", titles)

    def test_fail_open_without_constraints_or_start(self) -> None:
        events = [{"title": "Mystery", "starts_at": None}]
        kept, dropped = filter_events_by_constraints(events, None)
        self.assertEqual((len(kept), dropped), (1, 0))
        kept, dropped = filter_events_by_constraints(events, _evening_weekend_constraints())
        self.assertEqual((len(kept), dropped), (1, 0))  # unparseable start → keep

    def test_all_filtered_note_names_the_window(self) -> None:
        note = constraints_all_filtered_note(_evening_weekend_constraints())
        self.assertIn("outside", note.lower())
        self.assertIn("evenings", note.lower())
        self.assertIn("listen", note.lower())


class TestCaptureAndAck(unittest.TestCase):
    def test_capture_stamps_ctx_and_ack(self) -> None:
        ctx: dict = {}
        ack = capture_constraints_for_turn(ctx, _QA_LINE)
        self.assertIsNotNone(ack)
        self.assertIn("noted", ack.lower())
        self.assertIn(TIME_KIND, ctx[CONSTRAINT_CTX_KEY])
        self.assertEqual(ctx[ACK_CTX_KEY], ack)

    def test_finalize_prepends_ack_and_carries_memory(self) -> None:
        ctx_in: dict = {}
        capture_constraints_for_turn(ctx_in, _QA_LINE)
        turn_ctx: dict = {"routing_phase": "listening"}  # handler built a fresh dict
        reply = finalize_constraint_turn("Here's what's coming up:", ctx_in, turn_ctx)
        self.assertTrue(reply.lower().startswith("evenings"))
        self.assertIn("— noted.", reply)
        self.assertIn("Here's what's coming up:", reply)
        # Constraints ride the returned ctx into the persisted session; the ack doesn't.
        self.assertIn(CONSTRAINT_CTX_KEY, turn_ctx)
        self.assertNotIn(ACK_CTX_KEY, turn_ctx)

    def test_constraint_persists_across_turns_in_session_context(self) -> None:
        # Turn 1: constraint stated; the returned ctx merges into the stored session.
        ctx_in: dict = {}
        capture_constraints_for_turn(ctx_in, _QA_LINE)
        turn_ctx = {**ctx_in, "active_intent": "none"}
        finalize_constraint_turn("ok", ctx_in, turn_ctx)
        stored = merge_session_context({}, turn_ctx)
        self.assertIn(CONSTRAINT_CTX_KEY, stored)
        # Turn 2: an unrelated message — the constraint is still in session context
        # and still filters, with no re-acknowledgment.
        ctx2 = dict(stored)
        ack2 = capture_constraints_for_turn(ctx2, "what's happening this week?")
        self.assertIsNone(ack2)
        kept, dropped = filter_events_by_constraints(
            [{"title": "Coffee morning", "starts_at": "2026-07-15T10:00:00"}],
            ctx2[CONSTRAINT_CTX_KEY],
        )
        self.assertEqual((len(kept), dropped), (0, 1))


class TestBrowseTurnAppliesConstraints(unittest.TestCase):
    _EVENTS = [
        {
            "title": "Morning meetup",
            "starts_at": "2026-07-15T10:00:00",  # Wednesday 10 AM
            "venue_name": "Cafe",
            "cohort_tags": [],
        },
        {
            "title": "Twilight picnic",
            "starts_at": "2026-07-15T18:30:00",  # Wednesday 6:30 PM
            "venue_name": "Park",
            "cohort_tags": [],
        },
    ]

    def _browse_ctx(self) -> dict:
        ctx: dict = {
            "activity_browse_active": True,
            "browse_draft": {"_asked": True},
            "phone_verified": True,
        }
        capture_constraints_for_turn(ctx, _QA_LINE)
        return ctx

    @patch("app.activity_browse._fetch_block_events")
    def test_10am_event_excluded_from_browse_results(self, fetch) -> None:
        from app.activity_browse import run_activity_browse_turn

        fetch.return_value = list(self._EVENTS)
        ctx = self._browse_ctx()
        reply = run_activity_browse_turn(
            user_message="anything",
            session_ctx=ctx,
            history=[],
            user_jwt="jwt",
            home_block_id="b1",
        )
        self.assertIn("Twilight picnic", reply)
        self.assertNotIn("Morning meetup", reply)
        preview_titles = [p["title"] for p in ctx.get("activity_previews") or []]
        self.assertNotIn("Morning meetup", preview_titles)

    @patch("app.activity_browse._fetch_block_events")
    def test_graceful_note_when_everything_filtered(self, fetch) -> None:
        from app.activity_browse import run_activity_browse_turn

        fetch.return_value = [dict(self._EVENTS[0])]  # only the 10 AM event
        ctx = self._browse_ctx()
        reply = run_activity_browse_turn(
            user_message="anything",
            session_ctx=ctx,
            history=[],
            user_jwt="jwt",
            home_block_id="b1",
        )
        self.assertNotIn("Morning meetup", reply)
        self.assertIn("outside", reply.lower())
        self.assertIn("listen", reply.lower())
        self.assertEqual(ctx.get("activity_previews"), [])


class TestActivitiesPreviewAppliesConstraints(unittest.TestCase):
    @patch("app.discovery_route.fetch_preview_events_on_block")
    def test_preview_surface_excludes_out_of_window_events(self, fetch) -> None:
        from app.discovery_route import _show_activities_preview

        fetch.return_value = [
            {"id": "e1", "title": "Morning meetup", "starts_at": "2026-07-15T10:00:00"},
            {"id": "e2", "title": "Twilight picnic", "starts_at": "2026-07-15T18:30:00"},
        ]
        ctx_base: dict = {}
        capture_constraints_for_turn(ctx_base, _QA_LINE)
        reply, ctx, _routing, _peers = _show_activities_preview(
            ctx_base=ctx_base,
            block_id="b1",
            block_label="Lake Nona",
            msg="what's happening",
            phone_verified=True,
        )
        self.assertIn("Twilight picnic", reply)
        self.assertNotIn("Morning meetup", reply)
        titles = [p["title"] for p in ctx.get("activity_previews") or []]
        self.assertEqual(titles, ["Twilight picnic"])

    @patch("app.discovery_route.fetch_preview_events_on_block")
    def test_preview_surface_graceful_note_when_all_filtered(self, fetch) -> None:
        from app.discovery_route import _show_activities_preview

        fetch.return_value = [
            {"id": "e1", "title": "Morning meetup", "starts_at": "2026-07-15T10:00:00"},
        ]
        ctx_base: dict = {}
        capture_constraints_for_turn(ctx_base, _QA_LINE)
        reply, ctx, _routing, _peers = _show_activities_preview(
            ctx_base=ctx_base,
            block_id="b1",
            block_label="Lake Nona",
            phone_verified=True,
        )
        self.assertNotIn("Morning meetup", reply)
        self.assertIn("outside", reply.lower())
        self.assertEqual(ctx.get("activity_previews"), [])


class TestMultiNeed(unittest.TestCase):
    _TRIPLE = "I need a double stroller, want a walk buddy, and want to host a bbq"

    def test_detects_three_needs(self) -> None:
        needs = detect_multi_needs(self._TRIPLE)
        self.assertEqual(len(needs), 3)
        self.assertEqual({n["kind"] for n in needs}, {"need_item", "find_meet", "host"})

    def test_single_need_is_not_multi(self) -> None:
        self.assertEqual(detect_multi_needs("I want a walk buddy"), [])
        self.assertEqual(detect_multi_needs("what's happening this weekend?"), [])

    def test_capture_stores_three_needs_and_ack_references_all(self) -> None:
        ctx: dict = {}
        ack = capture_constraints_for_turn(ctx, self._TRIPLE)
        stack = ctx[MULTI_NEED_STACK_KEY]
        self.assertEqual(len(stack), 3)
        # The find-meet ask proceeds (that's the one routing handles); the rest park.
        self.assertEqual(len(ctx[PARKED_NEEDS_KEY]), 2)
        active = [n for n in stack if n["status"] == "active"]
        self.assertEqual(active[0]["kind"], "find_meet")
        # The reply line references all three needs, so nothing vanishes silently.
        low = ack.lower()
        self.assertIn("walk buddy", low)
        self.assertIn("stroller", low)
        self.assertIn("bbq", low)
        self.assertIn("holding", low)


if __name__ == "__main__":
    unittest.main()
