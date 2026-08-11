import unittest
from unittest.mock import MagicMock, patch

from app.community_surface import (
    _feature_label,
    _shared_line,
    _status_line,
    caller_affiliation_at,
    communities_card,
    community_members,
    community_profile,
    place_features,
    stamp_communities_card,
)


def _chain(data=None, count=None):
    m = MagicMock()
    for method in (
        "select",
        "eq",
        "neq",
        "is_",
        "in_",
        "or_",
        "gte",
        "lte",
        "limit",
        "order",
        "range",
    ):
        getattr(m, method).return_value = m
    m.execute.return_value = MagicMock(data=data if data is not None else [], count=count)
    return m


def _sb(tables: dict, rpc_data=None):
    sb = MagicMock()
    sb.table.side_effect = lambda name: tables.get(name, _chain([]))
    sb.rpc.return_value = MagicMock(
        execute=MagicMock(return_value=MagicMock(data=rpc_data if rpc_data is not None else []))
    )
    return sb


_CIRCLES = [
    {
        "id": "a1",
        "circle_type": "fitness",
        "status": "confirmed",
        "grounded": True,
        "place_id": "p1",
        "place_name": "OrangeTheory Narcoossee",
        "place_address": "9145 Narcoossee Rd",
        "detail": None,
        "member_count": 34,
        "active": True,
        "added_at": "2026-07-01T00:00:00Z",
    },
    {
        "id": "a2",
        "circle_type": "faith",
        "status": "confirmed",
        "grounded": True,
        "place_id": "p2",
        "place_name": "St. Luke's",
        "place_address": None,
        "detail": None,
        "member_count": 12,
        "active": True,
        "added_at": "2026-06-01T00:00:00Z",
    },
    {
        "id": "a3",
        "circle_type": "hobby",
        "status": "confirmed",
        "grounded": True,
        "place_id": "p3",
        "place_name": "Lake Nona Art Studio",
        "place_address": None,
        "detail": None,
        "member_count": 9,
        "active": True,
        "added_at": "2026-05-01T00:00:00Z",
    },
    {
        "id": "a4",
        "circle_type": "other",
        "status": "confirmed",
        "grounded": True,
        "place_id": "p4",
        "place_name": "Corner Café",
        "place_address": None,
        "detail": None,
        "member_count": 1,
        "active": False,
        "added_at": "2026-04-01T00:00:00Z",
    },
]


class TestStatusLine(unittest.TestCase):
    def test_alone_is_said_plainly(self) -> None:
        self.assertEqual(_status_line(1, 0), "just you so far")
        self.assertEqual(_status_line(0, 0), "just you so far")

    def test_people_and_meets(self) -> None:
        self.assertEqual(_status_line(34, 3), "34 people · 3 meets this week")
        self.assertEqual(_status_line(34, 1), "34 people · 1 meet this week")
        self.assertEqual(_status_line(12, 0), "12 people")


class TestCommunitiesCard(unittest.TestCase):
    @patch("app.community_surface.meets_this_week", return_value=3)
    @patch("app.circles_flow.list_my_circles")
    def test_top_three_and_more_count(self, circles, _meets) -> None:
        circles.return_value = list(_CIRCLES)
        card = communities_card("u1")
        self.assertEqual(len(card["items"]), 3)
        self.assertEqual(card["total"], 4)
        self.assertEqual(card["more_count"], 1)
        # Liveliest first, and the row carries what the profile endpoint needs.
        self.assertEqual(card["items"][0]["place_name"], "OrangeTheory Narcoossee")
        self.assertEqual(card["items"][0]["place_id"], "p1")
        self.assertEqual(card["items"][0]["status_line"], "34 people · 3 meets this week")
        self.assertEqual(card["items"][0]["relation"], "gym")

    @patch("app.circles_flow.list_my_circles", return_value=[])
    def test_no_communities_means_no_card(self, _circles) -> None:
        self.assertIsNone(communities_card("u1"))

    @patch("app.circles_flow.list_my_circles", return_value=[])
    def test_stamp_leaves_ctx_untouched_when_empty(self, _circles) -> None:
        ctx: dict = {}
        stamp_communities_card(ctx, "u1")
        self.assertNotIn("communities_card", ctx)

    @patch("app.circles_flow.list_my_circles", side_effect=RuntimeError("db down"))
    def test_stamp_never_raises(self, _circles) -> None:
        ctx: dict = {}
        stamp_communities_card(ctx, "u1")
        self.assertEqual(ctx, {})


