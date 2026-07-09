"""Event-creation guards (QA 2026-07-08): venue-vs-block distance, kid quiet-hours,
and ZIP validity — all phrased as warm clarifying questions, never hard rejections,
with one confirmation proceeding (remembered — no loop).

These run at the confirm-stage publish gate (`_host_publish_gate`), which BOTH host
paths funnel through: the conversational flow and the setup/wizard carousel (the
carousel stamps the draft via /event-setup, then the FE's "Looks good" advances the
same confirm stage). Pure-function coverage in the test_venue_resolvable style —
no LLM / DB / network (block centroid is mocked)."""

import os
import unittest
from unittest import mock

from app.event_guards import (
    GUARD_FAR_VENUE,
    GUARD_KID_HOURS,
    classify_guard_answer,
    far_venue_km,
    haversine_km,
    kid_quiet_hours_start,
    pending_event_guard,
)
from app.event_location import _normalize_zip5, is_valid_zip5
from app.lana_unified_pipeline import _host_publish_gate

# Lake Nona pilot centroid vs the QA case's New York geocode (≈ 1,500 km apart).
_LAKE_NONA = (28.3647, -81.2568)
_NEW_YORK = (40.7484, -73.9857)

_ET = {"EVENT_DEFAULT_TZ": "America/New_York"}


def _patch_centroid(value=_LAKE_NONA):
    return mock.patch("app.event_guards.host_block_centroid", return_value=value)


class TestFarVenueGuard(unittest.TestCase):
    def test_haversine_sane(self) -> None:
        self.assertAlmostEqual(haversine_km(*_LAKE_NONA, *_LAKE_NONA), 0.0)
        km = haversine_km(*_LAKE_NONA, *_NEW_YORK)
        self.assertGreater(km, 1000)
        self.assertLess(km, 2500)

    def test_ny_venue_on_lake_nona_block_triggers_question(self) -> None:
        # The prod QA case: a playdate venue geocoded to New York (a real, correctly
        # resolved place — just never cross-checked against the block).
        draft = {
            "title": "Playdate at the playground",
            "venue_name": "Greeley Square Park",
            "place_id": "ChIJOwg_06VPwokRYv534QaPC8g",
            "venue_lat": _NEW_YORK[0],
            "venue_lng": _NEW_YORK[1],
        }
        with _patch_centroid():
            self.assertIsNotNone(far_venue_km(draft, "user-1"))
            guard = pending_event_guard(draft, "user-1")
        self.assertEqual(guard["id"], GUARD_FAR_VENUE)
        # A question, not a rejection — names the venue, the distance, and asks.
        self.assertIn("Greeley Square Park", guard["question"])
        self.assertIn("km from your block", guard["question"])
        self.assertIn("did you mean somewhere nearby", guard["question"])
        self.assertTrue(guard["options"])

    def test_nearby_venue_is_silent(self) -> None:
        draft = {"venue_name": "Laureate Park", "venue_lat": 28.37, "venue_lng": -81.26}
        with _patch_centroid():
            self.assertIsNone(far_venue_km(draft, "user-1"))
            self.assertIsNone(pending_event_guard(draft, "user-1"))

    def test_unpinned_venue_needs_no_check(self) -> None:
        # No exact pin → publish resolves it biased to the block by construction.
        with _patch_centroid() as centroid:
            self.assertIsNone(far_venue_km({"venue_name": "my place"}, "user-1"))
            centroid.assert_not_called()

    def test_no_centroid_stays_silent(self) -> None:
        # Best-effort: if the block can't be placed, don't block the publish.
        draft = {"venue_lat": _NEW_YORK[0], "venue_lng": _NEW_YORK[1]}
        with _patch_centroid(None):
            self.assertIsNone(far_venue_km(draft, "user-1"))

    def test_confirmed_once_never_reasks(self) -> None:
        draft = {"venue_lat": _NEW_YORK[0], "venue_lng": _NEW_YORK[1]}
        with _patch_centroid():
            self.assertIsNone(
                pending_event_guard(draft, "user-1", confirmed={GUARD_FAR_VENUE: True})
            )


