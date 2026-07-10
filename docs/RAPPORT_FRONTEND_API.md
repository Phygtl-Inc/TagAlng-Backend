# Rapport (Ring C) — Frontend Integration Guide

**Audience:** frontend team · **Backend owner:** lana-worker · **Status:** shipped (v1)

This is the "**By the way…**" home-screen tile — the one that asks a mom one well-timed
follow-up question (*"about your morning run… — Do you usually run solo, or with other
moms?"*). Her answer becomes an identity claim that feeds matching + recommendations.

This doc is the **contract** so any surface (home tile, a settings screen, native app) can integrate.

---

## 1. The big idea — when things happen

Rapport is a **background loop**, not part of the chat response:

```
User chats normally  ──►  backend extracts identity claims (background)
                          └─► opens follow-up "gaps" (background)
                                        │
Home screen renders  ──►  YOU call  POST /lana/rapport/next-ask
                          └─► backend returns the single best gap to ask (or null)
                                        │
User answers/dismisses ─► YOU call  record-answer / record-skip / mute-fact
```

**Key rules the FE must internalize:**
- The ask **never comes back inside a chat turn.** You fetch it separately, on the home screen.
- `next-ask` returns **at most one** ask, or `null`. It is **not** shown every visit — the backend enforces "at most one *new* ask per 24h", relationship-tier gating, and per-topic mutes.
- **`null` is normal** → render nothing.
- All calls are **POST** (even the "get" one) — the PWA service worker breaks cross-origin GETs to the worker.

---

## 2. Auth & base URL

- Base URL: `NEXT_PUBLIC_LANA_WORKER_URL` (same worker as chat).
- Every request needs the Supabase user JWT: `Authorization: Bearer <access_token>`.
- **`user_id` is derived from the token server-side** — never send it in the body.
- Read the token fresh per call (same as the existing `lanaFetch` helper).

---

## 3. Endpoints

| Method | Path | Purpose | When to call |
|---|---|---|---|
| POST | `/lana/rapport/next-ask` | Get the ask to show (or null) | On home-screen render |
| POST | `/lana/rapport/record-answer` | Submit her answer | She types an answer + taps Share |
| POST | `/lana/rapport/record-skip` | Dismiss this ask | She taps "Not now" / the ✕ |
| POST | `/lana/rapport/mute-fact` | Never ask this topic again | She taps "Don't ask this" |

---

### 3.1 `POST /lana/rapport/next-ask`

**Request body** (all optional):
```json
{ "surface": "homescreen", "cycle": false }
```
- `surface` — free-form analytics label (default `"homescreen"`).
- `cycle` — set `true` when the user taps the tile's **refresh (⟳)** to get a *different* question. This retires the current ask and returns the next-best one **immediately, bypassing the 24h cap**. Default `false`.

**Response:**
```json
{
  "ask": {
    "gap_row_id": "b3f1…-uuid",
    "gap_id": "activity_social_pref",
    "parent_bucket": "activity",
    "why_frame": "about your morning run…",
    "question": "Do you usually do that solo, or with other moms?",
    "sensitivity_tier": "LOW",
    "chip_color_token": "--d-activity"
  }
}
```
…or, when there's nothing to ask:
```json
{ "ask": null }
```

**Field reference:**

| Field | Type | Use it for |
|---|---|---|
| `gap_row_id` | string (uuid) | **Pass back** to `record-answer` / `record-skip`. Opaque. |
| `gap_id` | string | **Pass back** to `mute-fact`. Also useful as a stable key. |
| `parent_bucket` | string | Pillar: `heritage`\|`stage`\|`vicinity`\|`faith`\|`activity`\|`interest`\|`general`. |
| `why_frame` | string | The **teaser** headline, e.g. *"about your morning run…"*. Lowercase, ends with `…`. |
| `question` | string | The **actual question** to show under the teaser, e.g. *"How old are your little ones?"*. |
| `sensitivity_tier` | `"LOW"`\|`"MED"`\|`"HIGH"` | Optional styling hint; backend already gates HIGH ones. |
| `chip_color_token` | string | CSS var for the pillar chip color, e.g. `--d-activity`. Map to your palette. |

**Behavior you can rely on:**
- **Idempotent while pending.** If a mom has been shown an ask but hasn't answered/skipped it yet, `next-ask` keeps returning the **same** ask. So it's safe to call on every render / remount — it won't "burn" the ask or flip to `null`. (Still, guard React Strict Mode double-fetch with a ref so you don't fire duplicate calls.)
- Once she answers or skips, the 24h cap applies → subsequent calls return `null` until tomorrow.
- **Refresh (⟳) → `{ "cycle": true }`.** Call next-ask again with `cycle: true` to swap the current question for a different one on demand. It skips the current ask and returns the next-best gap right away (cap bypassed). If there's genuinely only one gap left, it may return the same one; if nothing's left, `null`.

---

### 3.2 `POST /lana/rapport/record-answer`

Call when she submits a free-text answer.

**Request body:**
```json
{
  "gap_row_id": "b3f1…-uuid",
  "text": "usually with my neighbor Dana",
  "session_id": "optional-current-session",
  "message_id": "optional"
}
```
- `gap_row_id` (required) — from the ask.
- `text` (required) — her answer. The backend extracts it into an identity claim and closes the gap.
- `session_id` / `message_id` — optional, for provenance.

