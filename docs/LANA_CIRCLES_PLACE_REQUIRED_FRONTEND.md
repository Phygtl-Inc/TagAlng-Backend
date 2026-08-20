# FE handoff · Communities: place is now REQUIRED

Backend change (live on dev, worker + prod pending): **a community cannot exist
without a real location.** Two things change for the PWA — one is a hard break,
one is automatic.

## 1. Add drawer — place is now mandatory (hard break)

`POST /lana/circles/add` now **rejects** a request without `google_place_id`:

```
400 { "detail": "place_required" }
```

Fix in `src/features/voice/components/community-add-drawer.tsx`:

- Require a place pick before enabling Save — one-line change:

  ```tsx
  disabled={saving || !detail.trim() || !place}
  ```

- The submit payload doesn't need to change (`googlePlaceId: place?.googlePlaceId
  ?? null` is fine once the button gates on `place`), but you can drop the `?? null`
  if you prefer.

Copy: `circles.add.placeLabel` currently says "Which spot? **(optional)**" — drop
the "(optional)" in all three locales (`messages/en.json`, `es.json`, `pt-BR.json`,
key `circles.add.placeLabel`).

A successful add now always comes back grounded + confirmed
(`{place_id, place_name, status: "confirmed"}`) — no more
`{status: "suggested", grounded: false}` from this endpoint.

## 2. Communities list — ungrounded rows are gone (automatic)

`POST /lana/circles/list` now returns **grounded rows only**. Chat-captured
mentions that haven't been pinned to a place no longer appear — they're internal
candidates, and Lana asks "which spot is it?" (rapport tile / chat) to convert
them. Once grounded they show up here as normal.

Nothing breaks (the response shape is unchanged, there are just fewer rows), but
in `communities-panel.tsx` the `!circle.grounded` branch — the "You mentioned
this" / "Pick the spot" row and the dashed-border edit CTA — is now dead code and
can be removed whenever convenient. Same for any ungrounded-only paths in
`community-edit-drawer.tsx`.

## Unchanged

- `/lana/circles/ground-options` + `/lana/circles/ground` — same contracts (the
  rapport tile's grounding chips keep working as-is).
- `/lana/invites/self-confirm` — still creates the joiner's ungrounded candidate;
  she grounds her own place right after, exactly like today.
- `/lana/circles/update` / `/remove` — unchanged.
