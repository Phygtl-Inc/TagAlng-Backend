#!/usr/bin/env python3
"""Replay a rapport turn against the real policy and see what Lana would say.

The QA tool for the two failure modes Tommaso reported on 2026-09-15 (prod):

  DRILLING    — several questions in a row chasing detail out of ONE answer
                ("what stands out?" -> "which part?" -> "which of those three?").
  BARE CLOSE  — a warm line with no question, no chip and no offer: a dead end.

It runs the same turn N times (the policy runs at temperature 0.4, so one sample
tells you nothing) and prints what the person would read each time.

    cd services/lana-worker
    set -a && . ../../deploy/lana-worker.env && set +a        # dev
    PYTHONPATH=. python -m scripts.try_rapport_drilling <user-uuid>
    PYTHONPATH=. python -m scripts.try_rapport_drilling <user-uuid> "All of it"
    PYTHONPATH=. python -m scripts.try_rapport_drilling <user-uuid> "gotta go" 4

Reading the output:
  * "ASKS" only means the text contains a "?" — a bridge_offer ("want me to look
    for a spot?") trips it and is the CORRECT answer, so read the line, not the flag.
  * chips=[] with no question IS the bare-close failure.
  * kind=follow_thread at a spent budget is the drilling failure.

Any user id with a few claims works; it needs the worker's env (SUPABASE_URL,
SUPABASE_SERVICE_ROLE_KEY, and an LLM key) because the policy reads world state,
candidate goals and claims for real.
"""
import sys, json, logging
from app.policy.decide import decide_turn, MAX_CONSECUTIVE_ASKS

USER = sys.argv[1]
N = int(sys.argv[3]) if len(sys.argv) > 3 else 6
MSG = sys.argv[2] if len(sys.argv) > 2 else "All of it"

ASKED = [
    "What makes it your favorite when it's on the menu?",
    "What part stands out most to you: the sweet-savory mix, or that little sharp bite?",
    "What part of the mix do you look forward to most: the figs, the gorgonzola, or the onions?",
]

HISTORY = [
    {"role": "user", "content": "Pausa does a fig and gorgonzola pizza with caramelized onions, it's the best"},
    {"role": "assistant", "content": ASKED[0]},
    {"role": "user", "content": "The unique flavors"},
    {"role": "assistant", "content": ASKED[1]},
    {"role": "user", "content": "The mix"},
    {"role": "assistant", "content": ASKED[2]},
]

def run(label, recent_asks):
    ctx = {
        "lang": "en",
        "policy_ask_streak": 3,
        "policy_recent_asks": recent_asks,
        "policy_pending_question": ASKED[2],
        "_discovery_slots": {"linear_intent": "sharing.tip"},
    }
    action = decide_turn(
        user_id=USER, session_ctx=ctx, history=HISTORY,
        user_message=MSG, answering_question=ASKED[2],
    )
    print("=" * 72)
    print(label)
    print("=" * 72)
    if action is None:
        print("  (no action — policy returned None)")
        return
    print(f"  kind      : {action.kind}")
    print(f"  utterance : {action.utterance}")
    print(f"  chips     : {[c.get('label') for c in (action.chips or [])]}")
    print(f"  why       : {action.why[:160]}")
    print(f"  ASKS?     : {'YES — still a question' if '?' in action.utterance else 'no question'}")
    print()

def trials(label, recent, n=4):
    asked = 0
    for i in range(n):
        ctx = {"lang": "en", "policy_ask_streak": 3, "policy_recent_asks": recent,
               "policy_pending_question": ASKED[2],
               "_discovery_slots": {"linear_intent": "sharing.tip"}}
        a = decide_turn(user_id=USER, session_ctx=ctx, history=HISTORY,
                        user_message=MSG, answering_question=ASKED[2])
        u = (a.utterance if a else "")
        q = "?" in u
        asked += q
        chips = [c.get("label") for c in ((a.chips if a else None) or [])]
        print(f"  {i+1}. {'ASKS ' if q else 'gives'} | {(a.kind if a else 'none'):14s} | chips={chips}")
        print(f"     {u[:150]}")
    print(f"  --> asked again {asked}/{n}\n")

print(f"MAX_CONSECUTIVE_ASKS = {MAX_CONSECUTIVE_ASKS}, streak = 3 (over the ceiling)\n")
print(f"message: {MSG!r}\n"); trials("", ASKED, n=N)
