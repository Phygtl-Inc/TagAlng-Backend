"""Flash extraction for Layer 1 linear intents + discovery funnel slots."""

from __future__ import annotations

import os
import re
from typing import Any

from app.layer1_intents import (
    LINEAR_INTENTS,
    enrich_slots,
    intent_confidence_met,
    normalize_attr_filter_text,
    slots_indicate_hosting_signal,
    slots_linear_intent,
    slots_want_layer1_handling,
    utterance_indicates_swap_seek,
    utterance_indicates_tip_seek,
)
from app.signal_capture import is_signal_lane_intent
from app.orchestrator.llm import llm_configured, llm_json, router_model
from app.turn_timing import TurnTimer

_ZIP_IN_TEXT = re.compile(r"\b(\d{5})\b")

_SYSTEM = (
    "You are the ONLY router for TagAlng Lana discovery vs chat on each user message. "
    "Output only valid JSON. "
    "Discovery funnel = ZIP, giving self-description for matching, preview matches, verify phone, RSVP. "
    "When routing_phase=listening and user wants to meet/find/show/connect with neighbors or people "
    "(any phrasing: 'meet new people', 'make me meet people', 'find me people', 'stop asking questions') "
    "→ goal=peers, in_discovery=true — even if prior turns were casual chat. "
    "When user is frustrated and demands to see people/users/neighbors → goal=peers, in_discovery=true, NOT chat. "
    "If RECENT TURNS already contain self-description (heritage, family, life stage, short answers like 'toddlers') "
    "and the latest message asks to find/meet people → goal=peers and set identity_snippet synthesized from RECENT TURNS. "
    "Non-funnel chat = goal chat or none, in_discovery=false — companionship AI answers (profile questions, "
    "what are my claims, what's my name, random questions, meta, off-topic). "
    "identity_snippet = self-description for matching from the latest message OR synthesized from RECENT TURNS "
    "when routing_phase=need_identity, when the latest message is ZIP-only, or when goal=peers and RECENT TURNS "
    "already describe the user (include short answers like 'toddlers', 'parents' when synthesizing). "
    "Never set identity_snippet from questions or meta. "
    "When routing_phase=need_identity: user answering the identity step (even one word like 'British') → "
    "goal=continue, in_discovery=true; set identity_snippet from their answer enriched with RECENT TURNS if helpful. "
    "If the user only sent a ZIP code with no prior self-description in RECENT TURNS, identity_snippet must be null. "
    "When the latest message is only a ZIP code, keep the same goal as the prior browse request "
    "(activities stays activities, peers stays peers) — use goal=continue, in_discovery=true. "
    "Mid-funnel pushback or topic change in preview → in_discovery=false, goal=chat. "
    "When routing_phase=preview and phone_verified=true: user wants Lana to introduce them to a "
    "shown neighbor (introduce me, connect us, put us together, meet them, send intro to X, "
    "send an intro to Kashaf, yes introduce) "
    "→ goal=propose_intro, in_discovery=true, set peer_name when they name someone. "
    "NUMBERED INTRO — resolve WHO from RECENT TURNS + session active_intent (not a new peer search): "
    "If the latest assistant turn listed block-log swap/match rows (numbered 1., 2., 'active matches', "
    "'introduce me to #1') → goal=propose_intro, linear_intent=social.propose_intro, "
    "intro_source=block_log, intro_list_index=N (1-based from user message). NOT goal=peers. "
    "If the latest assistant turn showed identity neighbor preview cards (heritage %, Kashaf, etc.) "
    "→ goal=propose_intro, intro_source=peer_preview, intro_list_index=N and/or peer_name. NOT goal=peers. "
    "When user picks from a list Lana already showed, NEVER goal=peers — they are accepting a shown match. "
    "When routing_phase=preview and Lana just offered an intro (pending) and user says yes/sure/ok "
    "→ goal=propose_intro, in_discovery=true. "
    "'are these Brazilian?', 'why moms not dads?') → in_discovery=false, goal=chat — NOT peers. "
    "Never set identity_snippet from questions — only from new self-description. "
    "Pushback or frustration about match quality in preview (cards already shown) → goal=chat. "
    "Pushback while still in listening with no preview yet but user demands find/show people → goal=peers. "
    "Only goal=peers + in_discovery=true in preview when user gives NEW self-description for matching "
    "(different identity_snippet than session) and explicitly wants a fresh search — not for questions. "
    "AUTH vs discovery — classify carefully; infinite phrasing is normal: "
    "When user wants to LOG IN to an EXISTING account (log me in, sign in, I already have an account, "
    "returning user, let me back in, use my old account) → goal=login, in_discovery=false — "
    "NOT peers, NOT verify, NOT chat. "
    "When phone_verified=false and user wants to CREATE an account / SIGN UP / REGISTER / join TagAlng "
    "(sign me up, create account, get verified, see names, complete registration — any phrasing) "
    "→ goal=verify, in_discovery=true at ANY routing_phase — discovery collects phone, NOT profile chat. "
    "Do NOT classify signup/verify as goal=peers or goal=chat. "
    "When user wants to LOG OUT / sign out → goal=logout, in_discovery=false. "
    "If phone_verified=true, new signup/verify requests → goal=chat (already verified). "
    "goal: login = returning user access existing account; logout = sign out; "
    "peers = find/show neighbors; activities = browse events; both; verify = phone signup gate; rsvp; "
    "propose_intro = user wants Lana to formally introduce them to a shown neighbor (preview, verified); "
    "list_intros = user wants to see pending intros they sent or received "
    "(show my intros, pending intros, intro status, who did I introduce, intros waiting on me, "
    "'what did you send', 'show me what you sent to them', 'what intro message did you send'); "
    "When user asks what Lana sent in an intro ('what did you send to them'), "
    "choose goal=list_intros (not peers), set in_discovery=true, intro_direction=sent. "
    "For 'show my intros' / pending inbox / any intros → goal=list_intros, intro_direction=all. "
    "Do NOT choose goal=peers for intro-message/status questions even if user says 'show me'. "
    "save_signal = user is seeking OR offering something on their block — swap/borrow items, meetups/playgroups, "
    "or local tips/recommendations (any phrasing: looking for rain boots, I have a stroller to give, "
    "host a coffee morning, know a good pediatrician, anyone want to swap); "
    "show_block_log = user wants **their own** pending match log (show my block log(s), who matched with me, my matches); "
    "NOT what neighbors are posting — for 'what are people looking for on my block' use goal=peers or find_in_block, NOT show_block_log. "
    "profile_photo = user wants to add/change/upload a profile picture, agrees to Lana's photo suggestion "
    "(yes/sure), says they finished uploading, or cancels photo upload; "
    "chat = companionship / profile read / any non-funnel question; "
    "continue = user is answering the current funnel step (supplying ZIP or identity snippet); "
    "none = not discovery. "
    "abandon (separate boolean, any goal) = the user wants to STOP the activity Lana is currently "
    "helping with (hosting an event, a signal capture, the funnel) ENTIRELY, with NO replacement — "
    "classify by MEANING, not keywords. Set abandon=true only for phrasing that means 'stop / not "
    "now / drop it, and I'm not proposing anything instead': I don't wanna create an event, let's not "
    "for now, I have mixed feelings, maybe later, actually no, forget it, my plans changed, never mind. "
    "CRITICAL — abandon=false when the user rejects the CURRENT plan but proposes a DIFFERENT activity "
    "or detail in the same breath: that is a CHANGE, not a quit. 'I don't wanna host a bbq, what if we "
    "do a movie night?', 'scrap the picnic, let's do brunch', 'not Saturday — make it Sunday', 'make "
    "it for everyone instead' all keep the event alive (abandon=false) — they are editing it. Only "
    "abandon when they stop with no alternative. "
    "Also abandon=false for: answering a question (a title, a time, a chip tap) or mild uncertainty "
    "about ONE detail (not sure what to call it, what time is good?). abandon means quitting the whole "
    "task, never editing or swapping a part of it. "
    "When goal=profile_photo set profile_photo_action: start (wants upload), accept (yes after Lana suggested), "
    "done (finished uploading), skip (cancel/not now), none. "
    "When routing_phase=await_profile_photo map the latest message to the right profile_photo_action. "
    "When goal=save_signal set signal_intent: swap_seek|swap_offer|meet_seek|host_meet|tip_seek|tip_share, "
    "signal_detail = what they want/offer (short phrase from message), signal_category = optional bucket. "
    "Classify save_signal by MEANING (infinite phrasing is normal): "
    "meet_seek = wants a NEIGHBOR to do an ACTIVITY WITH them (jogging partner, walking buddy, playdate) — "
    "NOT acquiring items. "
    "swap_seek/swap_offer = physical ITEMS to borrow/swap/get for kids or home — NOT meet_seek. "
    "Possessive my + swap/give away = sharing.swap; looking for item = looking.swap. "
    "Kids clothing/gear (boots, onesies) may need size; bikes/electronics/furniture do not use 3T. "
    "Adult clothing (adult/adults/grown-up) never needs kid size like 3T. "
    "tip_seek/tip_share = local SERVICE or place (teacher, tutor, pediatrician, doctor, restaurant, "
    "plumber) — set signal_category education|health|food|home|activities; NOT swap_seek, NOT discovery. "
    "DECIDE BY ASK vs OFFER, not by the verb: "
    "tip_seek (looking.tip) = the user is ASKING you to find/suggest one — any request or question: "
    "'do you know a good restaurant', 'recommend a plumber', 'can you suggest good doctors', "
    "'suggest me a dentist', 'find me a tutor', 'know any good pediatricians', 'who is a good vet'. "
    "If the message is a question or a request directed at you, it is ALWAYS tip_seek — NEVER tip_share. "
    "tip_share (sharing.tip) = the user NAMES a specific provider/place THEY vouch for: "
    "'I recommend Dr Smith', 'try Dr Lee', 'my favorite pizza is Tony's', 'Dr Patel is a great dentist'. "
    "If no specific name/place is given, it is NOT tip_share. "
    "NEVER tip_seek when user wants to FIND/SHOW NEIGHBORS by heritage, life stage, or traits "
    "(find italian moms, find italian dads, brazilian parents on my block) — that is discovery.find_by_attrs. "
    "host_meet = the user is the ORGANIZER who wants to bring neighbors together for a gathering THEY "
    "create — classify by MEANING, not by specific words. Any phrasing where the user is hosting, "
    "planning, throwing, setting up, organizing, or creating something others attend is host_meet "
    "(I want to create an event, I'm planning a party, let's throw a get-together, I want to host "
    "something this weekend, set up a block hang, organize a brunch, plan a playdate at the park). "
    "It is STILL host_meet when NO specific activity type is named yet (a bare 'I want to create an "
    "event' / 'I want to host something') — the activity can be collected later; what matters is the "
    "user is the organizer INVITING/GATHERING others, not asking to be shown people. "
    "sharing.host + goal=save_signal. "
    "Contrast with discovery.find_by_attrs/find_peers, where the user wants to BE SHOWN matching "
    "neighbors (find/show me ...). This holds even if a heritage word appears: heritage + "
    "mom/dad/parent/neighbor with a search verb = find people; the user organizing a gathering "
    "(host/plan/create/throw/set up, optionally with an activity or time) = host. "
    "Set linear_intent: looking.meet for meet_seek, looking.swap for swap_seek, looking.tip for tip_seek, "
    "sharing.swap for swap_offer, sharing.host for host_meet, sharing.tip for tip_share. "
    "When goal=show_block_log set intro_direction null. "
    "LAYER 1 CATALOG — set linear_intent to the best match (confidence ≥ 0.85 when sure): "
    "discovery.find_peers|discovery.find_by_attrs|discovery.find_in_block|discovery.find_activities|"
    "discovery.block_log|discovery.show_peer_profile|discovery.explain_peer_match; "
    "identity.add_claim|identity.edit_claim|identity.complete_profile|identity.show_my_profile; "
    "looking.swap|looking.meet|looking.tip|sharing.swap|sharing.host|sharing.tip; "
    "tier.send_nudge|tier.respond_nudge|social.list_intros|social.propose_intro; "
    "auth.signup_phone|auth.login_phone|auth.logout|auth.upload_photo; "
    "settings.change_name|settings.change_zip|settings.notification_prefs; "
    "help.what_can_you_do|help.who_are_you. "
    "Use identity.show_my_profile for 'what do you know about me', 'show my claims', 'my profile'. "
    "When the user describes THEMSELVES at ANY phase "
    "(I am american, I have a young child, I'm a teacher, I am a doctor, I am a mom) → "
    "identity.add_claim, goal=chat, in_discovery=false, identity_snippet=null "
    "(do NOT set goal=peers). A profession in a self-description (I am a teacher/doctor/nurse) "
    "is an IDENTITY claim — it is NEVER tip_seek/save_signal and NEVER a request for that service. "
    "Only treat a service word as tip_seek when the user is ASKING for one "
    "(do you know a good teacher, find me a tutor) — not when they say they ARE one. "
    "When user corrects heritage (I'm not X, I'm Y, I told you I am american) → identity.edit_claim. "
    "Use discovery.show_peer_profile when user asks about a SPECIFIC neighbor's identity claims/profile "
    "OR wants to find/locate someone BY NAME on the block "
    "(find Kashaf on my block, is Sofia on my block, check neighbors for Sofia) — set peer_name, goal=chat, "
    "in_discovery=false, NOT goal=peers, NOT identity.show_my_profile. "
    "Use discovery.explain_peer_match when user asks HOW/WHY match % on shown cards "
    "(how is 100% match, what is matching, what things are matching) — goal=chat, in_discovery=false, "
    "optional peer_name if they name someone; NEVER re-run find_peers. "
    "Use identity.add_claim when user describes themselves (heritage, stage, interests). "
    "Use identity.edit_claim for corrections ('I'm not X, I'm Y', 'edit my identity'). "
    "Heritage is one slot — if user states a new heritage that contradicts prior, ask to confirm before replacing. "
    "Use discovery.find_by_attrs when user wants neighbors matching traits (ANY heritage/adjective + "
    "mom/dad/parent/language/stage — infinite phrasing: show me american moms, find italian dads, "
    "brazilian parents on my block). Set attr_filter to the trait phrase "
    "(e.g. american moms, italian dads). NOT identity.add_claim, NOT identity.edit_claim, goal=peers. "
    "find_by_attrs REQUIRES an explicit search verb (find/show/look for/who is/any/connect me) OR an "
    "'on my block' target. A bare self-description that only LISTS the user's OWN traits with NO search "
    "verb (I'm Asian with a teenager, we just moved here, I'm a new mom who loves hiking) is "
    "identity.add_claim — even when it contains heritage + family words. At routing_phase=need_identity "
    "a trait answer describes the USER → goal=continue / identity, NEVER find_by_attrs. "
    "Use discovery.find_in_block for block activity browse (what's on my block, what is happening on my block, "
    "what are people swapping, neighborhood activity) — NOT social.propose_intro even if a prior turn offered an intro. "
    "Use looking.swap/meet/tip for seeks; sharing.swap/host/tip for offers. "
    "Use settings.change_zip for moved/updated ZIP; settings.change_name for name changes "
    "(change my name, call me X, my name is X). "
    "Use help.what_can_you_do for help/what can you do; help.who_are_you for who are you. "
    "Also set legacy goal field when applicable (peers, save_signal, verify, login, etc.)."
)


