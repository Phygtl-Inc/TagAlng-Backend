from pathlib import Path
from typing import Any

from app.auth import service_client

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def load_prompt(name: str) -> str:
    path = _PROMPTS_DIR / name
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()


def build_system_prompt() -> str:
    product = load_prompt("tagalng_product.md")
    persona = load_prompt("lana_persona.md")
    return f"{product}\n\n---\n\n{persona}"


def load_user_context(user_id: str) -> dict[str, Any]:
    sb = service_client()
    user_row = (
        sb.table("users")
        .select("id, nickname, home_block_id, home_zip")
        .eq("id", user_id)
        .limit(1)
        .execute()
    )
    user = user_row.data[0] if user_row.data else {}
    block_id = user.get("home_block_id")
    block: dict[str, Any] = {}
    if block_id:
        block_row = (
            sb.table("blocks")
            .select("id, display_name, cluster_id, state")
            .eq("id", block_id)
            .limit(1)
            .execute()
        )
        block = block_row.data[0] if block_row.data else {}

    claims_row = (
        sb.table("user_identity_claims")
        .select("concept, label, disclosure")
        .eq("user_id", user_id)
        .is_("dismissed_at", "null")
        .execute()
    )
    claims = claims_row.data or []

    network = _load_block_network(user_id)
    vector_peers = _load_vector_peer_hints(user_id)
    event_purpose_ids = load_event_purpose_ids()

    return {
        "nickname": user.get("nickname"),
        "home_block_id": block_id,
        "home_zip": user.get("home_zip"),
        "block_display_name": block.get("display_name"),
        "block_cluster_id": block.get("cluster_id"),
        "block_state": block.get("state"),
        "existing_claims": claims,
        "block_network": network,
        "vector_peers": vector_peers,
        "event_purpose_ids": event_purpose_ids,
    }


def _load_vector_peer_hints(user_id: str) -> list[dict[str, Any]]:
    """Top peers on same block by claim embedding similarity (public claims only)."""
    try:
        sb = service_client()
        res = sb.rpc(
            "match_peers_by_claim_vectors_for_user",
            {"p_user_id": user_id, "p_limit": 5, "p_min_similarity": 0.65},
        ).execute()
        rows = res.data or []
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def _load_block_network(user_id: str) -> dict[str, Any]:
    """Retrieve block peers + upcoming events (agent retrieval; service role RPC)."""
    try:
        sb = service_client()
        res = sb.rpc("get_lana_block_context_for_user", {"p_user_id": user_id}).execute()
        data = res.data
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_event_purpose_ids() -> list[str]:
    try:
        sb = service_client()
        res = sb.rpc("get_event_purposes").execute()
        rows = res.data or []
        return [str(r["id"]) for r in rows if r.get("id")]
    except Exception:
        return [
            "faith_small_group",
            "running_fitness",
            "outdoor_adventure",
            "coffee_stroller",
            "heritage_language",
            "postpartum_support",
            "book_club_learning",
            "beauty_wellness",
            "lifestyle_social",
            "kids_led_activity",
        ]


def format_user_context(ctx: dict[str, Any], purpose: str) -> str:
    lines = [
        "CURRENT USER CONTEXT (from database — do not invent facts):",
        f"- Session purpose: {purpose}",
    ]
    if ctx.get("nickname"):
        lines.append(f"- Nickname: {ctx['nickname']}")
    if ctx.get("home_block_id"):
        lines.append(f"- Home block id: {ctx['home_block_id']}")
        if ctx.get("block_display_name"):
            lines.append(f"- Block name: {ctx['block_display_name']}")
        if ctx.get("block_cluster_id"):
            lines.append(f"- Cluster: {ctx['block_cluster_id']}")
        if ctx.get("block_state"):
            lines.append(f"- Block state: {ctx['block_state']}")
    if ctx.get("home_zip"):
        lines.append(f"- Home ZIP (coarse area only): {ctx['home_zip']}")
    claims = ctx.get("existing_claims") or []
    if claims:
        lines.append("- Existing profile threads (reference; user may update in chat):")
        for c in claims[:12]:
            lines.append(f"  · {c.get('label')} ({c.get('disclosure', 'public')})")
    else:
        lines.append("- Existing profile threads: none yet (signup)")

    if purpose == "event_draft":
        purpose_ids = ctx.get("event_purpose_ids") or []
        if purpose_ids:
            lines.append("- Event Purpose chips (cohort_tags ids the host may select):")
            for pid in purpose_ids[:12]:
                lines.append(f"  · {pid}")
        lines.append(
            "- Event host rule: extract title, time, venue, description, max_attendees, Purpose tags; "
            "ask one clarifying question only when title, time, or place is missing."
        )

    net = ctx.get("block_network") or {}
    if not net.get("has_block"):
        lines.append("- Block network: home block not assigned yet")
    else:
        name = net.get("block_display_name") or ctx.get("home_block_id") or "your block"
        mc = net.get("member_count", 0)
        lines.append(f"- Block network ({name}): {mc} other member(s) on this block")
        for ev in (net.get("upcoming_events") or [])[:3]:
            title = ev.get("title") or "Event"
            when = ev.get("starts_at") or ""
            lines.append(f"  · Upcoming activity: {title} ({when})")
        hints = net.get("neighbor_hints") or []
        if hints:
            lines.append("- Neighbor hints (public profile threads only — use for warmth, not stalking):")
            for h in hints[:5]:
                nick = h.get("nickname") or "Neighbor"
                shared = h.get("shared_public_claim_count", 0)
                labels = h.get("public_labels") or []
                label_txt = ", ".join(labels[:3]) if labels else "—"
                extra = f", {shared} shared thread(s)" if shared else ""
                lines.append(f"  · {nick}: {label_txt}{extra}")
        vpeers = ctx.get("vector_peers") or []
        if vpeers:
            lines.append("- Vector similarity neighbors (meaning-close public threads on this block):")
            for vp in vpeers[:5]:
                nick = vp.get("nickname") or "Neighbor"
                sim = vp.get("similarity_score")
                pct = f"{round(float(sim) * 100)}%" if sim is not None else "—"
                lbl = vp.get("matching_peer_label") or "—"
                exact = " (same thread slug)" if vp.get("has_exact_concept_match") else ""
                lines.append(f"  · {nick}: ~{pct} match via «{lbl}»{exact}")
        lines.append(
            "- Agent rule: you may mention that neighbors or activities exist on the block; "
            "do not invent names or events beyond this list."
        )

    return "\n".join(lines)
