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
    # System
    "system.out_of_scope",
    "system.unsafe",
    "system.medical",
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

INTENT_LANES: frozenset[str] = frozenset({
    "auth",
    "help",
    "settings",
    "identity",
    "discovery",
    "recommendation",
    "local_signal",
    "social",
    "tier",
    "safety",
    "out_of_scope",
    "chat",
})

INTENT_ACTIONS: frozenset[str] = frozenset({
    "signup",
    "login",
    "logout",
    "upload_photo",
    "what_can_you_do",
    "who_are_you",
    "change_name",
    "change_zip",
    "notification_prefs",
    "add_claim",
    "edit_claim",
    "complete_profile",
    "show_profile",
    "find_peers",
    "find_by_attrs",
    "find_in_block",
    "find_activities",
    "block_log",
    "show_peer_profile",
    "explain_peer_match",
    "recommend_value",
    "swap",
    "meet",
    "tip",
    "host",
    "send_nudge",
    "respond_nudge",
    "list_intros",
    "propose_intro",
    "unsafe",
    "medical",
    "out_of_scope",
    "chat",
})

INTENT_DIRECTIONS: frozenset[str] = frozenset({"seek", "offer", "share", "send", "respond"})
INTENT_SCOPES: frozenset[str] = frozenset({"neighbor", "event", "local_signal", "mixed", "block"})

_LINEAR_TO_HIERARCHICAL: dict[str, dict[str, str | None]] = {
    "discovery.find_peers": {"intent_lane": "discovery", "intent_action": "find_peers"},
    "discovery.find_by_attrs": {"intent_lane": "discovery", "intent_action": "find_by_attrs"},
    "discovery.find_in_block": {"intent_lane": "discovery", "intent_action": "find_in_block", "intent_scope": "block"},
    "discovery.find_activities": {"intent_lane": "discovery", "intent_action": "find_activities", "intent_scope": "event"},
    "discovery.block_log": {"intent_lane": "discovery", "intent_action": "block_log", "intent_scope": "block"},
    "discovery.show_peer_profile": {"intent_lane": "discovery", "intent_action": "show_peer_profile", "intent_scope": "neighbor"},
    "discovery.explain_peer_match": {"intent_lane": "discovery", "intent_action": "explain_peer_match", "intent_scope": "neighbor"},
    "identity.add_claim": {"intent_lane": "identity", "intent_action": "add_claim"},
    "identity.edit_claim": {"intent_lane": "identity", "intent_action": "edit_claim"},
    "identity.complete_profile": {"intent_lane": "identity", "intent_action": "complete_profile"},
    "identity.show_my_profile": {"intent_lane": "identity", "intent_action": "show_profile"},
    "looking.swap": {"intent_lane": "local_signal", "intent_action": "swap", "intent_direction": "seek"},
    "sharing.swap": {"intent_lane": "local_signal", "intent_action": "swap", "intent_direction": "offer"},
    "looking.meet": {"intent_lane": "local_signal", "intent_action": "meet", "intent_direction": "seek"},
    "sharing.host": {"intent_lane": "local_signal", "intent_action": "host", "intent_direction": "offer"},
    "looking.tip": {"intent_lane": "local_signal", "intent_action": "tip", "intent_direction": "seek"},
    "sharing.tip": {"intent_lane": "local_signal", "intent_action": "tip", "intent_direction": "share"},
    "tier.send_nudge": {"intent_lane": "tier", "intent_action": "send_nudge", "intent_direction": "send"},
    "tier.respond_nudge": {"intent_lane": "tier", "intent_action": "respond_nudge", "intent_direction": "respond"},
    "social.list_intros": {"intent_lane": "social", "intent_action": "list_intros"},
    "social.propose_intro": {"intent_lane": "social", "intent_action": "propose_intro"},
    "auth.signup_phone": {"intent_lane": "auth", "intent_action": "signup"},
    "auth.login_phone": {"intent_lane": "auth", "intent_action": "login"},
    "auth.logout": {"intent_lane": "auth", "intent_action": "logout"},
    "auth.upload_photo": {"intent_lane": "auth", "intent_action": "upload_photo"},
    "settings.change_name": {"intent_lane": "settings", "intent_action": "change_name"},
    "settings.change_zip": {"intent_lane": "settings", "intent_action": "change_zip"},
    "settings.notification_prefs": {"intent_lane": "settings", "intent_action": "notification_prefs"},
    "help.what_can_you_do": {"intent_lane": "help", "intent_action": "what_can_you_do"},
    "help.who_are_you": {"intent_lane": "help", "intent_action": "who_are_you"},
    "system.out_of_scope": {"intent_lane": "out_of_scope", "intent_action": "out_of_scope"},
    "system.unsafe": {"intent_lane": "safety", "intent_action": "unsafe"},
    "system.medical": {"intent_lane": "safety", "intent_action": "medical"},
}