def discovery_ai_enabled() -> bool:
    flag = os.environ.get("LANA_DISCOVERY_AI_SLOTS", "1").strip().lower()
    return flag not in ("0", "false", "off") and llm_configured()


def _extract_model() -> str:
    """Discovery slots use the orchestrator router model (gpt-4o-mini or Flash)."""
    if llm_configured():
        return router_model()
    return os.environ.get("VERTEX_EXTRACT_MODEL", "gemini-2.5-flash")


def _format_history(history: list[dict[str, Any]] | None, *, limit: int = 8) -> str:
    if not history:
        return "(none)"
    lines: list[str] = []
    for turn in history[-limit:]:
        role = str(turn.get("role") or "user")
        content = str(turn.get("content") or "").strip()
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines) or "(none)"


def _empty_slots() -> dict[str, Any]:
    return {
        "in_discovery": False,
        "linear_intent": None,
        "goal": "none",
        "intro_direction": None,
        "intro_source": None,
        "intro_list_index": None,
        "zip": None,
        "identity_snippet": None,
        "profile_photo_action": "none",
        "signal_intent": None,
        "signal_detail": None,
        "signal_category": None,
        "abandon": False,
        "confidence": 0.0,
    }


def ai_parse_discovery_turn(
    utterance: str,
    *,
    routing_phase: str,
    history: list[dict[str, Any]] | None,
    has_block: bool,
    has_identity: bool,
    phone_verified: bool = False,
    has_profile_photo: bool = False,
    session_ctx: dict[str, Any] | None = None,
    timer: TurnTimer | None = None,
) -> dict[str, Any]:
    """One Flash call: discovery yes/no, goal (peers/activities/profile_photo), zip, identity snippet."""
    if not discovery_ai_enabled():
        return _empty_slots()
    text = str(utterance or "").strip()
    if not text:
        return _empty_slots()
    try:
        attempts_box: list[int] = []
        if timer:
            with timer.stage("llm_discovery_slots"):
                raw = llm_json(
                    model=_extract_model(),
                    system=_SYSTEM,
                    user_payload=_discovery_slot_payload(
                        text,
                        routing_phase=routing_phase,
                        history=history,
                        has_block=has_block,
                        has_identity=has_identity,
                        phone_verified=phone_verified,
                        has_profile_photo=has_profile_photo,
                        session_ctx=session_ctx,
                    ),
                    max_tokens=160,
                    temperature=0.0,
                    llm_attempts=attempts_box,
                )
            if attempts_box:
                timer.set_count("llm_discovery_slots_attempts", attempts_box[0])
        else:
            raw = llm_json(
                model=_extract_model(),
                system=_SYSTEM,
                user_payload=_discovery_slot_payload(
                    text,
                    routing_phase=routing_phase,
                    history=history,
                    has_block=has_block,
                    has_identity=has_identity,
                    phone_verified=phone_verified,
                    has_profile_photo=has_profile_photo,
                    session_ctx=session_ctx,
                ),
                max_tokens=160,
                temperature=0.0,
            )
        goal = str(raw.get("goal") or "none").lower()
        if goal not in (
            "peers",
            "activities",
            "both",
            "verify",
            "login",
            "logout",
            "rsvp",
            "propose_intro",
            "list_intros",
            "save_signal",
            "show_block_log",
            "profile_photo",
            "chat",
            "continue",
            "none",
        ):
            goal = "none"
        signal_intent = raw.get("signal_intent")
        signal_intent_s = str(signal_intent).strip().lower() if signal_intent else None
        if signal_intent_s not in (
            "swap_seek",
            "swap_offer",
            "meet_seek",
            "host_meet",
            "tip_seek",
            "tip_share",
        ):
            signal_intent_s = None
        signal_detail = raw.get("signal_detail")
        signal_detail_s = str(signal_detail).strip()[:500] if signal_detail else None
        signal_category = raw.get("signal_category")
        signal_category_s = str(signal_category).strip()[:120] if signal_category else None
        signal_stage = raw.get("signal_stage")
        signal_stage_s = str(signal_stage).strip()[:80] if signal_stage else None
        signal_when = raw.get("signal_when")
        signal_when_s = str(signal_when).strip()[:120] if signal_when else None
        attr_filter = raw.get("attr_filter")
        attr_filter_s = str(attr_filter).strip()[:200] if attr_filter else None
        peer_name = raw.get("peer_name")
        peer_name_s = str(peer_name).strip()[:80] if peer_name else None
        intro_direction = raw.get("intro_direction")
        intro_direction_s = str(intro_direction).strip().lower() if intro_direction else None
        if intro_direction_s not in ("sent", "received", "all"):
            intro_direction_s = None
        intro_source = raw.get("intro_source")
        intro_source_s = str(intro_source).strip().lower() if intro_source else None
        if intro_source_s not in ("block_log", "peer_preview"):
            intro_source_s = None
        intro_list_index_s: int | None = None
        intro_list_index_raw = raw.get("intro_list_index")
        if intro_list_index_raw is not None:
            try:
                intro_list_index_s = int(intro_list_index_raw)
                if intro_list_index_s < 1:
                    intro_list_index_s = None
            except (TypeError, ValueError):
                intro_list_index_s = None
        photo_action = str(raw.get("profile_photo_action") or "none").lower()
        if photo_action not in ("start", "accept", "skip", "done", "none"):
            photo_action = "none"
        zip_val = raw.get("zip")
        zip_s = str(zip_val).strip() if zip_val else None
        if zip_s:
            m = _ZIP_IN_TEXT.search(zip_s)
            zip_s = m.group(1) if m else None
        ident = raw.get("identity_snippet")
        ident_s = str(ident).strip()[:400] if ident else None
        linear_raw = str(raw.get("linear_intent") or "").strip().lower()
        linear_intent = linear_raw if linear_raw in LINEAR_INTENTS else None
        return enrich_slots({
            "in_discovery": bool(raw.get("in_discovery")),
            "linear_intent": linear_intent,
            "goal": goal,
            "intro_direction": intro_direction_s,
            "intro_source": intro_source_s,
            "intro_list_index": intro_list_index_s,
            "zip": zip_s,
            "identity_snippet": ident_s,
            "profile_photo_action": photo_action,
            "signal_intent": signal_intent_s,
            "signal_detail": signal_detail_s,
            "signal_category": signal_category_s,
            "signal_stage": signal_stage_s,
            "signal_when": signal_when_s,
            "attr_filter": attr_filter_s,
            "peer_name": peer_name_s,
            "abandon": bool(raw.get("abandon")),
            "confidence": float(raw.get("confidence", 0.0)),
        }, msg=text)
    except Exception:
        return _empty_slots()


