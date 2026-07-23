"""C-FIND-MOM-RESULTS: ranked peer card enrichment + weak-match CTAs."""

from __future__ import annotations

from app.peer_discovery_surface import (
    build_discovery_surface,
    enrich_peer_match_row,
    stamp_peer_discovery_ctx,
)
from app.ui_actions import derive_ui_actions
from app.ui_intent import UI_INTENT_SHOW_PEER_PREVIEW


def test_enrich_peer_match_row_strong_fit() -> None:
    row = enrich_peer_match_row(
        {
            "peer_user_id": "u1",
            "nickname": "Kashaf",
            "similarity_score": 0.91,
            "matching_peer_label": "Mom of toddlers · Weekend hikes",
            "has_exact_concept_match": True,
        }
    )
    assert row["match_stars"] == 5
    assert row["match_band"] == "strong"
    # One proven shared claim (legacy exact row) is a FIT — never PERFECT.
    assert row["match_badge"] == "FIT"
    assert "Mom of toddlers" in row["trait_tags"]


def test_badge_counts_proven_shared_claims() -> None:
    def _badge(shared: list[str]) -> str:
        return enrich_peer_match_row(
            {
                "peer_user_id": "u1",
                "nickname": "Loka",
                "similarity_score": 1.0,
                "matching_peer_label": shared[0] if shared else "Enjoys hiking",
                "matching_my_label": "Enjoys hiking trips",
                "has_exact_concept_match": bool(shared),
                "shared_labels": shared,
            }
        )["match_badge"]

    assert _badge(["Soccer Fan", "Married 10 years", "Italian heritage"]) == "PERFECT FIT"
    assert _badge(["Soccer Fan", "Married 10 years"]) == "STRONG"
    assert _badge(["Soccer Fan"]) == "FIT"
    # Fuzzy-only at sim 1.0 still can't claim PERFECT — unproven overlap.
    assert _badge([]) == "FIT"


def test_stamp_peer_discovery_adds_actions_when_verified() -> None:
    ctx = {
        "peer_matches": [
            {
                "peer_user_id": "u1",
                "nickname": "Ada",
                "similarity_score": 0.72,
                "matching_peer_label": "Latina mom",
                "preview": False,
            },
            {
                "peer_user_id": "u2",
                "nickname": "Kashaf",
                "similarity_score": 0.88,
                "matching_peer_label": "American mom",
                "preview": False,
            },
        ]
    }
    stamp_peer_discovery_ctx(ctx, phone_verified=True)
    rows = ctx["peer_matches"]
    assert rows[0]["nickname"] == "Kashaf"
    assert rows[0]["actions"][0]["id"] == "peer_card_nudge"
    surface = ctx["discovery_surface"]
    assert surface is not None
    assert "strong" in surface["status_label"]
    assert surface["weak_peer"] is None


def test_weak_match_prompt_actions() -> None:
    ctx = {
        "peer_matches": [
            {
                "peer_user_id": "u1",
                "nickname": "Ada",
                "similarity_score": 0.90,
                "matching_peer_label": "Strong",
                "preview": False,
            },
            {
                "peer_user_id": "u2",
                "nickname": "Helena",
                "similarity_score": 0.55,
                "matching_peer_label": "Weak",
                "preview": False,
            },
        ]
    }
    stamp_peer_discovery_ctx(ctx, phone_verified=True)
    surface = build_discovery_surface(ctx["peer_matches"])
    assert surface is not None
    assert surface["weak_peer"]["nickname"] == "Helena"
    ctx["discovery_surface"] = surface
    actions = derive_ui_actions(ctx, UI_INTENT_SHOW_PEER_PREVIEW)
    assert len(actions) == 2
    assert actions[0]["id"] == "peer_wait_stronger"
    assert actions[1]["id"] == "peer_nudge_weak"


def test_preview_rows_skip_card_actions() -> None:
    ctx = {
        "peer_matches": [
            {
                "peer_user_id": None,
                "nickname": None,
                "matching_peer_label": "Mom of toddlers",
                "preview": True,
            }
        ]
    }
    stamp_peer_discovery_ctx(ctx, phone_verified=True)
    assert "actions" not in ctx["peer_matches"][0]


