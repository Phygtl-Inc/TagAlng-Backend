"""mem0 connectivity smoke-test — is the cloud actually reachable with our key?

Run:  ./.venv/bin/python scripts/mem0_smoketest.py

Reads MEM0_API_KEY from the process env or the repo-root .env.local (so the key never
has to be pasted anywhere). Does a real add -> search round-trip against a throwaway
user id, prints exactly what worked, then cleans up after itself. No product data touched.
"""

from __future__ import annotations

import os
import pathlib
import sys
import time

TEST_USER = "mem0-connectivity-smoketest"


def _load_env_local() -> None:
    """Best-effort: pull the repo-root .env.local into os.environ if MEM0_API_KEY isn't set."""
    if os.environ.get("MEM0_API_KEY", "").strip():
        return
    # services/lana-worker/scripts/ -> repo root is three parents up.
    root = pathlib.Path(__file__).resolve().parents[3]
    env_path = root / ".env.local"
    if not env_path.exists():
        return
    try:
        from dotenv import dotenv_values

        for k, v in dotenv_values(env_path).items():
            if v is not None and k not in os.environ:
                os.environ[k] = v
    except Exception:
        pass


def main() -> int:
    _load_env_local()

    print("mem0 connectivity smoke-test")
    print("=" * 44)

    # 1. package
    try:
        import mem0
        from mem0 import MemoryClient

        print(f"[ok]  mem0ai installed          v{getattr(mem0, '__version__', '?')}")
    except Exception as exc:
        print(f"[FAIL] mem0ai not importable: {exc}")
        print("       -> ./.venv/bin/python -m pip install 'mem0ai>=0.1.0,<1'")
        return 1

    # 2. key
    key = os.environ.get("MEM0_API_KEY", "").strip()
    if not key:
        print("[FAIL] MEM0_API_KEY not set (env or .env.local)")
        print("       -> add MEM0_API_KEY=<your key> to the repo-root .env.local, then re-run")
        return 1
    print(f"[ok]  MEM0_API_KEY present        (len={len(key)}, …{key[-4:]})")

    # 3. construct client
    try:
        client = MemoryClient(api_key=key)
        print("[ok]  MemoryClient constructed")
    except Exception as exc:
        print(f"[FAIL] MemoryClient construction failed: {exc}")
        print("       -> bad key, or no network egress to mem0 cloud")
        return 1

    # 4. add (write round-trip)
    messages = [
        {"role": "user", "content": "Connectivity check: I prefer aisle seats and morning meetings."},
        {"role": "assistant", "content": "Noted your seat and meeting-time preferences."},
    ]
    try:
        add_res = client.add(messages, user_id=TEST_USER)
        print(f"[ok]  add() accepted             -> {str(add_res)[:120]}")
    except Exception as exc:
        print(f"[FAIL] add() failed: {exc}")
        return 1

    # mem0 extracts memories server-side; give it a beat before searching.
    time.sleep(2.0)

    # 5. search (read round-trip) — confirm which SDK signature this version accepts,
    #    so it matches the defensive fallback in app/mem0_shadow.py:_do_search.
    sig = None
    results = None
    try:
        results = client.search("what seat does the user prefer?", user_id=TEST_USER, limit=5)
        sig = "search(query, user_id=..., limit=...)"
    except TypeError:
        try:
            results = client.search(
                "what seat does the user prefer?", filters={"user_id": TEST_USER}, top_k=5
            )
            sig = "search(query, filters={user_id}, top_k=...)"
        except Exception as exc:
            print(f"[FAIL] search() failed on both signatures: {exc}")
            return 1
    except Exception as exc:
        print(f"[FAIL] search() failed: {exc}")
        return 1

    if isinstance(results, dict):
        results = results.get("results", [])
    n = len(results or [])
    print(f"[ok]  search() returned {n} mem<->  via {sig}")
    for r in (results or [])[:5]:
        if isinstance(r, dict):
            mem = r.get("memory") or r.get("content")
            print(f"        · {mem}  (score={r.get('score')})")

    # 6. cleanup — don't leave the test user lingering in the account
    try:
        client.delete_all(user_id=TEST_USER)
        print("[ok]  cleanup: delete_all(test user)")
    except Exception as exc:
        print(f"[warn] cleanup skipped ({exc}); test user '{TEST_USER}' may persist")

    print("=" * 44)
    if n > 0:
        print("RESULT: CONNECTED ✅  add + search round-trip succeeded.")
    else:
        print(
            "RESULT: CONNECTED (write ok) ⚠️  search returned 0 — likely extraction lag, "
            "not a connection problem. Re-run, or increase the sleep."
        )
    print(f"NOTE: mem0_shadow.py should use this signature: {sig}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
