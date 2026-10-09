"""The zero-bug program's overnight section runner.

See tools/swarm/README.md. The short version: `run_swarm.py --section P1` walks
9 personas through a section spec, asserts on `TurnRouting` and the database, and
writes one `simulations` row per (run_id, section_id, persona_id, arm).
"""

__all__ = [
    "assertions",
    "config",
    "identity",
    "language",
    "preflight",
    "registry",
    "runner",
    "worker",
]
