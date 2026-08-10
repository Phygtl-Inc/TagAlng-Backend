import unittest
from unittest.mock import MagicMock, patch

from app.place_activities import (
    MAX_ACTIVITIES_PER_MEMBER,
    activities_for_places,
    add_activity,
    add_feature,
    remove_feature,
)


def _chain(data=None):
    m = MagicMock()
    methods = ("select", "eq", "is_", "in_", "limit", "order", "insert", "update", "delete")
    for method in methods:
        getattr(m, method).return_value = m
    m.execute.return_value = MagicMock(data=data if data is not None else [])
    return m


class TestActivitiesRead(unittest.TestCase):
    @patch("app.place_activities.service_client")
    def test_groups_by_concept_and_marks_mine(self, sb) -> None:
        sb.return_value.table.return_value = _chain(
            [
                {"place_id": "p1", "concept": "aerobics", "label": "Aerobics", "user_id": "me"},
                {"place_id": "p1", "concept": "aerobics", "label": "Aerobics", "user_id": "u2"},
                {"place_id": "p1", "concept": "sauna", "label": "sauna", "user_id": "u2"},
            ]
        )
        rows = activities_for_places(["p1"], "me")["p1"]
        # Most-shared first, labels normalized for display.
        self.assertEqual([r["label"] for r in rows], ["Aerobics", "Sauna"])
        self.assertEqual(rows[0]["member_count"], 2)
        self.assertTrue(rows[0]["mine"])
        self.assertFalse(rows[1]["mine"])

    @patch("app.place_activities.service_client")
    def test_empty_place_still_keyed(self, sb) -> None:
        sb.return_value.table.return_value = _chain([])
        self.assertEqual(activities_for_places(["p1", "p2"], "me"), {"p1": [], "p2": []})


class TestAddActivity(unittest.TestCase):
    @patch("app.place_activities._mirror_claim")
    @patch("app.place_activities._resolve_place", return_value="p1")
    @patch("app.place_activities.service_client")
    def test_writes_row_and_mirrors_claim(self, sb, _resolve, mirror) -> None:
        table = _chain([])
        sb.return_value.table.return_value = table
        result = add_activity("me", label="  weight  lifting ", place_id="p1")
        row = table.insert.call_args[0][0]
        self.assertEqual(row["concept"], "weight_lifting")
        self.assertEqual(row["label"], "Weight lifting")
        self.assertFalse(result["already_there"])
        mirror.assert_called_once()

    @patch("app.place_activities._mirror_claim")
    @patch("app.place_activities._resolve_place", return_value="p1")
    @patch("app.place_activities.service_client")
    def test_duplicate_is_a_no_op(self, sb, _resolve, mirror) -> None:
        table = _chain([{"id": "a1", "concept": "aerobics"}])
        sb.return_value.table.return_value = table
        result = add_activity("me", label="Aerobics", place_id="p1")
        self.assertTrue(result["already_there"])
        table.insert.assert_not_called()
        mirror.assert_not_called()

    @patch("app.place_activities._resolve_place", return_value="p1")
    @patch("app.place_activities.service_client")
    def test_cap_is_enforced(self, sb, _resolve) -> None:
        sb.return_value.table.return_value = _chain(
            [{"id": str(i), "concept": f"a{i}"} for i in range(MAX_ACTIVITIES_PER_MEMBER)]
        )
        with self.assertRaises(ValueError) as ctx:
            add_activity("me", label="One more", place_id="p1")
        self.assertEqual(str(ctx.exception), "too_many_activities")

    @patch("app.place_activities._resolve_place", return_value="p1")
    @patch("app.place_activities.service_client")
    def test_unsluggable_label_rejected(self, sb, _resolve) -> None:
        with self.assertRaises(ValueError) as ctx:
            add_activity("me", label="!!!", place_id="p1")
        self.assertEqual(str(ctx.exception), "label_required")


class TestFeatures(unittest.TestCase):
    @patch("app.place_activities.feature_emoji", return_value="🏊")
    @patch("app.place_activities.upsert_place_feature", return_value=True)
    @patch("app.place_activities._resolve_place", return_value="p1")
    def test_add_feature_prefixes_key(self, _resolve, upsert, _emoji) -> None:
        result = add_feature("me", label="Pool", place_id="p1")
        self.assertEqual(result["key"], "has_pool")
        self.assertEqual(upsert.call_args.kwargs["emoji"], "🏊")
        self.assertEqual(upsert.call_args.kwargs["contributed_by"], "me")

    @patch("app.place_activities.feature_emoji", return_value="")
    @patch("app.place_activities.upsert_place_feature", return_value=True)
    @patch("app.place_activities._resolve_place", return_value="p1")
    def test_existing_prefix_not_doubled(self, _resolve, upsert, _emoji) -> None:
        self.assertEqual(add_feature("me", label="has childcare", place_id="p1")["key"],
                         "has_childcare")

    @patch("app.place_activities._resolve_place", return_value="p1")
    @patch("app.place_activities.service_client")
    def test_remove_someone_elses_feature_refused(self, sb, _resolve) -> None:
        table = _chain([{"id": "f1", "contributed_by": "other"}])
        sb.return_value.table.return_value = table
        with self.assertRaises(ValueError) as ctx:
            remove_feature("me", key="has_pool", place_id="p1")
        self.assertEqual(str(ctx.exception), "not_yours")
        table.delete.assert_not_called()

    @patch("app.place_activities._resolve_place", return_value="p1")
    @patch("app.place_activities.service_client")
    def test_remove_own_feature(self, sb, _resolve) -> None:
        table = _chain([{"id": "f1", "contributed_by": "me"}])
        sb.return_value.table.return_value = table
        remove_feature("me", key="has_pool", place_id="p1")
        table.delete.assert_called_once()


if __name__ == "__main__":
    unittest.main()
