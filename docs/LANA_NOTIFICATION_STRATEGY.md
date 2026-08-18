# User Notification Strategy: Push vs. Email Channel Selection

**Author:** Daniel Walter de Paula · Data & AI
**Date:** August 2026
**Companion file:** `Matriz_Notificacao_TagAlng.xlsx` (full matrix, distribution copy — this document is the source of truth for engineering)

---

## 1. Objective

TagAlng currently has two outbound channels for user communication in production — push notifications and email. SMS was part of the original design (account verification via Twilio) but is not implemented today. As the product surface grows, decisions about which channel to use for which event have been made ad hoc, feature by feature. This document proposes a consistent framework for selecting the right channel per event, applies that framework against the events actually flowing through the app today, and flags the small number of open questions that remain.

## 2. Rationale — Channel Selection Framework

Three questions decide the channel for any given event:

- **Urgency** — does the user need to know right now, while they're likely to be engaged, or can it wait?
- **Permanence** — does this need to leave a durable record the user can return to, or is it disposable once seen?
- **Action window** — is a response expected within seconds/minutes, or is this purely informational?

| Channel | When to use it | Example |
|---|---|---|
| **SMS** (not currently implemented) | Security / transactional only, highest urgency, user may not have the app open. Costly — used sparingly. Included here for completeness of the framework, not as a live channel. | OTP verification code at RSVP / refine / host — originally planned, since discontinued |
| **Push** | Time-sensitive, actionable in seconds, no permanent record needed. Best while the user is engaged or likely to re-engage quickly. | "X wants to join your meet", "You're in 🎉" |
| **Email** | Needs a permanent record, richer content, or isn't urgent. Also the natural fallback when push permission isn't granted. | Welcome/account confirmation, weekly digest, milestone updates |

**Note on implementation vs. framework:** the original CTO Spec described FCM (native push) and Twilio (SMS) as the target stack. What is actually implemented and confirmed live today is Web Push (VAPID, PWA-based) and transactional email via Resend — no SMS and no FCM in the current build. This is not necessarily a gap: FCM is scoped to Phase 3 (RN/Expo native app alpha) in the R&D Kickoff, so its absence may simply reflect that Phase 2 (PWA) hasn't reached that stage yet — worth confirming rather than assuming divergence. Phone-OTP, on the other hand, does appear deliberately discontinued: migration `20260606130000_users_phone_nullable.sql` makes the `users.phone` field optional, consistent with dropping the Twilio Verify requirement. Neither point changes the framework above, which is about channel type, not provider — see Open Questions for the CTO Spec update recommendation.

## 3. Methodology

Rather than relying on a manual survey of the team, the event inventory and current-state findings below come from three independent, verifiable sources:

- **Amplitude** — full event schema export (Data → Events), production domain (`get.lana.help`), last 30 days as of August 14, 2026.
- **Codebase** — direct inspection of `services/lana-worker/app/notifications.py` and `docs/LANA_NOTIFICATIONS_SETUP.md` in `TagAlng-Backend`, plus a targeted code search for known notification providers (`twilio`, `fcm`, `sendgrid`, `resend`, `postmark`) and for phone/OTP-related code (`phone`, `otp`, `verify_code`, `sms`).
- **Product specs** — `LANA_CIRCLES_ZIP_MASTER_v1.md` and related Circles/ZIP-Unlock design docs, used to resolve the meaning of several ambiguous `circle_*` event names.

This approach keeps the findings grounded in what is actually happening in production and in what is actually implemented in code, rather than in what documentation says should be happening — which, given the pace of product change, is not always current.

## 4. Findings Summary

| Metric | Value |
|---|---|
| Total distinct events (Amplitude, production, last 30 days) | 34 |
| Internal telemetry (Lana conversation flow — not notification candidates) | 17 |
| Events tied to already-implemented flows (Push + Email, live) | 3 |
| Events requiring a channel decision | 14 |
| &nbsp;&nbsp;— Recommended to build now (P1) | 2 |
| &nbsp;&nbsp;— Recommended for later / low volume (P2) | 3 |
| &nbsp;&nbsp;— Product decision — intentionally silent / self-action | 7 |
| &nbsp;&nbsp;— Needs meaning clarified before deciding channel | 1 |
| &nbsp;&nbsp;— Likely deprecated (no production activity since June) | 1 |

## 5. Notification Matrix

Full event-by-event detail — volume, exact recipient, current status, proposed copy, and notes — below. The same data, formatted for non-technical stakeholders, lives in `Matriz_Notificacao_TagAlng.xlsx`.

### 5.1 Already implemented (P0)