def test_unscored_row_gets_no_stars_badge_or_tags() -> None:
    row = enrich_peer_match_row(
        {
            "peer_user_id": "u9",
            "nickname": "AK",
            "similarity_score": None,
            "matching_peer_label": "On your block",
        }
    )
    assert row["match_stars"] is None
    assert row["match_band"] is None
    assert row["match_badge"] is None
    assert row["trait_tags"] == []


def test_exact_concept_no_longer_boosts_stars() -> None:
    row = enrich_peer_match_row(
        {
            "peer_user_id": "u1",
            "nickname": "Loka",
            "similarity_score": 0.76,
            "matching_peer_label": "Married 10 years",
            "has_exact_concept_match": True,
        }
    )
    assert row["match_stars"] == 3
    assert row["match_band"] == "partial"
    assert row["match_badge"] == "FIT"  # one proven shared claim


def test_truthful_label_exact_pair() -> None:
    row = enrich_peer_match_row(
        {
            "peer_user_id": "u1",
            "nickname": "Jess",
            "similarity_score": 0.95,
            "matching_peer_label": "Loves playgrounds",
            "matching_my_label": "Loves playgrounds",
            "has_exact_concept_match": True,
        }
    )
    assert row["matching_peer_label"] == "You both: Loves playgrounds"
    assert row["trait_tags"] == ["Loves playgrounds"]


def test_truthful_label_lists_all_shared_claims() -> None:
    row = enrich_peer_match_row(
        {
            "peer_user_id": "u1",
            "nickname": "Loka",
            "similarity_score": 0.97,
            "matching_peer_label": "Soccer Fan",
            "matching_my_label": "Soccer Fan",
            "has_exact_concept_match": True,
            "shared_labels": ["Soccer Fan", "Enjoys outdoor activities", "Mom"],
        }
    )
    assert row["matching_peer_label"] == "You both: Soccer Fan · Enjoys outdoor activities · Mom"
    assert row["trait_tags"] == ["Soccer Fan", "Enjoys outdoor activities", "Mom"]


def test_truthful_label_fuzzy_pair_shows_both_sides() -> None:
    row = enrich_peer_match_row(
        {
            "peer_user_id": "u1",
            "nickname": "Daniel",
            "similarity_score": 0.77,
            "matching_peer_label": "Enjoys sports",
            "matching_my_label": "Enjoys Swimming",
            "has_exact_concept_match": False,
        }
    )
    assert row["matching_peer_label"] == "You: Enjoys Swimming · Them: Enjoys sports"
    assert row["trait_tags"] == ["Enjoys Swimming", "Enjoys sports"]


def test_surface_counts_unscored_as_block_neighbors() -> None:
    ctx = {
        "peer_matches": [
            {
                "peer_user_id": "u1",
                "nickname": "Ada",
                "similarity_score": 0.88,
                "matching_peer_label": "Latina mom",
                "preview": False,
            },
            {
                "peer_user_id": "u2",
                "nickname": "AK",
                "similarity_score": None,
                "matching_peer_label": "On your block",
                "preview": False,
            },
        ]
    }
    stamp_peer_discovery_ctx(ctx, phone_verified=True)
    surface = ctx["discovery_surface"]
    assert surface["strong_count"] == 1
    assert surface["weak_count"] == 0
    assert "near you" in surface["status_label"]
    assert "AK" not in surface["ranked_summary"]
    assert "?" not in surface["ranked_summary"]
    assert surface["weak_peer"] is None


def test_surface_all_unscored_has_no_ranked_summary() -> None:
    surface = build_discovery_surface(
        [
            enrich_peer_match_row(
                {
                    "peer_user_id": "u1",
                    "nickname": "AK",
                    "similarity_score": None,
                    "matching_peer_label": "On your block",
                }
            )
        ]
    )
    assert surface is not None
    assert surface["ranked_summary"] is None
    assert "near you" in surface["status_label"]
