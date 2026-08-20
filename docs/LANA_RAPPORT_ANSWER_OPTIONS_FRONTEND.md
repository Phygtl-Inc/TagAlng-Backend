# Rapport ask: suggested one-tap answers (`answer_options`)

**Status:** backend live (migration `20261029120000_rapport_answer_options.sql`). PWA unwired.

## What changed

The place-affinity question ("What do you enjoy most at the Orlando Public Library?")
shipped as a blank prompt — the user had to invent the shape of the answer. The
gap-opening call already AI-authors the question, so it now also returns 2–3 short
tappable facets. They ride on the existing `POST /lana/rapport/next-ask` payload:

```jsonc
{
  "ask": {
    "gap_row_id": "…",
    "question": "What do you enjoy most at the Orlando Public Library?",
    "place_name": "Orlando Public Library",
    "kind": "place_affinity",
    "answer_options": ["The quiet", "The kids' story hour", "The people"]  // NEW
  }
}
```

- `answer_options: string[]` — 0–3 strings, each ≤48 chars, already rendered in the
  user's language (same write-time i18n path as `question` / `why_reason`).
- **Absent or empty ⇒ free text only**, exactly as today. Not every ask has chips —
  only the place-affinity one authored on pin/join. Render nothing extra in that case.

## What the PWA needs

1. `src/lib/lana.schema.ts` → `rapportAskSchema`: add
   `answer_options: z.array(z.string()).nullish()`, and the matching optional field on
   the `RapportAsk` interface in `src/lib/lana.ts`.
2. `src/features/voice/components/rapport-expanded-card.tsx` (the opened tile — the one
   that today renders question + why-line + 👍/👎 + Not now): render the chips under the
   question. Mirror the chip visuals already used for grounding picks in
   `place-grounding-card.tsx` (single-select, selected = filled maroon + ✓).
3. Tap → `recordRapportAnswer(ask.gap_row_id, <chip text verbatim>)`. That's it — no new
   endpoint, no new field on the request. The chip text goes through the same answer path
   a typed answer takes, so it becomes the same place-tagged affinity claim.
4. "Type instead" stays available; chips are a shortcut, never a required choice.

Design reference: the `C-CIRCLE-ENJOY` frame — "What do you enjoy most there — the runs,
the childcare, the people?" with the three facets as chips.
