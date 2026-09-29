"""The reader's own claims, as a recommendation page may use them to ORDER and explain.

docs/superpowers/specs/2026-09-29-claims-rank-for-you-design.md. Claims RANK results and
back a "For you" line; they never filter. Which claims, and what may be SAID of them:

  * Own and household only (subject_kind self or a family member — "my kids"), never
    dismissed or transient. Private claims are never used: a page is a surface, and the
    reader chose not to have that one shown.
  * `sayable` — whether the line may NAME it ("You've mentioned you eat out with your
    kids"). Everyday threads may. Faith and heritage (by bucket) are QUIET: they may still
    reorder, but the line speaks about the place ("Reviewers note it's halal"), never the
    person. The composer is also told never to name health or anything intimate — a
    bucket cannot catch those, so `sayable` is only the floor, and the wire carries a
    claim's label ONLY when it is sayable.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

MAX_CLAIMS = 12
QUIET_BUCKETS = frozenset({"heritage", "faith"})


def load_reader_claims(user_id: str | None) -> list[dict[str, Any]]:
    """[{id, label, bucket, sayable, about}] for the reader. [] on any failure — a page
    without them is exactly today's page."""
    if not user_id:
        return []
    try:
        from app.auth import service_client

        rows = (
            service_client()
            .table("user_identity_claims")
            .select("id, label, bucket, disclosure, subject_kind, transient, created_at")
            .eq("user_id", user_id)
            .is_("dismissed_at", "null")
            .order("created_at", desc=True)
            .limit(60)
            .execute()
        ).data or []
    except Exception:  # noqa: BLE001
        logger.warning("reader_claims_load_failed user=%s", user_id, exc_info=True)
        return []
    return shape_claims(rows)


