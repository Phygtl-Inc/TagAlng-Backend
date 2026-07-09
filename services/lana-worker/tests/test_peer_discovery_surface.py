"""C-FIND-MOM-RESULTS: ranked peer card enrichment + weak-match CTAs."""

from __future__ import annotations

import re

from app.peer_discovery_surface import (
    build_discovery_surface,
    enrich_peer_match_row,
    enrich_peer_match_rows,
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
    assert "Mom of Toddlers" in row["trait_tags"]


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


# ── decision context (QA 2026-07-08: "PERFECT FIT" card was uninformative) ──


def test_tier_great_requires_two_shared_dimensions() -> None:
    # One shared trait — even at a sky-high score — can only be "good".
    one = enrich_peer_match_row(
        {
            "peer_user_id": "u1",
            "nickname": "David",
            "similarity_score": 0.93,
            "matching_peer_label": "Enjoys playgrounds",
        }
    )
    assert one["tier"] == "good"
    assert one["shared"] == ["Enjoys Playgrounds"]

    two = enrich_peer_match_row(
        {
            "peer_user_id": "u2",
            "nickname": "Kashaf",
            "similarity_score": 0.82,
            "matching_peer_label": "Mom of toddlers · Park mornings",
        }
    )
    assert two["tier"] == "great"
    assert two["shared"] == ["Mom of Toddlers", "Park Mornings"]


def test_tier_never_great_on_weak_band() -> None:
    row = enrich_peer_match_row(
        {
            "peer_user_id": "u1",
            "similarity_score": 0.56,
            "matching_peer_label": "Swims regularly · Hosts events",
        }
    )
    assert row["match_band"] == "weak"
    assert row["tier"] == "good"


def test_payload_has_stage_band_shared_distance_and_no_raw_age() -> None:
    row = enrich_peer_match_row(
        {
            "peer_user_id": "u1",
            "nickname": "Sofia",
            "similarity_score": 0.84,
            "matching_peer_label": "Mom of toddlers · Kids aged 3 · Park mornings",
        }
    )
    assert row["stage_band"] == "toddler"
    assert row["distance_label"] == "On your block"
    assert row["display_score"] is False
    # Reasons are Title Case and an age-looking fragment never reaches the card.
    assert row["shared"] == ["Mom of Toddlers", "Park Mornings"]
    for reason in row["shared"] + row["trait_tags"]:
        assert not re.search(r"\baged?\s*\d", reason, re.I)
    assert "stage_band" in row and row["stage_band"] in (
        "expecting",
        "baby",
        "toddler",
        "prek",
        "school",
    )


def test_stage_band_variants() -> None:
    cases = {
        "Expecting her first": "expecting",
        "New baby at home": "baby",
        "Mom of toddlers": "toddler",
        "Pre-K pickup crew": "prek",
        "Kindergarten mornings": "school",
        "Weekend hikes": None,
    }
    for label, band in cases.items():
        row = enrich_peer_match_row(
            {"peer_user_id": "u", "similarity_score": 0.8, "matching_peer_label": label}
        )
        assert row["stage_band"] == band, label


# ── moms-first ranking flag ──────────────────────────────────────────────────
#
# NOTE(production data): gender is deliberately never stored (no users.gender
# column; the claim extractor is forbidden from capturing sex/gender), so
# match_peers_by_claim_vectors rows carry no "gender" key and the damping is a
# no-op in production today. These tests exercise the mechanism with synthetic
# rows so it activates the moment the peer RPCs expose a self-declared field.


def test_moms_first_damping_when_flag_on(monkeypatch) -> None:
    monkeypatch.delenv("LANA_MOMS_FIRST_RANKING", raising=False)  # default ON
    rows = [
        {"peer_user_id": "m1", "nickname": "David", "similarity_score": 0.93, "gender": "male"},
        {"peer_user_id": "f1", "nickname": "Kashaf", "similarity_score": 0.82, "gender": "female"},
    ]
    ranked = enrich_peer_match_rows(rows, phone_verified=True, ask_text="2-3 moms nearby for park mornings")
    assert [r["peer_user_id"] for r in ranked] == ["f1", "m1"]
    # Damped in ranking only — telemetry score is untouched.
    assert ranked[1]["similarity_score"] == 0.93


def test_moms_first_flag_off_restores_score_order(monkeypatch) -> None:
    monkeypatch.setenv("LANA_MOMS_FIRST_RANKING", "0")
    rows = [
        {"peer_user_id": "m1", "similarity_score": 0.93, "gender": "male"},
        {"peer_user_id": "f1", "similarity_score": 0.82, "gender": "female"},
    ]
    ranked = enrich_peer_match_rows(rows, phone_verified=True, ask_text="moms nearby")
    assert [r["peer_user_id"] for r in ranked] == ["m1", "f1"]


def test_moms_first_bypassed_for_explicit_family_ask(monkeypatch) -> None:
    monkeypatch.delenv("LANA_MOMS_FIRST_RANKING", raising=False)
    rows = [
        {"peer_user_id": "m1", "similarity_score": 0.93, "gender": "male"},
        {"peer_user_id": "f1", "similarity_score": 0.82, "gender": "female"},
    ]
    ranked = enrich_peer_match_rows(
        rows, phone_verified=True, ask_text="families with dads welcome for weekend picnics"
    )
    assert [r["peer_user_id"] for r in ranked] == ["m1", "f1"]


def test_moms_first_noop_without_gender_data(monkeypatch) -> None:
    # Production reality: rows never carry gender → ordering is unchanged.
    monkeypatch.delenv("LANA_MOMS_FIRST_RANKING", raising=False)
    rows = [
        {"peer_user_id": "a", "similarity_score": 0.93},
        {"peer_user_id": "b", "similarity_score": 0.82},
    ]
    ranked = enrich_peer_match_rows(rows, phone_verified=True, ask_text="moms nearby")
    assert [r["peer_user_id"] for r in ranked] == ["a", "b"]
