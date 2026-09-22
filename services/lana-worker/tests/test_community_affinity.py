"""/lana/circles/discover says how well the caller fits each community, and why.

Three things are under test, in the order the endpoint does them:
  * the 0-1 blend (app/community_affinity.py) — and specifically that a cosine ALONE
    cannot carry a row to a high number. Shipping a raw similarity as a percentage is a
    failure this repo has already had in prod (2026-09-10).
  * the evidence that blend leaves behind, which is the ONLY thing the authored line may
    stand on — one read, so the number and the sentence cannot disagree.
  * the wire: `affinity` + the fit block are on it, `distance_text` and the internal
    basis are not.
"""

import unittest
from unittest.mock import MagicMock, patch

from app.auth import AuthSession
from app.community_affinity import (
    _W_CONCEPTS,
    _W_SEMANTIC,
    _W_TYPE,
    attach_affinity,
    basis_for,
    score_row,
)
from app.community_fit_line import attach_fit_lines
from app.main import CommunityDiscoverBody, post_circles_discover

AUTH = "Bearer test-token"


def _auth() -> AuthSession:
    return AuthSession(
        user_id="u-caller", is_anonymous=False, phone_verified=True, home_block_id="block-a"
    )


def _scored(**over) -> dict:
    row = {
        "place_id": "p1",
        "member_count": 12,
        "shared_concept_count": 0,
        "shared_concept_labels": [],
        "shared_concept_subjects": [],
        "same_type": False,
        "matched_type": None,
        "semantic_similarity": None,
        "my_label": None,
        "member_label": None,
    }
    row.update(over)
    return row


def _rpc(rows):
    client = MagicMock()
    client.rpc.return_value = MagicMock(execute=MagicMock(return_value=MagicMock(data=rows)))
    return client


class TestTheBlend(unittest.TestCase):
    def test_nothing_in_common_is_zero_not_null(self) -> None:
        # 0.0 means "we looked and there is no overlap"; None means "we could not look".
        # A panel that cannot tell those apart cannot be debugged.
        self.assertEqual(score_row(_scored()), 0.0)

    def test_each_arm_is_worth_exactly_its_weight(self) -> None:
        self.assertEqual(
            score_row(_scored(shared_concept_count=3)), round(_W_CONCEPTS, 2)
        )
        self.assertEqual(
            score_row(_scored(semantic_similarity=0.99)), round(_W_SEMANTIC, 2)
        )
        self.assertEqual(score_row(_scored(same_type=True)), round(_W_TYPE, 2))

    def test_concepts_saturate_at_three(self) -> None:
        # Three proven overlaps is already PERFECT FIT on a peer card; a ninth says
        # nothing more, and an uncapped count would let one chatty member run away.
        self.assertEqual(
            score_row(_scored(shared_concept_count=3)),
            score_row(_scored(shared_concept_count=9)),
        )
        self.assertLess(
            score_row(_scored(shared_concept_count=1)),
            score_row(_scored(shared_concept_count=3)),
        )

    def test_a_cosine_alone_can_never_look_like_a_strong_fit(self) -> None:
        """prod 2026-09-10: one shared word scored 0.76 for every pair and three
        unrelated rows rendered "76%". A fuzzy-only row is capped at the semantic
        weight here, however high the cosine gets."""
        self.assertLessEqual(score_row(_scored(semantic_similarity=1.0)), 0.35)

    def test_noise_below_the_floor_scores_nothing(self) -> None:
        # 0.55 is the floor discover_communities_semantic already treats as noise.
        self.assertEqual(score_row(_scored(semantic_similarity=0.54)), 0.0)
        self.assertGreater(score_row(_scored(semantic_similarity=0.72)), 0.0)

    def test_all_three_arms_reach_the_top_of_the_scale(self) -> None:
        self.assertEqual(
            score_row(
                _scored(shared_concept_count=4, semantic_similarity=0.95, same_type=True)
            ),
            1.0,
        )

    def test_a_missing_or_junk_similarity_is_not_a_crash(self) -> None:
        for bad in (None, "", "n/a"):
            self.assertEqual(score_row(_scored(semantic_similarity=bad)), 0.0)