def _discovery_slot_payload(
    text: str,
    *,
    routing_phase: str,
    history: list[dict[str, Any]] | None,
    has_block: bool,
    has_identity: bool,
    phone_verified: bool,
    has_profile_photo: bool = False,
    session_ctx: dict[str, Any] | None = None,
) -> str:
    active_intent = str((session_ctx or {}).get("active_intent") or "").strip() or "none"
    return (
        f"routing_phase: {routing_phase or 'listening'}\n"
        f"has_block: {has_block}\n"
        f"has_identity_in_session: {has_identity}\n"
        f"phone_verified: {phone_verified}\n"
        f"has_profile_photo: {has_profile_photo}\n"
        f"session_active_intent: {active_intent}\n\n"
        "RECENT TURNS:\n"
        f"{_format_history(history)}\n\n"
        f"LATEST USER MESSAGE:\n{text}\n\n"
        "Return JSON:\n"
        "{\n"
        '  "linear_intent": "<Layer 1 intent id or null>",\n'
        '  "in_discovery": true|false,\n'
        '  "goal": "peers"|"activities"|"both"|"verify"|"login"|"logout"|"rsvp"|"propose_intro"|"list_intros"|'
        '"save_signal"|"show_block_log"|"profile_photo"|"chat"|"continue"|"none",\n'
        '  "intro_direction": "sent"|"received"|"all"|null,\n'
        '  "intro_source": "block_log"|"peer_preview"|null,\n'
        '  "intro_list_index": 1-based integer when user picks #N from a shown list, else null,\n'
        '  "signal_intent": "swap_seek"|"swap_offer"|"meet_seek"|"host_meet"|"tip_seek"|"tip_share"|null,\n'
        '  "signal_detail": "string or null",\n'
        '  "signal_category": "string or null",\n'
        '  "signal_stage": "string or null",\n'
        '  "signal_when": "string or null",\n'
        '  "attr_filter": "string or null",\n'
        '  "peer_name": "neighbor name if asking about one person, else null",\n'
        '  "zip": "5-digit string or null",\n'
        '  "identity_snippet": "string or null",\n'
        '  "profile_photo_action": "start"|"accept"|"skip"|"done"|"none",\n'
        '  "abandon": true|false,\n'
        '  "confidence": 0.0-1.0\n'
        "}"
    )


