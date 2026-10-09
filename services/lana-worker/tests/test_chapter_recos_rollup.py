"""Chapters: a community's recommendations roll up across its family (20270203120000).

The family scope itself lives in SQL (find_neighbor_tips / recent_neighbor_tips /
neighbor_tip_type_counts read `community_family`); the worker's job is to carry the
`origin_place_id` / `origin_place_name` each crossing row comes back with onto every
surface that shows a tip — the Find feed row, the chat rec row, the subject card's
contributor — and to hand it to the reply composer as data.
"""

import re
import unittest
from pathlib import Path
from unittest.mock import patch

import app.reco_cards as reco_cards
import app.tip_feed as tip_feed
import app.tip_rec_cascade as cascade

RCC = "33333333-3333-3333-3333-333333333333"
SJSU = "44444444-4444-4444-4444-444444444444"

_MIGRATION = (
    Path(__file__).resolve().parents[3]
    / "supabase"
    / "migrations"
    / "20270203120000_chapter_recos_rollup.sql"
)


def _row(**over):
    """One find_neighbor_tips / recent_neighbor_tips row."""
    row = {
        "signal_id": "11111111-1111-1111-1111-111111111111",
        "peer_user_id": "22222222-2222-2222-2222-222222222222",
        "neighbor_label": "coral88",
        "avatar_url": None,
        "reco_name": "Philz Coffee",
        "reco_description": "quiet upstairs, good for studying",
        "detail_text": "Philz Coffee · coffee shop · quiet upstairs",
        "category": "coffee shop",
        "reco_type": "place",
        "reco_fields": [],
        "match_strength": 0.8,
        "shared_circles": [],
        "same_block": False,
        "helpful_count": 0,
        "created_at": "2026-10-01T10:00:00+00:00",
        "subject_ref": None,
        "origin_place_id": RCC,
        "origin_place_name": "RCC",
    }
    row.update(over)
    return row


class TestOriginFields(unittest.TestCase):
    def test_carries_the_origin(self):
        self.assertEqual(
            tip_feed.origin_fields(_row()),
            {"origin_place_id": RCC, "origin_place_name": "RCC"},
        )

    def test_own_community_row_is_unlabelled(self):
        self.assertEqual(
            tip_feed.origin_fields(_row(origin_place_id=None, origin_place_name=None)),
            {"origin_place_id": None, "origin_place_name": None},
        )

    def test_a_name_without_an_id_is_not_provenance(self):
        self.assertEqual(
            tip_feed.origin_fields(_row(origin_place_id="", origin_place_name="RCC")),
            {"origin_place_id": None, "origin_place_name": None},
        )


class TestFeedRow(unittest.TestCase):
    """/lana/tips/recent: the row the PWA renders."""

    def test_recent_tips_rows_carry_origin(self):
        own = _row(signal_id="sig-own", origin_place_id=None, origin_place_name=None)
        with patch.object(tip_feed, "call_rpc", return_value=[_row(), own]) as rpc:
            rows = tip_feed.recent_tips("jwt", circle_place_id=SJSU)
        self.assertEqual(rpc.call_args.args[2]["p_circle_place_id"], SJSU)
        self.assertEqual(rows[0]["origin_place_id"], RCC)
        self.assertEqual(rows[0]["origin_place_name"], "RCC")
        self.assertIsNone(rows[1]["origin_place_id"])
        self.assertIsNone(rows[1]["origin_place_name"])

    def test_area_read_rows_have_the_keys_but_no_origin(self):
        plain = _row(origin_place_id=None, origin_place_name=None)
        with patch.object(tip_feed, "call_rpc", return_value=[plain]):
            rows = tip_feed.recent_tips("jwt")
        self.assertIn("origin_place_id", rows[0])
        self.assertIsNone(rows[0]["origin_place_id"])


class TestChatRecRow(unittest.TestCase):
    """peer_matches rows on a looking.tip turn, through main's PeerMatchRow constructor."""

    def test_peer_row_and_peer_match_model_carry_origin(self):
        from app.main import _peer_matches_from_ctx

        rows = cascade.peer_rows_from_neighbor_tips([_row()], phone_verified=False)
        self.assertEqual(rows[0]["origin_place_id"], RCC)
        self.assertEqual(rows[0]["origin_place_name"], "RCC")
        built = _peer_matches_from_ctx({"peer_matches": rows})
        self.assertEqual(built[0].origin_place_id, RCC)
        self.assertEqual(built[0].origin_place_name, "RCC")

    def test_own_row_model_is_unlabelled(self):
        from app.main import _peer_matches_from_ctx

        rows = cascade.peer_rows_from_neighbor_tips(
            [_row(origin_place_id=None, origin_place_name=None)], phone_verified=False
        )
        built = _peer_matches_from_ctx({"peer_matches": rows})
        self.assertIsNone(built[0].origin_place_id)
        self.assertIsNone(built[0].origin_place_name)


