import unittest

from app.claim_embed import claim_embedding_text


class TestClaimEmbeddingText(unittest.TestCase):
    def test_heritage_uses_source_quote(self) -> None:
        text = claim_embedding_text(
            concept="brazilian_heritage",
            label="Brazilian Heritage",
            source_quote="brazlian mom",
            bucket="heritage",
        )
        self.assertEqual(text, "brazilian_heritage: brazlian mom")
        self.assertNotIn("Heritage", text)

    def test_faith_uses_source_quote(self) -> None:
        text = claim_embedding_text(
            concept="catholic_faith",
            label="Catholic",
            source_quote="Sunday mass at St Luke",
            bucket="faith",
        )
        self.assertIn("Sunday mass", text)

    def test_activity_includes_label_and_quote(self) -> None:
        text = claim_embedding_text(
            concept="cricket_player",
            label="Cricket Player",
            source_quote="playing cricket",
            bucket="activity",
        )
        self.assertIn("Cricket Player", text)
        self.assertIn("playing cricket", text)


if __name__ == "__main__":
    unittest.main()