def discovery_slots_for_turn(
    session_ctx: dict[str, Any],
    utterance: str,
    *,
    routing_phase: str,
    history: list[dict[str, Any]] | None,
    has_block: bool,
    has_identity: bool,
    phone_verified: bool = False,
    has_profile_photo: bool = False,
    timer: TurnTimer | None = None,
) -> dict[str, Any]:
    """Parse discovery slots once per user message; reuse within the same turn."""
    text = str(utterance or "").strip()
    cache_key = str(session_ctx.get("_discovery_slots_for") or "")
    cached = session_ctx.get("_discovery_slots")
    if text and cache_key == text and isinstance(cached, dict):
        return cached
    slots = ai_parse_discovery_turn(
        text,
        routing_phase=routing_phase,
        history=history,
        has_block=has_block,
        has_identity=has_identity,
        phone_verified=phone_verified,
        has_profile_photo=has_profile_photo,
        session_ctx=session_ctx,
        timer=timer,
    )
    if text:
        session_ctx["_discovery_slots"] = slots
        session_ctx["_discovery_slots_for"] = text
    return slots


def slots_want_propose_intro(slots: dict[str, Any]) -> bool:
    """AI decided user is accepting a shown match — not browsing for new peers."""
    goal = str(slots.get("goal") or "")
    linear = str(slots.get("linear_intent") or "")
    if goal != "propose_intro" and linear != "social.propose_intro":
        return False
    return float(slots.get("confidence", 0.0)) >= 0.5


