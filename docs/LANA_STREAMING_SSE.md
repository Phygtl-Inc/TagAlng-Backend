# Lana streaming turns — how the backend sends events

This documents how the lana-worker streams a turn's progress to a client over
Server-Sent Events (SSE). It covers the backend only: the endpoint, the wire
format, the event types, and the mechanics that produce them.

The turn logic is identical to the blocking endpoint — only the transport differs.
Nothing about how a turn is *computed* changes; we just surface the stages it
already goes through as it goes through them.

---

## Endpoints

| Method | Path | Transport | Body |
| --- | --- | --- | --- |
| POST | `/lana/sessions/{session_id}/messages` | one JSON blob (blocking) | `SendMessageResponse` |
| POST | `/lana/sessions/{session_id}/messages/stream` | `text/event-stream` (SSE) | a sequence of frames, ending in the same `SendMessageResponse` |

Both accept the same request body (`SendMessageRequest`) and the same
`Authorization: Bearer <jwt>` header. Both run the identical turn — the streaming
one is a thin transport wrapper around the same core (`_run_lana_message` in
`app/main.py`). If the stream is unavailable for any reason, the blocking endpoint
remains a complete, unchanged fallback.

---

## Response headers

The streaming endpoint responds with:

```
Content-Type: text/event-stream
Cache-Control: no-cache
Connection: keep-alive
X-Accel-Buffering: no        # disables nginx response buffering so frames flush live
```

`X-Accel-Buffering: no` matters: any intermediary that buffers `text/event-stream`
(nginx, some proxies) would batch every frame and deliver them all at once when the
response closes — which defeats the point. This header tells nginx not to.

---

## The wire format

We use a small, explicit event envelope: **every frame is a single `data:` line
carrying one JSON object with a `type` field**, terminated by a blank line. This is
standard SSE framing.

```
data: {"type":"status","label":"Reading your message…"}

data: {"type":"status","label":"Finding people near you…"}

data: {"type":"status","label":"Composing…"}

data: {"type":"result","turn": { ...full SendMessageResponse... }}
```

Keep-alive comments (see below) look like this and carry no data:

```
: ping
```

### Event types

| `type` | Payload | Meaning |
| --- | --- | --- |
| `status` | `{"label": string}` | A progress label; the turn advanced to a new stage. Zero or more per turn. |
| `result` | `{"turn": SendMessageResponse}` | Terminal success frame. The `turn` object is byte-for-byte the same shape the blocking endpoint returns. Exactly one, last. |
| `error` | `{"detail": string \| object}` | Terminal failure frame (e.g. the turn raised). At most one; no `result` follows. |
| *(comment)* | — | `: ping` keep-alive. Not an event; carries no `data:`. Ignore it. |

A well-formed stream is: **N × `status`** → **one `result`** *(or one `error`)* →
stream closes. `: ping` lines may appear anywhere between frames.

---

## The status labels

Labels are honest — each one reflects a real stage boundary the turn actually
crossed, not a timer animating through plausible words.

The **first** label is always generic (`"Reading your message…"`), because before
the router runs we don't yet know what the user wants. **Every label after routing**
is derived from the router's real decision, so a user hosting a meet and a user
hunting for neighbors see genuinely different words.

The mapping lives in `app/orchestrator/progress.py`:

- `label_for_routing(routing)` picks the most specific honest label:
  - it prefers the **tool** about to run (`tool_to_call` → e.g. `find_peers` →
    `"Finding people near you…"`),
  - else the broad **intent** (`intent_class` → e.g. `activity` →
    `"Setting up your meet…"`),
  - else falls back to `"Composing…"`.
- Constants `READING` and `COMPOSING` are the two fixed bookends.

To add or change wording, edit the `_BY_TOOL` / `_BY_INTENT` tables in that file —
no other code changes.

### Where the labels are emitted from

The stages are emitted from inside the existing pipeline, at the points the turn
already passes through:

- `app/lana_unified_pipeline.py` — emits `READING` at turn entry.
- `app/orchestrator/pipeline.py` (`run_turn`):
  - `READING` at the top,
  - `label_for_routing(routing)` the moment routing is decided (before any tool runs),
  - `COMPOSING` just before the synthesizer LLM call.

For a typical orchestrator turn the client sees, in order: `Reading your message…`
→ *(intent/tool label)* → `Composing…` → the `result`.

---

## How the events get onto the wire

The turn code is **synchronous and blocking** (the LLM clients are sync). We keep it
that way — no async rewrite — and bridge to SSE with a worker thread + a queue.

### 1. The emitter rides on `TurnTimer`

`TurnTimer` (`app/turn_timing.py`) is already threaded through every pipeline path,
so it's the cheapest injection point. It gained two methods:

- `set_emitter(fn)` — attach a callback `(label: str) -> None`.
- `emit(label)` — call the callback if one is attached; **no-op otherwise**, and it
  swallows any error from the callback. Progress is best-effort and must never break
  a turn.

The blocking endpoint attaches no emitter, so `emit(...)` is a silent no-op there —
the non-streaming path is completely unaffected.

### 2. The streaming endpoint wires emitter → queue → generator

In `app/main.py`, `stream_lana_message` does:

1. Create a `queue.Queue`.
2. Define `emit(label)` that puts `("status", label)` on the queue.
3. Run the real turn (`_run_lana_message(..., emit=emit)`) on a **worker thread**.
   As the pipeline advances, each `timer.emit(...)` drops a `status` item on the
   queue. When the turn finishes, the worker puts `("result", response)` — or
   `("error", detail)` if it raised — then a `("done", None)` sentinel.
4. A **sync generator** drains the queue and yields SSE frames:
   - `status` → `data: {"type":"status","label":...}`
   - `result` → `data: {"type":"result","turn": <response.model_dump(mode="json")>}`
   - `error` → `data: {"type":"error","detail":...}`
   - `done` → break (closes the stream)
5. The generator is handed to Starlette's `StreamingResponse`, which flushes each
   yielded frame to the socket as it's produced.

```
timer.emit(label)          worker thread                 SSE generator (main path)
      │                          │                               │
      └── queue.put ────────────►│──────── queue.get ───────────►│──► yield "data: …\n\n"
                                 │                               │
   _run_lana_message finishes ──►│ queue.put ("result", resp) ──►│──► yield result frame
                                 │ queue.put ("done") ──────────►│──► break → close
```

### 3. Keep-alive

While the generator waits on the queue it uses a 10-second timeout. On timeout it
yields a `: ping` comment. This keeps the connection open through a long synthesis
call so a proxy's idle timeout doesn't drop it, without emitting a spurious event.

### 4. Background jobs still run

`_run_lana_message` registers fire-and-forget work (embeddings, claim extraction,
etc.) on FastAPI's `BackgroundTasks`. The worker populates that list *before* it
queues the `result` frame, so FastAPI still runs those jobs after the stream closes —
exactly as it does for the blocking endpoint.

---

## Error semantics

- If the turn raises `HTTPException`, the worker emits `{"type":"error","detail": <exc.detail>}`.
- Any other exception is logged (`stream_lana_message worker failed …`) and emitted
  as `{"type":"error","detail":"lana_message_failed"}`.
- In both cases a `done` sentinel follows and the stream closes cleanly. No `result`
  frame is sent on error.

---

## Concurrency note

Each in-flight stream uses ~2 threads: the SSE generator runs in FastAPI's threadpool
and the turn runs on the worker thread we spawn. This is fine at current volume; if
streaming concurrency grows a lot, raise the server's thread limit accordingly.
