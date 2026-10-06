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


if __name__ == "__main__":
    unittest.main()
