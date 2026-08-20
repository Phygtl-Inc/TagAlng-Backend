# TPR: Lana User Funnel — Onboarding to Power User

**Author:** Daniel Walter de Paula · Data & AI
**Date:** August 2026
**Status:** Proposal for team discussion

---

## 1. Objective

This document proposes a single, event-gated funnel describing a mom's journey through TagAlng/Lana, from onboarding to becoming a power user. The goal is to give the team a shared, precise vocabulary for where users are in their journey — replacing ad hoc, inconsistent notions of "engaged" or "active" user with a definition tied to real, verifiable events. This follows the same approach validated on the Vyry funnel, adapted to Lana's actual product mechanics and event taxonomy.

## 2. Design Principles

- One funnel, kept deliberately simple — not three parallel funnels as in the original Vyry model. Simplicity was prioritized over completeness wherever it didn't cost essential information.
- Every stage is gated by a real, verifiable event or condition — never by a vague notion of "engagement."
- Passive signals (e.g. viewing an invite) do not count as progression — only actions that show real commitment do. This mirrors the correction made to the Vyry funnel, where `invite_viewed` was distinguished from `invite_activated`.
- Power user status is defined structurally (what the user has built or sustained), not by raw frequency — same principle applied in the Vyry PowerUser correction.

## 3. The Funnel

![Lana user funnel — five stages from Onboarded to Power user](assets/lana-user-funnel.png)

*Illustration only — for shared understanding in discussion, not a rendered data visualization.*

| # | Stage | Gate event / condition | Note |
|---|---|---|---|
| 1 | **Onboarded** | Signup + phone verified | Only phone-verified accounts count toward the product's own "active" definition (Circles spec anti-gaming rule). |
| 2 | **Grounded** | `circle_grounded` OR `circle_invite_self_confirm` | First real commitment — not passive. Both events resolved via the Circles product spec. |
| 3 | **Connected** | `connection_made` | Working definition, from the CTO Spec. The more recent Circles spec doesn't name this event directly, but conceptually it lines up with the relationship ladder's "Direct" tier (mutual unmask — names, photos, schedules exchanged). Treated as the same underlying mechanism unless Backend confirms otherwise — low-priority item to verify, not a blocker. |
| 4 | **Met IRL** | Co-presence at an event + 24 hours | Reuses the Circles spec's own locked definition of "IRL peer" (ladder tier 5) rather than relying solely on `event_checkin`, which is not confirmed to be firing reliably. |
| 5 | **Power user** | `event_hosted` 2+ times, OR `founding_earned_at` set, OR a vouch performed | Structural commitment, not frequency — mirrors the correction made to the Vyry PowerUser definition. Hosting threshold (2) is a starting point, to be revisited as the user base and hosting distribution grow. |

## 4. This Is a Living Framework

The five stages reflect the product as it exists today. Some parts of the product that will materially affect this funnel are still in development and not yet production-ready — most notably the Community surface, which sits alongside Circles but does not yet have real user activity flowing through it (`community_create_event`, `community_activity_added`, and `community_feature_added` exist in the event taxonomy but are pre-production). As Community and other in-development features reach production, this funnel is expected to gain new stages or have existing ones reshaped — it should not be treated as a fixed, permanent model. Recommendation: revisit this document each time a major feature crosses from in-development to live.

## 5. Open Questions

- Is the Stage 5 hosting threshold (2+) right? It's a starting assumption, not a data-backed number — the actual distribution of how many times hosts have hosted hasn't been checked yet. Should be revisited once the user base grows.
- Does a neighborhood's ZIP-unlock state (`closed` / `warming` / `open`) need to be tracked alongside individual funnel stage? A mom in a closed or warming block may be unable to progress past Stage 3 or 4 simply due to local supply scarcity, not lack of engagement — worth keeping in mind when interpreting drop-off, even if it doesn't become a stage of its own.
- Confirm `connection_made` still fires in the current codebase and lines up with the Circles ladder's "Direct" tier as assumed in Section 3 — low priority, does not block building the funnel in Amplitude.

## 6. Recommended Next Steps

1. Build and validate the funnel in Amplitude (Funnel Analysis + Retention), using `connection_made` as the Stage 3 gate; confirm alignment with the Circles ladder in parallel with Backend, not as a prerequisite.
2. Once stable, mirror the definition into Supabase for investor-facing reporting, if and when this funnel is adopted as an investor metric.
3. Check real hosting-frequency distribution before finalizing the Stage 5 threshold.
