import unittest
from unittest.mock import MagicMock, patch

from app.claims_persist import (
    detect_heritage_conflict,
    dismiss_claims_from_edit_message,
    extract_display_name_reply,
    extract_nickname_from_message,
    filter_extracted_claims,
    is_discovery_query_message,
    is_explicit_heritage_correction,
    is_negative_claim,
    reconcile_heritage_claims,
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

    def test_affirmative_not_treated_as_name(self) -> None:
        self.assertIsNone(extract_display_name_reply("ok"))
        self.assertIsNone(extract_display_name_reply("okay"))

    def test_change_name_to(self) -> None:
        self.assertEqual(
            extract_nickname_from_message("change my name to Sofia"),
            "Sofia",
        )
        self.assertEqual(
            extract_nickname_from_message("add my name as Ada"),
            "Ada",
        )

    def test_sofia_reply(self) -> None:
        self.assertEqual(extract_display_name_reply("sofia"), "Sofia")


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

    def test_skips_discovery_queries(self) -> None:
        self.assertFalse(should_extract_claims_from_message("find pakistani mom"))
        self.assertFalse(should_extract_claims_from_message("introduce me to Natasha"))
        self.assertTrue(is_discovery_query_message("find brazilian mom"))
        self.assertTrue(is_discovery_query_message("introduce me to Natasha"))


class TestClaimFilters(unittest.TestCase):
    def test_rejects_negative_heritage(self) -> None:
        claim = ExtractedClaim(
            concept="no_italian_heritage",
            label="No Italian Heritage",
            confidence=0.9,
            bucket="heritage",
        )
        self.assertTrue(is_negative_claim(claim))

    def test_filter_drops_negatives_and_search(self) -> None:
        claims = filter_extracted_claims(
            "find brazilian mom",
            [
                ExtractedClaim(
                    concept="brazilian_heritage",
                    label="Brazilian",
                    confidence=0.9,
                    bucket="heritage",
                )
            ],
        )
        self.assertEqual(claims, [])

    def test_filter_keeps_first_person_identity(self) -> None:
        claims = filter_extracted_claims(
            "I am a pakistani mom",
            [
                ExtractedClaim(
                    concept="pakistani_heritage",
                    label="Pakistani",
                    confidence=0.9,
                    bucket="heritage",
                    source_quote="pakistani",
                ),
                ExtractedClaim(
                    concept="no_italian_heritage",
                    label="No Italian Heritage",
                    confidence=0.9,
                    bucket="heritage",
                ),
                ExtractedClaim(
                    concept="mom",
                    label="Mom",
                    confidence=0.9,
                    bucket="stage",
                ),
            ],
        )
        self.assertEqual(len(claims), 2)
        self.assertEqual(claims[0].concept, "pakistani_heritage")


class TestReconcileHeritage(unittest.TestCase):
    @patch("app.claims_persist._dismiss_claims_by_ids")
    @patch("app.claims_persist.service_client")
    def test_reconcile_dismisses_conflicts_and_negatives(
        self, mock_client: MagicMock, mock_dismiss: MagicMock
    ) -> None:
        sb = MagicMock()
        mock_client.return_value = sb
        table = MagicMock()
        sb.table.return_value = table
        chain = MagicMock()
        table.select.return_value = chain
        chain.eq.return_value = chain
        chain.is_.return_value = chain
        chain.execute.return_value = MagicMock(
            data=[
                {"id": "c1", "concept": "italian_heritage", "label": "Italian", "bucket": "heritage"},
                {"id": "c2", "concept": "no_italian", "label": "No Italian Heritage", "bucket": "heritage"},
                {"id": "c3", "concept": "brazilian_heritage", "label": "Brazilian", "bucket": "heritage"},
            ]
        )
        batch = [
            ExtractedClaim(
                concept="pakistani_heritage",
                label="Pakistani",
                confidence=0.9,
                bucket="heritage",
            )
        ]
        reconcile_heritage_claims("user-1", batch)
        dismissed = mock_dismiss.call_args[0][1]
        self.assertEqual(set(dismissed), {"c1", "c2", "c3"})

    @patch("app.claims_persist._dismiss_claims_by_ids")
    @patch("app.claims_persist.service_client")
    def test_reconcile_replaces_american_with_brazilian(
        self, mock_client: MagicMock, mock_dismiss: MagicMock
    ) -> None:
        sb = MagicMock()
        mock_client.return_value = sb
        table = MagicMock()
        sb.table.return_value = table
        chain = MagicMock()
        table.select.return_value = chain
        chain.eq.return_value = chain
        chain.is_.return_value = chain
        chain.execute.return_value = MagicMock(
            data=[
                {"id": "c1", "concept": "american_heritage", "label": "American", "bucket": "heritage"},
            ]
        )
        batch = [
            ExtractedClaim(
                concept="brazilian_heritage",
                label="Brazilian",
                confidence=0.9,
                bucket="heritage",
            )
        ]
        reconcile_heritage_claims("user-1", batch)
        dismissed = mock_dismiss.call_args[0][1]
        self.assertEqual(dismissed, ["c1"])


class TestHeritageConflict(unittest.TestCase):
    @patch("app.claims_persist.fetch_active_heritage_claims")
    def test_detects_conflicting_heritage(self, mock_fetch: MagicMock) -> None:
        mock_fetch.return_value = [("american_heritage", "American")]
        conflict = detect_heritage_conflict(
            "user-1",
            [
                ExtractedClaim(
                    concept="brazilian_heritage",
                    label="Brazilian",
                    confidence=0.9,
                    bucket="heritage",
                )
            ],
        )
        self.assertIsNotNone(conflict)
        assert conflict is not None
        self.assertEqual(conflict[0], "American")
        self.assertEqual(conflict[1].label, "Brazilian")

    def test_explicit_correction_flag(self) -> None:
        self.assertTrue(
            is_explicit_heritage_correction("ok so i am not brazilian. I am american")
        )
        self.assertFalse(is_explicit_heritage_correction("I am Brazilian"))


class TestDismissClaimsFromEdit(unittest.TestCase):
    @patch("app.claims_persist._dismiss_claims_by_ids")
    @patch("app.claims_persist.service_client")
    def test_remove_italian_dismisses_both_positive_and_negative(
        self, mock_client: MagicMock, mock_dismiss: MagicMock
    ) -> None:
        sb = MagicMock()
        mock_client.return_value = sb
        table = MagicMock()
        sb.table.return_value = table
        chain = MagicMock()
        table.select.return_value = chain
        chain.eq.return_value = chain
        chain.is_.return_value = chain
        chain.execute.return_value = MagicMock(
            data=[
                {"id": "c1", "concept": "italian_heritage", "label": "Italian Heritage", "bucket": "heritage"},
                {"id": "c2", "concept": "no_italian", "label": "No Italian Heritage", "bucket": "heritage"},
                {"id": "c3", "concept": "pakistani_heritage", "label": "Pakistani", "bucket": "heritage"},
            ]
        )
        n = dismiss_claims_from_edit_message(
            "user-1",
            "remove no italian heritage and italian heritage. I am pakistani",
        )
        self.assertEqual(n, 2)
        dismissed = set(mock_dismiss.call_args[0][1])
        self.assertEqual(dismissed, {"c1", "c2"})


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
