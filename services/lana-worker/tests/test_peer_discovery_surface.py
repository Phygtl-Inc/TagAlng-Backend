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
    assert row["match_badge"] == "PERFECT FIT"
    assert "Mom of toddlers" in row["trait_tags"]


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
