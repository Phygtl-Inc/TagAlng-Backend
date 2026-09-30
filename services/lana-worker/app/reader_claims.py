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


_EXPAND = """Before any recommendation card is written, you decide what each thing the
reader has told us about themselves would make them WANT from the kind of place they just
asked for. The cards then look for proof of exactly that in the reviews.

For each claim return "look_for": ONE specific thing a review, or the place's opening
hours, could show — or null. And "because": "" when the link is obvious (kids →
kid-friendly, dog → dogs welcome, guitar → live music, gifts → a gift shop — almost
always); only when it is NOT, a short "so … matters" clause that reads right after the
claim ("You're training for an Ironman, so fuel matters").

- Read the claim for what it MEANS. "Usually late" = often not on time → a place that
  holds a table or seats walk-ins without a fuss — NEVER anything about opening hours:
  a place open late does not help someone who is late (they are late to a time they set). "Night owl" → open
  late. "Plays guitar" → live music. "Has a dog" → dogs welcome, e.g. on the patio.
  "Has young kids" → kid-friendly (patient staff, kids menu, room to move). "Vegetarian" →
  real vegetarian dishes. "Training for Ironman" / "Does triathlons" at a restaurant →
  dishes a review CALLS high-protein, lean or healthy (protein bowls, macros on the menu),
  "because": "so fuel matters" — never big portions or a good steak, which suit anyone
  hungry; on a trail → long-distance routes.
  "Likes cards and gifts" → a gift shop or cards on sale.
- null when the claim IS the ask ("Loves pizza" on a pizza ask, "Loves Italian food" on
  an Italian one) — they already said it.
- null when there is no real link for THIS kind of place. Better null than a stretch: a
  reason the reader would have to squint at is worse than none.
- THE TEST: would this look_for matter to THIS reader more than to anyone else looking
  for the same place? Live music — a guitarist, yes. Generous portions — everyone hungry,
  so no.
- NEVER generic quality: fresh, tasty, good service, friendly, quick, convenient, clean,
  cheap. Everyone wants those; they say nothing about this reader.
- A claim whose "sayable" is false (faith, heritage), or one about health, ethnicity,
  sexuality or anything intimate, is never expanded: its literal need or null ("Muslim"
  → "halal food", "because": "").

"by_hours": true ONLY when the place's opening hours alone could show look_for — it is
about the time of day ("Night owl" → open late, "Morning Runner" → open early). False for
everything else, "Usually late" included.

"you": the claim spoken TO the reader, in the "language" given, as it will open the line
— "You have two young kids", "You run in the mornings", "You play guitar", "You're
training for an Ironman", "Your kids love to draw" (about a household member). Never
what it points to ("You like live music"). "" when "sayable" is false. "because" is in
the same language.

Output ONLY JSON: {"claims": [{"id": "<claim id>", "look_for": "…" | null, "you": "…",
                              "because": "…", "by_hours": true|false}]}
"""


def expand_for_ask(
    claims: list[dict[str, Any]], ask: str, lang: str = "en",
) -> list[dict[str, Any]]:
    """The claims worth looking for on this page, each with the one thing it points to
    here (`look_for`) and the words that make the link obvious (`because`).

    ONE call per page, before any card is written, so every card hunts for the same honest
    link. It used to be each card's writer inventing its own — "you train for Ironman —
    it's a quick spot to take pizza to your room", "you're usually late — open until
    11 PM" (prod QA 2026-09-30). Fails CLOSED: no expansion, no "For you"."""
    if not claims:
        return []
    try:
        import json

        from app.orchestrator.llm import llm_configured, llm_json, synthesizer_model

        if not llm_configured():
            return []
        data = llm_json(
            model=synthesizer_model(),
            system=_EXPAND,
            user_payload=json.dumps({
                "ask": ask,
                "language": lang or "en",
                "claims": [{"id": c["id"], "claim": c["label"], "sayable": c["sayable"],
                            "about": c["about"]} for c in claims],
            }, ensure_ascii=False),
            max_tokens=40 + 56 * len(claims),
            temperature=0.0,
        )
    except Exception:  # noqa: BLE001
        logger.warning("reader_claims.expand_failed", exc_info=True)
        return []
    by_id = {c["id"]: c for c in claims}
    out: list[dict[str, Any]] = []
    for row in (data.get("claims") if isinstance(data, dict) else None) or []:
        if not isinstance(row, dict):
            continue
        claim = by_id.get(str(row.get("id") or ""))
        look = " ".join(str(row.get("look_for") or "").split())[:100]
        if not claim or not look or look.casefold() in ("null", "none"):
            continue
        because = " ".join(str(row.get("because") or "").split())[:60]
        you = " ".join(str(row.get("you") or "").split())[:80] if claim["sayable"] else ""
        out.append({**claim, "look_for": look, "because": because if claim["sayable"] else "",
                    "you": you,
                    # Whether opening hours may prove it — decided here, enforced in code.
                    "by_hours": row.get("by_hours") is True})
    return out