class TestSubjectCardContributor(unittest.TestCase):
    def test_contributor_and_card_model_carry_origin(self):
        from app.main import _reco_cards_from_ctx

        with patch.object(reco_cards, "_attach_cohorts"), patch.object(
            reco_cards, "_attach_reads"
        ):
            cards = reco_cards.subject_cards_from_tips([_row()])
        contrib = cards[0]["contributors"][0]
        self.assertEqual(contrib["origin_place_id"], RCC)
        self.assertEqual(contrib["origin_place_name"], "RCC")
        built = _reco_cards_from_ctx({"reco_cards": cards})
        self.assertEqual(built[0].contributors[0].origin_place_id, RCC)
        self.assertEqual(built[0].contributors[0].origin_place_name, "RCC")


class TestReplyFacts(unittest.TestCase):
    """The composer gets origin as DATA, never as wording to repeat."""

    def _facts(self, tips, scope="SJSU"):
        from app import discovery_route as dr

        captured = {}

        def _compose(*, goal, facts, session_ctx, fallback, max_sentences):
            captured.update(goal=goal, facts=facts)
            return "ok"

        with patch.object(dr, "compose_reply", _compose), patch.object(
            dr, "community_name", return_value=scope
        ):
            dr._compose_neighbor_tip_reply(tips, detail="study spot", session_ctx={})
        return captured["facts"]

    def test_a_chapter_row_names_where_it_was_shared(self):
        facts = self._facts([_row()])
        line = next(f for f in facts if "coral88 recommended" in f)
        self.assertIn("RCC", line)
        self.assertFalse(any(f.startswith("Every recommender below is at") for f in facts))
        self.assertTrue(any("related" in f and "SJSU" in f for f in facts))

    def test_no_family_row_keeps_the_old_scope_fact(self):
        facts = self._facts([_row(origin_place_id=None, origin_place_name=None)])
        self.assertIn(
            "Every recommender below is at SJSU, the community they are filtered to", facts
        )
        line = next(f for f in facts if "coral88 recommended" in f)
        self.assertNotIn("shared in", line)


class TestMigration(unittest.TestCase):
    """The three readers of community tip visibility all read the family, and only there."""

    @classmethod
    def setUpClass(cls):
        cls.sql = _MIGRATION.read_text()

    def _body(self, name):
        m = re.search(
            rf"create or replace function public\.{name}\(.*?\n(\$\$|\$function\$);",
            self.sql,
            re.S,
        )
        self.assertIsNotNone(m, name)
        return m.group(0)

    def test_every_reader_scopes_to_the_family(self):
        for fn in ("find_neighbor_tips", "recent_neighbor_tips", "neighbor_tip_type_counts"):
            body = self._body(fn)
            self.assertIn("public.community_family(v_me, p_circle_place_id)", body, fn)
            self.assertIn("s.circle_place_ref = any (v_family)", body, fn)
            self.assertNotIn("s.circle_place_ref = p_circle_place_id", body, fn)
            # The community itself is always in its own family, so a failed family read
            # narrows to the old single-community read and never widens.
            self.assertIn("v_family := array[p_circle_place_id] ||", body, fn)

    def test_row_readers_return_origin(self):
        for fn in ("find_neighbor_tips", "recent_neighbor_tips"):
            body = self._body(fn)
            self.assertIn("origin_place_id", body, fn)
            self.assertIn("origin_place_name", body, fn)
            self.assertIn("s.circle_place_ref <> p_circle_place_id", body, fn)

    def test_grants_restated_with_anon_revoked(self):
        for sig in (
            "find_neighbor_tips(text, text, text, int, text, double precision, uuid, "
            "extensions.vector, real, text[])",
            "recent_neighbor_tips(text, double precision, int, text, uuid, text[])",
            "neighbor_tip_type_counts(text, double precision, uuid)",
        ):
            self.assertIn(f"revoke all on function public.{sig}\n  from public, anon;", self.sql)
            self.assertIn(f"grant execute on function public.{sig}\n  to authenticated", self.sql)


if __name__ == "__main__":
    unittest.main()
