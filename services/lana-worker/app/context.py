import logging
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from app.auth import service_client

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# Fan-out pool for the independent Supabase reads in load_user_context. Each call is
# one ~300ms REST round-trip; running them together bounds the load at the slowest
# chain (user→block) instead of the sum of all six. The cached Supabase client is
# shared across threads the same way main.py's write pool already shares it.
_CTX_POOL = ThreadPoolExecutor(max_workers=6, thread_name_prefix="ctx-load")


def load_prompt(name: str) -> str:
    path = _PROMPTS_DIR / name
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()


# LANA_LINGO §3.3/§4: the current turn's inferred household role + grammatical
# gender. Request-scoped contextvars (each request runs in its own context copy),
# set once per turn in main.py — so every prompt built through lingo_constitution()
# picks them up without threading a parameter through ~50 composer call sites.
_USER_ROLE: ContextVar[str | None] = ContextVar("lana_user_role", default=None)
_USER_GRAM_GENDER: ContextVar[str | None] = ContextVar(
    "lana_user_gram_gender", default=None
)

# How each role sharpens address (§3.3). Unlisted/unknown roles stay neutral.
#
# These are DEFAULTS for referring to the household in general. They must never
# overwrite the words the user just used for a specific person: a stored
# role=grandparent turned "my 7 year old does karate" into "other grandkids who
# do the same" — Lana correcting the user about her own family.
_ROLE_OVERRIDE_GUARD = (
    "This is how to refer to their family in GENERAL. When the user names "
    "someone their own way in this turn (\"my 7 year old\", \"my son\", \"my "
    "daughter\"), mirror THEIR words for that person — never re-label them from "
    "the stored role."
)
_ROLE_FRAMING = {
    "parent": 'their family is "your kids" / "your family"',
    "expecting": 'be gentle — the baby is not here yet; say "when the baby comes"',
    "grandparent": 'their family is "your grandkids", never "your kids"',
    "caregiver": (
        'they care for someone else\'s family — say "the family you care for" / '
        '"the little one you care for", never a parent label'
    ),
    "guardian": 'say "your family" / "the kids in your care"',
    "relative": 'say "your family" (nieces, nephews — as they said it), never a parent label',
}


def set_address_context(
    role: str | None, grammatical_gender: str | None
) -> None:
    """Stamp the turn's role/gender for prompt building. None = unspecified."""
    _USER_ROLE.set(str(role or "").strip().lower() or None)
    _USER_GRAM_GENDER.set(str(grammatical_gender or "").strip().lower() or None)


def user_gram_gender() -> str:
    """This turn's grammatical gender ("" when unknown).

    Public because it is part of the CACHE KEY of every AI language render — see
    i18n._AI_RENDER_CACHE. Keyed on (text, lang) alone, the first user to warm a
    phrase decided its agreement for every later user in the process: a feminine
    user's "¡Bienvenida a la zona!" was served verbatim to a user whose gender was
    known masculine (eval 2026-09-01, lt_gender_es_known_masculine). That read as
    flaky because it is order-dependent, not random — and it worsens as the cache
    fills, so a cold-process run can never reproduce it.
    """
    return _USER_GRAM_GENDER.get() or ""


# English pronouns for each stored value. grammatical_gender is a CONJUGATION axis
# (es/pt agreement) and is documented never-shown — but "what are my pronouns?" is
# the user asking about their own profile, and Lana answered "I don't know your
# pronouns yet" to a user who had just set them (2026-09-04). Never-shown protects
# the value from OTHER users; it was never meant to hide someone's own preference
# from them.
_PRONOUNS = {"feminine": "she/her", "masculine": "he/him", "neutral": "they/them"}

# Appended whenever a value IS on file: the model has the fact but no permission to
# state it, so it hedged instead of answering.
_PRONOUN_READBACK = (
    "If they ASK how you address them (\"what are my pronouns?\", \"do you know if "
    "I'm he or she?\"), answer plainly with the value above — it is their own "
    "profile fact and they are entitled to it. Do NOT volunteer it unprompted, do "
    "NOT attach it to them as a label, and never state it to any OTHER user."
)

# Appended when nothing is on file. The old behaviour was to hedge and improvise
# pronoun chips that posted text nothing was listening for; this makes the ask
# concrete and tells the model the exact words that DO persist (see
# app/vertex_extract.py's stated-gender rule).
_PRONOUN_UNKNOWN_READBACK = (
    "If they ASK how you address them, say plainly that you don't have it yet and "
    "invite them to tell you — and phrase the invitation so their reply is one you "
    "can store: \"she/her\", \"he/him\" or \"they/them\" on its own is enough, as is "
    "\"call me she\". Offer those three as options. Never guess, and never claim to "
    "know."
)