_HIERARCHICAL_TO_LINEAR: dict[tuple[str, str, str | None], str] = {
    (str(v.get("intent_lane")), str(v.get("intent_action")), v.get("intent_direction")): k
    for k, v in _LINEAR_TO_HIERARCHICAL.items()
}
_HIERARCHICAL_TO_LINEAR.update({
    ("auth", "signup", None): "auth.signup_phone",
    ("auth", "login", None): "auth.login_phone",
    ("identity", "show_profile", None): "identity.show_my_profile",
})

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
    "out_of_scope": "system.out_of_scope",
    "unsafe": "system.unsafe",
    "medical": "system.medical",
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
    # The decline-vs-clarify gate: a confident out-of-scope read is declined outright;
    # below this the router asks one clarifying question instead of guessing.
    "system.out_of_scope": 0.6,
    # Safety wins early; the route layer also has a regex backstop, so keep the bar low.
    "system.unsafe": 0.5,
    # A health/medical concern gets the safety redirect early — err toward the gentle
    # decline+doctor-handoff over a mis-lane, so keep the bar low like the other rails.
    "system.medical": 0.5,
}

for _li in LOOKING_SHARING_INTENTS:
    INTENT_CONFIDENCE[_li] = 0.55

# Structural hosting vs neighbor-search — NOT open-ended discovery regex.
_HOSTING_PLAN_RE = re.compile(
    r"\b(?:want|wanna|plan(?:ning)?|host(?:ing)?|throw|organi[sz]e|set up|creat(?:e|ing)|put together)\b.{0,60}"
    r"\b(?:coffee|brunch|breakfast|meetup|meet-up|playdate|play date|gathering|get-?together|"
    r"picnic|event|party|hang ?out|block hang|barbecue|bbq|potluck|cookout)\b",
    re.I,
)
_FIND_NEIGHBORS_RE = re.compile(
    r"\b(?:find|show me|looking for)\s+(?:\w+\s+){0,4}"
    r"(?:moms?|dads?|parents?|neighbors?|people|families)\b",
    re.I,
)
_HOSTING_EVENT_NOUNS = frozenset({
    "coffee",
    "brunch",
    "breakfast",
    "meetup",
    "playdate",
    "gathering",
    "picnic",
    "morning",
})
_TIP_SHARE_RE = re.compile(
    r"\b(?:"
    r"(?:dr\.?\s+)?[\w'.-]+\s+is\s+(?:a\s+)?(?:great|good|wonderful|amazing|excellent)\s+"
    r"(?:doctor|dentist|pediatrician|tutor|teacher|plumber|therapist|clinic)|"
    # First-person offer only — "I recommend X" shares; "can you suggest me X" is a SEEK.
    r"i\s+(?:recommend|suggest)\s+(?!me\b|us\b)(?:dr\.?\s+)?[\w'.-]+|"
    r"try\s+dr\.?\s+[\w'.-]+|"
    r"my\s+favorite\s+(?:doctor|dentist|restaurant|pizza|place|spot)"
    r")\b",
    re.I,
)

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
# NOTE: "get rid of" deliberately excluded — it means decluttering/giving away items
# (a swap offer), not editing a profile claim. It used to hijack "get rid of old baby
# stuff" into identity.edit_claim. Claim edits read as remove/delete a stated attribute.
_EDIT_CLAIM_RE = re.compile(
    r"\b(?:remove|delete|drop|clear)\b",
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


def normalize_intent_lane(raw: str | None) -> str | None:
    lane = str(raw or "").strip().lower()
    return lane if lane in INTENT_LANES else None


def normalize_intent_action(raw: str | None) -> str | None:
    action = str(raw or "").strip().lower()
    return action if action in INTENT_ACTIONS else None


def normalize_intent_direction(raw: str | None) -> str | None:
    direction = str(raw or "").strip().lower()
    return direction if direction in INTENT_DIRECTIONS else None


def normalize_intent_scope(raw: str | None) -> str | None:
    scope = str(raw or "").strip().lower()
    return scope if scope in INTENT_SCOPES else None


def hierarchical_intent_from_linear(linear_intent: str | None) -> dict[str, str | None]:
    linear = normalize_linear_intent(linear_intent)
    if not linear:
        return {"intent_lane": None, "intent_action": None, "intent_direction": None, "intent_scope": None}
    base = dict(_LINEAR_TO_HIERARCHICAL.get(linear) or {})
    return {
        "intent_lane": base.get("intent_lane"),
        "intent_action": base.get("intent_action"),
        "intent_direction": base.get("intent_direction"),
        "intent_scope": base.get("intent_scope"),
    }


def linear_intent_from_hierarchy(slots: dict[str, Any]) -> str | None:
    lane = normalize_intent_lane(slots.get("intent_lane"))
    action = normalize_intent_action(slots.get("intent_action"))
    direction = normalize_intent_direction(slots.get("intent_direction"))
    if not lane or not action:
        return None
    return _HIERARCHICAL_TO_LINEAR.get((lane, action, direction)) or _HIERARCHICAL_TO_LINEAR.get(
        (lane, action, None)
    )


def stamp_hierarchical_intent(slots: dict[str, Any]) -> dict[str, Any]:
    """Populate hierarchical intent fields while preserving legacy linear_intent."""
    out = dict(slots)
    linear = normalize_linear_intent(out.get("linear_intent")) or linear_intent_from_hierarchy(out)
    if linear:
        out["linear_intent"] = linear
        hierarchical = hierarchical_intent_from_linear(linear)
        # Keep legacy and hierarchical fields internally consistent during migration.
        out["intent_lane"] = hierarchical["intent_lane"]
        out["intent_action"] = hierarchical["intent_action"]
        out["intent_direction"] = hierarchical["intent_direction"]
        out["intent_scope"] = hierarchical["intent_scope"]
        return out

    lane = normalize_intent_lane(out.get("intent_lane"))
    action = normalize_intent_action(out.get("intent_action"))
    direction = normalize_intent_direction(out.get("intent_direction"))
    scope = normalize_intent_scope(out.get("intent_scope"))
    out["intent_lane"] = lane
    out["intent_action"] = action
    out["intent_direction"] = direction
    out["intent_scope"] = scope
    return out


def slots_linear_intent(slots: dict[str, Any]) -> str | None:
    """Resolve canonical Layer 1 intent from classifier slots."""
    explicit = normalize_linear_intent(slots.get("linear_intent"))
    if explicit:
        return explicit
    hierarchical = linear_intent_from_hierarchy(slots)
    if hierarchical:
        return hierarchical
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
        out.pop("signal_intent", None)
        out.pop("signal_detail", None)
        out.pop("signal_category", None)
    elif linear == "social.propose_intro":
        out["goal"] = "propose_intro"
        out["in_discovery"] = True
    elif linear in ("identity.edit_claim", "identity.complete_profile", "identity.show_my_profile"):
        out["goal"] = "chat"
        out["in_discovery"] = False
    elif linear == "discovery.show_peer_profile":
        out["goal"] = "chat"
        out["in_discovery"] = False
    elif linear == "identity.add_claim":
        out["goal"] = "chat"
        out["in_discovery"] = False
    elif linear == "settings.change_name":
        out["goal"] = "chat"
        out["in_discovery"] = False


def utterance_indicates_hosting_plan(msg: str) -> bool:
    """Structural event-planning phrasing — not neighbor trait search."""
    text = str(msg or "").strip()
    if not text or _FIND_NEIGHBORS_RE.search(text):
        return False
    if utterance_indicates_tip_share(text):
        return False
    return bool(_HOSTING_PLAN_RE.search(text))


def utterance_indicates_tip_share(msg: str) -> bool:
    """Structural recommendation phrasing — not hosting or neighbor search."""
    text = str(msg or "").strip()
    if not text or _FIND_NEIGHBORS_RE.search(text):
        return False
    return bool(_TIP_SHARE_RE.search(text))


_TIP_SEEK_UTTERANCE_RE = re.compile(
    r"\b(?:do you know|know (?:of )?a good|any good|recommend(?:ation)?|"
    r"(?:can you |could you |please )?suggest|"
    r"looking for (?:a )?good|is there (?:a )?good)\b",
    re.I,
)
_TIP_SEEK_SERVICE_RE = re.compile(
    r"\b(?:doctor|pediatrician|dentist|plumber|tutor|teacher|lawyer|vet|"
    r"restaurant|pizza|contractor|handyman|physician|nanny|babysitter|"
    r"daycare|electrician|mechanic|barber|hairdresser|salon)s?\b",
    re.I,
)
_SWAP_SEEK_ITEM_RE = re.compile(
    r"\b(?:computer|laptop|bike|bicycle|boots|toy|stroller|phone|ipad|tablet|"
    r"rain\s+coat|jacket|car\s+seat|crib|high\s+chair)\b",
    re.I,
)
# First-person self-description ("I am a teacher", "I'm a doctor") — identity, not a seek.
_SELF_DESCRIPTION_RE = re.compile(
    r"\b(?:i'?m|i am|i've been|i have been|we're|we are)\b",
    re.I,
)
# Request/seek cues that turn a service noun into an actual tip seek.
_TIP_SEEK_CUE_RE = re.compile(
    r"\b(?:do you know|know (?:of )?(?:a|any)|any good|recommend|suggest|"
    r"looking for|need|want|find|is there|where (?:can|do)|got a|have a)\b",
    re.I,
)


def utterance_indicates_tip_seek(msg: str) -> bool:
    """Seeking a local service/places recommendation — not sharing a tip or finding moms."""
    text = str(msg or "").strip()
    if not text or _FIND_NEIGHBORS_RE.search(text):
        return False
    if utterance_indicates_tip_share(text):
        return False
    # "I am a teacher" / "I'm a doctor" describes the user — it is an identity claim,
    # NOT a request for a teacher/doctor. Only treat it as a seek if a request cue exists.
    if _SELF_DESCRIPTION_RE.search(text) and not _TIP_SEEK_CUE_RE.search(text):
        return False
    if _TIP_SEEK_SERVICE_RE.search(text):
        return True
    return bool(_TIP_SEEK_UTTERANCE_RE.search(text) and _TIP_SEEK_SERVICE_RE.search(text))


_SWAP_OFFER_PHRASE_RE = re.compile(
    r"\b(?:giv(?:e|ing)\s*(?:up|away)?|gave\s*away|giveaway|donat\w*|"
    r"offer(?:ing)?|hand(?:ing)?\s+(?:down|off)|get\s+rid\s+of)\b",
    re.I,
)


def utterance_indicates_swap_offer(msg: str) -> bool:
    """Offering/giving away a physical item — not a seek (must not downgrade to seek)."""
    text = str(msg or "").strip()
    if not text:
        return False
    return bool(_SWAP_OFFER_PHRASE_RE.search(text) and _SWAP_SEEK_ITEM_RE.search(text))


def utterance_indicates_swap_seek(msg: str) -> bool:
    """Seeking/borrowing a physical item — not neighbor identity browse, not an offer."""
    text = str(msg or "").strip()
    if not text or _FIND_NEIGHBORS_RE.search(text):
        return False
    if utterance_indicates_tip_seek(text) or utterance_indicates_hosting_plan(text):
        return False
    if utterance_indicates_swap_offer(text):
        return False
    if not re.search(r"\b(?:looking for|need(?:ing)?|want)\b", text, re.I):
        return False
    return bool(_SWAP_SEEK_ITEM_RE.search(text))


def slots_indicate_tip_share_signal(slots: dict[str, Any]) -> bool:
    signal_intent = str(slots.get("signal_intent") or "").strip().lower()
    if signal_intent == "tip_share":
        return True
    linear = normalize_linear_intent(slots.get("linear_intent"))
    return linear == "sharing.tip"


def slots_indicate_tip_seek_signal(slots: dict[str, Any]) -> bool:
    signal_intent = str(slots.get("signal_intent") or "").strip().lower()
    if signal_intent == "tip_seek":
        return True
    linear = normalize_linear_intent(slots.get("linear_intent"))
    return linear == "looking.tip"


def slots_indicate_swap_seek_signal(slots: dict[str, Any]) -> bool:
    signal_intent = str(slots.get("signal_intent") or "").strip().lower()
    if signal_intent in ("swap_seek", "swap_offer"):
        return True
    linear = normalize_linear_intent(slots.get("linear_intent"))
    return linear in ("looking.swap", "sharing.swap")


def slots_indicate_hosting_signal(slots: dict[str, Any]) -> bool:
    """AI slots (or reconciled hosting) — hosting/meetup save, not peer discovery."""
    signal_intent = str(slots.get("signal_intent") or "").strip().lower()
    if signal_intent == "host_meet":
        return True
    linear = normalize_linear_intent(slots.get("linear_intent"))
    return linear == "sharing.host"


# Intents the model owns outright — a regex signal reconciler must NEVER reclassify
# these as a swap/tip/host. ("I am a teacher" = identity, not a request for a teacher.)
_AI_OWNED_PREFIXES = ("identity.", "settings.", "help.", "social.", "tier.", "auth.", "system.")


def _reconcile_defer_to_llm(
    out: dict[str, Any],
    target_linear: str,
    *,
    utterance_match: bool = False,
    protect_discovery: bool = False,
) -> bool:
    """Trust the LLM; regex reconcilers are a fallback, not an override.

    Phrasing is infinite, so the LLM is the router. These regex reconcilers exist
    only to rescue the *known* failure mode — the model returns peer/activity
    discovery when the user is really posting a swap/tip/host signal. They must not
    overrule a confident model decision. Returns True to DEFER to the model.
    """
    linear = normalize_linear_intent(out.get("linear_intent"))
    conf = float(out.get("confidence", 0.0))

    # A confident pick of an AI-owned intent (identity/settings/help/social/tier/auth)
    # always wins — even over a "high-precision" regex utterance match. This is the
    # structural fix for regex hijacking the model (e.g. "I am a teacher" → tip_seek).
    if linear and conf >= 0.6 and any(linear.startswith(p) for p in _AI_OWNED_PREFIXES):
        return True

    # The hosting regex is low-precision (any want-verb + any event noun, so it fires on
    # "wanna find ... event"). A confident find-people / browse-activities pick must
    # therefore beat it — even on an utterance match. Scoped to hosting only; the
    # high-precision tip/swap seek regexes still rescue a misclassified peers pick.
    if (
        protect_discovery
        and conf >= 0.6
        and linear
        and (
            linear.startswith("discovery.")
            or str(out.get("goal") or "") in ("peers", "both", "activities")
        )
    ):
        return True

    # A high-precision structural utterance match ("Dr X is a great dentist",
    # "coffee this weekend") may correct the model only among signal lanes.
    if utterance_match:
        return False
    if not linear or linear == target_linear:
        return False
    if conf < 0.6:
        return False
    if linear.startswith("discovery.") or str(out.get("goal") or "") in (
        "peers",
        "both",
        "activities",
    ):
        return False
    return linear in LOOKING_SHARING_INTENTS


def reconcile_tip_share_signal(out: dict[str, Any], *, msg: str) -> None:
    """Recommendation share — wins over misclassified hosting or peer discovery."""
    tip_utterance = utterance_indicates_tip_share(msg)
    tip = tip_utterance or slots_indicate_tip_share_signal(out)
    if not tip:
        return
    if _reconcile_defer_to_llm(out, "sharing.tip", utterance_match=tip_utterance):
        return
    if utterance_indicates_hosting_plan(msg) and not utterance_indicates_tip_share(msg):
        return
    out.pop("attr_filter", None)
    out["goal"] = "save_signal"
    out["in_discovery"] = False
    out["signal_intent"] = "tip_share"
    out["linear_intent"] = "sharing.tip"
    out["confidence"] = max(float(out.get("confidence", 0.0)), 0.85)
    detail = str(out.get("signal_detail") or msg or "").strip()[:500]
    if detail:
        out["signal_detail"] = detail


def reconcile_tip_seek_signal(out: dict[str, Any], *, msg: str) -> None:
    """Local service/places seek — wins over heritage pending and peer browse."""
    if utterance_indicates_tip_share(msg):
        return
    tip_utterance = utterance_indicates_tip_seek(msg)
    tip = tip_utterance or slots_indicate_tip_seek_signal(out)
    if not tip:
        return
    if _reconcile_defer_to_llm(out, "looking.tip", utterance_match=tip_utterance):
        return
    out.pop("attr_filter", None)
    out["goal"] = "save_signal"
    out["in_discovery"] = False
    out["signal_intent"] = "tip_seek"
    out["linear_intent"] = "looking.tip"
    out["confidence"] = max(float(out.get("confidence", 0.0)), 0.85)
    detail = str(out.get("signal_detail") or msg or "").strip()[:500]
    if detail:
        out["signal_detail"] = detail
    if not str(out.get("signal_category") or "").strip():
        low = detail.lower()
        if any(w in low for w in ("doctor", "pediatrician", "dentist", "vet")):
            out["signal_category"] = "health"
        elif any(w in low for w in ("tutor", "teacher", "school")):
            out["signal_category"] = "education"
        elif any(w in low for w in ("restaurant", "pizza", "food")):
            out["signal_category"] = "food"


def _slots_already_swap_offer(slots: dict[str, Any]) -> bool:
    if str(slots.get("signal_intent") or "").strip().lower() == "swap_offer":
        return True
    return normalize_linear_intent(slots.get("linear_intent")) == "sharing.swap"


def reconcile_swap_seek_signal(out: dict[str, Any], *, msg: str) -> None:
    """Item swap/borrow seek — wins over sticky peer browse, but never a real offer."""
    if utterance_indicates_tip_seek(msg) or utterance_indicates_tip_share(msg):
        return
    # An offer (give away / sharing.swap) must not be downgraded to a seek.
    if _slots_already_swap_offer(out) or utterance_indicates_swap_offer(msg):
        return
    # "looking for stroller walk buddies" is a MEET, not a swap — the swap-seek
    # utterance pattern (verb + any item noun) is too blunt to override a
    # confident different-lane LLM pick, so defer without the utterance bypass.
    if _reconcile_defer_to_llm(out, "looking.swap"):
        return
    swap = utterance_indicates_swap_seek(msg) or slots_indicate_swap_seek_signal(out)
    if not swap:
        return
    out.pop("attr_filter", None)
    out["goal"] = "save_signal"
    out["in_discovery"] = False
    out["signal_intent"] = "swap_seek"
    out["linear_intent"] = "looking.swap"
    out["confidence"] = max(float(out.get("confidence", 0.0)), 0.85)
    detail = str(out.get("signal_detail") or msg or "").strip()[:500]
    if detail:
        out["signal_detail"] = detail


def reconcile_hosting_peer_slot_conflict(out: dict[str, Any], *, msg: str) -> None:
    """When Flash mixes heritage attr_filter with hosting, hosting wins."""
    if utterance_indicates_tip_share(msg) or slots_indicate_tip_share_signal(out):
        return
    hosting_utterance = utterance_indicates_hosting_plan(msg)
    hosting = slots_indicate_hosting_signal(out) or hosting_utterance
    if not hosting:
        return
    if _reconcile_defer_to_llm(
        out, "sharing.host", utterance_match=hosting_utterance, protect_discovery=True
    ):
        return
    out.pop("attr_filter", None)
    out["goal"] = "save_signal"
    out["in_discovery"] = False
    out["signal_intent"] = "host_meet"
    out["linear_intent"] = "sharing.host"
    out["confidence"] = max(float(out.get("confidence", 0.0)), 0.85)
    detail = str(out.get("signal_detail") or msg or "").strip()[:500]
    if detail:
        out["signal_detail"] = detail
    when = str(out.get("signal_when") or "").strip()
    if not when:
        from app.signal_capture import _WHEN_HINT

        m = _WHEN_HINT.search(str(msg or ""))
        if m:
            out["signal_when"] = m.group(0).strip()


def enrich_slots(slots: dict[str, Any], *, msg: str = "") -> dict[str, Any]:
    """Derive linear_intent, goal, and signal_intent for handlers."""
    out = stamp_hierarchical_intent(slots)
    phrase = phrase_linear_intent(msg) if msg else None
    ai_linear = normalize_linear_intent(out.get("linear_intent"))
    ai_conf = float(out.get("confidence", 0.0))

    # AI-AUTHORITATIVE routing. A regex phrase intent NEVER overrides the AI anymore — it is
    # only a FALLBACK for when the AI did not confidently classify the turn (and did not ask
    # to clarify). This removes the whole class of "a keyword hijacked the model" bugs
    # (get-rid-of→edit_claim, etc.): the model sees the whole sentence, a keyword sees one
    # word out of context. Two things stay deterministic because they are NOT intent
    # decisions: format parsing (zip/otp/email) and the unsafe safety backstop — both live
    # elsewhere. (See [[no-sticky-flows]] / [[sticky-lane-release-inversion]].)
    if str(out.get("clarify") or "").strip():
        phrase = None  # AI is asking — a keyword must not force a lane.
    ai_confident = bool(ai_linear) and ai_conf >= 0.6

    if ai_confident:
        out["linear_intent"] = ai_linear
        _apply_discovery_linear_slots(out, ai_linear, msg=msg)
    elif phrase:
        out["linear_intent"] = phrase
        out["confidence"] = max(ai_conf, 0.85)
        _apply_discovery_linear_slots(out, phrase, msg=msg)
    elif ai_linear:
        out["linear_intent"] = ai_linear
        _apply_discovery_linear_slots(out, ai_linear, msg=msg)

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
    reconcile_tip_share_signal(out, msg=msg)
    reconcile_tip_seek_signal(out, msg=msg)
    reconcile_swap_seek_signal(out, msg=msg)
    reconcile_hosting_peer_slot_conflict(out, msg=msg)
    if utterance_indicates_tip_share(msg):
        reconcile_tip_share_signal(out, msg=msg)
    if str(out.get("goal") or "") == "show_block_log":
        out.pop("signal_intent", None)
        out.pop("signal_detail", None)
        out.pop("signal_category", None)
    return stamp_hierarchical_intent(out)


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
    if linear in ("system.out_of_scope", "system.unsafe", "system.medical"):
        # Discovery owns these at ANY confidence so they never fall through to the
        # find_peers funnel: out_of_scope declines/clarifies, unsafe refuses, medical
        # gives the safety redirect. The route layer makes the actual call (and unsafe
        # has a regex backstop too).
        return True
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
