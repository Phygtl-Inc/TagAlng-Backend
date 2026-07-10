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

from app.i18n import session_lang, t

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
    for k in ("activity_browse_active", "browse_draft", "activity_previews", "browse_skip_seed"):
        session_ctx[k] = None
    session_ctx["browse_turns"] = 0


# The "A meet or playgroup" CTA's generic seed payloads — a tap, not typed content.
_BARE_ENTRY_PHRASES = frozenset({
    "a meet or playgroup",
    "a meet or a playgroup",
    "looking for a meet or playgroup",
    "i'm looking for a meet or playgroup",
    "im looking for a meet or playgroup",
})

# Signals that a short message SAYS something (a want, a question, a feeling) rather than
# naming a category — any of these means "classify it, don't can it".
_CONTENT_TOKEN_RE = re.compile(
    r"\b(?:i|me|my|we|our|you|need|want|find|show|help|know|have|get|do|does|is|are|"
    r"can|how|what|why|who|where|when|please)\b|\?",
    re.IGNORECASE,
)

_ZIP_ONLY_RE = re.compile(r"^\d{5}(?:-\d{4})?$")


def looks_like_bare_look_meet_entry(message: str) -> bool:
    """Is the "A meet or playgroup" CTA turn a BARE chip tap (the chip's seed payload, a
    category label, or a ZIP) with no semantic content of its own?

    Only those keep the fast canned entry (enter_activity_browse_from_cta). Free text that
    says anything real — "I need a babysitter for tonight", a safety worry, a long
    emotional message — must instead reach the AI intent classifier, never the canned
    "what kind of thing are you up for?" opener. Deterministic on purpose: no LLM cost on
    a chip tap."""
    msg = str(message or "").strip().lower().rstrip(".!")
    if not msg:
        return True
    if msg in _BARE_ENTRY_PHRASES:
        return True
    if _ZIP_ONLY_RE.match(msg):
        return True
    # Short label-like taps ("Family & kids", "Stroller walk") with no sentence signals.
    return len(msg.split()) <= 3 and not _CONTENT_TOKEN_RE.search(msg)


def enter_activity_browse_from_cta(session_ctx: dict[str, Any], message: str) -> bool:
    """Deterministic entry for the "A meet or playgroup" CTA (intent_hint="look_meet").

    A bare tap arms the sticky browse flow exactly as before (skip the generic seed so P1
    asks fresh) and returns True. A message carrying real semantic content is NOT captured:
    returns False and leaves session_ctx untouched, so the turn falls through to normal
    routing (handle_discovery_turn → layer-1 classification) and out-of-scope / unsafe /
    emotional turns reach their real handlers instead of the canned opener. See main.py."""
    if not looks_like_bare_look_meet_entry(message):
        return False
    session_ctx["activity_browse_active"] = True
    session_ctx["browse_turns"] = 0
    session_ctx["browse_draft"] = None
    # Button entry carries a generic seed phrase with no real interest — skip mining it so
    # the flow asks P1 ("what kind of meet?") and never releases on the seed turn (the
    # classifier mis-reads the generic payload as meet_seek). Consumed next turn.
    session_ctx["browse_skip_seed"] = True
    return True


# The "yes, listen for me" acceptance of the seek fallback offered when a search comes up
# empty (search-first model: looking for a meet ≡ searching activities; the seek to be
# matched is the fallback). "widen" broadens the search instead.
_ACCEPT_SEEK_RE = re.compile(
    r"\b(yes|yeah|yep|yup|sure|ok(?:ay)?|please|do it|go ahead|sounds good|listen|"
    r"text me|notify me|let me know)\b",
    re.IGNORECASE,
)
_WIDEN_RE = re.compile(
    r"\b(widen|broaden|wider|show all|everything|anything else|other|different|"
    r"expand)\b",
    re.IGNORECASE,
)

