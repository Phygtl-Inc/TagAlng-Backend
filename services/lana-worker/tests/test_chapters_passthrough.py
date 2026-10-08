"""Chapters: fields the backend computed and then dropped before the response.

Each test drives the REAL producer and the REAL shaper/route model, and asserts on the
serialized JSON — a field that only exists on an intermediate dict proves nothing (a past
bug dropped new fields in one of several constructors while the unit tests passed).

1. community_discovery.within (+ each row's parent) on the chat turn.
2. origin_place_id / origin_place_name on meets: chat browse rows, the community-about
   chat rows, the look card, /lana/circles/meets, the look draft.
3. can_manage on /lana/circles/list rows (creator or live operator — attach_chapter's
   standing, 20270125120000).
"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

import app.main as main
from app.auth import AuthSession
from app.models import CommunityMeetsResponse, SendMessageResponse

SJSU = "44444444-4444-4444-4444-444444444444"
RCC = "33333333-3333-3333-3333-333333333333"
CLUB = "55555555-5555-5555-5555-555555555555"
ME = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"


def _turn(**fields) -> dict:
    """The chat turn exactly as the client receives it."""
    return SendMessageResponse(
        session_id="s1", status="active", assistant_message="ok", **fields
    ).model_dump(mode="json")


def _soon(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")


def _event(eid: str, *, origin: bool) -> dict:
    row = {
        "id": eid,
        "title": f"Meet {eid}",
        "starts_at": _soon(1),
        "has_time": True,
        "venue_name": "MLK Library",
        "cover_emoji": "📚",
        "cohort_tags": ["ai"],
    }
    if origin:
        row["origin_place_id"] = RCC
        row["origin_place_name"] = "RCC"
    return row


def _auth(uid: str = ME) -> AuthSession:
    return AuthSession(user_id=uid, is_anonymous=False, phone_verified=True, home_block_id=None)


# ── 1. within + parent ────────────────────────────────────────────────────────


class TestChaptersTurnWithin(unittest.TestCase):
    """"What clubs does SJSU have?" — the card is headed by the parent it lists."""

    def _run(self) -> dict:
        from app.community_discovery import _chapters_turn

        chapters = [
            {
                "place_id": RCC,
                "place_name": "RCC",
                "member_count": 4,
                "is_member": False,
                "status_line": "4 members",
            }
        ]
        ctx: dict = {}
        with (
            patch(
                "app.community_discovery.community_chapters",
                return_value={"chapters": chapters},
            ),
            patch("app.reply_compose.compose_reply", return_value="SJSU has 1 club."),
        ):
            _chapters_turn(
                ME, parent={"place_id": SJSU, "place_name": "SJSU"}, topic=None,
                message="what clubs does sjsu have", session_ctx=ctx,
            )
        return _turn(community_discovery=main._community_discovery_from_ctx(ctx))

    def test_within_survives_to_the_turn(self) -> None:
        out = self._run()["community_discovery"]
        self.assertEqual(out["within"], {"place_id": SJSU, "place_name": "SJSU"})

    def test_each_row_keeps_its_parent(self) -> None:
        row = self._run()["community_discovery"]["communities"][0]
        self.assertEqual(row["place_id"], RCC)
        self.assertEqual(row["parent"]["place_id"], SJSU)
        self.assertEqual(row["parent"]["place_name"], "SJSU")

    def test_within_null_when_not_a_chapters_list(self) -> None:
        ctx = {"community_discovery": {"communities": [{"place_id": CLUB, "place_name": "X"}]}}
        out = _turn(community_discovery=main._community_discovery_from_ctx(ctx))
        self.assertIsNone(out["community_discovery"]["within"])
        self.assertIsNone(out["community_discovery"]["communities"][0]["parent"])

    def test_chat_card_and_routes_share_one_row_shaper(self) -> None:
        # A field on the route row must reach the chat card too — the chat path used to
        # build its own CommunityDiscoveryRow and dropped parent/lat/lng/description.
        ctx = {
            "community_discovery": {
                "communities": [
                    {
                        "place_id": RCC,
                        "place_name": "RCC",
                        "lat": 37.33,
                        "lng": -121.88,
                        "description": "Responsible computing",
                        "area_label": "San Jose",
                        "parent": {"place_id": SJSU, "place_name": "SJSU"},
                    }
                ]
            }
        }
        row = _turn(community_discovery=main._community_discovery_from_ctx(ctx))[
            "community_discovery"
        ]["communities"][0]
        self.assertEqual(row["lat"], 37.33)
        self.assertEqual(row["description"], "Responsible computing")
        self.assertEqual(row["area_label"], "San Jose")
        self.assertEqual(row["parent"]["place_id"], SJSU)


class TestAboutTurnNamedCardParent(unittest.TestCase):
    """"Tell me about RCC" — the one named card says it is SJSU's, and RCC's family meets
    under it say where they are from."""

    def _run(self) -> tuple[dict, dict]:
        from app.community_discovery import _community_about_turn

        prof = {
            "place_id": RCC,
            "place_name": "RCC",
            "membership": "member",
            "member_count": 4,
            "relation": "club",
            "parent": {"place_id": SJSU, "place_name": "SJSU", "emoji": "🎓", "member_count": 40},
            "upcoming_events": [
                {
                    "event_id": "e1",
                    "title": "SJSU town hall",
                    "starts_at": _soon(2),
                    "has_time": True,
                    "venue_name": "Student Union",
                    "origin_place_id": SJSU,
                    "origin_place_name": "SJSU",
                }
            ],
        }
        ctx: dict = {}
        with (
            patch("app.community_surface.community_profile", return_value=prof),
            patch("app.reply_compose.compose_reply", return_value="RCC is a club."),
            patch("app.community_opening.active_community_facts", return_value=None),
        ):
            _community_about_turn(
                ME, community={"place_id": RCC, "place_name": "RCC"},
                message="tell me about rcc", session_ctx=ctx,
            )
        out = _turn(
            community_discovery=main._community_discovery_from_ctx(ctx),
            activity_previews=main._activity_previews_from_ctx(ctx),
        )
        return out["community_discovery"], out

    def test_named_card_carries_parent(self) -> None:
        card, _ = self._run()
        self.assertTrue(card["named"])
        self.assertEqual(card["communities"][0]["parent"]["place_id"], SJSU)

    def test_family_meet_rows_carry_origin(self) -> None:
        _, ui = self._run()
        row = ui["activity_previews"][0]
        self.assertEqual(row["origin_place_id"], SJSU)
        self.assertEqual(row["origin_place_name"], "SJSU")


# ── 2. origin on meets ───────────────────────────────────────────────────────


class TestBrowseRowsOrigin(unittest.TestCase):
    """Chat browse rows: community_events labels a family meet; the card must say so."""

    def _rows(self, events: list[dict]) -> list[dict]:
        from app.discovery_route import activity_previews_from_events

        with patch("app.event_place.event_community", return_value=None):
            ctx = {"activity_previews": activity_previews_from_events(events)}
        return _turn(activity_previews=main._activity_previews_from_ctx(ctx))[
            "activity_previews"
        ]

    def test_family_meet_carries_origin(self) -> None:
        rows = self._rows([_event("e1", origin=True), _event("e2", origin=False)])
        self.assertEqual(rows[0]["origin_place_id"], RCC)
        self.assertEqual(rows[0]["origin_place_name"], "RCC")
        self.assertIsNone(rows[1]["origin_place_id"])
        self.assertIsNone(rows[1]["origin_place_name"])

    def test_activity_browse_community_path_reaches_the_row(self) -> None:
        # The real lane read: community_events labels via label_origin.
        from app.community_scope import community_events

        family = [
            {"place_id": SJSU, "place_name": "SJSU", "relation": "self"},
            {"place_id": RCC, "place_name": "RCC", "relation": "chapter"},
        ]
        raw = [dict(_event("e1", origin=False), circle_place_ref=RCC, place_ref=None)]

        class _Q:
            def __getattr__(self, _name):
                return lambda *a, **k: self

            def execute(self):
                return type("R", (), {"data": raw})()

        class _SB:
            def table(self, _name):
                return _Q()

        with (
            patch("app.community_chapter_ops.community_family", return_value=family),
            patch("app.auth.service_client", return_value=_SB()),
            patch("app.event_publish.roll_recurring_events", return_value=None),
        ):
            events = community_events(SJSU, viewer_id=ME)
        rows = self._rows(events)
        self.assertEqual(rows[0]["origin_place_id"], RCC)
        self.assertEqual(rows[0]["origin_place_name"], "RCC")


_CIRCLE = {
    "id": "a1",
    "place_id": SJSU,
    "place_name": "SJSU",
    "circle_type": "school",
    "member_count": 40,
    "active": True,
}


class TestLookCardOrigin(unittest.TestCase):
    """The look card's meets — no rollup on this read, so passed through when present."""

    def test_origin_survives_to_the_turn(self) -> None:
        from app.community_surface import communities_card

        with (
            patch("app.circles_flow.list_my_circles", return_value=[dict(_CIRCLE)]),
            patch(
                "app.community_surface._events_at_place",
                return_value=[_event("e1", origin=True), _event("e2", origin=False)],
            ),
            patch("app.community_surface._going_rosters", return_value={}),
        ):
            card = communities_card(ME)
        out = _turn(communities=main._communities_from_ctx({"communities_card": card}))
        meets = out["communities"]["items"][0]["meets"]
        self.assertEqual(meets[0]["origin_place_id"], RCC)
        self.assertEqual(meets[0]["origin_place_name"], "RCC")
        self.assertIsNone(meets[1]["origin_place_id"])


