"""Agentic "what's happening" browse — ask one refining question, then show the REAL
events on the block, with semantic re-filtering ("show me cricket ones instead").

This mirrors the look_meet capture's conversational shape, but it READS events (the
events table, via discovery_route.fetch_preview_events_on_block) instead of saving a
meet_seek signal. It is the BROWSE side of the browse-vs-seek split; look_meet is the
SEEK side.

Flow (ask-first):
  P1  "What kind of thing are you up for?"   (interest chips)
  P2  filter the block's events by that interest (semantic) → show event cards
  →   refine ("anything outdoors?", "no, cricket") re-filters the same event set; a
      pivot to another intent (find people / host / a meet) releases the flow.
"""

from __future__ import annotations

import logging
import re
from typing import Any

_INTEREST_SUGGESTIONS = ["Sports", "Family & kids", "Outdoors", "Social"]
_BROWSE_TURN_CAP = 12

# "Open to anything" answers — don't filter, just show everything. Anchored to the FULL
# message (trailing punctuation allowed) so "anything on July 5" is NOT treated as open —
# it carries a real constraint and must reach the date/host matcher.
_OPEN_RE = re.compile(
    r"^\s*(?:any(?:thing)?|all|everything|whatever|surprise me|idk|i don'?t know|"
    r"you (?:pick|choose|decide)|no preference|open)\s*[.!]*\s*$",
    re.IGNORECASE,
)
_CANCEL_RE = re.compile(
    r"\b(cancel|never\s*mind|nvm|stop|forget it|not now|skip this|exit|quit)\b",
    re.IGNORECASE,
)
# Explicit pivot to another intent — needs a find/show verb + people noun so an interest
# answer ("family", "all moms welcome") is never mistaken for a pivot.
_PIVOT_OUT_RE = re.compile(
    r"\b(?:find|show)\s+(?:me\s+)?(?:\w+\s+){0,3}(?:moms?|dads?|parents?|neighbou?rs?|people|families)\b|"
    r"\bshow (?:my )?(?:block log|intros)\b|\bmy block log\b|\blog\s?out\b|\bsign out\b",
    re.IGNORECASE,
)


def reset_activity_browse_state(session_ctx: dict[str, Any]) -> None:
    """Drop the browse flow + its state so the turn falls through to normal routing.
    Keys set to None (not popped) so the {**old, **new} session merge clears them."""
    for k in ("activity_browse_active", "browse_draft", "activity_previews"):
        session_ctx[k] = None
    session_ctx["browse_turns"] = 0


# Lanes this browse does NOT own — a confident classification into any of these is a
# pivot, never a browse refinement. Note meet_seek/looking.meet ARE foreign here so the
# user can switch from browsing events to being matched (handled by look_meet); plain
# "activities" / find_activities is THIS lane = a re-filter, so it is NOT foreign.
_FOREIGN_GOALS = frozenset(
    {"peers", "both", "propose_intro", "list_intros", "verify", "login",
     "logout", "show_block_log", "rsvp"}
)
_FOREIGN_LINEAR_PREFIXES = ("identity.", "social.", "auth.", "settings.", "tier.")
_FOREIGN_LINEARS = frozenset(
    {"discovery.find_peers", "discovery.find_by_attrs", "discovery.find_in_block",
     "discovery.block_log", "looking.meet", "sharing.host", "sharing.swap",
     "looking.swap", "sharing.tip", "looking.tip"}
)
_FOREIGN_SIGNALS = frozenset(
    {"meet_seek", "host_meet", "swap_seek", "swap_offer", "tip_seek", "tip_share"}
)


def _is_browse_answer(
    message: str, session_ctx: dict[str, Any], slots: dict[str, Any] | None
) -> bool:
    """Is this turn an answer/refine for the browse? Any non-foreign message is — the
    interest at P1, or a re-filter ("show me cricket instead", "anything outdoors")
    afterwards. A meta/question turn ("what's my zip?") or a confident pivot to another
    lane is NOT — release so it's answered instead of used as an event filter."""
    from app.lane_decision import is_confident_foreign, is_meta_or_chat

    if is_meta_or_chat(slots):
        return False
    return not is_confident_foreign(
        slots,
        foreign_goals=_FOREIGN_GOALS,
        foreign_linear_prefixes=_FOREIGN_LINEAR_PREFIXES,
        foreign_linears=_FOREIGN_LINEARS,
        foreign_signals=_FOREIGN_SIGNALS,
    )


