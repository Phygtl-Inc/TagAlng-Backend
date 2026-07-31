# TPR v1.1 — Session Replay Implementation

**Product:** Lana · Phygtl, Inc.
**Platform:** Amplitude
**Owner:** Data & AI
**Date:** July 2026
**Status:** Active — implementation pending (validation errors detected)

---

> **Purpose:** Step-by-step implementation guide for enabling Amplitude Session Replay across Frontend, Backend, and Analytics teams. Updated in v1.1 to include the two validation errors detected in Amplitude's Ingestion Monitor and their fixes.

---

## 📺 Feature Overview — Video Demo

Watch the video below for an introduction to Session Replay and a live demo of the feature:

[![Session Replay — Feature Overview & Demo](https://img.youtube.com/vi/92P8Yvn4jOg/maxresdefault.jpg)](https://www.youtube.com/watch?v=92P8Yvn4jOg)

---

## Current Status — Amplitude Validation Errors

Two errors were detected in Amplitude's **Session Replay Ingestion Monitor** that must be resolved before Session Replay is functional.

| # | Error | Location | Owner | Status |
|---|---|---|---|---|
| E1 | No Session Replay data received for this project | Session Replay Ingestion Validation | Frontend | 🔴 Blocking |
| E2 | Device ID mismatch detected in Session Replay plugin configuration | Session Replay Data Connection Validation | Frontend | 🔴 Blocking |

---

### E1 — No Session Replay data received

**Cause:** The Session Replay plugin has not been installed and connected to the existing Amplitude Browser SDK, or the API key used does not match the project.

**Fix:**

The Session Replay plugin must be installed separately from the analytics SDK. Two options depending on the current setup:

**Option A — Already using Amplitude Browser SDK 2:**

```bash
npm install @amplitude/plugin-session-replay-browser --save
```

```js
import * as amplitude from "@amplitude/analytics-browser";
import { sessionReplayPlugin } from "@amplitude/plugin-session-replay-browser";

// Initialize Session Replay BEFORE amplitude.init()
const sessionReplayTracking = sessionReplayPlugin({
  sampleRate: 1,              // 100% of sessions — adjust for production
  forceSessionTracking: true, // Ensures Session Start and Session End events fire
});
amplitude.add(sessionReplayTracking);

// Your existing initialization
amplitude.init(API_KEY, USER, {
  autocapture: {
    sessions: true, // Required — ensures session events are tracked
  },
});
```

**Option B — Using the Unified SDK (simpler, recommended if not yet installed):**

```bash
npm install @amplitude/unified
```

```ts
import { initAll } from "@amplitude/unified";

initAll("YOUR_AMPLITUDE_API_KEY", {
  sessionReplay: {
    sampleRate: 1,
  }
});
```

> **API Key:** Confirm the key used in the Session Replay SDK matches the project under **Settings > Projects > [Your Project] > API Key** in Amplitude. A mismatch silently prevents data from being sent to the correct project.

**Validation:** After deploying, open DevTools > Network tab and look for requests to `https://api-sr.amplitude.com/sessions/v2/track`. If requests appear, data is flowing.

---

### E2 — Device ID mismatch

**Cause:** The analytics SDK and the Session Replay plugin are using different device IDs. This typically happens when `deviceId` is overridden in the Session Replay plugin configuration, causing it to differ from the device ID used in analytics events. Amplitude requires both to match for session correlation to work.

**Fix:**

Do **not** set `deviceId` manually in the Session Replay plugin. Both the analytics SDK and the Session Replay plugin must use the same device ID — the one generated automatically by the Browser SDK.

```js
// ✅ CORRECT — let the SDK manage device ID automatically
const sessionReplayTracking = sessionReplayPlugin({
  sampleRate: 1,
});
amplitude.add(sessionReplayTracking);
amplitude.init(API_KEY);

// ❌ WRONG — do not override deviceId in the plugin
const sessionReplayTracking = sessionReplayPlugin({
  sampleRate: 1,
  deviceId: "custom-device-id", // This causes the mismatch
});
```

**If using multiple Amplitude instances**, attach Session Replay to the same instance used for analytics:

```html
<script>
  const sessionReplayTracking = window.sessionReplay.plugin();
  const instance = window.amplitude.createInstance();
  instance.add(sessionReplayTracking);
  instance.init(API_KEY);
</script>
```

**Validation check:** In Amplitude > User Lookup, find a recent user and look for an event with the `[Amplitude] Session Replay ID` property. If present, device IDs are matching correctly.

---

## Overview

| Team | Task | Key Action |
|---|---|---|
| Frontend | Install Session Replay plugin + fix device ID alignment | `npm install @amplitude/plugin-session-replay-browser` |
| Backend | Read `session_id` from request header; include in every HTTP API v2 event | Pass `session_id` in all Amplitude event POSTs |
| Analytics / Data & AI | Configure masking, privacy settings, and sample rate | Settings > Organizational Settings > Session Replay Settings |

> **Critical:** The `session_id` is the link between frontend session recordings and backend events. The exact same value must flow from the browser SDK to every server-side event — never generate a new one on the backend.

---

## 1. Frontend Team — Step-by-Step

### Step 1 — Install the Session Replay Plugin

Use the **Browser SDK Plugin** if Amplitude Browser SDK 2 is already installed (most likely for Lana). Use the Unified SDK only if starting from scratch.

#### Browser SDK Plugin (recommended for existing setup)

```bash
npm install @amplitude/plugin-session-replay-browser --save
```

```js
import * as amplitude from "@amplitude/analytics-browser";
import { sessionReplayPlugin } from "@amplitude/plugin-session-replay-browser";

const sessionReplayTracking = sessionReplayPlugin({
  sampleRate: 1,              // 100% for early-stage, adjust as user volume grows
  forceSessionTracking: true, // Captures Session Start / Session End events
});

amplitude.add(sessionReplayTracking);

amplitude.init(API_KEY, USER, {
  autocapture: {
    sessions: true,
  },
});
```

> **Important:** `forceSessionTracking: true` is required from plugin version 1.12.1 onwards. Without it, `[Amplitude] Start Session` and `[Amplitude] End Session` events are not captured, and replays may not appear in the UI.

#### Unified SDK (alternative, for fresh installs)

```bash
npm install @amplitude/unified
```

```ts
import { initAll } from "@amplitude/unified";

initAll("YOUR_AMPLITUDE_API_KEY", {
  sessionReplay: {
    sampleRate: 1,
  }
});
```

#### CDN option (non-bundled / legacy setup)

```html
<script src="https://cdn.amplitude.com/libs/analytics-browser-2.45.4-min.js.gz"></script>
<script src="https://cdn.amplitude.com/libs/plugin-session-replay-browser-1.33.6-min.js.gz"></script>
<script>
  const sessionReplayTracking = window.sessionReplay.plugin({
    sampleRate: 1,
    forceSessionTracking: true,
  });
  window.amplitude.add(sessionReplayTracking);
  window.amplitude.init(API_KEY);
</script>
```

---

### Step 2 — Expose the Session ID to the Backend

The frontend must share the active `session_id` with the backend so server-side events can be correlated to the replay.

```ts
import * as amplitude from "@amplitude/analytics-browser";

const sessionId = amplitude.getSessionId();
// Example value: 1699922971244 (Unix timestamp in milliseconds)
```

Pass via request header on every API call:

```
X-Amplitude-Session-Id: 1699922971244
```

---

### Step 3 — Privacy and Masking

Session Replay masks all `<input>` fields by default. Use CSS classes to control masking:

| Class | Effect |
|---|---|
| `.amp-mask` | Masks text in any element (shown as asterisks) |
| `.amp-unmask` | Unmasks a previously masked input field |
| `.amp-block` | Blocks the entire element (replaced by a placeholder) |

For advanced control, configure `privacyConfig` in the plugin:

```js
const sessionReplayTracking = sessionReplayPlugin({
  sampleRate: 1,
  privacyConfig: {
    defaultMaskLevel: 'medium',
    maskSelector: ['.sensitive-data', '.user-email'],
    unmaskSelector: ['.public-info'],
    blockSelector: ['.no-track', '#ads'],
  },
});
```

---

### Step 4 — Validate

1. Open DevTools > Network tab
2. Look for requests to `https://api-sr.amplitude.com/sessions/v2/track`
3. Go to Amplitude > User Lookup > find a recent user
4. Confirm the `[Amplitude] Session Replay ID` property is present on at least one event
5. The **Play Session** button should appear on the user's profile

**Local development tip:** If replays don't appear, enable debug mode:

```js
const sessionReplayTracking = sessionReplayPlugin({
  debugMode: true,
  sampleRate: 1,
});
```

---

### Known limitations (Frontend)

- Session Replay does **not** capture: Canvas, WebGL, Lottie animations, cross-origin iframes without additional config, assets behind authentication.
- Not compatible with ad blocking software.
- Session Replay captures only the page in focus — useful to know during local development.

---

## 2. Backend Team — Step-by-Step

### Step 1 — Read the Session ID from the Request

```js
// Node.js/Express example
const sessionId = req.headers["x-amplitude-session-id"];
```

---

### Step 2 — Include `session_id` in Every Amplitude Event

**API Endpoint:** `https://api2.amplitude.com/2/httpapi`

```json
POST https://api2.amplitude.com/2/httpapi
Content-Type: application/json

{
  "api_key": "YOUR_AMPLITUDE_API_KEY",
  "events": [
    {
      "event_type": "lana_turn",
      "user_id": "user-123",
      "session_id": 1699922971244,
      "event_properties": {
        "source": "backend",
        "turn_index": 3
      }
    }
  ]
}
```

> **Critical:** Use the **exact same** `session_id` value from the frontend SDK (13-digit Unix timestamp in milliseconds). Do not generate a new one on the backend. This is the root cause of the Device ID / Session ID mismatch errors.

---

## 3. Analytics / Data & AI — Step-by-Step

1. Go to **Settings** (gear icon, bottom-left in Amplitude)
2. Navigate to **Organizational Settings > Session Replay Settings**
3. Select the relevant **Project**
4. Configure:
   - **Masking Level** — Light, Medium, or Conservative. Start with Medium for production.
   - **Masking Overrides** — whitelist elements that are safe to show
   - **Sample Rate** — set to 100% (`1`) for early-stage testing; reduce as user volume grows
5. Monitor ingestion at **Session Replay > Ingestion Monitor**

> **Privacy first:** configure masking before enabling replay company-wide. All sensitive inputs (passwords, PII fields) are masked by default.

---

## 4. Configuration Reference

| Parameter | Type | Default | Description |
|---|---|---|---|
| `sampleRate` | number | `0` | Fraction of sessions to record (0–1). Set to `1` for 100%. |
| `forceSessionTracking` | boolean | `false` | Forces capture of Session Start/End events. **Required from v1.12.1+.** |
| `privacyConfig` | object | `undefined` | Advanced masking with CSS selectors. |
| `debugMode` | boolean | `false` | Enables extra debug info. Use only for troubleshooting, not in production. |
| `useWebWorker` | boolean | `false` | Moves compression off the main thread — improves performance. |
| `storeType` | string | `idb` | Storage: `idb` (IndexedDB, persistent) or `memory` (lost on page close). |
| `serverZone` | string | `US` | Set to `EU` for EU data residency. |

---

## 5. Why Session Replay Matters

Session Replay provides qualitative context that quantitative metrics alone cannot. With it, the team can:

- Watch exactly what a user did during a session — clicks, scrolls, navigation path
- Identify friction points where users hesitate or abandon flows (directly relevant to the UX issues observed in early user interviews — e.g. scroll issues, blocked flows)
- Correlate Lana conversation turns with frontend actions in a single timeline
- Catch UI bugs without relying on user-submitted bug reports
- Validate or invalidate product hypotheses without running additional research sessions

> With the current user base size, a **100% sample rate** (`sampleRate: 1`) is recommended. Adjust as volume grows and quota management becomes necessary.

---

## 6. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| No replays appearing in Amplitude | `sampleRate` is 0 (default) | Set `sampleRate: 1` in plugin config |
| Replays appear but no `[Amplitude] Session Replay ID` on events | Session events not tracked | Add `forceSessionTracking: true` and `autocapture: { sessions: true }` |
| Device ID mismatch error | `deviceId` overridden in plugin config, or multiple SDK instances | Remove manual `deviceId` from plugin; attach plugin to the same instance as analytics |
| CSS styling broken in replay | External stylesheets not accessible to Amplitude | Add `crossorigin="anonymous"` to `<link rel="stylesheet">` elements |
| Replay length exceeds session length | `[Amplitude] End Session` fires late (tab closed and re-opened) | Expected behavior — check `End Session Client Event Time` vs `Client Upload Time` |
| Content Security Policy error in console | CSP blocking Amplitude domains | Add required CSP directives (see below) |

### Required CSP directives

```
script-src: https://cdn.amplitude.com;
connect-src: https://api-secure.amplitude.com https://api-sr.amplitude.com;
worker-src: blob:;
```

---

*Lana · Phygtl, Inc. · Internal document · Not for external distribution · v1.1 · July 2026*