def claims_payload(claims: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """What a card writer is shown of the expanded claims."""
    return [
        {"id": c["id"], "claim": c["label"], "look_for": c.get("look_for") or c["label"],
         "by_hours": bool(c.get("by_hours")),
         "sayable": c["sayable"], "about": c["about"]}
        for c in claims or []
    ]


COMPOSER_RULES = """READER CLAIMS ("reader_claims") are what the reader has told Lana about
THEMSELVES or their household, each with "look_for": the one thing it makes them want
from this kind of place, already decided. Use them to find a "for_you" match: a quote
that shows THIS place has that "look_for".
- Look ONLY for "look_for" — never invent another link. A quote showing something else,
  or only generic quality (tasty, fresh, quick, friendly, convenient), serves no claim.
- "for_you": 0-3 candidates per card, EACH A DIFFERENT CLAIM, strongest first —
  {"claim": "<claim id>", "evidence": "…", "quotes": [<exactly one quote>]}.
  The page shows a claim on one card only, so a second and third candidate let every
  card say something different. NO item without a quote that shows "look_for".
- "evidence" is ONLY what the quote shows, as a clause that follows a dash, lower-case:
  "reviewers say a live band plays here", "reviewers say it's family friendly". Never
  the reader, never the claim (Lana puts "You play guitar —" in front of it), never a
  detail (a day, a dish) the quote does not contain.
- Cite opening hours ONLY for a claim whose "by_hours" is true, and say them as the
  place's hours, with the times the cited line actually gives — hours are never
  "reviewers say".
- For a claim about health, religion, ethnicity, heritage, sexuality or anything
  intimate, the evidence speaks only of the place ("everything here is halal").
- A claim "about" a household member ("child") is theirs, not the reader's: "your kids".
- A training or diet look_for needs the review to CALL the food what the claim needs
  (high-protein, lean, healthy, a protein bowl, macros listed). How it felt ("gave me
  energy", "kept me full") and what anyone would like (generous portions, two full
  skewers, a perfectly cooked steak) serve none.
- The quote must itself show the place is GOOD for look_for: "so patient with our
  toddlers" shows kid-friendly; "the kids were hungry and agitated" does not;
  "gluten-free chips" is not vegetarian food. Negative, neutral or merely nearby quotes
  never count — leave for_you empty rather than stretch.
- The evidence is in the "language" given, no counts of people.
"""

_JUDGE = """You check "For you" lines on recommendation cards. Each item has: "claim"
(something the reader told us about themselves), "look_for" (the one thing that claim
makes them want from this kind of place), the "line" the reader will see, and ONE
"quote" from a review, a neighbour, or the place's Google opening hours.

Answer TWO questions per item:

1. "supports": does the quote, on its own, explicitly show the place HAS look_for —
   positive and about that very thing? When unsure, false.
   - "kid-friendly" + "so patient with our two toddlers" → true
   - "kid-friendly" + "the kids were hungry and agitated" → false (negative)
   - "vegetarian dishes" + "the chips are gluten-free" → false (a different diet)
   - "vegetarian dishes" + "a gluten free waffle BLT" → false (a BLT is bacon)
   - "vegetarian dishes" + "lots of veggie options, the falafel is amazing" → true
   - "dogs welcome" + "we brought our pup and they brought him water" → true
   - "dogs welcome" + "there is outdoor seating as well" → false (not said to allow dogs)
   - "live music" + "a jazz trio plays on Friday nights" → true
   - "live music" + "played good music" → false (background music is not live)
   - "open late" + opening hours "Friday: 11:00 AM – 1:00 AM" → true
   - "early hours" + opening hours "Monday: 5:00 AM – 10:00 PM" → true; "10:00 AM" → false
   Opening hours show ONLY a look_for about the time of day (open late, open early) —
   never walk-ins, a table held, or anything for someone who is often late.
   - "high-protein meals" + "the crust was perfectly crispy" → false (generic quality)
   - "hearty high-protein or carb-rich food" + "giving me a lot of energy in the morning" →
     false: how a meal made someone FEEL (energy, full, satisfied, fuelled) is not what
     the food IS.
   - "high-protein or lean dishes" + "portions are generous" / "two full skewers" / "the
     steak was cooked perfectly" → false: anyone hungry wants that; it says nothing about
     training. The review must call the food high-protein, lean or healthy.
   - "high-protein or lean dishes" + "great high-protein bowls, they list the macros" → true
   The test for every item: does the quote show something that matters to THIS claim more
   than to anyone else looking for the same place? If not, false.
   Generic praise (tasty, fresh, crispy, quick, friendly, convenient) never shows a
   specific look_for.

2. "clear": reading ONLY the line, would the reader immediately see why their claim makes
   this place a better fit FOR THEM — and does the line say only what the quote shows?
   "You play guitar — reviewers say there's live music on Fridays": true. "You like cards
   and gifts — reviewers say there's a gift shop with souvenirs": true. "You train for
   Ironman — reviewers say it's a quick spot to grab pizza to take to your room": false.
   Every fact after the dash — a time, a day, a dish — must be in the quote: "it opens
   at 5 AM" on hours "Monday: Open 24 hours" is false.
   A line about the reader that never names their claim ("You like live music" for
   "Plays guitar", "Dogs are allowed here" for "Has a dog") is false — unless the claim is
   one never to be named (faith, heritage, health), whose line speaks only of the place.

Output ONLY JSON:
{"items": [{"key": "<key>", "claim": "<the item's claim, copied>", "supports": true|false,
            "clear": true|false}]}
"""


JUDGE_CHUNK = 3


def judge_for_you(pairs: list[dict[str, str]]) -> set[str]:
    """The keys of `pairs` ({key, ask, claim, look_for, line, quote}) the judge confirms.

    A second, independent read — the writer proposes, this confirms. Verbatim checks prove
    a quote EXISTS; only this proves it is ON POINT (prod QA 2026-09-29: real quotes matched
    "kids" to "the kids were hungry and agitated" and "vegetarian" to "gluten-free chips").
    Small calls in parallel, never one big one: with 18 pairs in a single call the model
    filed verdicts under the wrong keys ("Sicilian heritage" on the "Loves Italian food"
    pair, 2026-09-30). Fails CLOSED: any error drops that chunk's lines."""
    if not pairs:
        return set()
    try:
        from app.orchestrator.llm import llm_configured

        if not llm_configured():
            return set()
    except Exception:  # noqa: BLE001
        return set()
    # A claim that was never expanded is looked for as itself.
    pairs = [{**p, "look_for": str(p.get("look_for") or "").strip() or p.get("claim", "")}
             for p in pairs]
    chunks = [pairs[i:i + JUDGE_CHUNK] for i in range(0, len(pairs), JUDGE_CHUNK)]
    if len(chunks) == 1:
        return _judge_chunk(chunks[0])
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=min(len(chunks), 8), thread_name_prefix="fyjudge") as pool:
        return set().union(*pool.map(_judge_chunk, chunks))