class TestCirclesMeetsOrigin(unittest.TestCase):
    """POST /lana/circles/meets through the route and its response model."""

    def test_route_serializes_origin(self) -> None:
        with (
            patch("app.main.verify_auth", return_value=_auth()),
            patch("app.circles_flow.list_my_circles", return_value=[dict(_CIRCLE)]),
            patch(
                "app.community_surface._events_at_place",
                return_value=[_event("e1", origin=True), _event("e2", origin=False)],
            ),
            patch("app.community_surface._going_rosters", return_value={}),
            patch("app.community_surface._going_faces", return_value={}),
        ):
            res = TestClient(main.app).post(
                "/lana/circles/meets", json={}, headers={"Authorization": "Bearer x"}
            )
        self.assertEqual(res.status_code, 200)
        meets = res.json()["communities"][0]["meets"]
        self.assertEqual(meets[0]["origin_place_id"], RCC)
        self.assertEqual(meets[0]["origin_place_name"], "RCC")
        self.assertIsNone(meets[1]["origin_place_id"])
        # And the model itself accepts the shaper's dict unchanged.
        CommunityMeetsResponse(**{"communities": res.json()["communities"], "total": 1})


class TestLookDraftOrigin(unittest.TestCase):
    """look_meet with a community selected reads community_events (family-rolled)."""

    def test_draft_events_carry_origin(self) -> None:
        from app.look_meet import _rank_activities

        with patch("app.event_place.communities_for_events", return_value={}):
            events = _rank_activities(
                [_event("e1", origin=True), _event("e2", origin=False)], None, 5
            )
        draft = main._look_draft_from_dict({"kind": "meetup", "events": events, "ready": True})
        out = _turn(look_draft=draft)["look_draft"]["events"]
        self.assertEqual(out[0]["origin_place_id"], RCC)
        self.assertEqual(out[0]["origin_place_name"], "RCC")
        self.assertIsNone(out[1]["origin_place_id"])


