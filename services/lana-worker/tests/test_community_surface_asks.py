"""PWA backend-asks §49, §58(a)(b), §59(a)(b)(c), §24(b) — community read surfaces.

Each test names the ask it pins. SQL visibility (who sees which chapter, block filtering,
the creator block on the handle reads) is proven against a real Postgres in the PR's
scenario run; these pin the worker's half: shaping, routing, null rules and the emoji
pipeline.
"""

from __future__ import annotations

import unittest
from unittest import mock
from unittest.mock import MagicMock, patch

from fastapi import HTTPException


def _chain(data=None):
    m = MagicMock()
    for method in ("select", "eq", "neq", "is_", "in_", "or_", "limit", "order", "range", "not_"):
        getattr(m, method).return_value = m
    m.not_ = m
    m.execute.return_value = MagicMock(data=data if data is not None else [], count=None)
    return m


def _chain_seq(*datas):
    m = _chain([])
    m.execute.side_effect = [MagicMock(data=d, count=None) for d in datas]
    return m


def _sb(tables: dict, rpc=None):
    sb = MagicMock()
    sb.table.side_effect = lambda name: tables.get(name, _chain([]))
    sb.rpc.side_effect = lambda name, args=None: MagicMock(
        execute=MagicMock(return_value=MagicMock(data=(rpc or {}).get(name, [])))
    )
    return sb


def _auth(**kw):
    base = dict(user_id="u1", phone_verified=True, is_anonymous=False)
    base.update(kw)
    return mock.Mock(**base)


_CHAPTER_ROWS = [
    {
        "place_id": "cA",
        "name": "Chapter Orlando",
        "address": "1 Lake Nona Blvd",
        "place_type": "fitness",
        "zip": "32827",
        "lat": 28.5,
        "lng": -81.3,
        "member_count": 2,
        "member_types": ["fitness"],
        "is_member": True,
    },
    {
        "place_id": "cB",
        "name": "Chapter Boston",
        "address": None,
        "place_type": "fitness",
        "zip": "02101",
        "lat": 42.3,
        "lng": -71.0,
        "member_count": 1,
        "member_types": ["fitness"],
        "is_member": False,
    },
]


# ── §59(a) chapters ─────────────────────────────────────────────────────────────