class TestMembership(unittest.TestCase):
    @patch("app.community_surface.service_client")
    def test_non_member_cannot_read_a_profile(self, sb) -> None:
        sb.return_value = _sb({"circle_affiliations": _chain([])})
        with self.assertRaises(ValueError) as err:
            community_profile("u1", place_id="p1")
        self.assertEqual(str(err.exception), "not_a_member")

    @patch("app.community_surface.service_client")
    def test_non_member_cannot_list_members(self, sb) -> None:
        sb.return_value = _sb({"circle_affiliations": _chain([])})
        with self.assertRaises(ValueError) as err:
            community_members("u1", place_id="p1")
        self.assertEqual(str(err.exception), "not_a_member")

    @patch("app.community_surface.service_client")
    def test_membership_predicate_is_confirmed_and_live(self, sb) -> None:
        affs = _chain([{"id": "a1", "circle_type": "fitness"}])
        sb.return_value = _sb({"circle_affiliations": affs})
        self.assertIsNotNone(caller_affiliation_at("u1", "p1"))
        eq_args = [c.args for c in affs.eq.call_args_list]
        self.assertIn(("status", "confirmed"), eq_args)
        self.assertIn(("place_ref", "p1"), eq_args)
        self.assertIn(("dismissed_at", "null"), [c.args for c in affs.is_.call_args_list])


class TestFeatures(unittest.TestCase):
    def test_label_humanizes_the_key(self) -> None:
        self.assertEqual(_feature_label("has_pool", None, ""), "Pool")
        self.assertEqual(_feature_label("has_childcare", "true", ""), "Childcare")
        self.assertEqual(_feature_label("class_schedule", "full", ""), "Class schedule: full")
        self.assertEqual(_feature_label("has_pool", None, "toddler_swim"), "Pool (toddler swim)")

    @patch("app.community_surface.service_client")
    def test_low_confidence_features_are_not_repeated(self, sb) -> None:
        rows = [
            {"key": "has_pool", "value": None, "sub_group": "", "confidence": 0.9},
            {"key": "has_sauna", "value": None, "sub_group": "", "confidence": 0.2},
        ]
        sb.return_value = _sb({"place_features": _chain(rows)})
        labels = [f["label"] for f in place_features("p1")]
        self.assertEqual(labels, ["Pool"])


class TestSharedLine(unittest.TestCase):
    def test_shared_threads_are_named(self) -> None:
        self.assertEqual(
            _shared_line(["Runner", "Toddler stage"], "gym"),
            "You both: Runner · Toddler stage",
        )

    def test_nothing_shared_claims_only_the_place(self) -> None:
        # The one fact that IS true of every row here — never an invented affinity.
        self.assertEqual(_shared_line([], "gym"), "You both go to this gym")


