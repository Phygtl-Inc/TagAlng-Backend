"""/lana/fellows location: the distance it already had, the area name, and a pin.

backend-asks §33 (a row carries no location), §36 (a device pin as the search anchor)
and §30(c) (distance_meters + match_strength as the "Nearest" / "Best fit" sort keys).
"""

import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.auth import AuthSession
from app.layer1_handlers import attach_peer_area_names, peers_to_match_rows
from app.main import FellowsBody, _peer_matches_from_ctx, app, post_fellows
from app.peer_radius import fetch_peer_matches_near_point
from app.tip_rec_cascade import peer_rows_from_neighbor_tips

AUTH = "Bearer test-token"


def _auth(*, verified: bool = True, block: str | None = "block-a") -> AuthSession:
    return AuthSession(
        user_id="u-caller", is_anonymous=False, phone_verified=verified, home_block_id=block
    )


def _peer(n: int, *, meters: float | None = None, text: str | None = None) -> dict:
    row = {
        "peer_user_id": f"p-{n}",
        "nickname": f"Peer{n}",
        "avatar_url": None,
        "similarity_score": 0.8,
        "matching_peer_label": "Runs at dawn",
        "has_exact_concept_match": False,
    }
    if meters is not None:
        row["distance_meters"] = meters
    if text is not None:
        row["distance_text"] = text
    return row


class _FakeSb:
    """users → home_block_id, blocks → display_name; records every table read."""

    def __init__(self, users: dict[str, str | None], blocks: dict[str, str | None]):
        self.users = users
        self.blocks = blocks
        self.reads: list[str] = []

    def table(self, name: str):
        self.reads.append(name)
        sb = self
        q = MagicMock()

        def _in(_col, ids):
            res = MagicMock()
            if name == "users":
                data = [
                    {"id": i, "home_block_id": sb.users[i]} for i in ids if i in sb.users
                ]
            else:
                data = [
                    {"id": i, "display_name": sb.blocks[i]} for i in ids if i in sb.blocks
                ]
            res.execute.return_value = MagicMock(data=data)
            return res

        q.select.return_value.in_.side_effect = _in
        return q


# ── §33(a): distance forwarded, never guessed ─────────────────────────────────────────


class TestDistanceForwarded(unittest.TestCase):
    def test_radius_row_keeps_its_distance(self) -> None:
        rows = peers_to_match_rows(
            [_peer(1, meters=640.0, text="8 min walk")], phone_verified=True
        )
        self.assertEqual(rows[0]["distance_text"], "8 min walk")
        self.assertEqual(rows[0]["distance_meters"], 640.0)

    def test_block_scoped_row_stays_null_not_a_guess(self) -> None:
        # match_peers_by_claim_vectors measures nothing: no distance must become no field.
        rows = peers_to_match_rows([_peer(1)], phone_verified=True)
        self.assertIsNone(rows[0]["distance_text"])
        self.assertIsNone(rows[0]["distance_meters"])

    def test_unverified_caller_still_reads_distance_only(self) -> None:
        rows = peers_to_match_rows(
            [_peer(1, meters=640.0, text="8 min walk")], phone_verified=False
        )
        self.assertIsNone(rows[0]["nickname"])
        self.assertEqual(rows[0]["distance_text"], "8 min walk")
        self.assertEqual(rows[0]["distance_meters"], 640.0)

    def test_garbage_meters_are_dropped(self) -> None:
        for bad in ("far", -3, True):
            rows = peers_to_match_rows([_peer(1, meters=bad)], phone_verified=True)
            self.assertIsNone(rows[0]["distance_meters"], bad)


# ── §33(b): the area name, gated for unverified ───────────────────────────────────────


class TestAreaName(unittest.TestCase):
    def _attach(self, peers, *, verified=True, users=None, blocks=None):
        rows = peers_to_match_rows(peers, phone_verified=verified)
        sb = _FakeSb(users or {}, blocks or {})
        with patch("app.layer1_handlers.service_client", return_value=sb):
            attach_peer_area_names(rows, peers, phone_verified=verified)
        return rows, sb

    def test_verified_caller_gets_the_peers_home_area(self) -> None:
        rows, _ = self._attach(
            [_peer(1), _peer(2), _peer(3)],
            users={"p-1": "blk-lp", "p-2": None, "p-3": "blk-gone"},
            blocks={"blk-lp": "Laureate Park"},
        )
        self.assertEqual(
            [r["area_name"] for r in rows], ["Laureate Park", None, None]
        )

    def test_label_is_cleaned_like_everywhere_else(self) -> None:
        rows, _ = self._attach(
            [_peer(1)],
            users={"p-1": "blk-a"},
            blocks={"blk-a": "Lake Nona — Block A (placeholder)"},
        )
        self.assertEqual(rows[0]["area_name"], "Lake Nona — Area A")

    def test_unverified_caller_gets_none_on_every_row_and_no_lookup(self) -> None:
        rows, sb = self._attach(
            [_peer(1), _peer(2)],
            verified=False,
            users={"p-1": "blk-lp", "p-2": "blk-lp"},
            blocks={"blk-lp": "Laureate Park"},
        )
        self.assertEqual([r["area_name"] for r in rows], [None, None])
        self.assertEqual(sb.reads, [])

    def test_lookup_failure_costs_the_label_not_the_list(self) -> None:
        rows = peers_to_match_rows([_peer(1)], phone_verified=True)
        with patch(
            "app.layer1_handlers.service_client", side_effect=RuntimeError("down")
        ):
            attach_peer_area_names(rows, [_peer(1)], phone_verified=True)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["area_name"])


