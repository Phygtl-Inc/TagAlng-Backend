"""Semantic tip matching (20261126120000).

The scoring itself is SQL (_tip_match_strength) and is exercised against a real database,
not here. What these cover is the Python side that decides whether the database ever gets
the chance: the embedding text a tip is stored under, the fallback ladder that keeps an
un-migrated DB answering, and the rule that a community read never silently widens.
"""

import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app import local_signals
from app.tip_embed import tip_embedding_text


def _pgrst202() -> HTTPException:
    return HTTPException(status_code=502, detail="PGRST202 no function matches")


class TipEmbeddingText(unittest.TestCase):
    def test_card_fields_lead_and_prose_follows(self) -> None:
        text = tip_embedding_text(
            detail_text="Quill & Co on Main — huge paper selection, they cut mat board",
            category="stationery",
            reco_name="Quill & Co",
            reco_place="Main St",
            reco_description="Huge paper selection",
        )
        self.assertTrue(text.startswith("Quill & Co"))
        self.assertIn("stationery", text)
        self.assertIn("mat board", text)

    def test_a_field_is_not_repeated_when_detail_text_already_carries_it(self) -> None:
        text = tip_embedding_text(
            detail_text="Huge paper selection",
            reco_description="Huge paper selection",
        )
        self.assertEqual(text.lower().count("huge paper selection"), 1)

    def test_tags_ride_along(self) -> None:
        text = tip_embedding_text(detail_text="Quill & Co", affinity_tags=["art", "craft"])
        self.assertIn("art, craft", text)

    def test_an_empty_tip_embeds_to_nothing_rather_than_punctuation(self) -> None:
        self.assertEqual(tip_embedding_text(detail_text=None), "")


class AskEmbedding(unittest.TestCase):
    def test_no_embedding_degrades_to_the_lexical_call(self) -> None:
        with patch("app.layer1_handlers._embed_attr_filter", return_value=None):
            self.assertEqual(local_signals._ask_embedding_args("art supplies"), {})

    def test_an_embedding_is_sent_as_a_pgvector_literal_with_the_floor(self) -> None:
        with patch("app.layer1_handlers._embed_attr_filter", return_value=[0.1, 0.2]):
            args = local_signals._ask_embedding_args("art supplies")
        self.assertEqual(args["p_query_embedding"], "[0.1,0.2]")
        self.assertIsInstance(args["p_min_similarity"], float)


class FallbackLadder(unittest.TestCase):
    def test_the_ask_carries_its_meaning_when_embedding_is_available(self) -> None:
        with patch("app.layer1_handlers._embed_attr_filter", return_value=[0.5]), patch.object(
            local_signals, "call_rpc", return_value=[]
        ) as rpc:
            local_signals.find_neighbor_tips("jwt", block_id="b1", query="art supplies")
        self.assertEqual(rpc.call_args[0][2]["p_query_embedding"], "[0.5]")

    def test_a_db_without_the_migration_is_retried_lexically(self) -> None:
        with patch("app.layer1_handlers._embed_attr_filter", return_value=[0.5]), patch.object(
            local_signals, "call_rpc", side_effect=[_pgrst202(), []]
        ) as rpc:
            local_signals.find_neighbor_tips("jwt", block_id="b1", query="art supplies")
        self.assertEqual(rpc.call_count, 2)
        second = rpc.call_args_list[1][0][2]
        self.assertNotIn("p_query_embedding", second)
        self.assertEqual(second["p_query"], "art supplies")

    def test_a_community_read_keeps_its_scope_on_every_retry(self) -> None:
        """The v5 retry drops the vector, never the community — answering "what's good at
        CF Fitness?" with the whole neighbourhood is the one wrong answer here."""
        with patch("app.layer1_handlers._embed_attr_filter", return_value=[0.5]), patch.object(
            local_signals, "call_rpc", side_effect=[_pgrst202(), _pgrst202()]
        ) as rpc:
            rows = local_signals.find_neighbor_tips(
                "jwt", block_id="b1", query="dentist", circle_place_id="place-1"
            )
        self.assertEqual(rows, [])
        for call in rpc.call_args_list:
            self.assertEqual(call[0][2]["p_circle_place_id"], "place-1")

    def test_a_non_missing_function_error_is_not_retried(self) -> None:
        boom = HTTPException(status_code=502, detail="permission denied")
        with patch("app.layer1_handlers._embed_attr_filter", return_value=[0.5]), patch.object(
            local_signals, "call_rpc", side_effect=boom
        ) as rpc:
            rows = local_signals.find_neighbor_tips("jwt", block_id="b1", query="dentist")
        self.assertEqual(rows, [])
        self.assertEqual(rpc.call_count, 1)

    def test_a_very_old_db_still_gets_the_v1_argument_list(self) -> None:
        with patch("app.layer1_handlers._embed_attr_filter", return_value=None), patch.object(
            local_signals, "call_rpc", side_effect=[_pgrst202(), []]
        ) as rpc:
            local_signals.find_neighbor_tips("jwt", block_id="b1", query="dentist", locale="es")
        self.assertNotIn("p_locale", rpc.call_args_list[1][0][2])


class TipWriteEmbedding(unittest.TestCase):
    def _save(self, intent: str, **kw):
        calls: list[tuple[str, dict]] = []

        def _rpc(_jwt, name, payload):
            calls.append((name, payload))
            return {"signal_id": "sig-1", "matches_created": 0}

        with patch("app.layer1_handlers._embed_attr_filter", return_value=[0.25]), patch.object(
            local_signals, "call_rpc", side_effect=_rpc
        ):
            local_signals.save_local_signal(
                "jwt", intent=intent, detail_text="Quill & Co on Main", **kw
            )
        return calls

    def test_a_shared_tip_gets_a_vector(self) -> None:
        calls = self._save("tip_share", reco_name="Quill & Co")
        embed = [p for name, p in calls if name == "set_signal_embedding"]
        self.assertEqual(len(embed), 1)
        self.assertEqual(embed[0]["p_signal_id"], "sig-1")
        self.assertEqual(embed[0]["p_embedding"], "[0.25]")

    def test_a_swap_does_not(self) -> None:
        """Swaps match on item words by design (20260706120000) — embedding one would
        imply a semantic path that deliberately does not exist for them."""
        calls = self._save("swap_offer")
        self.assertNotIn("set_signal_embedding", [name for name, _ in calls])

    def test_a_failed_embedding_does_not_fail_the_post(self) -> None:
        def _rpc(_jwt, name, _payload):
            if name == "set_signal_embedding":
                raise HTTPException(status_code=502, detail="boom")
            return {"signal_id": "sig-1", "matches_created": 0}

        with patch("app.layer1_handlers._embed_attr_filter", return_value=[0.25]), patch.object(
            local_signals, "call_rpc", side_effect=_rpc
        ):
            result = local_signals.save_local_signal(
                "jwt", intent="tip_share", detail_text="Quill & Co"
            )
        self.assertEqual(result["signal_id"], "sig-1")


if __name__ == "__main__":
    unittest.main()
