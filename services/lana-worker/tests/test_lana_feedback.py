"""Lana feedback (👍/👎) unit tests — ownership checks, snapshotting, toggle semantics.

The Supabase client is faked (no network), same pattern as test_rapport: selects return
canned rows per table (filters are no-ops), writes are recorded for assertion.
"""

import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app import feedback


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, table, store):
        self.table = table
        self.store = store
        self._op = None
        self._payload = None

    def select(self, *a, **k):
        self._op = "select"
        return self

    def insert(self, row):
        self._op = "insert"
        self._payload = row
        return self

    def update(self, row):
        self._op = "update"
        self._payload = row
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, col, val):
        self.store.setdefault("filters", []).append((self.table, col, val))
        return self

    def limit(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def execute(self):
        if self._op == "select":
            return _Result(list(self.store["selects"].get(self.table, [])))
        if self._op == "insert":
            self.store["inserts"].append((self.table, self._payload))
            return _Result([self._payload])
        if self._op == "update":
            self.store["updates"].append((self.table, self._payload))
            return _Result([])
        if self._op == "delete":
            self.store["deletes"].append(self.table)
            return _Result([])
        return _Result([])


class _Supabase:
    def __init__(self, store):
        self.store = store

    def table(self, name):
        return _Query(name, self.store)

    def rpc(self, name, params):
        q = _Query(f"rpc:{name}", self.store)
        q._op = "select"
        return q


def _store(
    messages=None, sessions=None, gaps=None, existing=None, recs=None,
    events=None, places=None, members=None, fit_lines=None,
):
    return {
        "selects": {
            "lana_messages": messages or [],
            "lana_sessions": sessions or [],
            "rapport_gaps": gaps or [],
            "peer_rec_lines": recs or [],
            "events": events or [],
            "event_fit_lines": fit_lines or [],
            "places": places or [],
            "rpc:visible_place_members": members or [],
            "lana_feedback": existing or [],
        },
        "inserts": [],
        "updates": [],
        "deletes": [],
    }


_MSG = {"id": "m1", "session_id": "s1", "role": "assistant", "content": "Try the park!"}
_SES = {"id": "s1", "user_id": "u1"}
_GAP = {"gap_row_id": "g1", "user_id": "u1", "gap_id": "family.pets", "question": "Any pets?"}
_REC = {
    "id": "r1",
    "user_id": "u1",
    "peer_user_id": "p1",
    "line": "You're both early risers who'd rather run the lake trail.",
}

_EVENT = {"id": "e1", "title": "Saturday run club", "cohort_tags": ["runners", "early_risers"]}
_PLACE = {"id": "pl1", "name": "Lake Nona Run Club"}


class TestRecordFeedback(unittest.TestCase):
    def _run(self, store, **kwargs):
        with patch.object(feedback, "service_client", return_value=_Supabase(store)), patch(
            "app.context.cohort_tag_labels_for",
            side_effect=lambda tags: [
                {"runners": "Runners", "early_risers": "Early risers"}.get(t, t) for t in tags
            ],
        ):
            return feedback.record_feedback("u1", **kwargs)

    def test_up_on_assistant_message_inserts_with_db_snapshot(self):
        store = _store(messages=[_MSG], sessions=[_SES])
        out = self._run(store, rating="up", message_id="m1")
        self.assertEqual(out, {"rating": "up", "target_kind": "message"})
        (table, row), = store["inserts"]
        self.assertEqual(table, "lana_feedback")
        self.assertEqual(row["rating"], "up")
        self.assertEqual(row["content_snapshot"], "Try the park!")
        self.assertEqual(row["context"]["session_id"], "s1")

    def test_message_owned_by_someone_else_is_404(self):
        store = _store(messages=[_MSG], sessions=[{"id": "s1", "user_id": "other"}])
        with self.assertRaises(HTTPException) as ctx:
            self._run(store, rating="up", message_id="m1")
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(store["inserts"], [])

    def test_user_role_message_is_400(self):
        store = _store(messages=[{**_MSG, "role": "user"}], sessions=[_SES])
        with self.assertRaises(HTTPException) as ctx:
            self._run(store, rating="down", message_id="m1")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_down_on_rapport_question_snapshots_question(self):
        store = _store(gaps=[_GAP])
        out = self._run(store, rating="down", gap_row_id="g1")
        self.assertEqual(out, {"rating": "down", "target_kind": "rapport_question"})
        (_, row), = store["inserts"]
        self.assertEqual(row["content_snapshot"], "Any pets?")
        self.assertEqual(row["context"]["gap_id"], "family.pets")

    def test_rapport_question_of_other_user_is_404(self):
        store = _store(gaps=[{**_GAP, "user_id": "other"}])
        with self.assertRaises(HTTPException) as ctx:
            self._run(store, rating="up", gap_row_id="g1")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_thumb_on_fellows_rec_line_snapshots_the_authored_line(self):
        # The third rateable surface ("Was this rec useful?"). The rated text is the
        # STORED line, not anything the client sent, and the pairing rides in context so
        # the team can read the 👎 without joining a row that may be re-authored later.
        store = _store(recs=[_REC])
        out = self._run(store, rating="down", rec_id="r1", context={"surface": "fellows"})
        self.assertEqual(out, {"rating": "down", "target_kind": "peer_rec"})
        (_, row), = store["inserts"]
        self.assertEqual(row["rec_id"], "r1")
        self.assertEqual(row["content_snapshot"], _REC["line"])
        self.assertEqual(row["context"]["peer_user_id"], "p1")
        self.assertEqual(row["context"]["surface"], "fellows")

    def test_rec_line_authored_for_someone_else_is_404(self):
        # peer_rec_lines rows are per-viewer, so ownership IS the disclosure check —
        # a guessed rec_id must not echo another user's line back in the snapshot.
        store = _store(recs=[{**_REC, "user_id": "other"}])
        with self.assertRaises(HTTPException) as ctx:
            self._run(store, rating="up", rec_id="r1")
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(store["inserts"], [])

    def test_two_targets_at_once_is_400(self):
        store = _store(recs=[_REC], gaps=[_GAP])
        with self.assertRaises(HTTPException) as ctx:
            self._run(store, rating="up", rec_id="r1", gap_row_id="g1")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_down_with_comment_stores_trimmed_comment(self):
        store = _store(gaps=[_GAP])
        self._run(store, rating="down", gap_row_id="g1", comment="  too personal  ")
        (_, row), = store["inserts"]
        self.assertEqual(row["comment"], "too personal")

    def test_rating_without_comment_stores_null_comment(self):
        store = _store(gaps=[_GAP])
        self._run(store, rating="down", gap_row_id="g1")
        (_, row), = store["inserts"]
        self.assertIsNone(row["comment"])

    def test_rerate_without_comment_clears_previous_comment(self):
        # Flip 👎(+comment) → 👍: the stale explanation must not ride on the new thumb.
        store = _store(
            gaps=[_GAP],
            existing=[{"id": "f1", "rating": "down", "comment": "too personal"}],
        )
        self._run(store, rating="up", gap_row_id="g1")
        (_, row), = store["updates"]
        self.assertIsNone(row["comment"])

    def test_comment_followup_updates_existing_row(self):
        # The FE posts 👎 first, then the free-text as a second call on the same rating.
        store = _store(
            gaps=[_GAP],
            existing=[{"id": "f1", "rating": "down", "comment": None}],
        )
        out = self._run(store, rating="down", gap_row_id="g1", comment="asks this too often")
        self.assertEqual(out["rating"], "down")
        self.assertEqual(store["inserts"], [])
        (_, row), = store["updates"]
        self.assertEqual(row["comment"], "asks this too often")

    def test_overlong_comment_is_capped(self):
        store = _store(gaps=[_GAP])
        self._run(store, rating="down", gap_row_id="g1", comment="x" * 3000)
        (_, row), = store["inserts"]
        self.assertEqual(len(row["comment"]), 2000)

    def test_second_rating_updates_in_place(self):
        store = _store(
            messages=[_MSG],
            sessions=[_SES],
            existing=[{"id": "f1", "rating": "up"}],
        )
        out = self._run(store, rating="down", message_id="m1")
        self.assertEqual(out["rating"], "down")
        self.assertEqual(store["inserts"], [])
        (table, row), = store["updates"]
        self.assertEqual(table, "lana_feedback")
        self.assertEqual(row["rating"], "down")

    def test_clear_deletes_existing_row(self):
        store = _store(
            messages=[_MSG],
            sessions=[_SES],
            existing=[{"id": "f1", "rating": "up"}],
        )
        out = self._run(store, rating="clear", message_id="m1")
        self.assertEqual(out, {"rating": None, "target_kind": "message"})
        self.assertEqual(store["deletes"], ["lana_feedback"])
        self.assertEqual(store["inserts"], [])

    def test_clear_without_existing_row_is_a_noop(self):
        store = _store(messages=[_MSG], sessions=[_SES])
        out = self._run(store, rating="clear", message_id="m1")
        self.assertEqual(out["rating"], None)
        self.assertEqual(store["deletes"], [])

    def test_exactly_one_target_required(self):
        for kwargs in (
            {},
            {"message_id": "m1", "gap_row_id": "g1"},
        ):
            with self.assertRaises(HTTPException) as ctx:
                self._run(_store(), rating="up", **kwargs)
            self.assertEqual(ctx.exception.status_code, 400)

    # ── event target (§51, the map's meet card) ──────────────────────────────

    def test_thumb_on_meet_snapshots_title_and_cohort_tags(self):
        store = _store(events=[_EVENT])
        out = self._run(store, rating="up", event_id="e1", context={"surface": "map_peek_meet"})
        self.assertEqual(out, {"rating": "up", "target_kind": "event"})
        (table, row), = store["inserts"]
        self.assertEqual(table, "lana_feedback")
        self.assertEqual(row["target_kind"], "event")
        self.assertEqual(row["event_id"], "e1")
        self.assertIsNone(row["place_id"])
        # Tag LABELS (cohorts.label), never the raw taxonomy ids.
        self.assertEqual(row["content_snapshot"], "Saturday run club · Runners, Early risers")
        self.assertEqual(row["context"]["surface"], "map_peek_meet")
        self.assertEqual(row["context"]["event_title"], "Saturday run club")

    def test_thumb_on_meet_snapshots_the_viewers_fit_line_when_one_was_shown(self):
        # §51 → §50(b): the line the card showed this viewer, read from event_fit_lines.
        line = "You're into early runs, and this one starts at sunrise by the lake."
        store = _store(events=[_EVENT], fit_lines=[{"line": line}])
        self._run(store, rating="up", event_id="e1")
        (_, row), = store["inserts"]
        self.assertEqual(row["content_snapshot"], line)
        self.assertEqual(row["context"]["event_title"], "Saturday run club")
        # Read for THIS viewer and THIS meet only.
        self.assertIn(("event_fit_lines", "user_id", "u1"), store["filters"])
        self.assertIn(("event_fit_lines", "event_id", "e1"), store["filters"])

    def test_meet_without_cohort_tags_snapshots_title_alone(self):
        store = _store(events=[{**_EVENT, "cohort_tags": []}])
        self._run(store, rating="down", event_id="e1")
        (_, row), = store["inserts"]
        self.assertEqual(row["content_snapshot"], "Saturday run club")

    def test_unknown_meet_is_404(self):
        store = _store()
        with self.assertRaises(HTTPException) as ctx:
            self._run(store, rating="up", event_id="nope")
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(ctx.exception.detail, "event_not_found")
        self.assertEqual(store["inserts"], [])

    def test_meet_opposite_thumb_flips_and_clear_deletes(self):
        store = _store(events=[_EVENT], existing=[{"id": "f1", "rating": "up"}])
        self.assertEqual(self._run(store, rating="down", event_id="e1")["rating"], "down")
        # The existing row is found by (user, event_id) — the column the unique index keys.
        self.assertIn(("lana_feedback", "event_id", "e1"), store["filters"])
        self.assertEqual(store["inserts"], [])
        (_, row), = store["updates"]
        self.assertEqual(row["rating"], "down")
        out = self._run(store, rating="clear", event_id="e1")
        self.assertEqual(out, {"rating": None, "target_kind": "event"})
        self.assertEqual(store["deletes"], ["lana_feedback"])

    # ── place target (§55, the map's community card) ─────────────────────────

    def test_thumb_on_community_snapshots_discovery_status_line(self):
        # Two visible members, neither the caller → the discovery row read "2 people".
        store = _store(places=[_PLACE], members=[{"user_id": "a"}, {"user_id": "b"}])
        out = self._run(store, rating="up", place_id="pl1", context={"surface": "map_peek_community"})
        self.assertEqual(out, {"rating": "up", "target_kind": "place"})
        (_, row), = store["inserts"]
        self.assertEqual(row["target_kind"], "place")
        self.assertEqual(row["place_id"], "pl1")
        self.assertIsNone(row["event_id"])
        self.assertEqual(row["content_snapshot"], "2 people")
        # The status line alone doesn't say which community — the name rides in context.
        self.assertEqual(row["context"]["place_name"], "Lake Nona Run Club")

    def test_community_status_line_counts_the_caller_as_you(self):
        store = _store(places=[_PLACE], members=[{"user_id": "u1"}, {"user_id": "b"}])
        self._run(store, rating="down", place_id="pl1")
        (_, row), = store["inserts"]
        self.assertEqual(row["content_snapshot"], "You + 1 others")
        self.assertIn(("lana_feedback", "place_id", "pl1"), store["filters"])
        self.assertIn(("rpc:visible_place_members", "place_ref", "pl1"), store["filters"])

    def test_community_with_no_visible_members_snapshots_its_name(self):
        store = _store(places=[_PLACE], members=[])
        self._run(store, rating="up", place_id="pl1")
        (_, row), = store["inserts"]
        self.assertEqual(row["content_snapshot"], "Lake Nona Run Club")

    def test_unknown_place_is_404(self):
        with self.assertRaises(HTTPException) as ctx:
            self._run(_store(), rating="up", place_id="nope")
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(ctx.exception.detail, "place_not_found")

    def test_place_and_event_together_is_400(self):
        store = _store(events=[_EVENT], places=[_PLACE])
        with self.assertRaises(HTTPException) as ctx:
            self._run(store, rating="up", event_id="e1", place_id="pl1")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_invalid_rating_is_400(self):
        with self.assertRaises(HTTPException) as ctx:
            self._run(_store(messages=[_MSG], sessions=[_SES]), rating="meh", message_id="m1")
        self.assertEqual(ctx.exception.status_code, 400)


class TestFeedbackRoute(unittest.TestCase):
    """POST /lana/feedback carries the two new ids through to the writer — the body the
    PWA sends beside recordRecFeedback: {rating, event_id|place_id, surface}."""

    def _post(self, store, body):
        from types import SimpleNamespace

        from fastapi.testclient import TestClient

        from app import main

        with patch.object(feedback, "service_client", return_value=_Supabase(store)), patch.object(
            main, "verify_auth", return_value=SimpleNamespace(user_id="u1")
        ), patch.object(main, "amplitude_track") as track:
            res = TestClient(main.app).post("/lana/feedback", json=body)
        return res, track

    # The route validates ids as uuids (every lana_feedback target column is one), so
    # the route tests use real-shaped ids; the writer tests above keep their short ones.
    PL = "6f1d1c3e-2a4b-4c5d-8e9f-0a1b2c3d4e5f"
    EV = "0b9d5a52-3c1e-4f7a-9b2d-6e8f1a2b3c4d"

    def test_place_body_writes_place_row(self):
        store = _store(places=[_PLACE], members=[{"user_id": "a"}])
        res, track = self._post(
            store, {"rating": "down", "place_id": self.PL, "surface": "map_peek_community"}
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"ok": True, "rating": "down", "target_kind": "place"})
        (_, row), = store["inserts"]
        self.assertEqual((row["place_id"], row["content_snapshot"]), (self.PL, "1 person"))
        self.assertEqual(row["context"]["surface"], "map_peek_community")
        self.assertEqual(track.call_args.kwargs["event_properties"]["place_id"], self.PL)

    def test_event_body_writes_event_row(self):
        store = _store(events=[_EVENT])
        res, _ = self._post(
            store, {"rating": "up", "event_id": f" {self.EV} ", "surface": "map_peek_meet"}
        )
        self.assertEqual(res.json()["target_kind"], "event")
        (_, row), = store["inserts"]
        self.assertEqual(row["event_id"], self.EV)

    def test_malformed_id_is_400_for_every_target_and_never_reaches_the_db(self):
        # e2e: event_id "not-a-uuid" reached PostgREST, 22P02, surfaced as a 500.
        for key in ("message_id", "gap_row_id", "rec_id", "event_id", "place_id"):
            store = _store(events=[_EVENT], places=[_PLACE])
            res, _ = self._post(store, {"rating": "up", key: "not-a-uuid"})
            self.assertEqual(res.status_code, 400, key)
            self.assertEqual(res.json()["detail"], f"invalid_{key}")
            self.assertEqual(store.get("filters", []), [], key)
            self.assertEqual(store["inserts"], [], key)


if __name__ == "__main__":
    unittest.main()
