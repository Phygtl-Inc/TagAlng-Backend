import unittest
from unittest.mock import MagicMock, patch

from app.circles_flow import (
    _advisory_place_type,
    _flush_parked_features,
    add_circle,
    ground_affiliation,
    ground_options,
    list_my_circles,
    tag_claim_place_from_gap,
    upsert_canonical_place,
)


def _chain(data=None, count=None):
    m = MagicMock()
    for method in ("select", "eq", "is_", "in_", "limit", "order", "insert", "update"):
        getattr(m, method).return_value = m
    m.execute.return_value = MagicMock(data=data or [], count=count)
    return m


def _sb_with_tables(tables: dict):
    sb = MagicMock()
    sb.table.side_effect = lambda name: tables[name]
    return sb


_DETAILS = {
    "place_id": "gpid1",
    "name": "OrangeTheory Narcoossee",
    "address": "123 Narcoossee Rd",
    "lat": 28.4,
    "lng": -81.2,
    "zip": "32827",
    "types": ["gym", "point_of_interest"],
}


class TestAdvisoryPlaceType(unittest.TestCase):
    def test_google_type_wins(self) -> None:
        self.assertEqual(_advisory_place_type(["gym"], "hobby"), "fitness")
        self.assertEqual(_advisory_place_type(["mosque"], None), "faith")

    def test_falls_back_to_hint(self) -> None:
        self.assertEqual(_advisory_place_type(["point_of_interest"], "hobby"), "hobby")
        self.assertIsNone(_advisory_place_type(["point_of_interest"], "not_a_type"))


class TestUpsertCanonicalPlace(unittest.TestCase):
    @patch("app.circles_flow.service_client")
    def test_existing_place_refreshes_google_fields_only(self, sb) -> None:
        table = _chain([{"id": "p1"}])
        sb.return_value.table.return_value = table
        place_id = upsert_canonical_place(_DETAILS, circle_type_hint="fitness", created_by="u1")
        self.assertEqual(place_id, "p1")
        patch_row = table.update.call_args[0][0]
        self.assertEqual(
            set(patch_row), {"name", "address", "lat", "lng", "zip"}
        )  # never created_by / source / place_type / claimed_by

    @patch("app.circles_flow.service_client")
    def test_new_place_seeds_first_grounder(self, sb) -> None:
        table = _chain()
        # select -> no rows; insert -> returns the new row
        table.execute.side_effect = [
            MagicMock(data=[]),
            MagicMock(data=[{"id": "p2"}]),
        ]
        sb.return_value.table.return_value = table
        place_id = upsert_canonical_place(_DETAILS, circle_type_hint="fitness", created_by="u1")
        self.assertEqual(place_id, "p2")
        row = table.insert.call_args[0][0]
        self.assertEqual(row["source"], "user_grounded")
        self.assertEqual(row["created_by"], "u1")
        self.assertEqual(row["place_type"], "fitness")
        self.assertNotIn("claimed_by", row)

    def test_rejects_missing_fields(self) -> None:
        self.assertIsNone(upsert_canonical_place({"place_id": "", "name": "x"}))
        self.assertIsNone(upsert_canonical_place({"place_id": "g", "name": ""}))


