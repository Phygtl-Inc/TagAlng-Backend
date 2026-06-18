"""Structured CTAs for FE — tap posts `message` back to Lana (same as typing in chat)."""

from __future__ import annotations

from typing import Any, Literal

UiActionStyle = Literal["primary", "secondary", "ghost"]


def _action(
    *,
    action_id: str,
    label: str,
    message: str,
    style: UiActionStyle = "primary",
    intro_id: str | None = None,
    peer_user_id: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": action_id,
        "label": label,
        "message": message,
        "style": style,
    }
    if intro_id:
        row["intro_id"] = intro_id
    if peer_user_id:
        row["peer_user_id"] = peer_user_id
    return row


def intro_respond_actions(
    *,
    nickname: str | None = None,
    intro_id: str | None = None,
) -> list[dict[str, Any]]:
    """Received intro waiting on caller — C-8 / inbox accept-decline."""
    nick = str(nickname or "them").strip() or "them"
    return [
        _action(
            action_id="intro_accept",
            label=f"Yes, introduce us",
            message="yes introduce us",
            style="primary",
            intro_id=intro_id,
        ),
        _action(
            action_id="intro_decline",
            label="Not now",
            message="not now",
            style="secondary",
            intro_id=intro_id,
        ),
    ]


def duplicate_intro_sent_actions() -> list[dict[str, Any]]:
    """Intro already sent — inbox check, not another nudge."""
    return [
        _action(
            action_id="intro_show_inbox",
            label="Show my intros",
            message="show my intros",
            style="primary",
        ),
        _action(
            action_id="intro_pass",
            label="Not yet",
            message="not now",
            style="secondary",
        ),
    ]


def intro_offer_actions(
    *,
    nickname: str | None = None,
    peer_user_id: str | None = None,
) -> list[dict[str, Any]]:
    """Lana offered to introduce user to a shown neighbor."""
    nick = str(nickname or "them").strip() or "them"
    return [
        _action(
            action_id="intro_propose",
            label=f"Send {nick} a nudge",
            message=f"introduce me to {nick}",
            style="primary",
            peer_user_id=peer_user_id,
        ),
        _action(
            action_id="intro_pass",
            label="Not yet",
            message="not now",
            style="secondary",
        ),
    ]


def peer_card_nudge_action(
    *,
    nickname: str,
    peer_user_id: str,
) -> dict[str, Any]:
    """Per-card nudge on ranked peer results (C-FIND-MOM-RESULTS)."""
    nick = str(nickname or "them").strip() or "them"
    return _action(
        action_id="peer_card_nudge",
        label="Nudge",
        message=f"introduce me to {nick}",
        style="primary",
        peer_user_id=peer_user_id,
    )


def weak_match_prompt_actions(
    *,
    nickname: str,
    peer_user_id: str | None = None,
) -> list[dict[str, Any]]:
    """Founder weak-match prompt below ranked cards."""
    nick = str(nickname or "them").strip() or "them"
    return [
        _action(
            action_id="peer_wait_stronger",
            label="Wait for stronger",
            message="wait for stronger matches",
            style="secondary",
        ),
        _action(
            action_id="peer_nudge_weak",
            label=f"Nudge {nick} anyway",
            message=f"introduce me to {nick}",
            style="primary",
            peer_user_id=peer_user_id,
        ),
    ]


def block_log_nudge_actions(
    *,
    nickname: str | None = None,
    peer_user_id: str | None = None,
) -> list[dict[str, Any]]:
    """Featured block-log match — nudge neighbor #1."""
    nick = str(nickname or "").strip()
    generic = nick.lower() in ("", "a neighbor", "a neighbor on your block", "neighbor match")
    if generic:
        label = "Introduce me to #1"
        message = "introduce me to #1"
    else:
        label = f"Send {nick} a nudge"
        message = f"introduce me to {nick}"
    return [
        _action(
            action_id="block_log_nudge",
            label=label,
            message=message,
            style="primary",
            peer_user_id=peer_user_id,
        ),
        _action(
            action_id="block_log_pass",
            label="Not now",
            message="maybe later",
            style="secondary",
        ),
    ]


def hosting_open_actions(*, matches_nearby: int = 0) -> list[dict[str, Any]]:
    """C-4-EVENT-P3 — open meetup vs share off-app."""
    _ = matches_nearby
    return [
        _action(
            action_id="hosting_open",
            label="Open the meet up",
            message="open the meet up",
            style="primary",
        ),
        _action(
            action_id="hosting_send",
            label="Send to a mom",
            message="send to a mom",
            style="secondary",
        ),
    ]


