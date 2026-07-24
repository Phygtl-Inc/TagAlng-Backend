import unittest
from unittest.mock import MagicMock, patch

from app.circles_capture import (
    CircleCandidate,
    PlaceFeatureCandidate,
    parse_circle_candidates,
    parse_place_feature_candidates,
    persist_circle_candidates,
    persist_place_feature_candidates,
    upsert_place_feature,
)


def _chain(data=None):
    """A supabase table mock where every query-builder call returns itself."""
    m = MagicMock()
    for method in ("select", "eq", "is_", "limit", "insert", "update"):
        getattr(m, method).return_value = m
    m.execute.return_value = MagicMock(data=data or [])
    return m


class TestParseCircleCandidates(unittest.TestCase):
    def test_valid_candidate(self) -> None:
        out = parse_circle_candidates(
            {
                "circle_candidates": [
                    {
                        "circle_type": "fitness",
                        "circle_key": "spin_class",
                        "raw_phrase": "my Tuesday spin class",
                        "confidence": 0.9,
                    }
                ]
            }
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].circle_key, "spin_class")
        self.assertEqual(out[0].circle_type, "fitness")

    def test_bad_circle_type_dropped(self) -> None:
        out = parse_circle_candidates(
            {"circle_candidates": [{"circle_type": "gym", "circle_key": "gym"}]}
        )
        self.assertEqual(out, [])

    def test_slug_fallback_from_phrase(self) -> None:
        out = parse_circle_candidates(
            {
                "circle_candidates": [
                    {
                        "circle_type": "faith",
                        "circle_key": "NOT A SLUG!!",
                        "raw_phrase": "our church downtown",
                    }
                ]
            }
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].circle_key, "church_downtown")

    def test_dedupes_same_key(self) -> None:
        cand = {"circle_type": "hobby", "circle_key": "book_club", "raw_phrase": "book club"}
        out = parse_circle_candidates({"circle_candidates": [cand, dict(cand)]})
        self.assertEqual(len(out), 1)

    def test_non_dict_input(self) -> None:
        self.assertEqual(parse_circle_candidates(None), [])
        self.assertEqual(parse_circle_candidates({"circle_candidates": "nope"}), [])


class TestParsePlaceFeatureCandidates(unittest.TestCase):
    def test_valid_feature(self) -> None:
        out = parse_place_feature_candidates(
            {
                "place_feature_candidates": [
                    {
                        "circle_key": "spin_class",
                        "key": "has_pool",
                        "value": "true",
                        "confidence": 0.8,
                    }
                ]
            }
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].key, "has_pool")
        self.assertEqual(out[0].sub_group, "")

    def test_bad_key_dropped(self) -> None:
        out = parse_place_feature_candidates(
            {
                "place_feature_candidates": [
                    {"circle_key": "spin_class", "key": "Has Pool?"},
                    {"circle_key": "??", "key": "has_pool"},
                ]
            }
        )
        self.assertEqual(out, [])


class TestPersistCircleCandidates(unittest.TestCase):
    @patch("app.circles_capture._embed_circle", return_value=None)
    @patch("app.circles_capture._fetch_affiliation", return_value=None)
    @patch("app.circles_capture.service_client")
    def test_new_candidate_inserts_suggested(self, sb, _fetch, _embed) -> None:
        table = _chain()
        sb.return_value.table.return_value = table
        n = persist_circle_candidates(
            "u1",
            [CircleCandidate("fitness", "spin_class", "my spin class", 0.9)],
        )
        self.assertEqual(n, 1)
        row = table.insert.call_args[0][0]
        self.assertEqual(row["status"], "suggested")
        self.assertEqual(row["source"], "chat_extraction")
        self.assertNotIn("place_ref", row)

    @patch(
        "app.circles_capture._fetch_affiliation",
        return_value={"id": "a1", "confidence": 0.7, "status": "confirmed"},
    )
    @patch("app.circles_capture.service_client")
    def test_re_mention_corroborates(self, sb, _fetch) -> None:
        table = _chain()
        sb.return_value.table.return_value = table
        n = persist_circle_candidates(
            "u1", [CircleCandidate("fitness", "spin_class", "spin", 0.6)]
        )
        self.assertEqual(n, 1)
        table.insert.assert_not_called()
        patch_row = table.update.call_args[0][0]
        self.assertAlmostEqual(patch_row["confidence"], 0.75)
        # Never promotes/demotes status or touches place_ref from capture.
        self.assertEqual(set(patch_row), {"confidence"})

    @patch("app.circles_capture.service_client")
    def test_low_confidence_skipped(self, sb) -> None:
        n = persist_circle_candidates(
            "u1", [CircleCandidate("fitness", "spin_class", "spin", 0.4)]
        )
        self.assertEqual(n, 0)
        sb.return_value.table.assert_not_called()


