# TagAlng invariants

## Query / API

- **Inclusive-only** cohort and identity filters  
- **No exclude operator** for protected attributes → return **422** + log `protected_attribute_redaction_attempt`  
- **No empty rooms** — server rejects surfacing dead blocks  
- Combine filters: support 2–4 cohort AND for engagement/moat metrics  

## Data model

| Never store | Store with care |
|-------------|-----------------|
| race | faith, sobriety, LGBTQ+ → mutual disclosure |
| exact age | life-stage claims (coarse) |
| sex | — |
| street address | H3 block + optional finer res with opt-in |

| Always | Conditional |
|--------|-------------|
| nickname | realName only if connected |
| block_id (H3) | raw GPS never broadcast |

## Activities

- Rows in `events` are **only** created by authenticated hosts (users)  
- No cron/worker inserts "suggested" events  
- No push for events user did not RSVP/thread into  

## Agents (MVP)

- Cloud Run **workers**, not autonomous agent loops  
- Gemini calls logged; claims user-editable  
- Moderation: flag only, no auto-delete at MVP  

## Negative space (do not build)

- Autonomous agent platform as product  
- Generative activity feed  
- Swipe / public photos before co-checkin  
- Surveillance / live GPS broadcast  
- General-purpose social graph  
- Paid reach into blocks  

## GTM vs product

- "Mom", "Founding Moms", Lake Nona dossier = **launch wedge**  
- Platform copy in code: **user / host / fellow / block**  
