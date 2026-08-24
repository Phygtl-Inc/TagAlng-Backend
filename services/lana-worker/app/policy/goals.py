"""candidate_goals(user_id, world) — the four goal queues, normalized into one
list the policy arbitrates over (engineering doc §C.2).

This is a consumption merge, not a schema merge: rapport_gaps,
circle_affiliations (ungrounded → ask which place; grounded → offer to organize
something for it), suggestion_queue, pending_signal_asks, and the available
capabilities keep their own tables — they just arrive at the policy in one
shape:

    {id, kind, summary, value_hint, context}

value_hint is a SOFT prior (0.0-1.0, folded from unlock_score / confidence /
surface_priority) — the policy may override it; nothing is ordered by it in
code. Every read is best-effort: a dead queue contributes nothing, never an
error.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.auth import service_client
from app.policy.world import capabilities_available

logger = logging.getLogger(__name__)

_MAX_PER_QUEUE = 4

# How long a gap asked in CONVERSATION stays off the candidate list. Asked and
# ignored is not answered, so it may come back — but not on the next turn, and
# not three turns running (QA 2026-08-03).
CHAT_ASK_COOLDOWN_HOURS = 24

_GAP_FIELDS = (
    "gap_row_id, gap_id, question, covers_concept, why_frame, why_reason, "
    "unlock_score, chat_asked_at, affiliation_ref"
)
# Pre-20260930 / pre-20260928 environments lack why_reason / chat_asked_at; step down
# rather than degrade the whole rapport queue to empty. The last tier also drops
# affiliation_ref (pre-circles), which only costs the grounded-gap prune below.
_GAP_FIELDS_NO_REASON = (
    "gap_row_id, gap_id, question, covers_concept, why_frame, unlock_score, "
    "chat_asked_at, affiliation_ref"
)
_GAP_FIELDS_LEGACY = (
    "gap_row_id, gap_id, question, covers_concept, why_frame, unlock_score"
)


# Goal kinds that name a SPECIFIC real-world thing (a venue, or a community tied to
# one). These are standing goals — they sit on the menu every turn — so they are the
# ones that surface at the wrong moment.
_PLACE_KINDS = ("ungrounded_circle", "circle_offer")

_GATE_STOPWORDS = frozenset(
    {"the", "a", "an", "my", "our", "your", "at", "in", "on", "of", "and", "or",
     "to", "for", "is", "it", "with", "st", "street", "road", "rd", "ave"}
)


def _gate_tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", str(text or "").lower())
    return {w for w in words if len(w) > 2 and w not in _GATE_STOPWORDS}


def goal_matches_message(goal: dict[str, Any], message: str) -> bool:
    """Does this goal name something the person is ACTUALLY talking about?

    Compared against the goal's most SPECIFIC identifier — the pinned venue name
    when there is one, else the circle's own words. Comparing the category instead
    would defeat the whole thing: "restaurant" is highly related to "Piazza Italia
    or Mangia", so the circle pinned to Mizu Sushi would be waved through and offered
    again (the 2026-08-05 bug).

    ponytail: lexical overlap, not embeddings. We are matching NAMES and activity
    words, which are lexical, and it costs nothing on a hot path. A miss ("ping pong"
    vs "table tennis") withholds the goal, which is the safe direction — the home tile
    still asks it. Upgrade to the claim-ranking embedding if real misses show up.
    """
    said = _gate_tokens(message)
    if not said:
        return False
    ctx = goal.get("context") or {}
    subject = str(ctx.get("place_name") or "") or str(ctx.get("circle_key") or "")
    return bool(_gate_tokens(subject) & said)


def filter_place_goals(
    goals: list[dict[str, Any]], *, message: str, turn_has_topic: bool
) -> list[dict[str, Any]]:
    """Withhold place-naming goals that have nothing to do with this turn.

    The policy picks from what it is given, so a stale goal on the menu is a stale
    goal that can ship. Twice now one has: a gym-grounding question answered a
    restaurant message, and a get-together at Mizu Sushi was offered four messages
    into a conversation about The Piazza Italia. A prompt rule covered both and held
    two out of three times.

    When the person raised NOTHING of their own, stale goals are exactly right —
    nothing is being interrupted and a specific question beats "how can I help you
    today?". So they are withheld only when there is a real topic to trample.
    """
    place_goals = [g for g in goals if g.get("kind") in _PLACE_KINDS]
    if not place_goals:
        return goals
    if not str(message or "").strip():
        # No message to judge against — gate nothing. Withholding on absent
        # information would silently empty the menu for any caller that does not
        # pass one, which is worse than the mistimed goal this filter exists to stop.
        return goals
    relevant = [g for g in place_goals if goal_matches_message(g, message)]
    if relevant:
        keep = {id(g) for g in relevant}
    elif not turn_has_topic:
        keep = {id(g) for g in place_goals}  # an empty turn — all fair game
    else:
        keep = set()
    dropped = len(place_goals) - len(keep)
    if dropped:
        logger.info(
            "place_goals_withheld %d of %d (topic=%s)",
            dropped, len(place_goals), turn_has_topic,
        )
    return [g for g in goals if g.get("kind") not in _PLACE_KINDS or id(g) in keep]


def _clamp01(x: Any) -> float:
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return 0.5


def _asked_in_chat_recently(row: dict[str, Any]) -> bool:
    """True while a conversationally-asked gap is still cooling down."""
    from datetime import datetime, timedelta, timezone

    raw = str(row.get("chat_asked_at") or "").strip()
    if not raw:
        return False
    try:
        when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - when < timedelta(hours=CHAT_ASK_COOLDOWN_HOURS)


def _rapport_goals(user_id: str) -> list[dict[str, Any]]:
    """Open 'By the way…' questions — the one queue that's fully live today.

    Gaps Lana already asked in conversation are held back for a day: the policy
    reads this list every turn, so without the filter one open gap was re-offered
    (and re-asked, reworded) turn after turn.
    """
    rows: list[dict[str, Any]] = []
    for fields in (_GAP_FIELDS, _GAP_FIELDS_NO_REASON, _GAP_FIELDS_LEGACY):
        try:
            res = (
                service_client()
                .table("rapport_gaps")
                .select(fields)
                .eq("user_id", user_id)
                .eq("status", "open")
                .order("unlock_score", desc=True)
                # Over-fetch: the cooldown filter below runs in Python, so the
                # cap must survive dropping a few rows.
                .limit(_MAX_PER_QUEUE * 3)
                .execute()
            )
            rows = [r for r in (res.data or []) if isinstance(r, dict)]
            break
        except Exception:
            continue
    else:
        logger.warning("goals_rapport_failed user=%s", user_id)
        return []
    rows = [r for r in rows if not _asked_in_chat_recently(r)]
    # A grounding gap whose place has since been pinned is not a question any more —
    # the chat path has its own gap queue, so the tile-side prune doesn't reach it and
    # Lana asked "which Fitness CF is yours?" about a community she was showing members
    # of (2026-08-18). Before the cap: a dead ask must not hold a slot either.
    if any(r.get("affiliation_ref") for r in rows):
        try:
            from app.circles_flow import prune_grounded_gaps

            rows = prune_grounded_gaps(user_id, rows)
        except Exception:  # noqa: BLE001 — a stale ask beats an empty queue
            logger.exception("goals_grounded_prune_failed user=%s", user_id)
    rows = rows[:_MAX_PER_QUEUE]
    return [
        {
            "id": f"gap:{r.get('gap_row_id')}",
            "kind": "rapport_gap",
            "summary": str(r.get("question") or r.get("covers_concept") or "")[:200],
            "value_hint": _clamp01(r.get("unlock_score")),
            # why_reason is the real rationale ("so I can introduce you to neighbors
            # who…"); the teaser is only a topic label, so it's the fallback.
            "context": {
                "why": r.get("why_reason") or r.get("why_frame"),
                "concept": r.get("covers_concept"),
            },
        }
        for r in rows
        if r.get("question") or r.get("covers_concept")
    ]


def _grounding_goals(world: dict[str, Any]) -> list[dict[str, Any]]:
    """Communities the user mentioned that aren't pinned to a real place yet
    ('my gym' → which gym?). Read straight off the world snapshot — no extra query."""
    from app.circles_capture import same_community

    grounded = [
        c
        for c in (world.get("circles") or [])
        if c.get("grounded") and str(c.get("key") or "").strip()
    ]
    out: list[dict[str, Any]] = []
    for c in world.get("circles") or []:
        if c.get("grounded"):
            continue
        key = str(c.get("key") or "").strip()
        if not key:
            continue
        # Already answered under a different phrasing. Prod 2026-08-07 asked "which
        # specific spot is your Fitness CF St. Cloud gym?" while `fitness_cf` was
        # pinned to Fitness CF - St. Cloud — a question we could already answer,
        # asked because capture had made a second row for the same gym.
        if any(
            same_community(
                key, str(c.get("type") or ""), str(g.get("key") or ""),
                str(g.get("type") or ""),
                a_noun=c.get("noun"), b_noun=g.get("noun"),
            )
            for g in grounded
        ):
            logger.info("grounding_skipped_sibling_pinned key=%s", key)
            continue
        out.append(
            {
                "id": f"circle:{key}",
                "kind": "ungrounded_circle",
                "summary": f"user mentioned their {key.replace('_', ' ')} — the exact place is not known yet",
                "value_hint": 0.6,
                "context": {"circle_key": key, "circle_type": c.get("type")},
            }
        )
        if len(out) >= _MAX_PER_QUEUE:
            break
    return out


def _circle_offer_goals(world: dict[str, Any]) -> list[dict[str, Any]]:
    """Grounded communities the policy can offer to organize something FOR.

    Ungrounded circles arrive above as `ungrounded_circle` (ask which place);
    once pinned they used to leave the goal list entirely, so nothing
    community-shaped remained to pursue and a vague "I'm bored" degraded into a
    read-out of the capability catalog (QA 2026-07-31 — four features named,
    three generic chips, no offer). Hosting and inviting are always available
    (§D.2 — unlock gates consumption, never creation), so this goal is honest
    even in a still-waking area. `send` is pre-written so a chip accepting the
    offer is self-contained: the host engine re-reads it as a fresh message and
    never saw the bubble it came from.
    """
    out: list[dict[str, Any]] = []
    for c in world.get("circles") or []:
        if not (c.get("grounded") and c.get("confirmed")):
            continue
        key = str(c.get("key") or "").strip()
        if not key:
            continue
        # Their own word for the group, never a label we invented.
        topic = key.replace("_", " ")
        place = str(c.get("place") or "").strip()
        where = f" at {place}" if place else ""
        pinned = f", pinned to {place}" if place else ""
        out.append(
            {
                "id": f"circle_offer:{key}",
                "kind": "circle_offer",
                "summary": (
                    f"their {topic}{pinned} — a community of theirs you can offer to "
                    "organize ONE specific get-together for"
                ),
                "value_hint": 0.7,
                "context": {
                    "circle_key": key,
                    "circle_type": c.get("type"),
                    "place_name": place or None,
                    "send": f"help me host a get-together for my {topic}{where}",
                },
            }
        )
        if len(out) >= _MAX_PER_QUEUE:
            break
    return out


def _offer_goals(user_id: str) -> list[dict[str, Any]]:
    """Unsurfaced latent-intent suggestions (Layer 3). Empty unless
    LANA_LATENT_EXTRACT has been collecting — the merge tolerates that."""
    try:
        res = (
            service_client()
            .table("suggestion_queue")
            .select("id, capability_id, suggestion_text, confidence, trigger_context")
            .eq("user_id", user_id)
            .is_("surfaced_at", "null")
            .is_("user_action", "null")
            .or_("expires_at.is.null,expires_at.gt.now()")
            .order("confidence", desc=True)
            .limit(_MAX_PER_QUEUE)
            .execute()
        )
        rows = [r for r in (res.data or []) if isinstance(r, dict)]
    except Exception:
        logger.exception("goals_offers_failed user=%s", user_id)
        return []
    return [
        {
            "id": f"offer:{r.get('id')}",
            "kind": "pending_offer",
            "summary": str(r.get("suggestion_text") or "")[:200],
            "value_hint": _clamp01(r.get("confidence")),
            "context": {"capability_id": r.get("capability_id")},
        }
        for r in rows
        if r.get("suggestion_text")
    ]


def _pending_ask_goals(user_id: str) -> list[dict[str, Any]]:
    """A stashed signal ask waiting to resume (peek only — pop stays with the
    login recovery path that owns the delete)."""
    try:
        res = (
            service_client()
            .table("pending_signal_asks")
            .select("ask")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        row = (res.data or [None])[0]
    except Exception:
        return []
    ask = row.get("ask") if isinstance(row, dict) else None
    if not isinstance(ask, dict) or not ask:
        return []
    summary = str(ask.get("detail_text") or ask.get("category") or ask.get("intent") or "")[:200]
    if not summary:
        return []
    return [
        {
            "id": "pending_ask",
            "kind": "pending_offer",
            "summary": f"they earlier asked about: {summary} — still unresolved",
            "value_hint": 0.7,
            "context": {"ask": ask},
        }
    ]


def _capability_goals(caps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What Lana can offer right now — capability_index filtered by
    required_state ⊆ user states. surface_priority folds into value_hint as a
    weak prior (5→0.5, 8→0.8), nothing more.

    Takes the already-fetched list: candidate_goals needs the same rows for its
    inactive-capability filter, and reading capability_index twice per turn is
    latency for nothing."""
    out: list[dict[str, Any]] = []
    for cap in caps:
        cap_id = str(cap.get("capability_id") or "").strip()
        if not cap_id:
            continue
        out.append(
            {
                "id": f"cap:{cap_id}",
                "kind": "capability",
                "summary": str(cap.get("description") or cap.get("capability_name") or "")[:200],
                "value_hint": _clamp01((cap.get("surface_priority") or 5) / 10.0),
                "context": {"capability_id": cap_id},
            }
        )
    return out


