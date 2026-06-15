# Block-Log · the missing match-visibility surface · v1

*Compiled 2026-06-12 · for Asjid (backend) · Abdullah (frontend) · Tommaso (founder review)*

> **The gap:** v0.2 has no marketplace · so when Lana finds a potential match for mom A, the only output is a notification fired to mom B (the ICP who'd be interested). Mom A herself doesn't see "what could match her" anywhere in-app. **That's a missing feature** the founder flagged.
>
> **The fix:** add a **Block-Log** view under the RADAR popover's `§1 · YOUR BLOCK` tab · shows the live state of "what's potentially matching for you in this block" · including outbound matches (others' offers/asks that could fit you) AND inbound matches (your offers/asks that have nearby fit-candidates).
>
> **Without this:** mom A only sees her own contributions ("I'm listening for 3T rain boots · 4 days") · she has no surface that says "by the way · 3 moms in your block have boots near your size."

---

## §1 · The two channels of match output

When the matcher finds a fit between mom A's signal and mom B's signal, the system produces **TWO outputs**:

### Channel 1 · Outbound notification (push/SMS)
- **Who:** mom B (the ICP who'd be interested)
- **When:** real-time at match creation
- **What:** SMS or push notification · "Lana: a mom in your block is looking for 3T rain boots · want me to introduce you?"
- **Where:** in their messaging app / device notification center
- **Action surface:** tap → opens Lana chat with a draft response
- **Status today:** PARTIAL · notification wiring partially built, no clean visible match-detail yet

### Channel 2 · Block-Log (in-app · MISSING today)
- **Who:** mom A (the asker · the original signal-creator)
- **When:** on demand · when mom opens the RADAR → BLOCK tab
- **What:** a log of "what's happening in your block that could match" · grouped by intent
- **Where:** in the BLOCK tab of the RADAR popover · below the lane status pill
- **Action surface:** tap a match-row → opens it for review or sends a nudge
- **Status today:** **MISSING** · this spec defines it

The asymmetry is intentional. Channel 1 is **push** (Lana surfaces the match to the most-likely buyer/responder). Channel 2 is **pull** (mom checks what's around when she has a moment).

---

## §2 · What goes in the Block-Log (data model)

Each entry in the Block-Log is a **match candidate** computed against mom's active `local_signals` + her identity profile + block proximity. The entries are NOT the raw signals · they are **enriched + ranked** match suggestions.

```typescript
type BlockLogEntry = {
  match_id: string;                       // UUID
  match_type:
    | 'inbound_for_my_seek'      // someone OFFERS what I'm seeking
    | 'inbound_for_my_offer'     // someone SEEKS what I'm offering
    | 'meet_invite_potential'    // someone hosting that fits my profile
    | 'meet_attendee_potential'  // I'm hosting · this mom might join
    | 'fellow_overlap_high'      // high-affinity fellow Lana noticed
    | 'tip_match'                // someone shared a tip relevant to a question I asked
  ;
  my_signal_id?: string;                  // the signal of mine this matches
  peer_signal_id?: string;                // the signal of theirs that matches
  peer_user_id_redacted: string;          // hashed/redacted until verified
  peer_preview_label: string;             // "Mom · 2 blocks away · pre-K · Brazilian"
  match_strength: number;                 // 0.0 - 1.0 (cosine + tag overlap)
  match_reasons: string[];                // ["3T boots seek matches her offer", "same pre-K stage"]
  block_id: string;
  created_at: string;
  expires_at: string;                     // 14d default for swap, 30d for meet, 7d for tip
  user_acted_at?: string;                 // when mom tapped the row
  action_taken?: 'nudged' | 'dismissed' | 'saved' | 'ignored';
  notification_sent_to_peer: boolean;     // was Channel 1 fired?
  notification_sent_at?: string;
};
```

**Privacy:** the `peer_user_id` is **redacted** until mom A has either (a) verified her phone OR (b) sent a nudge that mom B accepted. Until then, only `peer_preview_label` shows. Same redaction rule as `peer_matches` in the discovery API.

---

## §3 · Where Block-Log renders in the UI

In the RADAR popover · `§1 · YOUR BLOCK` tab · BELOW the lane status pill · ABOVE the "Take your first ___" CTA.

### §3.1 · Section anatomy (proposed)

```
┌─────────────────────────────────────────────┐
│ §1 · YOUR BLOCK  (tab active)               │
├─────────────────────────────────────────────┤
│ ╔═══════════════════════════════════════╗   │
│ ║  Lake Nona East                        ║   │ ← existing lane-status card
│ ║  7 of 20 moms · 2 of 5 anchors         ║   │
│ ║                       [Sparked pill]    ║   │
│ ╚═══════════════════════════════════════╝   │
│                                             │
│ Lana voice intro                            │
│ "Your block is where it all starts."        │ ← existing
│                                             │
│ ───────── NEW BLOCK-LOG SECTION ─────────   │
│                                             │
│ ┌─ POTENTIAL MATCHES (3) ────────────────┐  │ ← NEW
│ │ ↔  3T rain boots                        │  │
│ │     2 moms in your block have a fit     │  │
│ │     [hidden until you nudge or verify]  │  │
│ │     Sent today · 1 viewed · 1 pending   │  │
│ │     →                                    │  │
│ ├─────────────────────────────────────────┤  │
│ │ ☕  Brazilian moms coffee (you hosting)  │  │
│ │     5 moms in your block fit · 3 yes     │  │
│ │     Notified · 3 yes · 2 not seen        │  │
│ │     →                                    │  │
│ ├─────────────────────────────────────────┤  │
│ │ ★  Dr. Sarah pediatric dentist (tip)    │  │
│ │     1 mom asked about pediatric dental  │  │
│ │     Tip shared with them · seen today    │  │
│ │     →                                    │  │
│ └─────────────────────────────────────────┘  │
│                                             │
│ [existing "Take your first" CTA at bottom]  │
└─────────────────────────────────────────────┘
```

### §3.2 · Empty state

If mom has no active signals → no match candidates → show:

> Lana voice: *"When you ask for or share something, this is where the matches will land. Tap the bell to start."*

Same "Take your first" CTA pattern from v0.2.5.

### §3.3 · How it complements the existing FELLOWS tab

- **FELLOWS tab** (§2) — moms in your block ranked by **identity affinity** (who you are vs who they are). Static-ish · ranked by who-you-might-want-to-know.
- **BLOCK tab** (§1) — match candidates ranked by **active-signal fit** (what you're doing right now vs what they're doing). Dynamic · ranked by who-might-help-you-now.

Both surfaces use the same redacted-preview-until-verified privacy model. FELLOWS is the directory; BLOCK is the activity log.

---

## §4 · Match engine (backend · Asjid)

### §4.1 · When matches are computed

```
EVENT: new local_signal created (any user, any intent)
  ↓
RUN matcher:
  1. Pull all other active local_signals in same block_id
     (or proximity radius if block has <20 moms)
  2. Filter to opposing intents:
       swap_seek ↔ swap_offer
       meet_seek ↔ host_meet
       tip_seek  ↔ tip_share
  3. Score each candidate:
       - embedding cosine similarity (0-1)
       - affinity_tag overlap (0-1)
       - block proximity (0-1, decay by H3 ring)
       - life-stage match (1.0 if same, 0.5 if adjacent, 0.0 if far)
     match_strength = weighted average
  4. Keep matches with match_strength >= 0.65
  5. For each kept match:
       - Insert into block_log entries (TWO rows: one for each side)
       - Mark notification_sent=false initially
       - If match_strength >= 0.80 AND peer.notification_prefs allow,
         fire push/SMS notification + set notification_sent=true
```

### §4.2 · DB schema (proposed)

```sql
CREATE TABLE block_log_entries (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  for_user_id UUID REFERENCES users(id) NOT NULL,
  match_type TEXT NOT NULL CHECK (match_type IN (
    'inbound_for_my_seek','inbound_for_my_offer',
    'meet_invite_potential','meet_attendee_potential',
    'fellow_overlap_high','tip_match'
  )),
  my_signal_id UUID REFERENCES local_signals(id),
  peer_signal_id UUID REFERENCES local_signals(id),
  peer_user_id UUID REFERENCES users(id),
  block_id UUID REFERENCES blocks(id) NOT NULL,
  match_strength REAL NOT NULL CHECK (match_strength >= 0 AND match_strength <= 1),
  match_reasons TEXT[],
  created_at TIMESTAMPTZ DEFAULT NOW(),
  expires_at TIMESTAMPTZ NOT NULL,
  user_acted_at TIMESTAMPTZ,
  action_taken TEXT CHECK (action_taken IN ('nudged','dismissed','saved','ignored')),
  notification_sent_to_peer BOOLEAN DEFAULT FALSE,
  notification_sent_at TIMESTAMPTZ
);

CREATE INDEX idx_block_log_user_active ON block_log_entries(for_user_id, created_at DESC)
  WHERE action_taken IS NULL AND expires_at > NOW();
CREATE INDEX idx_block_log_block_recent ON block_log_entries(block_id, created_at DESC);
```

### §4.3 · API endpoint (proposed)

```http
GET /lana/users/{user_id}/block-log
Authorization: Bearer <access_token>

→ 200 OK
{
  "block_id": "uuid",
  "block_name": "Lake Nona East",
  "entries": [
    {
      "id": "uuid",
      "match_type": "inbound_for_my_seek",
      "peer_preview_label": "Mom · 2 blocks · pre-K · Brazilian",
      "match_strength": 0.83,
      "match_reasons": ["3T rain boots offer matches your seek", "same pre-K stage"],
      "created_at": "2026-06-12T14:30:00Z",
      "notification_sent_to_peer": true,
      "expires_at": "2026-06-26T14:30:00Z"
    }
  ]
}
```

```http
POST /lana/block-log/{entry_id}/action
Authorization: Bearer <access_token>
{ "action": "nudged" | "dismissed" | "saved" | "ignored" }

→ 200 OK
```

---

## §5 · Frontend impact (Abdullah)

### §5.1 · v0.2.8 HTML mockup work
Add a `block-log` section to the BLOCK tab in C-13 (and C-13-EXPANDED). Use the existing `.contribution-row` component pattern · just with the new icon/label/meta combinations. No new CSS class needed.

### §5.2 · API integration (when backend ships)
- Fetch `GET /lana/users/{me}/block-log` when RADAR opens AND BLOCK tab is active (or pre-fetch on RADAR open)
- Render entries in chronological order (newest first)
- Wire tap → open a detail sheet that lets mom nudge / dismiss / save / ignore
- Refresh on `pull-to-refresh` or after taking an action

### §5.3 · Privacy gates
- Don't show `peer_user_id` until mom has phone-verified OR mom has sent a nudge that peer accepted
- Use `peer_preview_label` until then
- Same redaction model as `peer_matches`

---

## §6 · Notification channels · priority + delivery (v0.2.9 founder add)

The match notification (Channel 1) ships across **three delivery channels** with explicit priority order. Founder verbatim: *"Notifications first via push notification · via email?"* — answer: push primary · SMS fallback · email tertiary.

### §6.0 · Channel priority

| Order | Channel | When to use | Latency target | Cost-per-msg |
|---|---|---|---|---|
| 1 | **Push notification** (iOS APNS · Android FCM · Web Push for PWA on Android) | **Primary** · instant · in-the-moment match alerts · Lana check-ins · proactive nudges | <5s | $0 |
| 2 | **SMS via Twilio** | **Fallback** when push isn't enabled OR when user is signed out · we already have their phone from signup gate | <30s | ~$0.01 |
| 3 | **Email** | **Tertiary** · weekly digest only · founding-mom invite · safety escalation · NOT for match alerts (too slow · email is async by design) | hours | $0 (via Postmark/Resend free tier) |

### §6.0.1 · Per-channel content rules

**Push:** ≤80 chars · 1 CTA · deep-link to Lana chat with context payload. Title = "Lana" · body = the message. Action button = "Open" (deep-links to relevant frame).

**SMS:** ≤140 chars (sub-MMS · stays one message) · always signed `— Lana` · phone-call CTA optional · short URL via `lana.help/m/{matchId}` redirects to the in-app surface.

**Email:** structured digest format ONLY (weekly recap of block activity · NOT individual match alerts) · brand header · unsubscribe link in footer · weekly cadence default · Lana voice in copy.

### §6.0.2 · Channel selection logic (backend Asjid)

```
On match.created event:
  1. Check mom's notification_prefs:
     - if push_enabled AND device_token valid → send PUSH
     - else if phone_verified AND sms_enabled → send SMS
     - else queue for next weekly email digest
  2. Track delivery in match_notifications table:
     - {match_id, channel, sent_at, delivered_at, opened_at, channel_used}
  3. If push fails (no device or token expired) within 60s → fallback to SMS
  4. If SMS fails (Twilio error) within 30s → queue for digest only
```

### §6.0.3 · Frequency caps (defensive against spam)

- Max **3 match notifications per day** per mom (push + SMS combined)
- Max **1 proactive Lana check-in per day** (the "by the way" outreach)
- Hard quiet hours: 9pm-7am local TZ (use mom's ZIP to derive TZ)
- "Mute today" CTA in every notification → suppresses for 24h

---

## §6.1 · Notification copy (Channel 1 · for Aki + copywriter)

When the matcher fires a notification to the peer:

| Match type | SMS / push copy |
|---|---|
| `inbound_for_my_seek` (peer has what mom seeks) | "Lana · a mom in your block is looking for 3T rain boots · want me to introduce you?" |
| `inbound_for_my_offer` (peer offers what mom needs) | "Lana · a mom in your block has 3T rain boots her kid outgrew · want me to send her your way?" |
| `meet_invite_potential` | "Lana · a Brazilian moms coffee is happening Saturday at Foxtail · 3 yes already · want in?" |
| `tip_match` | "Lana · Sara just shared a pediatric dentist rec that fits what you were asking about · want me to send it?" |

All notifications: ≤140 chars · sender = "Lana" · one CTA · never multi-step.

---

## §7 · Open questions

1. **Asjid:** is `block_log_entries` a new table OR a materialized view on top of `local_signals`? Recommend table (separate concerns · matcher writes once, view reads often).
2. **Asjid:** notification dispatch → which service? (Twilio for SMS, Expo/APNS for push)
3. **Asjid:** how often does the matcher run? Real-time on insert OR batch every N min? Recommend trigger-based on `local_signals` insert.
4. **Abdullah:** do we expose a polling or websocket update for the Block-Log when mom has RADAR open? Recommend polling (every 30s when RADAR visible) for v0.2 simplicity.
5. **Founder:** do we want to surface match reasons to mom A (e.g., "matches your 3T boots seek + same pre-K stage") OR keep it opaque? Recommend transparent · reasons build trust.

---

*v1 spec · 2026-06-12 · this becomes a new section of the master Lana TPR alongside the Intent Catalog.*
