"""In-chat "looking for a meet / playgroup" capture (the meet_seek flow), mirroring the
tip / pass-along flows: the LLM does STRUCTURED extraction, the questions are driven in
code so it stays on-script and never loops.

Flow (matches the C-4-look-meet mock):
  P1  "What kind of meet would help?"            (nothing captured yet)
  P2  "Got it — weekday playground meet" + chips (kind / day / trait)
  P3  "Anyone with a similar kid-stage matter?"  (affinity, tappable options)
  P4  assembled card → "Start listening for me" / "Send to a mom"
  →   saved to local_signals (meet_seek); the matcher pairs it with host_meet (phase C
      adds semantic embedding matching so "playground meet" ≈ "park playdate").
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.i18n import session_lang, t

_KIND_SUGGESTIONS = ["Playground meet", "Stroller walk", "Coffee & kids", "Library storytime"]
_AFFINITY_QUESTION = "Anyone with a similar kid-stage matter?"
_AFFINITY_OPTIONS = ["Same kid-stage", "Any toddler parent", "Open to everyone"]
_MAX_ENRICH = 2

_CANCEL_RE = re.compile(
    r"\b(cancel|never\s*mind|nvm|stop|forget it|not now|skip this|exit|quit)\b",
    re.IGNORECASE,
)
# The "Start listening for me" CTA / any go-ahead to post the seek.
_LISTEN_RE = re.compile(
    r"\b(start listening|listen for me|listening for me|post it|that'?s it|"
    r"go ahead|done|sounds good|do it)\b",
    re.IGNORECASE,
)
_LOOK_MEET_TURN_CAP = 12

# NL entry into this flow is owned by the AI classifier (find-activities → look_meet, or
# a stated meet_seek), not a keyword backstop — only the explicit "A meet or playgroup"
# CTA (intent_hint) enters deterministically. See main.py / handle_discovery_turn.


# Explicit pivot to another intent — release the capture so normal routing handles it.
# Needs a find/show verb + a people noun so a meet answer ("all moms") is never a pivot.
_PIVOT_OUT_RE = re.compile(
    r"\b(?:find|show)\s+(?:me\s+)?(?:\w+\s+){0,3}(?:moms?|dads?|parents?|neighbou?rs?|people|families)\b|"
    r"\bshow (?:my )?(?:(?:block|neighborhood) log|intros)\b|"
    r"\bmy (?:block|neighborhood) log\b|\blog\s?out\b|\bsign out\b|"
    r"\b(?:host|create|throw|plan|organi[sz]e)\s+(?:an?\s+|my\s+)?(?:event|party|meetup|gathering)\b",
    re.IGNORECASE,
)


# What THIS meet capture owns: looking for a meet (meet_seek) and browsing activities to
# find one (find_activities is search-first for a meet). Everything else — find people,
# host, swap, tip, out_of_scope, unsafe, auth, … — is off-lane and releases. We list only
# what we own; the open-ended rest is handled generically (see is_confident_off_lane).
_NATIVE_GOALS = frozenset({"activities"})
_NATIVE_LINEARS = frozenset({"discovery.find_activities", "looking.meet"})
_NATIVE_SIGNALS = frozenset({"meet_seek"})


def _is_new_meet_kind(slots: dict[str, Any] | None) -> bool:
    """The classifier reads this turn as a (re)statement of a meet to look for — i.e. a
    CHANGED request once a kind is already captured, so we restart fresh rather than treat
    it as an answer to the current draft."""
    if not slots:
        return False
    from app.layer1_intents import normalize_linear_intent

    goal = str(slots.get("goal") or "")
    linear = normalize_linear_intent(slots.get("linear_intent")) or ""
    signal_intent = str(slots.get("signal_intent") or "")
    return (
        goal == "activities"
        or linear == "discovery.find_activities"
        or (goal == "save_signal" and signal_intent == "meet_seek")
    )


def _is_look_meet_answer(
    message: str, session_ctx: dict[str, Any], slots: dict[str, Any] | None
) -> bool:
    """Is this turn a genuine answer/refine/confirm for the meet capture's current step?"""
    from app.lane_decision import is_confident_off_lane, is_meta_or_chat

    # A question / meta turn ("what's my zip?") is never an answer — release so it's
    # answered, not captured as a meet field.
    if is_meta_or_chat(slots):
        return False
    # A confident pivot to another lane (or an out_of_scope / unsafe turn) is never an
    # answer to a meet question.
    if is_confident_off_lane(
        slots,
        native_goals=_NATIVE_GOALS,
        native_linears=_NATIVE_LINEARS,
        native_signals=_NATIVE_SIGNALS,
    ):
        return False
    # On the ready card, only a confirm CTA or a chip edit counts; anything else is a new
    # request and should release.
    if session_ctx.get("look_ready"):
        return bool(_LISTEN_RE.search(message) or re.match(r"\s*fix:\w+\s*$", message))
    # Mid follow-up (affinity / detail) — any reply answers it.
    if session_ctx.get("look_pending_ask"):
        return True
    draft = session_ctx.get("look_draft")
    kind_set = isinstance(draft, dict) and bool(str(draft.get("kind") or "").strip())
    # P1 (no kind yet) — any reply that isn't a foreign pivot IS the "what kind?" answer.
    if not kind_set:
        return True
    # A kind is captured: a fresh meet statement = a changed request → release & restart;
    # anything else (a day/place/trait detail) refines the current draft → stay.
    return not _is_new_meet_kind(slots)


