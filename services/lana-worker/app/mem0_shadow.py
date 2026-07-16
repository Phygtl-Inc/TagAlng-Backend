"""mem0 cloud shadow / A-B harness — run mem0 alongside native MemGPT recall.

This is a TRIAL scaffold (see docs: mem0 evaluation report). Its whole job is to let us
answer "is mem0 better than what we already have?" with data instead of opinion, without
putting the live turn at any risk. Three invariants hold everywhere in this file:

  * env-gated       — with LANA_MEM0_ENABLED unset/false, every public function is a no-op
                      or a passthrough. Zero behavior change, zero mem0 network calls.
  * timeout-safe    — mem0 calls run in a bounded thread pool. A slow or failing mem0 call
                      never blocks the turn and never raises into the pipeline; it degrades
                      to native recall and is logged.
  * isolated        — the native claims/recall path is untouched. mem0 writes go to mem0's
                      cloud + the mem0_compare_log telemetry table only.

A/B design
----------
Users are split deterministically by user_id hash into two arms:
    arm 'A' (control)   -> native recall is injected into the prompt (status quo)
    arm 'B' (treatment) -> mem0 recall is injected instead
LANA_MEM0_ARM_PCT controls what fraction land in arm B (default 50).

BOTH arms dual-write to mem0 and log native-vs-mem0 retrieval to mem0_compare_log, so the
offline judge can score retrieval quality across the whole population while only arm B
changes what the LLM actually sees. We deliberately do NOT inject both sources into the same
prompt: that entangles them and makes "which is better" unmeasurable (there'd be no baseline).

Privacy note: this trial routes PII-scrubbed utterances to mem0's MANAGED cloud. That is a
conscious, signed-off trade-off for the trial only — it is not the steady-state design.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Any

_log = logging.getLogger(__name__)

# Single small pool. dual_write submits and never waits; search submits and waits with a tight
# timeout. Sized so a burst of turns can't starve either path.
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="mem0")

_client_instance: Any = None
_client_failed = False  # latch: if construction fails once, stop retrying every turn


# --------------------------------------------------------------------------- config


def enabled() -> bool:
    flag = os.environ.get("LANA_MEM0_ENABLED", "").strip().lower()
    return flag in ("1", "true", "on", "yes")


def _api_key() -> str:
    return os.environ.get("MEM0_API_KEY", "").strip()


def _arm_pct() -> int:
    """Percentage of users assigned to arm B (mem0 injected). Clamped to [0, 100]."""
    raw = os.environ.get("LANA_MEM0_ARM_PCT", "50").strip()
    try:
        return max(0, min(100, int(raw)))
    except ValueError:
        return 50


def _search_timeout_sec() -> float:
    """Hard ceiling on the arm-B critical-path mem0.search call. Small on purpose —
    if mem0 can't answer this fast it's not viable for a live turn anyway."""
    raw = os.environ.get("LANA_MEM0_TIMEOUT_SEC", "2.0").strip()
    try:
        return max(0.25, float(raw))
    except ValueError:
        return 2.0


def _search_k() -> int:
    raw = os.environ.get("LANA_MEM0_SEARCH_K", "5").strip()
    try:
        return max(1, min(20, int(raw)))
    except ValueError:
        return 5


def arm_for_user(user_id: str) -> str:
    """Deterministic, stable arm assignment. Same user always lands in the same arm across
    sessions, so their whole history accrues under one condition. Salted so the split doesn't
    correlate with any other user_id-hash bucketing elsewhere in the system."""
    pct = _arm_pct()
    if pct <= 0:
        return "A"
    if pct >= 100:
        return "B"
    digest = hashlib.sha256(f"mem0-arm:{user_id}".encode()).hexdigest()
    bucket = int(digest[:8], 16) % 100  # 0..99
    return "B" if bucket < pct else "A"


# --------------------------------------------------------------------------- client


def _client():
    """Lazy MemoryClient singleton. Returns None (and latches) if mem0 is unavailable, so a
    missing dependency or bad key degrades to native recall instead of crashing turns."""
    global _client_instance, _client_failed
    if _client_instance is not None:
        return _client_instance
    if _client_failed:
        return None
    key = _api_key()
    if not key:
        _client_failed = True
        _log.warning("mem0 enabled but MEM0_API_KEY is not set; shadow disabled")
        return None
    try:
        from mem0 import MemoryClient  # imported lazily; optional dependency

        _client_instance = MemoryClient(api_key=key)
        return _client_instance
    except Exception as exc:  # ImportError, auth, network — all degrade the same way
        _client_failed = True
        _log.warning("mem0 client construction failed; shadow disabled: %s", exc)
        return None


def _do_add(user_id: str, messages: list[dict[str, str]]) -> None:
    client = _client()
    if client is None:
        return
    try:
        client.add(messages, user_id=user_id)
    except Exception as exc:
        _log.debug("mem0.add failed for user=%s: %s", user_id, exc)


def _do_search(user_id: str, query: str, k: int) -> list[dict[str, Any]]:
    """Call mem0.search across SDK signature variants (kwargs shifted between versions).
    Returns the raw results list (possibly empty). Raises on hard failure — the caller
    (submitted to the pool with a timeout) turns any raise/timeout into a logged miss."""
    client = _client()
    if client is None:
        return []
    try:
        res = client.search(query, user_id=user_id, limit=k)
    except TypeError:
        # Newer API takes a filters dict instead of user_id/limit kwargs.
        res = client.search(query, filters={"user_id": user_id}, top_k=k)
    if isinstance(res, dict):
        res = res.get("results", [])
    return res if isinstance(res, list) else []


# --------------------------------------------------------------------------- shaping


def _to_recall_shape(mem0_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map mem0's {memory, score, ...} rows onto the prefetch dict shape the core block and
    format_core_block already understand, so arm B needs ZERO downstream changes."""
    shaped: list[dict[str, Any]] = []
    for r in mem0_results:
        if not isinstance(r, dict):
            continue
        content = r.get("memory") or r.get("content")
        if not content:
            continue
        shaped.append(
            {
                "source_type": "mem0",
                "source_id": r.get("id"),
                "content": content,
                "similarity": r.get("score"),
                "captured_at": r.get("created_at"),
                "scope": "self",
                "prefetch": True,
            }
        )
    return shaped


