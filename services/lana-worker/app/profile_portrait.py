"""The one-line portrait above a person's threads — theirs, and the one peers read.

Two lines, never one string used twice:

  * `users.portrait`        — over ALL their threads, addressed to them ("you"). The
    owner is the only reader, which is why it may reference a thread about their kid.
  * `users.public_portrait` — over their `disclosure='public'` claims ONLY, in the
    third person. This is what `get_peer_profile` hands to anyone who opens the
    profile. Every fact in it is already a chip on that same screen; the synthesis is
    the only new thing.

WRITTEN ONCE, ON THE WRITE PATH (migration 20261026). It used to be composed on every
drawer open behind an in-process lru_cache — which died with the container, so each pod
re-bought the same sentence, and the drawer rendered the bare thread list first and
swapped the prose in a second later. Now `refresh_portraits` runs in the background when
a claim lands or is retracted, and every reader just reads a column.

Read-time refresh would not have worked for the public line at all: peers fetch
`get_peer_profile` straight from Supabase, so nobody but the owner would ever pass
through the worker to trigger one.

AI-authored from true facts only, and None (never a canned line) when there is nothing
true to say ([[ai-authored-copy-not-canned]]).
"""

from __future__ import annotations

import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)

_PORTRAIT_PROMPT = """You write a one-line portrait of a person for their OWN private \
profile page in a neighborhood app. They are the only reader.

Output ONLY JSON: {"portrait": "..."}

Rules:
- Ground it ONLY in the threads given. Never invent a job, a family fact, a place, a \
mood, or how much they like anything.
- Address them as "you". Never use he/she/his/her and never guess their gender.
- One sentence, max 200 characters, warm and plain — a portrait, not a list. Group \
what belongs together and let the smaller threads go.
- A thread marked (about their child) is the CHILD's, not theirs — say "your kid" for \
those, and never merge it with what they do themselves.
- Never the words "thread", "claim", "block", or "match" (backstage vocabulary).
- English only."""

_PUBLIC_PORTRAIT_PROMPT = """You write a one-line portrait of a NEIGHBOR, shown to \
other people in a neighborhood app who opened their profile.

Output ONLY JSON: {"portrait": "..."}

Rules:
- Ground it ONLY in the threads given. Never invent a job, a family fact, a place, a \
mood, or how much they like anything. These are the only things you may say about them.
- Never "you" — the reader is someone else. Use "they/them", never he/she/his/her, and \
never guess their gender.
- Never their name — the card already shows it.
- One sentence, max 200 characters, warm and plain — a portrait, not a list. Group what \
belongs together and let the smaller threads go.
- Say nothing about children, family, or anyone but the person themselves.
- Never the words "thread", "claim", "block", or "match" (backstage vocabulary).
- English only."""

# One worker: authoring is best-effort background work, and a single thread keeps a
# burst of claim writes from fanning out into parallel model calls.
_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="portrait")
# Users being written right now, so two claims landing together buy one pass.
_INFLIGHT: set[str] = set()
# How many threads reach the model. Past this it is a list, not a portrait.
_MAX_FACTS = 20


def _facts_for_self(claims: list[dict[str, Any]]) -> list[str]:
    """Their threads, deduped. A child's thread rides in marked and NAMELESS — the
    portrait can never spend a name it was never given."""
    out: list[str] = []
    seen: set[str] = set()
    for c in claims or []:
        if not isinstance(c, dict):
            continue
        label = str(c.get("label") or c.get("concept") or "").strip()
        key = label.lower()
        if not label or key in seen:
            continue
        seen.add(key)
        child = str(c.get("subject_kind") or "self") == "child"
        out.append(f"{label} (about their child)" if child else label)
    return out


def _facts_for_public(claims: list[dict[str, Any]]) -> list[str]:
    """Public threads about the PERSON. Anything about someone else in their life is
    dropped outright rather than trusted to a prompt rule — a public sentence is the
    wrong place to learn that a disclosure setting was mis-set."""
    out: list[str] = []
    seen: set[str] = set()
    for c in claims or []:
        if not isinstance(c, dict):
            continue
        if str(c.get("disclosure") or "") != "public":
            continue
        if str(c.get("subject_kind") or "self") != "self":
            continue
        label = str(c.get("label") or c.get("concept") or "").strip()
        key = label.lower()
        if not label or key in seen:
            continue
        seen.add(key)
        out.append(label)
    return out


def _fingerprint(scope: str, facts: list[str], area: str | None = None) -> str:
    """What the line was written FROM. A different fingerprint means a thread landed or
    was taken back, and the line is rewritten.

    `scope` keeps the two lines' keys apart even when their fact sets coincide — a user
    whose only thread is public produces identical facts for both, and two keys that are
    equal by accident invite comparing the wrong pair later."""
    return hashlib.sha256(
        "|".join([scope, area or "", *facts]).encode("utf-8")
    ).hexdigest()[:32]


