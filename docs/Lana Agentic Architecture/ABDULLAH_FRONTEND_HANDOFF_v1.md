# Abdullah · Frontend lead · handoff · v1

*Your tour of v2 · what you own · what to ship · 2026-06-12*

> Abdullah · the mockup work in v0.2.x is essentially production-ready for Day Zero. The handoff now is: (1) **port mockup → production** with high fidelity, (2) **wire the unified-chat API contract** from `lana-worker`, (3) prepare for v0.3 surfaces (Block-Log · latent suggestions · Lana state animations). You consume backend contracts · render mom's experience · don't touch AI logic or schema design.

---

## §1 · What you own

| Area | Scope |
|---|---|
| **PWA + native mobile UI** | The Next.js app · iOS + Android via Expo or similar · the Vercel deployment · everything the user sees and touches |
| **Mockup → production parity** | Take the v0.2.10 walkthrough and explorer · port every frame to actual React components with real API integration |
| **The `ui_intent` switcher** | Read `ui_intent` every turn · render the right input chrome (text · phone · OTP · ZIP · file picker · etc.) |
| **The 4-state Lana mascot** | Wire the `data-lana-state` attribute to backend signals · animate the 4 states per `LANA_STATE_MODEL_v1.md` |
| **The rail-collapse compass interaction** | v0.2.10 pattern · in production with real navigation · not just static mockup |
| **The CSS-radio tab pattern** | v0.2.7 pattern in C-13 RADAR · ported · with backend-provided tab data |
| **The Block-Log surface** | v0.2.8 mock → production · pulls from `GET /lana/users/{me}/block-log` |
| **Auth flow integration** | `auth_action` handling · Supabase signup / login / logout / OTP per `LANA_UNIFIED_DISCOVERY_FRONTEND.md` |
| **iOS-safe inputs** | 16px+ font-size · proper `inputmode` · keyboard avoidance |
| **Mobile responsiveness** | All breakpoints (320px · 375px · 414px · 768px) |

---

## §2 · What you DON'T own

| Area | Who owns |
|---|---|
| AI integration logic · prompts · model behavior | Yunchao |
| Backend schema · API design · ranker | Asjid |
| Product spec · Lana voice copy · brand decisions | Tommaso |
| Marketing site (`lana.help` / `tagalng.com`) | Tommaso + designer (separate stack) |

You consume APIs. You don't design them. If an API doesn't fit what you need, push back · negotiate · agree · then implement.

---

## §3 · Week-1 priorities (June 13-19 · pre-Day-Zero)

### Critical · must ship for Day Zero
1. **Port the FTUE** (C-INTRO-1 + C-INTRO-2) from mockup to production React components · animated · responsive · ~6 hours
2. **Port the LOOKING capture cascade** (C-3-look · C-4-look-swap-P1/P2/P3/P4) · wire to `POST /lana/sessions/{id}/messages` · ~8 hours
3. **Port the canonical signup** (C-SIGNUP-1/2/3/4) · wire to `auth_action` handling · `ui_intent: collect_phone/collect_otp` · ~6 hours
4. **Port the sign-in flow** (C-SIGNIN-1/2/3) · same auth contract · ~3 hours
5. **Port the RADAR popover** (C-13 + variants · with CSS-radio tabs · with Block-Log preview) · wire to `GET /lana/users/{me}/block-log` · ~8 hours
6. **Wire the compass aggregator nav** (v0.2.10 pattern) · tap-to-expand with backdrop · ~3 hours
7. **Implement the 16px iOS-safe input pattern** for all inputs · verify no keyboard zoom · ~2 hours

**Total: ~36 hours over the week.** Tight but doable if you focus.

### High priority · ship in v0.2
8. The 4-state Lana mascot integration (when the 4 SVG poses are delivered · wire `data-lana-state` switching based on turn state)
9. The full Block-Log experience (not just the preview · the tap-into-match-detail flow · the nudge-from-match action)
10. The proactive Lana surface ("by the way..." inline turns from Layer 3) · render when backend sends `latent_suggestion`

---

## §4 · API consumption surface

You consume FOUR endpoint patterns. Memorize them.

### 4.1 · The conversation loop (per `LANA_UNIFIED_DISCOVERY_FRONTEND.md`)

