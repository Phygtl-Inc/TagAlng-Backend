"""Keyword matchers may only be called from the places reviewed for AI-first routing.

"AI-first was a habit, not something enforced" (intent-classification review, 2026-10-13):
the community-create rescue called the matcher directly, skipped the guard, and armed a
capture on "startup community". This test makes a NEW call site fail loudly.

If it fails for your change: the classifier decides the lane. A keyword may only fill in
when the model is unsure (confidence < 0.6), gave no label, or failed — route through
`layer1_intents._reconcile_defer_to_llm`, or follow `_turn_is_community_create`. Then add
the call site below with a one-line reason in your PR. Vetoes (a keyword that only BLOCKS
a wrong flow) are fine; overrides of a confident pick are not.
"""

from __future__ import annotations

import ast
import pathlib

MATCHERS = frozenset({
    "utterance_indicates_tip_share",
    "utterance_indicates_tip_seek",
    "utterance_indicates_swap_seek",
    "utterance_indicates_swap_offer",
    "utterance_indicates_hosting_plan",
    "looks_like_community_create",
    "phrase_linear_intent",
})

# (file, enclosing function) — reviewed 2026-10-14.
ALLOWED = frozenset({
    # The guarded reconcilers and the fallback inside enrich_slots.
    ("app/layer1_intents.py", "enrich_slots"),
    ("app/layer1_intents.py", "reconcile_hosting_peer_slot_conflict"),
    ("app/layer1_intents.py", "reconcile_swap_seek_signal"),
    ("app/layer1_intents.py", "reconcile_tip_seek_signal"),
    ("app/layer1_intents.py", "reconcile_tip_share_signal"),
    # Matchers narrowing each other (exclusions, not routing).
    ("app/layer1_intents.py", "utterance_indicates_hosting_plan"),
    ("app/layer1_intents.py", "utterance_indicates_swap_seek"),
    ("app/layer1_intents.py", "utterance_indicates_tip_seek"),
    # Capture entries — defer to a confident model pick before the keyword speaks.
    ("app/lana_unified_pipeline.py", "_turn_is_community_create"),
    ("app/lana_unified_pipeline.py", "_turn_is_tip_share"),
    # Vetoes / lane releases / refetch skips: only BLOCK a wrong flow.
    ("app/activity_browse.py", "_is_browse_answer"),
    ("app/discovery_route.py", "_message_bypasses_heritage_pending"),
    ("app/discovery_route.py", "_peer_find_turn_blocked"),
    ("app/discovery_route.py", "_should_skip_preview_refetch"),
    ("app/discovery_slots.py", "slots_want_preview_refetch"),
    # Route through enrich_slots' guarded reconcilers before acting.
    ("app/discovery_route.py", "_try_signal_seek_early_turn"),
    ("app/discovery_route.py", "_try_tip_seek_fast_turn"),
    ("app/discovery_route.py", "handle_discovery_turn"),
    # Known soft spot: vetoes a browse before reading the model — review in step 6.
    ("app/discovery_route.py", "_browse_or_seek_decision"),
    # phrase_linear_intent consumers for structural, non-lane decisions.
    ("app/discovery_route.py", "_try_change_name_turn"),
    ("app/discovery_route.py", "_try_list_intros_turn"),
    ("app/discovery_route.py", "_try_phrase_policy_turn"),
    ("app/discovery_route.py", "_try_respond_nudge_turn"),
    ("app/discovery_route.py", "_wants_block_log"),
})

_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _call_sites() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in sorted((_ROOT / "app").rglob("*.py")):
        rel = path.relative_to(_ROOT).as_posix()
        stack: list[str] = []

        class _Visitor(ast.NodeVisitor):
            def visit_FunctionDef(self, node: ast.AST) -> None:
                stack.append(node.name)  # type: ignore[attr-defined]
                self.generic_visit(node)
                stack.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Call(self, node: ast.Call) -> None:
                fn = node.func
                name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
                if name in MATCHERS:
                    found.add((rel, stack[-1] if stack else "<module>"))
                self.generic_visit(node)

        _Visitor().visit(ast.parse(path.read_text(encoding="utf-8")))
    return found


def test_no_new_keyword_matcher_call_sites() -> None:
    new = sorted(_call_sites() - ALLOWED)
    assert not new, (
        "New keyword-matcher call site(s) — a keyword must not override a confident "
        f"classifier pick (see this file's docstring): {new}"
    )


def test_the_allowlist_has_no_dead_entries() -> None:
    """Keep the list honest: a removed call site comes off the list too."""
    assert not sorted(ALLOWED - _call_sites())
