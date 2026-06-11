import unittest
from unittest.mock import MagicMock, patch

from app.claims_persist import (
    extract_display_name_reply,
    extract_nickname_from_message,
    should_extract_claims_from_message,
    upsert_claims,
)
from app.models import ExtractedClaim
from app.vertex_extract import parse_incremental_claims_data


class TestNicknameExtract(unittest.TestCase):
    def test_my_name_is(self) -> None:
        self.assertEqual(
            extract_nickname_from_message("btw my name is brigade"),
            "Brigade",
        )

    def test_does_not_treat_heritage_as_name(self) -> None:
        self.assertIsNone(extract_nickname_from_message("I am an italian mom"))

    def test_single_word_when_asked(self) -> None:
        self.assertEqual(extract_display_name_reply("brigade"), "Brigade")


class TestShouldExtractClaims(unittest.TestCase):
    def test_skips_zip_and_otp(self) -> None:
        self.assertFalse(should_extract_claims_from_message("32827"))
        self.assertFalse(should_extract_claims_from_message("000000"))
        self.assertFalse(should_extract_claims_from_message("+9233079925113"))

    def test_accepts_identity_lines(self) -> None:
        self.assertTrue(
            should_extract_claims_from_message("I am an italian mom who is a mom")
        )
        self.assertTrue(
            should_extract_claims_from_message(
                "I am brinda and I like brazilian people"
            )
        )

    def test_skips_short_ack(self) -> None:
        self.assertFalse(should_extract_claims_from_message("ok"))


class TestParseIncrementalClaims(unittest.TestCase):
    def test_empty_identity(self) -> None:
        nickname, claims = parse_incremental_claims_data(
            {"nickname": None, "claims": []}
        )
        self.assertIsNone(nickname)
        self.assertEqual(claims, [])

    def test_italian_mom_split(self) -> None:
        nickname, claims = parse_incremental_claims_data(
            {
                "nickname": None,
                "claims": [
                    {
                        "concept": "italian_heritage",
                        "label": "Italian",
                        "confidence": 0.9,
                        "disclosure": "public",
                        "source_quote": "italian mom",
                        "bucket": "heritage",
                    },
                    {
                        "concept": "mom_of_toddlers",
                        "label": "Mom",
                        "confidence": 0.88,
                        "disclosure": "public",
                        "source_quote": "mom",
                        "bucket": "stage",
                    },
                ],
            }
        )
        self.assertIsNone(nickname)
        self.assertEqual(len(claims), 2)
        self.assertEqual(claims[0].concept, "italian_heritage")


class TestUpsertClaims(unittest.TestCase):
    @patch("app.claims_persist.service_client")
    @patch("app.claims_persist._embed_claim", return_value=[0.1] * 768)
    def test_upserts_by_concept(self, _embed: MagicMock, mock_client: MagicMock) -> None:
        sb = MagicMock()
        mock_client.return_value = sb
        table = MagicMock()
        sb.table.return_value = table
        select_chain = MagicMock()
        table.select.return_value = select_chain
        select_chain.eq.return_value = select_chain
        select_chain.is_.return_value = select_chain
        select_chain.limit.return_value = select_chain
        select_chain.execute.return_value = MagicMock(data=[])

        claim = ExtractedClaim(
            concept="italian_heritage",
            label="Italian",
            confidence=0.9,
            source_quote="italian mom",
            bucket="heritage",
        )
        n = upsert_claims("user-1", [claim])
        self.assertEqual(n, 1)
        table.insert.assert_called_once()


if __name__ == "__main__":
    unittest.main()