class TestGroundAffiliation(unittest.TestCase):
    @patch("app.circles_flow._own_affiliation", return_value=None)
    def test_unknown_affiliation_raises(self, _own) -> None:
        with self.assertRaises(ValueError):
            ground_affiliation("u1", "a1", "gpid1")

    @patch("app.places.place_details", return_value=None)
    @patch(
        "app.circles_flow._own_affiliation",
        return_value={"id": "a1", "circle_type": "fitness", "detail": None},
    )
    def test_place_details_failure_raises(self, _own, _details) -> None:
        with self.assertRaises(ValueError):
            ground_affiliation("u1", "a1", "gpid1")

    @patch("app.rapport_gaps.open_semantic_gap")
    @patch("app.circles_flow._place_affinity_question", return_value=("Q?", "about X…"))
    @patch("app.circles_flow._flush_parked_features", return_value=0)
    @patch("app.circles_flow.upsert_canonical_place", return_value="p1")
    @patch("app.places.place_details", return_value=dict(_DETAILS))
    @patch(
        "app.circles_flow._own_affiliation",
        return_value={"id": "a1", "circle_type": "fitness", "detail": "my gym"},
    )
    @patch("app.circles_flow.service_client")
    def test_success_confirms_and_opens_tagged_gap(
        self, sb, _own, _details, _upsert, _flush, _q, open_gap
    ) -> None:
        table = _chain()
        sb.return_value.table.return_value = table
        result = ground_affiliation("u1", "a1", "gpid1")
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(result["place_id"], "p1")
        patch_row = table.update.call_args[0][0]
        self.assertEqual(patch_row, {"place_ref": "p1", "status": "confirmed"})
        self.assertEqual(open_gap.call_args.kwargs.get("place_ref"), "p1")

    @patch("app.rapport_gaps.mark_answered")
    @patch("app.rapport_gaps.open_semantic_gap")
    @patch("app.circles_flow._place_affinity_question", return_value=("Q?", "about X…"))
    @patch("app.circles_flow._flush_parked_features", return_value=0)
    @patch("app.circles_flow.upsert_canonical_place", return_value="p1")
    @patch("app.places.place_details", return_value=dict(_DETAILS))
    @patch(
        "app.circles_flow._own_affiliation",
        return_value={"id": "a1", "circle_type": "fitness", "detail": "my gym"},
    )
    @patch("app.circles_flow.service_client")
    def test_grounding_closes_the_which_spot_ask(
        self, sb, _own, _details, _upsert, _flush, _q, _open_gap, mark_answered
    ) -> None:
        # Pinning through any path closes the open ask, or the tile re-asks for a
        # place already on the profile (FE ask #3, issues #63).
        table = _chain([{"gap_row_id": "g7"}])
        sb.return_value.table.return_value = table
        ground_affiliation("u1", "a1", "gpid1")
        mark_answered.assert_called_once_with("g7")


class TestFlushParkedFeatures(unittest.TestCase):
    @patch("app.circles_flow.upsert_place_feature", return_value=True)
    @patch("app.circles_flow.service_client")
    def test_moves_notes_to_place_and_cleans_detail(self, sb, upsert) -> None:
        table = _chain()
        sb.return_value.table.return_value = table
        aff = {"id": "a1", "detail": "my spin class; has_pool=true; has_sauna=yes"}
        n = _flush_parked_features("u1", aff, "p1")
        self.assertEqual(n, 2)
        keys = [c.kwargs["key"] for c in upsert.call_args_list]
        self.assertEqual(keys, ["has_pool", "has_sauna"])
        cleaned = table.update.call_args[0][0]["detail"]
        self.assertEqual(cleaned, "my spin class")

    def test_no_detail_no_writes(self) -> None:
        self.assertEqual(_flush_parked_features("u1", {"id": "a1", "detail": ""}, "p1"), 0)


class TestGroundOptions(unittest.TestCase):
    @patch("app.places.search_places")
    def test_unnamed_circle_searches_the_type_keyword(self, search) -> None:
        # No venue name to go on ("my spin class") — the type keyword is all we
        # have, and its results are suggestions, never the user's own spot.
        search.return_value = [
            {"name": "OrangeTheory", "address": "addr", "place_id": "g1"},
            {"name": "NoId", "address": "addr", "place_id": ""},
        ]
        aff = {
            "id": "a1",
            "circle_type": "fitness",
            "detail": "my spin class; has_pool=true",
            "place_name": "",
        }
        options = ground_options("u1", aff, block_id="b1")
        self.assertEqual(search.call_args.kwargs["query"], "gym")
        self.assertEqual(search.call_args.kwargs["included_type"], "gym")
        self.assertEqual(len(options), 1)
        self.assertEqual(options[0]["google_place_id"], "g1")
        self.assertTrue(options[0]["suggested"])

    @patch("app.places.search_places", return_value=[])
    def test_query_override(self, search) -> None:
        aff = {"id": "a1", "circle_type": "faith", "detail": "our church"}
        ground_options("u1", aff, block_id="b1", query="St. Luke's")
        self.assertEqual(search.call_args.kwargs["query"], "St. Luke's")


