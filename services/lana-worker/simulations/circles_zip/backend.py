"""
backend.py — the SIM_BACKEND=stub|live switch.

This is the only place sweep.py (or any future caller) asks "which implementation."
Swapping to the real backend later means finishing live_impl.py and setting
SIM_BACKEND=live — no changes anywhere else in the harness.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from ports import AreaStatePort, CircleActivationPort, DisclosurePort, MatcherPort


@dataclass
class Backend:
    matcher: MatcherPort
    disclosure: DisclosurePort
    area_state: AreaStatePort
    activation: CircleActivationPort


def get_backend(
    zip_adjacency: dict[str, set[str]] | None = None,
    blocked_pairs: set[frozenset[str]] | None = None,
) -> Backend:
    """`blocked_pairs` is the harness stand-in for `user_blocks` / `lana_is_blocked` — the
    real matcher drops blocked peers on BOTH scoring arms (migration 20260914120000:76,:113)
    and the predicate is symmetric, so pairs are UNORDERED frozensets. It is threaded through
    here (not just accepted by MatcherStub) because otherwise nothing could ever exercise the
    exclusion: population.py generates the pairs, run_one_config passes them in. LIVE ignores
    it — the DB holds the real blocks."""
    kind = os.environ.get("SIM_BACKEND", "stub").strip().lower()
    if kind == "stub":
        from stub_impl import AreaStateStub, CircleActivationStub, DisclosureStub, MatcherStub

        return Backend(
            matcher=MatcherStub(zip_adjacency=zip_adjacency, blocked_pairs=blocked_pairs),
            disclosure=DisclosureStub(),
            area_state=AreaStateStub(),
            activation=CircleActivationStub(),
        )
    if kind == "live":
        from live_impl import AreaStateLive, CircleActivationLive, DisclosureLive, MatcherLive

        return Backend(
            matcher=MatcherLive(),
            disclosure=DisclosureLive(),
            area_state=AreaStateLive(),
            activation=CircleActivationLive(),
        )
    raise ValueError(f"Unknown SIM_BACKEND={kind!r}, expected 'stub' or 'live'")
