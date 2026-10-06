"""Fixes from the PR #197 end-to-end run (local stack, real model, 2026-10-06).

One class per bug. Pure functions and in-memory doubles — no LLM, no DB.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch


class _Q:
    """A PostgREST builder stand-in: records every chained call, returns `rows`."""

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[tuple[str, tuple]] = []

    def __getattr__(self, name: str):
        def _m(*args, **kwargs):
            self.calls.append((name, args))
            return self

        return _m

    def execute(self):
        return SimpleNamespace(data=self.rows, count=len(self.rows))


class TestPrivateMeetCopy(unittest.TestCase):
    """§29: an invite-only meet is never listed, so the copy must not send it to
    "neighbors" / "people nearby" — it tells the host to share the link."""

    def test_private_publish_note_says_share_the_link(self) -> None:
        from app.lana_unified_pipeline import _event_published_reply

        reply = _event_published_reply("", {"title": "Book club", "is_private": True})
        self.assertIn("Book club", reply)
        self.assertIn("invite-only", reply)
        self.assertIn("link", reply)
        self.assertNotIn("in your area", reply)
        self.assertNotIn("Neighbors", reply)

    def test_public_publish_note_unchanged(self) -> None:
        from app.lana_unified_pipeline import _event_published_reply

        for draft in ({"title": "Book club", "is_private": False}, {"title": "Book club"}):
            reply = _event_published_reply("", draft)
            self.assertIn("live in your area", reply)
            self.assertNotIn("invite-only", reply)

    def test_audience_fact_follows_the_privacy_card(self) -> None:
        from app.lana_unified_pipeline import _host_audience_fact

        private = _host_audience_fact({"is_private": True})
        self.assertIn("INVITE-ONLY", private)
        self.assertIn("link", private)
        public = _host_audience_fact({"is_private": False})
        self.assertIn("public", public)
        self.assertNotIn("INVITE-ONLY", public)
        self.assertEqual(_host_audience_fact(None), public)


def _event_requests_columns() -> set[str]:
    """public.event_requests' real columns, read from the migrations that define it —
    so a select naming a column the table never had fails here, not silently in prod."""
    import pathlib
    import re

    mig = pathlib.Path(__file__).resolve().parents[3] / "supabase" / "migrations"
    cols: set[str] = set()
    for f in sorted(mig.glob("*.sql")):
        sql = f.read_text()
        m = re.search(r"create table if not exists public\.event_requests \((.*?)\n\);", sql, re.S)
        if m:
            for line in m.group(1).splitlines():
                w = line.strip().split()
                if w and w[0] not in ("unique", "constraint", "primary", "check", "foreign"):
                    cols.add(w[0])
        for block in re.findall(r"alter table public\.event_requests(.*?);", sql, re.S):
            cols.update(re.findall(r"add column if not exists (\w+)", block))
    return cols


class _SchemaQ(_Q):
    """Like PostgREST: selecting a column the table does not have is an error."""

    def __init__(self, rows, columns):
        super().__init__(rows)
        self.columns = columns

    def select(self, cols, *a, **k):
        for c in (x.strip() for x in cols.split(",")):
            if c not in self.columns:
                raise RuntimeError(f"column event_requests.{c} does not exist")
        self.calls.append(("select", (cols,)))
        return self


class TestGoingRostersReadTheRealColumn(unittest.TestCase):
    """community_surface._going_rosters selected event_requests.user_id; the column is
    requester_id, PostgREST rejected it, the except swallowed it, going_count was 0."""

    def test_schema_has_requester_id_not_user_id(self) -> None:
        cols = _event_requests_columns()
        self.assertIn("requester_id", cols)
        self.assertIn("rsvp_status", cols)
        self.assertNotIn("user_id", cols)

    def test_roster_counts_going_requesters(self) -> None:
        from app import community_surface

        rows = [
            {"event_id": "e1", "requester_id": "u2"},
            {"event_id": "e1", "requester_id": "u3"},
            {"event_id": "e2", "requester_id": "u4"},
        ]
        q = _SchemaQ(rows, _event_requests_columns())
        sb = SimpleNamespace(table=lambda name: q)
        with patch.object(community_surface, "service_client", return_value=sb):
            rosters = community_surface._going_rosters(["e1", "e2"])
            counts = community_surface._going_counts(["e1", "e2"])
        self.assertEqual(rosters, {"e1": ["u2", "u3"], "e2": ["u4"]})
        self.assertEqual(counts, {"e1": 2, "e2": 1})
        self.assertIn(("eq", ("rsvp_status", "going")), q.calls)


if __name__ == "__main__":
    unittest.main()