def look_meet_should_release(
    message: str,
    session_ctx: dict[str, Any],
    slots: dict[str, Any] | None = None,
) -> bool:
    """Whether the sticky meet capture should release this turn and hand back to routing.

    Continue-only-on-match: the AI classifier drives the call (its ``abandon`` flag and
    intent lane); the capture is kept only for a genuine answer / chip edit / confirm CTA
    (``_is_look_meet_answer``) or an explicit cancel (a graceful in-flow exit). Everything
    else — a pivot, a vague switch, or a low-confidence read — releases, so the user is
    never trapped."""
    from app.lane_decision import lane_should_continue

    # Seed turn: the "A meet or playgroup" button just entered this flow and sends a generic
    # payload ("I'm looking for a meet or playgroup"). That phrase is the user's EXPLICIT
    # choice of this lane, not a pivot — and the classifier mis-reads "looking for a meet" as
    # find_peers. Never release on the seed turn; run P1 ("what kind of meet?") first. After
    # this turn the flag is consumed, so every later turn re-decides intent as normal.
    if session_ctx.get("look_meet_skip_seed"):
        return False

    return not lane_should_continue(
        message,
        session_ctx,
        slots,
        is_valid_answer=_is_look_meet_answer,
        pivot_re=_PIVOT_OUT_RE,
    )


def look_meet_user_moved_on(message: str, session_ctx: dict[str, Any]) -> bool:
    """Regex/ready-only release check (no classifier). Retained for callers without AI
    slots; ``look_meet_should_release`` is the AI-driven decision used in the pipeline."""
    return look_meet_should_release(message, session_ctx, slots=None)


def reset_look_meet_state(session_ctx: dict[str, Any]) -> None:
    """Drop the in-progress (unsaved) meet capture and its flags so the turn falls
    through to normal routing. Keys set to None (not popped) so the {**old, **new}
    session merge clears them instead of letting a stale value survive."""
    for k in (
        "look_meet_active",
        "look_draft",
        "look_ready",
        "look_pending_ask",
        "look_enrich_count",
        "look_affinity_asked",
        "look_meet_skip_seed",
    ):
        session_ctx[k] = None
    session_ctx["look_turns"] = 0


def _gate_guest_before_save(session_ctx: dict[str, Any], draft: dict[str, Any]) -> str:
    """A guest tapped 'Start listening' — stash the ready seek and gate into verify.

    Releases the look_meet flow (so the email/OTP turns route to the signup handler) and
    flips on the verify gate. `look_seek_pending` survives the merge and is saved by
    save_pending_meet_seek once the user is verified. The phase literal matches
    PHASE_AWAIT_SIGNUP_PHONE — kept as a string to avoid a circular import.
    """
    session_ctx["look_seek_pending"] = dict(draft)
    for k in ("look_meet_active", "look_ready", "look_pending_ask", "look_affinity_asked"):
        session_ctx[k] = None
    session_ctx["look_turns"] = 0
    session_ctx["requires_phone_verification"] = True
    session_ctx["routing_phase"] = "await_signup_phone"
    return t("meet.verify_gate", session_lang(session_ctx))