def activity_browse_should_release(
    message: str,
    session_ctx: dict[str, Any],
    slots: dict[str, Any] | None = None,
) -> bool:
    """Whether the sticky browse flow should release this turn and hand back to routing.

    Continue-only-on-match: the browse is kept for a genuine refine/answer
    (``_is_browse_answer``) or an explicit cancel (a graceful in-flow exit). A confident
    pivot to another lane, a semantic abandon, or a low-confidence read all release — so
    the user is never trapped. A different ACTIVITY ("show me cricket instead") is a
    refine and stays; switching to being matched (meet_seek) releases."""
    from app.lane_decision import lane_should_continue

    return not lane_should_continue(
        message,
        session_ctx,
        slots,
        is_valid_answer=_is_browse_answer,
        pivot_re=_PIVOT_OUT_RE,
    )


# How many upcoming events to pull as the candidate pool BEFORE filtering. Larger than
# the 5 we ultimately show, so a date/host query ("on July 5", "hosted by Asjid") can
# reach events that aren't among the soonest few.
_BROWSE_POOL = 40


def _attach_host_names(events: list[dict[str, Any]]) -> None:
    """Stamp each event with `host_name` (the host's nickname) so the filter can match
    'hosted by <name>' queries. Best-effort — leaves host_name absent on any failure."""
    host_ids = {str(e.get("host_id")) for e in events if e.get("host_id")}
    if not host_ids:
        return
    try:
        from app.auth import service_client

        res = (
            service_client()
            .table("users")
            .select("id, nickname")
            .in_("id", list(host_ids))
            .execute()
        )
        names = {
            str(r.get("id")): str(r.get("nickname") or "").strip()
            for r in (res.data or [])
            if isinstance(r, dict)
        }
        for e in events:
            e["host_name"] = names.get(str(e.get("host_id")), "")
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception("activity_browse_host_names_failed")


def _fetch_block_events(
    user_jwt: str, block_id: str | None, *, weekend_only: bool
) -> list[dict[str, Any]]:
    if not block_id:
        return []
    try:
        from app.discovery_route import fetch_preview_events_on_block

        events = fetch_preview_events_on_block(
            block_id, limit=_BROWSE_POOL, pool=_BROWSE_POOL, weekend_only=weekend_only
        )
        _attach_host_names(events)
        return events
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception("activity_browse_fetch_failed")
        return []


def _today_str() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d (%A)")


def _event_when_parts(raw: Any) -> str:
    """'2026-07-05 Sat' for the LLM — so it can match a date/timeframe query."""
    from datetime import datetime

    s = str(raw or "").strip()
    if not s:
        return ""
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %a")
    except ValueError:
        return s[:10]