def shape_claims(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in rows:
        label = " ".join(str(r.get("label") or "").split())
        if not label or label.casefold() in seen:
            continue
        # Absent disclosure is treated as private — the reco_cohort rule.
        if str(r.get("disclosure") or "") in ("", "private") or r.get("transient"):
            continue
        bucket = str(r.get("bucket") or "").strip().lower() or "general"
        seen.add(label.casefold())
        out.append({
            "id": f"c{len(out) + 1}",
            "label": label[:80],
            "bucket": bucket,
            "sayable": bucket not in QUIET_BUCKETS,
            # "self" or the household member it is about ("child") — the composer phrases
            # "your kids" rather than attributing a child's trait to the reader.
            "about": str(r.get("subject_kind") or "self"),
        })
        if len(out) >= MAX_CLAIMS:
            break
    return out


COMPOSER_RULES = """READER CLAIMS ("reader_claims") are what the reader has told Lana about
THEMSELVES or their household. Use them to find a "for_you" match: a claim the place (or
recommendation) genuinely serves, PROVEN by a quote that is about that thing.
- "for_you": 0-1 item per card — the reader's strongest fit — {"claim": "<claim id>",
  "line": "…", "quotes": [<exactly one quote>]}.
  NO item without a supporting quote. Never stretch: a steakhouse does not serve a
  vegetarian claim because it "has a salad".
- If the claim's "sayable" is true, the line may name it, spoken to the reader: "You've
  mentioned you eat out with your kids — reviewers say it's great for families."
- If "sayable" is false, or the claim is about health, religion, ethnicity, heritage,
  sexuality or anything intimate, the line NEVER mentions the reader or the claim — only
  the place: "Reviewers note everything is halal." Never "because you are …".
- A claim "about" a household member ("child") is theirs, not the reader's: "your kids".
- The quote must itself show the place is GOOD for that exact thing: "so patient with our
  toddlers" serves "has kids"; "the kids were hungry and agitated" does not, and
  "gluten-free chips" does not serve "vegetarian". Negative, neutral or merely nearby
  quotes never count — leave for_you empty rather than stretch. A diet claim needs food
  that IS that diet (vegetarian = no meat or fish); "dairy-free", "gluten-free", "healthy"
  or "has salads" serve none of them.
- The line is in the "language" given, one sentence, no counts of people.
"""

_JUDGE = """You check "For you" evidence on recommendation cards. Each item pairs something a
reader told us about themselves with ONE quote from a review or a neighbour.

For each item answer "supports": true ONLY if the quote, on its own, explicitly shows the
place is GOOD for that exact claim — positive, and about that very thing. Examples:
- claim "Has young kids" + "so patient with our two toddlers" → true
- claim "Has young kids" + "the kids were hungry and agitated" → false (negative)
- claim "Has young kids" + "my son was not a fan of the kids burrito" → false (negative)
- claim "Vegetarian" + "the chips are gluten-free" → false (a different diet)
- claim "Vegetarian" + "rich stuffed French toast" → false (not about being vegetarian)
- claim "Vegetarian" + "lots of veggie options, the falafel is amazing" → true
- claim "Vegetarian" + "the gluten free yuca waffle BLT was dairy free" → false (a BLT is
  bacon; dairy-free and gluten-free are other diets)
A diet claim is served only by food that IS that diet: vegetarian = no meat or fish, vegan
= no animal products, halal / kosher = said to be so. "Healthy", "light", "dairy-free",
"gluten-free" or "has salads" serve none of them. A quote naming a meat dish never serves
vegetarian or vegan.
When unsure, false.

Output ONLY JSON: {"items": [{"key": "<key>", "supports": true|false}]}
"""


def judge_for_you(pairs: list[dict[str, str]]) -> set[str]:
    """The keys of `pairs` ({key, claim, quote}) whose quote genuinely supports the claim.

    A second, independent read — the writer proposes, this confirms. Verbatim checks prove
    a quote EXISTS; only this proves it is ON POINT (prod QA 2026-09-29: real quotes matched
    "kids" to "the kids were hungry and agitated" and "vegetarian" to "gluten-free chips").
    Fails CLOSED: any error returns set(), so no "For you" line ships unconfirmed."""
    if not pairs:
        return set()
    try:
        import json

        from app.orchestrator.llm import llm_configured, llm_json, router_model

        if not llm_configured():
            return set()
        data = llm_json(
            model=router_model(),
            system=_JUDGE,
            user_payload=json.dumps({"items": pairs}, ensure_ascii=False),
            max_tokens=40 + 24 * len(pairs),
            temperature=0.0,
        )
    except Exception:  # noqa: BLE001
        logger.warning("reader_claims.judge_failed", exc_info=True)
        return set()
    items = data.get("items") if isinstance(data, dict) else None
    return {
        str(i.get("key")) for i in items if isinstance(i, dict) and i.get("supports") is True
    } if isinstance(items, list) else set()


def keep_judged(items: list[dict[str, Any]], claims: list[dict[str, Any]]) -> None:
    """In place: drop from each item's `for_you` whatever the judge does not confirm, and
    within a confirmed one keep only confirmed quotes. `items` are dicts carrying
    `for_you: [{claim_id, quotes: [{text, …}]}]`. One judge call for the whole page."""
    labels = {c["id"]: c["label"] for c in claims or []}
    pairs: list[dict[str, str]] = []
    for i, it in enumerate(items):
        for j, f in enumerate(it.get("for_you") or []):
            for k, q in enumerate(f.get("quotes") or []):
                pairs.append({"key": f"{i}.{j}.{k}", "claim": labels.get(f.get("claim_id"), ""),
                              "quote": str(q.get("text") or "")})
    ok = judge_for_you(pairs)
    for i, it in enumerate(items):
        kept = []
        for j, f in enumerate(it.get("for_you") or []):
            quotes = [q for k, q in enumerate(f.get("quotes") or []) if f"{i}.{j}.{k}" in ok]
            if quotes:
                kept.append({**f, "quotes": quotes})
        it["for_you"] = kept

