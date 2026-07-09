"""Lana agent orchestrator — Haiku router + Sonnet synthesizer (Vertex Claude)."""

__all__ = ["orchestrator_enabled", "run_opening", "run_turn"]


def orchestrator_enabled():
    from app.orchestrator.pipeline import orchestrator_enabled as _orchestrator_enabled

    return _orchestrator_enabled()


def run_opening(*args, **kwargs):
    from app.orchestrator.pipeline import run_opening as _run_opening

    return _run_opening(*args, **kwargs)


def run_turn(*args, **kwargs):
    from app.orchestrator.pipeline import run_turn as _run_turn

    return _run_turn(*args, **kwargs)