class TestTheEvidence(unittest.TestCase):
    def test_proven_overlap_wins_and_splits_by_subject(self) -> None:
        basis = basis_for(
            _scored(
                shared_concept_count=3,
                shared_concept_labels=["Karate", "Sourdough", "Trail running"],
                shared_concept_subjects=["child", "self", "self"],
                semantic_similarity=0.9,
                my_label="I bake",
                member_label="I cook",
            ),
            {"member_count": 12},
        )
        # subjects[i] describes labels[i]: the karate is the kids', the rest is theirs.
        self.assertEqual(basis["kids_shared"], ["Karate"])
        self.assertEqual(basis["shared"], ["Sourdough", "Trail running"])
        # The fuzzy pair is a FALLBACK — with proven labels it would only tempt the
        # composer to pad.
        self.assertNotIn("you_said", basis)
        self.assertEqual(basis["members"], 12)

    def test_the_fuzzy_pair_carries_a_row_with_no_shared_concept(self) -> None:
        basis = basis_for(
            _scored(semantic_similarity=0.8, my_label="Gymmer", member_label="Gym enthusiast"),
            {"member_count": 4},
        )
        self.assertEqual(basis["you_said"], "Gymmer")
        self.assertEqual(basis["members_say"], "Gym enthusiast")

    def test_same_kind_is_offered_only_when_it_is_all_there_is(self) -> None:
        thin = basis_for(_scored(same_type=True, matched_type="fitness"), {"member_count": 2})
        self.assertIn("you_already_have", thin)
        rich = basis_for(
            _scored(
                same_type=True,
                matched_type="fitness",
                shared_concept_count=1,
                shared_concept_labels=["Trail running"],
                shared_concept_subjects=["self"],
            ),
            {"member_count": 2},
        )
        self.assertNotIn("you_already_have", rich)

    def test_no_evidence_is_an_empty_basis_not_a_thin_one(self) -> None:
        # A half-pair ("she said X, nobody said anything") is not evidence.
        self.assertEqual(basis_for(_scored(my_label="Gymmer"), {"member_count": 3}), {})

    def test_the_place_itself_never_enters_the_evidence(self) -> None:
        basis = basis_for(
            _scored(shared_concept_count=1, shared_concept_labels=["Sourdough"],
                    shared_concept_subjects=["self"]),
            {"member_count": 9, "place_name": "OrangeTheory Narcoossee",
             "place_address": "9145 Narcoossee Rd", "zip": "32827"},
        )
        blob = repr(basis)
        for leaked in ("OrangeTheory", "Narcoossee", "32827"):
            self.assertNotIn(leaked, blob)


class TestAttachAffinity(unittest.TestCase):
    def _rows(self) -> list[dict]:
        return [{"place_id": "p1"}, {"place_id": "p2"}]

    @patch("app.community_affinity.service_client")
    def test_scores_and_stashes_evidence_per_place(self, sb) -> None:
        sb.return_value = _rpc(
            [
                _scored(place_id="p1", shared_concept_count=3,
                        shared_concept_labels=["A", "B", "C"],
                        shared_concept_subjects=["self"] * 3),
                _scored(place_id="p2"),
            ]
        )
        rows = self._rows()
        attach_affinity("u-caller", rows)
        self.assertEqual(rows[0]["affinity"], round(_W_CONCEPTS, 2))
        self.assertEqual(rows[1]["affinity"], 0.0)
        self.assertEqual(rows[0]["_fit_basis"]["shared"], ["A", "B", "C"])
        self.assertIsNone(rows[1]["_fit_basis"])

    @patch("app.community_affinity.service_client")
    def test_only_the_places_on_the_page_are_scored(self, sb) -> None:
        client = _rpc([])
        sb.return_value = client
        attach_affinity("u-caller", self._rows())
        self.assertEqual(client.rpc.call_args[0][1]["p_place_ids"], ["p1", "p2"])

    @patch("app.community_affinity.service_client")
    def test_a_failed_read_leaves_unscored_rows_not_zeroed_ones(self, sb) -> None:
        sb.side_effect = RuntimeError("no such function")
        rows = self._rows()
        attach_affinity("u-caller", rows)
        # null, NOT 0.0 — "we could not score this" must not read as "nothing in common".
        self.assertEqual([r["affinity"] for r in rows], [None, None])

    @patch("app.community_affinity.service_client")
    def test_a_place_the_rpc_did_not_answer_for_stays_unscored(self, sb) -> None:
        sb.return_value = _rpc([_scored(place_id="p1", same_type=True)])
        rows = self._rows()
        attach_affinity("u-caller", rows)
        self.assertEqual(rows[0]["affinity"], round(_W_TYPE, 2))
        self.assertIsNone(rows[1]["affinity"])


