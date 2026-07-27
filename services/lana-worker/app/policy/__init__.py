"""Lana conversational policy — the unified decide_turn brain.

One decision per turn over one context: world state (world.py), candidate
goals from the four queues (goals.py), the NextAction policy call (decide.py),
and session memory hygiene (summary.py). Gated by LANA_DECIDE_TURN
(off | shadow | on) — see lana_paths.decide_turn_mode().
"""