def _filter_events_by_query(
    events: list[dict[str, Any]], query: str
) -> tuple[list[dict[str, Any]], str]:
    """Parse + match the user's request against the block's events with ONE LLM call.

    The request can name a topic ('cricket', 'family'), a date/timeframe ('on July 5',
    'this weekend', 'tomorrow'), and/or a host ('hosted by Asjid') — in any phrasing. The
    model gets each event's title, tags, date, weekday and host, plus today's date, and
    returns the events matching ALL constraints the request expresses, with a short label
    for the header. Open/vague requests return everything. Returns (matched, label).
    """
    if not events:
        return [], ""
    query = str(query or "").strip()
    if not query or _OPEN_RE.match(query):
        return events, ""
    try:
        from app.orchestrator.llm import llm_configured, llm_json, router_model

        if llm_configured():
            lines = []
            for i, ev in enumerate(events):
                tags = ev.get("cohort_tags")
                tagstr = ", ".join(tags) if isinstance(tags, list) else str(tags or "")
                when = _event_when_parts(ev.get("starts_at"))
                host = str(ev.get("host_name") or "").strip()
                lines.append(
                    f"{i}: {ev.get('title', '')} | date: {when} | "
                    f"host: {host or '?'} | tags: {tagstr}"
                )
            data = llm_json(
                model=router_model(),
                system=(
                    "You filter a neighborhood's upcoming events by a resident's request. "
                    f"Today is {_today_str()}. The request may mention a TOPIC "
                    "('cricket'/'sports' fits a 'FIFA watch party'; 'family' fits a "
                    "'park playdate'), a DATE or TIMEFRAME ('on July 5', 'this weekend', "
                    "'tomorrow' — resolve relative to today), and/or a HOST name ('hosted "
                    "by Asjid' → match the host field, case-insensitive). Return JSON "
                    '{"match_indices":[ints], "label":"short phrase"}: indices of events '
                    "satisfying EVERY constraint the request expresses (a date query must "
                    "match the event's date; a host query must match the host). label is a "
                    "short human phrase for the filter (e.g. 'July 5', 'hosted by Asjid', "
                    "'cricket') or \"\" if the request is open/unfiltered. If open, return "
                    "all indices. Empty match_indices if nothing fits."
                ),
                user_payload=(
                    f"Request: {query}\nEvents:\n" + "\n".join(lines)
                    + '\nReturn {"match_indices":[...], "label":"..."}.'
                ),
                max_tokens=200,
                temperature=0.0,
            )
            if isinstance(data, dict):
                idxs = data.get("match_indices")
                label = str(data.get("label") or "").strip()
                if isinstance(idxs, list):
                    picked = [
                        events[i]
                        for i in idxs
                        if isinstance(i, int) and 0 <= i < len(events)
                    ]
                    return picked, label
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception("activity_browse_filter_failed")
    # Fallback (no LLM): keyword match on title + tags + host; nothing matched → show all.
    kw = query.lower()
    matched = [
        e
        for e in events
        if kw
        in (
            str(e.get("title", ""))
            + " "
            + str(e.get("cohort_tags", ""))
            + " "
            + str(e.get("host_name", ""))
        ).lower()
    ]
    return (matched or events), ""


def _format_browse_message(
    events: list[dict[str, Any]], label: str | None, *, phone_verified: bool
) -> str:
    label = (label or "").strip() or None
    if not events:
        lead = f"No {label} ones" if label else "Nothing"
        return (
            f"{lead} on your block in the next couple weeks. Want me to widen it, "
            "try another kind, or set up your own?"
        )
    from app.discovery_route import _format_event_when

    head = f"Here's what's coming up{(' for ' + label) if label else ''}:"
    lines = [head]
    for ev in events[:5]:
        title = str(ev.get("title") or "Activity")
        venue = str(ev.get("venue_name") or "").strip()
        when = _format_event_when(ev.get("starts_at"))
        line = f"• {title}"
        if venue:
            line += f" at {venue}"
        if when:
            line += f" ({when})"
        lines.append(line)
    tail = (
        "Tap one to RSVP, or tell me to narrow it (e.g. 'just cricket')."
        if phone_verified
        else "Verify your email to RSVP, or tell me to narrow it (e.g. 'just cricket')."
    )
    lines.append(tail)
    return "\n".join(lines)


