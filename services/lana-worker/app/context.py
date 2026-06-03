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

    return {
        "nickname": user.get("nickname"),
        "home_block_id": block_id,
        "home_zip": user.get("home_zip"),
        "block_display_name": block.get("display_name"),
        "block_cluster_id": block.get("cluster_id"),
        "block_state": block.get("state"),
        "existing_claims": claims,
    }


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
    return "\n".join(lines)