def _judge_chunk(pairs: list[dict[str, str]]) -> set[str]:
    try:
        import json

        from app.orchestrator.llm import llm_json, synthesizer_model

        data = llm_json(
            # The synth tier (gpt-4.1): this is a judgement, not wording. On 18 labelled
            # pairs x 3 runs (2026-09-30) it was 18/18 every run in ~2s; gpt-4.1-mini
            # missed 4-5 a run ("Morning Runner" = jogging, "Plays guitar" adds nothing to a
            # steakhouse) and gpt-5.4-mini 1-3. A few hundred tokens a card.
            model=synthesizer_model(),
            system=_JUDGE,
            user_payload=json.dumps({"items": pairs}, ensure_ascii=False),
            max_tokens=40 + 40 * len(pairs),
            temperature=0.0,
        )
    except Exception:  # noqa: BLE001
        logger.warning("reader_claims.judge_failed", exc_info=True)
        return set()
    items = data.get("items") if isinstance(data, dict) else None
    claim_of = {str(p["key"]): " ".join(str(p.get("claim") or "").split()).casefold() for p in pairs}
    return {
        str(i.get("key")) for i in items
        if isinstance(i, dict) and str(i.get("key")) in claim_of
        # The verdict must name the claim it judged: one filed under the wrong key is lost.
        and " ".join(str(i.get("claim") or "").split()).casefold() == claim_of[str(i.get("key"))]
        and i.get("supports") is True and i.get("clear") is True
    } if isinstance(items, list) else set()