class TestKidQuietHoursGuard(unittest.TestCase):
    def test_2300_kids_party_asks(self) -> None:
        draft = {"title": "6th birthday kids party", "starts_at": "2026-08-12T23:00:00"}
        with mock.patch.dict(os.environ, _ET):
            self.assertIsNotNone(kid_quiet_hours_start(draft))
            guard = pending_event_guard(draft, "user-1")
        self.assertEqual(guard["id"], GUARD_KID_HOURS)
        self.assertIn("11 PM", guard["question"])
        self.assertIn("noon", guard["question"])

    def test_qa_case_utc_start_is_midnight_local(self) -> None:
        # The prod QA case: 2026-08-12T04:00Z = midnight Eastern.
        draft = {
            "title": "6th birthday party",
            "cohort_tags": ["kids"],
            "starts_at": "2026-08-12T04:00:00Z",
        }
        with mock.patch.dict(os.environ, _ET):
            local = kid_quiet_hours_start(draft)
        self.assertIsNotNone(local)
        self.assertEqual(local.hour, 0)

    def test_noon_kids_party_is_silent(self) -> None:
        draft = {"title": "Kids playdate", "starts_at": "2026-08-12T12:00:00"}
        with mock.patch.dict(os.environ, _ET):
            self.assertIsNone(kid_quiet_hours_start(draft))
            self.assertIsNone(pending_event_guard(draft, "user-1"))

    def test_late_adult_event_is_silent(self) -> None:
        # "moms"/"parents" wording is deliberately NOT kid-flagged — a moms' wine
        # night at 9:30 PM is a normal meet.
        draft = {
            "title": "Moms wine night",
            "cohort_tags": ["parents"],
            "starts_at": "2026-08-12T21:30:00",
        }
        with mock.patch.dict(os.environ, _ET):
            self.assertIsNone(kid_quiet_hours_start(draft))

    def test_kid_tag_detection_via_cohort_tags(self) -> None:
        draft = {"title": "Morning meetup", "cohort_tags": ["playdate"],
                 "starts_at": "2026-08-12T22:00:00"}
        with mock.patch.dict(os.environ, _ET):
            self.assertIsNotNone(kid_quiet_hours_start(draft))

    def test_confirmed_once_never_reasks(self) -> None:
        draft = {"title": "kids campout", "starts_at": "2026-08-12T23:00:00"}
        with mock.patch.dict(os.environ, _ET):
            self.assertIsNone(
                pending_event_guard(draft, "user-1", confirmed={GUARD_KID_HOURS: True})
            )


class TestGuardAnswerReading(unittest.TestCase):
    def test_confirm_answers(self) -> None:
        for msg in ["Yes, that's the spot", "yep", "Keep it at 11 PM", "that's right"]:
            self.assertEqual(classify_guard_answer(msg), "confirm", msg)

    def test_change_answers(self) -> None:
        for msg in ["Pick somewhere nearby", "Make it noon", "no, I meant somewhere else"]:
            self.assertEqual(classify_guard_answer(msg), "change", msg)

    def test_unclear_answers_hold(self) -> None:
        for msg in ["", "hmm", "what does that mean?"]:
            self.assertIsNone(classify_guard_answer(msg), msg)