def save_pending_meet_seek(
    *,
    session_ctx: dict[str, Any],
    user_jwt: str,
    block_id: str | None,
    zip_code: str | None,
) -> str | None:
    """Save a seek that was stashed when a guest hit 'Start listening', once verified.

    Returns Lana's reply, or None when there is nothing pending. Sets look_meet_saved_now
    so the FE shows the saved card.
    """
    draft = session_ctx.get("look_seek_pending")
    if not isinstance(draft, dict) or not draft:
        return None
    session_ctx["look_seek_pending"] = None
    saved = _save_meet_seek(draft=draft, user_jwt=user_jwt, block_id=block_id, zip_code=zip_code)
    if not saved:
        return "You're verified! I couldn't save your listing just now — say 'start listening' to retry."
    draft["signal_id"] = saved.get("signal_id")
    draft["saved"] = True
    draft["chips"] = _build_chips(draft)
    session_ctx["look_draft"] = draft
    session_ctx["look_meet_saved_now"] = True
    matches = int(saved.get("matches_created") or 0)
    tail = (
        f" {matches} neighbor{'s' if matches != 1 else ''} wanting the same just matched!"
        if matches
        else " I'll text you when another neighbor wants the same near you."
    )
    return f"✅ You're in — I'm listening for a **{_summary(draft)}**.{tail}"


def start_meet_seek_from_interest(
    *,
    interest: str,
    session_ctx: dict[str, Any],
    user_jwt: str,
    block_id: str | None,
    zip_code: str | None,
) -> str:
    """Save a MINIMAL meet seek built from a single interest phrase — the search-first
    fallback: "looking for a meet" searched the block's activities, found none, and the user
    said "listen for me". Guests are gated into verify (the seek auto-saves post-verify via
    save_pending_meet_seek + the existing-account login recovery); verified users save now.
    Returns Lana's reply. Sets look_draft + look_meet_saved_now so the FE shows the card.

    Callers (e.g. activity_browse) must reset their own lane flags after this — the guest
    gate only releases the look_meet flags, not theirs.
    """
    draft: dict[str, Any] = {"kind": str(interest or "").strip()[:80] or "a meet"}
    if not session_ctx.get("phone_verified"):
        return _gate_guest_before_save(session_ctx, draft)
    saved = _save_meet_seek(draft=draft, user_jwt=user_jwt, block_id=block_id, zip_code=zip_code)
    if not saved:
        return "I couldn't set that up just now — say 'listen for me' to retry."
    draft["signal_id"] = saved.get("signal_id")
    draft["saved"] = True
    draft["chips"] = _build_chips(draft)
    session_ctx["look_draft"] = draft
    session_ctx["look_meet_saved_now"] = True
    matches = int(saved.get("matches_created") or 0)
    tail = (
        f" {matches} neighbor{'s' if matches != 1 else ''} wanting the same just matched!"
        if matches
        else " I'll text you when a neighbor wants the same near you."
    )
    return f"✅ You're in — I'm listening for a **{_summary(draft)}**.{tail}"


_MEET_VALUE_FIELDS = ("kind", "day", "place", "trait")

_EXTRACT_SYSTEM = """You extract structured fields about a kind of MEET-UP / playgroup a \
neighbor is LOOKING FOR (not hosting), and propose ONE smart follow-up question.

Return ONE compact JSON object with exactly these keys:
{"kind","day","place","trait","ask"}

- kind: the type of meet they want, e.g. "playground meet","stroller walk","library storytime","coffee & kids". null if not stated.
- day: a day/time preference, e.g. "weekday","weekend","mornings","Saturday". null if not stated.
- place: a place preference if mentioned, e.g. "the park","Lake Nona". null otherwise.
- trait: a standout preference about the meet, e.g. "stroller-friendly","with other moms","toddler-paced". null if not stated.
- ask: the single MOST useful follow-up to sharpen the match, TAILORED to what's unknown,
  with 2-4 tappable answers that fit. Shape: {"field": <snake_case>, "question": <one short question>, "options":[...]}.
  Return null for `ask` when kind + day + trait is already enough. Do NOT ask about who-it's-for (that's a separate step).

Use null for any string the text does not support. Never invent a value."""