class TestCommunityMembers(unittest.TestCase):
    def _tables(self):
        return {
            "circle_affiliations": _chain(
                [
                    {"id": "a1", "circle_type": "fitness", "user_id": "u1"},
                    {"user_id": "u2", "circle_type": "fitness", "created_at": "2026-01-01"},
                    {"user_id": "u3", "circle_type": "fitness", "created_at": "2026-02-01"},
                    {"user_id": "blocked", "circle_type": "fitness", "created_at": "2026-03-01"},
                ]
            ),
            "places": _chain([{"id": "p1", "name": "OrangeTheory", "place_type": "fitness"}]),
            "users": _chain(
                [
                    {"id": "u2", "nickname": "mapleluz", "profile_photo_url": "http://a/2.png"},
                    {"id": "u3", "nickname": "coral88", "profile_photo_url": None},
                ]
            ),
            "user_blocks": _chain([{"blocker": "u1", "blocked": "blocked"}]),
        }

    @patch("app.community_surface.service_client")
    def test_rows_carry_shared_threads_and_a_nudge(self, sb) -> None:
        sb.return_value = _sb(
            self._tables(),
            rpc_data=[
                {
                    "peer_user_id": "u2",
                    "shared_concept_labels": ["Gardens", "Runs"],
                    "shared_concept_count": 2,
                }
            ],
        )
        out = community_members("u1", place_id="p1")
        rows = {r["peer_user_id"]: r for r in out["members"]}
        self.assertEqual(rows["u2"]["shared_line"], "You both: Gardens · Runs")
        self.assertEqual(rows["u2"]["actions"][0]["id"], "peer_card_nudge")
        # No shared concepts → the honest place-only line, and still no fake score.
        self.assertEqual(rows["u3"]["shared_line"], "You both go to this gym")
        for row in out["members"]:
            for invented in ("match_stars", "match_band", "match_badge", "similarity_score"):
                self.assertNotIn(invented, row)

    @patch("app.community_surface.service_client")
    def test_self_and_blocked_are_absent(self, sb) -> None:
        sb.return_value = _sb(self._tables())
        out = community_members("u1", place_id="p1")
        ids = [r["peer_user_id"] for r in out["members"]]
        self.assertEqual(sorted(ids), ["u2", "u3"])

    @patch("app.community_surface.service_client")
    def test_unverified_caller_gets_the_count_only(self, sb) -> None:
        sb.return_value = _sb(self._tables())
        out = community_members("u1", place_id="p1", phone_verified=False)
        self.assertEqual(out["members"], [])
        self.assertTrue(out["requires_phone_verification"])
        self.assertEqual(out["member_count"], 4)

    @patch("app.community_surface.service_client")
    def test_block_read_failure_hides_names(self, sb) -> None:
        tables = self._tables()
        failing = _chain([])
        failing.execute.side_effect = RuntimeError("no blocks table")
        tables["user_blocks"] = failing
        sb.return_value = _sb(tables)
        out = community_members("u1", place_id="p1")
        self.assertEqual(out["members"], [])


class TestCommunityProfile(unittest.TestCase):
    @patch("app.community_surface._blurb", return_value="A gym in 32827 — pool.")
    @patch("app.community_surface.service_client")
    def test_profile_shape_and_popular_ordering(self, sb, _blurb) -> None:
        events = _chain(
            [
                {"id": "e1", "title": "Saturday run", "starts_at": "2026-08-08T13:00:00", "has_time": True},
                {"id": "e2", "title": "Toddler swim", "starts_at": "2026-08-07T13:00:00", "has_time": True},
            ]
        )
        tables = {
            "circle_affiliations": _chain(
                [
                    {"id": "a1", "circle_type": "fitness", "user_id": "u1", "detail": None},
                    {"user_id": "u2", "circle_type": "fitness", "created_at": "2026-01-01"},
                ]
            ),
            "places": _chain(
                [
                    {
                        "id": "p1",
                        "name": "OrangeTheory",
                        "address": "9145 Narcoossee Rd",
                        "zip": "32827",
                        "google_place_id": "ChIJgoogle",
                        "lat": 28.4,
                        "lng": -81.2,
                    }
                ]
            ),
            "place_features": _chain(
                [{"key": "has_pool", "value": None, "sub_group": "", "confidence": 0.8}]
            ),
            "events": events,
            "event_requests": _chain([{"event_id": "e1"}, {"event_id": "e1"}, {"event_id": "e2"}]),
            "users": _chain([{"id": "u2", "nickname": "mapleluz", "profile_photo_url": None}]),
            "user_blocks": _chain([]),
        }
        sb.return_value = _sb(tables)
        out = community_profile("u1", place_id="p1")
        self.assertEqual(out["place_name"], "OrangeTheory")
        self.assertEqual([f["label"] for f in out["features"]], ["Pool"])
        self.assertEqual(out["description"], "A gym in 32827 — pool.")
        # Best-attended meet leads; the count is the real going roster.
        self.assertEqual(out["upcoming_events"][0]["event_id"], "e1")
        self.assertEqual(out["upcoming_events"][0]["going_count"], 2)
        # Hosting still goes through chat (one host implementation), but the venue is
        # stamped from here first — and `place_id` must be the GOOGLE id, since our
        # places.id would publish a meet whose place_ref never matches this community.
        self.assertEqual([a["id"] for a in out["actions"]], ["community_create_event"])
        self.assertEqual(out["create_event_venue"]["place_id"], "ChIJgoogle")
        self.assertEqual(out["create_event_venue"]["name"], "OrangeTheory")
        self.assertIn("circle_key", out)

    @patch("app.community_surface._blurb", return_value=None)
    @patch("app.community_surface.service_client")
    def test_unverified_caller_gets_no_ctas_or_faces(self, sb, _blurb) -> None:
        tables = {
            "circle_affiliations": _chain(
                [
                    {"id": "a1", "circle_type": "fitness", "user_id": "u1", "detail": None},
                    {"user_id": "u2", "circle_type": "fitness", "created_at": "2026-01-01"},
                ]
            ),
            "places": _chain([{"id": "p1", "name": "OrangeTheory"}]),
            "place_features": _chain([]),
            "events": _chain([]),
            "event_requests": _chain([]),
            "users": _chain([]),
            "user_blocks": _chain([]),
        }
        sb.return_value = _sb(tables)
        out = community_profile("u1", place_id="p1", phone_verified=False)
        self.assertEqual(out["actions"], [])
        self.assertIsNone(out["create_event_venue"])
        self.assertEqual(out["member_preview"], [])


