"""Per-turn latency breakdown for Lana (ms)."""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable

# Step-by-step turn trace. On by default so a local `run-lana-worker-local.sh` prints
# a line as each stage of a turn starts/finishes — set LANA_TRACE=0 to silence it.
# Logs on the "app" logger, which main.py wires to stderr at INFO.
_TRACE_LOG = logging.getLogger("app.turn.trace")
_TRACE_ON = os.environ.get("LANA_TRACE", "1").strip().lower() not in ("0", "false", "no", "")


class TurnTimer:
    """Accumulate stage durations for one request."""

    def __init__(self) -> None:
        self._starts: dict[str, float] = {}
        self.ms: dict[str, int] = {}
        self.counts: dict[str, int] = {}
        # Optional progress emitter (streaming endpoint attaches one). No-op otherwise,
        # so every non-streaming caller is unaffected. The timer is already threaded
        # through every pipeline path, so this is the cheapest place to surface progress.
        self._emit: Callable[[str], None] | None = None
        # Optional token-delta emitter (streaming endpoint attaches one). Receives
        # incremental assistant_message text while the synthesizer LLM generates —
        # the SSE layer forwards each fragment as a {"type":"delta"} frame.
        self._emit_delta: Callable[[str], None] | None = None

    def set_emitter(self, emit: Callable[[str], None] | None) -> None:
        self._emit = emit

    def set_delta_emitter(self, emit: Callable[[str], None] | None) -> None:
        self._emit_delta = emit

    @property
    def streams_deltas(self) -> bool:
        return self._emit_delta is not None

    def emit_delta(self, text: str) -> None:
        """Push an incremental chunk of assistant text to the client, if streaming.

        Best-effort, like `emit`: swallows emitter errors, no-op when not streaming.
        """
        if self._emit_delta is None or not text:
            return
        try:
            self._emit_delta(text)
        except Exception:  # noqa: BLE001 — deltas are best-effort
            pass

    def emit(self, label: str) -> None:
        """Push a human-readable progress label to the client, if streaming.

        Swallows any emitter error: progress is best-effort and must never break a turn.
        """
        if self._emit is None:
            return
        try:
            self._emit(label)
        except Exception:  # noqa: BLE001 — progress is best-effort
            pass

    def set_count(self, name: str, value: int) -> None:
        if value > 0:
            self.counts[name] = value

    def start(self, stage: str) -> None:
        self._starts[stage] = time.perf_counter()

    def stop(self, stage: str) -> None:
        started = self._starts.pop(stage, None)
        if started is None:
            return
        elapsed = int((time.perf_counter() - started) * 1000)
        self.ms[stage] = self.ms.get(stage, 0) + elapsed

    class _Stage:
        def __init__(self, timer: TurnTimer, stage: str) -> None:
            self._timer = timer
            self._stage = stage

        def __enter__(self) -> None:
            self._timer.start(self._stage)
            if _TRACE_ON:
                _TRACE_LOG.info("→ %s", self._stage)

        def __exit__(self, exc_type: object, *args: object) -> None:
            self._timer.stop(self._stage)
            if _TRACE_ON:
                ms = self._timer.ms.get(self._stage, 0)
                if exc_type is not None:
                    _TRACE_LOG.info("✗ %s (%dms) — raised %s", self._stage, ms, getattr(exc_type, "__name__", exc_type))
                else:
                    _TRACE_LOG.info("✓ %s (%dms)", self._stage, ms)

    def stage(self, name: str) -> _Stage:
        return self._Stage(self, name)

    def add(self, stage: str, ms: int) -> None:
        if ms > 0:
            self.ms[stage] = self.ms.get(stage, 0) + ms

    def total_ms(self) -> int:
        return sum(self.ms.values())

    def to_dict(self) -> dict[str, Any]:
        out = dict(sorted(self.ms.items(), key=lambda kv: kv[1], reverse=True))
        out.update(self.counts)
        out["total_ms"] = self.total_ms()
        return out
