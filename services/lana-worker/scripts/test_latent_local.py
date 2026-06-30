#!/usr/bin/env python3
"""Local smoke test for Layer 3 latent intent.

Calls run_latent_intent directly (no HTTP/auth) on a sample message and prints what it
wrote. Grab a real user_id + session_id from the lana_sessions table (FK constraints
require them to exist).

Usage (from services/lana-worker, env sourced):
    python -m scripts.test_latent_local --user <user_id> --session <session_id>
    python -m scripts.test_latent_local --user <id> --session <id> --message "my kid does karate"
"""

from __future__ import annotations

import argparse


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--user", required=True)
    p.add_argument("--session", required=True)
    p.add_argument("--message", default="my kid just started karate on saturdays and loves it")
    args = p.parse_args()

    from app.latent_extract import run_latent_intent

    print(f"message: {args.message!r}\n")
    result = run_latent_intent(
        user_id=args.user,
        session_id=args.session,
        turn_id=None,
        block_id=None,
        message=args.message,
    )
    print(f"result: {result}")
    print(
        "\nNow check in Supabase:\n"
        f"  latent_signals   where user_id = '{args.user}'\n"
        f"  suggestion_queue where user_id = '{args.user}'"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
