"""Onion scoring wrapper (Circles §C) — app/onion.py.

Wrapper behavior only. The SQL body (20260914120000_score_onion_candidates.sql)
is not reachable from Python without a database and is out of scope here.

The rule that matters most: the ZIP gate is enforced in the wrapper and ONLY in
the wrapper (the RPC deliberately does not enforce it), so a blocked area must
never reach the RPC at all — not "call it and discard", never called.

No test in this file may touch a database. service_client is patched at
app.onion.service_client (module-level import); discovery_zip_gate and
gate_framing_facts are patched at app.zip_unlock.* because onion.py imports
them inside the function body, so the name never exists on app.onion.
"""

import unittest
from unittest.mock import MagicMock, patch

from app.onion import score_onion_candidates


def _rpc_result(data):
    m = MagicMock()
    m.rpc.return_value.execute.return_value = MagicMock(data=data)
    return m


_BLOCKED_FRAME = {
    "mode": "hard",
    "blocked": True,
    "zip5": "32827",
    "state": "warming",
    "count": 7,
    "threshold": 10,
}

# Rows as the RPC returns them, already ranked by SQL (score desc).
# NOTE: same_place_bonus is 3 (not 1) — the +3 weight is baked into the column
# by the migration, so the invariant is a plain sum, not a re-weighted one.
_ROW_SAME_PLACE = {
    "peer_user_id": "p1",
    "nickname": "Daniel",
    "avatar_url": None,
    "score": 5,
    "same_place_bonus": 3,
    "same_type_bonus": 0,
    "shared_concept_count": 2,
    "shared_concept_labels": ["Gym enthusiast", "Runner"],
    "shared_place_ref": "11111111-1111-1111-1111-111111111111",
}
_ROW_CONCEPTS_ONLY = {
    "peer_user_id": "p2",
    "nickname": "Sam",
    "avatar_url": "https://cdn.example/p2.jpg",
    "score": 4,
    "same_place_bonus": 0,
    "same_type_bonus": 0,
    "shared_concept_count": 4,
    "shared_concept_labels": ["Reader", "Cyclist", "Baker", "Gardener"],
    "shared_place_ref": None,
}
_ROW_SAME_TYPE = {
    "peer_user_id": "p3",
    "nickname": "Alex",
    "avatar_url": None,
    "score": 1,
    "same_place_bonus": 0,
    "same_type_bonus": 1,
    "shared_concept_count": 0,
    "shared_concept_labels": [],
    "shared_place_ref": None,
}
_ALL_ROWS = [_ROW_SAME_PLACE, _ROW_CONCEPTS_ONLY, _ROW_SAME_TYPE]


class TestOnionGateShortCircuit(unittest.TestCase):
    @patch("app.zip_unlock.gate_framing_facts", return_value=["fact one", "fact two"])
    @patch("app.zip_unlock.discovery_zip_gate", return_value=dict(_BLOCKED_FRAME))
    @patch("app.onion.service_client")
    def test_blocked_gate_never_calls_rpc(self, sb, _gate, _facts) -> None:
        out = score_onion_candidates("u1")
        # The assertion this file exists for: a gated area does not reach SQL.
        sb.return_value.rpc.assert_not_called()
        sb.assert_not_called()
        self.assertTrue(out["gated"])
        self.assertEqual(out["candidates"], [])
        self.assertEqual(out["gate"]["state"], "warming")
        self.assertEqual(out["gate_facts"], ["fact one", "fact two"])

    @patch("app.zip_unlock.discovery_zip_gate", return_value={"blocked": False, "count": 7})
    @patch("app.onion.service_client")
    def test_soft_frame_is_not_blocking(self, sb, _gate) -> None:
        sb.return_value = _rpc_result([dict(_ROW_SAME_TYPE)])
        out = score_onion_candidates("u1")
        self.assertFalse(out["gated"])
        self.assertEqual(len(out["candidates"]), 1)

    @patch("app.zip_unlock.discovery_zip_gate", side_effect=RuntimeError("boom"))
    @patch("app.onion.service_client")
    def test_gate_error_fails_open(self, sb, _gate) -> None:
        sb.return_value = _rpc_result([dict(_ROW_SAME_TYPE)])
        out = score_onion_candidates("u1")
        self.assertFalse(out["gated"])
        self.assertEqual(len(out["candidates"]), 1)


class TestOnionRpcCall(unittest.TestCase):
    @patch("app.zip_unlock.discovery_zip_gate", return_value=None)
    @patch("app.onion.service_client")
    def test_clear_gate_calls_rpc_once_with_params(self, sb, _gate) -> None:
        sb.return_value = _rpc_result([])
        score_onion_candidates("u1")
        sb.return_value.rpc.assert_called_once_with(
            "score_onion_candidates_for_user",
            {"p_user_id": "u1", "p_limit": 20, "p_min_score": 1},
        )

    @patch("app.zip_unlock.discovery_zip_gate", return_value=None)
    @patch("app.onion.service_client")
    def test_explicit_limit_and_min_score_pass_through(self, sb, _gate) -> None:
        sb.return_value = _rpc_result([])
        score_onion_candidates("u9", limit=5, min_score=3)
        payload = sb.return_value.rpc.call_args.args[1]
        self.assertEqual(payload["p_user_id"], "u9")
        self.assertEqual(payload["p_limit"], 5)
        self.assertEqual(payload["p_min_score"], 3)