# Replies to the out-of-coverage waitlist offer ("want me to text you the day we
# arrive?"). Decline is checked FIRST — "no thanks, keep looking" must not read as an
# accept via its stray "ok"-ish words.
_WAITLIST_ACCEPT_RE = re.compile(
    r"\b(yes|yeah|yep|yup|sure|ok(?:ay)?|please|join|sign me up|wait\s?list|"
    r"text me|notify me|let me know|count me in)\b",
    re.IGNORECASE,
)
_KEEP_LOOKING_RE = re.compile(
    r"\b(keep look(?:ing)?|look(?:ing)? around|no thanks|no thank you|nah|not now|"
    r"maybe later|later)\b",
    re.IGNORECASE,
)


# Lanes this browse does NOT own — a confident classification into any of these is a
# pivot, never a browse refinement. Note meet_seek/looking.meet ARE foreign here so the
# user can switch from browsing events to being matched (handled by look_meet); plain
# "activities" / find_activities is THIS lane = a re-filter, so it is NOT foreign.
# What THIS lane owns: browsing/finding existing activities on the block. Everything else
# (find people, a meet to be matched, host, swap, tip, out_of_scope, unsafe, auth, …) is
# off-lane and releases — we never enumerate that open-ended set (see is_confident_off_lane).
_NATIVE_GOALS = frozenset({"activities"})
_NATIVE_LINEARS = frozenset({"discovery.find_activities", "discovery.find_in_block"})
_NATIVE_SIGNALS: frozenset[str] = frozenset()