class TestCommunityChapters(unittest.TestCase):
    def test_rows_are_the_discovery_row_with_her_own_marked(self) -> None:
        from app import community_discovery as cd

        sb = _sb(
            {"places": _chain([{"id": "P", "name": "Iron Man Training"}])},
            rpc={"discover_community_chapters": _CHAPTER_ROWS},
        )
        with patch.object(cd, "service_client", return_value=sb):
            out = cd.community_chapters("u1", "P")
        self.assertEqual(out["place_id"], "P")
        self.assertEqual(out["place_name"], "Iron Man Training")
        self.assertEqual([r["place_id"] for r in out["chapters"]], ["cA", "cB"])
        first = out["chapters"][0]
        self.assertTrue(first["is_member"])
        self.assertEqual(first["status_line"], "You + 1 others")
        self.assertEqual((first["lat"], first["lng"]), (28.5, -81.3))
        self.assertEqual(first["relation"], cd.place_relation_noun("fitness"))
        # The RPC is asked about THIS parent for THIS caller.
        name, args = sb.rpc.call_args.args
        self.assertEqual(name, "discover_community_chapters")
        self.assertEqual((args["p_user_id"], args["p_place_id"]), ("u1", "P"))

    def test_unknown_place_is_place_not_found(self) -> None:
        from app import community_discovery as cd

        with patch.object(cd, "service_client", return_value=_sb({"places": _chain([])})):
            with self.assertRaises(ValueError) as err:
                cd.community_chapters("u1", "nope")
        self.assertEqual(str(err.exception), "place_not_found")

    def test_no_chapters_is_an_empty_list_not_an_error(self) -> None:
        from app import community_discovery as cd

        sb = _sb({"places": _chain([{"id": "P", "name": "Gym"}])})
        with patch.object(cd, "service_client", return_value=sb):
            self.assertEqual(cd.community_chapters("u1", "P")["chapters"], [])

    def test_a_failed_rpc_reads_as_none_yet(self) -> None:
        from app import community_discovery as cd

        sb = _sb({"places": _chain([{"id": "P", "name": "Gym"}])})
        sb.rpc.side_effect = RuntimeError("PGRST202")
        with patch.object(cd, "service_client", return_value=sb):
            self.assertEqual(cd.community_chapters("u1", "P")["chapters"], [])

    def test_route_404s_unknown_and_serves_the_wire_shape(self) -> None:
        from app import main

        with patch.object(main, "verify_auth", return_value=_auth()), patch(
            "app.community_discovery.community_chapters", side_effect=ValueError("place_not_found")
        ):
            with self.assertRaises(HTTPException) as err:
                main.post_circles_chapters(main.CommunityChaptersBody(place_id="x"), authorization="B")
        self.assertEqual(err.exception.status_code, 404)

        from app import community_discovery as cd

        data = {"place_id": "P", "place_name": "Iron Man", "chapters": cd._near_rows(_CHAPTER_ROWS)}
        with patch.object(main, "verify_auth", return_value=_auth()), patch(
            "app.community_discovery.community_chapters", return_value=data
        ), patch("app.community_affinity._fetch", return_value={}), patch(
            "app.community_discovery.service_client",
            return_value=_sb({
                "places": _chain([{"id": "cA", "blurb": "Sunrise swims.", "zip": "32827"}]),
                "zip_centroids": _chain([{"zip5": "32827", "city": "Lake Nona"}]),
            }),
        ):
            out = main.post_circles_chapters(main.CommunityChaptersBody(place_id="P"), authorization="B")
        body = out.model_dump()
        self.assertEqual(body["place_id"], "P")
        self.assertEqual(body["place_name"], "Iron Man")
        row = body["chapters"][0]
        for key in ("place_id", "place_name", "place_address", "place_type", "relation", "emoji",
                    "member_count", "is_member", "status_line", "affinity", "fit_line", "fit_chips",
                    "description", "area_label"):
            self.assertIn(key, row)
        self.assertTrue(row["is_member"])
        self.assertEqual(row["description"], "Sunrise swims.")
        self.assertEqual(row["area_label"], "Lake Nona")
        self.assertNotIn("_fit_basis", row)


# ── §58(b) description + area on discovery rows ──────────────────────────────────


class TestDescriptionAndArea(unittest.TestCase):
    def _run(self, rows, places, zips):
        from app import community_discovery as cd

        sb = _sb({"places": _chain(places), "zip_centroids": _chain(zips)})
        with patch.object(cd, "service_client", return_value=sb):
            cd.attach_description_and_area(rows)
        return rows

    def test_geo_row_reads_its_zip_area_and_stored_blurb(self) -> None:
        rows = self._run(
            [{"place_id": "g"}],
            [{"id": "g", "blurb": " A pool gym. ", "zip": "32827", "place_type": "fitness", "hq_city": None}],
            [{"zip5": "32827", "city": "Lake Nona"}],
        )
        self.assertEqual(rows[0]["description"], "A pool gym.")
        self.assertEqual(rows[0]["area_label"], "Lake Nona")

    def test_creator_row_reads_the_city_it_is_run_from(self) -> None:
        rows = self._run(
            [{"place_id": "c"}],
            [{"id": "c", "blurb": None, "zip": None, "place_type": "creator", "hq_city": "Tampa, FL"}],
            [],
        )
        self.assertIsNone(rows[0]["description"])  # never a template on a list
        self.assertEqual(rows[0]["area_label"], "Tampa, FL")

    def test_unknown_zip_falls_back_to_hq_then_null(self) -> None:
        rows = self._run(
            [{"place_id": "g"}, {"place_id": "h"}],
            [
                {"id": "g", "blurb": "", "zip": "99999", "place_type": "fitness", "hq_city": "Austin"},
                {"id": "h", "blurb": "", "zip": None, "place_type": "fitness", "hq_city": None},
            ],
            [],
        )
        self.assertEqual(rows[0]["area_label"], "Austin")
        self.assertIsNone(rows[1]["area_label"])
        self.assertIsNone(rows[1]["description"])

    def test_a_failed_read_leaves_both_null(self) -> None:
        from app import community_discovery as cd

        rows = [{"place_id": "g"}]
        with patch.object(cd, "service_client", side_effect=RuntimeError("down")):
            cd.attach_description_and_area(rows)
        self.assertEqual((rows[0]["description"], rows[0]["area_label"]), (None, None))