def tip_pass_actions() -> list[dict[str, Any]]:
    """C-4-RECO-P3 — pass tip vs invite a mom."""
    return [
        _action(
            action_id="tip_pass",
            label="Pass the tip along",
            message="pass the tip along",
            style="primary",
        ),
        _action(
            action_id="tip_send_mom",
            label="Send to a mom",
            message="send to a mom",
            style="secondary",
        ),
    ]


def signal_saved_actions() -> list[dict[str, Any]]:
    """After swap/meet/tip seek saved — check block log for matches."""
    return [
        _action(
            action_id="signal_show_block_log",
            label="Show my block log",
            message="show my block log",
            style="primary",
        ),
        _action(
            action_id="signal_wait",
            label="Not yet",
            message="maybe later",
            style="secondary",
        ),
    ]


def signal_listen_actions() -> list[dict[str, Any]]:
    """Deprecated alias — use signal_saved_actions."""
    return signal_saved_actions()


def attach_intro_row_actions(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    if str(out.get("direction") or "") != "received":
        return out
    if out.get("status") and str(out["status"]) != "proposed":
        return out
    out["actions"] = intro_respond_actions(
        nickname=str(out.get("nickname") or ""),
        intro_id=str(out.get("intro_id") or out.get("id") or "") or None,
    )
    return out


def derive_ui_actions(ctx: dict[str, Any], ui_intent: str) -> list[dict[str, Any]]:
    """Top-level bubble CTAs (outside chat composer) for the current turn."""
    from app.ui_intent import (
        UI_INTENT_OFFER_NEIGHBOR_INTRO,
        UI_INTENT_PROPOSE_NEIGHBOR_INTRO,
        UI_INTENT_RESPOND_PENDING_INTRO,
        UI_INTENT_SHOW_BLOCK_LOG,
        UI_INTENT_SHOW_PEER_PREVIEW,
        UI_INTENT_SHOW_PENDING_INTROS,
        UI_INTENT_SIGNAL_SAVED,
    )

    if ui_intent == UI_INTENT_RESPOND_PENDING_INTRO:
        pending = ctx.get("pending_intro_respond")
        if not isinstance(pending, dict):
            return []
        return intro_respond_actions(
            nickname=str(pending.get("nickname") or ""),
            intro_id=str(pending.get("intro_id") or "") or None,
        )

    if ui_intent == UI_INTENT_OFFER_NEIGHBOR_INTRO:
        offer = ctx.get("pending_intro_offer")
        if isinstance(offer, dict):
            return intro_offer_actions(
                nickname=str(offer.get("candidate_nickname") or ""),
                peer_user_id=str(offer.get("candidate_user_id") or "") or None,
            )
        return intro_offer_actions()

    if ui_intent == UI_INTENT_PROPOSE_NEIGHBOR_INTRO:
        intro = ctx.get("intro_proposal")
        if isinstance(intro, dict):
            return [
                _action(
                    action_id="intro_sent_ack",
                    label="Got it",
                    message="show my intros",
                    style="primary",
                ),
            ]

    if ui_intent == UI_INTENT_SIGNAL_SAVED:
        saved = ctx.get("signal_saved")
        if isinstance(saved, dict) and str(saved.get("intent") or "") == "host_meet":
            if saved.get("hosting_opened"):
                return []
            matches = int(saved.get("matches_created") or 0)
            return hosting_open_actions(matches_nearby=matches)
        if isinstance(saved, dict) and str(saved.get("intent") or "") == "tip_share":
            if saved.get("tip_passed"):
                return []
            return tip_pass_actions()
        return signal_saved_actions()

    if ui_intent == UI_INTENT_SHOW_PEER_PREVIEW:
        from app.peer_discovery_surface import weak_match_ui_actions

        return weak_match_ui_actions(ctx)

    if ui_intent == UI_INTENT_SHOW_BLOCK_LOG:
        dup = ctx.get("recent_intro_duplicate")
        if isinstance(dup, dict) and dup.get("candidate_user_id"):
            if ctx.get("pending_intro_respond"):
                pend = ctx.get("pending_intro_respond")
                if isinstance(pend, dict):
                    return intro_respond_actions(
                        nickname=str(pend.get("nickname") or ""),
                        intro_id=str(pend.get("intro_id") or "") or None,
                    )
            return duplicate_intro_sent_actions()
        entries = ctx.get("block_log_entries")
        if isinstance(entries, list) and entries:
            row = entries[0]
            if isinstance(row, dict):
                return block_log_nudge_actions(
                    nickname=str(row.get("peer_preview_label") or ""),
                    peer_user_id=str(row.get("peer_user_id") or "") or None,
                )
        return []

    # Inbox list — actions live on each received row, not duplicated below the bubble.
    if ui_intent == UI_INTENT_SHOW_PENDING_INTROS:
        return []

    return []
