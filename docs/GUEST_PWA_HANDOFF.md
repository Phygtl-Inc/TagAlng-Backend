# Guest onboarding — PWA handoff (screens → APIs)

**Live demo:** `apps/admin` → `/lana/meet` (run `npm run dev` in `apps/admin`)

**Backend reference:** [`GUEST_ONBOARDING_FLOW.md`](./GUEST_ONBOARDING_FLOW.md) · Postman: `TagAlng-Guest-Onboarding-Full.postman_collection.json`

---

## Screen map (matches design mockups)

| # | Screen | User action | API |
|---|--------|-------------|-----|
| 1 | **Meet Lana** landing | Tap "Meet Lana" | `supabase.auth.signInAnonymously()` |
| 2 | **Who are you** chat | Type life stage + heritage | `POST /lana/sessions` then `POST …/messages` |
| 3 | **Joint moment** purple card | Tap "Yes · introduce us" | `POST …/messages` with `"Yes"` (backend already called `get_joint_moment_candidate`) |
| 4 | **Intro name** (chat) | Type name e.g. "Linda" | `POST …/messages` |
| 5 | **Phone verify** | Enter number + OTP | `updateUser({ phone })` → `verifyOtp({ type: 'phone_change' })` |
| 6 | **Post-verify chat** | Kids, interests | `POST …/messages` until `ready_to_complete` |
| 7 | **Complete** | Tap "That's me" | `POST …/complete` → `assign_home_block` → `send_joint_moment_intro` |

---

## What to read from each Lana message response

```typescript
type LanaTurn = {
  assistant_message: string;
  onboarding_step?: string;
  requires_phone_verification?: boolean;
  joint_moment?: {
    joint_moment_id: string;
    candidate: { nickname: string; user_id: string };
    lana_copy: string;
    is_demo: boolean;
  };
  phone_verified?: boolean;
  ready_to_complete?: boolean;
};
```

| `onboarding_step` | UI |
|-------------------|-----|
| `early_chat` | Normal chat composer |
| `offered_intro` | Show **Joint moment card** + Maria peer chip; hide composer or show "tap above" |
| `awaiting_intro_name` | Chat composer — placeholder "What should Maria call you?" |
| `await_phone` | Navigate to **phone screen** (`requires_phone_verification: true`) |
| `post_verify` | Resume chat — kids / interests |

---

## Phone OTP (critical)

Anonymous guest must **keep the same `user_id`** as the Lana session.

```typescript
// 1. Link phone (sends OTP) — user must still be signed in as anonymous
await supabase.auth.updateUser({ phone: '+15550999012' });

// 2. Verify — MUST be phone_change, NOT sms
await supabase.auth.verifyOtp({
  phone: '+15550999012',
  token: '000000',
  type: 'phone_change',
});
```

Using `type: 'sms'` signs into a **different** user → Lana returns `session_not_found`.

**Dashboard:** Auth → Phone → add test number + OTP `000000`.

---

## Env vars (`apps/admin/.env.local`)

```
NEXT_PUBLIC_SUPABASE_URL=https://rjlcyvwogmfmngemhbmn.supabase.co
NEXT_PUBLIC_SUPABASE_ANON_KEY=<anon key>
NEXT_PUBLIC_LANA_WORKER_URL=https://tagalng-lana-worker-s5gmxb6whq-ue.a.run.app
```

**Dashboard:** enable Anonymous sign-ins + Manual linking.

---

## Run the demo

```bash
cd apps/admin
cp .env.example .env.local   # fill anon key
npm install
npm run dev
```

Open **http://localhost:3000/lana/meet**

**Happy path:** Meet Lana → "I'm a Latino mom in Lake Nona…" → Joint moment → Yes → Linda → phone + 000000 → kids/interests → That's me.