class TestPersistPlaceFeatures(unittest.TestCase):
    @patch("app.circles_capture.upsert_place_feature", return_value=True)
    @patch(
        "app.circles_capture._fetch_affiliation",
        return_value={"id": "a1", "place_ref": "p1", "status": "confirmed"},
    )
    @patch("app.circles_capture.service_client")
    def test_grounded_confirmed_writes_place_feature(self, sb, _fetch, upsert) -> None:
        n = persist_place_feature_candidates(
            "u1", [PlaceFeatureCandidate("spin_class", "has_pool", "true", "", 0.8)]
        )
        self.assertEqual(n, 1)
        kwargs = upsert.call_args.kwargs
        self.assertEqual(kwargs["place_id"], "p1")
        self.assertEqual(kwargs["contributed_by"], "u1")
        self.assertEqual(kwargs["source"], "rapport")

    @patch(
        "app.circles_capture._fetch_affiliation",
        return_value={"id": "a1", "place_ref": None, "status": "suggested", "detail": "my spin class"},
    )
    @patch("app.circles_capture.service_client")
    def test_ungrounded_folds_into_detail(self, sb, _fetch) -> None:
        table = _chain()
        sb.return_value.table.return_value = table
        n = persist_place_feature_candidates(
            "u1", [PlaceFeatureCandidate("spin_class", "has_pool", "true", "", 0.8)]
        )
        self.assertEqual(n, 1)
        patch_row = table.update.call_args[0][0]
        self.assertIn("has_pool=true", patch_row["detail"])
        self.assertIn("my spin class", patch_row["detail"])

    @patch("app.circles_capture._fetch_affiliation", return_value=None)
    @patch("app.circles_capture.service_client")
    def test_no_affiliation_drops_feature(self, sb, _fetch) -> None:
        n = persist_place_feature_candidates(
            "u1", [PlaceFeatureCandidate("spin_class", "has_pool", "true", "", 0.8)]
        )
        self.assertEqual(n, 0)


class TestUpsertPlaceFeature(unittest.TestCase):
    @patch("app.circles_capture.service_client")
    def test_owner_row_immune_to_rapport(self, sb) -> None:
        table = _chain([{"id": "f1", "confidence": 0.5, "source": "owner"}])
        sb.return_value.table.return_value = table
        ok = upsert_place_feature(
            place_id="p1", key="has_pool", value="false", confidence=0.9, source="rapport"
        )
        self.assertFalse(ok)
        table.update.assert_not_called()

    @patch("app.circles_capture.service_client")
    def test_lower_confidence_does_not_overwrite(self, sb) -> None:
        table = _chain([{"id": "f1", "confidence": 0.9, "source": "rapport"}])
        sb.return_value.table.return_value = table
        ok = upsert_place_feature(
            place_id="p1", key="has_pool", value="false", confidence=0.6, source="rapport"
        )
        self.assertFalse(ok)
        table.update.assert_not_called()

    @patch("app.circles_capture.service_client")
    def test_equal_or_higher_confidence_overwrites(self, sb) -> None:
        table = _chain([{"id": "f1", "confidence": 0.6, "source": "rapport"}])
        sb.return_value.table.return_value = table
        ok = upsert_place_feature(
            place_id="p1", key="has_pool", value="50m lap pool", confidence=0.6, source="rapport"
        )
        self.assertTrue(ok)
        patch_row = table.update.call_args[0][0]
        self.assertEqual(patch_row["value"], "50m lap pool")

    @patch("app.circles_capture.service_client")
    def test_new_feature_inserts(self, sb) -> None:
        table = _chain([])
        sb.return_value.table.return_value = table
        ok = upsert_place_feature(place_id="p1", key="has_sauna", value="true")
        self.assertTrue(ok)
        row = table.insert.call_args[0][0]
        self.assertEqual(row["sub_group"], "")


if __name__ == "__main__":
    unittest.main()