def _extract_meet_fields(
    *, history: list[dict[str, Any]], user_message: str, prev: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """LLM structured extraction. Returns (fields_found, ask). ({}, None) on failure."""
    try:
        from app.orchestrator.llm import llm_configured, llm_json, synthesizer_model

        if not llm_configured():
            return {}, None
        convo = "\n".join(
            f"{m.get('role', '?')}: {str(m.get('content') or '').strip()}"
            for m in (history or [])[-8:]
            if str(m.get("content") or "").strip()
        )
        known = {k: prev.get(k) for k in _MEET_VALUE_FIELDS}
        payload = "\n\n".join(
            [
                "CURRENT MEET DRAFT (merge updates into this):\n"
                + json.dumps(known, ensure_ascii=False),
                "CONVERSATION SO FAR:\n" + (convo or "(none)"),
                f"USER'S NEW MESSAGE:\n{user_message.strip()}",
            ]
        )
        data = llm_json(
            model=synthesizer_model(),
            system=_EXTRACT_SYSTEM,
            user_payload=payload,
            max_tokens=300,
            temperature=0.2,
        )
        if not isinstance(data, dict):
            return {}, None
        out: dict[str, Any] = {}
        for k in _MEET_VALUE_FIELDS:
            v = data.get(k)
            if isinstance(v, str) and v.strip() and v.strip().lower() != "null":
                out[k] = v.strip()
        ask = data.get("ask")
        if isinstance(ask, dict):
            q = str(ask.get("question") or "").strip()
            opts = [
                str(o).strip()
                for o in (ask.get("options") or [])
                if isinstance(o, str) and str(o).strip()
            ][:4]
            ask = {"field": str(ask.get("field") or "detail").strip(), "question": q, "options": opts} if (q and len(opts) >= 2) else None
        else:
            ask = None
        return out, ask
    except Exception:  # noqa: BLE001 - extraction is best-effort
        import logging

        logging.getLogger(__name__).exception("look_meet_extract_failed")
        return {}, None


def _has(draft: dict[str, Any], key: str) -> bool:
    return bool(str(draft.get(key) or "").strip())


def _build_chips(draft: dict[str, Any]) -> list[dict[str, str]]:
    """The 'Got it' chips — kind + day + trait + the affinity choice. Tap to correct."""
    chips: list[dict[str, str]] = [{"label": "Looking for", "tone": "sky", "field": "kind"}]
    if _has(draft, "kind"):
        chips.append({"label": str(draft["kind"]), "tone": "coral", "field": "kind"})
    if _has(draft, "day"):
        chips.append({"label": str(draft["day"]), "tone": "sky", "field": "day"})
    if _has(draft, "trait"):
        chips.append({"label": str(draft["trait"]), "tone": "amber", "field": "trait"})
    for d in (draft.get("details") or []):
        if str(d).strip():
            chips.append({"label": str(d).strip(), "tone": "violet", "field": "details"})
    if _has(draft, "affinity"):
        chips.append({"label": str(draft["affinity"]), "tone": "green", "field": "affinity"})
    return chips


def _question_for_field(field: str) -> tuple[str, list[str]]:
    if field == "kind":
        return "What kind of meet would help?", _KIND_SUGGESTIONS
    if field == "affinity":
        return _AFFINITY_QUESTION, _AFFINITY_OPTIONS
    if field == "details":
        return "What detail should I update?", []
    return "What should I change?", []


def _summary(draft: dict[str, Any]) -> str:
    bits = []
    if _has(draft, "day"):
        bits.append(str(draft["day"]))
    bits.append(str(draft.get("kind") or "meet").strip())
    if _has(draft, "trait"):
        bits.append(str(draft["trait"]))
    return " ".join(b for b in bits if b)


def _detail_text(draft: dict[str, Any]) -> str:
    parts = []
    if _has(draft, "day"):
        parts.append(str(draft["day"]).strip())
    parts.append(str(draft.get("kind") or "").strip())
    if _has(draft, "trait"):
        parts.append(str(draft["trait"]).strip())
    parts += [str(d).strip() for d in (draft.get("details") or []) if str(d).strip()]
    if _has(draft, "place"):
        parts.append(str(draft["place"]).strip())
    return " · ".join([p for p in parts if p]) or str(draft.get("kind") or "meet")


def _affinity_tags(draft: dict[str, Any]) -> list[str]:
    a = str(draft.get("affinity") or "").strip().lower()
    if not a or a.startswith("open"):
        return []
    if "same" in a or "kid-stage" in a or "kid stage" in a:
        return ["same_kid_stage"]
    if "toddler" in a:
        return ["toddler"]
    return [a.replace(" ", "_")[:40]]


def _jwt_sub(jwt: str | None) -> str | None:
    """The JWT's `sub` (user id), decoded locally — no crypto: the token was
    verified upstream this request; this is only for a read-side display gate."""
    try:
        import base64
        import json as _json

        payload = str(jwt or "").split(".")[1]
        payload += "=" * (-len(payload) % 4)
        sub = _json.loads(base64.urlsafe_b64decode(payload)).get("sub")
        return str(sub) if sub else None
    except Exception:  # noqa: BLE001
        return None


def _find_block_events(
    *, user_jwt: str, kind: str | None, zip_code: str | None, block_id: str | None, limit: int = 3
) -> list[dict[str, Any]]:
    """Existing open meets near the seeker (next 14 days), kind-matched first.

    Events are the source of truth for real meetups; the meet_seek signal matcher
    only pairs latent demand (host_meet signals). So to actually surface joinable
    meets — including ones hosted as events — we read get_nearby_activities here.

    get_nearby_activities needs a location. The look flow runs before discovery sets
    the session zip, so we fall back to the home block's centroid (every verified
    neighbour has a home_block_id) — that's what actually makes this fire.
    """
    # No area gate here (reverted 2026-08-05): §D.2 is supply-aware — real events
    # stay visible to the neighbours they were created for in any unlock state, and
    # hiding them starves the meets that make an area come alive. The 2026-07-30
    # block was aimed at off-topic matches, which the relevance floor below handles
    # precisely. See zip_unlock.discovery_zip_gate for the full reasoning.
    args: dict[str, Any] = {"p_limit": 20}
    if zip_code:
        args["p_zip"] = zip_code
    else:
        try:
            from app.places import _centroid

            loc = _centroid(zip_code, block_id)
        except Exception:  # noqa: BLE001
            loc = None
        if not loc:
            return []
        args["p_lat"], args["p_lng"] = loc[0], loc[1]
    try:
        from app.supabase_rpc import call_rpc

        rows = call_rpc(user_jwt, "get_nearby_activities", args)
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(rows, list):
        return []

    keywords = [w for w in re.findall(r"[a-z]+", str(kind or "").lower()) if len(w) > 2]

    def relevance(ev: dict[str, Any]) -> int:
        hay = (str(ev.get("title") or "") + " " + " ".join(ev.get("cohort_tags") or [])).lower()
        return sum(1 for w in keywords if w in hay)

    candidates = [e for e in rows if isinstance(e, dict) and e.get("id")]
    if keywords:
        # Relevance floor (bridge spec §2): the seeker named what kind of meet they
        # want — an unrelated event is NOT a match, and returning it here blocks the
        # create+invite fall-through (QA 2026-07-30: "meet other runners" surfaced a
        # coffee catch-up because top-N kept zero-relevance events). Nothing on-topic
        # → return empty and let the seek/create path own the turn.
        candidates = [e for e in candidates if relevance(e) > 0]
    ranked = sorted(candidates, key=relevance, reverse=True)
    out: list[dict[str, Any]] = []
    for e in ranked[:limit]:
        out.append({
            "event_id": str(e.get("id")),
            "title": str(e.get("title") or "A neighbourhood meet"),
            "starts_at": e.get("starts_at"),
            "has_time": e.get("has_time") is not False,
            "venue_name": e.get("venue_name"),
        })
    return out


def _save_meet_seek(
    *, draft: dict[str, Any], user_jwt: str, block_id: str | None, zip_code: str | None
) -> dict[str, Any] | None:
    try:
        from app.local_signals import save_local_signal

        return save_local_signal(
            user_jwt,
            intent="meet_seek",
            detail_text=_detail_text(draft),
            category=str(draft.get("kind") or "").strip() or "meetup",
            block_id=block_id,
            zip_code=zip_code,
        )
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).exception("look_meet_save_failed")
        return None


