"""Session-scoped draft state + fresh=true session creation.

QA (2026-07-08): concurrent sessions under one account received the same session_id
(resume-active) and their host drafts overwrote each other. These tests pin the fix:

- POST /lana/sessions default stays resume-active; {"fresh": true} archives the current
  active session and creates a distinct one.
- Draft state is keyed by session id, never user id: a draft written in session A is
  invisible to session B of the same user, and the pending_event_drafts stash holds one
  slot per source session (pop takes newest, leaves the other session's stash intact).
"""

import itertools
import unittest
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from app.auth import AuthSession
from app.db import (
    create_session,
    get_session_for_user,
    pop_pending_event_draft,
    stash_pending_event_draft,
    update_session_context,
)
from app.models import CreateSessionRequest

_SEQ = itertools.count(1)


class FakeQuery:
    """Minimal in-memory stand-in for the supabase query builder (one query per call)."""

    def __init__(self, store: dict, name: str) -> None:
        self.store = store
        self.name = name
        self.op = "select"
        self.payload: dict | None = None
        self.conflict_col: str | None = None
        self.filters: list[tuple[str, object]] = []
        self.order_col: str | None = None
        self.order_desc = False
        self.lim: int | None = None

    # builder ------------------------------------------------------------
    def insert(self, row):
        self.op, self.payload = "insert", dict(row)
        return self

    def upsert(self, row, on_conflict=None):
        self.op, self.payload, self.conflict_col = "upsert", dict(row), on_conflict
        return self

    def update(self, patch):
        self.op, self.payload = "update", dict(patch)
        return self

    def delete(self):
        self.op = "delete"
        return self

    def select(self, *_args, **_kwargs):
        self.op = "select"
        return self

    def eq(self, col, val):
        self.filters.append((col, val))
        return self

    def is_(self, _col, _val):
        return self

    def order(self, col, desc=False):
        self.order_col, self.order_desc = col, desc
        return self

    def limit(self, n):
        self.lim = n
        return self

    # execution ----------------------------------------------------------
    def _rows(self):
        return self.store.setdefault(self.name, [])

    def _matches(self, row):
        return all(str(row.get(col)) == str(val) for col, val in self.filters)

    def execute(self):
        rows = self._rows()
        result = MagicMock()
        if self.op == "insert":
            row = self._stamp(self.payload)
            rows.append(row)
            result.data = [dict(row)]
            return result
        if self.op == "upsert":
            row = self._stamp(self.payload)
            if self.conflict_col is not None and row.get(self.conflict_col) is not None:
                for i, existing in enumerate(rows):
                    if existing.get(self.conflict_col) == row.get(self.conflict_col):
                        row["id"] = existing["id"]
                        rows[i] = row
                        result.data = [dict(row)]
                        return result
            rows.append(row)
            result.data = [dict(row)]
            return result
        if self.op == "update":
            hit = []
            for row in rows:
                if self._matches(row):
                    row.update(self.payload)
                    hit.append(dict(row))
            result.data = hit
            return result
        if self.op == "delete":
            kept = [r for r in rows if not self._matches(r)]
            self.store[self.name] = kept
            result.data = []
            return result
        # select
        out = [dict(r) for r in rows if self._matches(r)]
        if self.order_col:
            out.sort(
                key=lambda r: (str(r.get(self.order_col) or ""), r.get("_seq", 0)),
                reverse=self.order_desc,
            )
        if self.lim is not None:
            out = out[: self.lim]
        result.data = out
        return result

    def _stamp(self, row: dict) -> dict:
        row = dict(row)
        row.setdefault("id", str(uuid.uuid4()))
        now = datetime.now(timezone.utc).isoformat()
        row.setdefault("created_at", now)
        row.setdefault("updated_at", now)
        row["_seq"] = next(_SEQ)  # deterministic newest-first tiebreak
        return row


def _fake_service_client(store: dict) -> MagicMock:
    sb = MagicMock()
    sb.table.side_effect = lambda name: FakeQuery(store, name)
    return sb


