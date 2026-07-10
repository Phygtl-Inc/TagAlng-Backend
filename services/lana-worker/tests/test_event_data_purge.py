"""is_test fence + dedupe guard (the 2026-07-08 QA junk-feed findings).

Pure-unit coverage of the worker side of the purge migration
(20260811120000_event_data_purge_is_test.sql):
  * QA-account detection from the host's email plus-tag,
  * build_create_event_fields stamping is_test for QA hosts (and only QA hosts),
  * the duplicate_event (unique-index) conflict surfacing as a friendly
    "you already have that meet" reply instead of a 502,
  * every worker-side events read excluding is_test rows.
The SQL itself can't run here — it is reviewed for idempotency instead.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from app.event_publish import (
    _is_duplicate_event_error,
    build_create_event_fields,
    is_qa_email,
    publish_event,
)
from app.lana_unified_pipeline import _publish_failure_reply
from app.models import EventDraft


class TestQaEmailDetection(unittest.TestCase):
    def test_qa_plus_tags_match(self) -> None:
        # The account from the QA findings, plus the general pattern.
        self.assertTrue(is_qa_email("t+lanaqa1@phygtl.com"))
        self.assertTrue(is_qa_email("t+qa2@phygtl.com"))
        self.assertTrue(is_qa_email("someone+qa@example.com"))
        self.assertTrue(is_qa_email("  T+LanaQA1@Phygtl.com  "))  # case/space-proof

    def test_real_members_never_match(self) -> None:
        self.assertFalse(is_qa_email("t@phygtl.com"))
        self.assertFalse(is_qa_email("mom+park@example.com"))  # plus-tag without qa
        self.assertFalse(is_qa_email("aqua@example.com"))  # 'qa' but no plus-tag
        self.assertFalse(is_qa_email("tqa@example.com"))
        self.assertFalse(is_qa_email(""))
        self.assertFalse(is_qa_email(None))


class TestHostIsQaAccount(unittest.TestCase):
    def _client_returning(self, rows):
        class _Q:
            def select(self, *a, **k):
                return self

            def eq(self, *a, **k):
                return self

            def limit(self, *a, **k):
                return self

            def execute(self):
                return SimpleNamespace(data=rows)

        class _SB:
            def table(self, name):
                return _Q()

        return _SB()

    def test_qa_host_detected(self) -> None:
        from app.event_publish import _host_is_qa_account

        with patch(
            "app.event_publish.service_client",
            return_value=self._client_returning([{"email": "t+lanaqa1@phygtl.com"}]),
        ):
            self.assertTrue(_host_is_qa_account("uid-1"))

    def test_member_host_not_flagged(self) -> None:
        from app.event_publish import _host_is_qa_account

        with patch(
            "app.event_publish.service_client",
            return_value=self._client_returning([{"email": "maria@example.com"}]),
        ):
            self.assertFalse(_host_is_qa_account("uid-2"))

    def test_lookup_failure_is_not_qa(self) -> None:
        # Fence is best-effort: a broken lookup must never hide a real member's event.
        from app.event_publish import _host_is_qa_account

        with patch("app.event_publish.service_client", side_effect=RuntimeError("boom")):
            self.assertFalse(_host_is_qa_account("uid-3"))
        self.assertFalse(_host_is_qa_account(""))


class TestBuildCreateEventFieldsIsTest(unittest.TestCase):
    def _draft(self) -> EventDraft:
        return EventDraft(
            title="Pre-K Playground Meetup",
            venue_name="Laureate Park Zipline Playground",
            starts_at="2026-07-10T09:30:00",
        )

    @patch("app.event_publish._valid_purpose_ids", return_value=set())
    @patch("app.event_publish.resolve_event_location", return_value=(28.37, -81.25, "block-a"))
    def test_qa_host_stamps_is_test(self, _loc, _purposes) -> None:
        with patch("app.event_publish._host_is_qa_account", return_value=True):
            fields = build_create_event_fields("qa-uid", self._draft())
        self.assertIs(fields.get("is_test"), True)

    @patch("app.event_publish._valid_purpose_ids", return_value=set())
    @patch("app.event_publish.resolve_event_location", return_value=(28.37, -81.25, "block-a"))
    def test_member_host_omits_is_test(self, _loc, _purposes) -> None:
        # Absent (not False) so the RPC's coalesce(default false) owns the value.
        with patch("app.event_publish._host_is_qa_account", return_value=False):
            fields = build_create_event_fields("member-uid", self._draft())
        self.assertNotIn("is_test", fields)


class TestDuplicateEventConflict(unittest.TestCase):
    def test_duplicate_error_detection(self) -> None:
        self.assertTrue(_is_duplicate_event_error("P0001: duplicate_event"))
        self.assertTrue(_is_duplicate_event_error('{"code":"23505","message":"..."}'))
        self.assertTrue(
            _is_duplicate_event_error(
                'duplicate key value violates unique constraint '
                '"events_host_title_starts_live_uniq"'
            )
        )
        self.assertFalse(_is_duplicate_event_error("phone_not_verified"))
        self.assertFalse(_is_duplicate_event_error("location_required"))
        self.assertFalse(_is_duplicate_event_error(""))

    def test_publish_event_maps_duplicate_to_409(self) -> None:
        resp = SimpleNamespace(status_code=400, text='{"message":"duplicate_event"}')

        class _Client:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, *a, **k):
                return resp

        with (
            patch("app.event_publish.SUPABASE_URL", "http://sb.local"),
            patch("app.event_publish.SUPABASE_ANON_KEY", "anon"),
            patch("app.event_publish.build_create_event_fields", return_value={"title": "x"}),
            patch("app.event_publish.httpx.Client", _Client),
        ):
            with self.assertRaises(HTTPException) as ctx:
                publish_event("uid", "jwt", EventDraft(title="x"))
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail, "duplicate_event")

    def test_publish_failure_reply_is_friendly(self) -> None:
        reply = _publish_failure_reply("duplicate_event", "Brazilian Coffee Morning")
        self.assertIn("already have that meet", reply)
        self.assertIn("edit", reply)
        # Duplicate must NOT read as the generic "try again" snag (which would make the
        # user re-post and hit the same conflict forever).
        self.assertNotIn("snag", reply)

    def test_other_failures_keep_existing_replies(self) -> None:
        self.assertIn("verified", _publish_failure_reply("phone_not_verified", "T"))
        self.assertIn("map", _publish_failure_reply("location_required", "T"))


class TestPreviewFeedExcludesTestEvents(unittest.TestCase):
    def test_fetch_preview_events_on_block_fences_is_test(self) -> None:
        eq_calls: list[tuple[str, object]] = []

        class _Q:
            def select(self, *a, **k):
                return self

            def eq(self, key, value):
                eq_calls.append((key, value))
                return self

            def gte(self, *a, **k):
                return self

            def order(self, *a, **k):
                return self

            def limit(self, *a, **k):
                return self

            def execute(self):
                return SimpleNamespace(data=[])

        class _SB:
            def table(self, name):
                assert name == "events"
                return _Q()

        from app.discovery_route import fetch_preview_events_on_block

        with patch("app.discovery_route.service_client", return_value=_SB()):
            rows = fetch_preview_events_on_block("block-a")

        self.assertEqual(rows, [])
        self.assertIn(("is_test", False), eq_calls)
        self.assertIn(("status", "open"), eq_calls)


if __name__ == "__main__":
    unittest.main()
