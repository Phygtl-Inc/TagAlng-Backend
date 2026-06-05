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


def build_event_host_system_prompt() -> str:
    return load_prompt("lana_event_host.md")


def format_event_draft_context(ctx: dict[str, Any]) -> str:
    from app.event_context import format_event_draft_context as _format

    return _format(ctx)


def load_event_draft_context(user_id: str) -> dict[str, Any]:
    """Minimal DB context for event_draft fast path (form-filling only)."""
    sb = service_client()
    user_row = (
        sb.table("users")
        .select("id, nickname, full_name, home_block_id")
        .eq("id", user_id)
        .limit(1)
        .execute()
    )
    user = user_row.data[0] if user_row.data else {}
    block_id = user.get("home_block_id")
    block_display_name: str | None = None
    if block_id:
        block_row = (
            sb.table("blocks")
            .select("display_name")
            .eq("id", block_id)
            .limit(1)
            .execute()
        )
        if block_row.data:
            block_display_name = block_row.data[0].get("display_name")

    return {
        "nickname": user.get("nickname"),
        "full_name": user.get("full_name"),
        "home_block_id": block_id,
        "block_display_name": block_display_name,
        "event_purpose_ids": load_event_purpose_ids(),
    }


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
    relationship_tiers = _load_relationship_tiers(user_id, vector_peers, network)
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
        "relationship_tiers": relationship_tiers,
        "event_purpose_ids": event_purpose_ids,
    }


def _load_relationship_tiers(
    user_id: str,
    vector_peers: list[dict[str, Any]],
    network: dict[str, Any],
) -> dict[str, str]:
    """Map neighbor user_id -> relationship tier for Lana routing."""
    ids: list[str] = []
    for vp in vector_peers or []:
        uid = vp.get("peer_user_id") or vp.get("user_id")
        if uid:
            ids.append(str(uid))
    for h in (network.get("neighbor_hints") or [])[:8]:
        uid = h.get("user_id")
        if uid:
            ids.append(str(uid))
    unique = list(dict.fromkeys(ids))[:12]
    if not unique:
        return {}
    try:
        sb = service_client()
        res = sb.rpc(
            "get_relationship_tiers_for_user",
            {"p_user_id": user_id, "p_other_user_ids": unique},
        ).execute()
        rows = res.data or []
        return {
            str(r["other_user_id"]): str(r.get("tier", "stranger"))
            for r in rows
            if r.get("other_user_id")
        }
    except Exception:
        return {}


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
        tiers = ctx.get("relationship_tiers") or {}
        if vpeers:
            lines.append("- Vector similarity neighbors (meaning-close public threads on this block):")
            for vp in vpeers[:5]:
                nick = vp.get("nickname") or "Neighbor"
                uid = vp.get("peer_user_id") or vp.get("user_id")
                tier = tiers.get(str(uid), "stranger") if uid else "stranger"
                sim = vp.get("similarity_score")
                pct = f"{round(float(sim) * 100)}%" if sim is not None else "—"
                lbl = vp.get("matching_peer_label") or "—"
                exact = " (same thread slug)" if vp.get("has_exact_concept_match") else ""
                id_hint = f" [user_id={uid}]" if uid else ""
                lines.append(
                    f"  · {nick}{id_hint}: ~{pct} match via «{lbl}»{exact} · tier={tier}"
                )
        lines.append(
            "- Agent rule: you may mention that neighbors or activities exist on the block; "
            "do not invent names or events beyond this list."
        )

    return "\n".join(lines)