```typescript
// Every user message:
const turn = await fetch('/lana/sessions/{id}/messages', {
  method: 'POST',
  headers: { Authorization: `Bearer ${accessToken}` },
  body: JSON.stringify({ message: userText })
}).then(r => r.json());

// turn contains:
applyTurn(turn);

function applyTurn(turn) {
  renderBubble(turn.assistant_message);              // Lana voice
  setLanaState(deriveLanaState(turn));               // 4-state mascot
  setUiChrome(turn.ui_intent);                       // input field type
  if (turn.peer_matches) renderPeerCards(turn.peer_matches);
  if (turn.activity_previews) renderActivityCards(turn.activity_previews);
  if (turn.recommendations) renderHeterogeneousRecs(turn.recommendations);  // v0.2 NEW
  if (turn.latent_suggestion) renderLatentBubble(turn.latent_suggestion);   // v0.2 NEW
  
  // Auth side-effects
  const action = authActionFromTurn(turn);
  if (action) handleLanaAuthAction(action);
}
```

### 4.2 · The auth flow

Five `auth_action` types per `LANA_UNIFIED_DISCOVERY_FRONTEND.md §C/D`:
- `link_phone_signup` → PUT /auth/v1/user
- `verify_signup_otp` → POST /auth/v1/verify (type: phone_change)
- `send_login_otp` → POST /auth/v1/otp
- `verify_login_otp` → POST /auth/v1/verify (type: sms)
- `logout` → signOut() + anonymous re-signup

You implement these as a single dispatcher · `apps/admin/lib/demo-user.ts` already has the reference impl · port to your codebase.

### 4.3 · The Block-Log surface

```typescript
// On RADAR popover open with Block tab active:
const blockLog = await fetch('/lana/users/{me}/block-log').then(r => r.json());
// blockLog.entries = [{match_id, match_type, peer_preview_label, ...}]

// On user action:
await fetch('/lana/block-log/{entryId}/action', {
  method: 'POST',
  body: JSON.stringify({ action: 'nudged' | 'dismissed' | 'saved' | 'ignored' })
});
```

### 4.4 · Recommendation impressions

```typescript
// Every time you SURFACE a recommendation:
await fetch('/lana/recommendations/{recId}/impression', {
  method: 'POST',
  body: JSON.stringify({ surfaced_at: new Date().toISOString() })
});

// When user takes action:
await fetch('/lana/recommendations/{recId}/impression', {
  method: 'POST',
  body: JSON.stringify({ user_action: 'accepted', action_at: new Date().toISOString() })
});
```

---

## §5 · The `ui_intent` → UI map

From `LANA_UNIFIED_DISCOVERY_FRONTEND.md` plus v0.2 additions. Wire each value to its UI shell.

| `ui_intent` | UI to render |
|---|---|
| `chat` | Default text composer + bell mic |
| `collect_zip` | Numeric input · max 5 digits · "32827" placeholder |
| `collect_identity` | Multi-line text composer · "Tell me about you..." |
| `collect_display_name` | Single-line text · "Your name or a nickname..." |
| `collect_phone` | Tel input · 16px JBM font · "(407) 555-0198" placeholder |
| `collect_otp` | 6-box dashed OTP input (per C-SIGNUP-2/3) |
| `show_peer_preview` | Redacted peer cards · NO names/avatars |
| `show_activity_preview` | Activity cards (events, meets) |
| `confirm_profile` | MAPPED YOU layout · "That's me ✓" CTA |
| `upload_profile_photo` | "Add photo" button → file/camera picker |
| `sign_out` | Sign-out confirmation · execute the logout flow |
| `show_heterogeneous_recs` (NEW v0.2) | Mixed cards · ranked · with reason text · tap to act |
| `show_latent_suggestion` (NEW v0.2 · maybe v0.3) | Inline "by the way..." pill above composer |

---

## §6 · The 4-state Lana mascot integration

Per `LANA_STATE_MODEL_v1.md` · once the 4 SVG poses are delivered:

```typescript
function deriveLanaState(turn): 'idle' | 'talking' | 'listening' | 'thinking' {
  if (turn.streaming) return 'talking';
  if (turn.isUserComposing) return 'listening';
  if (turn.isRequestInFlight) return 'thinking';
  if (turn.ui_intent.startsWith('collect_')) return 'listening';
  if (turn.ui_intent === 'upload_profile_photo') return 'listening';
  if (turn.routing_phase?.startsWith('await_')) return 'listening';
  return 'idle';
}

function applyLanaState(state) {
  document.querySelectorAll('.lana-svg-wrap').forEach(el => {
    el.setAttribute('data-lana-state', state);
  });
}
```

