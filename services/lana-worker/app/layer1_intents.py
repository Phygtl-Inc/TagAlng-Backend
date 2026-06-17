"""Layer 1 linear intent catalog — LANA_INTENTS_AND_ROUTING_v1.md §2 + LANA_BLUEPRINT_v1.md §2."""

from __future__ import annotations

import re
from typing import Any

# Canonical linear intents (classifier output). Auth OTP + tier.advance_* are phase/background.
LINEAR_INTENTS: frozenset[str] = frozenset({
    # Discovery
    "discovery.find_peers",
    "discovery.find_by_attrs",
    "discovery.find_in_block",
    "discovery.find_activities",
    "discovery.block_log",
    "discovery.show_peer_profile",
    "discovery.explain_peer_match",
    # Identity
    "identity.add_claim",
    "identity.edit_claim",
    "identity.complete_profile",
    "identity.show_my_profile",
    # Looking lane
    "looking.swap",
    "looking.meet",
    "looking.tip",
    # Sharing lane
    "sharing.swap",
    "sharing.host",
    "sharing.tip",
    # Relationship tier
    "tier.send_nudge",
    "tier.respond_nudge",
    "social.list_intros",
    "social.propose_intro",
    # Auth (OTP sub-phases are routing_phase-driven)
    "auth.signup_phone",
    "auth.login_phone",
    "auth.logout",
    "auth.upload_photo",
    # Settings + help
    "settings.change_name",
    "settings.change_zip",
    "settings.notification_prefs",
    "help.what_can_you_do",
    "help.who_are_you",
})

LOOKING_SHARING_INTENTS: frozenset[str] = frozenset({
    "looking.swap",
    "looking.meet",
    "looking.tip",
    "sharing.swap",
    "sharing.host",
    "sharing.tip",
})

SIGNAL_INTENT_BY_LINEAR: dict[str, str] = {
    "looking.swap": "swap_seek",
    "looking.meet": "meet_seek",
    "looking.tip": "tip_seek",
    "sharing.swap": "swap_offer",
    "sharing.host": "host_meet",
    "sharing.tip": "tip_share",
}

# Legacy goal → linear_intent (backward compat for tests + gradual rollout).
_GOAL_TO_LINEAR: dict[str, str] = {
    "peers": "discovery.find_peers",
    "activities": "discovery.find_activities",
    "both": "discovery.find_peers",
    "verify": "auth.signup_phone",
    "login": "auth.login_phone",
    "logout": "auth.logout",
    "propose_intro": "social.propose_intro",
    "list_intros": "social.list_intros",
    "save_signal": "looking.swap",
    "show_block_log": "discovery.block_log",
    "profile_photo": "auth.upload_photo",
    "continue": "discovery.find_peers",
}

LAYER1_DEFAULT_CONFIDENCE = 0.85

# Per-intent thresholds — funnel steps stay lower so discovery flow is not blocked.
INTENT_CONFIDENCE: dict[str, float] = {
    "discovery.find_peers": 0.45,
    "discovery.find_by_attrs": 0.55,
    "discovery.show_peer_profile": 0.55,
    "discovery.explain_peer_match": 0.55,
    "discovery.find_activities": 0.45,
    "identity.add_claim": 0.55,
    "identity.edit_claim": 0.55,
    "auth.signup_phone": 0.5,
    "auth.login_phone": 0.5,
    "auth.logout": 0.5,
    "auth.upload_photo": 0.5,
    "social.propose_intro": 0.5,
    "social.list_intros": 0.5,
    "discovery.block_log": 0.5,
    "tier.send_nudge": 0.5,
    "tier.respond_nudge": 0.5,
    "settings.change_name": 0.55,
    "settings.change_zip": 0.55,
    "settings.notification_prefs": 0.55,
    "help.what_can_you_do": 0.5,
    "help.who_are_you": 0.5,
}

for _li in LOOKING_SHARING_INTENTS:
    INTENT_CONFIDENCE[_li] = 0.55

# Phrase overrides — ONLY structural/policy cases where regex is safe.
# Open-ended intent (find neighbors, swap, tip, heritage filters) is classified by Flash in discovery_slots.
PHRASE_POLICY_OVERRIDES: frozenset[str] = frozenset({
    "identity.complete_profile",
    "identity.edit_claim",
    "settings.change_name",
    "discovery.block_log",
})

