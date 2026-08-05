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


def clarify_chip_actions(options: list[str]) -> list[dict[str, Any]]:
    """Tap-able answers for a clarify question (scope / browse-vs-meet). Tapping posts the
    label back as a normal message, which the next turn re-classifies to route the user."""
    rows: list[dict[str, Any]] = []
    for i, opt in enumerate(options):
        label = str(opt or "").strip()
        if not label:
            continue
        rows.append(
            _action(
                action_id=f"clarify_{i}",
                label=label,
                message=label,
                style="primary" if i == 0 else "secondary",
            )
        )
    return rows[:3]


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


def community_profile_actions(*, place_name: str, relation: str) -> list[dict[str, Any]]:
    """The primary CTA on a community profile (C-CIRCLE-COMM-PROFILE): create a meet
    there. It posts a normal chat message, so hosting stays one implementation — a meet
    created here is just a meet whose venue Lana already knows.

    "Invite people" is deliberately NOT here: minting a labeled invite link and opening
    the share sheet is a native FE action (/lana/invites/mint with the profile's
    circle_key), and a message-posting chip for it would route nowhere (there is no
    invite intent in chat) — the same reason event_created_actions returns nothing.
    """
    _ = relation
    name = str(place_name or "").strip()
    if not name:
        return []
    return [
        _action(
            action_id="community_create_event",
            label="Create an event",
            message=f"I want to host something at {name}",
            style="primary",
        ),
    ]