def slots_peer_name(slots: dict[str, Any] | None) -> str | None:
    """Neighbor name from AI slots (not utterance regex)."""
    if not slots:
        return None
    name = str(slots.get("peer_name") or "").strip().lower()
    if not name or name in ("a", "an", "the", "neighbor", "neighbour"):
        return None
    return name


def slots_picking_shown_peer(
    slots: dict[str, Any] | None,
    session_ctx: dict[str, Any],
) -> bool:
    """AI + session: user is choosing from cards Lana already showed — not a new search."""
    if not slots:
        return False
    if slots_want_propose_intro(slots):
        return True
    enriched = enrich_slots(dict(slots))
    if str(enriched.get("intro_source") or "").strip():
        return True
    if enriched.get("intro_list_index") is not None and session_ctx.get("peer_matches"):
        return True
    name = slots_peer_name(enriched)
    if not name:
        return False
    stored = session_ctx.get("peer_matches")
    if not isinstance(stored, list):
        return False
    for row in stored:
        if not isinstance(row, dict):
            continue
        nick = str(row.get("nickname") or "").strip().lower()
        if nick and (nick == name or name in nick or nick in name):
            return True
    return False


def slots_want_preview_refetch(
    slots: dict[str, Any],
    session_ctx: dict[str, Any],
    *,
    msg: str = "",
) -> bool:
    """AI-only: re-run peer preview when user supplied new matching criteria (not questions)."""
    enriched = enrich_slots(dict(slots), msg=msg)
    if str(enriched.get("goal") or "") == "save_signal":
        return False
    linear = slots_linear_intent(enriched)
    if linear and is_signal_lane_intent(enriched) and intent_confidence_met(enriched, linear):
        return False
    if msg and (utterance_indicates_tip_seek(msg) or utterance_indicates_swap_seek(msg)):
        return False
    if slots_want_propose_intro(enriched) or slots_picking_shown_peer(enriched, session_ctx):
        return False
    if linear and (
        linear.startswith("identity.")
        or linear
        in (
            "discovery.show_peer_profile",
            "discovery.explain_peer_match",
        )
    ):
        return False
    goal = str(enriched.get("goal") or "none")
    if goal not in ("peers", "both") or not enriched.get("in_discovery"):
        return False
    if float(enriched.get("confidence", 0.0)) < 0.5:
        return False
    raw = enriched.get("identity_snippet")
    if not raw:
        return False
    new_sn = str(raw).strip()[:400]
    if not new_sn:
        return False
    stored = str(session_ctx.get("identity_snippet") or "").strip()
    return not stored or new_sn.lower() != stored.lower()