def portrait_from_claims(claims: list[dict[str, Any]], *, area: str | None = None) -> str | None:
    """One sentence over the caller's own threads, or None when we know nothing.

    The synchronous composer, kept for callers that have claims in hand and no user row
    to read (and for the tests that pin the fact-shaping rules). Everything user-facing
    now reads `users.portrait` instead — see `refresh_portraits`."""
    facts = _facts_for_self(claims)
    if not facts:
        return None
    return _portrait_cached(facts=tuple(facts[:_MAX_FACTS]), area=(area or "").strip() or None)


@lru_cache(maxsize=512)
def _portrait_cached(*, facts: tuple[str, ...], area: str | None) -> str | None:
    return _compose(_PORTRAIT_PROMPT, list(facts), area=area)


def _compose(prompt: str, facts: list[str], *, area: str | None = None) -> str | None:
    if not facts:
        return None
    from app.orchestrator.llm import llm_configured, llm_json, router_model

    if not llm_configured():
        return None
    lines = [f"- {f}" for f in facts]
    if area:
        lines.insert(0, f"- Home area: {area}")
    try:
        # Router tier: one 200-character sentence from facts we hand it.
        data = llm_json(
            model=router_model(),
            system=prompt,
            user_payload="\n".join(lines),
            max_tokens=160,
            temperature=0.3,
        )
    except Exception:  # noqa: BLE001
        return None
    text = str((data or {}).get("portrait") or "").strip()
    return text[:280] or None


# ── stored portraits ──────────────────────────────────────────────────────────


def _claims_for(user_id: str) -> list[dict[str, Any]]:
    from app.auth import service_client

    try:
        return (
            service_client()
            .table("user_identity_claims")
            .select("label, concept, disclosure, subject_kind, confidence")
            .eq("user_id", user_id)
            .is_("dismissed_at", "null")
            .order("confidence", desc=True)
            .limit(200)
            .execute()
        ).data or []
    except Exception:
        logger.exception("portrait_claims_read_failed user=%s", user_id)
        return []


def _stored(user_id: str) -> dict[str, Any]:
    from app.auth import service_client

    try:
        rows = (
            service_client()
            .table("users")
            .select("portrait, portrait_key, public_portrait, public_portrait_key, home_zip")
            .eq("id", user_id)
            .limit(1)
            .execute()
        ).data or []
    except Exception:
        # Pre-20261026 environments have no portrait columns. Nothing to compare
        # against and nowhere to write — the caller simply does nothing.
        logger.exception("portrait_row_read_failed user=%s", user_id)
        return {}
    return rows[0] if rows else {}


def refresh_portraits(user_id: str) -> None:
    """Rewrite whichever stored line no longer matches the claims behind it.

    Called from the claim write paths, in the background. Cheap when nothing moved:
    one row read, two hashes, no model call."""
    if not user_id:
        return
    stored = _stored(user_id)
    if not stored:
        return
    claims = _claims_for(user_id)
    area = str(stored.get("home_zip") or "").strip() or None
    patch: dict[str, Any] = {}

    self_facts = _facts_for_self(claims)[:_MAX_FACTS]
    self_key = _fingerprint("self", self_facts, area)
    if self_facts and str(stored.get("portrait_key") or "") != self_key:
        line = _compose(_PORTRAIT_PROMPT, self_facts, area=area)
        if line:
            patch.update({"portrait": line, "portrait_key": self_key})

    public_facts = _facts_for_public(claims)[:_MAX_FACTS]
    public_key = _fingerprint("public", public_facts)
    if public_facts and str(stored.get("public_portrait_key") or "") != public_key:
        line = _compose(_PUBLIC_PORTRAIT_PROMPT, public_facts)
        if line:
            patch.update({"public_portrait": line, "public_portrait_key": public_key})

    if not patch:
        return
    from app.auth import service_client

    try:
        service_client().table("users").update(patch).eq("id", user_id).execute()
    except Exception:
        logger.exception("portrait_write_failed user=%s", user_id)


def schedule_portrait_refresh(user_id: str) -> None:
    """Queue a refresh behind the turn. Never blocks the reply, never runs twice at once."""
    uid = str(user_id or "").strip()
    if not uid or uid in _INFLIGHT:
        return
    _INFLIGHT.add(uid)

    def _run() -> None:
        try:
            refresh_portraits(uid)
        except Exception:  # noqa: BLE001 — a stale line beats a broken turn
            logger.exception("portrait_refresh_failed user=%s", uid)
        finally:
            _INFLIGHT.discard(uid)

    try:
        _POOL.submit(_run)
    except Exception:  # noqa: BLE001
        _INFLIGHT.discard(uid)
        logger.exception("portrait_schedule_failed user=%s", uid)


def clear_portraits(user_id: str) -> None:
    """Drop both stored lines NOW, then rewrite behind.

    For a retraction only. A missing new thread makes a portrait incomplete, which is
    survivable; a retracted one makes it false — "you play tennis" on the profile of
    someone who just said they don't. The thread list is the honest thing to show for
    the moment it takes to write the replacement."""
    if not user_id:
        return
    from app.auth import service_client

    try:
        service_client().table("users").update(
            {"portrait": None, "portrait_key": None,
             "public_portrait": None, "public_portrait_key": None}
        ).eq("id", user_id).execute()
    except Exception:
        logger.exception("portrait_clear_failed user=%s", user_id)
    schedule_portrait_refresh(user_id)
