"""
n2_extract_baseline.py — how good and how STABLE is the extractor today? (D1 / D2 baseline)

    cd services/lana-worker/simulations/subject_eval
    python n2_extract_baseline.py --env ../../../../.env.local.dev-backup --messages 40 --trials 3

Calls `app.latent_extract.extract_entities_from_message` — the PURE extraction function. It does
not write: `run_latent_intent` is the one that persists, and it is deliberately not used here.
The message corpus is read-only from dev, test accounts excluded.

WHY REPRODUCIBILITY BEFORE ACCURACY
-----------------------------------
D1 gates predicate-level F1 at >= 0.70 and D2 gates certainty accuracy at >= 0.85. Both need
hand labels — ~10 hours for F-EXTRACT's 200 messages.

Before spending that, one label-free question decides how the gate has to be shaped: **is the
extractor deterministic?** If the same message yields a different entity set on a re-run, then a
single-pass F1 measures sampling as much as quality, and the gate needs trials and an interval —
exactly what the recommendation eval found when a 43% rate came back 37% on an identical re-run.

WHAT IT REPORTS
  yield           entities per message, and how often the product's own gate skips a message
  set_stability   share of messages whose ENTITY SET is identical across trials
  text_jaccard    mean overlap of entity strings across trials
  type_stability  share where every repeated entity kept the same `type`
  subject_stability  ...and the same `subject` (self/child/partner), which D2's certainty axis
                  is adjacent to

None of this is quality. It is the denominator quality has to be measured against.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path

import httpx
from dotenv import dotenv_values, load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parents[1]))          # services/lana-worker -> app.*
from run_eval import wilson_ci  # noqa: E402


def _fetch_messages(url: str, key: str, want: int) -> list[str]:
    h = {"apikey": key, "Authorization": f"Bearer {key}"}
    with httpx.Client(timeout=90) as c:
        test_ids = {u["id"] for u in c.get(
            f"{url}/rest/v1/users?select=id&email=like.*phygtl*", headers=h).json()}
        sess: list[dict] = []
        while True:
            hh = dict(h); hh["Range"] = f"{len(sess)}-{len(sess)+999}"
            got = c.get(f"{url}/rest/v1/lana_sessions?select=id,user_id", headers=hh).json()
            sess.extend(got)
            if len(got) < 1000:
                break
        keep = [s["id"] for s in sess if s["user_id"] not in test_ids]
        print(f"  sessions: {len(sess)} total, {len(keep)} from non-test users")
        out: list[str] = []
        # Page through the non-test sessions in chunks; stop once we have plenty of candidates.
        for i in range(0, len(keep), 40):
            ids = ",".join(keep[i:i + 40])
            rows = c.get(f"{url}/rest/v1/lana_messages"
                         f"?select=content&role=eq.user&session_id=in.({ids})&limit=400",
                         headers=h).json()
            out.extend(r["content"] for r in rows if (r.get("content") or "").strip())
            if len(out) >= want * 12:
                break
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="D1/D2 extractor baseline (read-only)")
    ap.add_argument("--env", default=str(_HERE.parents[3] / ".env.local"))
    ap.add_argument("--messages", type=int, default=40)
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--out", default=str(_HERE / "out" / "n2_extract_baseline.md"))
    args = ap.parse_args()

    cfg = dotenv_values(args.env)
    url, key = cfg.get("SUPABASE_URL") or "", cfg.get("SUPABASE_SERVICE_ROLE_KEY") or ""
    if not url or not key:
        print(f"no credentials in {args.env}")
        return 2
    # The extractor needs the LLM config; the repo-root .env.local carries it.
    # override=True, deliberately. A stale `OPENAI_API_KEY` exported in the shell will
    # otherwise shadow the repo key silently — which is exactly what happened on
    # 2026-09-15/16: the ambient key was exhausted while the file key had credits, so
    # every call 429d and the run looked like an extractor problem. The rest of the
    # suite already uses override=True for this reason (policy_eval/run_eval.py).
    load_dotenv(_HERE.parents[3] / ".env.local", override=True)
    os.environ.setdefault("LANA_LLM_FALLBACK", "0")   # never let a retry cross providers

    from app.latent_extract import extract_entities_from_message, should_extract_entities
    from app.orchestrator.llm import llm_configured, synthesizer_model

    if not llm_configured():
        print("no LLM configured — set OPENAI_API_KEY")
        return 2
    print(f"[N2] corpus {url} (read-only) · extractor model {synthesizer_model()}")

    pool = _fetch_messages(url, key, args.messages)
    gated = [m for m in pool if should_extract_entities(m)]
    print(f"  user messages fetched: {len(pool)} · pass the product's own gate: {len(gated)} "
          f"({len(gated)/max(1,len(pool)):.0%})")
    msgs = gated[: args.messages]
    if len(msgs) < 5:
        print("  too few extractable messages")
        return 2

    rows = []
    for i, m in enumerate(msgs, 1):
        trials = []
        for _ in range(args.trials):
            try:
                ents = extract_entities_from_message(m)
            except Exception as e:  # noqa: BLE001
                ents = []
                print(f"    [error] {e}")
            trials.append(ents)
        rows.append((m, trials))
        if i % 10 == 0:
            print(f"    {i}/{len(msgs)}")

    # --- stability ---------------------------------------------------------------------
    def texts(ents) -> frozenset[str]:
        return frozenset((e.text or "").strip().lower() for e in ents if (e.text or "").strip())

    identical = jac_sum = type_ok = subj_ok = 0
    counts: list[int] = []
    empties = 0
    for _, trials in rows:
        sets = [texts(t) for t in trials]
        counts.extend(len(t) for t in trials)
        empties += sum(1 for t in sets if not t)
        if all(s == sets[0] for s in sets):
            identical += 1
        union = set().union(*sets) or {""}
        inter = set(sets[0]).intersection(*sets[1:]) if len(sets) > 1 else set(sets[0])
        jac_sum += len(inter) / len(union)
        # type/subject agreement on entities that appear in every trial
        shared = inter
        tmap: dict[str, set[str]] = {}
        smap: dict[str, set[str]] = {}
        for t in trials:
            for e in t:
                k = (e.text or "").strip().lower()
                if k in shared:
                    tmap.setdefault(k, set()).add(e.type)
                    smap.setdefault(k, set()).add(getattr(e, "subject", "unknown"))
        type_ok += all(len(v) == 1 for v in tmap.values()) if tmap else 1
        subj_ok += all(len(v) == 1 for v in smap.values()) if smap else 1

    n = len(rows)

    # AN ALL-EMPTY RUN IS AN ERROR, NOT A PERFECT SCORE.
    #
    # Learned the hard way on 2026-09-15: the OpenAI account ran out of credits, `llm_json`
    # raised 429, `extract_entities_from_message` swallowed it, fell through to Vertex, found no
    # Google credentials, swallowed that too, and returned []. Ninety failed calls.
    #
    # This function then reported **100% set stability, 100% type stability, 100% subject
    # stability** — because every trial agreed perfectly about nothing. That is the exact failure
    # this suite keeps finding in other people's systems, in my own tool, on its first real run.
    #
    # Stability over an empty result set is undefined, not perfect. Fail closed.
    if empties == len(counts):
        print(f"\n  INVALID RUN — every one of {len(counts)} calls returned zero entities.")
        print("  Stability over an empty set is undefined, not 100%. Common causes:")
        print("    * the LLM account is out of credits or the key is dead (429 / 401)")
        print("    * `extract_entities_from_message` swallows BOTH its OpenAI and its Vertex")
        print("      failure and returns [] — a dead key is indistinguishable from 'nothing found'")
        print("  Re-run once the model is reachable. Nothing is written.")
        return 3
    if empties > len(counts) * 0.5:
        print(f"\n  [!] {empties}/{len(counts)} calls returned nothing — the stability numbers "
              f"below are dominated by empty results and should not be quoted.")

    lo, hi = wilson_ci(identical, n)
    mean_y = sum(counts) / max(1, len(counts))
    # WHICH KEY, not just which model. This harness is the one that reported 100% stability on
    # ninety failed calls because a dead shadowing key made every extraction return []. Naming
    # the credential in the report is how a reader tells "stable" from "stably broken".
    sys.path.insert(0, str(_HERE.parents[0]))   # simulations/ -> provenance
    import provenance as _prov  # noqa: PLC0415

    L = ["# N2 — extractor baseline (D1 / D2)", "",
         f"- corpus: `{url}` (read-only, test accounts excluded) · model `{synthesizer_model()}` "
         f"· key `{_prov.key_fingerprint()}` · `LANA_LLM_FALLBACK=0`",
         f"- {n} messages x {args.trials} trials · product's own `should_extract_entities` gate "
         f"applied first", "",
         "## Stability — the denominator quality has to be measured against", "",
         f"**{identical}/{n} ({identical/n:.0%}, 95% CI {lo:.0%}-{hi:.0%}) of messages produced "
         f"an IDENTICAL entity set across {args.trials} identical calls.**", "",
         "| metric | value |", "|---|---|",
         f"| identical entity set across trials | {identical/n:.0%} |",
         f"| mean entity overlap (Jaccard) | {jac_sum/n:.2f} |",
         f"| stable `type` on shared entities | {type_ok/n:.0%} |",
         f"| stable `subject` on shared entities | {subj_ok/n:.0%} |",
         f"| mean entities per message | {mean_y:.2f} |",
         f"| trials returning nothing | {empties}/{len(counts)} ({empties/max(1,len(counts)):.0%}) |",
         "", "## What this means for D1 and D2", "",
         "D1 (`F1 >= 0.70`) and D2 (`certainty >= 0.85`) are single-number gates. If the entity "
         "set moves between identical calls, a one-pass F1 measures sampling as much as quality "
         "— so the gate needs **trials and an interval**, and the threshold has to be set from a "
         "baseline rather than assumed. Same correction the recommendation eval needed after a "
         "43% rate came back 37% on a re-run.", ""]
    dist = Counter(counts)
    L += ["## Entities per call", "", "| entities | calls |", "|---|---|"]
    L += [f"| {k} | {v} |" for k, v in sorted(dist.items())]
    L.append("")

    out = Path(args.out); out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L), encoding="utf-8")
    print(f"\n  identical set across trials  {identical}/{n} = {identical/n:.0%}  (CI {lo:.0%}-{hi:.0%})")
    print(f"  mean entity overlap          {jac_sum/n:.2f}")
    print(f"  stable type / subject        {type_ok/n:.0%} / {subj_ok/n:.0%}")
    print(f"  mean entities per message    {mean_y:.2f}   (empty calls {empties}/{len(counts)})")
    print(f"  report -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