**Response:** `{ "ok": true }`

---

### 3.3 `POST /lana/rapport/record-skip`

Call when she dismisses ("Not now" / ✕).

**Request body:** `{ "gap_row_id": "b3f1…-uuid" }`
**Response:** `{ "ok": true }`

Behavior: the gap reopens for later, but **expires after 3 skips** (backend-managed). It won't re-show today (24h cap).

---

### 3.4 `POST /lana/rapport/mute-fact`

Call when she opts out of a topic ("Don't ask this").

**Request body:** `{ "gap_id": "activity_social_pref" }`  ← note: `gap_id`, not `gap_row_id`
**Response:** `{ "ok": true }`

Behavior: that topic is **never asked again**.

---

## 4. Worked example (TypeScript)

Example client code you can drop in — a small POST wrapper (`lanaFetch`) plus typed calls.

```ts
// example client wrapper (POST helper + typed calls)
export interface RapportAsk {
  gap_row_id: string;
  gap_id: string;
  parent_bucket: string;
  why_frame: string;
  question: string;
  sensitivity_tier: 'LOW' | 'MED' | 'HIGH';
  chip_color_token: string;
}

export async function nextRapportAsk(surface = 'homescreen'): Promise<RapportAsk | null> {
  try {
    const { ask } = await lanaFetch<{ ask: RapportAsk | null }>(
      '/lana/rapport/next-ask', { surface });
    return ask ?? null;
  } catch {
    return null; // best-effort: on any error, just don't show the tile
  }
}

export const recordRapportAnswer = (gapRowId: string, text: string) =>
  lanaFetch('/lana/rapport/record-answer', { gap_row_id: gapRowId, text });
export const skipRapportGap = (gapRowId: string) =>
  lanaFetch('/lana/rapport/record-skip', { gap_row_id: gapRowId });
export const muteRapportGap = (gapId: string) =>
  lanaFetch('/lana/rapport/mute-fact', { gap_id: gapId });
```

```tsx
// Home tile — fetch once on mount, render nothing if null.
function RapportAskTile() {
  const [ask, setAsk] = useState<RapportAsk | null>(null);
  const fetched = useRef(false);

  useEffect(() => {
    if (fetched.current) return;   // guard Strict Mode double-invoke
    fetched.current = true;
    nextRapportAsk().then(setAsk);
  }, []);

  if (!ask) return null;           // null → show nothing

  return (
    <Card>
      <Eyebrow>BY THE WAY</Eyebrow>
      <Teaser>{ask.why_frame}</Teaser>              {/* "about your morning run…" */}
      <Question>{ask.question}</Question>            {/* the real question */}
      <Input onSubmit={(text) => recordRapportAnswer(ask.gap_row_id, text).then(() => setAsk(null))} />
      <button onClick={() => { skipRapportGap(ask.gap_row_id); setAsk(null); }}>Not now</button>
      <button onClick={() => { muteRapportGap(ask.gap_id); setAsk(null); }}>Don’t ask this</button>
    </Card>
  );
}
```

---

## 5. Worked example (curl)

```bash
TOKEN="<supabase access_token>"
BASE="$NEXT_PUBLIC_LANA_WORKER_URL"   # e.g. http://127.0.0.1:8081

# 1) Get the ask
curl -s -X POST "$BASE/lana/rapport/next-ask" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"surface":"homescreen"}'
# → {"ask": {"gap_row_id":"…","why_frame":"about your morning run…","question":"…", …}}  or  {"ask":null}

# 2) Answer it
curl -s -X POST "$BASE/lana/rapport/record-answer" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"gap_row_id":"<gap_row_id>","text":"usually with a neighbor"}'

# 3) Skip / 4) Mute
curl -s -X POST "$BASE/lana/rapport/record-skip" -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"gap_row_id":"<gap_row_id>"}'
curl -s -X POST "$BASE/lana/rapport/mute-fact" -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"gap_id":"<gap_id>"}'
```

---

## 6. Rendering guidance

- **Two lines:** `why_frame` is the teaser (her words, e.g. *"about your morning run…"*); `question` is the sentence she answers. Show both — teaser as eyebrow/headline, question above the input.
- **Chip color:** map `chip_color_token` (e.g. `--d-activity`) to your palette. Buckets: identity/heritage, stage, vicinity, faith, activity, interest, general.
- **Always offer an out:** a "Not now" (skip) and a "Don't ask this" (mute). Never make the ask blocking.
- **Never render in the chat thread.** It belongs on the idle/home surface, between tasks.

---

## 7. Gotchas / FAQ

- **"It returns `null` — is it broken?"** No. `null` means: no open gap, or the 24h cap is in effect, or everything's muted/tier-gated. Render nothing.
- **"Can I call it on every render?"** Yes — it's idempotent while an ask is pending. Still add a fetch guard to avoid duplicate network calls (React Strict Mode fires effects twice in dev).
- **"Do I need to poll?"** No. Fetch once when the home surface mounts.
- **"How often will a user see a tile?"** At most one *new* ask per 24h. Sensitive topics (`HIGH`) only appear once she's warmed into the community; the backend handles that — you don't gate it.
- **"What if the worker/network fails?"** Treat as `null` and hide the tile — swallow the error in your `next-ask` wrapper and return `null`.
```