# ── §58(a) query-less creator read ───────────────────────────────────────────────


_CREATORS = [
    {"place_id": "c1", "name": "Big Club", "place_type": "creator", "hq_city": "Tampa, FL",
     "hq_lat": None, "hq_lng": None, "member_count": 40, "is_member": False},
    {"place_id": "c2", "name": "Small Fit", "place_type": "creator", "hq_city": "Austin, TX",
     "hq_lat": 30.2, "hq_lng": -97.7, "member_count": 3, "is_member": False},
    {"place_id": "c3", "name": "Unscored", "place_type": "creator", "hq_city": None,
     "hq_lat": None, "hq_lng": None, "member_count": 99, "is_member": True},
]


class TestCreatorRead(unittest.TestCase):
    def test_ranked_by_her_fit_unscored_last_and_cut(self) -> None:
        from app import community_discovery as cd

        scored = {
            "c1": {"place_id": "c1", "shared_concept_count": 0, "semantic_similarity": 0.6},
            "c2": {"place_id": "c2", "shared_concept_count": 3, "semantic_similarity": 0.9},
        }
        with patch.object(cd, "service_client", return_value=_sb({}, rpc={"discover_creator_communities": _CREATORS})), \
                patch("app.community_affinity._fetch", return_value=scored):
            rows = cd.discover_creator_communities_for("u1", limit=2)
        # c2 fits best despite fewer members; the unscored c3 is cut, never ranked first.
        self.assertEqual([r["place_id"] for r in rows], ["c2", "c1"])
        self.assertGreater(rows[0]["affinity"], rows[1]["affinity"])
        self.assertEqual(rows[0]["hq_city"], "Austin, TX")
        self.assertEqual(rows[0]["member_count"], 3)
        self.assertIsNone(rows[0]["matched_label"])

    def test_unscored_rows_trail_the_scored(self) -> None:
        from app import community_discovery as cd

        with patch.object(cd, "service_client", return_value=_sb({}, rpc={"discover_creator_communities": _CREATORS})), \
                patch("app.community_affinity._fetch",
                      return_value={"c2": {"place_id": "c2", "shared_concept_count": 0}}):
            rows = cd.discover_creator_communities_for("u1", limit=5)
        self.assertEqual(rows[0]["place_id"], "c2")
        # Two unscored rows after it, livelier first.
        self.assertEqual([r["place_id"] for r in rows[1:]], ["c3", "c1"])

    def test_route_without_query_uses_the_creator_read(self) -> None:
        from app import main

        row = {"place_id": "c2", "place_name": "Small Fit", "place_type": "creator",
               "hq_city": "Austin, TX", "member_count": 3, "is_member": False,
               "affinity": 0.71, "_fit_basis": None}
        with patch.object(main, "verify_auth", return_value=_auth()), \
                patch("app.community_discovery.discover_creator_communities_for", return_value=[row]) as cr, \
                patch("app.community_discovery.discover_communities_by_topic") as topic, \
                patch("app.community_discovery.attach_description_and_area",
                      side_effect=lambda rows: [r.update(description="Run club.", area_label="Austin, TX") for r in rows]):
            out = main.post_circles_discover_topic(main.CommunityDiscoverTopicBody(), authorization="B")
        topic.assert_not_called()
        cr.assert_called_once()
        r = out.model_dump()["communities"][0]
        self.assertEqual(r["affinity"], 0.71)
        self.assertEqual(r["hq_city"], "Austin, TX")
        self.assertEqual(r["description"], "Run club.")
        self.assertEqual(r["area_label"], "Austin, TX")
        self.assertIn("fit_line", r)
        self.assertEqual(out.query, "")

    def test_route_with_query_keeps_the_topic_read(self) -> None:
        from app import main

        with patch.object(main, "verify_auth", return_value=_auth()), \
                patch("app.community_discovery.discover_creator_communities_for") as cr, \
                patch("app.community_discovery.discover_communities_by_topic", return_value=[]) as topic, \
                patch("app.community_discovery.attach_description_and_area"):
            main.post_circles_discover_topic(
                main.CommunityDiscoverTopicBody(query="triathlon"), authorization="B"
            )
        cr.assert_not_called()
        self.assertEqual(topic.call_args.args[1], "triathlon")

    def test_body_accepts_no_query(self) -> None:
        from app import main

        self.assertIsNone(main.CommunityDiscoverTopicBody().query)


