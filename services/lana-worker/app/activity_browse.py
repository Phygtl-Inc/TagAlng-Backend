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
    r"\bshow (?:my )?(?:(?:block|neighborhood) log|intros)\b|"
    r"\bmy (?:block|neighborhood) log\b|\blog\s?out\b|\bsign out\b",
    re.IGNORECASE,
)


def reset_activity_browse_state(session_ctx: dict[str, Any]) -> None:
    """Drop the browse flow + its state so the turn falls through to normal routing.
    Keys set to None (not popped) so the {**old, **new} session merge clears them."""
    for k in ("activity_browse_active", "browse_draft", "activity_previews", "browse_skip_seed"):
        session_ctx[k] = None
    session_ctx["browse_turns"] = 0


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
    #   • the "want me to listen for you?" seek offer (shown when a search came up empty): only
    #     its KNOWN pills (accept / widen) are claimed unconditionally — "yes, listen for me"
    #     reads as a foreign meet_seek to the classifier and must not release. Anything else
    #     falls through to the normal off-lane check: a fresh kind ("what about cricket") reads
    #     native/vague and stays as a re-search, while a self-description ("I like badminton")
    #     is a confident identity.add_claim and RELEASES to the profile brain — it must never
    #     be swallowed as a search and answered with another "No badminton activities…".
    # A genuine pivot/abandon/cancel already released upstream (lane_should_continue) before us.
    draft = session_ctx.get("browse_draft")
    if isinstance(draft, dict):
        if draft.get("_asked") and not str(draft.get("interest") or "").strip():
            return True
        if draft.get("_seek_offer") and (
            _ACCEPT_SEEK_RE.search(message) or _WIDEN_RE.search(message)
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

    from app.event_publish import event_tz

    # Event-local "today", not the server clock — late-evening ET the server (UTC) is
    # already on tomorrow, which shifts every relative-date match by a day.
    return datetime.now(event_tz()).strftime("%Y-%m-%d (%A)")


def _count_upcoming_events_anywhere() -> int | None:
    """Best-effort count of upcoming open events across ALL blocks — lets the ZIP ask say
    honestly that neighbors ARE hosting things, we just don't know the user's block yet."""
    try:
        from datetime import datetime

        from app.auth import service_client

        now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
        res = (
            service_client()
            .table("events")
            .select("id", count="exact")
            .eq("status", "open")
            .gte("starts_at", now_iso)
            .limit(1)
            .execute()
        )
        return int(res.count) if res.count is not None else None
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception("activity_browse_count_events_failed")
        return None


def _compose_zip_ask(interest: str, *, user_reply: str = "", lang: str | None = None) -> str:
    """AI-authored ask for the user's ZIP (Lana's voice), not a canned template.

    Grounded ONLY in what's true: the user's own ask (interest) and the real count of
    upcoming activities across blocks — never invents nearby events (we don't know where
    'nearby' is yet, and events are block-scoped, so listing other blocks' activities
    would show things the user can't attend). When the user replied without a ZIP (maybe
    hesitant), their reply is acknowledged instead of robotically repeating the ask.
    Falls back to a plain friendly ask (localized) when no LLM is configured."""
    interest = str(interest or "").strip()
    fallback = t("browse.ask_zip", lang)
    try:
        from app.orchestrator.llm import llm_configured, llm_json, synthesizer_model

        if not llm_configured():
            return fallback
        count = _count_upcoming_events_anywhere()
        facts = [
            f"The user is looking for: {interest or '(anything nearby)'}",
            (
                f"Neighbors have {count} upcoming activities across TagAlng blocks right now"
                if count
                else "You don't know yet how many activities are coming up"
            ),
            "You don't know the user's block yet; activities are grouped per neighborhood block",
            "You only need a 5-digit US ZIP code — never a street address",
        ]
        if user_reply:
            facts.append(
                f'You already asked for the ZIP and the user replied: "{user_reply[:200]}" '
                "(not a ZIP — maybe hesitant). Acknowledge their reply and gently explain "
                "why you need the ZIP; do not repeat your previous ask verbatim."
            )
        from app.i18n import synth_language_directive

        lang_line = synth_language_directive(lang) if lang else None
        data = llm_json(
            model=synthesizer_model(),
            system=(
                "You are Lana, a warm neighborhood concierge. Write ONE short chat message "
                "(max 2 sentences) asking for the user's ZIP code so you can show the "
                "activities on their block. Ground it ONLY in the facts given — never "
                "invent events or claim something is near them. "
                + (f"{lang_line} " if lang_line else "")
                + 'Return JSON {"message": "..."}.'
            ),
            user_payload="\n".join(f"- {f}" for f in facts),
            max_tokens=120,
            temperature=0.4,
        )
        msg = str((data or {}).get("message") or "").strip() if isinstance(data, dict) else ""
        return msg or fallback
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception("activity_browse_zip_ask_failed")
        return fallback


def _compose_out_of_coverage(zip5: str, *, user_msg: str = "", lang: str | None = None) -> str:
    """AI-authored out-of-coverage reply (Lana's voice) — the honest state for a real ZIP
    Lana doesn't serve yet. Grounded ONLY in what's true: the area was just saved as
    expansion demand and the user can opt into a launch text. NEVER 'try another ZIP'
    (they don't have one) and never invented nearby events. Localized static fallback."""
    zip5 = str(zip5 or "").strip()
    fallback = t("zip.out_of_coverage", lang, zip=zip5)
    try:
        from app.orchestrator.llm import llm_configured, llm_json, synthesizer_model

        if not llm_configured():
            return fallback
        facts = [
            f"You (Lana) are not live around ZIP {zip5} yet — no blocks or activities exist there",
            "The user is one of the first from that area; their ZIP was just saved so the team knows where to open next",
            "You can text them the moment you go live there IF they opt in (they can tap 'Yes, text me at launch')",
            "Never tell them to try a different ZIP, and never invent events near them",
        ]
        if user_msg:
            facts.append(f'What the user said: "{str(user_msg)[:200]}"')
        from app.i18n import synth_language_directive

        lang_line = synth_language_directive(lang) if lang else None
        data = llm_json(
            model=synthesizer_model(),
            system=(
                "You are Lana, a warm neighborhood concierge. Write ONE short chat message "
                "(max 2 sentences) telling the user you're not in their area yet, that "
                "their spot is saved, and offering to text them the moment you launch "
                "there. Ground it ONLY in the facts given. "
                + (f"{lang_line} " if lang_line else "")
                + 'Return JSON {"message": "..."}.'
            ),
            user_payload="\n".join(f"- {f}" for f in facts),
            max_tokens=120,
            temperature=0.4,
        )
        msg_out = str((data or {}).get("message") or "").strip() if isinstance(data, dict) else ""
        return msg_out or fallback
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception("activity_browse_out_of_coverage_failed")
        return fallback


def _compose_empty_seek_offer(
    interest: str, *, user_msg: str = "", lang: str | None = None
) -> str:
    """AI-authored "search came up empty" reply (Lana's voice), not a canned template.

    Grounded ONLY in what's true: the user's own words, the kind searched, and that zero
    matching activities exist on THEIR block right now — never invented events. The two
    real options (keep listening + text them / widen the search) must both survive in the
    copy because the pills under it say exactly that. Localized static fallback."""
    interest = str(interest or "").strip()
    fallback = (
        t("browse.empty_interest_offer", lang, interest=interest)
        if interest
        else t("browse.empty_generic_offer", lang)
    )
    try:
        from app.orchestrator.llm import llm_configured, llm_json, synthesizer_model

        if not llm_configured():
            return fallback
        facts = [
            (
                f"You searched their block for: {interest}"
                if interest
                else "You searched their block for upcoming activities"
            ),
            "Zero matching activities exist on their block right now — that is the honest state",
            "Option A: you can keep an ear out and TEXT them the moment a matching one pops up "
            "(the pill under your message says 'Yes, listen for me')",
            "Option B: they can widen the search to everything on the block "
            "(the pill says 'Widen the search')",
            "Never invent or promise events, and never claim something is happening nearby",
        ]
        if user_msg:
            facts.append(
                f'What the user actually said: "{str(user_msg)[:200]}" — acknowledge their '
                "words naturally (if they shared a taste or excitement, react to it warmly "
                "first) instead of a robotic no-results template."
            )
        from app.i18n import synth_language_directive

        lang_line = synth_language_directive(lang) if lang else None
        data = llm_json(
            model=synthesizer_model(),
            system=(
                "You are Lana, a warm neighborhood concierge. Write ONE short chat message "
                "(max 2 sentences) telling the user nothing matched on their block right now "
                "and offering both options: you keep listening and text them when one pops up, "
                "or widen the search. Ground it ONLY in the facts given. "
                + (f"{lang_line} " if lang_line else "")
                + 'Return JSON {"message": "..."}.'
            ),
            user_payload="\n".join(f"- {f}" for f in facts),
            max_tokens=140,
            temperature=0.4,
        )
        msg_out = str((data or {}).get("message") or "").strip() if isinstance(data, dict) else ""
        return msg_out or fallback
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception("activity_browse_empty_offer_failed")
        return fallback


def _event_when_parts(raw: Any, *, has_time: bool = True) -> str:
    """'2026-07-05 Sat 7:00 PM' for the LLM — in the EVENT's local timezone, so it can
    match date/timeframe AND time-of-day queries. starts_at is stored UTC; rendering it
    raw would put an 8:30 PM ET event on the wrong day (00:30 UTC next-day) and hide
    the clock time the user's 'evenings after 6pm' must be checked against.
    has_time=False (date-only event, midnight placeholder — #56) omits the clock so
    the model never matches 'mornings'/'after 6pm' against a time nobody set."""
    from datetime import datetime, timezone

    from app.event_publish import event_tz

    s = str(raw or "").strip()
    if not s:
        return ""
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone(event_tz())
        if not has_time:
            return f"{local.strftime('%Y-%m-%d %a')} (no time set)"
        clock = local.strftime("%I:%M %p").lstrip("0")
        return f"{local.strftime('%Y-%m-%d %a')} {clock}"
    except ValueError:
        return s[:10]


def _filter_events_by_query(
    events: list[dict[str, Any]], query: str
) -> tuple[list[dict[str, Any]], str]:
    """Parse + match the user's request against the block's events with ONE LLM call.

    The request can name a topic ('cricket', 'family'), a date/timeframe ('on July 5',
    'this weekend', 'tomorrow'), a time of day ('mornings', 'after 6pm'), and/or a host
    ('hosted by Asjid') — in any phrasing. The model gets each event's title, tags, date,
    weekday, local start time and host, plus today's date, and returns the events
    matching ALL constraints the request expresses, with a short label for the header.
    Open/vague requests return everything. Returns (matched, label).
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
                when = _event_when_parts(
                    ev.get("starts_at"), has_time=ev.get("has_time") is not False
                )
                host = str(ev.get("host_name") or "").strip()
                lines.append(
                    f"{i}: {ev.get('title', '')} | date: {when} | "
                    f"host: {host or '?'} | tags: {tagstr}"
                )
            data = llm_json(
                model=router_model(),
                system=(
                    "You filter a neighborhood's upcoming events by a resident's request. "
                    f"Today is {_today_str()}. Each event's date and start time are already "
                    "in the neighborhood's local timezone. The request may mention a TOPIC "
                    "— match it semantically: 'soccer' or 'sports' fits a 'FIFA watch "
                    "party'; 'family' fits a 'park playdate' — a DATE or TIMEFRAME ('on "
                    "July 5', 'this weekend', 'tomorrow' — resolve relative to today), a "
                    "TIME OF DAY ('mornings', 'evenings', 'after 6pm' — check the event's "
                    "start time: morning is before 12 PM, afternoon 12–5 PM, evening/night "
                    "5 PM onward), and/or a HOST name ('hosted by Asjid' → match the host "
                    "field, case-insensitive). Return "
                    'JSON {"match_indices":[ints], "label":"short phrase"}: indices of '
                    "events satisfying EVERY constraint the request expresses (a date query "
                    "must match the event's date; a time-of-day query the start time; a "
                    "host query the host). label "
                    "is a short human phrase naming the filter in the REQUEST'S OWN WORDS "
                    "('FIFA' for 'show me FIFA events'; a resolved date like 'July 5'; "
                    "'hosted by Asjid') or \"\" if the request is open/unfiltered. Never "
                    "label with a topic word the request itself doesn't use — not from "
                    "these instructions, not from the event list. If open, return all "
                    "indices. Empty match_indices if nothing fits."
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


def _refine_suggestions(events: list[dict[str, Any]]) -> list[str]:
    """Refine chips drawn from the SHOWN events' real cohort tags — grounded in what's
    actually on the block, not a hardcoded category list. Empty when events carry no tags
    (the reply already invites 'tell me to narrow it')."""
    out: list[str] = []
    seen: set[str] = set()
    for ev in events[:5]:
        tags = ev.get("cohort_tags")
        for tag in tags if isinstance(tags, list) else []:
            # Tags are stored machine-style ("kids_led_activity") — humanize for the pill.
            t = re.sub(r"[_-]+", " ", str(tag).strip()).strip()
            if t and t.lower() not in seen:
                seen.add(t.lower())
                out.append(t[:1].upper() + t[1:])
        if len(out) >= 4:
            break
    return out[:4]


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
    # The FE renders these same events as a card list (activity_previews) right under this
    # message — a short lead-in is enough; enumerating them in text too reads as a bug.
    head = (
        t("browse.events_header_label", lang, label=label)
        if label
        else t("browse.events_header", lang)
    )
    tail = (
        t("browse.events_tail_verified", lang)
        if phone_verified
        else t("browse.events_tail_guest", lang)
    )
    return f"{head} {tail}"


def run_activity_browse_turn(
    *,
    user_message: str,
    session_ctx: dict[str, Any],
    history: list[dict[str, Any]],
    user_jwt: str,
    home_block_id: str | None,
    slots: dict[str, Any] | None = None,
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
        from app.reply_compose import compose_reply

        reset_activity_browse_state(session_ctx)
        session_ctx["routing_phase"] = "listening"
        return compose_reply(
            goal=(
                "The user cancelled the activity browse (or it ran long). Close "
                "warmly with zero pressure and invite them to ask for anything else."
            ),
            fallback="No problem — we can look another time. What else can I help with?",
            cache=True,
        )

    # ── Seed turn: the "A meet or playgroup" CTA entered with a generic payload; don't mine
    #    it as an interest — drop it so P1 asks fresh (mirrors look_meet_skip_seed). ──
    if session_ctx.get("browse_skip_seed"):
        session_ctx["browse_skip_seed"] = False
        msg = ""

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
            draft["_asked"] = True  # widening means show all — never re-ask P1
            draft["_seek_offer"] = None
            msg = ""
        else:
            # Not an accept/widen tap — treat it as a fresh kind to search for.
            draft["_seek_offer"] = None

    # ── P1: ask the interest ONCE (with chips) — only when there's nothing to mine (the
    #    CTA's generic seed was dropped above, leaving msg empty). A natural-language entry
    #    ("any fifa activities for my 6 year old?") IS the interest — it falls through and
    #    is searched immediately instead of being discarded for a generic re-ask. ──
    if not msg and not draft.get("interest") and not draft.get("_asked"):
        draft["_asked"] = True
        draft["suggestions"] = _INTEREST_SUGGESTIONS
        session_ctx["browse_draft"] = draft
        session_ctx["activity_browse_active"] = True
        session_ctx["routing_phase"] = "listening"
        return t("browse.ask_interest", lang)

    from app.discovery_route import (
        ZIP_INVALID,
        extract_zip,
        note_zip_out_of_coverage,
        resolve_block_id,
        resolve_zip_coverage,
    )

    def _zip_from_turn(*, asked: bool = False) -> str | None:
        # A ZIP mentioned anywhere in the message ("I'm in NYC, zip 10025 — anything this
        # week?") counts as the answer — never re-ask for what the user already said. The
        # AI slot is the authority on whether a number IS a ZIP (a price, a count, a house
        # number are not); bare digits are only trusted when we explicitly asked for the
        # ZIP, where a 5-digit reply is unambiguous.
        zip5 = extract_zip(str((slots or {}).get("zip") or ""))
        if not zip5 and asked:
            zip5 = extract_zip(msg)
        return zip5

    def _set_preview_block(zip5: str, block: dict[str, Any]) -> str:
        bid = str(block.get("block_id") or "")
        session_ctx["preview_block_id"] = bid
        session_ctx["preview_zip"] = zip5
        session_ctx["preview_block_label"] = str(
            block.get("display_name") or block.get("label") or block.get("name") or zip5
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
        # The answer we're waiting for is a ZIP — interest chips only make sense when the
        # interest itself is still unknown.
        draft["suggestions"] = [] if str(draft.get("interest") or "").strip() else _INTEREST_SUGGESTIONS
        session_ctx["browse_draft"] = draft
        session_ctx["activity_browse_active"] = True
        session_ctx["routing_phase"] = "listening"
        return prompt

    def _offer_expansion(zip5: str) -> str:
        # Out-of-coverage state: the ZIP is real (or can't be disproven) — capture it for
        # expansion (pending_zip + feature_requests) and offer the launch text instead of
        # rejecting. The accept/decline reply is read next turn (_expansion_offer).
        note_zip_out_of_coverage(
            zip5=zip5, session_ctx=session_ctx, user_id=user_id, user_message=msg
        )
        draft["_expansion_offer"] = zip5
        draft["_need_zip"] = None
        draft["suggestions"] = ["Yes, text me at launch", "No thanks"]
        session_ctx["browse_draft"] = draft
        session_ctx["activity_browse_active"] = True
        session_ctx["routing_phase"] = "listening"
        return _compose_out_of_coverage(zip5, user_msg=msg, lang=lang)

    # ── Reply to the "text me when I arrive?" launch offer (out-of-coverage ZIP). A fresh
    #    ZIP in the reply re-resolves ("oh — my sister's block is 32827"); yes → the same
    #    verify gate guests already use (verifying attaches a phone to the logged demand);
    #    anything else → warm close, never a re-ask. ──
    if draft.get("_expansion_offer"):
        offered_zip = str(draft.get("_expansion_offer") or "")
        zip_new = _zip_from_turn(asked=True)
        if zip_new and zip_new != offered_zip:
            draft["_expansion_offer"] = None
            draft["_need_zip"] = True  # consume the fresh ZIP below as a ZIP answer
        elif _ACCEPT_SEEK_RE.search(msg):
            reset_activity_browse_state(session_ctx)
            if not phone_verified:
                session_ctx["requires_phone_verification"] = True
                session_ctx["routing_phase"] = "await_signup_phone"
                return t("zip.expansion_verify_gate", lang)
            session_ctx["routing_phase"] = "listening"
            return t("zip.expansion_saved", lang, zip=offered_zip)
        else:
            reset_activity_browse_state(session_ctx)
            session_ctx["routing_phase"] = "listening"
            return t("zip.expansion_close", lang)

    # ── If we asked for a ZIP last turn, this message is the ZIP (don't treat it as the
    #    interest). Otherwise the message is the interest (first answer) or a refinement. ──
    if draft.get("_need_zip"):
        zip5 = _zip_from_turn(asked=True)
        if not zip5:
            # No ZIP in the reply — maybe a question or a decline. Let the AI answer it
            # in context instead of repeating the same canned line.
            return _ask_zip(
                _compose_zip_ask(
                    str(draft.get("interest") or ""), user_reply=msg, lang=lang
                )
            )
        # Create-on-miss (same as the discovery funnel): an uncovered-but-real ZIP gets a
        # waitlist block; only a geocoder-confirmed fake ZIP earns a re-check ask.
        block, status = resolve_zip_coverage(user_jwt, zip5)
        if status == ZIP_INVALID:
            return _ask_zip(t("discovery.zip_unplaceable", lang, zip=zip5))
        if not block:
            return _offer_expansion(zip5)
        _set_preview_block(zip5, block)
        draft["_need_zip"] = None
    elif msg:
        # A tapped concierge chip ("See badminton events") dispatches with a model-authored
        # send text that can come out generic ("show me what's happening this weekend") —
        # the offer's structured topic is the committed subject, so it REPLACES the send
        # text outright (filter, weekend pre-narrow, everything downstream).
        if str((slots or {}).get("_forced_kind") or "") == "find_activities":
            forced_topic = str((slots or {}).get("signal_detail") or "").strip()
            if forced_topic:
                msg = forced_topic
        draft["interest"] = msg[:80]
    interest = str(draft.get("interest") or "")

    # Resolve the block to read events from — a ZIP given anywhere in this conversation
    # (session preview_block_id, or a pending out-of-coverage ZIP) counts, not just the
    # persisted profile block. Ask in-flow rather than dead-ending when none is known.
    block_id = resolve_block_id(session_ctx, home_block_id)
    if not block_id:
        zip5 = (
            _zip_from_turn()
            or session_ctx.get("preview_zip")
            or session_ctx.get("pending_zip")
        )
        if zip5:
            block, status = resolve_zip_coverage(user_jwt, str(zip5))
            if block:
                block_id = _set_preview_block(str(zip5), block)
            elif status == ZIP_INVALID:
                # The user DID give a ZIP — it just isn't real. Say that, never re-ask
                # as if none was given.
                return _ask_zip(t("discovery.zip_unplaceable", lang, zip=zip5))
            else:
                return _offer_expansion(str(zip5))
        if not block_id:
            return _ask_zip(_compose_zip_ask(interest, lang=lang))

    # "weekend" is handled by the LLM date matcher too, but keep the SQL-side weekend
    # filter as a cheap pre-narrow when the word appears verbatim.
    weekend_only = bool(re.search(r"\bweekend\b", interest, re.I) or re.search(r"\bweekend\b", msg, re.I))
    events = _fetch_block_events(user_jwt, block_id, weekend_only=weekend_only)
    matched, label = _filter_events_by_query(events, interest)

    from app.discovery_route import activity_previews_from_events

    # Search-first fallback: a concrete search that found nothing → offer the seek (listen and
    # text them when a matching meet appears) rather than dead-ending. The accept/widen reply
    # is read next turn. No interest (a "show me anything" browse) keeps the generic message.
    if not matched and interest:
        # Echo (and store) the filter's short label, not the raw sentence — a full NL entry
        # ("are there any fifa activities for my 6 year old") would otherwise be parroted
        # here and become the saved seek's kind on accept. A zero-event block never reaches
        # the LLM filter (no label), so a raw interest is echoed only when it's chip-short.
        short = (label or "").strip() or (interest if len(interest.split()) <= 4 else "")
        draft["interest"] = short or interest
        draft["_seek_offer"] = True
        draft["suggestions"] = ["Yes, listen for me", "Widen the search"]
        session_ctx["browse_draft"] = draft
        session_ctx["activity_browse_active"] = True
        session_ctx["activity_previews"] = []
        session_ctx["routing_phase"] = "listening"
        return _compose_empty_seek_offer(short, user_msg=msg, lang=lang)

    draft["_seek_offer"] = None
    draft["suggestions"] = _refine_suggestions(matched)
    session_ctx["browse_draft"] = draft
    session_ctx["activity_browse_active"] = True
    session_ctx["activity_previews"] = activity_previews_from_events(matched)
    session_ctx["routing_phase"] = "listening"
    return _format_browse_message(matched, label, phone_verified=phone_verified, lang=lang)