CSS in `LANA_STATE_MODEL_v1.md §2.3` already specifies the show/hide rules + animations.

---

## §7 · The trust-tier UI gating

Different content visible at different tiers. Apply across all peer-rendering components:

| Trust tier | What to show |
|---|---|
| `stranger` | "A mom in your block" · reason text only · NO name · NO avatar · "Nudge" CTA visible |
| `nudge_pending` | First name visible · still no avatar · "Awaiting response" status |
| `acquaintance` | Full first name · avatar · neighborhood (not exact address) |
| `direct` | Full preview · activity history visible |
| `irl` | Full access · chat threads · etc. |

Backend enforces via `peer_preview_label` (per `LANA_UNIFIED_DISCOVERY_FRONTEND.md` "Preview vs full peer_matches"). You just render what the API returns · don't try to enhance.

---

## §8 · Mockup → production parity rules

Per the project CLAUDE.md:
- **Use canonical components only** from `PWA_INVENTORY_v1.md`
- **iOS-safe inputs** at 16px+ font-size · real `<input>` elements · no styled divs
- **Sheep via `<use href="#sheep-brand"/>`** · never redraw
- **DM Sans for UI · Fraunces for display · JetBrains Mono for data**
- **No turn-counter pills** in Lana conversations
- **No auto-wrap by turn count** · user controls flow
- **`.active` class only** for screen visibility · no `style.display='flex'` inline

When porting a mockup frame to production:
1. Read the EXACT markup from `lana-v01-walkthrough.html`
2. Pull HTML structure verbatim (don't reinvent)
3. Replace mock data with API integration points
4. Verify mobile responsiveness across breakpoints
5. Smoke-test in production preview before merging

---

## §9 · Week-2-to-Month-1 priorities

### Production hardening
1. Error handling — every API call · structured error display · "Lana is taking a break, try again" not raw errors
2. Loading states — skeleton screens · loading indicators · "Lana is thinking..." with the thinking mascot state
3. Offline fallback — at minimum a "you're offline" screen that doesn't break the UX
4. Push notification handling — open the right deep-link based on notification payload

### v0.3 prep
5. The 4-state Lana mascot animations (when SVGs delivered)
6. The latent suggestion inline bubble pattern
7. The proactive Lana check-in (push notification → open Lana with pre-filled context)

### Polish
8. Mobile keyboard handling — ensure inputs don't get hidden when keyboard opens
9. Accessibility — VoiceOver / TalkBack compatibility · keyboard navigation
10. Performance — lazy-load images · code-split the routes · keep TTFI < 3s on 4G

---

## §10 · Reading list

| # | Doc | Why |
|---|---|---|
| 1 | `LANA_BLUEPRINT_v1.md` | Strategic context · esp §3 · §6 |
| 2 | `LANA_UNIFIED_DISCOVERY_FRONTEND.md` | THE API contract · read end-to-end · this is your interface |
| 3 | `LANA_STATE_MODEL_v1.md` | The 4-state mascot you'll wire |
| 4 | `BLOCK_LOG_SPEC_v1.md` §3, §5 | The Block-Log UI you'll build |
| 5 | `app/public/lana-v01-walkthrough.html v0.2.10` | The canonical mockup · port from this |
| 6 | `PWA_INVENTORY_v1.md` | Canonical component library |
| 7 | `BRAND.md` | Voice + visual + spacing rules |

---

## §11 · Open questions back to Tommaso

1. **PWA-first or native-first?** v0.2 mockup is PWA. v0.3+ may want native (Expo/React Native). Decision impacts auth, push, file picker integration.
2. **Resend email integration** — when do we add email-based notifications (weekly digests)? UI affordance needed?
3. **Loading state Lana voice** — what does Lana SAY while thinking? Same as the mascot animation but in copy too?
4. **Error recovery copy** — when an API call fails, what's the Lana-voice fallback message? Need brand copy here.
5. **Onboarding skip mechanics** — currently the mockup has "skip for now" on the name capture. Is that enforced UI-side or backend-side? Need to confirm with Asjid.

---

*Abdullah · the mockup work is done. The next 36 hours is porting it to production with high fidelity. Then we sprint v0.2 together. You make Lana real for moms. — Tommaso (via Claude as CTO proxy) · 2026-06-12*