# ── §49 + §59(c) profile ─────────────────────────────────────────────────────────


class TestProfileHead(unittest.TestCase):
    def _profile(self, *, me_status, place, phone_verified=True, parent=None):
        from app import community_surface as cs

        mine = [{"id": "a1", "circle_type": "fitness", "user_id": "u1", "status": me_status}] if me_status else []
        roster = [{"user_id": "u2", "circle_type": "fitness", "created_at": "2026-01-01"}]
        if me_status:
            roster = mine + roster
        places = _chain_seq([place], [parent] if parent else [])
        tables = {
            "circle_affiliations": _chain_seq(mine, roster) if not me_status else _chain(roster),
            "places": places,
            "place_features": _chain([]),
            "events": _chain([]),
            "event_requests": _chain([]),
            "users": _chain([]),
            "user_blocks": _chain([]),
            "circle_invite_redemptions": _chain([]),
        }
        with patch.object(cs, "service_client", return_value=_sb(tables)), \
                patch.object(cs, "_blurb", return_value=None), \
                patch("app.circles_flow._member_counts", return_value={"P": 12}):
            return cs.community_profile("u1", place_id="p1", phone_verified=phone_verified)

    _GYM = {"id": "p1", "name": "OrangeTheory", "address": "9145 Narcoossee Rd",
            "google_place_id": "ChIJg", "lat": 28.4, "lng": -81.2}

    def test_a_visitor_gets_the_point_but_no_venue_block(self) -> None:
        out = self._profile(me_status=None, place=self._GYM)
        self.assertEqual(out["membership"], "visitor")
        self.assertEqual((out["lat"], out["lng"]), (28.4, -81.2))
        self.assertIsNone(out["create_event_venue"])
        self.assertIsNone(out["parent"])

    def test_an_unverified_member_gets_her_communitys_point(self) -> None:
        out = self._profile(me_status="confirmed", place=self._GYM, phone_verified=False)
        self.assertEqual((out["lat"], out["lng"]), (28.4, -81.2))
        venue = out["create_event_venue"]
        self.assertIsNotNone(venue)
        self.assertEqual((venue["lat"], venue["lng"]), (28.4, -81.2))
        # Hosting is still verified-only.
        self.assertEqual(out["actions"], [])

    def test_a_creator_community_has_no_point(self) -> None:
        creator = {"id": "p1", "name": "Iron Man", "place_type": "creator", "lat": None, "lng": None}
        out = self._profile(me_status="confirmed", place=creator)
        self.assertIsNone(out["lat"])
        self.assertIsNone(out["lng"])

    def test_a_chapter_names_its_parent(self) -> None:
        chapter = dict(self._GYM, parent_place_ref="P")
        parent = {"id": "P", "name": "Iron Man Training", "place_type": "creator"}
        out = self._profile(me_status="confirmed", place=chapter, parent=parent)
        self.assertEqual(
            out["parent"],
            {"place_id": "P", "place_name": "Iron Man Training",
             "emoji": out["parent"]["emoji"], "member_count": 12},
        )
        self.assertTrue(out["parent"]["emoji"])

    def test_route_serializes_point_and_parent(self) -> None:
        from app import main

        data = {"place_id": "p1", "place_name": "Chapter Orlando", "lat": 28.5, "lng": -81.3,
                "parent": {"place_id": "P", "place_name": "Iron Man", "emoji": "🏃", "member_count": 12}}
        with patch.object(main, "verify_auth", return_value=_auth(phone_verified=False)), \
                patch("app.community_surface.community_profile", return_value=data):
            out = main.post_circles_profile(main.CommunityProfileBody(place_id="p1"), authorization="B")
        body = out.model_dump()
        self.assertEqual((body["lat"], body["lng"]), (28.5, -81.3))
        self.assertEqual(body["parent"]["place_name"], "Iron Man")
        self.assertEqual(body["parent"]["member_count"], 12)
        with patch.object(main, "verify_auth", return_value=_auth()), \
                patch("app.community_surface.community_profile", return_value={"place_id": "p1"}):
            plain = main.post_circles_profile(main.CommunityProfileBody(place_id="p1"), authorization="B")
        self.assertIsNone(plain.parent)
        self.assertIsNone(plain.lat)