# ── 3. can_manage on /lana/circles/list ──────────────────────────────────────


class _Res:
    def __init__(self, data):
        self.data = data


class _Query:
    """A PostgREST chain that applies eq / in_ / is_(null) to an in-memory table."""

    def __init__(self, rows: list[dict]):
        self._rows = list(rows)

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self._rows = [r for r in self._rows if str(r.get(col)) == str(val)]
        return self

    def in_(self, col, vals):
        vals = {str(v) for v in vals}
        self._rows = [r for r in self._rows if str(r.get(col)) in vals]
        return self

    def is_(self, col, val):
        assert val == "null"
        self._rows = [r for r in self._rows if r.get(col) is None]
        return self

    def execute(self):
        return _Res(self._rows)


class _DB:
    # SJSU: created by ME. RCC: ME is a live operator. CLUB: ME was an operator, removed.
    # OTHER_CLUB: ME is only a member.
    TABLES = {
        "places": [
            {"id": SJSU, "created_by": ME},
            {"id": RCC, "created_by": OTHER},
            {"id": CLUB, "created_by": OTHER},
            {"id": "66666666-6666-6666-6666-666666666666", "created_by": None},
        ],
        "place_managers": [
            {"place_id": RCC, "user_id": ME, "role": "operator", "removed_at": None},
            {"place_id": CLUB, "user_id": ME, "role": "operator", "removed_at": "2026-09-01"},
            {
                "place_id": "66666666-6666-6666-6666-666666666666",
                "user_id": ME,
                "role": "manager",
                "removed_at": None,
            },
        ],
    }

    def table(self, name):
        return _Query(self.TABLES[name])