def candidate_goals(
    user_id: str,
    world: dict[str, Any],
    *,
    deferred_goal_ids: list[str] | None = None,
    user_message: str = "",
    turn_has_topic: bool = True,
) -> list[dict[str, Any]]:
    """The unified goal list for one turn. Goals the policy previously deferred
    (capture_defer) are marked so it knows they're fair game to resurface at
    the next natural pause."""
    # One read of capability_index, used twice below.
    caps = capabilities_available(world)
    goals = (
        _rapport_goals(user_id)
        + _grounding_goals(world)
        + _circle_offer_goals(world)
        + _offer_goals(user_id)
        + _pending_ask_goals(user_id)
        + _capability_goals(caps)
    )
    # A goal naming a capability that is switched off must not reach the policy.
    # _capability_goals already filters on is_active; _offer_goals (latent
    # suggestions) did not, so a retired capability could still be offered from
    # the queue — which is how an unshipped feature gets pitched in chat.
    active = {c.get("capability_id") for c in caps}
    goals = [
        g for g in goals
        if not g["context"].get("capability_id")
        or g["context"]["capability_id"] in active
    ]
    # Withhold place-naming goals that this turn is not about (see filter_place_goals).
    goals = filter_place_goals(
        goals, message=user_message, turn_has_topic=turn_has_topic
    )
    deferred = set(deferred_goal_ids or [])
    for g in goals:
        if g["id"] in deferred:
            g["context"]["deferred_earlier"] = True
    return goals