class TestTheAuthoredFitLine(unittest.TestCase):
    """Same contract as the fellows line: grounded, AI-authored, cached, silent on
    failure — and the evidence never reaches the wire."""

    def _rows(self) -> list[dict]:
        return [
            {"place_id": "p1", "_fit_basis": {"shared": ["Trail running"], "members": 12}},
            {"place_id": "p2", "_fit_basis": None},
        ]

    @patch("app.community_fit_line._store")
    @patch("app.community_fit_line._compose", return_value=[("They run your trails.", ["Trail running"])])
    @patch("app.community_fit_line._cached", return_value={})
    @patch("app.lang_pref.get_user_preferred_language", return_value="en")
    def test_authors_only_where_there_is_evidence(self, _lang, _cache, compose, _store) -> None:
        rows = self._rows()
        attach_fit_lines("u-caller", rows)
        self.assertEqual(rows[0]["fit_line"], "They run your trails.")
        self.assertEqual(rows[0]["fit_chips"], ["Trail running"])
        # A row with no overlap is not written about — it renders without the block.
        self.assertIsNone(rows[1]["fit_line"])
        self.assertEqual(rows[1]["fit_chips"], [])
        self.assertEqual(compose.call_args[0][0], [{"shared": ["Trail running"], "members": 12}])

    @patch("app.community_fit_line._store")
    @patch("app.community_fit_line._compose")
    @patch("app.community_fit_line._cached", return_value={})
    @patch("app.lang_pref.get_user_preferred_language", return_value="en")
    def test_the_evidence_never_reaches_the_wire(self, _lang, _cache, _compose, _store) -> None:
        rows = self._rows()
        attach_fit_lines("u-caller", rows)
        for row in rows:
            self.assertNotIn("_fit_basis", row)

    @patch("app.community_fit_line._store")
    @patch("app.community_fit_line._compose", return_value=[])
    @patch("app.community_fit_line._cached", return_value={})
    @patch("app.lang_pref.get_user_preferred_language", return_value="en")
    def test_a_failed_compose_is_a_card_without_the_block_never_canned_copy(
        self, _lang, _cache, _compose, store
    ) -> None:
        rows = self._rows()
        attach_fit_lines("u-caller", rows)
        self.assertIsNone(rows[0]["fit_line"])
        store.assert_not_called()

    @patch("app.community_fit_line._store")
    @patch("app.community_fit_line._compose")
    @patch("app.lang_pref.get_user_preferred_language", return_value="en")
    def test_a_stored_line_costs_no_llm_call(self, _lang, compose, _store) -> None:
        from app.peer_rec_line import _basis_sig

        basis = {"shared": ["Trail running"], "members": 12}
        cached = {("p1", _basis_sig(basis)): {"id": "r1", "line": "cached", "chips": ["Trail"]}}
        rows = self._rows()
        with patch("app.community_fit_line._cached", return_value=cached):
            attach_fit_lines("u-caller", rows)
        self.assertEqual(rows[0]["fit_line"], "cached")
        compose.assert_not_called()

    @patch("app.community_fit_line._store")
    @patch("app.community_fit_line._compose", return_value=[("fresh", ["Trail"])])
    @patch("app.lang_pref.get_user_preferred_language", return_value="en")
    def test_a_new_overlap_authors_a_new_line(self, _lang, compose, _store) -> None:
        from app.peer_rec_line import _basis_sig

        stale = {("p1", _basis_sig({"shared": ["Sourdough"]})): {"id": "r1", "line": "old", "chips": []}}
        rows = self._rows()
        with patch("app.community_fit_line._cached", return_value=stale):
            attach_fit_lines("u-caller", rows)
        self.assertEqual(rows[0]["fit_line"], "fresh")
        compose.assert_called_once()


class TestTheWire(unittest.TestCase):
    _ROW = {
        "place_id": "p1",
        "place_name": "OrangeTheory Narcoossee",
        "place_address": "9145 Narcoossee Rd",
        "place_type": "fitness",
        "relation": "gym",
        "emoji": "💪",
        "zip": "32827",
        "member_count": 34,
        "is_member": False,
        "status_line": "34 people",
    }

    def _call(self):
        def _score(user_id, rows):
            for row in rows:
                row["affinity"] = 0.65
                row["_fit_basis"] = {"shared": ["Trail running"]}

        def _author(user_id, rows):
            for row in rows:
                row.pop("_fit_basis", None)
                row["fit_line"] = "A few of them run your trails."
                row["fit_chips"] = ["Trail running"]

        with (
            patch("app.main.verify_auth", return_value=_auth()),
            patch("app.community_discovery.discover_communities", return_value=[dict(self._ROW)]),
            patch("app.community_affinity.attach_affinity", side_effect=_score),
            patch("app.community_fit_line.attach_fit_lines", side_effect=_author),
        ):
            return post_circles_discover(CommunityDiscoverBody(), authorization=AUTH)

    def test_affinity_and_the_fit_block_ride_out(self) -> None:
        row = self._call().communities[0]
        self.assertEqual(row.affinity, 0.65)
        self.assertEqual(row.fit_line, "A few of them run your trails.")
        self.assertEqual(row.fit_chips, ["Trail running"])

    def test_distance_text_is_gone_from_the_contract(self) -> None:
        payload = self._call().communities[0].model_dump()
        self.assertNotIn("distance_text", payload)
        self.assertNotIn("_fit_basis", payload)
        # Still no identities: dropping a field must not have opened another door.
        for leaked in ("nickname", "avatar_url", "peer_user_id", "members"):
            self.assertNotIn(leaked, payload)

    def test_an_unscored_row_is_null_not_zero_on_the_wire(self) -> None:
        with (
            patch("app.main.verify_auth", return_value=_auth()),
            patch("app.community_discovery.discover_communities", return_value=[dict(self._ROW)]),
            patch("app.community_affinity.attach_affinity"),
            patch("app.community_fit_line.attach_fit_lines"),
        ):
            row = post_circles_discover(CommunityDiscoverBody(), authorization=AUTH).communities[0]
        self.assertIsNone(row.affinity)
        self.assertIsNone(row.fit_line)
        self.assertEqual(row.fit_chips, [])


if __name__ == "__main__":
    unittest.main()
