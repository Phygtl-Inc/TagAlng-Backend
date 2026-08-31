import unittest
from unittest.mock import MagicMock, patch

from app.orchestrator.tools import _pair_claim_overlap, _propose_intro


def _make_sc_mock(rows: list[dict]) -> MagicMock:
    """Build a chainable service_client() mock that returns `rows` from execute()."""
    execute = MagicMock()
    execute.return_value.data = rows
    chain = MagicMock()
    chain.table.return_value.select.return_value.in_.return_value.eq.return_value.is_.return_value.execute = execute
    return chain


class TestPairClaimOverlap(unittest.TestCase):
    def test_shared_concept_returns_two_sided(self) -> None:
        rows = [
            {"user_id": "u1", "concept": "food.pizza", "label": "Pizza lover"},
            {"user_id": "u2", "concept": "food.pizza", "label": "Pizza fan"},
        ]
        with patch("app.orchestrator.tools.service_client", return_value=_make_sc_mock(rows)):
            result = _pair_claim_overlap("u1", "u2")
        self.assertTrue(result["has_exact_concept_match"])
        self.assertEqual(result["matching_my_label"], "Pizza lover")
        self.assertEqual(result["matching_peer_label"], "Pizza fan")
        self.assertEqual(result["matching_peer_concept"], "food.pizza")

    def test_no_shared_concept_returns_false(self) -> None:
        rows = [
            {"user_id": "u1", "concept": "sport.running", "label": "Runner"},
            {"user_id": "u2", "concept": "food.pizza", "label": "Pizza fan"},
        ]
        with patch("app.orchestrator.tools.service_client", return_value=_make_sc_mock(rows)):
            result = _pair_claim_overlap("u1", "u2")
        self.assertFalse(result["has_exact_concept_match"])
        self.assertIsNone(result["matching_peer_label"])

    def test_empty_caller_id_returns_false(self) -> None:
        result = _pair_claim_overlap("", "u2")
        self.assertFalse(result["has_exact_concept_match"])

    def test_service_error_returns_false(self) -> None:
        sc = MagicMock()
        sc.table.side_effect = RuntimeError("db down")
        with patch("app.orchestrator.tools.service_client", return_value=sc):
            result = _pair_claim_overlap("u1", "u2")
        self.assertFalse(result["has_exact_concept_match"])


class TestProposeIntroTool(unittest.TestCase):
    def _run(
        self,
        *,
        rows: list[dict],
        args: dict,
        caller_id: str = "caller-1",
        candidate_id: str = "candidate-2",
    ) -> tuple[dict, str | None]:
        """Run _propose_intro and return (result, match_reason_at_rpc)."""
        propose_return = {
            "intro_id": "intro-test",
            "candidate_user_id": candidate_id,
            "match_reason": "stored",
        }
        sc_mock = _make_sc_mock(rows)
        with (
            patch("app.orchestrator.tools.service_client", return_value=sc_mock),
            patch("app.auth.jwt_user_id", return_value=caller_id),
            patch("app.intro_proposal.propose_neighbor_intro", return_value=propose_return) as mock_propose,
        ):
            result = _propose_intro(user_jwt="valid-jwt", args=args)
            reason = mock_propose.call_args.kwargs.get("match_reason") if mock_propose.called else None
        return result, reason

    def test_ignores_llm_reason_no_shared_concept(self) -> None:
        # Neither user claims hang-gliding — the LLM's hallucination must not reach the DB.
        rows = [
            {"user_id": "caller-1", "concept": "sport.running", "label": "Runner"},
            {"user_id": "candidate-2", "concept": "food.pizza", "label": "Pizza fan"},
        ]
        result, reason = self._run(
            rows=rows,
            args={"other_user_id": "candidate-2", "match_reason": "You both love hang-gliding"},
        )
        self.assertEqual(result["status"], "ok")
        self.assertIsNotNone(reason)
        self.assertNotIn("hang-gliding", (reason or "").lower())
        self.assertIn("click", (reason or "").lower())

    def test_shared_concept_produces_you_both(self) -> None:
        rows = [
            {"user_id": "caller-1", "concept": "food.pizza", "label": "Pizza lover"},
            {"user_id": "candidate-2", "concept": "food.pizza", "label": "Pizza fan"},
        ]
        result, reason = self._run(
            rows=rows,
            args={"other_user_id": "candidate-2", "match_reason": "LLM noise"},
        )
        self.assertEqual(result["status"], "ok")
        self.assertIsNotNone(reason)
        self.assertIn("you both:", (reason or "").lower())

    def test_no_shared_concept_produces_neutral(self) -> None:
        rows = [
            {"user_id": "caller-1", "concept": "sport.running", "label": "Runner"},
            {"user_id": "candidate-2", "concept": "music.jazz", "label": "Jazz fan"},
        ]
        result, reason = self._run(
            rows=rows,
            args={"other_user_id": "candidate-2", "match_reason": "LLM noise"},
        )
        self.assertEqual(result["status"], "ok")
        self.assertIn("click", (reason or "").lower())
        self.assertNotIn("you both:", (reason or "").lower())

    def test_missing_candidate_returns_error(self) -> None:
        result, _ = self._run(rows=[], args={"match_reason": "Some reason"})
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["reason"], "other_user_id_required")

    def test_missing_jwt_returns_error(self) -> None:
        result = _propose_intro(user_jwt=None, args={"other_user_id": "u2"})
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["reason"], "auth_required")

    def test_shared_concept_dims_passed_to_rpc(self) -> None:
        rows = [
            {"user_id": "caller-1", "concept": "food.pizza", "label": "Pizza lover"},
            {"user_id": "candidate-2", "concept": "food.pizza", "label": "Pizza fan"},
        ]
        propose_return = {
            "intro_id": "intro-1",
            "candidate_user_id": "candidate-2",
            "match_reason": "stored",
        }
        sc_mock = _make_sc_mock(rows)
        with (
            patch("app.orchestrator.tools.service_client", return_value=sc_mock),
            patch("app.auth.jwt_user_id", return_value="caller-1"),
            patch("app.intro_proposal.propose_neighbor_intro", return_value=propose_return) as mock_propose,
        ):
            _propose_intro(user_jwt="jwt", args={"other_user_id": "candidate-2"})
        dims = mock_propose.call_args.kwargs.get("shared_dimensions")
        self.assertEqual(dims, ["food.pizza"])

    def test_no_shared_concept_dims_empty(self) -> None:
        rows = [
            {"user_id": "caller-1", "concept": "sport.running", "label": "Runner"},
            {"user_id": "candidate-2", "concept": "music.jazz", "label": "Jazz fan"},
        ]
        propose_return = {
            "intro_id": "intro-2",
            "candidate_user_id": "candidate-2",
            "match_reason": "stored",
        }
        sc_mock = _make_sc_mock(rows)
        with (
            patch("app.orchestrator.tools.service_client", return_value=sc_mock),
            patch("app.auth.jwt_user_id", return_value="caller-1"),
            patch("app.intro_proposal.propose_neighbor_intro", return_value=propose_return) as mock_propose,
        ):
            _propose_intro(user_jwt="jwt", args={"other_user_id": "candidate-2"})
        dims = mock_propose.call_args.kwargs.get("shared_dimensions")
        self.assertEqual(dims, [])


if __name__ == "__main__":
    unittest.main()
