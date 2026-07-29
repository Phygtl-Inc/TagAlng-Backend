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


def get_backend(zip_adjacency: dict[str, set[str]] | None = None) -> Backend:
    kind = os.environ.get("SIM_BACKEND", "stub").strip().lower()
    if kind == "stub":
        from stub_impl import AreaStateStub, CircleActivationStub, DisclosureStub, MatcherStub

        return Backend(
            matcher=MatcherStub(zip_adjacency=zip_adjacency),
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