# What each grammatical gender means for agreement. 'neutral' is a real stored
# value, not an absence: a user who states they/them must not fall back to the
# unknown-gender guess, which is allowed to pick masculine as a last resort.
_GENDER_GUIDANCE = {
    "feminine": (
        "USER CONTEXT — grammatical gender: feminine; in gendered languages "
        "(es/pt/…) conjugate every greeting and adjective to agree (femenino)."
    ),
    "masculine": (
        "USER CONTEXT — grammatical gender: masculine; in gendered languages "
        "(es/pt/…) conjugate every greeting and adjective to agree (masculino)."
    ),
    "neutral": (
        "USER CONTEXT — grammatical gender: neutral (they/them, stated by the "
        "user). NEVER use a gendered form for them in ANY language. In gendered "
        "languages rephrase so agreement never arises — address them with verbs "
        "and neutral nouns (\"qué bueno tenerte por aquí\") instead of an "
        "adjective that must agree (\"bienvenido/bienvenida\"). Never invent a "
        "neologism (-e/-x) unless the user used it first."
    ),
}

# No stored gender. The composer must NOT coin-flip: "bienvenida" for an unknown
# user is the exact failure the eval caught (lt_gender_es_unknown_neutral). The old
# rule said only "stay gender-neutral — rephrase rather than pick a gendered form",
# which is unsatisfiable for a word like bienvenido/a, so the model sampled — and
# Spanish/Portuguese training data leans feminine in a warm welcoming register.
_GENDER_UNKNOWN_GUIDANCE = (
    "USER CONTEXT — grammatical gender: UNKNOWN. Do not guess it, and do not "
    "guess from their name. FIRST rephrase so agreement never arises (\"qué bueno "
    "tenerte por aquí\" / \"que bom ter você por aqui\" rather than "
    "\"bienvenido/bienvenida\"). ONLY if the language leaves no ungendered "
    "construction, use the MASCULINE form — it is the grammatical default in "
    "es/pt. NEVER default to the feminine form."
)


def address_guidance() -> str:
    """Per-user address lines appended to the constitution.

    Unlike role, gender ALWAYS emits a line — including when it is unknown. An
    empty string used to mean "the constitution's neutral rules cover it", but the
    constitution has no rule for a word that cannot be written ungendered, so the
    composer picked one (and picked feminine). Naming the unknown case explicitly,
    with a stated fallback, is what closes that.
    """
    role = _USER_ROLE.get() or ""
    gender = _USER_GRAM_GENDER.get() or ""
    lines: list[str] = []
    if role in _ROLE_FRAMING:
        lines.append(
            f"USER CONTEXT — household role: {role}; {_ROLE_FRAMING[role]}. "
            "Role sharpens warmth only — never announce it or attach it to them as a label. "
            + _ROLE_OVERRIDE_GUARD
        )
    known = _GENDER_GUIDANCE.get(gender)
    if known:
        lines.append(f"{known} Their pronouns are {_PRONOUNS[gender]}.")
        lines.append(_PRONOUN_READBACK)
    else:
        lines.append(_GENDER_UNKNOWN_GUIDANCE)
        lines.append(_PRONOUN_UNKNOWN_READBACK)
    return "\n".join(lines)


def lingo_constitution() -> str:
    """LANA_LINGO §2/§14.1: the hard word rules every user-facing composer obeys
    (never "mom"/"block"/"circle" in-app, role/gender-aware address, locked
    outcome verbs). Appended to every system prompt that authors user copy —
    extractors and classifiers don't need it. Carries the current user's role/
    gender address guidance when main.py stamped it for this turn."""
    base = load_prompt("lana_lingo_constitution.md")
    extra = address_guidance()
    return f"{base}\n\n{extra}" if extra else base


def build_system_prompt() -> str:
    product = load_prompt("tagalng_product.md")
    persona = load_prompt("lana_persona.md")
    return f"{product}\n\n---\n\n{persona}\n\n---\n\n{lingo_constitution()}"


def build_event_host_system_prompt() -> str:
    return load_prompt("lana_event_host.md") + "\n\n---\n\n" + lingo_constitution()


