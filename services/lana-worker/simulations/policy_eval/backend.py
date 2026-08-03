"""
backend.py — the SIM_BACKEND=stub|live|dry switch (mirrors circles_zip/backend.py).

run_eval.py never imports stub_policy/live_policy directly — only through get_backend(). Swap
targets by env or by run_eval's --backend flag:
  stub  -> StubPolicy      (reference policy, needs OPENAI_API_KEY)
  live  -> LivePolicy      (adapter onto real POST /lana/sessions/.../messages)
  dry   -> DryPolicy       (deterministic canned actions; no API key, no server — smoke test)
"""

from __future__ import annotations

import os

from ports import Chip, NextAction, PolicyPort, TurnContext
from world_state import available_capabilities


class DryPolicy:
    """A deterministic, safe-by-construction PolicyPort for `--dry-run`. It emits well-formed,
    lingo-clean, capability-grounded actions so the WHOLE pipeline (mechanical checks + report)
    can be exercised with no OpenAI key and no live backend. It is NOT a quality reference — it
    only proves the plumbing. (Deliberately English-only text so it never trips the ES/PT
    gendered-token check regardless of scenario locale.)"""

    def decide_turn(self, ctx: TurnContext) -> NextAction:
        text = ctx.user_text.lower()
        avail = available_capabilities(ctx.world)
        recent_has_task = any(t.get("role") == "assistant" for t in ctx.recent)
        deferrable = next((g for g in ctx.goals if g.kind == "ungrounded_circle"), None)

        # mid-task interruption -> defer
        if recent_has_task and deferrable:
            return NextAction(
                kind="capture_defer",
                utterance="Got it — noted for later. Let's keep going: what time works?",
                defer_goal_id=deferrable.id,
                chips=[Chip(label="Weekday mornings", action="CONTINUE"),
                       Chip(label="Maybe later", action="NOT_NOW")],
                why="user is mid event-setup; capture the aside without derailing",
            )
        # low-signal close
        if text.strip() in {"ok thanks", "ok", "thanks", "thank you"}:
            return NextAction(kind="reply",
                              utterance="Anytime — I'm here whenever you want to find your people.",
                              why="low signal; warm close, no forced question")
        # crisis / distress -> care + handoff
        if any(k in text for k in ("don't see the point", "really low", "hopeless")):
            return NextAction(
                kind="handoff",
                utterance="I'm really glad you told me. That sounds heavy — you don't have to carry "
                          "it alone. Would it help if I shared a way to reach someone who can talk it "
                          "through with you right now?",
                why="distress signal; care + resource, drop the task",
            )
        # place mention -> ground
        if "gym" in text or "go to" in text:
            return NextAction(
                kind="ground_place",
                utterance="Nice — which spot is it? I'll keep an ear out for people from there.",
                chips=[Chip(label="Tell you the name", action="GROUND_PLACE"),
                       Chip(label="Maybe later", action="NOT_NOW")],
                why="natural pause after a place mention; ground it",
            )
        # wants to meet -> grounded offer (discovery if available, else seed via host)
        if "meet" in text or "who's around" in text or "people who" in text:
            if "discovery.find_peers" in avail:
                return NextAction(
                    kind="bridge_offer", tool="discovery.find_peers",
                    utterance="There are a couple of people near you I think you'd click with — want an intro?",
                    chips=[Chip(label="Yes, introduce me", action="ACCEPT_INTRO"),
                           Chip(label="Not right now", action="NOT_NOW")],
                    why="area is open; offering an intro is the right next step")
            return NextAction(
                kind="bridge_offer", tool="sharing.host" if "sharing.host" in avail else None,
                utterance="Your area's just getting started — but you don't have to wait. Want to set "
                          "something up and bring your people in?",
                chips=[Chip(label="Set something up", action="CREATE_GATHERING"),
                       Chip(label="Maybe later", action="NOT_NOW")],
                why="area quiet; seed instead of offering discovery")
        # default: warm acknowledge + one offer to create (always-on)
        return NextAction(
            kind="bridge_offer", tool="sharing.host" if "sharing.host" in avail else None,
            utterance="Love that — want me to help set something up around it near you?",
            chips=[Chip(label="Set something up", action="CREATE_GATHERING"),
                   Chip(label="Maybe later", action="NOT_NOW")],
            why="acknowledge the interest and offer the one best next step")


def get_backend(kind: str | None = None) -> PolicyPort:
    kind = (kind or os.environ.get("SIM_BACKEND", "stub")).strip().lower()
    if kind == "dry":
        return DryPolicy()
    if kind == "stub":
        from stub_policy import StubPolicy
        return StubPolicy()
    if kind == "live":
        from live_policy import LivePolicy
        return LivePolicy()
    raise ValueError(f"Unknown backend={kind!r}, expected 'stub', 'live', or 'dry'")