_BLOCK_BROWSE_PHRASE = (
    r"what (?:are|is) people (?:looking for|offering|swapping)|"
    r"who(?:'s| is) (?:looking for|offering|swapping)|"
    r"what(?:'s| is) (?:everyone|neighbors?) (?:looking for|offering|swapping)|"
    r"any(?:one|body) (?:looking for|offering|swapping)|"
    r"what(?:'s| are) swaps?|what are people swapping|"
    r"show (?:those|the|my)?\s*\d*\s*(?:neighbor )?(?:asks?|offers?|swaps?)|"
    r"show me what(?:'s| is) on (?:the )?block|"
    r"what(?:'s| is) on (?:the )?block(?: marketplace)?"
)
_BLOCK_LOG_RE = re.compile(
    r"\b(?:show (?:my )?block logs?|block logs?|who matched(?: with me)?|what matched|"
    r"block radar|my matches on (?:the )?block)\b",
    re.I,
)
_CHANGE_NAME_RE = re.compile(
    r"\b(?:change my name|update my name|rename me|call me|my name is|add my name)\b",
    re.I,
)
_EDIT_CLAIM_RE = re.compile(
    r"\b(?:remove|delete|drop|clear|get rid of)\b",
    re.I,
)
_PROFILE_ACK_RE = re.compile(
    r"^\s*(?:ok\s+)?(?:that'?s me|that is me|sounds? good|looks? good|correct|"
    r"good to go|yes that'?s (?:me|right))[\s!.?]*$",
    re.I,
)
_ATTR_FILTER_STOP = frozenset({
    "find", "a", "an", "the", "with", "on", "my", "block", "which", "are", "of",
    "near", "me", "who", "speak", "for", "to", "looking", "lookingfor", "some",
    "any", "good", "know", "want", "please", "can", "you", "help",
})


def is_profile_acknowledgment(msg: str) -> bool:
    return bool(_PROFILE_ACK_RE.match(str(msg or "").strip()))


def is_block_activity_browse(msg: str) -> bool:
    """Neighbor marketplace browse — NOT the user's personal block log."""
    return bool(re.search(rf"\b(?:{_BLOCK_BROWSE_PHRASE})\b", str(msg or ""), re.I))


def phrase_linear_intent(msg: str) -> str | None:
    """Narrow policy overrides only — NOT open-ended routing.

    Flash (discovery_slots) classifies find neighbors, swap/meet/tip, heritage filters,
    and all natural phrasing. Regex here is only for structural cases where a wrong AI
    label causes bad side effects (claim pollution, block-log vs marketplace confusion).
    """
    text = str(msg or "").strip()
    if not text:
        return None
    if _PROFILE_ACK_RE.match(text):
        return "identity.complete_profile"
    if _EDIT_CLAIM_RE.search(text):
        return "identity.edit_claim"
    if _CHANGE_NAME_RE.search(text):
        return "settings.change_name"
    if _BLOCK_LOG_RE.search(text) and not is_block_activity_browse(text):
        return "discovery.block_log"
    return None


def attr_filter_tokens(filter_text: str) -> list[str]:
    """Tokenize attr filter for multi-claim AND matching."""
    cleaned = re.sub(r"^\s*(?:find|looking for)\s+", "", str(filter_text or ""), flags=re.I)
    cleaned = re.sub(r"[,.]", " ", cleaned)
    words = re.findall(r"[a-z0-9]+", cleaned.lower())
    out: list[str] = []
    for w in words:
        if len(w) < 2 or w in _ATTR_FILTER_STOP:
            continue
        if w.endswith("s") and len(w) > 3:
            singular = w[:-1]
            if singular not in _ATTR_FILTER_STOP and singular not in out:
                out.append(singular)
        if w not in out:
            out.append(w)
    return out[:8]


def normalize_attr_filter_text(msg: str, slots: dict[str, Any] | None = None) -> str:
    raw = ""
    if slots:
        raw = str(slots.get("attr_filter") or slots.get("identity_snippet") or "").strip()
    if not raw:
        raw = str(msg or "").strip()
    raw = re.sub(r"^\s*(?:find|looking for)\s+", "", raw, flags=re.I).strip()
    return raw[:200]


def normalize_linear_intent(raw: str | None) -> str | None:
    intent = str(raw or "").strip().lower()
    if intent in LINEAR_INTENTS:
        return intent
    return None


def slots_linear_intent(slots: dict[str, Any]) -> str | None:
    """Resolve canonical Layer 1 intent from classifier slots."""
    explicit = normalize_linear_intent(slots.get("linear_intent"))
    if explicit:
        return explicit
    goal = str(slots.get("goal") or "none").lower()
    if goal == "save_signal":
        sig = str(slots.get("signal_intent") or "").lower()
        for linear, signal in SIGNAL_INTENT_BY_LINEAR.items():
            if signal == sig:
                return linear
        return "looking.swap"
    mapped = _GOAL_TO_LINEAR.get(goal)
    if mapped:
        return mapped
    return None


