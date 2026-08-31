import unittest
from unittest.mock import MagicMock, patch


def _sc_mock(claim_rows: list[dict]) -> tuple[MagicMock, MagicMock]:
    """Chainable service_client() mock. Returns (client_mock, table_method_mock)
    so the test can assert exactly-once on client_mock.table."""
    execute = MagicMock()
    execute.return_value.data = claim_rows
    chain = MagicMock()
    chain.select.return_value.in_.return_value.in_.return_value.execute = execute
    client = MagicMock()
    client.table.return_value = chain
    return client, chain


class TestIntroStaleness(unittest.TestCase):
    def test_dismissed_after_created_blanks_reason(self) -> None:
        # Intro was created at t=100. Initiator dismissed their food.pizza claim
        # at t=200, well after the intro. The persisted reason must not surface.
        rpc_rows = [
            {
                "id": "intro-1",
                "other_user_id": "u-candidate",
                "created_at": "2026-08-01T00:00:00Z",
                "match_reason": "You both: Pizza lovers",
                "shared_dimensions": ["food.pizza"],
                "direction": "sent",
                "status": "proposed",
            }
        ]
        claim_rows = [
            {
                "user_id": "u-caller",
                "concept": "food.pizza",
                "dismissed_at": "2026-08-05T00:00:00Z",
            }
        ]
        client, _ = _sc_mock(claim_rows)
        with (
            patch("app.intro_list.call_rpc", return_value=rpc_rows),
            patch("app.auth.jwt_user_id", return_value="u-caller"),
            patch("app.auth.service_client", return_value=client),
        ):
            from app.intro_list import fetch_my_intros, normalize_intro_row

            rows = fetch_my_intros("jwt", direction="all")
            self.assertEqual(len(rows), 1)
            self.assertTrue(rows[0].get("_stale_reason"))
            norm = normalize_intro_row(rows[0])
        self.assertEqual(norm["match_reason"], "")

    def test_dismissed_before_created_preserves_reason(self) -> None:
        # Claim was dismissed BEFORE the intro was written — that dismissal is
        # not the source of a lie; the reason still reflects reality at write time.
        rpc_rows = [
            {
                "id": "intro-1",
                "other_user_id": "u-candidate",
                "created_at": "2026-08-10T00:00:00Z",
                "match_reason": "You both: Pizza lovers",
                "shared_dimensions": ["food.pizza"],
                "direction": "sent",
                "status": "proposed",
            }
        ]
        claim_rows = [
            {
                "user_id": "u-caller",
                "concept": "food.pizza",
                "dismissed_at": "2026-08-01T00:00:00Z",
            }
        ]
        client, _ = _sc_mock(claim_rows)
        with (
            patch("app.intro_list.call_rpc", return_value=rpc_rows),
            patch("app.auth.jwt_user_id", return_value="u-caller"),
            patch("app.auth.service_client", return_value=client),
        ):
            from app.intro_list import fetch_my_intros, normalize_intro_row

            rows = fetch_my_intros("jwt", direction="all")
            self.assertFalse(rows[0].get("_stale_reason"))
            norm = normalize_intro_row(rows[0])
        self.assertEqual(norm["match_reason"], "You both: Pizza lovers")

    def test_no_dismissed_claims_preserves_reason(self) -> None:
        # No dismissed claims at all — nothing to blank.
        rpc_rows = [
            {
                "id": "intro-1",
                "other_user_id": "u-candidate",
                "created_at": "2026-08-10T00:00:00Z",
                "match_reason": "You both: Runners",
                "shared_dimensions": ["sport.running"],
                "direction": "sent",
                "status": "proposed",
            }
        ]
        client, _ = _sc_mock([])
        with (
            patch("app.intro_list.call_rpc", return_value=rpc_rows),
            patch("app.auth.jwt_user_id", return_value="u-caller"),
            patch("app.auth.service_client", return_value=client),
        ):
            from app.intro_list import fetch_my_intros, normalize_intro_row

            rows = fetch_my_intros("jwt", direction="all")
            self.assertFalse(rows[0].get("_stale_reason"))
            norm = normalize_intro_row(rows[0])
        self.assertEqual(norm["match_reason"], "You both: Runners")

    def test_exactly_one_supplementary_query_for_n_intros(self) -> None:
        # Six intros, one batch fetch — not six.
        rpc_rows = [
            {
                "id": f"intro-{i}",
                "other_user_id": f"u-cand-{i}",
                "created_at": "2026-08-01T00:00:00Z",
                "match_reason": f"reason {i}",
                "shared_dimensions": ["food.pizza", "sport.running"],
                "direction": "sent",
                "status": "proposed",
            }
            for i in range(6)
        ]
        client, _ = _sc_mock([])
        with (
            patch("app.intro_list.call_rpc", return_value=rpc_rows),
            patch("app.auth.jwt_user_id", return_value="u-caller"),
            patch("app.auth.service_client", return_value=client),
        ):
            from app.intro_list import fetch_my_intros

            rows = fetch_my_intros("jwt", direction="all")
        self.assertEqual(len(rows), 6)
        table_calls = [
            c for c in client.table.call_args_list if c.args and c.args[0] == "user_identity_claims"
        ]
        self.assertEqual(
            len(table_calls),
            1,
            f"expected exactly one supplementary query, got {len(table_calls)}",
        )

    def test_no_shared_dimensions_skips_query_entirely(self) -> None:
        # Historical rows written before Phase 1 have empty dims and can't be
        # detected as stale — that's the known limitation. Don't waste the query.
        rpc_rows = [
            {
                "id": "intro-old",
                "other_user_id": "u-cand",
                "created_at": "2026-07-01T00:00:00Z",
                "match_reason": "old reason",
                "shared_dimensions": [],
                "direction": "sent",
                "status": "proposed",
            }
        ]
        client, _ = _sc_mock([])
        with (
            patch("app.intro_list.call_rpc", return_value=rpc_rows),
            patch("app.auth.jwt_user_id", return_value="u-caller"),
            patch("app.auth.service_client", return_value=client),
        ):
            from app.intro_list import fetch_my_intros, normalize_intro_row

            rows = fetch_my_intros("jwt", direction="all")
            norm = normalize_intro_row(rows[0])
        # Zero supplementary queries when there are no concepts to check.
        self.assertEqual(client.table.call_count, 0)
        self.assertEqual(norm["match_reason"], "old reason")

    def test_dismissed_on_candidate_side_also_blanks(self) -> None:
        # Both sides of the pair count — if the candidate dismisses their side
        # of the shared trait, the "You both" claim is now half a lie.
        rpc_rows = [
            {
                "id": "intro-1",
                "other_user_id": "u-candidate",
                "created_at": "2026-08-01T00:00:00Z",
                "match_reason": "You both: Runners",
                "shared_dimensions": ["sport.running"],
                "direction": "sent",
                "status": "proposed",
            }
        ]
        claim_rows = [
            {
                "user_id": "u-candidate",
                "concept": "sport.running",
                "dismissed_at": "2026-08-05T00:00:00Z",
            }
        ]
        client, _ = _sc_mock(claim_rows)
        with (
            patch("app.intro_list.call_rpc", return_value=rpc_rows),
            patch("app.auth.jwt_user_id", return_value="u-caller"),
            patch("app.auth.service_client", return_value=client),
        ):
            from app.intro_list import fetch_my_intros, normalize_intro_row

            rows = fetch_my_intros("jwt", direction="all")
            norm = normalize_intro_row(rows[0])
        self.assertEqual(norm["match_reason"], "")


if __name__ == "__main__":
    unittest.main()