class TestBlurbTruthfulness(unittest.TestCase):
    def test_template_floor_only_states_given_facts(self) -> None:
        from app.community_surface import _blurb

        with patch("app.orchestrator.llm.llm_configured", return_value=False):
            text = _blurb(
                place_name="OrangeTheory",
                relation="gym",
                area="Lake Nona",
                features=["Pool", "Childcare"],
                members=34,
            )
        self.assertEqual(text, "A gym in Lake Nona — pool, childcare.")

    def test_nothing_known_says_nothing(self) -> None:
        from app.community_surface import _blurb

        with patch("app.orchestrator.llm.llm_configured", return_value=False):
            self.assertIsNone(
                _blurb(place_name="X", relation="spot", area=None, features=[], members=1)
            )


class TestCreateEventCta(unittest.TestCase):
    def test_its_message_enters_hosting_without_the_classifier(self) -> None:
        """The CTA posts a normal chat message, so its WORDS decide the lane. Phrased as
        "host something at X" it read as a search and answered "there aren't any
        activities by them in your area" — a discovery result for a hosting button."""
        from app.discovery_route import looks_like_host_event_entry
        from app.ui_actions import community_profile_actions

        actions = community_profile_actions(place_name="St. Luke's", relation="church")
        self.assertTrue(looks_like_host_event_entry(actions[0]["message"]))


class TestEventsAtPlace(unittest.TestCase):
    """A community's upcoming list holds meets held HERE plus meets created FOR it (the
    setup card's community tag), and the two-column filter only ever sees a real uuid."""

    PID = "3f2a0c4e-1111-4222-8333-444455556666"

    @patch("app.community_surface.service_client")
    def test_uuid_place_matches_both_columns(self, sb) -> None:
        from app.community_surface import _events_at_place

        chain = _chain([{"id": "e1"}])
        sb.return_value = _sb({"events": chain})
        self.assertEqual(_events_at_place(self.PID, limit=5), [{"id": "e1"}])
        self.assertEqual(
            chain.or_.call_args.args[0],
            f"place_ref.eq.{self.PID},circle_place_ref.eq.{self.PID}",
        )

    @patch("app.community_surface.service_client")
    def test_non_uuid_never_reaches_the_or_filter(self, sb) -> None:
        from app.community_surface import _events_at_place

        chain = _chain([{"id": "e1"}])
        sb.return_value = _sb({"events": chain})
        _events_at_place("p1,circle_place_ref.not.is.null", limit=5)
        chain.or_.assert_not_called()
        chain.eq.assert_any_call("place_ref", "p1,circle_place_ref.not.is.null")


if __name__ == "__main__":
    unittest.main()
