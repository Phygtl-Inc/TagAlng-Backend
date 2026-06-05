"""Per-turn latency breakdown for Lana (ms)."""

from __future__ import annotations

import time
from typing import Any


class TurnTimer:
    """Accumulate stage durations for one request."""

    def __init__(self) -> None:
        self._starts: dict[str, float] = {}
        self.ms: dict[str, int] = {}
        self.counts: dict[str, int] = {}

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

        def __exit__(self, *args: object) -> None:
            self._timer.stop(self._stage)

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