| Event | Recipient | Channel | Copy | Note |
|---|---|---|---|---|
| `event_hosted` | Host | Push + Email | "Your meet is live 🎉" | Live — confirm `VAPID_PRIVATE_KEY` / `RESEND_API_KEY` are set in production |
| `event_setup_submitted` | Host | Push + Email | — | Likely same trigger as `event_hosted` — see Open Questions |
| `event_open_meet` | Host | Push + Email | "X wants to join" / "X joined" | Live |
| Join (RPC-based, no distinct Amplitude event) | Joiner | Push + Email | "Request sent" / "You're in 🎉" | Not captured in Amplitude — FE→Supabase RPC direct, bypasses the worker |
| Host approval (RPC-based) | Joiner | Push + Email | "You're in 🎉" | Same as above |
| Host decline (RPC-based) | Joiner | Push only | "Update on …" | Push only, deliberately no email |

### 5.2 Build now (P1)

| Event | Group | Recipient | Vol 30d | Channel | Copy | Note |
|---|---|---|---|---|---|---|
| `community_create_event` | Community | Creator | 8 | Push + Email | "Your community event was created" | Reuses `event_hosted` pattern |
| `circle_invite_redeemed` | Circle | Inviter | 9 | Push only | "X joined via your invite" | Corrected: redemption is a growth signal only, not circle membership — "an invite is NOT membership" per Circles spec |

### 5.3 Build later (P2)

| Event | Group | Recipient | Vol 30d | Channel | Copy |
|---|---|---|---|---|---|
| `circle_invite_opened` | Circle | Inviter | 19 | Push | "X saw your invite" |
| `community_activity_added` | Community | Community members | 2 | Push | "New activity in [community]" |
| `item_listed` | Marketplace | Nearby neighbors | 0 | Push | "New item nearby" — feature not yet live |

### 5.4 Product decision — no notification

| Event | Group | Recipient | Vol 30d | Rationale |
|---|---|---|---|---|
| `event_invite_shared` | Hosting | Sender | 35 | Self-action, no notification needed |
| `circle_invite_minted` | Circle | Sender | 20 | Self-action, no notification needed |
| `circle_invite_shared` | Circle | Sender | 12 | Self-action, no notification needed |
| `circle_invite_self_confirm` | Circle | Joiner (self) | 11 | Self-action — post-redeem self-grounding prompt, never reveals inviter's circle, feedback given in-flow |
| `circle_added` | Circle | Self (own affiliation) | 4 | Circles are personal affiliations, not shared groups — no "added by someone else" concept |
| `circle_grounded` | Circle | Self (own affiliation) | 4 | In-chat grounding moment (Circles spec §A.1) — feedback already given by Lana in the conversation |
| `circle_removed` | Circle | Removed member | 2 | Intentional — avoids social friction |

### 5.5 Needs meaning clarified

| Event | Group | Vol 30d | Status |
|---|---|---|---|
| `community_feature_added` | Community | 1 | Name is ambiguous, volume too low to infer confidently |

### 5.6 Likely deprecated

| Event | Group | Vol 30d | Status |
|---|---|---|---|
| `venue_picked` | Marketplace | 0 | No occurrences since June 23. Strong hypothesis: same concept as `circle_grounded` (tapping a suggested place), possibly superseded by a newer event name |

### 5.7 Internal telemetry — not notification candidates

`rapport_gap_shown`, `rapport_open`, `rapport_cycle`, `rapport_feedback`, `rapport_dismiss`, `rapport_ground_chip`, `rapport_ground_confirm`, `rapport_gap_answered`, `rapport_gap_skipped`, `rapport_mute`, `rapport_skip`, `lana_turn`, `lana_feedback`, `flow_start`, `signal_saved`, `meet_seek_saved`, `faq_answered` (17 events) — internal telemetry from the Lana conversation flow.

## 6. Open Questions

None of the following block the work above from proceeding — they are consolidated here to be raised once, rather than as separate ad hoc questions during the process.

1. Are `VAPID_PRIVATE_KEY` and `RESEND_API_KEY` currently set in the production environment? (No Vercel access to verify directly — this is the one item that could reorder priorities, since all five P0 flows depend on it.)
2. What does `community_feature_added` represent?
3. `venue_picked` has had zero occurrences since June 23 — can it be retired from the Tracking Plan, or is it still expected to fire?
4. Should the CTO Spec be updated to reflect the current notification stack (Web Push + Resend, no SMS)? Note: FCM's absence may simply reflect that Phase 3 (native app) hasn't started — worth confirming before treating it as a divergence.
5. Are `event_setup_submitted` and `event_hosted` two stages of the same flow, or genuinely distinct triggers?

## 7. Recommended Next Steps

1. Confirm production env keys for push/email are active (Open Question 1).
2. Implement the two P1 events (`community_create_event`, `circle_invite_redeemed`) by reusing the existing `notify_user()` pattern in `app/notifications.py` — no new infrastructure required.
3. Resolve the remaining open questions in a single round with Backend/Frontend.
4. Revisit P2 events once their production volume justifies the build.