def _circle(pid: str, name: str) -> dict:
    return {
        "id": f"a-{name}",
        "circle_type": "school",
        "grounded": True,
        "place_id": pid,
        "place_name": name,
        "member_count": 3,
        "active": True,
        "activities": [],
    }


_LIST = [
    _circle(SJSU, "SJSU"),
    _circle(RCC, "RCC"),
    _circle(CLUB, "Old club"),
    _circle("66666666-6666-6666-6666-666666666666", "Modded"),
]


class TestCirclesListCanManage(unittest.TestCase):
    def _post(self, body: dict, viewer: str = ME) -> dict:
        with (
            patch("app.main.verify_auth", return_value=_auth(viewer)),
            patch("app.circles_flow.list_my_circles", return_value=[dict(r) for r in _LIST]),
            patch("app.circles_flow.service_client", return_value=_DB()),
            patch("app.community_surface._blocked_ids", return_value=set()),
            patch("app.circles_flow._my_place_refs", return_value=set()),
        ):
            res = TestClient(main.app).post(
                "/lana/circles/list", json=body, headers={"Authorization": "Bearer x"}
            )
        self.assertEqual(res.status_code, 200)
        return {r["place_name"]: r["can_manage"] for r in res.json()["circles"]}

    def test_own_list_creator_and_operator_only(self) -> None:
        self.assertEqual(
            self._post({}),
            {"SJSU": True, "RCC": True, "Old club": False, "Modded": False},
        )

    def test_someone_elses_list_answers_for_the_viewer(self) -> None:
        # OTHER's list, viewed by ME: whether *I* run each place.
        self.assertEqual(
            self._post({"user_id": OTHER}),
            {"SJSU": True, "RCC": True, "Old club": False, "Modded": False},
        )

    def test_stranger_runs_nothing(self) -> None:
        stranger = "77777777-7777-7777-7777-777777777777"
        out = self._post({"user_id": ME}, viewer=stranger)
        self.assertEqual(set(out.values()), {False})

    def test_read_failure_is_unknown_not_false(self) -> None:
        with (
            patch("app.main.verify_auth", return_value=_auth()),
            patch("app.circles_flow.list_my_circles", return_value=[dict(r) for r in _LIST]),
            patch("app.circles_flow.service_client", side_effect=RuntimeError("down")),
        ):
            res = TestClient(main.app).post(
                "/lana/circles/list", json={}, headers={"Authorization": "Bearer x"}
            )
        self.assertEqual({r["can_manage"] for r in res.json()["circles"]}, {None})


if __name__ == "__main__":
    unittest.main()