def _slim_native(native: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Trim native prefetch rows to the fields worth logging for the judge."""
    out: list[dict[str, Any]] = []
    for r in native or []:
        if not isinstance(r, dict):
            continue
        out.append(
            {
                "source_type": r.get("source_type"),
                "content": r.get("content"),
                "similarity": r.get("similarity"),
                "scope": r.get("scope"),
            }
        )
    return out


def _slim_mem0(mem0_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in mem0_results or []:
        if not isinstance(r, dict):
            continue
        out.append(
            {
                "memory": r.get("memory") or r.get("content"),
                "score": r.get("score"),
                "categories": r.get("categories"),
            }
        )
    return out


# --------------------------------------------------------------------------- telemetry


def _log_comparison(row: dict[str, Any]) -> None:
    from app.auth import service_client

    try:
        service_client().table("mem0_compare_log").insert(row).execute()
    except Exception as exc:
        _log.debug("mem0_compare_log insert failed: %s", exc)


# --------------------------------------------------------------------------- public API


def dual_write(
    *,
    user_id: str,
    session_id: str | None,
    user_text: str,
    assistant_text: str,
) -> None:
    """Fire-and-forget: push this turn's user+assistant exchange into mem0 so both arms build
    the same mem0-side memory. Never blocks the turn; failures are swallowed and logged."""
    if not enabled() or not user_id:
        return
    ut = (user_text or "").strip()
    if not ut:
        return
    messages = [{"role": "user", "content": ut}]
    at = (assistant_text or "").strip()
    if at:
        messages.append({"role": "assistant", "content": at})
    try:
        fut = _executor.submit(_do_add, user_id, messages)
        fut.add_done_callback(lambda f: f.exception())  # drain, keep the pool quiet
    except Exception as exc:
        _log.debug("mem0 dual_write submit failed: %s", exc)


def apply_prefetch(
    *,
    user_id: str,
    session_id: str | None,
    turn_id: str | None,
    utterance: str,
    native_prefetched: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The single retrieval entry point the pipeline calls.

    Runs mem0.search alongside the already-computed native prefetch, logs both for the judge,
    and returns the list to actually inject:
        arm A -> native_prefetched (unchanged control)
        arm B -> mem0 results, shaped like native; falls back to native if mem0 is empty/errored
    With the flag off, returns native_prefetched untouched (pure passthrough)."""
    if not enabled() or not user_id or not utterance.strip():
        return native_prefetched

    arm = arm_for_user(user_id)
    k = _search_k()
    mem0_results: list[dict[str, Any]] = []
    mem0_error: str | None = None
    started = time.monotonic()
    try:
        fut = _executor.submit(_do_search, user_id, utterance[:2000], k)
        mem0_results = fut.result(timeout=_search_timeout_sec())
    except FutureTimeout:
        mem0_error = f"timeout>{_search_timeout_sec()}s"
    except Exception as exc:
        mem0_error = str(exc)[:300]
    mem0_latency_ms = int((time.monotonic() - started) * 1000)

    # Log both snapshots for the offline judge (fire-and-forget).
    try:
        row = {
            "user_id": user_id,
            "session_id": session_id,
            "turn_id": turn_id,
            "arm": arm,
            "query": utterance[:2000],
            "native_results": _slim_native(native_prefetched),
            "mem0_results": _slim_mem0(mem0_results),
            "native_count": len(native_prefetched or []),
            "mem0_count": len(mem0_results or []),
            "mem0_latency_ms": mem0_latency_ms,
            "mem0_error": mem0_error,
        }
        f2 = _executor.submit(_log_comparison, row)
        f2.add_done_callback(lambda f: f.exception())
    except Exception as exc:
        _log.debug("mem0 compare-log submit failed: %s", exc)

    # Only arm B changes what the LLM sees, and only when mem0 actually returned something.
    if arm == "B" and mem0_results:
        return _to_recall_shape(mem0_results)
    return native_prefetched


# --------------------------------------------------------------------------- offline judge

_JUDGE_SYSTEM = (
    "You are evaluating two memory-retrieval systems for a neighborhood social assistant. "
    "Given a user's message and two candidate sets of recalled memories (NATIVE and MEM0), decide "
    "which set is more RELEVANT and USEFUL for responding to that message. Judge only relevance to "
    "the query — ignore formatting and length. Reply with strict JSON: "
    '{"winner": "native"|"mem0"|"tie"|"both_empty", "reason": "<one sentence>"}.'
)


def judge_pending(limit: int = 50) -> dict[str, int]:
    """Offline scoring pass — NOT in the turn path. Grabs unjudged compare-log rows, asks the
    existing Lana LLM which retrieval set is more relevant, and writes the verdict back.
    Run from a cron/REPL: `from app.mem0_shadow import judge_pending; judge_pending(200)`.
    Returns a tally, e.g. {'native': 12, 'mem0': 30, 'tie': 5, 'both_empty': 3}."""
    from datetime import datetime, timezone

    from app.auth import service_client
    from app.orchestrator.llm import llm_json, router_model

    sb = service_client()
    rows = (
        sb.table("mem0_compare_log")
        .select("id, query, native_results, mem0_results")
        .is_("judged_at", "null")
        .order("created_at")
        .limit(max(1, min(int(limit), 500)))
        .execute()
    ).data or []

    tally = {"native": 0, "mem0": 0, "tie": 0, "both_empty": 0}
    model = router_model()
    for row in rows:
        native = row.get("native_results") or []
        mem0 = row.get("mem0_results") or []
        if not native and not mem0:
            verdict, reason = "both_empty", "neither system returned memories"
        else:
            payload = (
                f"USER MESSAGE:\n{row.get('query')}\n\n"
                f"NATIVE memories:\n{_fmt_for_judge(native, 'native')}\n\n"
                f"MEM0 memories:\n{_fmt_for_judge(mem0, 'mem0')}"
            )
            try:
                out = llm_json(model=model, system=_JUDGE_SYSTEM, user_payload=payload, max_tokens=256)
                verdict = str(out.get("winner") or "tie").strip().lower()
                if verdict not in tally:
                    verdict = "tie"
                reason = str(out.get("reason") or "")[:500]
            except Exception as exc:
                _log.debug("judge llm failed for row %s: %s", row.get("id"), exc)
                continue
        tally[verdict] += 1
        try:
            sb.table("mem0_compare_log").update(
                {
                    "judge_verdict": verdict,
                    "judge_reason": reason,
                    "judge_model": model,
                    "judged_at": datetime.now(timezone.utc).isoformat(),
                }
            ).eq("id", row.get("id")).execute()
        except Exception as exc:
            _log.debug("judge write-back failed for row %s: %s", row.get("id"), exc)
    return tally


def _fmt_for_judge(items: list[dict[str, Any]], kind: str) -> str:
    if not items:
        return "(none)"
    lines = []
    for it in items[:8]:
        if kind == "native":
            lines.append(f"- [{it.get('source_type')}] {it.get('content')}")
        else:
            lines.append(f"- {it.get('memory')}")
    return "\n".join(lines)