def _apply_discovery_linear_slots(out: dict[str, Any], linear: str, *, msg: str) -> None:
    """Derive goal/attr_filter from AI linear_intent (not regex)."""
    if linear == "discovery.find_peers":
        out.setdefault("goal", "peers")
        out["in_discovery"] = True
    elif linear == "discovery.find_by_attrs":
        out.setdefault("goal", "peers")
        out["in_discovery"] = True
        filt = str(out.get("attr_filter") or "").strip() or normalize_attr_filter_text(msg, out)
        if filt:
            out["attr_filter"] = filt
    elif linear == "discovery.find_in_block":
        out.setdefault("goal", "peers")
        out["in_discovery"] = True
    elif linear == "discovery.block_log":
        out["goal"] = "show_block_log"
        out["in_discovery"] = True
    elif linear in ("identity.edit_claim", "identity.complete_profile"):
        out["goal"] = "chat"
        out["in_discovery"] = False
    elif linear == "settings.change_name":
        out["goal"] = "chat"
        out["in_discovery"] = False


def enrich_slots(slots: dict[str, Any], *, msg: str = "") -> dict[str, Any]:
    """Derive linear_intent, goal, and signal_intent for handlers."""
    out = dict(slots)
    phrase = phrase_linear_intent(msg) if msg else None
    ai_linear = normalize_linear_intent(out.get("linear_intent"))
    ai_conf = float(out.get("confidence", 0.0))

    if phrase in PHRASE_POLICY_OVERRIDES:
        out["linear_intent"] = phrase
        out["confidence"] = max(ai_conf, 0.9)
        _apply_discovery_linear_slots(out, phrase, msg=msg)
    elif ai_linear:
        out["linear_intent"] = ai_linear
        _apply_discovery_linear_slots(out, ai_linear, msg=msg)
    elif phrase:
        out["linear_intent"] = phrase
        out["confidence"] = max(ai_conf, 0.85)
        _apply_discovery_linear_slots(out, phrase, msg=msg)

    linear = slots_linear_intent(out)
    if linear:
        out["linear_intent"] = linear
    if linear in SIGNAL_INTENT_BY_LINEAR:
        out["signal_intent"] = SIGNAL_INTENT_BY_LINEAR[linear]
        out.setdefault("goal", "save_signal")
    elif linear:
        for goal, li in _GOAL_TO_LINEAR.items():
            if li == linear:
                out.setdefault("goal", goal)
                break
    if str(out.get("goal") or "") == "save_signal":
        sig = str(out.get("signal_intent") or "").strip().lower()
        if sig:
            for li, si in SIGNAL_INTENT_BY_LINEAR.items():
                if si == sig:
                    out.setdefault("linear_intent", li)
                    break
    return out


def intent_confidence_met(slots: dict[str, Any], linear_intent: str) -> bool:
    conf = float(slots.get("confidence", 0.0))
    threshold = INTENT_CONFIDENCE.get(linear_intent, LAYER1_DEFAULT_CONFIDENCE)
    return conf >= threshold


def slots_want_layer1_handling(
    slots: dict[str, Any],
    *,
    routing_phase: str = "",
) -> bool:
    """Should discovery route handle this turn (Layer 1 explicit intent)?"""
    enriched = enrich_slots(slots)
    linear = slots_linear_intent(enriched)
    if linear in (
        "identity.show_my_profile",
        "identity.add_claim",
        "identity.edit_claim",
        "identity.complete_profile",
        "help.what_can_you_do",
        "help.who_are_you",
        "settings.change_name",
        "settings.change_zip",
        "settings.notification_prefs",
        "discovery.find_in_block",
        "discovery.block_log",
        "discovery.show_peer_profile",
        "discovery.explain_peer_match",
    ):
        return intent_confidence_met(enriched, linear)
    goal = str(enriched.get("goal") or "none")
    if goal in ("chat", "none"):
        return False
    if goal in ("profile_photo", "login", "logout"):
        return False
    if not linear:
        return False
    if linear == "discovery.find_in_block":
        return intent_confidence_met(enriched, linear)
    if linear in LOOKING_SHARING_INTENTS:
        return intent_confidence_met(enriched, linear)
    if linear in (
        "social.propose_intro",
        "social.list_intros",
        "discovery.block_log",
        "tier.send_nudge",
        "tier.respond_nudge",
    ):
        return intent_confidence_met(enriched, linear)
    if goal == "continue":
        phase = routing_phase or "listening"
        if phase == "preview":
            return False
        if phase in ("need_zip", "need_identity", "need_display_name"):
            return True
        return enriched.get("in_discovery") and float(enriched.get("confidence", 0.0)) >= 0.6
    if linear in ("discovery.find_peers", "discovery.find_by_attrs", "discovery.find_activities"):
        phase = routing_phase or "listening"
        if phase in ("listening", "") and linear == "discovery.find_peers":
            return intent_confidence_met(enriched, linear)
        if enriched.get("in_discovery"):
            return float(enriched.get("confidence", 0.0)) >= 0.5
        return intent_confidence_met(enriched, linear)
    if goal == "verify" or linear == "auth.signup_phone":
        return intent_confidence_met(enriched, "auth.signup_phone")
    if goal == "rsvp":
        return float(enriched.get("confidence", 0.0)) >= 0.5
    return intent_confidence_met(enriched, linear)
