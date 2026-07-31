"""Rapport gap tree — the fixed catalogue of follow-up questions ("gaps") Lana may open.

A gap is unlocked when the user reveals a claim in its trigger bucket (the facet parser
that already runs every turn writes those claims — see app/claims_persist.py). Opening a
gap does NOT phrase a question to the user; it just queues a topic. The ranker
(app/rapport_ranker.py) later picks one open gap for the home-screen "By the way…" tile.

Contract alignment (this is the whole point of the reconciliation):
  * `covers_concept` is a single lowercase token matching the user_identity_claims
    concept CHECK `^[a-z][a-z0-9_]{1,63}$`. When a claim with that concept lands, the gap
    is considered answered and closes. It also drives suppression — never open a gap for
    something already known.
  * `parent_bucket` is one of the real claim buckets:
    heritage | stage | vicinity | faith | activity | interest | general.
  * `sensitivity_tier` gates the ask by relationship tier (see rapport_ranker):
    LOW → anyone, MED → anyone (soft), HIGH → tier >= acquaintance.
  * `why_frame_template` renders the tile copy from the triggering claim's label, e.g.
    "about your {label}…" → "about your Morning Run…". `{label}` is optional.
"""

from __future__ import annotations

import re
from typing import Any

# Buckets the extractor actually emits (mirrors the user_identity_claims bucket CHECK).
CLAIM_BUCKETS = frozenset(
    {"heritage", "stage", "vicinity", "faith", "activity", "interest", "general"}
)

_CONCEPT_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")

# gap_id → definition. gap_id is free-form (slug); covers_concept must satisfy _CONCEPT_RE.
#   why_frame_template → the tile teaser (may interpolate the triggering claim's {label}).
#   question           → the actual question shown under the teaser (static, clear sentence).
#   requires_any_keyword (optional) → the gap only opens if one of the user's active claim
#     labels/concepts contains one of these tokens (so, e.g., we don't ask about kids unless
#     she's actually mentioned kids). Absent = open on the parent_bucket alone.
GAP_TREE: dict[str, dict[str, Any]] = {
    # ── heritage ────────────────────────────────────────────────────────────
    "heritage_practice_with_kids": {
        "parent_bucket": "heritage",
        "covers_concept": "heritage_practice",
        "why_frame_template": "about your family's traditions…",
        "question": "Do you keep any of your family's traditions alive with your kids?",
        "sensitivity_tier": "LOW",
        "unlock_score": 0.70,
    },
    "language_home": {
        "parent_bucket": "heritage",
        "covers_concept": "home_language",
        "why_frame_template": "about the language you speak at home…",
        "question": "What language do you speak at home?",
        "sensitivity_tier": "LOW",
        "unlock_score": 0.60,
    },
    # ── vicinity (moved here / neighborhood) ─────────────────────────────────
    "relocation_recency": {
        "parent_bucket": "vicinity",
        "covers_concept": "relocation_recency",
        "why_frame_template": "about settling into the neighborhood…",
        "question": "How long have you been in the neighborhood?",
        "sensitivity_tier": "LOW",
        "unlock_score": 0.60,
    },
    # ── faith ────────────────────────────────────────────────────────────────
    "faith_community_ties": {
        "parent_bucket": "faith",
        "covers_concept": "faith_community",
        "why_frame_template": "about your faith community…",
        "question": "Are you part of a faith community nearby?",
        "sensitivity_tier": "MED",
        "unlock_score": 0.55,
    },
    # ── stage (mom role / work / kids stage) ─────────────────────────────────
    "kids_ages": {
        "parent_bucket": "stage",
        "covers_concept": "kids_ages",
        "why_frame_template": "about your little ones…",
        "question": "How old are your little ones?",
        # Only open when she's actually mentioned kids — not off a bare "married" claim.
        "requires_any_keyword": [
            "kid", "kids", "child", "children", "son", "daughter",
            "baby", "babies", "toddler", "little one", "newborn",
        ],
        "sensitivity_tier": "LOW",
        "unlock_score": 0.70,
    },
    "daily_rhythm": {
        "parent_bucket": "stage",
        "covers_concept": "daily_rhythm",
        "why_frame_template": "about how your days are shaped…",
        "question": "What do your days usually look like?",
        "sensitivity_tier": "LOW",
        "unlock_score": 0.55,
    },
    # ── activity (things she does) ───────────────────────────────────────────
    "activity_social_pref": {
        "parent_bucket": "activity",
        "covers_concept": "activity_social_pref",
        "why_frame_template": "about your {label}…",
        "question": "Do you usually do that solo, or with other parents?",
        "sensitivity_tier": "LOW",
        "unlock_score": 0.65,
    },
    "activity_frequency": {
        "parent_bucket": "activity",
        "covers_concept": "activity_frequency",
        "why_frame_template": "about how often you get to your {label}…",
        "question": "How often do you get to it these days?",
        "sensitivity_tier": "LOW",
        "unlock_score": 0.55,
    },
    # ── interest (things she enjoys) ─────────────────────────────────────────
    "social_food_events": {
        "parent_bucket": "interest",
        "covers_concept": "social_food_events",
        "why_frame_template": "about the {label} you enjoy…",
        "question": "Are you into meetups or food get-togethers with other parents?",
        "sensitivity_tier": "LOW",
        "unlock_score": 0.60,
    },
    "free_windows": {
        "parent_bucket": "interest",
        "covers_concept": "free_windows",
        "why_frame_template": "about when you get a free moment…",
        "question": "When do you usually get a free moment?",
        "sensitivity_tier": "LOW",
        "unlock_score": 0.50,
    },
    # ── HIGH-sensitivity (defined, but gated to tier >= acquaintance) ────────
    "support_need": {
        "parent_bucket": "general",
        "covers_concept": "support_need",
        "why_frame_template": "about how you're doing lately…",
        "question": "Is there anything you could use a hand with lately?",
        "sensitivity_tier": "HIGH",
        "unlock_score": 0.50,
    },
    "budget_sensitivity": {
        "parent_bucket": "interest",
        "covers_concept": "budget_sensitivity",
        "why_frame_template": "about what fits your budget…",
        "question": "Do you tend to look for budget-friendly options?",
        "sensitivity_tier": "HIGH",
        "unlock_score": 0.40,
    },
}


def get_gap(gap_id: str) -> dict[str, Any] | None:
    """Return the gap definition for `gap_id`, or None if it isn't in the tree."""
    return GAP_TREE.get(gap_id)


def gaps_for_bucket(bucket: str) -> list[tuple[str, dict[str, Any]]]:
    """All (gap_id, gap) pairs unlocked by a claim in `bucket`."""
    return [(gid, g) for gid, g in GAP_TREE.items() if g["parent_bucket"] == bucket]


def render_why_frame(gap: dict[str, Any], trigger_label: str | None) -> str:
    """Fill the `{label}` slot from the triggering claim's label; fall back cleanly.

    "about your {label}…" + "Morning Run" → "about your morning run…". If the template
    has no slot (or we have no label), return it verbatim / with the slot dropped.
    """
    template = str(gap.get("why_frame_template") or "").strip()
    if "{label}" not in template:
        return template
    label = str(trigger_label or "").strip().lower()
    if label:
        return template.replace("{label}", label)
    # No label to interpolate → drop the possessive slot gracefully.
    return template.replace("your {label}", "that").replace("the {label}", "that").replace("{label}", "that")


def concept_is_valid(concept: str) -> bool:
    """True if `concept` satisfies the user_identity_claims concept-format CHECK."""
    return bool(_CONCEPT_RE.match(concept or ""))
