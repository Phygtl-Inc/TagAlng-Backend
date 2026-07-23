"""Lana agent orchestrator — Haiku router + Sonnet synthesizer (Vertex Claude).

Exports resolve lazily (PEP 562): submodules like json_util are imported by modules
outside the package (e.g. vertex_extract), and an eager pipeline import here would
close a circular chain (vertex_extract → json_util → __init__ → pipeline → tools →
vertex_extract) whenever such a module loads first.
"""

from typing import Any

__all__ = ["orchestrator_enabled", "run_opening", "run_turn"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from app.orchestrator import pipeline

        return getattr(pipeline, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