class TestOnionRowShaping(unittest.TestCase):
    def _candidates(self, rows):
        with patch("app.zip_unlock.discovery_zip_gate", return_value=None), \
             patch("app.onion.service_client") as sb:
            sb.return_value = _rpc_result(rows)
            return score_onion_candidates("u1")["candidates"]

    def test_peer_user_id_is_renamed_to_user_id(self) -> None:
        cand = self._candidates([dict(_ROW_SAME_PLACE)])[0]
        self.assertEqual(cand["user_id"], "p1")
        self.assertNotIn("peer_user_id", cand)

    def test_expected_keys_and_values(self) -> None:
        cand = self._candidates([dict(_ROW_SAME_PLACE)])[0]
        self.assertEqual(
            set(cand),
            {
                "user_id",
                "nickname",
                "avatar_url",
                "score",
                "same_place_bonus",
                "same_type_bonus",
                "shared_concept_count",
                "shared_concept_labels",
                "shared_child_concept_labels",
                "shared_place_ref",
                # Relationship state, carried through the reshape since 2026-08-18: the
                # reply writer sees only this dict, and offering an intro to a peer whose
                # card reads "✓ Sent" is a tap that can only fail the pair cooldown.
                "connection",
            },
        )
        self.assertEqual(cand["nickname"], "Daniel")
        self.assertEqual(cand["shared_concept_labels"], ["Gym enthusiast", "Runner"])
        self.assertEqual(cand["shared_place_ref"], "11111111-1111-1111-1111-111111111111")

    def test_child_concepts_are_split_out_of_the_adults_labels(self) -> None:
        row = dict(_ROW_SAME_PLACE)
        row["shared_concept_labels"] = ["Does karate", "Runner"]
        row["shared_concept_subjects"] = ["child", "self"]
        cand = self._candidates([row])[0]
        self.assertEqual(cand["shared_concept_labels"], ["Runner"])
        self.assertEqual(cand["shared_child_concept_labels"], ["Does karate"])

    def test_rows_without_subjects_stay_the_adults(self) -> None:
        # Pre-20261022 rows carry no subjects array; nothing may silently become
        # a claim about someone's child.
        cand = self._candidates([dict(_ROW_SAME_PLACE)])[0]
        self.assertEqual(cand["shared_child_concept_labels"], [])

    def test_sql_ranking_order_is_preserved(self) -> None:
        cands = self._candidates([dict(r) for r in _ALL_ROWS])
        self.assertEqual([c["user_id"] for c in cands], ["p1", "p2", "p3"])

    def test_score_equals_sum_of_its_components(self) -> None:
        """The wrapper re-weights nothing: components sum to score, unchanged.

        same_place_bonus already carries the +3 weight and same_type_bonus the
        +1, so this is a plain sum. Covers same-place + affinities, same-type
        only, and affinities with no circle at all.
        """
        for cand in self._candidates([dict(r) for r in _ALL_ROWS]):
            with self.subTest(user=cand["user_id"]):
                self.assertEqual(
                    cand["score"],
                    cand["same_place_bonus"]
                    + cand["same_type_bonus"]
                    + cand["shared_concept_count"],
                )

    def test_concepts_only_row_has_no_place(self) -> None:
        cand = self._candidates([dict(_ROW_CONCEPTS_ONLY)])[0]
        self.assertIsNone(cand["shared_place_ref"])
        self.assertEqual(cand["same_place_bonus"], 0)
        self.assertEqual(cand["shared_concept_count"], 4)

    def test_same_type_row_has_no_concepts(self) -> None:
        cand = self._candidates([dict(_ROW_SAME_TYPE)])[0]
        self.assertEqual(cand["same_type_bonus"], 1)
        self.assertEqual(cand["shared_concept_labels"], [])


class TestOnionDegradesSafely(unittest.TestCase):
    def _result(self, *, data=None, rpc_raises=False):
        with patch("app.zip_unlock.discovery_zip_gate", return_value=None), \
             patch("app.onion.service_client") as sb:
            if rpc_raises:
                sb.return_value.rpc.side_effect = RuntimeError("boom")
            else:
                sb.return_value = _rpc_result(data)
            return score_onion_candidates("u1")

    def test_empty_result_is_empty_list_not_none(self) -> None:
        out = self._result(data=[])
        self.assertEqual(out["candidates"], [])
        self.assertIsNotNone(out["candidates"])
        self.assertFalse(out["gated"])

    def test_none_data_returns_safe_default(self) -> None:
        self.assertEqual(self._result(data=None)["candidates"], [])

    def test_non_list_data_returns_safe_default(self) -> None:
        for junk in ({"peer_user_id": "p1"}, "nope", 7):
            with self.subTest(junk=junk):
                self.assertEqual(self._result(data=junk)["candidates"], [])

    def test_non_dict_rows_are_skipped(self) -> None:
        out = self._result(data=["junk", None, dict(_ROW_SAME_TYPE)])
        self.assertEqual([c["user_id"] for c in out["candidates"]], ["p3"])

    def test_rpc_exception_does_not_propagate(self) -> None:
        out = self._result(rpc_raises=True)
        self.assertEqual(out["candidates"], [])
        self.assertFalse(out["gated"])
        self.assertEqual(out["gate_facts"], [])


if __name__ == "__main__":
    unittest.main()
