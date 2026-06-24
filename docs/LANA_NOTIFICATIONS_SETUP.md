# Lana — Push + Email Notifications · Setup

Web Push (PWA) + transactional email (Resend) for event activity. Everything **no-ops
until the env keys are set**, so nothing breaks before configuration.

## What fires today

| Event | Who | Push + Email |
|---|---|---|
| A meet is published | **host** | "Your meet is live 🎉" |
| Someone joins (request) | **host** | "X wants to join" (approve-each) · "X joined" (anyone-can-join) |
| Someone joins | **joiner** | "Request sent — host will confirm" · "You're in 🎉" (auto-approved) |
| Host approves a request | **joiner** | "You're in 🎉" |
| Host declines | **joiner** | push only ("Update on …") |

> New-meet broadcast to *nearby neighbors* is intentionally **not** built (you chose
> host-only). It'd need audience targeting + a per-user opt-in.

## Architecture (no new infra)

- **Push:** FE subscribes via the service worker (`sw.ts` has the `push` / `notificationclick`
  handlers) → stores the subscription (`push_subscriptions` table, `upsert_push_subscription`
  RPC). The worker sends with `pywebpush` + VAPID (`app/notifications.py`).
- **Email:** the worker calls Resend's HTTP API.
- **Triggers:** meet-publish notifies the host **inline** in the worker. Join/approve go
  FE→Supabase RPC directly (not through the worker), so the FE pings two worker hooks
  **after** the RPC succeeds: `POST /hooks/event-join` and `POST /hooks/event-decision`.
  The worker re-reads the rows server-side (service role) and sends.
  - *Limitation:* the **actor** (joiner / approving host) must be online to fire the ping —
    fine, since they just performed the action. The **recipient** can be offline (push/email
    deliver async).

## Required env

**lana-worker** (`deploy/lana-worker.env`, then redeploy — already in the deploy allowlist):
```
VAPID_PUBLIC_KEY=BDjxyd8leOwaRblApTo8dTSMetXR9_BQCpElln0Q1NalNyp13G8W6mCsc3ss88lp420Jf0mQyYx8iWdEqSvQG9c
VAPID_PRIVATE_KEY=6HfwEBXP--keQBDHJI6etEvx1xlZabi7OolIgnHi6Pc
VAPID_SUBJECT=mailto:hello@yourdomain.com
RESEND_API_KEY=re_xxxxxxxx          # from resend.com
RESEND_FROM=Lana <hi@yourdomain.com> # a verified Resend sender/domain
APP_BASE_URL=https://your-pwa-origin  # for absolute links in emails
```

**PWA** (`.env.local` / Vercel):
```
NEXT_PUBLIC_VAPID_PUBLIC_KEY=BDjxyd8leOwaRblApTo8dTSMetXR9_BQCpElln0Q1NalNyp13G8W6mCsc3ss88lp420Jf0mQyYx8iWdEqSvQG9c
```
(Same public key as the worker. The private key stays server-only.)

> These VAPID keys were generated for this project. Rotating them invalidates existing
> subscriptions (users re-enable). Keep `VAPID_PRIVATE_KEY` secret.

## Steps to go live

1. **Apply the migration** `20260720120000_push_subscriptions.sql` (Dashboard SQL Editor or pooler).
2. **Resend:** create an account, verify a sending domain, make an API key → set `RESEND_API_KEY` + `RESEND_FROM`.
3. Set the env vars above (worker + PWA), **redeploy** both. `pip install -r requirements.txt`
   picks up `pywebpush` on the worker build.
4. In the app, open **Profile → Notifications → Turn on** (grants permission + subscribes).

## Caveats

- **iOS:** Web Push only works if the user **adds the PWA to their Home Screen** (iOS 16.4+).
  Android/Chrome works in-browser. There is no native app, so this is the only push channel.
- Dead subscriptions (404/410 from the push service) are auto-pruned on send.
- A user with no email on file simply gets push-only.

## Files

- DB: `supabase/migrations/20260720120000_push_subscriptions.sql`
- Worker: `app/notifications.py`, hooks + meet-publish notify in `app/main.py`
- PWA: `src/lib/push.ts`, `src/app/sw.ts` (handlers), `src/app/(protected)/profile/notifications-toggle.tsx`,
  pings in `src/lib/events.ts` (`requestToJoinEvent` / `decideEventRequest`)