# ── §59(b) /circles/list ─────────────────────────────────────────────────────────


class TestListParent(unittest.TestCase):
    def _list(self, place_rows, parent_rows):
        from app import circles_flow as cf

        affs = _chain([
            {"id": "a1", "circle_type": "fitness", "circle_key": "orlando", "status": "confirmed",
             "place_ref": "cA", "created_at": "2026-01-01"},
            {"id": "a2", "circle_type": "fitness", "circle_key": "gym", "status": "confirmed",
             "place_ref": "g", "created_at": "2025-01-01"},
        ])
        sb = _sb({"circle_affiliations": affs, "places": _chain(place_rows)})
        psb = _sb({"places": _chain(parent_rows)})
        with patch.object(cf, "service_client", return_value=sb), \
                patch("app.community_surface.service_client", return_value=psb), \
                patch.object(cf, "_member_counts", return_value={"P": 7}), \
                patch("app.place_activities.activities_for_places", return_value={}):
            return cf.list_my_circles("u1")

    def test_a_chapter_row_nests_under_its_parent(self) -> None:
        rows = self._list(
            [{"id": "cA", "name": "Chapter Orlando", "parent_place_ref": "P"},
             {"id": "g", "name": "Fitness CF", "parent_place_ref": None}],
            [{"id": "P", "name": "Iron Man Training", "place_type": "creator"}],
        )
        by = {r["place_id"]: r for r in rows}
        self.assertEqual(by["cA"]["parent_place_id"], "P")
        self.assertEqual(by["cA"]["parent_place_name"], "Iron Man Training")
        self.assertTrue(by["cA"]["parent_emoji"])
        self.assertIsNone(by["g"]["parent_place_id"])
        self.assertIsNone(by["g"]["parent_place_name"])
        self.assertIsNone(by["g"]["parent_emoji"])

    def test_someone_elses_list_carries_the_parent_too(self) -> None:
        from app import circles_flow as cf

        for key in ("parent_place_id", "parent_place_name", "parent_emoji"):
            self.assertIn(key, cf._PUBLIC_CIRCLE_FIELDS)


# ── §24(b) an emoji on every feature row ─────────────────────────────────────────