def build_profile_system_prompt() -> str:
    return load_prompt("lana_profile_intake.md") + "\n\n---\n\n" + lingo_constitution()


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
    def _user_and_block() -> tuple[dict[str, Any], dict[str, Any]]:
        sb = service_client()
        user_row = (
            sb.table("users")
            .select("id, nickname, home_block_id, home_zip, locale")
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
        return user, block

    def _claims() -> list[dict[str, Any]]:
        sb = service_client()

        def _select(columns: str) -> list[dict[str, Any]]:
            return (
                sb.table("user_identity_claims")
                .select(columns)
                .eq("user_id", user_id)
                .is_("dismissed_at", "null")
                .execute()
            ).data or []

        try:
            # subject_* is owner-only: this context is built for one user and
            # feeds only their own turn, so Lana can say "how's Sara's karate?"
            # to the parent. It must never be copied into a peer-facing payload.
            return _select(
                "concept, label, disclosure, subject_kind, subject_name, subject_birth_year"
            )
        except Exception:
            # An environment without 20261021120000 rejects the whole select, and
            # Lana then walks into the turn believing the user has told her
            # nothing. Everything she already knew matters more than the subject.
            logger.warning("claims_subject_columns_absent — falling back user=%s", user_id)
            return _select("concept, label, disclosure")

    # Only the user→block chain and the tier lookup have ordering constraints; the
    # rest are independent reads. Tiers still runs after peers+network (its inputs).
    user_block_f = _CTX_POOL.submit(_user_and_block)
    claims_f = _CTX_POOL.submit(_claims)
    network_f = _CTX_POOL.submit(_load_block_network, user_id)
    vector_peers_f = _CTX_POOL.submit(_load_vector_peer_hints, user_id)
    purpose_ids_f = _CTX_POOL.submit(load_event_purpose_ids)

    network = network_f.result()
    vector_peers = vector_peers_f.result()
    relationship_tiers = _load_relationship_tiers(user_id, vector_peers, network)
    user, block = user_block_f.result()
    block_id = user.get("home_block_id")
    claims = claims_f.result()
    event_purpose_ids = purpose_ids_f.result()

    return {
        "nickname": user.get("nickname"),
        "home_block_id": block_id,
        "home_zip": user.get("home_zip"),
        "preferred_language": user.get("locale"),
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
    """Top nearby peers by claim embedding similarity (public claims only).

    "Nearby" is a radius when LANA_PEER_RADIUS_MATCH is on, the caller's home
    block otherwise. Fail-open both ways: the radius twin falling over drops
    back to the block-scoped original rather than emptying the context pack.
    """
    from app.peer_radius import radius_match_enabled, radius_meters

    base = "match_peers_by_claim_vectors_for_user"
    args: dict[str, Any] = {"p_user_id": user_id, "p_limit": 5, "p_min_similarity": 0.70}
    try:
        sb = service_client()
        if radius_match_enabled():
            try:
                res = sb.rpc(
                    f"{base}_near", {**args, "p_radius_meters": radius_meters()}
                ).execute()
                rows = res.data or []
                return rows if isinstance(rows, list) else []
            except Exception:
                logger.exception("peer_hints_radius_failed user=%s — block fallback", user_id)
        res = sb.rpc(base, args).execute()
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


# Mirrors the cohorts seed in 20260611120000_event_purpose_cohorts.sql.
_PURPOSE_FALLBACK: list[dict[str, Any]] = [
    {"id": "faith_small_group", "label": "Faith small group", "emoji": "⛪"},
    {"id": "running_fitness", "label": "Running / fitness", "emoji": "🏃"},
    {"id": "outdoor_adventure", "label": "Outdoor + adventure", "emoji": "🌳"},
    {"id": "coffee_stroller", "label": "Coffee + stroller", "emoji": "☕"},
    {"id": "heritage_language", "label": "Heritage / language", "emoji": "🌍"},
    {"id": "postpartum_support", "label": "Postpartum + support", "emoji": "🌿"},
    {"id": "book_club_learning", "label": "Book club / learning", "emoji": "📖"},
    {"id": "beauty_wellness", "label": "Beauty + wellness", "emoji": "💆"},
    {"id": "lifestyle_social", "label": "Lifestyle + social", "emoji": "🍷"},
    {"id": "kids_led_activity", "label": "Kids-led activity", "emoji": "🧸"},
]

_purpose_cache: dict[str, Any] = {"at": 0.0, "rows": None}
_PURPOSE_TTL_S = 600.0


def load_event_purposes() -> list[dict[str, Any]]:
    """Event Purpose catalog rows ({id, label, emoji}) — the taxonomy is near-static,
    so rows are cached in-process; falls back to the migration seed on any DB failure."""
    now = time.monotonic()
    if _purpose_cache["rows"] is not None and now - _purpose_cache["at"] < _PURPOSE_TTL_S:
        return _purpose_cache["rows"]
    try:
        sb = service_client()
        res = sb.rpc("get_event_purposes").execute()
        rows = [
            {"id": str(r["id"]), "label": str(r.get("label") or ""), "emoji": r.get("emoji")}
            for r in (res.data or [])
            if r.get("id")
        ] or list(_PURPOSE_FALLBACK)
    except Exception:
        rows = list(_PURPOSE_FALLBACK)
    _purpose_cache.update(at=now, rows=rows)
    return rows


def load_event_purpose_ids() -> list[str]:
    return [r["id"] for r in load_event_purposes()]


def cohort_tag_labels_for(tags: list[str]) -> list[str]:
    """Display labels for cohort_tags ids (cohorts.label, e.g. lifestyle_social ->
    'Lifestyle + social'); unknown ids de-snake as a last resort so a chip never
    regresses to a raw taxonomy id."""
    by_id = {r["id"]: r["label"] for r in load_event_purposes()}
    return [by_id.get(t) or t.replace("_", " ") for t in tags]


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
        name = net.get("block_display_name") or ctx.get("home_block_id") or "your area"
        mc = net.get("member_count", 0)
        lines.append(
            f"- Neighborhood ({name}): {mc} other member(s) nearby"
            " (backstage unit: block — never say 'block' to the user)"
        )
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
            "- Agent rule: you may mention that neighbors or activities exist nearby; "
            "do not invent names or events beyond this list."
        )

    return "\n".join(lines)