def run_look_meet_turn(
    *,
    user_message: str,
    session_ctx: dict[str, Any],
    history: list[dict[str, Any]],
    user_jwt: str,
    home_block_id: str | None,
) -> str:
    """Drive one look-meet capture turn. Mutates session_ctx (look_draft,
    look_meet_active, look_meet_saved_now, routing_phase). Returns Lana's reply."""
    msg = str(user_message or "").strip()
    draft: dict[str, Any] = dict(session_ctx.get("look_draft") or {})
    zip_code = str(session_ctx.get("zip_code") or session_ctx.get("zip") or "").strip() or None
    session_ctx["look_meet_saved_now"] = False

    # ── Loop safety ──
    turns = int(session_ctx.get("look_turns") or 0) + 1
    session_ctx["look_turns"] = turns
    if _CANCEL_RE.search(msg) or turns > _LOOK_MEET_TURN_CAP:
        for k in ("look_meet_active", "look_draft", "look_ready", "look_pending_ask",
                  "look_enrich_count", "look_affinity_asked"):
            session_ctx[k] = None
        session_ctx["look_turns"] = 0
        session_ctx["routing_phase"] = "listening"
        return "No problem — we can do that another time. What else can I help with?"

    # ── The "Start listening for me" CTA on the ready card → save the seek ──
    if session_ctx.get("look_ready") and _LISTEN_RE.search(msg):
        # Guests must verify before we can save under a real account (same email prompt
        # logs in an existing account or signs up a new one). Stash the ready seek and
        # gate into verify; it auto-saves the moment they're verified.
        if not session_ctx.get("phone_verified"):
            return _gate_guest_before_save(session_ctx, draft)
        saved = _save_meet_seek(draft=draft, user_jwt=user_jwt, block_id=home_block_id, zip_code=zip_code)
        for k in ("look_meet_active", "look_ready", "look_pending_ask", "look_affinity_asked"):
            session_ctx[k] = None
        session_ctx["look_turns"] = 0
        session_ctx["look_enrich_count"] = 0
        session_ctx["routing_phase"] = "listening"
        if not saved:
            session_ctx["look_draft"] = None
            return "I couldn't save that just now — let's try again in a moment."
        matches = int(saved.get("matches_created") or 0)
        draft["signal_id"] = saved.get("signal_id")
        draft["saved"] = True
        draft["chips"] = _build_chips(draft)
        session_ctx["look_draft"] = draft
        session_ctx["look_meet_saved_now"] = True
        tail = (
            f" {matches} neighbor{'s' if matches != 1 else ''} wanting the same just matched!"
            if matches
            else " I'll text you when another neighbor wants the same near you."
        )
        return f"✅ Saved — I'm listening for a **{_summary(draft)}**.{tail}"

    # ── Correction: chip tap "fix:<field>" → clear + re-ask that field ──
    fix = re.match(r"\s*fix:(\w+)\s*$", msg)
    if fix:
        field = fix.group(1)
        if field == "details":
            draft["details"] = []
            session_ctx["look_pending_ask"] = "details"
            session_ctx["look_enrich_count"] = 0
        elif field == "affinity":
            draft.pop("affinity", None)
            session_ctx["look_affinity_asked"] = None
        elif field in _MEET_VALUE_FIELDS:
            draft.pop(field, None)
        session_ctx["look_ready"] = None
        q, opts = _question_for_field(field)
        draft["chips"] = _build_chips(draft)
        draft["suggestions"] = opts
        session_ctx["look_draft"] = draft
        session_ctx["look_meet_active"] = True
        session_ctx["routing_phase"] = "listening"
        return f"Sure — {q}"

    # ── Seed turn from the "A meet or playgroup" button: don't mine the generic
    #    entry phrase — drop it so P1 ("what kind of meet?") asks fresh. ──
    if session_ctx.get("look_meet_skip_seed"):
        session_ctx["look_meet_skip_seed"] = False
        msg = ""

    # ── Capture a pending answer (enrichment detail, or the affinity choice) ──
    pending = session_ctx.get("look_pending_ask")
    if pending and msg and not _LISTEN_RE.search(msg):
        if pending == "affinity":
            draft["affinity"] = msg
        else:
            details = list(draft.get("details") or [])
            if msg not in details:
                details.append(msg)
            draft["details"] = details
        session_ctx["look_pending_ask"] = None

    # ── Extract fields + tailored follow-up ──
    ask: dict[str, Any] | None = None
    if msg and not fix:
        found, ask = _extract_meet_fields(history=history, user_message=msg, prev=draft)
        for k, v in found.items():
            draft[k] = v

    # ── P1: nothing yet → "What kind of meet would help?" ──
    if not _has(draft, "kind"):
        # Ask the kind question (with chips) ONCE. If they reply again without a nameable
        # kind, take their words as the kind and move on — never re-ask the identical
        # question, which dead-ended vague input like "any fun activity". The flag rides on
        # the draft, so it resets whenever the capture does.
        if draft.get("_p1_asked") and msg:
            draft["kind"] = msg[:80]
        else:
            draft["_p1_asked"] = True
            draft["suggestions"] = _KIND_SUGGESTIONS
            session_ctx["look_draft"] = draft
            session_ctx["look_meet_active"] = True
            session_ctx["routing_phase"] = "listening"
            return t("meet.ask_kind", session_lang(session_ctx))

    chips = _build_chips(draft)

    # ── AI-tailored enrichment (day / place / vibe), capped ──
    enrich_count = int(session_ctx.get("look_enrich_count") or 0)
    if ask and enrich_count < _MAX_ENRICH and not _has(draft, "affinity"):
        session_ctx["look_pending_ask"] = ask["field"]
        session_ctx["look_enrich_count"] = enrich_count + 1
        draft["chips"] = chips
        draft["suggestions"] = ask["options"]
        session_ctx["look_draft"] = draft
        session_ctx["look_meet_active"] = True
        session_ctx["routing_phase"] = "listening"
        return f"Got it — **{_summary(draft)}**. {ask['question']}"

    # ── P3: affinity (who's it for?) once ──
    if not _has(draft, "affinity") and not session_ctx.get("look_affinity_asked"):
        draft["chips"] = chips
        draft["suggestions"] = _AFFINITY_OPTIONS
        session_ctx["look_pending_ask"] = "affinity"
        session_ctx["look_affinity_asked"] = True
        session_ctx["look_draft"] = draft
        session_ctx["look_meet_active"] = True
        session_ctx["routing_phase"] = "listening"
        return f"Got it — **{_summary(draft)}**. {_AFFINITY_QUESTION}"

    # ── P4: ready → assembled card + dual CTA (saved only when they confirm) ──
    events = _find_block_events(
        user_jwt=user_jwt, kind=draft.get("kind"), zip_code=zip_code, block_id=home_block_id
    )
    draft["chips"] = chips
    draft["suggestions"] = []
    draft["events"] = events
    draft["ready"] = True
    session_ctx["look_draft"] = draft
    session_ctx["look_meet_active"] = True
    session_ctx["look_ready"] = True
    session_ctx["routing_phase"] = "listening"
    # Guests aren't saved until they verify (tapping "Start listening" gates into signup,
    # then auto-saves). Say so up front so the "I'll text you" promise doesn't read as
    # already-listening — sign-up comes first, then the seek is created.
    guest = not session_ctx.get("phone_verified")
    listen_promise = (
        "**Start listening for me** — I'll get you signed up, then text you when a neighbor wants the same"
        if guest
        else "**Start listening for me** and I'll text you when a neighbor wants the same"
    )
    if events:
        n = len(events)
        return (
            f"Here's what I've got — **{_summary(draft)}**. I also found {n} meet"
            f"{'s' if n != 1 else ''} near you that you could join — take a look below, or "
            f"{listen_promise}."
        )
    return (
        f"Here's what I've got — **{_summary(draft)}**. {listen_promise}, "
        "or send it to a neighbor you know."
    )
