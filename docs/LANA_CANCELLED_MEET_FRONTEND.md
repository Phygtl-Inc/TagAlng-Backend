# Cancelled-meet affordances — FE handoff (C-18-CANCELLED / C-18-ATTENDEE-CANCELLED)

Backend for the two stubbed "coming soon" affordances is in
`supabase/migrations/20260904120000_event_dismissals_cancel_notify.sql` (needs
`db push`) plus a new worker hook. What the PWA needs to change:

## 1. "Remove from my list" → `dismiss_event`

```ts
await supabase.rpc("dismiss_event", { p_event_id: eventId });
```

- Per-user hide: inserts an `event_dismissals(user_id, event_id)` row for the
  caller only — nothing global changes. Works for the host of a cancelled meet
  and for an attendee (issue #53 self-remove).
- Idempotent — repeat calls no-op. Errors: `event_not_found` (bad id),
  `not_authenticated` (anon).
- `get_my_contributions` now filters dismissed events out of both the
  host/co-host arm and the joined-meets arm, so a refetch after the RPC drops
  the card. Optimistically removing the card client-side is safe.

## 2. Cancel fan-out → call the new hook after `cancel_event`

`cancel_event` itself changed (same migration):

- Host-only now enforced in the function (`not_event_host` error for anyone
  else), idempotent (`already cancelled` is a silent no-op).
- On the first successful cancel it posts a `kind='system'` message —
  "This meet was cancelled by the host." — into the meet's group chat, so
  every approved attendee gets the in-app notice.

Push + email to the going roster comes from the worker. Right after the RPC
succeeds, mirror the existing `/hooks/event-join` pattern:

```ts
await fetch(`${WORKER_URL}/hooks/event-cancel`, {
  method: "POST",
  headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
  body: JSON.stringify({ event_id: eventId }),
});
// → { ok: true, notified: N }
```

- The hook re-verifies the caller is the host and the event is actually
  `cancelled`, then sends web-push + email to every request with
  `status in (approved, attended)` and `rsvp_status = 'going'`
  (host excluded). Fire-and-forget; a failed hook call never blocks the UI.
- `notified` is the roster size — usable for the "I let everyone know" /
  per-attendee "Notified" copy. Call the hook once per cancel: it is not
  deduplicated, so a second call would re-push.

## Scope notes (related gaps from issue #55)

- **Attendee "Leave meet" on a CANCELLED meet** → use `dismiss_event` (this
  ticket). It hides the meet from the attendee's `get_my_contributions` only;
  the meet page itself (`/meet/<id>` preview) is untouched by design.
- **Attendee "Leave meet" on an ACTIVE meet** → out of scope here, split it.
  Different semantics: today `set_event_rsvp(p_event_id, 'not_going')` flips
  intent and deliberately KEEPS group-chat membership (migration 20260826);
  a true `leave_event` (withdraw request + drop chat membership) reverses that
  decision and needs its own RPC + product sign-off.
- **Uncancel/restore** → no RPC exists; out of scope (low priority per ticket).

## Deploy order

1. `supabase db push` (migration `20260904120000`).
2. Deploy the worker (new `POST /hooks/event-cancel`).
3. FE unstubs the two affordances.