def _is_browse_answer(
    message: str, session_ctx: dict[str, Any], slots: dict[str, Any] | None
) -> bool:
    """Is this turn an answer/refine for the browse? A re-filter ("show me cricket instead",
    "anything outdoors") or a vague reply is — only a confident pivot to another lane, a
    meta/question turn ("what's my zip?"), or an out_of_scope/unsafe turn releases."""
    from app.lane_decision import is_confident_off_lane, is_meta_or_chat
    from app.layer1_intents import utterance_indicates_tip_seek

    # Deterministic backstop to the AI classifier: an explicit request for a standing
    # PLACE/venue/service recommendation ("find me restaurants", "know a good pizza place")
    # is a tip_seek PIVOT, never a browse refinement — the browse reads time-bound EVENTS
    # and a place is not an event. Release so routing hands it to the tip_seek → Google
    # Places path. Bare activity/topic refinements never trip this (the regex needs a
    # service/place noun), so a genuine "cricket"/"outdoors" refine still stays. Checked FIRST
    # so an explicit place request still escapes even while we're awaiting the interest.
    if utterance_indicates_tip_seek(message):
        return False
    # A reply we EXPLICITLY asked for belongs to THIS flow — never release on it (that was the
    # "Social" loop: the P1 answer read as a foreign meet_seek and released, so the browse-vs-meet
    # clarifier re-asked forever). Two pending prompts:
    #   • P1 "what kind of thing are you up for?" (offered chips like "Social"/"Outdoors"): the
    #     next reply IS the interest — stay and capture it, even if a bare topic reads off-lane.
    #   • the "want me to listen for you?" seek offer (shown when a search came up empty): its
    #     reply (yes / widen / a new kind) is interpreted by the turn.
    # A genuine pivot/abandon/cancel already released upstream (lane_should_continue) before us.
    #   • the out-of-coverage waitlist offer: its reply (join / keep looking / another ZIP)
    #     is interpreted by the turn, same as the seek offer.
    draft = session_ctx.get("browse_draft")
    if isinstance(draft, dict) and (
        draft.get("_seek_offer")
        or draft.get("_coverage_offer")
        or (draft.get("_asked") and not str(draft.get("interest") or "").strip())
    ):
        return True
    if is_meta_or_chat(slots):
        return False
    return not is_confident_off_lane(
        slots,
        native_goals=_NATIVE_GOALS,
        native_linears=_NATIVE_LINEARS,
        native_signals=_NATIVE_SIGNALS,
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

    # Seed turn: the "A meet or playgroup" CTA enters this flow with a generic payload
    # ("I'm looking for a meet or playgroup"), which the classifier mis-reads as meet_seek
    # (foreign here) and would release on entry — the same seed-turn trap look_meet has. The
    # button is an explicit choice of the search lane; never release on the seed turn. The
    # flag is consumed in run_activity_browse_turn, so later turns re-decide as normal.
    if session_ctx.get("browse_skip_seed"):
        return False

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
    events: list[dict[str, Any]],
    label: str | None,
    *,
    phone_verified: bool,
    lang: str | None = None,
) -> str:
    label = (label or "").strip() or None
    if not events:
        if label:
            return t("browse.events_empty_label", lang, label=label)
        return t("browse.events_empty", lang)
    from app.discovery_route import _format_event_when

    head = (
        t("browse.events_header_label", lang, label=label)
        if label
        else t("browse.events_header", lang)
    )
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
        t("browse.events_tail_verified", lang)
        if phone_verified
        else t("browse.events_tail_guest", lang)
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
    user_id: str | None = None,
) -> str:
    """Drive one browse turn. Mutates session_ctx (browse_draft, activity_browse_active,
    activity_previews, routing_phase). Returns Lana's reply."""
    msg = str(user_message or "").strip()
    draft: dict[str, Any] = dict(session_ctx.get("browse_draft") or {})
    phone_verified = bool(session_ctx.get("phone_verified"))
    lang = session_lang(session_ctx)

    turns = int(session_ctx.get("browse_turns") or 0) + 1
    session_ctx["browse_turns"] = turns
    if _CANCEL_RE.search(msg) or turns > _BROWSE_TURN_CAP:
        reset_activity_browse_state(session_ctx)
        session_ctx["routing_phase"] = "listening"
        return "No problem — we can look another time. What else can I help with?"

    # ── Seed turn: the "A meet or playgroup" CTA entered with a generic payload; don't mine
    #    it as an interest — drop it so P1 asks fresh (mirrors look_meet_skip_seed). ──
    if session_ctx.get("browse_skip_seed"):
        session_ctx["browse_skip_seed"] = False
        msg = ""

    # ── Reply to the out-of-coverage waitlist offer ("text you the day we arrive?"). The
    #    ZIP she gave is already persisted on the session (coverage_zip) — it is NEVER
    #    re-asked. Join → store the waitlist row; a new ZIP → try that one; keep looking /
    #    anything else → release gracefully with the door left open. ──
    if draft.get("_coverage_offer"):
        from app.discovery_route import extract_zip

        zip_pending = str(session_ctx.get("coverage_zip") or "").strip()
        new_zip = extract_zip(msg)
        if new_zip and new_zip != zip_pending:
            # Trying another ZIP — route it through the ZIP branch below (which validates
            # and resolves it) so the ZIP message is never mined as an interest.
            draft["_coverage_offer"] = None
            draft["_need_zip"] = True
        elif _KEEP_LOOKING_RE.search(msg):
            reset_activity_browse_state(session_ctx)
            session_ctx["routing_phase"] = "listening"
            return (
                "Of course — I'm still here for questions and planning in the meantime. "
                "What are you in the mood for?"
            )
        elif _WAITLIST_ACCEPT_RE.search(msg):
            from app.db import save_coverage_waitlist

            looking_for = str(draft.get("interest") or "").strip() or None
            saved = save_coverage_waitlist(
                user_id=user_id,
                zip_code=zip_pending,
                looking_for=looking_for,
            )
            reset_activity_browse_state(session_ctx)
            session_ctx["routing_phase"] = "listening"
            if not saved:
                return (
                    "I couldn't save that just now — say 'join the waitlist' in a bit and "
                    "I'll try again."
                )
            session_ctx["coverage_waitlisted"] = True
            bits = [f"You're on the list for {zip_pending}!"]
            if looking_for:
                bits.append(
                    f"The day Lana lands on your block, a {looking_for} search is the "
                    "first thing I'll run for you."
                )
            if phone_verified:
                bits.append("I'll text you the day we arrive.")
            else:
                bits.append(
                    "Verify your email whenever you're ready so I can actually reach you "
                    "the day we arrive."
                )
            return " ".join(bits)
        else:
            # Not a tap, not a new ZIP — don't trap her in the offer.
            reset_activity_browse_state(session_ctx)
            session_ctx["routing_phase"] = "listening"
            return (
                "No worries — say 'join the waitlist' anytime and I'll text you the day "
                "Lana lands on your block. What else can I help with?"
            )

    # ── Reply to the "want me to listen for you?" seek offer (search came up empty). The
    #    reply is a tap on a known pill, so read it by label; the lane release already went
    #    through the AI classifier. Accept → save a minimal seek; widen → drop the filter and
    #    show everything; a new kind → fall through and re-search. ──
    if draft.get("_seek_offer"):
        if _ACCEPT_SEEK_RE.search(msg) and not _WIDEN_RE.search(msg):
            from app.discovery_route import resolve_block_id
            from app.look_meet import start_meet_seek_from_interest

            interest = str(draft.get("interest") or "").strip()
            zip_code = str(session_ctx.get("zip") or session_ctx.get("zip_code") or "").strip() or None
            reply = start_meet_seek_from_interest(
                interest=interest,
                session_ctx=session_ctx,
                user_jwt=user_jwt,
                block_id=resolve_block_id(session_ctx, home_block_id),
                zip_code=zip_code,
            )
            # Hand the lane off: a guest is now in the verify gate, a verified user is saved.
            # Either way the browse flow is done (clears activity_browse_active for next turn).
            reset_activity_browse_state(session_ctx)
            return reply
        if _WIDEN_RE.search(msg):
            draft["interest"] = ""  # clear the filter → show everything below
            draft["_seek_offer"] = None
            msg = ""
        else:
            # Not an accept/widen tap — treat it as a fresh kind to search for.
            draft["_seek_offer"] = None

    # ── P1: ask the interest ONCE (with chips), unless they already named one ──
    if not draft.get("interest") and not draft.get("_asked"):
        draft["_asked"] = True
        draft["suggestions"] = _INTEREST_SUGGESTIONS
        session_ctx["browse_draft"] = draft
        session_ctx["activity_browse_active"] = True
        session_ctx["routing_phase"] = "listening"
        return t("browse.ask_interest", lang)

    from app.discovery_route import (
        extract_zip,
        fetch_blocks_for_zip,
        invalid_zip_hint,
        is_placeholder_zip,
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

    def _out_of_coverage(zip5: str) -> str:
        """Honest out-of-coverage state for a valid ZIP with no block (QA 2026-07-08:
        12/12 metro moms dead-ended). The ZIP she gave is persisted on the session so it
        is NEVER re-asked, and she's never pointed at someone else's ZIP (the old "try
        32827" told a Manhattan mom to try Orlando). Offers the waitlist instead."""
        session_ctx["coverage_zip"] = zip5
        draft["_need_zip"] = None
        if session_ctx.get("coverage_waitlisted"):
            reset_activity_browse_state(session_ctx)
            session_ctx["routing_phase"] = "listening"
            return (
                f"You're already on the waitlist for {zip5} — I'll text you the day Lana "
                "lands on your block. Anything else in the meantime?"
            )
        draft["_coverage_offer"] = True
        draft["suggestions"] = ["Join the waitlist", "Keep looking around"]
        session_ctx["browse_draft"] = draft
        session_ctx["activity_browse_active"] = True
        session_ctx["routing_phase"] = "listening"
        return (
            "We're not on your block yet — Lana's starting in Lake Nona, Orlando. "
            "Want me to text you the day we arrive?"
        )

    # ── If we asked for a ZIP last turn, this message is the ZIP (don't treat it as the
    #    interest). Otherwise the message is the interest (first answer) or a refinement. ──
    if draft.get("_need_zip"):
        zip5 = extract_zip(msg)
        if not zip5:
            # A bogus/short ZIP gets the explanatory hint (e.g. "99999 isn't a real
            # US ZIP") instead of the same bare re-prompt.
            return _ask_zip(invalid_zip_hint(msg) or t("browse.ask_zip_retry", lang))
        if is_placeholder_zip(zip5):
            return _ask_zip(
                f"Hmm, {zip5} doesn't look like a US ZIP — typo? What's your ZIP code?"
            )
        blocks = fetch_blocks_for_zip(user_jwt, zip5)
        if not blocks:
            return _out_of_coverage(zip5)
        _set_preview_block(zip5, blocks)
        draft["_need_zip"] = None
    elif msg:
        draft["interest"] = msg[:80]
    interest = str(draft.get("interest") or "")

    # Resolve the block to read events from — a ZIP given anywhere in this conversation
    # (session preview_block_id, or an unresolved coverage_zip) counts, not just the
    # persisted profile block. Ask for the ZIP in-flow ONLY when none was ever given; a
    # known-but-uncovered ZIP goes to the out-of-coverage state, never a re-ask.
    block_id = resolve_block_id(session_ctx, home_block_id)
    if not block_id:
        zip5 = str(
            extract_zip(msg)
            or session_ctx.get("preview_zip")
            or session_ctx.get("coverage_zip")
            or ""
        ).strip()
        if zip5 and is_placeholder_zip(zip5):
            session_ctx["coverage_zip"] = None
            return _ask_zip(
                f"Hmm, {zip5} doesn't look like a US ZIP — typo? What's your ZIP code?"
            )
        if zip5:
            blocks = fetch_blocks_for_zip(user_jwt, zip5)
            if blocks:
                block_id = _set_preview_block(zip5, blocks)
            else:
                return _out_of_coverage(zip5)
        if not block_id:
            return _ask_zip(t("browse.ask_zip", lang))

    # "weekend" is handled by the LLM date matcher too, but keep the SQL-side weekend
    # filter as a cheap pre-narrow when the word appears verbatim.
    weekend_only = bool(re.search(r"\bweekend\b", interest, re.I) or re.search(r"\bweekend\b", msg, re.I))
    events = _fetch_block_events(user_jwt, block_id, weekend_only=weekend_only)
    matched, label = _filter_events_by_query(events, interest)

    # Hard availability constraints ("evenings after 6 or weekends") always apply —
    # a 10 AM coffee morning must never be offered to an evenings-only mom
    # (QA 2026-07-08). Captured per-turn in main.py; read from session context here.
    from app.constraints import (
        constraints_all_filtered_note,
        filter_events_by_constraints,
    )

    matched, constraint_dropped = filter_events_by_constraints(
        matched, session_ctx.get("user_constraints")
    )

    from app.discovery_route import activity_previews_from_events

    # Everything on the block was OUTSIDE her windows — say so gracefully (never show
    # the excluded events) and offer the seek fallback, instead of the generic
    # "nothing like X" message that ignores WHY nothing fits.
    if not matched and constraint_dropped:
        draft["_seek_offer"] = True
        draft["suggestions"] = ["Yes, listen for me", "Widen the search"]
        session_ctx["browse_draft"] = draft
        session_ctx["activity_browse_active"] = True
        session_ctx["activity_previews"] = []
        session_ctx["routing_phase"] = "listening"
        return constraints_all_filtered_note(session_ctx.get("user_constraints"))

    # Search-first fallback: a concrete search that found nothing → offer the seek (listen and
    # text them when a matching meet appears) rather than dead-ending. The accept/widen reply
    # is read next turn. No interest (a "show me anything" browse) keeps the generic message.
    if not matched and interest:
        draft["_seek_offer"] = True
        draft["suggestions"] = ["Yes, listen for me", "Widen the search"]
        session_ctx["browse_draft"] = draft
        session_ctx["activity_browse_active"] = True
        session_ctx["activity_previews"] = []
        session_ctx["routing_phase"] = "listening"
        return t("browse.empty_interest_offer", lang, interest=interest)

    draft["_seek_offer"] = None
    draft["suggestions"] = _INTEREST_SUGGESTIONS
    session_ctx["browse_draft"] = draft
    session_ctx["activity_browse_active"] = True
    session_ctx["activity_previews"] = activity_previews_from_events(matched)
    session_ctx["routing_phase"] = "listening"
    return _format_browse_message(matched, label, phone_verified=phone_verified, lang=lang)
