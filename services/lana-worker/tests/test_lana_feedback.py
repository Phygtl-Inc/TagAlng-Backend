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

    def eq(self, *a, **k):
        return self

    def limit(self, *a, **k):
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


def _store(messages=None, sessions=None, gaps=None, existing=None, recs=None):
    return {
        "selects": {
            "lana_messages": messages or [],
            "lana_sessions": sessions or [],
            "rapport_gaps": gaps or [],
            "peer_rec_lines": recs or [],
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


class TestRecordFeedback(unittest.TestCase):
    def _run(self, store, **kwargs):
        with patch.object(feedback, "service_client", return_value=_Supabase(store)):
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

    def test_invalid_rating_is_400(self):
        with self.assertRaises(HTTPException) as ctx:
            self._run(_store(messages=[_MSG], sessions=[_SES]), rating="meh", message_id="m1")
        self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
