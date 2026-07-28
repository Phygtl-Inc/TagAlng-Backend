"""Onion → find-peers blend (Circles §C consumption) — app/onion_blend.py.

No test here touches a database. score_onion_candidates and service_client are
patched at app.onion_blend.* (module-level imports). The rules that matter:

  * Fail-open: disabled flag, missing user, gated area, empty candidates, or a
    raising scorer all return the vector list UNCHANGED.
  * A new seat is earned by a proven same-place overlap only — concept-only or
    type-only onion candidates never join the list (the vector matcher is
    block-scoped on purpose; reaching beyond the block needs the shared place).
  * Disclosure: an appended row carries the caller-relative tag ("your gym"),
    never a place name — and no invented cosine (similarity_score stays None).
"""

import unittest
from unittest.mock import MagicMock, patch

from app.onion_blend import blend_onion_matches
from app.peer_discovery_surface import enrich_peer_match_row

_PLACE = "11111111-1111-1111-1111-111111111111"

_VECTOR_PEER = {
    "peer_user_id": "v1",
    "nickname": "Fish",
    "avatar_url": None,
    "similarity_score": 0.91,
    "matching_peer_label": "Runner",
    "matching_peer_concept": "runner",
    "has_exact_concept_match": True,
    "shared_labels": ["Runner"],
}

_CAND_SAME_PLACE_NEW = {
    "user_id": "p1",
    "nickname": "Daniel",
    "avatar_url": None,
    "score": 5,
    "same_place_bonus": 3,
    "same_type_bonus": 0,
    "shared_concept_count": 2,
    "shared_concept_labels": ["Gym enthusiast", "Triathlon"],
    "shared_place_ref": _PLACE,
}

_CAND_CONCEPTS_ONLY = {
    "user_id": "p2",
    "nickname": "Sam",
    "avatar_url": None,
    "score": 4,
    "same_place_bonus": 0,
    "same_type_bonus": 0,
    "shared_concept_count": 4,
    "shared_concept_labels": ["Reader", "Cyclist", "Baker", "Gardener"],
    "shared_place_ref": None,
}

_CAND_SAME_PLACE_KNOWN = {
    "user_id": "v1",
    "nickname": "Fish",
    "avatar_url": None,
    "score": 4,
    "same_place_bonus": 3,
    "same_type_bonus": 0,
    "shared_concept_count": 1,
    "shared_concept_labels": ["Runner"],
    "shared_place_ref": _PLACE,
}


def _scorer(candidates, *, gated=False):
    return MagicMock(
        return_value={
            "gated": gated,
            "candidates": candidates,
            "gate": None,
            "gate_facts": [],
        }
    )


def _sb_with_affiliations(rows):
    sb = MagicMock()
    (
        sb.return_value.table.return_value.select.return_value.eq.return_value.eq.return_value.is_.return_value.not_.is_.return_value.limit.return_value.execute.return_value
    ) = MagicMock(data=rows)
    return sb


_GYM_AFFILIATION = [{"place_ref": _PLACE, "circle_type": "fitness"}]


class TestBlendFailOpen(unittest.TestCase):
    def test_no_user_id_returns_peers_unchanged(self) -> None:
        peers = [dict(_VECTOR_PEER)]
        with patch("app.onion_blend.score_onion_candidates") as scorer:
            self.assertEqual(blend_onion_matches(peers, user_id=None), peers)
            scorer.assert_not_called()

    @patch.dict("os.environ", {"LANA_ONION_MATCHER": "off"})
    def test_disabled_flag_never_scores(self) -> None:
        peers = [dict(_VECTOR_PEER)]
        with patch("app.onion_blend.score_onion_candidates") as scorer:
            self.assertEqual(blend_onion_matches(peers, user_id="u1"), peers)
            scorer.assert_not_called()

    def test_gated_area_blends_nothing(self) -> None:
        peers = [dict(_VECTOR_PEER)]
        with patch(
            "app.onion_blend.score_onion_candidates",
            _scorer([dict(_CAND_SAME_PLACE_NEW)], gated=True),
        ):
            self.assertEqual(blend_onion_matches(peers, user_id="u1"), peers)

    def test_empty_candidates_unchanged(self) -> None:
        peers = [dict(_VECTOR_PEER)]
        with patch("app.onion_blend.score_onion_candidates", _scorer([])):
            self.assertEqual(blend_onion_matches(peers, user_id="u1"), peers)

    def test_scorer_raising_unchanged(self) -> None:
        peers = [dict(_VECTOR_PEER)]
        with patch(
            "app.onion_blend.score_onion_candidates",
            MagicMock(side_effect=RuntimeError("boom")),
        ):
            self.assertEqual(blend_onion_matches(peers, user_id="u1"), peers)