def slots_want_profile_photo(
    slots: dict[str, Any],
    *,
    routing_phase: str = "",
) -> bool:
    """AI decision: should profile-photo code handle this turn?"""
    phase = routing_phase or ""
    if phase == "await_profile_photo":
        return True
    if str(slots.get("goal") or "none") != "profile_photo":
        return False
    return float(slots.get("confidence", 0.0)) >= 0.5


_AUTH_SLOT_CONF = 0.5


def slots_want_login(slots: dict[str, Any] | None) -> bool:
    if not slots:
        return False
    return (
        str(slots.get("goal") or "none") == "login"
        and float(slots.get("confidence", 0.0)) >= _AUTH_SLOT_CONF
    )


def slots_want_logout(slots: dict[str, Any] | None) -> bool:
    if not slots:
        return False
    return (
        str(slots.get("goal") or "none") == "logout"
        and float(slots.get("confidence", 0.0)) >= _AUTH_SLOT_CONF
    )


def slots_want_signup_gate(slots: dict[str, Any] | None) -> bool:
    if not slots:
        return False
    return (
        str(slots.get("goal") or "none") == "verify"
        and float(slots.get("confidence", 0.0)) >= _AUTH_SLOT_CONF
    )


_IDENTITY_PROFILE_LINEAR = frozenset({
    "identity.add_claim",
    "identity.edit_claim",
    "identity.show_my_profile",
    "discovery.show_peer_profile",
})