def community_join_actions(communities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One "Join <place>" chip per nearby community listed in chat.

    The `message` is the canonical payload the join reader matches when the LLM is
    unavailable, so it must stay literal ("Join Lp Fit"). Names are the places'
    real names — the FE localizes the LABEL only, same contract as the tip offer.
    """
    rows: list[dict[str, Any]] = []
    for i, c in enumerate(communities or []):
        if not isinstance(c, dict):
            continue
        name = str(c.get("place_name") or "").strip()
        if not name:
            continue
        emoji = str(c.get("emoji") or "").strip()
        rows.append(
            _action(
                action_id=f"community_join_{i}",
                label=f"{emoji} Join {name}".strip(),
                message=f"Join {name}",
                style="primary" if i == 0 else "secondary",
            )
        )
        if len(rows) >= 3:
            break
    return rows


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
            label="Send to a parent",
            message="send to a parent",
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
            label="Send to a parent",
            message="send to a parent",
            style="secondary",
        ),
    ]


def signal_saved_actions() -> list[dict[str, Any]]:
    """After swap/meet/tip seek saved — check block log for matches."""
    return [
        _action(
            action_id="signal_show_block_log",
            label="Show my neighborhood log",
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


def rec_widen_actions(noun: str) -> list[dict[str, Any]]:
    """After a claim-personalized tip recommendation — let the user act on the "want me to
    widen?" offer. "See all …" posts a message whose "show me all" phrasing the tip_seek
    fallback reads as a widen (skips personalization → unfiltered nearby list). Block-log
    stays as the secondary."""
    label_noun = str(noun or "").strip() or "options"
    return [
        _action(
            action_id="rec_widen",
            label=f"See all {label_noun}",
            message=f"show me all {label_noun}",
            style="primary",
        ),
        _action(
            action_id="signal_show_block_log",
            label="Show my neighborhood log",
            message="show my block log",
            style="secondary",
        ),
    ]


def rec_chip_actions(chips: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Render the tip personalizer's chips (ask-first angle picks, or post-result refine
    chips). Each chip is {label, message, style?}; tapping posts `message`, which re-enters
    the tip_seek fallback with that angle (or a widen). Falls back to the plain saved-signal
    actions when the list is empty/malformed."""
    out: list[dict[str, Any]] = []
    for i, c in enumerate(chips or []):
        if not isinstance(c, dict):
            continue
        label = str(c.get("label") or "").strip()
        message = str(c.get("message") or "").strip()
        if not label or not message:
            continue
        out.append(
            _action(
                action_id=f"rec_chip_{i}",
                label=label,
                message=message,
                style=str(c.get("style") or "secondary"),
            )
        )
    return out or signal_saved_actions()


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


def activity_browse_actions(ctx: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Pills for the agentic browse. When a search came up empty we offer the seek fallback
    (listen for me / widen) — tapping posts the label back, which run_activity_browse_turn
    reads as accept/widen. Otherwise the lane's draft owns the chips (draft.suggestions):
    interest chips only on the P1 ask, event-tag refine chips under results, none on a ZIP
    ask — no unconditional hardcoded category list."""
    draft = (ctx or {}).get("browse_draft")
    if not isinstance(draft, dict):
        return []
    if draft.get("_seek_offer"):
        return [
            _action(action_id="browse_seek_yes", label="Yes, listen for me",
                    message="Yes, listen for me", style="primary"),
            _action(action_id="browse_seek_widen", label="Widen the search",
                    message="Widen the search", style="secondary"),
        ]
    labels = [str(s).strip() for s in (draft.get("suggestions") or []) if str(s).strip()]
    return [
        _action(
            action_id=f"browse_{label.split()[0].lower()}",
            label=label,
            message=label,
            style="secondary",
        )
        for label in labels[:4]
    ]


def peer_seek_offer_actions() -> list[dict[str, Any]]:
    """Pills under an empty peers search — mirror of the browse lane's seek offer.
    Tapping posts the message; discovery_route._try_peer_seek_offer_reply_turn reads
    it next turn (notify → save a seek signal; widen → drop the filter and show
    neighbors nearby). Labels must match what format_attr_peers_reply promises."""
    return [
        _action(
            action_id="peer_seek_notify",
            label="Yes, notify me",
            message="Yes, notify me when someone like that joins",
            style="primary",
        ),
        _action(
            action_id="peer_seek_widen",
            label="Show everyone nearby",
            message="Show everyone nearby",
            style="secondary",
        ),
    ]


def tip_ask_offer_actions(rec_chips: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Pills under a recommendation answer — the offer that gates the posting.

    The accept/decline messages are the canonical English strings discovery_route compares
    against when the LLM offer-reader is unavailable (_read_offer_reply), so they must match
    _TIP_ASK_ACCEPT_MSG / _TIP_ASK_DECLINE_MSG exactly. Labels are localized at render.

    The reply's LAST question is the offer, so accept leads. One refine chip from the
    personalizer ("Vegetarian", "See all restaurants") rides along when there is one —
    otherwise arming the offer would silently hide the angles the personalizer just found."""
    rows = [
        _action(
            action_id="tip_ask_yes",
            label="Yes, ask my neighbors",
            message="Yes, ask my neighbors",
            style="primary",
        )
    ]
    for i, c in enumerate(rec_chips or []):
        if not isinstance(c, dict):
            continue
        label = str(c.get("label") or "").strip()
        message = str(c.get("message") or "").strip()
        if label and message:
            rows.append(
                _action(
                    action_id=f"tip_rec_{i}", label=label, message=message, style="secondary"
                )
            )
            break
    rows.append(
        _action(
            action_id="tip_ask_no",
            label="No, just the list",
            message="No, just the list",
            style="secondary",
        )
    )
    return rows


def posting_manage_actions() -> list[dict[str, Any]]:
    """Pills after a posting went out — the removal offer is now real (close_local_signal),
    so it is safe to show. Message must match discovery_route._POSTING_REMOVE_MSG."""
    return [
        _action(
            action_id="posting_show_log",
            label="Show my neighborhood log",
            message="show my block log",
            style="primary",
        ),
        _action(
            action_id="posting_remove",
            label="Take it down",
            message="Take my posting down",
            style="secondary",
        ),
    ]


def event_created_actions() -> list[dict[str, Any]]:
    """After an event publishes the FE renders the native CTAs (Open the meet up /
    Share with a mom) — those navigate / open the share sheet, which a message-sending
    ui_action can't do — so no server-driven pills here."""
    return []


def _rapport_action_chip(action: dict[str, Any]) -> dict[str, Any] | None:
    """One action chip for a concierge reply. Both the label and the message posted on tap
    are authored by the model (action.label / action.send) — routing stays AI-owned; we
    only pass the strings through, no kind→message mapping here."""
    label = str(action.get("label") or "").strip()
    message = str(action.get("send") or "").strip()
    if not label or not message:
        return None
    return _action(action_id="rapport_action", label=label, message=message, style="primary")


def rapport_reply_actions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Tap-able chips for a concierge reply to a "By the way…" tile answer — either the
    suggested first-person answers to a follow-up question, or a single action chip.
    Tapping posts the message back to Lana as a normal turn."""
    options = payload.get("options")
    if isinstance(options, list) and options:
        rows: list[dict[str, Any]] = []
        for i, opt in enumerate(options):
            if not isinstance(opt, dict):
                continue
            label = str(opt.get("label") or "").strip()
            if not label:
                continue
            # Post the SHORT value she tapped (the pill label), NOT the model's full
            # first-person sentence — the sentence puts words in her mouth and its extra
            # context can skew the read of her answer. The concierge still gets the tile
            # question for context, so a terse answer ("solo") reads fine.
            message = label
            rows.append(
                _action(
                    action_id=f"rapport_opt_{i}",
                    label=label,
                    message=message,
                    style="primary" if i == 0 else "secondary",
                )
            )
            if len(rows) >= 4:
                break
        if rows:
            return rows
    action = payload.get("action")
    if isinstance(action, dict):
        chip = _rapport_action_chip(action)
        if chip:
            return [chip]
    return []


def derive_ui_actions(ctx: dict[str, Any], ui_intent: str) -> list[dict[str, Any]]:
    """Top-level bubble CTAs (outside chat composer) for the current turn."""
    from app.ui_intent import (
        UI_INTENT_EVENT_CREATED,
        UI_INTENT_OFFER_NEIGHBOR_INTRO,
        UI_INTENT_PROPOSE_NEIGHBOR_INTRO,
        UI_INTENT_RESPOND_PENDING_INTRO,
        UI_INTENT_SHOW_ACTIVITY_PREVIEW,
        UI_INTENT_SHOW_BLOCK_LOG,
        UI_INTENT_SHOW_PEER_PREVIEW,
        UI_INTENT_SHOW_PENDING_INTROS,
        UI_INTENT_SIGNAL_SAVED,
    )

    # Clarify questions (scope / browse-vs-meet) carry their tap-able answers here. They
    # render regardless of ui_intent (the turn is otherwise a plain "chat" reply).
    clarify_opts = ctx.get("clarify_options")
    if isinstance(clarify_opts, list) and clarify_opts:
        chips = clarify_chip_actions([str(o) for o in clarify_opts])
        if chips:
            return chips

    # decide_turn policy chips — label + send are policy-authored (lexicon-enforced
    # upstream by lingo_guard). Render regardless of ui_intent, same as clarify.
    policy_chips = ctx.get("policy_chips")
    if isinstance(policy_chips, list) and policy_chips:
        rows = []
        for i, c in enumerate(policy_chips[:3]):
            label = str((c or {}).get("label") or "").strip() if isinstance(c, dict) else ""
            if not label:
                continue
            send = str(c.get("send") or label).strip()
            rows.append(
                _action(
                    action_id=f"policy_{i}",
                    label=label,
                    message=send,
                    style="primary" if i == 0 else "secondary",
                )
            )
        if rows:
            return rows

    # Empty peers search — the reply offers "notify me / widen"; these pills ARE those
    # options. Renders on ui_intent chat (zero matches never reach show_peer_preview).
    if ctx.get("peer_seek_offer"):
        return peer_seek_offer_actions()

    # Nearby communities just listed in chat ("Join Lp Fit"). Renders on a plain chat
    # turn, so it can't hang off a ui_intent — same as the clarify / policy chips.
    discovery = ctx.get("community_discovery")
    if isinstance(discovery, dict):
        chips = community_join_actions(discovery.get("communities") or [])
        if chips:
            return chips

    # Recommendation-ask surfaces. These render regardless of ui_intent because the answer
    # turn no longer writes a signal (so there is no signal_saved / UI_INTENT_SIGNAL_SAVED to
    # hang them off) — the chips must answer the question the reply actually ended on:
    #   tip_ask_offer   → "want me to ask your neighbors too?" (the write is a tap away)
    #   posting_manage  → the posting went out; "Take it down" is now backed by a real RPC
    #   rec_chips       → the angle pick / widen chips when no offer was armed this turn
    rec_chips_any = ctx.get("rec_chips")
    rec_chips_any = rec_chips_any if isinstance(rec_chips_any, list) else []
    if ctx.get("tip_ask_offer"):
        return tip_ask_offer_actions(rec_chips_any)
    if ctx.get("posting_manage"):
        return posting_manage_actions()
    if rec_chips_any:
        return rec_chip_actions(rec_chips_any)

    # Concierge reply to a rapport tile answer — suggested answers or one action chip.
    # Also render regardless of ui_intent (the turn is a plain "chat" reply).
    rapport_reply = ctx.get("rapport_reply")
    if isinstance(rapport_reply, dict):
        chips = rapport_reply_actions(rapport_reply)
        if chips:
            return chips

    if ui_intent == UI_INTENT_EVENT_CREATED:
        return event_created_actions()

    # Agentic browse — refine chips so the user can narrow ("Sports", "Outdoors").
    if ui_intent == UI_INTENT_SHOW_ACTIVITY_PREVIEW and ctx.get("activity_browse_active"):
        return activity_browse_actions(ctx)

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
        # Claim-personalized tip rec chips take priority: ask-first angle picks
        # ("Vegetarian" / "Kid-friendly" / "Just show all") or post-result refine chips.
        rec_chips = ctx.get("rec_chips")
        if isinstance(saved, dict) and str(saved.get("intent") or "") == "tip_seek" and rec_chips:
            return rec_chip_actions(rec_chips)
        # Legacy single "See all …" widen chip (kept for the plain personalized path).
        rec_noun = ctx.get("rec_widen_noun")
        if isinstance(saved, dict) and str(saved.get("intent") or "") == "tip_seek" and rec_noun:
            return rec_widen_actions(str(rec_noun))
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
