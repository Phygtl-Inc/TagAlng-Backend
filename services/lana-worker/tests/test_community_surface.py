import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from app.community_surface import (
    _feature_label,
    community_meets,
    _member_attributes,
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


def _chain_seq(*datas):
    """A table read more than once in one call, answering differently each time."""
    m = _chain([])
    m.execute.side_effect = [MagicMock(data=d, count=None) for d in datas]
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


def _soon(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")


def _meet(eid: str, title: str, days: int) -> dict:
    return {
        "id": eid,
        "title": title,
        "starts_at": _soon(days),
        "has_time": True,
        "venue_name": "Laureate Park",
        "cover_emoji": "🏃",
    }


class TestCommunitiesCard(unittest.TestCase):
    @patch("app.community_surface._going_rosters", return_value={"e1": ["u2", "u3"]})
    @patch(
        "app.community_surface._events_at_place",
        side_effect=lambda pid, **_: [
            _meet("e1", "Saturday run", 1),
            _meet("e2", "Sunday spin meet", 2),
            _meet("e3", "Member social", 3),
        ]
        if pid == "p1"
        else [],
    )
    @patch("app.circles_flow.list_my_circles")
    def test_top_three_and_more_count(self, circles, _events, _rosters) -> None:
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
        # The card is about what's on: two meets listed, both openable, and the count
        # says what they are a slice of.
        self.assertEqual(
            [m["event_id"] for m in card["items"][0]["meets"]], ["e1", "e2"]
        )
        self.assertEqual(card["items"][0]["meets"][0]["title"], "Saturday run")
        self.assertEqual(card["items"][0]["upcoming_count"], 3)
        # A real going figure, not the hard 0 the field used to ship (#97a).
        self.assertEqual(card["items"][0]["meets"][0]["going_count"], 2)
        # Nothing coming up at the others — listed, with no meets invented.
        self.assertEqual(card["items"][1]["meets"], [])

    @patch(
        "app.community_surface._events_at_place",
        side_effect=lambda pid, **_: [_meet("e9", "Latte morning", 1)] if pid == "p4" else [],
    )
    @patch("app.circles_flow.list_my_circles")
    def test_a_place_with_a_meet_outranks_a_bigger_one_without(self, circles, _events) -> None:
        circles.return_value = list(_CIRCLES)
        card = communities_card("u1")
        self.assertEqual(card["items"][0]["place_name"], "Corner Café")
        # The rest keep the liveliest-first order behind it.
        self.assertEqual(
            [i["place_name"] for i in card["items"][1:]],
            ["OrangeTheory Narcoossee", "St. Luke's"],
        )

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


class TestCommunityMeets(unittest.TestCase):
    """C-CIRCLE-COMMS-ALL: every meet across her communities, outside a turn."""

    def _events(self, pid: str, **_) -> list[dict]:
        if pid != "p1":
            return []
        return [
            {
                "id": "e1",
                "title": "Saturday run",
                "description": "Easy 5k, all paces welcome.",
                "starts_at": _soon(1),
                "has_time": True,
                "venue_name": "Laureate Park",
                "cover_emoji": "🏃",
            },
            {
                "id": "e2",
                "title": "Sunday spin meet",
                "starts_at": _soon(2),
                "has_time": True,
                "venue_name": "Studio A",
                "cover_emoji": "🚴",
            },
            {"id": "e3", "title": "Member social", "starts_at": _soon(5), "has_time": True},
        ]

    _ROSTERS = {"e1": ["u2", "u3", "u4", "u5", "u6", "u7"], "e2": ["u2"]}

    @patch("app.community_surface._users_by_id")
    @patch("app.community_surface._blocked_ids", return_value=set())
    @patch("app.community_surface._going_rosters")
    @patch("app.circles_flow.list_my_circles")
    def test_one_group_per_place_with_something_on(self, circles, rosters, _blocked, users) -> None:
        circles.return_value = list(_CIRCLES)
        rosters.return_value = dict(self._ROSTERS)
        users.return_value = {
            uid: {"nickname": uid, "profile_photo_url": None} for uid in self._ROSTERS["e1"]
        }
        with patch("app.community_surface._events_at_place", side_effect=self._events):
            out = community_meets("u1")
        # Three of her four communities have nothing on, so they are not groups here.
        self.assertEqual(len(out["communities"]), 1)
        # `total` stays every community she holds — "1 of your 4 has something on".
        self.assertEqual(out["total"], 4)
        group = out["communities"][0]
        self.assertEqual(group["place_name"], "OrangeTheory Narcoossee")
        self.assertEqual(group["upcoming_count"], 3)
        # A week being scanned: dates ascending, not popularity — e3 has nobody going
        # and still comes last because it is last.
        self.assertEqual([m["event_id"] for m in group["meets"]], ["e1", "e2", "e3"])
        # Real counts, and the avatar stack capped without lying about the count.
        self.assertEqual(group["meets"][0]["going_count"], 6)
        self.assertEqual(len(group["meets"][0]["going_preview"]), 5)
        self.assertEqual(group["meets"][0]["going_preview"][0]["user_id"], "u2")
        self.assertEqual(group["meets"][0]["description"], "Easy 5k, all paces welcome.")
        # A host who wrote no copy gets no invented line.
        self.assertIsNone(group["meets"][1]["description"])
        self.assertEqual(group["meets"][2]["going_count"], 0)
        self.assertEqual(group["meets"][2]["going_preview"], [])

    @patch("app.community_surface._users_by_id")
    @patch("app.community_surface._blocked_ids", return_value=set())
    @patch("app.community_surface._going_rosters")
    @patch("app.circles_flow.list_my_circles")
    def test_readable_twice_without_a_turn(self, circles, rosters, _blocked, users) -> None:
        circles.return_value = list(_CIRCLES)
        rosters.return_value = dict(self._ROSTERS)
        users.return_value = {"u2": {"nickname": "Priya", "profile_photo_url": None}}
        with patch("app.community_surface._events_at_place", side_effect=self._events):
            first = community_meets("u1")
            second = community_meets("u1")
        # No turn state in it at all — the same payload, twice in a row.
        self.assertEqual(first, second)

    @patch("app.community_surface._blocked_ids", return_value=set())
    @patch("app.community_surface._going_rosters")
    @patch("app.circles_flow.list_my_circles")
    def test_unverified_caller_gets_counts_but_no_faces(self, circles, rosters, _blocked) -> None:
        circles.return_value = list(_CIRCLES)
        rosters.return_value = dict(self._ROSTERS)
        with patch("app.community_surface._events_at_place", side_effect=self._events):
            out = community_meets("u1", phone_verified=False)
        meet = out["communities"][0]["meets"][0]
        self.assertEqual(meet["going_count"], 6)
        self.assertEqual(meet["going_preview"], [])

    @patch("app.community_surface._users_by_id", return_value={})
    @patch("app.community_surface._blocked_ids", return_value={"u2", "u3", "u4", "u5", "u6", "u7"})
    @patch("app.community_surface._going_rosters")
    @patch("app.circles_flow.list_my_circles")
    def test_a_blocked_pair_never_shows_a_face(self, circles, rosters, _blocked, _users) -> None:
        circles.return_value = list(_CIRCLES)
        rosters.return_value = dict(self._ROSTERS)
        with patch("app.community_surface._events_at_place", side_effect=self._events):
            out = community_meets("u1")
        self.assertEqual(out["communities"][0]["meets"][0]["going_preview"], [])

    @patch("app.circles_flow.list_my_circles", return_value=[])
    def test_no_communities_means_nothing(self, _circles) -> None:
        self.assertEqual(community_meets("u1"), {"communities": [], "total": 0})

    @patch("app.circles_flow.list_my_circles", side_effect=RuntimeError("db down"))
    def test_never_raises(self, _circles) -> None:
        self.assertEqual(community_meets("u1"), {"communities": [], "total": 0})


class TestMembership(unittest.TestCase):
    @patch("app.community_surface._blurb", return_value=None)
    @patch("app.community_surface.service_client")
    def test_a_visitor_opens_the_head_without_the_people(self, sb, _blurb) -> None:
        # She belongs to nothing here — reached the place from a peer's profile. The head
        # opens (discovery already names it to her); the roster does not.
        affs = _chain_seq([], [{"user_id": "u2", "circle_type": "fitness"}])
        sb.return_value = _sb({
            "circle_affiliations": affs,
            "places": _chain([{"id": "p1", "name": "OrangeTheory"}]),
            "place_features": _chain([]),
            "events": _chain([]),
            "event_requests": _chain([]),
            "users": _chain([{"id": "u2", "nickname": "mapleluz", "profile_photo_url": None}]),
            "user_blocks": _chain([]),
        })
        out = community_profile("u1", place_id="p1")
        self.assertEqual(out["membership"], "visitor")
        self.assertEqual(out["place_name"], "OrangeTheory")
        self.assertEqual(out["member_count"], 1)
        # The three members-only parts, all empty.
        self.assertEqual(out["member_preview"], [])
        self.assertEqual(out["actions"], [])
        self.assertIsNone(out["create_event_venue"])
        self.assertIsNone(out["detail"])
        self.assertEqual(out["affiliation_id"], "")
        # "just you so far" would be a lie — she is not one of the people counted.
        self.assertEqual(out["status_line"], "1 person")

    @patch("app.community_surface._blurb", return_value=None)
    @patch("app.community_surface.service_client")
    def test_a_place_only_blocked_people_go_to_does_not_open(self, sb, _blurb) -> None:
        # discover_communities_near drops it from the list; opening it by id must agree.
        affs = _chain_seq([], [{"user_id": "u2", "circle_type": "fitness"}])
        sb.return_value = _sb({
            "circle_affiliations": affs,
            "places": _chain([{"id": "p1", "name": "OrangeTheory"}]),
            "place_features": _chain([]),
            "events": _chain([]),
            "event_requests": _chain([]),
            "users": _chain([]),
            "user_blocks": _chain([{"blocker": "u1", "blocked": "u2"}]),
        })
        with self.assertRaises(ValueError) as err:
            community_profile("u1", place_id="p1")
        self.assertEqual(str(err.exception), "place_not_found")

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
        self.assertIn(("status", ["confirmed"]), [c.args for c in affs.in_.call_args_list])
        self.assertIn(("place_ref", "p1"), eq_args)
        self.assertIn(("dismissed_at", "null"), [c.args for c in affs.is_.call_args_list])


class TestFeatures(unittest.TestCase):
    def test_label_humanizes_the_key(self) -> None:
        self.assertEqual(_feature_label("has_pool", None, ""), "Pool")
        self.assertEqual(_feature_label("has_childcare", "true", ""), "Childcare")
        self.assertEqual(_feature_label("class_schedule", "full", ""), "Class schedule: full")
        self.assertEqual(_feature_label("has_pool", None, "toddler_swim"), "Pool (toddler swim)")

    def test_a_stored_label_wins_over_the_slug(self) -> None:
        # The key cannot round-trip casing, digits or punctuation — 20261030 stores the
        # member's own words next to it, and they win.
        self.assertEqual(_feature_label("has_byob", None, "", "BYOB"), "BYOB")
        self.assertEqual(_feature_label("has_24_7_access", None, "", "24/7 access"), "24/7 access")
        # Null label (chat-learned, or written before the column) still derives.
        self.assertEqual(_feature_label("has_byob", None, "", None), "Byob")
        self.assertEqual(_feature_label("has_byob", None, "", "   "), "Byob")
        # The value and sub_group qualifiers still decorate a stored label.
        self.assertEqual(_feature_label("has_byob", "true", "", "BYOB"), "BYOB")
        self.assertEqual(_feature_label("has_byob", None, "patio", "BYOB"), "BYOB (patio)")

    @patch("app.community_surface.service_client")
    def test_features_read_steps_down_before_the_label_migration(self, sb) -> None:
        # Deployed ahead of the migration, the first select 400s on the unknown column.
        # Losing the label is fine; losing every chip on the card is not.
        rows = [{"key": "has_pool", "value": None, "sub_group": "", "confidence": 0.9}]
        feats = _chain(rows)
        feats.execute.side_effect = [RuntimeError("column does not exist"), MagicMock(data=rows)]
        sb.return_value = _sb({"place_features": feats})
        self.assertEqual([f["label"] for f in place_features("p1")], ["Pool"])

    @patch("app.community_surface.service_client")
    def test_low_confidence_features_are_not_repeated(self, sb) -> None:
        rows = [
            {"key": "has_pool", "value": None, "sub_group": "", "confidence": 0.9},
            {"key": "has_sauna", "value": None, "sub_group": "", "confidence": 0.2},
        ]
        sb.return_value = _sb({"place_features": _chain(rows)})
        labels = [f["label"] for f in place_features("p1")]
        self.assertEqual(labels, ["Pool"])

    @patch("app.community_surface.service_client")
    def test_mine_marks_only_what_the_caller_contributed(self, sb) -> None:
        # The × belongs on the rows /features/remove will actually delete (issues #77).
        rows = [
            {"key": "has_pool", "sub_group": "", "confidence": 0.9, "contributed_by": "u1"},
            {"key": "has_sauna", "sub_group": "", "confidence": 0.9, "contributed_by": "u2"},
        ]
        sb.return_value = _sb({"place_features": _chain(rows)})
        self.assertEqual(
            {f["label"]: f["mine"] for f in place_features("p1", "u1")},
            {"Pool": True, "Sauna": False},
        )
        # No caller (a read that isn't on anyone's behalf) claims nothing.
        self.assertFalse(any(f["mine"] for f in place_features("p1")))


class TestMemberAttributes(unittest.TestCase):
    def test_their_own_threads_are_listed(self) -> None:
        self.assertEqual(
            _member_attributes(["Colombian roots", "Loves to cook"], []),
            ["Colombian roots", "Loves to cook"],
        )

    def test_shared_threads_come_first_and_are_not_duplicated(self) -> None:
        self.assertEqual(
            _member_attributes(["Gardens", "Runner"], [("Runner", "self")]),
            ["Runner", "Gardens"],
        )

    def test_a_childs_thread_is_never_listed_as_theirs(self) -> None:
        # "they do karate" about a parent whose KID does karate is false.
        self.assertEqual(_member_attributes([], [("Does karate", "child")]), [])
        self.assertEqual(
            _member_attributes(["Gardens"], [("Runner", "self"), ("Does karate", "child")]),
            ["Runner", "Gardens"],
        )

    def test_nothing_known_lists_nothing(self) -> None:
        self.assertEqual(_member_attributes([], []), [])

    def test_the_row_stays_scannable(self) -> None:
        # Shared first, so their own threads are the ones the cap drops.
        self.assertEqual(
            _member_attributes(["A", "B", "C"], [("E", "self")]), ["E", "A", "B"]
        )


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
        self.assertEqual(rows["u2"]["attributes"], ["Gardens", "Runs"])
        self.assertEqual(rows["u2"]["actions"][0]["id"], "peer_card_nudge")
        # Nothing shared and nothing public on file → nothing listed, and still no
        # fake score.
        self.assertEqual(rows["u3"]["attributes"], [])
        for row in out["members"]:
            for invented in ("match_stars", "match_band", "match_badge", "similarity_score"):
                self.assertNotIn(invented, row)

    @patch("app.community_surface.service_client")
    def test_a_members_own_public_threads_carry_their_line(self, sb) -> None:
        tables = self._tables()
        tables["user_identity_claims"] = _chain(
            [
                {"user_id": "u3", "label": "Colombian roots", "confidence": 0.9},
                {"user_id": "u3", "label": "Loves to cook", "confidence": 0.8},
            ]
        )
        sb.return_value = _sb(tables)
        rows = {r["peer_user_id"]: r for r in community_members("u1", place_id="p1")["members"]}
        self.assertEqual(rows["u3"]["attributes"], ["Colombian roots", "Loves to cook"])
        # The caller is never described back to herself.
        self.assertEqual(rows["u1"]["attributes"], [])

    @patch("app.community_surface.service_client")
    def test_the_caller_is_in_the_list_and_the_blocked_are_not(self, sb) -> None:
        # §17: member_count counts the caller, so the roster carries her too — one row
        # per person counted, hers flagged `me` with no shared line and no Nudge.
        sb.return_value = _sb(self._tables())
        out = community_members("u1", place_id="p1")
        ids = [r["peer_user_id"] for r in out["members"]]
        self.assertEqual(sorted(ids), ["u1", "u2", "u3"])
        self.assertEqual(len(out["members"]), out["member_count"] - 1)  # 'blocked' aside
        me = next(r for r in out["members"] if r["peer_user_id"] == "u1")
        self.assertTrue(me["me"])
        self.assertEqual(me["attributes"], [])
        self.assertEqual(me["actions"], [])
        self.assertFalse(any(r["me"] for r in out["members"] if r["peer_user_id"] != "u1"))

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


class TestMembershipKindAndActivities(unittest.TestCase):
    """The roster's two chip kinds: what someone IS here (member / curious) and what
    they DO here. Curious joiners are listed but never counted as members (§19)."""

    def _tables(self):
        return {
            "circle_affiliations": _chain(
                [
                    {"id": "a1", "circle_type": "fitness", "user_id": "u1",
                     "status": "confirmed", "created_at": "2026-01-01"},
                    {"user_id": "u2", "circle_type": "fitness",
                     "status": "confirmed", "created_at": "2026-01-02"},
                    {"user_id": "u4", "circle_type": "fitness",
                     "status": "curious", "created_at": "2026-01-03"},
                ]
            ),
            "places": _chain([{"id": "p1", "name": "OrangeTheory", "place_type": "fitness"}]),
            "users": _chain(
                [
                    {"id": "u2", "nickname": "coral88", "profile_photo_url": None},
                    {"id": "u4", "nickname": "mapleluz", "profile_photo_url": None},
                ]
            ),
            "user_blocks": _chain([]),
            "place_activities": _chain(
                [
                    {"user_id": "u2", "label": "CrossFit"},
                    {"user_id": "u2", "label": "Spin"},
                    {"user_id": "u2", "label": "CrossFit"},  # a repeat is one chip
                ]
            ),
        }

    @patch("app.community_surface.service_client")
    @patch("app.place_activities.service_client")
    def test_rows_say_member_or_curious_and_what_they_do(self, acts_sb, sb) -> None:
        tables = self._tables()
        sb.return_value = _sb(tables)
        acts_sb.return_value = _sb(tables)
        out = community_members("u1", place_id="p1")
        rows = {r["peer_user_id"]: r for r in out["members"]}
        self.assertEqual(rows["u2"]["membership"], "member")
        self.assertEqual(rows["u4"]["membership"], "curious")
        self.assertEqual(rows["u2"]["activities"], ["CrossFit", "Spin"])
        # Members lead the roster; curious joiners follow, so paging keeps the sections.
        self.assertEqual(
            [r["peer_user_id"] for r in out["members"]], ["u1", "u2", "u4"]
        )

    @patch("app.community_surface.service_client")
    @patch("app.place_activities.service_client")
    def test_curious_joiners_are_counted_apart(self, acts_sb, sb) -> None:
        tables = self._tables()
        sb.return_value = _sb(tables)
        acts_sb.return_value = _sb(tables)
        out = community_members("u1", place_id="p1")
        # "3 people · 2 go here in real life" — member_count keeps its old meaning.
        self.assertEqual(out["member_count"], 2)
        self.assertEqual(out["curious_count"], 1)

    @patch("app.community_surface.service_client")
    def test_unverified_caller_still_gets_both_counts(self, sb) -> None:
        sb.return_value = _sb(self._tables())
        out = community_members("u1", place_id="p1", phone_verified=False)
        self.assertEqual(out["members"], [])
        self.assertEqual((out["member_count"], out["curious_count"]), (2, 1))


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
            # A going row is a PERSON going — the roster read carries user_id now, so the
            # all-meets avatar stack and this count come out of one query.
            "event_requests": _chain(
                [
                    {"event_id": "e1", "user_id": "u2"},
                    {"event_id": "e1", "user_id": "u3"},
                    {"event_id": "e2", "user_id": "u2"},
                ]
            ),
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

    @patch("app.community_surface._blurb", return_value=None)
    @patch("app.community_surface.service_client")
    def test_a_curious_joiner_gets_the_head_and_no_roster(self, sb, _blurb) -> None:
        # §19: she said she does NOT go here. The place opens, the count is real, and
        # the people who DO go here keep their names.
        tables = {
            "circle_affiliations": _chain(
                [
                    {"id": "a1", "circle_type": "fitness", "user_id": "u1", "status": "curious"},
                    {"user_id": "u2", "circle_type": "fitness", "created_at": "2026-01-01"},
                ]
            ),
            "places": _chain([{"id": "p1", "name": "OrangeTheory", "google_place_id": "ChIJg"}]),
            "place_features": _chain([]),
            "events": _chain([]),
            "event_requests": _chain([]),
            "users": _chain([{"id": "u2", "nickname": "mapleluz", "profile_photo_url": None}]),
            "user_blocks": _chain([]),
        }
        sb.return_value = _sb(tables)
        out = community_profile("u1", place_id="p1")
        self.assertEqual(out["membership"], "curious")
        self.assertEqual(out["place_name"], "OrangeTheory")
        self.assertEqual(out["member_preview"], [])
        self.assertEqual(out["actions"], [])
        self.assertIsNone(out["create_event_venue"])

    @patch("app.community_surface._blurb", return_value=None)
    @patch("app.community_surface.service_client")
    def test_head_counts_goers_only_and_reports_curious_apart(self, sb, _blurb) -> None:
        """The Mizu Sushi split (QA 2026-08-20): six confirmed + one curious joiner read
        as "7 people" on this head while chat, the communities card, discovery and the
        join mail all said 6. The head is the one that was wrong."""
        tables = {
            "circle_affiliations": _chain(
                [
                    {"id": "a1", "circle_type": "other", "user_id": "u1",
                     "status": "confirmed", "created_at": "2026-01-01"},
                    {"user_id": "u2", "circle_type": "other",
                     "status": "confirmed", "created_at": "2026-01-02"},
                    {"user_id": "u3", "circle_type": "other",
                     "status": "curious", "created_at": "2026-01-03"},
                ]
            ),
            "places": _chain([{"id": "p1", "name": "Mizu Sushi & Steakhouse"}]),
            "place_features": _chain([]),
            "events": _chain([]),
            "event_requests": _chain([]),
            "users": _chain(
                [
                    {"id": "u1", "nickname": "jake", "profile_photo_url": None},
                    {"id": "u2", "nickname": "asjid", "profile_photo_url": None},
                    {"id": "u3", "nickname": "pouya", "profile_photo_url": None},
                ]
            ),
            "user_blocks": _chain([]),
        }
        sb.return_value = _sb(tables)
        out = community_profile("u1", place_id="p1")
        self.assertEqual((out["member_count"], out["curious_count"]), (2, 1))
        # Faces come off the same list as the count — a curious face over a count that
        # excludes her is the "2 members over one face" bug in reverse.
        self.assertEqual(
            [f["peer_user_id"] for f in out["member_preview"]], ["u1", "u2"]
        )

    def test_a_place_only_curious_people_watch_says_so(self) -> None:
        # count is confirmed-only now, so 0 is reachable — and "0 people" is true and
        # unreadable.
        self.assertEqual(_status_line(0, 0, is_member=False), "nobody goes here yet")

    @patch("app.community_surface._blurb", return_value=None)
    @patch("app.community_surface.service_client")
    def test_a_member_reads_as_one(self, sb, _blurb) -> None:
        tables = {
            "circle_affiliations": _chain(
                [{"id": "a1", "circle_type": "fitness", "user_id": "u1", "status": "confirmed"}]
            ),
            "places": _chain([{"id": "p1", "name": "OrangeTheory"}]),
            "place_features": _chain([]),
            "events": _chain([]),
            "event_requests": _chain([]),
            "users": _chain([{"id": "u1", "nickname": "coral88", "profile_photo_url": None}]),
            "user_blocks": _chain([]),
        }
        sb.return_value = _sb(tables)
        out = community_profile("u1", place_id="p1")
        self.assertEqual(out["membership"], "member")
        # Her own face is in the preview (§17) — one member, one avatar.
        self.assertEqual([m["peer_user_id"] for m in out["member_preview"]], ["u1"])
        self.assertTrue(out["member_preview"][0]["me"])


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