_DISCOVERY_LINEAR_INTENTS = frozenset({
    "discovery.find_peers",
    "discovery.find_by_attrs",
    "discovery.find_in_block",
    "discovery.find_activities",
    "discovery.explain_peer_match",
})


def slots_indicate_peer_discovery(slots: dict[str, Any] | None) -> bool:
    """AI classified neighbor search — not self-identity (no regex on utterance)."""
    if not slots:
        return False
    enriched = enrich_slots(dict(slots))
    if slots_indicate_hosting_signal(enriched):
        return False
    goal = str(enriched.get("goal") or "none")
    if goal in ("peers", "both", "activities"):
        if float(enriched.get("confidence", 0.0)) >= 0.5:
            return True
    if str(enriched.get("attr_filter") or "").strip():
        return True
    linear = slots_linear_intent(enriched)
    if linear in _DISCOVERY_LINEAR_INTENTS:
        if intent_confidence_met(enriched, linear):
            return True
        if linear in ("discovery.find_peers", "discovery.find_by_attrs"):
            return float(enriched.get("confidence", 0.0)) >= 0.5
    return False


def slots_want_identity_profile_handling(slots: dict[str, Any] | None) -> bool:
    """AI classified show/add/edit own profile or look up a named neighbor."""
    if not slots:
        return False
    enriched = enrich_slots(dict(slots))
    goal = str(enriched.get("goal") or "none")
    if goal in ("peers", "both", "activities"):
        return False
    if str(enriched.get("attr_filter") or "").strip():
        return False
    linear = slots_linear_intent(enriched)
    if linear in _DISCOVERY_LINEAR_INTENTS:
        return False
    if linear not in _IDENTITY_PROFILE_LINEAR:
        return False
    return intent_confidence_met(enriched, linear)


def slots_want_discovery_handling(
    slots: dict[str, Any],
    *,
    routing_phase: str = "",
) -> bool:
    """AI decision: should discovery code handle this turn (not orchestrator)?"""
    enriched = enrich_slots(slots)
    if slots_want_layer1_handling(enriched, routing_phase=routing_phase):
        return True
    goal = str(enriched.get("goal") or "none")
    if goal == "save_signal":
        return float(enriched.get("confidence", 0.0)) >= 0.5
    if goal in ("chat", "none", "profile_photo", "login", "logout"):
        return False
    return False


def ai_wants_discovery(
    utterance: str,
    *,
    history: list[dict[str, Any]] | None = None,
    routing_phase: str = "",
) -> bool:
    slots = ai_parse_discovery_turn(
        utterance,
        routing_phase=routing_phase,
        history=history,
        has_block=False,
        has_identity=False,
    )
    return slots_want_discovery_handling(slots, routing_phase=routing_phase)