def keep_judged(
    items: list[dict[str, Any]], claims: list[dict[str, Any]], ask: str = "",
) -> None:
    """In place: drop from each item's `for_you` whatever the judge does not confirm, and
    within a confirmed one keep only confirmed quotes. `items` are dicts carrying
    `for_you: [{claim_id, quotes: [{text, …}]}]`."""
    labels = {c["id"]: c["label"] for c in claims or []}
    looks = {c["id"]: c.get("look_for") or "" for c in claims or []}
    pairs: list[dict[str, str]] = []
    for i, it in enumerate(items):
        for j, f in enumerate(it.get("for_you") or []):
            for k, q in enumerate(f.get("quotes") or []):
                pairs.append({"key": f"{i}.{j}.{k}", "ask": ask,
                              "claim": labels.get(f.get("claim_id"), ""),
                              "look_for": looks.get(f.get("claim_id"), ""),
                              "line": str(f.get("line") or ""),
                              "quote": str(q.get("text") or "")})
    ok = judge_for_you(pairs)
    for i, it in enumerate(items):
        kept = []
        for j, f in enumerate(it.get("for_you") or []):
            quotes = [q for k, q in enumerate(f.get("quotes") or []) if f"{i}.{j}.{k}" in ok]
            if quotes:
                kept.append({**f, "quotes": quotes})
        it["for_you"] = kept


def one_claim_per_card(items: list[dict[str, Any]]) -> None:
    """In place, in page order: each item keeps ONE confirmed `for_you` — its strongest
    whose claim no earlier card already used. Three steakhouses all saying "you're usually
    late" (prod QA 2026-09-30) tell the reader nothing about the second and third; a card
    whose every claim is taken shows none."""
    used: set[str] = set()
    for it in items:
        pick = next((f for f in it.get("for_you") or [] if f.get("claim_id") not in used), None)
        it["for_you"] = [pick] if pick else []
        if pick:
            used.add(pick.get("claim_id"))


def assemble_line(claim: dict[str, Any], evidence: str) -> str:
    """The line the reader sees: the claim spoken to them (written once, per page, by the
    expansion), the link when it is not obvious, then what the quote shows —
    "You're training for an Ironman, so fuel matters — reviewers say …". A quiet claim,
    or one never expanded, gets the evidence alone: it never names the reader."""
    ev = " ".join(str(evidence or "").split()).strip(" .—-")
    if not ev:
        return ""
    you = str(claim.get("you") or "").strip(" .,—-") if claim.get("sayable") else ""
    if not you:
        return ev[0].upper() + ev[1:] + "."
    because = str(claim.get("because") or "").strip(" .,—-")
    return f"{you}{', ' + because if because else ''} — {ev[0].lower() + ev[1:]}."
