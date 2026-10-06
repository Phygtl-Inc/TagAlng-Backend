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


class TestRecoTypeOtherIsWritable(unittest.TestCase):
    """reco_type 'other': set_signal_reco accepted it, the table CHECK did not (23514 →
    /lana/tips/update 502, and a capture silently lost every reco_* field)."""

    @staticmethod
    def _sets() -> tuple[set[str], set[str]]:
        import pathlib
        import re

        mig = pathlib.Path(__file__).resolve().parents[3] / "supabase" / "migrations"
        table: set[str] = set()
        rpc: set[str] = set()
        for f in sorted(mig.glob("*.sql")):
            sql = f.read_text()
            for m in re.finditer(r"check \(reco_type is null or reco_type in \((.*?)\)\)", sql, re.S):
                table = set(re.findall(r"'(\w+)'", m.group(1)))
            if "function public.set_signal_reco(" in sql:
                m = re.search(r"p_reco_type not in \((.*?)\)", sql, re.S)
                if m:
                    rpc = set(re.findall(r"'(\w+)'", m.group(1)))
        return table, rpc

    def test_table_check_accepts_everything_the_writer_accepts(self) -> None:
        table, rpc = self._sets()
        self.assertIn("other", rpc)
        self.assertIn("other", table)
        self.assertEqual(table, rpc)

    def test_chat_edit_keeps_the_rows_type_when_the_draft_has_none(self) -> None:
        from app import tip_share

        seen = {}

        def _rpc(jwt, name, payload):
            seen[name] = payload

        with patch("app.supabase_rpc.call_rpc", side_effect=_rpc):
            tip_share._update_posted_tip(draft={"name": "Canvas"}, user_jwt="j", signal_id="s1")
            self.assertIsNone(seen["set_signal_reco"]["p_reco_type"])  # null = leave it
            tip_share._update_posted_tip(
                draft={"name": "Canvas", "reco_type": "restaurant"}, user_jwt="j", signal_id="s1"
            )
            self.assertEqual(seen["set_signal_reco"]["p_reco_type"], "restaurant")


class TestFixChipReaskSpeaksAboutTheContent(unittest.TestCase):
    """§30(e): tapping the "takes Delta Dental" chip got "change the qualifier 'takes
    Delta Dental'" — the field key was handed to the composer as the part's name."""

    def _reask(self, field: str) -> tuple[str, list[str]]:
        from app import discovery_route as dr

        seen = {}

        def _compose(**kw):
            seen.update(kw)
            return "ok"

        ctx = {
            "ask_draft_pending": {
                "title": "Orthodontist who takes Delta Dental",
                "detail": "orthodontist in lake nona who takes delta dental",
                "chips": [
                    {"label": "orthodontist", "field": "category"},
                    {"label": "Lake Nona", "field": "locality"},
                    {"label": "takes Delta Dental", "field": "qualifier"},
                ],
            }
        }
        with patch.object(dr, "compose_reply", side_effect=_compose):
            out = dr._try_ask_draft_reply_turn(
                msg=f"fix:{field}", session_ctx=ctx, user_jwt="j", phone_verified=True,
                home_block_id="b1", phase="listening", user_id="u1",
            )
        self.assertIsNotNone(out)
        return seen["goal"], seen["facts"]

    def test_qualifier_is_described_by_its_words_not_its_key(self) -> None:
        goal, facts = self._reask("qualifier")
        part = next(f for f in facts if f.startswith("The part they tapped"))
        self.assertIn('"takes Delta Dental"', part)
        self.assertIn("a requirement it has to meet", part)
        self.assertNotIn("qualifier", part)
        self.assertIn("never call it by a label", goal)

    def test_every_chip_field_has_plain_words(self) -> None:
        from app import discovery_route as dr

        self.assertEqual(set(dr._ASK_DRAFT_FIX_MEANING), set(dr._ASK_DRAFT_FIX_FIELDS))
        for field in dr._ASK_DRAFT_FIX_FIELDS:
            _goal, facts = self._reask(field)
            part = next(f for f in facts if f.startswith("The part they tapped"))
            self.assertNotIn(field, part)
            self.assertNotIn(field.replace("_", " "), part)


class TestReadyCardPostIsARenderedControl(unittest.TestCase):
    """e2e: "find me a dentist" → "I recommend Canvas restaurant" → steps → ready card →
    "pass the tip along" posted the DENTIST ask: the classifier released the lane on the
    card's own button and the still-armed ask offer read it as a yes."""

    @staticmethod
    def _ctx(ready: bool) -> dict:
        return {
            "tip_share_active": True,
            "tip_ready": ready,
            "tip_draft": {"name": "Canvas restaurant", "reco_type": "restaurant", "ready": ready},
        }

    def test_the_card_button_holds_the_lane_whatever_the_classifier_says(self) -> None:
        from app import tip_share as ts

        pivot = {"abandon": True, "goal": "looking.tip", "confidence": 0.95}
        for msg in ("pass the tip along", "Pass the tip along "):
            self.assertTrue(ts._is_ready_post(msg, self._ctx(True)), msg)
            self.assertFalse(ts.tip_share_should_release(msg, self._ctx(True), pivot), msg)

    def test_only_while_the_ready_card_is_showing(self) -> None:
        from app import tip_share as ts

        self.assertFalse(ts._is_ready_post("pass the tip along", self._ctx(False)))
        self.assertFalse(ts._is_ready_post("pass the tip along", {"tip_ready": True}))
        # Free text that merely contains the words is not the button.
        self.assertFalse(ts._is_ready_post("can you pass the tip along to Sam", self._ctx(True)))

    def test_the_utterance_is_what_the_button_sends(self) -> None:
        from app import tip_share as ts
        from app.ui_actions import tip_pass_actions

        sent = {a["message"] for a in tip_pass_actions()}
        self.assertIn(ts._POST_UTTERANCE, sent)

    def test_a_capture_turn_spends_stale_offers_and_seek_rows(self) -> None:
        import pathlib

        src = (pathlib.Path(__file__).resolve().parents[1] / "app" / "lana_unified_pipeline.py").read_text()
        lane = src[src.index("if tip_share_should_release(user_message"):src.index("run_tip_share_turn(\n                    user_message")]
        for key in ("tip_ask_offer_pending", "posting_manage_pending"):
            self.assertIn(f'session_ctx["{key}"] = None', lane)
        self.assertIn('session_ctx["peer_matches"] = []', lane)


if __name__ == "__main__":
    unittest.main()
