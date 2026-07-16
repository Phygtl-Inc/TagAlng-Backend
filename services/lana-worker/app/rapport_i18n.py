"""Write-time localization for rapport ("By the way…") tile questions.

Questions are stored English-canonical in ``rapport_gaps.question`` /
``why_frame`` (same model as the rest of the DB), and AI-rendered into the
user's preferred language into ``question_i18n`` at the moments nobody is
waiting:

1. when a gap is opened (``open_semantic_gap`` — already off the chat hot path);
2. when ``users.locale`` changes (``set_user_preferred_language`` re-renders the
   whole open queue in the background, so a language switch never leaves stale
   old-language questions on the tile).

The home-screen read path (``rapport_ranker.next_ask``) then just picks
``question_i18n[locale]`` — a plain DB read, no LLM latency. On a miss (race
right after a switch, or an old row) it serves the English text and kicks a
background render here so the next fetch self-heals.

``question_i18n`` shape: ``{"pt": {"question": "...", "why_frame": "..."}}`` —
one key per language actually used; English needs no entry.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from app.auth import service_client
from app.i18n import _ai_render, normalize_lang_code  # noqa: PLC2701 — same-package renderer

logger = logging.getLogger(__name__)


def render_gap_texts(question: str, why_frame: str | None, lang: str) -> dict[str, str] | None:
    """AI-render a gap's question + teaser into ``lang``.

    Returns ``{"question": ..., "why_frame": ...}`` or None when there is
    nothing to store (English/invalid lang, LLM unconfigured, render failure) —
    callers must NOT write an entry on None, so the serve-time fallback keeps
    seeing the miss and can retry later."""
    code = normalize_lang_code(lang)
    if not code or code == "en":
        return None
    q = str(question or "").strip()
    if not q:
        return None
    rendered_q = _ai_render(q, code)
    if not rendered_q:  # None (unconfigured) or "" (hard failure) — don't store English under a non-en key
        return None
    out = {"question": rendered_q}
    frame = str(why_frame or "").strip()
    if frame:
        out["why_frame"] = _ai_render(frame, code) or frame
    return out


def localize_gap_row(
    gap_row_id: str,
    question: str,
    why_frame: str | None,
    lang: str,
    existing_i18n: dict[str, Any] | None = None,
) -> bool:
    """Render one gap into ``lang`` and merge it into ``question_i18n``. Best-effort."""
    code = normalize_lang_code(lang)
    if not gap_row_id or not code or code == "en":
        return False
    rendered = render_gap_texts(question, why_frame, code)
    if not rendered:
        return False
    merged = dict(existing_i18n) if isinstance(existing_i18n, dict) else {}
    merged[code] = rendered
    try:
        service_client().table("rapport_gaps").update({"question_i18n": merged}).eq(
            "gap_row_id", gap_row_id
        ).execute()
        return True
    except Exception:  # noqa: BLE001
        logger.exception("rapport-i18n: store failed for gap %s", gap_row_id)
        return False


def localize_user_gaps(user_id: str, lang: str) -> int:
    """Render every open/asked gap of ``user_id`` into ``lang`` (skipping rows that
    already have it). Called in the background when the preferred language changes,
    so the queued backlog is already in the new language by the next home render."""
    code = normalize_lang_code(lang)
    if not user_id or not code or code == "en":
        return 0
    try:
        rows = (
            service_client()
            .table("rapport_gaps")
            .select("gap_row_id, question, why_frame, question_i18n")
            .eq("user_id", user_id)
            .in_("status", ["open", "asked"])
            .execute()
        ).data or []
    except Exception:  # noqa: BLE001
        logger.exception("rapport-i18n: open-gap load failed for %s", user_id)
        return 0
    done = 0
    for row in rows:
        i18n = row.get("question_i18n")
        if isinstance(i18n, dict) and isinstance(i18n.get(code), dict):
            continue  # already rendered for this language
        if localize_gap_row(
            row["gap_row_id"], row.get("question") or "", row.get("why_frame"), code, i18n
        ):
            done += 1
    if done:
        logger.info("rapport-i18n: rendered %d gap(s) into %s for %s", done, code, user_id)
    return done


def localize_user_gaps_async(user_id: str, lang: str) -> None:
    """Fire-and-forget ``localize_user_gaps`` — the callers sit on request paths."""
    code = normalize_lang_code(lang)
    if not user_id or not code or code == "en":
        return
    threading.Thread(
        target=localize_user_gaps, args=(user_id, code), daemon=True, name="rapport-i18n"
    ).start()


def localize_gap_row_async(
    gap_row_id: str,
    question: str,
    why_frame: str | None,
    lang: str,
    existing_i18n: dict[str, Any] | None = None,
) -> None:
    """Fire-and-forget single-row render — the next-ask self-heal on a cache miss."""
    code = normalize_lang_code(lang)
    if not gap_row_id or not code or code == "en":
        return
    threading.Thread(
        target=localize_gap_row,
        args=(gap_row_id, question, why_frame, code, existing_i18n),
        daemon=True,
        name="rapport-i18n-row",
    ).start()
