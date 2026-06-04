# TagAlng data model — claims vs “fields per user”

**Audience:** engineers comparing TagAlng to typical social / dating / community apps.

---

## Short answer

We store **rows in shared tables**, not a custom set of columns per user.

Each woman gets **many claim rows** in `user_identity_claims` (flexible “threads”), plus normal rows in `users`, `lana_messages`, etc. We do **not** add `users.baking`, `users.italian`, … as the product learns new facets.

That pattern is common for **AI-native / semantic profiles** and uncommon for **fixed-form signup**.

---

## How similar apps usually work

| Style | Example apps | Storage |
|--------|----------------|---------|
| **Wide user table** | Early Facebook, simple directories | `users.job`, `users.city`, nullable columns |
| **EAV / profile answers** | Dating apps, questionnaires | `profile_answers(question_id, value)` |
| **Tags / interests** | Meetup, LinkedIn skills | `user_tags(tag_id)` join table |
| **Graph DB** | Some recommendation systems | Nodes + edges (friends, likes) |
| **Semantic claims + vectors** | Modern AI matching stacks | Entity rows + embeddings (what TagAlng does) |

TagAlng is closest to **semantic claims + vectors**, scoped by **block (vicinity)**.

---

## TagAlng tables (mental map)

```
users                    ← one row per person (auth, home_block_id, nickname)
user_identity_claims     ← many rows per person (identity threads)
lana_sessions            ← chat sessions
lana_messages            ← chat lines (not matching truth)
events / event_requests  ← activity on the block
blocks                   ← vicinity unit
```

**Profile truth for matching:** `user_identity_claims` only.  
**Chat:** transcript for Lana; compressed into claims on **complete**.

---

## One claim row (example)

User said: *“Italian, love thick pizza, two kids.”*

| column | example | role |
|--------|---------|------|
| `concept` | `italian_heritage` | Stable id for overlap (“same thread”) |
| `label` | `Italian` | UI card title |
| `synonyms` | `{...}` | “≈” chips |
| `bucket` | `heritage` | UI color category (7 allowed values) |
| `source_quote` | `"I'm Italian"` | “From …” line |
| `disclosure` | `public` / `mutual` / `private` | Who can see it |
| `embedding` | vector(768) | Similarity search (pgvector) |

**Buckets are not hobbies.** “Baking” is a **claim** with `bucket = activity`, not a new bucket type.

---

## Compared to “building fields for each user”

| Per-user fields | TagAlng claims |
|-----------------|----------------|
| Schema migration for every new attribute | New **rows**, same table |
| Same columns for everyone | Sparse: users have different claim sets |
| Hard for AI to add nuance | AI extracts `concept` + `label` + quote |
| Matching on column equality | Matching on `concept` overlap + embeddings |

---

## Is this “correct” for the CEO vision?

Yes — **identity × vicinity × activity**:

- **Identity** → claims (+ Lana intake)  
- **Vicinity** → `users.home_block_id`, blocks, geo RPCs  
- **Affinity** → claims + event `cohort_tags` + (future) intents  
- **Lana agent** → reads claims + **retrieves** block context (`get_lana_block_context_for_user`) before replying  

---

## Vector peer match (live)

**RPC (signed-in PWA):** `match_peers_by_claim_vectors(p_limit, p_min_similarity)`

- Compares your **public** claim embeddings to others on the **same `home_block_id`**.  
- Returns best peer per person with `similarity_score` (0–1, cosine).  
- `has_exact_concept_match` = also shares the same `concept` slug (old overlap rule).  

**Worker:** `match_peers_by_claim_vectors_for_user` (service role) — injected into Lana context.

Requires embeddings on claims (after Lana/identity **complete**). Signup chat before complete → usually empty.

Migration: `20260609120000_match_peers_by_vector.sql`

---

## Agent retrieval (not full RAG)

On each Lana turn the worker loads:

1. User + block + existing claims (structured)  
2. **Block network** — neighbor public labels, upcoming events (RPC)  
3. **Vector peers** — similar-meaning neighbors on block (pgvector)  
4. Chat history (recent messages)  

That is **retrieve → augment prompt → generate**, without a separate doc search product.

---

## Related docs

- [`LANA_API.md`](./LANA_API.md) — HTTP API  
- [`FRONTEND_API.md`](./FRONTEND_API.md) — signup order  
- [`PWA_V01_BACKEND_HANDOFF.md`](./PWA_V01_BACKEND_HANDOFF.md) — events / RTJ  