# ── §36: a pin is the anchor ──────────────────────────────────────────────────────────


def _rpc(data=None, *, boom=False):
    m = MagicMock()
    if boom:
        m.rpc.return_value.execute.side_effect = RuntimeError("rpc down")
    else:
        m.rpc.return_value.execute.return_value = MagicMock(data=data)
    return m


class TestNearPointFetch(unittest.TestCase):
    def test_runs_with_the_radius_flag_off(self) -> None:
        sb = _rpc([_peer(1, meters=300.0, text="4 min walk")])
        with (
            patch.dict("os.environ", {"LANA_PEER_RADIUS_MATCH": "off"}),
            patch("app.peer_radius.service_client", return_value=sb),
            patch("app.peer_radius.radius_meters", return_value=8000.0),
            patch(
                "app.peer_discovery_surface.drop_connected_peers",
                side_effect=lambda rows, user_id: rows,
            ),
        ):
            got = fetch_peer_matches_near_point("u-caller", lat=28.4, lng=-81.2, limit=7)
        self.assertEqual([p["peer_user_id"] for p in got], ["p-1"])
        name, args = sb.rpc.call_args.args
        self.assertEqual(name, "match_peers_near_point")
        self.assertEqual(
            args,
            {
                "p_user_id": "u-caller",
                "p_lat": 28.4,
                "p_lng": -81.2,
                "p_radius_meters": 8000.0,
                "p_limit": 7,
                "p_locale": "en",
            },
        )

    def test_rpc_failure_is_none_not_empty(self) -> None:
        with (
            patch("app.peer_radius.service_client", return_value=_rpc(boom=True)),
            patch("app.peer_radius.radius_meters", return_value=8000.0),
        ):
            self.assertIsNone(
                fetch_peer_matches_near_point("u-caller", lat=1.0, lng=2.0)
            )