class TestFreshVsResume(unittest.TestCase):
    def setUp(self) -> None:
        self.store: dict = {}
        patcher = patch(
            "app.db.service_client", return_value=_fake_service_client(self.store)
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_default_resumes_active_session(self) -> None:
        s1, resumed1 = create_session("u1", "lana")
        s2, resumed2 = create_session("u1", "lana")
        self.assertFalse(resumed1)
        self.assertTrue(resumed2)
        self.assertEqual(s1["id"], s2["id"])
        self.assertEqual(
            len([r for r in self.store["lana_sessions"] if r["status"] == "active"]), 1
        )

    def test_fresh_creates_distinct_session_and_archives_old(self) -> None:
        s1, _ = create_session("u1", "lana")
        s2, resumed = create_session("u1", "lana", force_new=True)
        self.assertFalse(resumed)
        self.assertNotEqual(s1["id"], s2["id"])
        old = next(r for r in self.store["lana_sessions"] if r["id"] == s1["id"])
        self.assertEqual(old["status"], "abandoned")
        new = next(r for r in self.store["lana_sessions"] if r["id"] == s2["id"])
        self.assertEqual(new["status"], "active")

    def test_draft_in_session_a_invisible_to_session_b(self) -> None:
        s1, _ = create_session("u1", "lana")
        update_session_context(
            str(s1["id"]), {"event_draft": {"title": "Birthday party"}}
        )
        s2, _ = create_session("u1", "lana", force_new=True)
        ctx_b = dict(get_session_for_user(str(s2["id"]), "u1").get("context") or {})
        self.assertNotIn("event_draft", ctx_b)
        # B builds its own draft; A's stays untouched — no bleed either direction.
        update_session_context(
            str(s2["id"]), {"event_draft": {"title": "Park hangout"}}
        )
        ctx_a = dict(get_session_for_user(str(s1["id"]), "u1").get("context") or {})
        ctx_b = dict(get_session_for_user(str(s2["id"]), "u1").get("context") or {})
        self.assertEqual(ctx_a["event_draft"]["title"], "Birthday party")
        self.assertEqual(ctx_b["event_draft"]["title"], "Park hangout")


class TestSessionScopedStash(unittest.TestCase):
    def setUp(self) -> None:
        self.store: dict = {}
        patcher = patch(
            "app.db.service_client", return_value=_fake_service_client(self.store)
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_two_sessions_hold_two_stashes_without_bleed(self) -> None:
        stash_pending_event_draft(
            "u1", {"event_draft": {"title": "Birthday party"}}, session_id="sess-a"
        )
        stash_pending_event_draft(
            "u1", {"event_draft": {"title": "Park hangout"}}, session_id="sess-b"
        )
        self.assertEqual(len(self.store["pending_event_drafts"]), 2)
        titles = {
            r["host_ctx"]["event_draft"]["title"]
            for r in self.store["pending_event_drafts"]
        }
        self.assertEqual(titles, {"Birthday party", "Park hangout"})

    def test_restash_replaces_own_session_slot_only(self) -> None:
        stash_pending_event_draft(
            "u1", {"event_draft": {"title": "Birthday party"}}, session_id="sess-a"
        )
        stash_pending_event_draft(
            "u1", {"event_draft": {"title": "Park hangout"}}, session_id="sess-b"
        )
        stash_pending_event_draft(
            "u1", {"event_draft": {"title": "Birthday party v2"}}, session_id="sess-a"
        )
        rows = self.store["pending_event_drafts"]
        self.assertEqual(len(rows), 2)
        by_session = {r["session_id"]: r["host_ctx"]["event_draft"]["title"] for r in rows}
        self.assertEqual(by_session["sess-a"], "Birthday party v2")
        self.assertEqual(by_session["sess-b"], "Park hangout")

    def test_pop_takes_newest_and_leaves_other_sessions_stash(self) -> None:
        stash_pending_event_draft(
            "u1", {"event_draft": {"title": "Birthday party"}}, session_id="sess-a"
        )
        stash_pending_event_draft(
            "u1", {"event_draft": {"title": "Park hangout"}}, session_id="sess-b"
        )
        first = pop_pending_event_draft("u1")
        self.assertIsNotNone(first)
        assert first is not None
        self.assertEqual(first["event_draft"]["title"], "Park hangout")
        remaining = self.store["pending_event_drafts"]
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["session_id"], "sess-a")
        second = pop_pending_event_draft("u1")
        assert second is not None
        self.assertEqual(second["event_draft"]["title"], "Birthday party")
        self.assertIsNone(pop_pending_event_draft("u1"))


class TestCreateSessionEndpointFresh(unittest.TestCase):
    """POST /lana/sessions wiring: {"fresh": true} forces a new session; default resumes."""

    def _call(self, body):
        from app import main as m

        auth = AuthSession(
            user_id="u1", is_anonymous=False, phone_verified=True, home_block_id="b1"
        )
        with patch.object(m, "_vertex_configured", return_value=True), patch.object(
            m, "verify_auth", return_value=auth
        ), patch.object(m, "create_session") as mock_create, patch.object(
            m, "pop_pending_event_draft", return_value=None
        ), patch.object(
            m, "pop_pending_meet_seek", return_value=None
        ), patch.object(
            m, "user_needs_display_name", return_value=False
        ), patch.object(
            m, "lana_unified_opening", return_value=("Hi!", "continue", {}, None)
        ), patch.object(
            m, "insert_message", return_value="m1"
        ), patch.object(
            m, "update_session_context"
        ):
            mock_create.return_value = ({"id": "s-new", "context": {}}, False)
            resp = m.create_lana_session(body, authorization="Bearer x")
        return mock_create, resp

    def test_fresh_true_maps_to_force_new(self) -> None:
        mock_create, resp = self._call(CreateSessionRequest(purpose="lana", fresh=True))
        mock_create.assert_called_once_with("u1", "lana", force_new=True)
        self.assertEqual(resp.session_id, "s-new")

    def test_default_body_resumes(self) -> None:
        mock_create, _ = self._call(CreateSessionRequest(purpose="lana"))
        mock_create.assert_called_once_with("u1", "lana", force_new=False)

    def test_missing_body_resumes(self) -> None:
        mock_create, _ = self._call(None)
        mock_create.assert_called_once_with("u1", "lana", force_new=False)

    def test_force_new_still_honored(self) -> None:
        # The login-reset flow already sends force_new — must keep working.
        mock_create, _ = self._call(
            CreateSessionRequest(purpose="lana", force_new=True)
        )
        mock_create.assert_called_once_with("u1", "lana", force_new=True)

    def test_fresh_parses_from_json_body(self) -> None:
        req = CreateSessionRequest.model_validate({"fresh": True})
        self.assertTrue(req.fresh)
        self.assertEqual(req.purpose, "lana")
        self.assertFalse(CreateSessionRequest.model_validate({}).fresh)


if __name__ == "__main__":
    unittest.main()