class TestFeatureEmoji(unittest.TestCase):
    def _upsert(self, existing, **kw):
        from app import circles_capture as cc

        tbl = _chain([existing] if existing else [])
        tbl.insert.return_value = MagicMock(execute=MagicMock(return_value=MagicMock(data=[{"id": "new1"}])))
        tbl.update.return_value = tbl
        sb = _sb({"place_features": tbl})
        with patch.object(cc, "service_client", return_value=sb), \
                patch("app.place_activities.schedule_feature_emoji") as sched:
            cc.upsert_place_feature(place_id="p1", key="has_charging", value=None, **kw)
        return sched

    def test_a_chat_learned_insert_queues_a_pick(self) -> None:
        sched = self._upsert(None)
        sched.assert_called_once_with(["new1"])

    def test_a_row_written_with_its_emoji_queues_nothing(self) -> None:
        self._upsert(None, emoji="🔌").assert_not_called()

    def test_an_update_queues_only_when_the_row_is_still_bare(self) -> None:
        self._upsert({"id": "r1", "confidence": 0.1, "source": "rapport", "emoji": None}).assert_called_once_with(["r1"])
        self._upsert({"id": "r1", "confidence": 0.1, "source": "rapport", "emoji": "🔌"}).assert_not_called()

    def test_fill_asks_the_model_for_the_chip_text_and_never_overwrites(self) -> None:
        from app import place_activities as pa

        tbl = _chain([{"id": "r1", "key": "has_charging_station", "value": None, "sub_group": "",
                       "label": None, "emoji": None}])
        tbl.update.return_value = tbl
        with patch.object(pa, "service_client", return_value=_sb({"place_features": tbl})), \
                patch.object(pa, "feature_emoji", return_value="🔌") as pick:
            self.assertEqual(pa.fill_feature_emoji("r1"), "🔌")
        pick.assert_called_once_with("Charging station")
        tbl.update.assert_called_once_with({"emoji": "🔌"})
        # Conditional on the column still being null — a member's glyph wins a race.
        tbl.is_.assert_any_call("emoji", "null")

    def test_fill_skips_a_row_that_already_has_one(self) -> None:
        from app import place_activities as pa

        tbl = _chain([{"id": "r1", "key": "has_pool", "emoji": "🏊"}])
        with patch.object(pa, "service_client", return_value=_sb({"place_features": tbl})), \
                patch.object(pa, "feature_emoji") as pick:
            self.assertEqual(pa.fill_feature_emoji("r1"), "")
        pick.assert_not_called()
        tbl.update.assert_not_called()

    def test_schedule_is_a_noop_without_a_model(self) -> None:
        from app import place_activities as pa

        with patch("app.orchestrator.llm.llm_configured", return_value=False), \
                patch.object(pa, "_EMOJI_POOL") as pool:
            self.assertEqual(pa.schedule_feature_emoji(["r9"]), 0)
        pool.submit.assert_not_called()

    def test_schedule_dedupes_inflight_rows(self) -> None:
        from app import place_activities as pa

        with patch("app.orchestrator.llm.llm_configured", return_value=True), \
                patch.object(pa, "_EMOJI_POOL") as pool, \
                patch.object(pa, "_EMOJI_INFLIGHT", set()):
            self.assertEqual(pa.schedule_feature_emoji(["r1", "r1", "r2"]), 2)
            self.assertEqual(pa.schedule_feature_emoji(["r1"]), 0)
        self.assertEqual(pool.submit.call_count, 2)

    def test_a_profile_read_heals_bare_rows_only(self) -> None:
        from app import community_surface as cs

        rows = [
            {"id": "r1", "key": "has_pool", "confidence": 0.8, "emoji": "🏊"},
            {"id": "r2", "key": "has_charging_station", "confidence": 0.8, "emoji": None},
            {"id": "r3", "key": "has_sauna", "confidence": 0.1, "emoji": None},  # not served
        ]
        with patch.object(cs, "service_client", return_value=_sb({"place_features": _chain(rows)})), \
                patch("app.place_activities.schedule_feature_emoji") as sched:
            out = cs.place_features("p1")
        self.assertEqual([f["label"] for f in out], ["Pool", "Charging station"])
        sched.assert_called_once_with(["r2"])


if __name__ == "__main__":
    unittest.main()