def run_activity_browse_turn(
    *,
    user_message: str,
    session_ctx: dict[str, Any],
    history: list[dict[str, Any]],
    user_jwt: str,
    home_block_id: str | None,
) -> str:
    """Drive one browse turn. Mutates session_ctx (browse_draft, activity_browse_active,
    activity_previews, routing_phase). Returns Lana's reply."""
    msg = str(user_message or "").strip()
    draft: dict[str, Any] = dict(session_ctx.get("browse_draft") or {})
    phone_verified = bool(session_ctx.get("phone_verified"))

    turns = int(session_ctx.get("browse_turns") or 0) + 1
    session_ctx["browse_turns"] = turns
    if _CANCEL_RE.search(msg) or turns > _BROWSE_TURN_CAP:
        reset_activity_browse_state(session_ctx)
        session_ctx["routing_phase"] = "listening"
        return "No problem — we can look another time. What else can I help with?"

    # ── P1: ask the interest ONCE (with chips), unless they already named one ──
    if not draft.get("interest") and not draft.get("_asked"):
        draft["_asked"] = True
        draft["suggestions"] = _INTEREST_SUGGESTIONS
        session_ctx["browse_draft"] = draft
        session_ctx["activity_browse_active"] = True
        session_ctx["routing_phase"] = "listening"
        return "Love it — what kind of thing are you up for?"

    from app.discovery_route import (
        extract_zip,
        fetch_blocks_for_zip,
        resolve_block_id,
    )

    def _set_preview_block(zip5: str, blocks: list[dict[str, Any]]) -> str:
        bid = str(blocks[0].get("block_id") or "")
        session_ctx["preview_block_id"] = bid
        session_ctx["preview_zip"] = zip5
        session_ctx["preview_block_label"] = str(
            blocks[0].get("label") or blocks[0].get("name") or zip5
        )
        # A verified user who gives their ZIP here should have it stick to their profile, so
        # they aren't re-asked next session (best-effort; no-op if already assigned).
        if phone_verified and not home_block_id:
            try:
                from app.discovery_route import _try_assign_home_block

                _try_assign_home_block(user_jwt, session_ctx=session_ctx, home_block_id=None)
            except Exception:  # noqa: BLE001
                logging.getLogger(__name__).exception("activity_browse_assign_block_failed")
        return bid

    def _ask_zip(prompt: str) -> str:
        draft["_need_zip"] = True
        draft["suggestions"] = _INTEREST_SUGGESTIONS
        session_ctx["browse_draft"] = draft
        session_ctx["activity_browse_active"] = True
        session_ctx["routing_phase"] = "listening"
        return prompt

    # ── If we asked for a ZIP last turn, this message is the ZIP (don't treat it as the
    #    interest). Otherwise the message is the interest (first answer) or a refinement. ──
    if draft.get("_need_zip"):
        zip5 = extract_zip(msg)
        if not zip5:
            return _ask_zip("What's your ZIP so I can see what's on your block?")
        blocks = fetch_blocks_for_zip(user_jwt, zip5)
        if not blocks:
            return _ask_zip(
                f"I couldn't find a block for ZIP {zip5}. Try another (e.g. 32827 for Lake Nona)."
            )
        _set_preview_block(zip5, blocks)
        draft["_need_zip"] = None
    elif msg:
        draft["interest"] = msg[:80]
    interest = str(draft.get("interest") or "")

    # Resolve the block to read events from — a ZIP given anywhere in this conversation
    # (session preview_block_id) counts, not just the persisted profile block. Ask for the
    # ZIP in-flow rather than dead-ending on "Nothing on your block" when none is known.
    block_id = resolve_block_id(session_ctx, home_block_id)
    if not block_id:
        zip5 = extract_zip(msg) or session_ctx.get("preview_zip")
        if zip5:
            blocks = fetch_blocks_for_zip(user_jwt, str(zip5))
            if blocks:
                block_id = _set_preview_block(str(zip5), blocks)
        if not block_id:
            return _ask_zip(
                "What's your ZIP code? Once I know your block I can show what's happening nearby."
            )

    # "weekend" is handled by the LLM date matcher too, but keep the SQL-side weekend
    # filter as a cheap pre-narrow when the word appears verbatim.
    weekend_only = bool(re.search(r"\bweekend\b", interest, re.I) or re.search(r"\bweekend\b", msg, re.I))
    events = _fetch_block_events(user_jwt, block_id, weekend_only=weekend_only)
    matched, label = _filter_events_by_query(events, interest)

    from app.discovery_route import activity_previews_from_events

    draft["suggestions"] = _INTEREST_SUGGESTIONS
    session_ctx["browse_draft"] = draft
    session_ctx["activity_browse_active"] = True
    session_ctx["activity_previews"] = activity_previews_from_events(matched)
    session_ctx["routing_phase"] = "listening"
    return _format_browse_message(matched, label, phone_verified=phone_verified)