class TestZipValidity(unittest.TestCase):
    def test_valid_zips(self) -> None:
        self.assertTrue(is_valid_zip5("32827"))
        self.assertTrue(is_valid_zip5("32827-1234"))  # ZIP+4 keeps its ZIP5

    def test_bogus_and_malformed_rejected(self) -> None:
        for z in ["99999", "00000", "123", "", None, "abc"]:
            self.assertFalse(is_valid_zip5(z), repr(z))
        self.assertIsNone(_normalize_zip5("99999"))

    def test_extract_zip_rejects_bogus(self) -> None:
        from app.discovery_route import extract_zip, invalid_zip_hint

        self.assertEqual(extract_zip("I'm in 32827"), "32827")
        self.assertIsNone(extract_zip("99999"))
        self.assertIsNone(extract_zip("00000"))
        # …and the host gets a friendly explanation, not a silent re-prompt.
        hint = invalid_zip_hint("99999")
        self.assertIsNotNone(hint)
        self.assertIn("isn't a real US ZIP", hint)
        self.assertIsNone(invalid_zip_hint("32827"))


class TestPublishGateFlow(unittest.TestCase):
    """The confirm-stage gate both host paths share: question → one confirmation →
    publish proceeds (state remembered, no loop)."""

    def _far_draft(self) -> dict:
        return {
            "title": "Playdate",
            "venue_name": "Greeley Square Park",
            "venue_lat": _NEW_YORK[0],
            "venue_lng": _NEW_YORK[1],
            "starts_at": "2026-08-12T10:00:00",
        }

    def test_drop_with_far_venue_asks_and_blocks_publish(self) -> None:
        ed, turn_ctx = self._far_draft(), {}
        with _patch_centroid():
            reply, proceed = _host_publish_gate(
                user_message="Drop the meet up", ed=ed, wd="2026-08-12",
                user_id="u1", session_ctx={}, turn_ctx=turn_ctx,
                title="Playdate", wants_drop=True,
            )
        self.assertFalse(proceed)
        self.assertIn("km from your block", reply)
        self.assertEqual(turn_ctx["event_guard_pending"], GUARD_FAR_VENUE)
        self.assertEqual(turn_ctx["host_stage"], "confirm")
        self.assertTrue(turn_ctx["host_aside"])  # the question must be visible
        self.assertIn("Yes, that's the spot", ed["suggestions"])

    def test_confirmation_proceeds_and_is_remembered(self) -> None:
        ed, turn_ctx = self._far_draft(), {}
        session_ctx = {"event_guard_pending": GUARD_FAR_VENUE, "event_guards_confirmed": {}}
        with _patch_centroid():
            reply, proceed = _host_publish_gate(
                user_message="Yes, that's the spot", ed=ed, wd="2026-08-12",
                user_id="u1", session_ctx=session_ctx, turn_ctx=turn_ctx,
                title="Playdate", wants_drop=False,
            )
        self.assertIsNone(reply)
        self.assertTrue(proceed)  # one confirmation proceeds straight to publish
        self.assertTrue(turn_ctx["event_guards_confirmed"][GUARD_FAR_VENUE])
        self.assertIsNone(turn_ctx["event_guard_pending"])

    def test_confirmed_guard_never_loops_on_next_drop(self) -> None:
        ed, turn_ctx = self._far_draft(), {}
        session_ctx = {"event_guards_confirmed": {GUARD_FAR_VENUE: True}}
        with _patch_centroid():
            reply, proceed = _host_publish_gate(
                user_message="Drop the meet up", ed=ed, wd="2026-08-12",
                user_id="u1", session_ctx=session_ctx, turn_ctx=turn_ctx,
                title="Playdate", wants_drop=True,
            )
        self.assertIsNone(reply)
        self.assertTrue(proceed)

    def test_bare_drop_tap_counts_as_confirmation(self) -> None:
        # Tapping "Drop the meet up" again after the question is the host insisting —
        # that's the one confirmation; don't trap them.
        ed, turn_ctx = self._far_draft(), {}
        session_ctx = {"event_guard_pending": GUARD_FAR_VENUE}
        with _patch_centroid():
            reply, proceed = _host_publish_gate(
                user_message="Drop the meet up", ed=ed, wd="2026-08-12",
                user_id="u1", session_ctx=session_ctx, turn_ctx=turn_ctx,
                title="Playdate", wants_drop=True,
            )
        self.assertIsNone(reply)
        self.assertTrue(proceed)

    def test_nearby_change_reopens_where_step(self) -> None:
        ed, turn_ctx = self._far_draft(), {}
        session_ctx = {"event_guard_pending": GUARD_FAR_VENUE, "event_venue": {"name": "x"}}
        with _patch_centroid():
            reply, proceed = _host_publish_gate(
                user_message="Pick somewhere nearby", ed=ed, wd="2026-08-12",
                user_id="u1", session_ctx=session_ctx, turn_ctx=turn_ctx,
                title="Playdate", wants_drop=False,
            )
        self.assertFalse(proceed)
        self.assertIn("where should it be", reply)
        for k in ("venue_name", "venue_lat", "venue_lng", "place_id", "venue_address"):
            self.assertNotIn(k, ed)
        self.assertFalse(turn_ctx["event_place_asked"])
        self.assertIsNone(session_ctx["event_venue"])
        self.assertEqual(turn_ctx["host_stage"], "review")

    def test_2300_kids_party_asks_then_noon_fix_applies(self) -> None:
        ed = {"title": "6th birthday kids party", "starts_at": "2026-08-12T23:00:00",
              "duration_minutes": 90}
        turn_ctx: dict = {}
        with mock.patch.dict(os.environ, _ET):
            reply, proceed = _host_publish_gate(
                user_message="Drop the meet up", ed=ed, wd="2026-08-12",
                user_id="u1", session_ctx={}, turn_ctx=turn_ctx,
                title="6th birthday kids party", wants_drop=True,
            )
            self.assertFalse(proceed)
            self.assertIn("noon", reply)
            self.assertEqual(turn_ctx["event_guard_pending"], GUARD_KID_HOURS)
            # "Did you mean noon?" → yes: the fix is applied, then one drop publishes.
            turn2: dict = {}
            reply2, proceed2 = _host_publish_gate(
                user_message="Make it noon", ed=ed, wd="2026-08-12",
                user_id="u1",
                session_ctx={"event_guard_pending": GUARD_KID_HOURS},
                turn_ctx=turn2, title="6th birthday kids party", wants_drop=False,
            )
            self.assertFalse(proceed2)
            self.assertIn("noon", reply2)
            self.assertEqual(ed["starts_at"], "2026-08-12T12:00:00")
            self.assertEqual(ed["ends_at"], "2026-08-12T13:30:00")
            self.assertEqual(turn2["event_when_time"], "12:00")
            # The corrected draft passes clean on the next drop — no re-ask.
            turn3: dict = {}
            reply3, proceed3 = _host_publish_gate(
                user_message="Drop the meet up", ed=ed, wd="2026-08-12",
                user_id="u1", session_ctx=dict(turn2), turn_ctx=turn3,
                title="6th birthday kids party", wants_drop=True,
            )
        self.assertIsNone(reply3)
        self.assertTrue(proceed3)

    def test_noon_kids_party_publishes_without_question(self) -> None:
        ed = {"title": "Kids playdate", "starts_at": "2026-08-12T12:00:00"}
        with mock.patch.dict(os.environ, _ET):
            reply, proceed = _host_publish_gate(
                user_message="Drop the meet up", ed=ed, wd="2026-08-12",
                user_id="u1", session_ctx={}, turn_ctx={},
                title="Kids playdate", wants_drop=True,
            )
        self.assertIsNone(reply)
        self.assertTrue(proceed)

    def test_non_drop_turn_passes_through(self) -> None:
        reply, proceed = _host_publish_gate(
            user_message="what does auto approve mean?", ed={}, wd=None,
            user_id="u1", session_ctx={}, turn_ctx={},
            title="Playdate", wants_drop=False,
        )
        self.assertIsNone(reply)
        self.assertFalse(proceed)


if __name__ == "__main__":
    unittest.main()
