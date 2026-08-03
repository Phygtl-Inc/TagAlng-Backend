import unittest
from unittest.mock import MagicMock, patch

from app.peer_radius import (
    radius_rpc,
    fetch_peer_matches_within_radius,
    radius_match_enabled,
    radius_meters,
)

_ROW = {
    "peer_user_id": "p1",
    "nickname": "Sam",
    "avatar_url": None,
    "similarity_score": 0.81,
    "matching_peer_label": "Runs at dawn",
    "matching_peer_concept": "running",
    "has_exact_concept_match": True,
    # The point of the whole exercise: an adjacent-block neighbour, 700 m away.
    "distance_meters": 700.0,
    "distance_text": "9 min walk",
}


def _rpc(data):
    m = MagicMock()
    m.rpc.return_value.execute.return_value = MagicMock(data=data)
    return m


class TestFlag(unittest.TestCase):
    def test_default_is_off(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(radius_match_enabled())

    def test_on_values(self) -> None:
        for v in ("1", "on", "true", "YES"):
            with patch.dict("os.environ", {"LANA_PEER_RADIUS_MATCH": v}):
                self.assertTrue(radius_match_enabled(), v)

    def test_off_values(self) -> None:
        for v in ("0", "off", "false", "", "nonsense"):
            with patch.dict("os.environ", {"LANA_PEER_RADIUS_MATCH": v}):
                self.assertFalse(radius_match_enabled(), v)


class TestRadius(unittest.TestCase):
    def test_default(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(radius_meters(), 8000.0)

    def test_override_and_clamp(self) -> None:
        cases = {"1500": 1500.0, "10": 100.0, "9999999": 200000.0, "abc": 8000.0}
        for raw, want in cases.items():
            with patch.dict("os.environ", {"LANA_PEER_RADIUS_METERS": raw}):
                self.assertEqual(radius_meters(), want, raw)


class TestFetch(unittest.TestCase):
    """None means 'use the old block-scoped path'; [] is a real empty result."""

    def test_flag_off_declines(self) -> None:
        with patch.dict("os.environ", {"LANA_PEER_RADIUS_MATCH": "off"}):
            self.assertIsNone(fetch_peer_matches_within_radius("u1"))

    def test_no_user_declines(self) -> None:
        with patch.dict("os.environ", {"LANA_PEER_RADIUS_MATCH": "on"}):
            self.assertIsNone(fetch_peer_matches_within_radius(None))

    @patch("app.peer_radius.service_client")
    def test_rpc_error_declines_rather_than_empties(self, sb) -> None:
        sb.return_value.rpc.side_effect = RuntimeError("boom")
        with patch.dict("os.environ", {"LANA_PEER_RADIUS_MATCH": "on"}):
            # None, not [] — a broken RPC must fall back, not erase the matches
            # the block-scoped path would still have found.
            self.assertIsNone(fetch_peer_matches_within_radius("u1"))

    @patch("app.peer_radius.service_client")
    def test_empty_is_a_real_answer(self, sb) -> None:
        sb.return_value = _rpc([])
        with patch.dict("os.environ", {"LANA_PEER_RADIUS_MATCH": "on"}):
            self.assertEqual(fetch_peer_matches_within_radius("u1"), [])

    @patch("app.peer_radius.service_client")
    def test_returns_rows_and_passes_radius(self, sb) -> None:
        client = _rpc([dict(_ROW)])
        sb.return_value = client
        with patch.dict(
            "os.environ",
            {"LANA_PEER_RADIUS_MATCH": "on", "LANA_PEER_RADIUS_METERS": "5000"},
        ):
            out = fetch_peer_matches_within_radius("u1", limit=3, locale="es")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["distance_text"], "9 min walk")
        name, args = client.rpc.call_args[0]
        self.assertEqual(name, "match_peers_within_radius")
        self.assertEqual(args["p_radius_meters"], 5000.0)
        self.assertEqual(args["p_user_id"], "u1")
        self.assertEqual(args["p_limit"], 3)
        self.assertEqual(args["p_locale"], "es")


class TestRadiusRpcResolver(unittest.TestCase):
    def test_off_returns_original_untouched(self) -> None:
        with patch.dict("os.environ", {"LANA_PEER_RADIUS_MATCH": "off"}):
            name, args = radius_rpc("find_peers_by_attr_filter", {"p_limit": 5})
        self.assertEqual(name, "find_peers_by_attr_filter")
        self.assertEqual(args, {"p_limit": 5})

    def test_on_returns_near_twin_with_radius(self) -> None:
        with patch.dict(
            "os.environ",
            {"LANA_PEER_RADIUS_MATCH": "on", "LANA_PEER_RADIUS_METERS": "3000"},
        ):
            name, args = radius_rpc("find_peers_by_claim_semantic", {"p_limit": 5})
        self.assertEqual(name, "find_peers_by_claim_semantic_near")
        self.assertEqual(args, {"p_limit": 5, "p_radius_meters": 3000.0})

    def test_does_not_mutate_caller_args(self) -> None:
        original = {"p_limit": 5}
        with patch.dict("os.environ", {"LANA_PEER_RADIUS_MATCH": "on"}):
            radius_rpc("find_peers_by_attr_filter", original)
        self.assertEqual(original, {"p_limit": 5})


class TestLayer1Dispatch(unittest.TestCase):
    """The four attr/semantic paths swap RPC name without changing transport."""

    @patch("app.layer1_handlers.call_rpc")
    def test_flag_off_calls_original(self, rpc) -> None:
        from app.layer1_handlers import _call_peer_rpc

        rpc.return_value = []
        with patch.dict("os.environ", {"LANA_PEER_RADIUS_MATCH": "off"}):
            _call_peer_rpc("jwt", "find_peers_by_attr_filter", {"p_limit": 5})
        self.assertEqual(rpc.call_args[0][1], "find_peers_by_attr_filter")

    @patch("app.layer1_handlers.call_rpc")
    def test_flag_on_calls_near_twin(self, rpc) -> None:
        from app.layer1_handlers import _call_peer_rpc

        rpc.return_value = []
        with patch.dict("os.environ", {"LANA_PEER_RADIUS_MATCH": "on"}):
            _call_peer_rpc("jwt", "find_peers_by_attr_filter", {"p_limit": 5})
        self.assertEqual(rpc.call_args[0][1], "find_peers_by_attr_filter_near")
        self.assertEqual(rpc.call_args[0][2]["p_radius_meters"], 8000.0)

    @patch("app.layer1_handlers.call_rpc")
    def test_near_failure_falls_back_to_original(self, rpc) -> None:
        from app.layer1_handlers import _call_peer_rpc

        calls: list[str] = []

        def side_effect(_jwt, name, _payload):
            calls.append(name)
            if name.endswith("_near"):
                raise RuntimeError("missing function")
            return [{"peer_user_id": "p1"}]

        rpc.side_effect = side_effect
        with patch.dict("os.environ", {"LANA_PEER_RADIUS_MATCH": "on"}):
            out = _call_peer_rpc("jwt", "find_peers_by_attr_filter", {"p_limit": 5})
        # A radius twin that is missing or broken must never cost the user the
        # block-scoped matches they were already getting.
        self.assertEqual(calls, ["find_peers_by_attr_filter_near", "find_peers_by_attr_filter"])
        self.assertEqual(out, [{"peer_user_id": "p1"}])

    @patch("app.layer1_handlers.call_rpc")
    def test_original_error_still_propagates(self, rpc) -> None:
        from app.layer1_handlers import _call_peer_rpc

        rpc.side_effect = RuntimeError("db down")
        with patch.dict("os.environ", {"LANA_PEER_RADIUS_MATCH": "off"}):
            # Callers distinguish "the RPC failed" from "no matches" — swallowing
            # this would turn an outage into a silent empty result.
            with self.assertRaises(RuntimeError):
                _call_peer_rpc("jwt", "find_peers_by_claim_filters", {})


class TestContextPackHints(unittest.TestCase):
    @patch("app.context.service_client")
    def test_flag_on_uses_near_twin(self, sb) -> None:
        from app.context import _load_vector_peer_hints

        sb.return_value.rpc.return_value.execute.return_value = MagicMock(data=[])
        with patch.dict("os.environ", {"LANA_PEER_RADIUS_MATCH": "on"}):
            _load_vector_peer_hints("u1")
        name, payload = sb.return_value.rpc.call_args[0]
        self.assertEqual(name, "match_peers_by_claim_vectors_for_user_near")
        self.assertEqual(payload["p_radius_meters"], 8000.0)

    @patch("app.context.service_client")
    def test_near_failure_falls_back(self, sb) -> None:
        from app.context import _load_vector_peer_hints

        names: list[str] = []

        def rpc(name, _payload):
            names.append(name)
            m = MagicMock()
            if name.endswith("_near"):
                m.execute.side_effect = RuntimeError("missing")
            else:
                m.execute.return_value = MagicMock(data=[{"peer_user_id": "p1"}])
            return m

        sb.return_value.rpc.side_effect = rpc
        with patch.dict("os.environ", {"LANA_PEER_RADIUS_MATCH": "on"}):
            out = _load_vector_peer_hints("u1")
        self.assertEqual(
            names,
            [
                "match_peers_by_claim_vectors_for_user_near",
                "match_peers_by_claim_vectors_for_user",
            ],
        )
        self.assertEqual(out, [{"peer_user_id": "p1"}])


class TestCallSiteFallback(unittest.TestCase):
    """_fetch_verified_peer_matches must keep working when radius declines."""

    @patch("app.discovery_route.blend_onion_matches", create=True)
    @patch("app.discovery_route.fetch_peer_matches")
    @patch("app.discovery_route.fetch_peer_matches_within_radius", return_value=None)
    @patch("app.discovery_route.kick_claim_embedding_backfill")
    def test_declined_radius_uses_block_path(
        self, _kick, radius, block_path, _blend
    ) -> None:
        from app.discovery_route import _fetch_verified_peer_matches

        block_path.return_value = [{"peer_user_id": "old"}]
        _fetch_verified_peer_matches("jwt", user_id="u1", block_id="b1", limit=5)
        radius.assert_called_once()
        block_path.assert_called_once()

    @patch("app.discovery_route.blend_onion_matches", create=True)
    @patch("app.discovery_route.fetch_peer_matches")
    @patch("app.discovery_route.fetch_peer_matches_within_radius", return_value=[])
    @patch("app.discovery_route.kick_claim_embedding_backfill")
    def test_real_empty_does_not_fall_back(
        self, _kick, _radius, block_path, _blend
    ) -> None:
        from app.discovery_route import _fetch_verified_peer_matches

        _fetch_verified_peer_matches("jwt", user_id="u1", block_id="b1", limit=5)
        # Located and searched, nobody near — asking the block path too would
        # reintroduce exactly the equality behaviour the radius replaces.
        block_path.assert_not_called()


if __name__ == "__main__":
    unittest.main()