class TestBlendMergeAndAppend(unittest.TestCase):
    def test_same_place_candidate_joins_and_outranks(self) -> None:
        peers = [dict(_VECTOR_PEER)]
        with (
            patch(
                "app.onion_blend.score_onion_candidates",
                _scorer([dict(_CAND_SAME_PLACE_NEW)]),
            ),
            patch("app.onion_blend.service_client", _sb_with_affiliations(_GYM_AFFILIATION)),
        ):
            out = blend_onion_matches(peers, user_id="u1", limit=5)
        self.assertEqual(len(out), 2)
        top = out[0]
        # Proven place + 2 exact concepts (3 shared things) outranks 1 shared claim.
        self.assertEqual(top["peer_user_id"], "p1")
        self.assertEqual(top["shared_labels"], ["your gym", "Gym enthusiast", "Triathlon"])
        self.assertEqual(top["matching_peer_label"], "your gym")
        self.assertIsNone(top["similarity_score"])  # no invented cosine, ever
        self.assertTrue(top["has_exact_concept_match"])
        self.assertTrue(top["onion_match"])
        self.assertEqual(top["same_place_bonus"], 3)
        # The place is referenced by uuid only — never a name/address key.
        self.assertEqual(top["shared_place_ref"], _PLACE)
        self.assertNotIn("place_name", top)

    def test_concept_only_candidate_never_earns_a_seat(self) -> None:
        peers = [dict(_VECTOR_PEER)]
        with (
            patch(
                "app.onion_blend.score_onion_candidates",
                _scorer([dict(_CAND_CONCEPTS_ONLY)]),
            ),
            patch("app.onion_blend.service_client", _sb_with_affiliations(_GYM_AFFILIATION)),
        ):
            out = blend_onion_matches(peers, user_id="u1", limit=5)
        self.assertEqual([r["peer_user_id"] for r in out], ["v1"])

    def test_same_place_without_caller_tag_is_skipped(self) -> None:
        peers = [dict(_VECTOR_PEER)]
        with (
            patch(
                "app.onion_blend.score_onion_candidates",
                _scorer([dict(_CAND_SAME_PLACE_NEW)]),
            ),
            patch("app.onion_blend.service_client", _sb_with_affiliations([])),
        ):
            out = blend_onion_matches(peers, user_id="u1", limit=5)
        self.assertEqual([r["peer_user_id"] for r in out], ["v1"])

    def test_existing_vector_row_gets_proof_attached(self) -> None:
        peers = [dict(_VECTOR_PEER)]
        with (
            patch(
                "app.onion_blend.score_onion_candidates",
                _scorer([dict(_CAND_SAME_PLACE_KNOWN)]),
            ),
            patch("app.onion_blend.service_client", _sb_with_affiliations(_GYM_AFFILIATION)),
        ):
            out = blend_onion_matches(peers, user_id="u1", limit=5)
        self.assertEqual(len(out), 1)
        row = out[0]
        self.assertEqual(row["peer_user_id"], "v1")
        # Place tag reads first; the already-shown label is kept, deduped.
        self.assertEqual(row["shared_labels"], ["your gym", "Runner"])
        self.assertEqual(row["similarity_score"], 0.91)  # cosine untouched
        self.assertTrue(row["onion_match"])
        # The input list object the caller holds is not mutated.
        self.assertNotIn("onion_match", peers[0])

    def test_limit_caps_and_keeps_strongest_proof(self) -> None:
        weak_vector = dict(_VECTOR_PEER, peer_user_id="v2", shared_labels=[], similarity_score=0.72)
        peers = [dict(_VECTOR_PEER), weak_vector]
        with (
            patch(
                "app.onion_blend.score_onion_candidates",
                _scorer([dict(_CAND_SAME_PLACE_NEW)]),
            ),
            patch("app.onion_blend.service_client", _sb_with_affiliations(_GYM_AFFILIATION)),
        ):
            out = blend_onion_matches(peers, user_id="u1", limit=2)
        self.assertEqual([r["peer_user_id"] for r in out], ["p1", "v1"])


class TestEnrichOnionRow(unittest.TestCase):
    def test_onion_row_badges_from_proof_without_cosine(self) -> None:
        row = enrich_peer_match_row(
            {
                "peer_user_id": "p1",
                "nickname": "Daniel",
                "similarity_score": None,
                "onion_match": True,
                "matching_peer_label": "your gym",
                "shared_labels": ["your gym", "Gym enthusiast", "Triathlon"],
                "has_exact_concept_match": True,
            }
        )
        self.assertIsNone(row["match_stars"])  # never an invented similarity
        self.assertIsNone(row["match_band"])
        self.assertEqual(row["match_badge"], "PERFECT FIT")  # 3 proven shared things
        self.assertEqual(row["shared_count"], 3)
        self.assertEqual(
            row["matching_peer_label"], "You both: your gym · Gym enthusiast · Triathlon"
        )

    def test_plain_preview_row_still_unscored(self) -> None:
        row = enrich_peer_match_row(
            {"peer_user_id": "n1", "nickname": "Ada", "similarity_score": None}
        )
        self.assertIsNone(row["match_badge"])
        self.assertEqual(row["trait_tags"], [])


if __name__ == "__main__":
    unittest.main()
