"""
live_policy.py — best-effort adapter from the CURRENT runtime onto the NextAction contract.

Unlike circles_zip/live_impl.py (pure NotImplementedError — that backend truly doesn't exist),
the conversational runtime DOES exist: run_lana_unified_pipeline behind
POST /lana/sessions/{id}/messages. This adapter drives that endpoint and maps its response onto
NextAction, so the tone/lingo/safety axes (which only need the utterance + chip labels) score
REAL Lana output today. Fields the runtime does not emit are set to None/"" and FLAGGED — the
handoff report asks Asjid/Yunchao to add them (a `NextAction` shape, or at least kind/tool/why
in the routing payload) so decision-structure evals become observable.

WHAT LIVE MODE CAN AND CANNOT MEASURE (be honest about this):
  CAN  : lingo (utterance + chips), gender-agreement, role-address, anti-sycophancy, AI-honesty,
         crisis handling, never-dead-end (structural), chip cleanliness. All from real output.
  CANNOT (yet, without instrumentation):
    - `why` — the runtime emits no rationale (schema `why` check is auto-exempted for live).
    - typed chip `action` — ui_actions carry only labels; every chip is mapped to action=CONTINUE
      (# FLAGGED). Chip-escape (NOT_NOW) detection is therefore unreliable in live mode.
    - `kind` — inferred heuristically from routing.outcome/intent_class (# FLAGGED); not authoritative.
    - capability-grounding — the response exposes tool_called (a tool NAME, not necessarily a
      registered capability_id), and this harness cannot set the account's real world-state
      (zip_unlock/role/gender) from here, so scenario.world does NOT reflect the live account.
      Treat live capability-grounding results as indicative only until NextAction + a seedable
      test account exist.

Auth/HTTP idioms mirror simulation.py. Env: LANA_BASE_URL, SUPABASE_URL, SUPABASE_ANON_KEY,
SIM_PASSWORD, and SIM_LIVE_EMAIL (the one provisioned account live mode drives).
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from ports import Chip, NextAction, TurnContext

LANA_BASE_URL = os.environ.get("LANA_BASE_URL", "http://localhost:8000")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
SIM_PASSWORD = os.environ.get("SIM_PASSWORD", "")
SIM_LIVE_EMAIL = os.environ.get("SIM_LIVE_EMAIL", "")


def _kind_from_routing(routing: dict[str, Any], event_draft: Any, has_actions: bool) -> str:
    """# FLAGGED heuristic: the runtime has no `kind`. Infer a plausible NextAction.kind from the
    routing payload so decision-quality axes have something to read. NOT authoritative."""
    outcome = (routing or {}).get("outcome") or ""
    intent = (routing or {}).get("intent_class") or ""
    if outcome in ("decline", "out_of_scope", "clarify_out_of_scope"):
        return "reply"
    if event_draft:
        return "bridge_offer"
    if "clarify" in outcome or intent in ("ambiguous", "clarify"):
        return "ask_gap"
    if (routing or {}).get("tool_called"):
        return "bridge_offer"
    if has_actions:
        return "bridge_offer"
    return "reply"


def map_to_next_action(resp: dict[str, Any]) -> NextAction:
    """Pure mapping (unit-testable without a server): runtime response JSON -> NextAction.
    Every field the runtime doesn't provide is None/""/CONTINUE and flagged in the module docstring."""
    routing = resp.get("routing") or {}
    ui_actions = resp.get("ui_actions") or []
    event_draft = resp.get("event_draft")
    chips = [Chip(label=(a.get("label") or ""), action="CONTINUE") for a in ui_actions]  # # FLAGGED action
    return NextAction(
        kind=_kind_from_routing(routing, event_draft, bool(ui_actions)),  # # FLAGGED
        utterance=resp.get("assistant_message", ""),
        tool=routing.get("tool_called"),  # # FLAGGED: a tool name, not necessarily a capability_id
        tool_args={},
        defer_goal_id=None,
        chips=chips,
        why="",  # # FLAGGED: runtime emits no rationale — schema check auto-exempts live
    )


class LivePolicy:
    """PolicyPort adapter onto POST /lana/sessions/{id}/messages."""

    def __init__(self) -> None:
        missing = [k for k, v in {
            "SUPABASE_URL": SUPABASE_URL, "SUPABASE_ANON_KEY": SUPABASE_ANON_KEY,
            "SIM_PASSWORD": SIM_PASSWORD, "SIM_LIVE_EMAIL": SIM_LIVE_EMAIL,
        }.items() if not v]
        if missing:
            raise RuntimeError(
                "LivePolicy needs a running Lana + a provisioned account. Missing env: "
                + ", ".join(missing)
                + ". (Use SIM_BACKEND=stub or run_eval.py --dry-run to run without a live backend.)"
            )
        self._jwt = self._authenticate()

    def _authenticate(self) -> str:
        with httpx.Client(timeout=60) as http:
            resp = http.post(
                f"{SUPABASE_URL}/auth/v1/token?grant_type=password",
                headers={"apikey": SUPABASE_ANON_KEY},
                json={"email": SIM_LIVE_EMAIL, "password": SIM_PASSWORD},
            )
            resp.raise_for_status()
            return resp.json()["access_token"]

    def decide_turn(self, ctx: TurnContext) -> NextAction:
        # NOTE: scenario.world is NOT pushed to the backend (no seed path from here) — see docstring.
        headers = {"Authorization": f"Bearer {self._jwt}"}
        with httpx.Client(timeout=120) as http:
            sess = http.post(f"{LANA_BASE_URL}/lana/sessions",
                             json={"purpose": "lana", "force_new": True}, headers=headers)
            sess.raise_for_status()
            session_id = sess.json()["session_id"]

            # Replay prior USER turns to rebuild context server-side (assistant turns are the
            # server's own; it regenerates them). Then send the turn under test.
            last: dict[str, Any] = {}
            for t in ctx.recent:
                if t.get("role") == "user":
                    r = http.post(f"{LANA_BASE_URL}/lana/sessions/{session_id}/messages",
                                  json={"message": t["content"]}, headers=headers)
                    r.raise_for_status()
            r = http.post(f"{LANA_BASE_URL}/lana/sessions/{session_id}/messages",
                          json={"message": ctx.user_text}, headers=headers)
            r.raise_for_status()
            last = r.json()

            http.post(f"{LANA_BASE_URL}/lana/sessions/{session_id}/complete",
                      json={"force": True, "publish": False}, headers=headers)

        return map_to_next_action(last)