class TestAddCircle(unittest.TestCase):
    def test_invalid_type_raises(self) -> None:
        with self.assertRaises(ValueError):
            add_circle("u1", circle_type="gym", google_place_id="g1")

    def test_profile_add_without_place_raises(self) -> None:
        # A community's place is mandatory: a profile add can never create one
        # without a real location.
        with self.assertRaises(ValueError) as ctx:
            add_circle("u1", circle_type="hobby", detail="book club")
        self.assertEqual(str(ctx.exception), "place_required")
        with self.assertRaises(ValueError):
            add_circle("u1", circle_type="hobby", detail="book club", google_place_id="  ")

    @patch(
        "app.circles_flow.ground_affiliation",
        return_value={
            "affiliation_id": "a9",
            "place_id": "p1",
            "place_name": "OrangeTheory",
            "status": "confirmed",
        },
    )
    @patch("app.circles_flow.service_client")
    def test_profile_add_grounds_and_confirms_in_one_step(self, sb, ground) -> None:
        table = _chain()
        table.execute.side_effect = [
            MagicMock(data=[]),  # no existing
            MagicMock(data=[{"id": "a9"}]),  # insert
        ]
        sb.return_value.table.return_value = table
        result = add_circle(
            "u1", circle_type="hobby", detail="book club", google_place_id="g1"
        )
        self.assertEqual(result["status"], "confirmed")
        ground.assert_called_once_with("u1", "a9", "g1")
        row = table.insert.call_args[0][0]
        self.assertEqual(row["source"], "profile_add")
        self.assertEqual(row["circle_key"], "book_club")
        self.assertEqual(row["status"], "suggested")

    @patch("app.circles_flow.service_client")
    def test_invite_self_confirm_still_parks_placeless_candidate(self, sb) -> None:
        # §A.2: the joiner grounds her OWN place right after — until then the row
        # is an internal candidate, not a community, and shows nowhere.
        table = _chain()
        table.execute.side_effect = [
            MagicMock(data=[]),  # no existing
            MagicMock(data=[{"id": "a9"}]),  # insert
        ]
        sb.return_value.table.return_value = table
        result = add_circle(
            "u1", circle_type="hobby", detail="book club", source="invite_confirmed"
        )
        self.assertEqual(
            result, {"affiliation_id": "a9", "status": "suggested", "grounded": False}
        )
        row = table.insert.call_args[0][0]
        self.assertEqual(row["source"], "invite_confirmed")


class TestListMyCircles(unittest.TestCase):
    @patch("app.circles_flow._member_count", return_value=3)
    @patch("app.circles_flow.service_client")
    def test_only_grounded_rows_are_communities(self, sb, _count) -> None:
        affs = _chain(
            [
                {
                    "id": "a1",
                    "circle_type": "fitness",
                    "circle_key": "gym",
                    "detail": "my gym",
                    "status": "confirmed",
                    "place_ref": "p1",
                    "created_at": "2026-07-01",
                }
            ]
        )
        affs.not_ = affs  # .not_.is_ chains back through the same stub
        places = _chain([{"id": "p1", "name": "OrangeTheory", "address": "123 Elm"}])
        sb.return_value = _sb_with_tables(
            {"circle_affiliations": affs, "places": places}
        )
        rows = list_my_circles("u1")
        # The query itself excludes ungrounded rows — a community without a place
        # does not exist, so it can never reach the profile surface.
        self.assertIn(("place_ref", "null"), [c.args for c in affs.is_.call_args_list])
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["grounded"])
        self.assertEqual(rows[0]["place_name"], "OrangeTheory")


class TestTagClaimPlace(unittest.TestCase):
    @patch("app.circles_flow.service_client")
    @patch("app.rapport_gaps.get_gap_row", return_value={"place_ref": "p1"})
    def test_stamps_place_ref(self, _gap, sb) -> None:
        table = _chain()
        sb.return_value.table.return_value = table
        tag_claim_place_from_gap("g1", "c1")
        self.assertEqual(table.update.call_args[0][0], {"place_ref": "p1"})

    @patch("app.circles_flow.service_client")
    @patch("app.rapport_gaps.get_gap_row", return_value={"place_ref": None})
    def test_no_place_no_write(self, _gap, sb) -> None:
        tag_claim_place_from_gap("g1", "c1")
        sb.return_value.table.assert_not_called()


if __name__ == "__main__":
    unittest.main()