class TestFellowsPin(unittest.TestCase):
    def _call(self, body, *, auth=None, near=None, home=None):
        with (
            patch("app.main.verify_auth", return_value=auth or _auth()),
            patch(
                "app.peer_radius.fetch_peer_matches_near_point", return_value=near
            ) as near_fetch,
            patch(
                "app.discovery_route._fetch_verified_peer_matches",
                return_value=home if home is not None else [_peer(9)],
            ) as home_fetch,
            patch("app.peer_discovery_surface.stamp_reachability") as stamp,
            patch("app.peer_rec_line.attach_rec_lines"),
            patch("app.layer1_handlers.service_client", side_effect=RuntimeError),
        ):
            res = post_fellows(body, authorization=AUTH)
        return res, near_fetch, home_fetch, stamp

    def test_pin_searches_around_the_pin_not_home(self) -> None:
        res, near, home, stamp = self._call(
            FellowsBody(lat=28.37, lng=-81.24),
            near=[_peer(1, meters=480.0, text="6 min walk")],
        )
        near.assert_called_once()
        self.assertEqual(near.call_args.kwargs["lat"], 28.37)
        self.assertEqual(near.call_args.kwargs["lng"], -81.24)
        home.assert_not_called()
        stamp.assert_called_once()
        self.assertEqual([f.peer_user_id for f in res.fellows], ["p-1"])
        self.assertEqual(res.fellows[0].distance_text, "6 min walk")
        self.assertEqual(res.fellows[0].distance_meters, 480.0)

    def test_pin_with_nobody_near_is_an_honest_empty_list(self) -> None:
        res, _, home, _ = self._call(FellowsBody(lat=40.7, lng=-74.0), near=[])
        self.assertEqual(res.fellows, [])
        home.assert_not_called()

    def test_pin_search_failure_never_falls_back_to_home(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            self._call(FellowsBody(lat=40.7, lng=-74.0), near=None)
        self.assertEqual(caught.exception.status_code, 502)
        self.assertEqual(caught.exception.detail, "fellows_pin_search_failed")

    def test_no_pin_is_the_unchanged_home_path(self) -> None:
        res, near, home, _ = self._call(FellowsBody(limit=5))
        near.assert_not_called()
        home.assert_called_once()
        self.assertEqual(home.call_args.kwargs["block_id"], "block-a")
        self.assertEqual(home.call_args.kwargs["limit"], 5)
        self.assertEqual([f.peer_user_id for f in res.fellows], ["p-9"])

    def test_half_a_pin_is_refused(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            self._call(FellowsBody(lat=28.4))
        self.assertEqual(caught.exception.status_code, 422)

    def test_out_of_range_pin_is_a_validation_error(self) -> None:
        with self.assertRaises(ValidationError):
            FellowsBody(lat=91, lng=0)
        with self.assertRaises(ValidationError):
            FellowsBody(lat=0, lng=-181)

    def test_a_community_filter_outranks_the_pin(self) -> None:
        # The scope pill is ONE of area / community / around-me; with place_id the
        # community is the scope and the pin is ignored.
        with patch("app.community_surface.caller_affiliation_at", return_value=True), patch(
            "app.community_surface._member_rows",
            return_value=[{"user_id": "p-9", "status": "confirmed"}],
        ):
            res, near, home, _ = self._call(
                FellowsBody(lat=28.4, lng=-81.2, place_id="place-1"), near=[_peer(1)]
            )
        near.assert_not_called()
        home.assert_called_once()
        self.assertEqual([f.peer_user_id for f in res.fellows], ["p-9"])

    def test_pin_works_without_a_home_block(self) -> None:
        res, near, _, _ = self._call(
            FellowsBody(lat=28.4, lng=-81.2), auth=_auth(block=None), near=[_peer(1)]
        )
        near.assert_called_once()
        self.assertEqual(len(res.fellows), 1)

    def test_no_pin_and_no_home_is_still_home_block_missing(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            self._call(FellowsBody(), auth=_auth(block=None))
        self.assertEqual(caught.exception.detail, "home_block_missing")


# ── The wire: what the frontend actually parses ───────────────────────────────────────


class TestFellowsWire(unittest.TestCase):
    def _post(self, body, *, verified: bool):
        peers = [_peer(1, meters=480.0, text="6 min walk"), _peer(2)]
        sb = _FakeSb({"p-1": "blk-lp", "p-2": "blk-lp"}, {"blk-lp": "Laureate Park"})
        with (
            patch("app.main.verify_auth", return_value=_auth(verified=verified)),
            patch("app.discovery_route._fetch_verified_peer_matches", return_value=peers),
            patch("app.peer_rec_line.attach_rec_lines"),
            patch("app.layer1_handlers.service_client", return_value=sb),
        ):
            return TestClient(app).post(
                "/lana/fellows", json=body, headers={"Authorization": AUTH}
            )

    def test_verified_row_carries_area_and_distance(self) -> None:
        resp = self._post({}, verified=True)
        self.assertEqual(resp.status_code, 200, resp.text)
        a, b = resp.json()["fellows"]
        self.assertEqual(a["area_name"], "Laureate Park")
        self.assertEqual(a["distance_text"], "6 min walk")
        self.assertEqual(a["distance_meters"], 480.0)
        # A block-scoped row has no distance, so the keys are absent (exclude_none).
        self.assertEqual(b["area_name"], "Laureate Park")
        self.assertNotIn("distance_text", b)
        self.assertNotIn("distance_meters", b)

    def test_unverified_rows_are_distance_only_on_every_row(self) -> None:
        resp = self._post({}, verified=False)
        self.assertEqual(resp.status_code, 200, resp.text)
        rows = resp.json()["fellows"]
        self.assertTrue(all("area_name" not in r for r in rows))
        self.assertEqual(rows[0]["distance_text"], "6 min walk")

    def test_out_of_range_pin_is_422(self) -> None:
        resp = self._post({"lat": 120, "lng": 0}, verified=True)
        self.assertEqual(resp.status_code, 422)


# ── §30(c): the two sort keys on PeerMatchRow ─────────────────────────────────────────


class TestSortKeys(unittest.TestCase):
    def _tip(self, **over):
        tip = {
            "signal_id": "sig-1",
            "detail_text": "Dr. Reyes — gentle with toddlers",
            "peer_user_id": "peer-1",
            "neighbor_label": "Marisol",
            "match_strength": 0.82,
            "distance_meters": 640.0,
            "distance_text": "8 min walk",
        }
        tip.update(over)
        return tip

    def test_rec_row_ships_both_numbers(self) -> None:
        rows = _peer_matches_from_ctx(
            {"peer_matches": peer_rows_from_neighbor_tips([self._tip()])}
        )
        self.assertEqual(rows[0].match_strength, 0.82)
        self.assertEqual(rows[0].distance_meters, 640.0)
        self.assertEqual(rows[0].distance_text, "8 min walk")

    def test_unscored_rec_is_null_not_zero(self) -> None:
        tip = self._tip()
        del tip["match_strength"]
        tip["distance_meters"] = None
        rows = _peer_matches_from_ctx(
            {"peer_matches": peer_rows_from_neighbor_tips([tip])}
        )
        self.assertIsNone(rows[0].match_strength)
        self.assertIsNone(rows[0].distance_meters)

    def test_claim_affinity_row_has_no_match_strength(self) -> None:
        shaped = peers_to_match_rows(
            [_peer(1, meters=700.0, text="9 min walk")], phone_verified=True
        )
        rows = _peer_matches_from_ctx({"peer_matches": shaped})
        self.assertIsNone(rows[0].match_strength)
        self.assertEqual(rows[0].distance_meters, 700.0)


if __name__ == "__main__":
    unittest.main()
