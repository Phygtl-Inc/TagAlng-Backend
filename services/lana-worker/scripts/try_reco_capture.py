#!/usr/bin/env python3
"""Drive the typed recommendation capture from the terminal — real LLM, no server, no DB.

    ./scripts/try_reco_capture.py                     # interactive
    ./scripts/try_reco_capture.py "Dr. Sarah is..."   # scripted: one msg per arg

Prints the reco_type the extractor picked and the live carousel after every turn, so you can
see whether a chatty opener actually collapses the steps. The save is stubbed, so nothing is
written and "pass the tip along" just prints the payload that WOULD be posted.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services" / "lana-worker"))

# Secrets come from the same file the local server uses — never hardcoded here.
for line in (ROOT / "deploy" / "lana-worker.env").read_text().splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

import app.tip_share as tip_share  # noqa: E402

SAVED: list[dict] = []
tip_share._save_tip = lambda **kw: (  # noqa: SLF001 — harness stub, no DB round-trip
    SAVED.append(kw["draft"]) or {"signal_id": "sig-local", "matches_created": 0}
)


def show(ctx: dict) -> None:
    draft = ctx.get("tip_draft") or {}
    steps = draft.get("steps") or []
    done = sum(1 for s in steps if s.get("answer"))
    print(f"\n    type={draft.get('reco_type') or '—'}  "
          f"name={draft.get('name') or '—'}  "
          f"steps={done}/{len(steps)}  missing={draft.get('missing') or []}")
    for s in steps:
        mark = "✓" if s.get("answer") else ("!" if s.get("required") else "·")
        print(f"      {mark} {s['label']:<14} {s.get('answer') or s['question']}")
    if draft.get("suggestions"):
        print(f"      chips: {draft['suggestions']}")


def main() -> None:
    ctx: dict = {"zip_code": "32827"}
    history: list[dict] = []
    scripted = sys.argv[1:]
    was_scripted = bool(scripted)
    while True:
        if scripted:
            msg = scripted.pop(0)
            print(f"\n> {msg}")
        elif was_scripted:
            break  # scripted run is done — never fall through to a blocking prompt
        else:
            try:
                msg = input("\n> ").strip()
            except EOFError:
                break
            if msg in {"", "quit", "exit"}:
                break
        history.append({"role": "user", "content": msg})
        reply = tip_share.run_tip_share_turn(
            user_message=msg,
            session_ctx=ctx,
            history=history,
            user_jwt="local-harness",
            home_block_id="local-block",
        )
        history.append({"role": "assistant", "content": reply})
        print(f"\nLana: {reply}")
        show(ctx)
        if SAVED:
            print("\n  WOULD POST:\n" + json.dumps(
                {"reco_type": SAVED[-1].get("reco_type"), "answers": SAVED[-1].get("answers")},
                indent=2, ensure_ascii=False))
            break


if __name__ == "__main__":
    main()
