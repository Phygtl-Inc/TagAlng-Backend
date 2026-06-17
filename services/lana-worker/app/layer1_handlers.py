"""Layer 1 intent handlers — identity, block summary, settings, help."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from app.auth import service_client
from app.claims_persist import (
    dismiss_claims_from_edit_message,
    extract_and_upsert_claims_from_message,
    extract_display_name_reply,
    persist_profile_patch,
)
from app.claim_search import (
    ClaimFilter,
    filters_to_rpc_payload,
    parse_claim_filters,
)
from app.layer1_intents import (
    LINEAR_INTENTS,
    attr_filter_tokens,
    is_block_activity_browse,
    slots_linear_intent,
)
from app.local_signals import fetch_my_block_log
from app.supabase_rpc import call_rpc

HELP_WHAT_CAN_YOU_DO = (
    "I'm Lana — your block concierge. I can help you find neighbors like you, "
    "swap or borrow items, meet for playgroups, share local tips, host small gatherings, "
    "and make warm introductions when you're ready. What would you like to start with?"
)

HELP_WHO_ARE_YOU = (
    "I'm Lana — your local concierge for TagAlng. I remember who you are, "
    "what's happening on your block, and I help connect you with neighbors at your pace. "
    "Nothing leaves your block without you saying so."
)

_BUCKET_ORDER = (
    "heritage",
    "stage",
    "activity",
    "interest",
    "lifestyle",
    "language",
    "faith",
    "vicinity",
    "general",
)


def fetch_identity_dashboard(user_jwt: str) -> dict[str, Any]:
    raw = call_rpc(user_jwt, "get_my_profile_dashboard", {})
    return raw if isinstance(raw, dict) else {}


def _sort_claims_for_display(claims: list[dict[str, Any]], *, limit: int = 18) -> list[dict[str, Any]]:
    bucket_rank = {bucket: idx for idx, bucket in enumerate(_BUCKET_ORDER)}
    seen: set[str] = set()
    ordered: list[dict[str, Any]] = []

    def rank(claim: dict[str, Any]) -> tuple[int, float]:
        bucket = str(claim.get("bucket") or "general").strip()
        try:
            confidence = float(claim.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        return (bucket_rank.get(bucket, len(_BUCKET_ORDER)), -confidence)

    for claim in sorted(claims, key=rank):
        if not isinstance(claim, dict):
            continue
        label = str(claim.get("label") or claim.get("concept") or "").strip()
        key = label.lower()
        if not key or key in seen:
            continue
        seen.add(key)
        ordered.append(claim)
        if len(ordered) >= limit:
            break
    return ordered


def format_identity_profile_reply(dashboard: dict[str, Any]) -> str:
    profile = dashboard.get("profile") if isinstance(dashboard.get("profile"), dict) else {}
    claims = dashboard.get("claims") if isinstance(dashboard.get("claims"), list) else []
    nick = str(profile.get("nickname") or profile.get("full_name") or "").strip()
    block = str(profile.get("block_display_name") or profile.get("home_zip") or "").strip()
    summary = str(dashboard.get("mapped_summary") or "").strip()

    lines: list[str] = []
    if nick:
        lines.append(f"Here's what I have for {nick}:")
    else:
        lines.append("Here's what I know about you so far:")
    if block:
        lines.append(f"Home block: {block}.")
    if summary:
        lines.append(summary)
    elif claims:
        by_bucket: dict[str, list[str]] = {}
        seen_labels: set[str] = set()
        for c in claims:
            if not isinstance(c, dict):
                continue
            label = str(c.get("label") or c.get("concept") or "").strip()
            if not label:
                continue
            key = label.lower()
            if key in seen_labels:
                continue
            seen_labels.add(key)
            bucket = str(c.get("bucket") or "general").strip()
            by_bucket.setdefault(bucket, []).append(label)
        claim_bits: list[str] = []
        for bucket in _BUCKET_ORDER:
            labels = by_bucket.get(bucket) or []
            if labels:
                claim_bits.append(", ".join(labels[:4]))
        if claim_bits:
            lines.append(" · ".join(claim_bits[:7]))
    if not summary and not claims:
        lines.append(
            "I don't have identity threads saved yet — tell me a bit about yourself "
            "(heritage, life stage, interests) and I'll map you."
        )
    else:
        lines.append(f"I have {len(claims)} identity thread{'s' if len(claims) != 1 else ''} on file.")
    lines.append("Want to add or change anything?")
    return " ".join(lines)


def stamp_identity_profile_ctx(ctx: dict[str, Any], dashboard: dict[str, Any]) -> None:
    claims = dashboard.get("claims") if isinstance(dashboard.get("claims"), list) else []
    profile = dashboard.get("profile") if isinstance(dashboard.get("profile"), dict) else {}
    sorted_claims = _sort_claims_for_display([c for c in claims if isinstance(c, dict)])
    ctx["identity_profile"] = {
        "profile": profile,
        "mapped_summary": dashboard.get("mapped_summary"),
        "claims": sorted_claims,
        "stats": dashboard.get("stats") if isinstance(dashboard.get("stats"), dict) else {},
    }
    ctx["active_intent"] = "identity.show_my_profile"


def format_block_summary_reply(
    *,
    block_name: str,
    neighbor_count: int,
    match_count: int,
    block_state: str | None = None,
    active_signal_count: int = 0,
    browse_mode: bool = False,
) -> str:
    if browse_mode:
        sig_bit = (
            f"{active_signal_count} neighbor ask{'s' if active_signal_count != 1 else ''} "
            f"or offer{'s' if active_signal_count != 1 else ''} active on your block right now "
            "(swaps, meetups, tips)."
            if active_signal_count > 0
            else "No public neighbor asks or offers on your block right now."
        )
        return (
            f"On {block_name}: {sig_bit} "
            "I can't list every neighbor's post yet — that's coming. "
            "Say **show my block log** for matches tied to *your* posts, "
            "or tell me what you're looking for or offering."
        )
    state_bit = ""
    if block_state and block_state not in ("live", "unknown"):
        state_bit = f" Block status: {block_state.replace('_', ' ')}."
    match_bit = ""
    if match_count > 0:
        match_bit = f" You have {match_count} potential match{'es' if match_count != 1 else ''} in your block log."
    elif neighbor_count > 0:
        match_bit = " Your block log is quiet — ask for something or offer to help neighbors and matches will land here."
    return (
        f"On {block_name}: about {neighbor_count} neighbor{'s' if neighbor_count != 1 else ''} "
        f"on your block right now.{state_bit}{match_bit} "
        "Want to find people, post a swap, or see your block log?"
    )


def _count_neighbors_on_block(block_id: str) -> int:
    try:
        res = (
            service_client()
            .table("users")
            .select("id", count="exact")
            .eq("home_block_id", block_id)
            .limit(1)
            .execute()
        )
        return int(res.count or 0)
    except Exception:
        return 0


def fetch_block_summary(user_jwt: str, *, block_id: str | None = None) -> dict[str, Any]:
    try:
        raw = call_rpc(user_jwt, "get_block_summary", {})
        if isinstance(raw, dict):
            return {
                "block_id": raw.get("block_id") or block_id,
                "block_name": str(raw.get("block_name") or "your block"),
                "block_state": raw.get("block_state"),
                "neighbor_count": int(raw.get("neighbor_count") or 0),
                "match_count": int(raw.get("active_match_count") or 0),
                "active_signal_count": int(raw.get("active_signal_count") or 0),
            }
    except HTTPException:
        pass
    profile_raw = call_rpc(user_jwt, "get_my_profile", {})
    profile = profile_raw if isinstance(profile_raw, dict) else {}
    bid = block_id or profile.get("home_block_id")
    block_name = str(profile.get("block_display_name") or profile.get("home_zip") or "your block")
    block_state = str(profile.get("block_state") or "").strip() or None
    neighbor_count = _count_neighbors_on_block(str(bid)) if bid else 0
    match_count = 0
    try:
        match_count = len(fetch_my_block_log(user_jwt))
    except HTTPException:
        pass
    return {
        "block_id": bid,
        "block_name": block_name,
        "block_state": block_state,
        "neighbor_count": neighbor_count,
        "match_count": match_count,
        "active_signal_count": 0,
    }


def _fetch_peers_single_attr(user_jwt: str, token: str, *, limit: int = 20) -> list[dict[str, Any]]:
    raw = call_rpc(
        user_jwt,
        "find_peers_by_attr_filter",
        {"p_filter_text": token, "p_limit": limit},
    )
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    return []


def _fetch_peers_by_claim_filters_rpc(
    user_jwt: str,
    filters: list[ClaimFilter],
    *,
    limit: int = 5,
) -> list[dict[str, Any]] | None:
    payload = filters_to_rpc_payload(filters)
    if not payload:
        return []
    try:
        raw = call_rpc(
            user_jwt,
            "find_peers_by_claim_filters",
            {"p_filters": payload, "p_limit": limit},
        )
    except HTTPException:
        return None
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    return []


def _fetch_peers_token_and(user_jwt: str, filter_text: str, *, limit: int = 5) -> list[dict[str, Any]]:
    tokens = attr_filter_tokens(filter_text)
    if not tokens:
        return []

    peer_maps: list[dict[str, dict[str, Any]]] = []
    for tok in tokens:
        rows = _fetch_peers_single_attr(user_jwt, tok, limit=20)
        peer_maps.append(
            {str(r.get("peer_user_id") or ""): r for r in rows if r.get("peer_user_id")}
        )

    if not peer_maps or not peer_maps[0]:
        return []

    common_ids = set(peer_maps[0].keys())
    for pm in peer_maps[1:]:
        common_ids &= set(pm.keys())

    out: list[dict[str, Any]] = []
    for pid in common_ids:
        base = dict(peer_maps[0][pid])
        labels = {
            str(peer_maps[i][pid].get("matching_peer_label") or "")
            for i in range(len(peer_maps))
            if pid in peer_maps[i]
        }
        labels.discard("")
        if labels:
            base["matching_peer_label"] = " · ".join(sorted(labels))
        out.append(base)
    return out[:limit]


def summarize_partial_claim_matches(
    user_jwt: str,
    filters: list[ClaimFilter],
) -> str | None:
    parts: list[str] = []
    for f in filters:
        for term in f.terms[:3]:
            rows = _fetch_peers_single_attr(user_jwt, term, limit=15)
            if not rows:
                continue
            n = len(rows)
            parts.append(f"{n} neighbor{'s' if n != 1 else ''} matching \"{term}\"")
            break
    if not parts:
        return None
    return "I see " + ", ".join(parts) + ", but no one with all of those yet."


def fetch_peers_by_attr_filter(
    user_jwt: str,
    filter_text: str,
    *,
    limit: int = 5,
    slots: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    filters = parse_claim_filters(filter_text, slots)
    if filters:
        structured = _fetch_peers_by_claim_filters_rpc(user_jwt, filters, limit=limit)
        if structured is not None:
            return structured[:limit]
    return _fetch_peers_token_and(user_jwt, filter_text, limit=limit)


def peers_to_match_rows(
    peers: list[dict[str, Any]],
    *,
    phone_verified: bool,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in peers[:8]:
        if not isinstance(row, dict):
            continue
        nick = str(row.get("nickname") or "").strip() or None
        out.append(
            {
                "peer_user_id": row.get("peer_user_id"),
                "nickname": nick,
                "avatar_url": row.get("avatar_url"),
                "similarity_score": row.get("similarity_score") or 0.85,
                "matching_peer_label": str(row.get("matching_peer_label") or "shared traits"),
                "matching_peer_concept": row.get("matching_peer_concept"),
                "has_exact_concept_match": bool(row.get("has_exact_concept_match")),
                "preview": not phone_verified or not nick,
            }
        )
    return out


def format_peer_detail_reply(
    peer: dict[str, Any],
    *,
    index: int | None = None,
) -> str:
    label = str(peer.get("matching_peer_label") or "shared interests on your block").strip()
    nick = str(peer.get("nickname") or "").strip()
    preview = bool(peer.get("preview", True))
    idx_label = f"Neighbor {(index or 0) + 1}" if index is not None else "This neighbor"
    who = nick if nick and not preview else idx_label
    pct = ""
    score = peer.get("similarity_score")
    if score is not None:
        try:
            pct = f" (~{int(float(score) * 100)}% match)"
        except (TypeError, ValueError):
            pct = ""
    return (
        f"{who} — {label}{pct}. "
        "Want me to introduce you? Say the neighbor number or ask for an intro."
    )


def format_attr_peers_reply(
    peers: list[dict[str, Any]],
    *,
    filter_text: str,
    partial_summary: str | None = None,
) -> str:
    if not peers:
        if partial_summary:
            return (
                f"No one on your block matches all of \"{filter_text}\" yet. "
                f"{partial_summary}"
            )
        return (
            f"I don't see neighbors matching \"{filter_text}\" on your block yet. "
            "Try broadening the description or complete your profile so I can match better."
        )
    lines = [f"I found {len(peers)} neighbor{'s' if len(peers) != 1 else ''} matching \"{filter_text}\":"]
    for p in peers[:5]:
        nick = str(p.get("nickname") or "A neighbor")
        label = str(p.get("matching_peer_label") or "shared traits")
        lines.append(f"• {nick} — {label}")
    lines.append("Want me to introduce you to any of them?")
    return "\n".join(lines)


def handle_notification_prefs(user_jwt: str, message: str) -> tuple[str, str]:
    low = message.lower()
    if any(w in low for w in ("stop", "off", "unsubscribe", "none", "never")):
        pref = "off"
    elif any(w in low for w in ("fewer", "less", "quiet", "mute", "minimum")):
        pref = "quiet"
    else:
        pref = "normal"
    try:
        call_rpc(user_jwt, "update_user_notification_prefs", {"p_sms": pref})
    except HTTPException:
        pass
    if pref == "off":
        return "Done — I won't text you unless you ask.", pref
    if pref == "quiet":
        return "Got it — I'll only text for important matches.", pref
    return "You're back on normal updates — I'll text when there's a good match.", pref


def handle_add_or_edit_claim(
    user_id: str | None,
    message: str,
    *,
    linear_intent: str,
) -> tuple[str, int]:
    if not user_id:
        return (
            "Verify your phone first — then I can save identity threads to your profile.",
            0,
        )
    dismissed = 0
    if linear_intent == "identity.edit_claim" or re.search(
        r"\b(?:remove|delete|drop|clear)\b", message, re.I
    ):
        dismissed = dismiss_claims_from_edit_message(user_id, message)
    saved = extract_and_upsert_claims_from_message(user_id, message)
    total = dismissed + saved
    if total > 0:
        parts: list[str] = []
        if dismissed:
            parts.append(f"removed {dismissed}")
        if saved:
            parts.append(f"updated {saved}")
        return (
            f"Got it — I {' and '.join(parts)} identity thread{'s' if total != 1 else ''} on your profile.",
            total,
        )
    return (
        "Tell me more — heritage, life stage, interests, language, faith, or what you like to do.",
        0,
    )


def handle_change_name(user_id: str | None, message: str) -> tuple[str, str | None]:
    nick = extract_display_name_reply(message)
    if not nick:
        return "What should I call you? Just your first name is fine.", None
    if user_id:
        persist_profile_patch(user_id, {"nickname": nick})
    return f"Done — I'll call you {nick} from here.", nick


def linear_intent_label(linear_intent: str) -> str:
    return linear_intent if linear_intent in LINEAR_INTENTS else "unknown"
