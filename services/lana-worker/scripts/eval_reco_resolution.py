#!/usr/bin/env python3
"""Does the resolver put the right recommendations together? — end-to-end, real decisions.

Every other test of `app/reco_subject.py` either mocks the decision or hands it the answer:
the unit tests patch `call_rpc`, and the SQL end-to-end passes `set_signal_subject` a
hardcoded place id, so two tips merged because the fixture said so. Neither exercises
`ground_reco_subject` — the tapped/chip/search cascade, the 0.82 Google floor, the
candidate scoring, the adjudicator. That function IS the model, and this is what runs it.

WHAT IT MEASURES

Cases are grouped by `truth`: two cases sharing a truth label are the same real-world
thing and MUST land on one subject; two cases with different labels MUST NOT. That makes
both errors visible, and they are not equally bad:

    a missed merge   two cards where one would do. Visible, recoverable.
    a false merge    invents corroboration, silently. The count a stranger trusts is wrong.

So precision on merges is reported separately from recall, and a false merge is the
headline. Reaching for recall by lowering a floor is how the count becomes a lie.

HONEST LIMITS

- Names here are real Lake Nona businesses, but the *phrasings* are written by us. Real
  users are messier. This measures the mechanism and the hard negatives; it does not
  measure the true base rate (see docs/LANA_SUBJECT_GRAPH_RECONCILIATION.md §5).
- It needs a LOCAL stack. It writes signals and subjects, so it refuses anything else.

USAGE (local stack up, see simulations/LOCAL_STACK.md)

    python -m scripts.eval_reco_resolution
    python -m scripts.eval_reco_resolution --keep      # leave rows for inspection
"""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import sys
import time
import uuid
from typing import Any

# (id, truth, reco_type, name, category, description, place_based) — `truth` is ground
# truth by construction: same label = same real-world thing. `place_based` mirrors what the
# extractor decides during capture, and it is not decoration: it is what says a clinic has a
# storefront to ground against and a plumber does not.
CASES: list[tuple[str, str, str, str, str, str, bool]] = [
    # ---- Google-groundable, SAME place named two ways --------------------------------
    ("g1", "nona-kids", "professional", "Nona Kids Dentists",
     "pediatric dentist", "so gentle with the toddlers", True),
    ("g2", "nona-kids", "professional", "Nona Kids' Dentists & Orthodontics",
     "pediatric dentist", "they show the tools first, no tears", True),
    # ---- A DIFFERENT real dentist. Must not join the two above ------------------------
    ("g3", "celebration", "professional", "Celebration Pediatric Dentistry",
     "pediatric dentist", "worth the drive, lovely staff", True),
    # ---- Shares the word "Pediatric" with g3 and is a different practice --------------
    ("g4", "pdg", "professional", "Pediatric Dental Group",
     "pediatric dentist", "quick appointments", True),
    # ---- No listing: the Stage 2 identity path decides ---------------------------------
    ("i1", "mike-plumb", "service", "Mike the Plumber",
     "plumber", "fixed our water heater same day", False),
    ("i2", "mike-plumb", "service", "Mike the Plumber",
     "plumber", "came out on a Sunday", False),
    # ---- THE ADVERSARIAL ONE: identical name, different trade. Must NOT merge ----------
    ("i3", "mike-barber", "service", "Mike the Barber",
     "barber", "best fade on the block", False),
    # ---- THE ADJUDICATION BAND. Without these, every merge is auto and the model that
    # ---- decides the hard middle is never exercised (the first run of this harness
    # ---- scored 100% without once calling it).
    # "Mike Plumber" vs "Mike the Plumber" = 0.857 -> band. Same plumber, should merge.
    ("i4", "mike-plumb", "service", "Mike Plumber",
     "plumber", "reasonable and he turns up", False),
    # Identical NAME, different trade: scores 1.00, capped to 0.92 by the category
    # mismatch -> band -> the model must refuse. This is the case the cap exists for.
    ("i5", "mike-plumb-name-clash", "service", "Mike the Plumber",
     "barber", "sharp fades, walk-ins fine", False),
    # ---- Collections share a subject but never blend -----------------------------------
    ("c1", "banana-bread", "recipe", "Banana bread",
     "baking", "brown the butter first", False),
    ("c2", "banana-bread", "recipe", "Banana bread",
     "baking", "I use three very ripe bananas and no sugar", False),
]

BLOCK = "8a2a1072b59ffff"
ZIP = "32827"


def _load_env() -> str:
    """Local stack for the DB, dev worker env for the model + Maps keys. Refuses non-local."""
    root = pathlib.Path(__file__).resolve().parents[3]
    st = dict(re.findall(r'^([A-Z_]+)="?([^"\n]*)"?$',
                         (root / "supabase" / ".temp" / "status.env").read_text(), re.M)) \
        if (root / "supabase" / ".temp" / "status.env").exists() else {}
    local = dict(re.findall(r'^([A-Z0-9_]+)=(.*)$', (root / ".env.local").read_text(), re.M))
    dev = dict(re.findall(r'^([A-Z0-9_]+)=(.*)$',
                          (root / "deploy" / "lana-worker.env").read_text(), re.M))

    url = (st.get("API_URL") or local.get("SUPABASE_URL", "")).strip().strip('"')
    if "127.0.0.1" not in url and "localhost" not in url:
        sys.exit(f"REFUSING: SUPABASE_URL is {url!r}, not a local stack. This script writes.")
    os.environ["SUPABASE_URL"] = url
    for k in ("SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_ANON_KEY"):
        os.environ[k] = (st.get(k.replace("SUPABASE_", "")) or local.get(k, "")).strip().strip('"')
    # Model + Maps come from the dev worker env; neither touches a database.
    for k in ("OPENAI_API_KEY", "LANA_LLM_PROVIDER", "OPENAI_SYNTH_MODEL",
              "OPENAI_ROUTER_MODEL", "GOOGLE_MAPS_API_KEY"):
        if dev.get(k):
            os.environ[k] = dev[k].strip().strip('"')
    return url


def _jwt_for(user_id: str) -> str:
    import jwt as pyjwt

    root = pathlib.Path(__file__).resolve().parents[3]
    secret = ""
    tmp = root / "supabase" / ".temp" / "status.env"
    if tmp.exists():
        m = re.search(r'^JWT_SECRET="?([^"\n]*)"?$', tmp.read_text(), re.M)
        secret = m.group(1) if m else ""
    secret = secret or os.environ.get("SUPABASE_JWT_SECRET", "")
    if not secret:
        sys.exit("No local JWT secret found — run `supabase status -o env > supabase/.temp/status.env`")
    return pyjwt.encode(
        {"sub": user_id, "role": "authenticated", "aud": "authenticated",
         "exp": int(time.time()) + 3600},
        secret, algorithm="HS256",
    )


def _users(sb: Any, n: int) -> list[str]:
    """One author per case, cycling if the stack has fewer users than cases.

    Reuse is realistic — a neighbour may recommend more than one thing — and authorship
    is irrelevant to what this measures, which is whether two recommendations land on one
    subject. Only distinctness of the SIGNALS matters.
    """
    rows = (sb.table("users").select("id").limit(50).execute().data or [])
    if not rows:
        sys.exit("No users on the local stack — seed it first (supabase/seed_sim_accounts.sql).")
    return [rows[i % len(rows)]["id"] for i in range(n)]


def _trials(args: Any) -> int:
    """Run the suite N times and report, per truth-pair, how often it merged.

    A pair that merges 5/5 is stable. A pair that merges 3/5 is the honest picture of a
    judgement call sitting on the model's decision boundary, and averaging it away into a
    single precision number would hide precisely what a reader needs to know.
    """
    from collections import Counter

    merged: Counter = Counter()
    seen: Counter = Counter()
    fps = 0
    for t in range(args.trials):
        print(f"\n─── trial {t + 1}/{args.trials} " + "─" * 40)
        res = _one_run(keep=False, quiet=True)
        for a, b, same_truth, did_merge in res["pairs"]:
            key = (a, b)
            seen[key] += 1
            if did_merge:
                merged[key] += 1
            if did_merge and not same_truth:
                fps += 1
        print(f"  precision {res['precision']:.0%}  recall {res['recall']:.0%}")

    print("\n" + "=" * 62)
    print(f"  {args.trials} trials · false merges across all trials: {fps}")
    unstable = [(k, merged[k], seen[k]) for k in seen
                if 0 < merged[k] < seen[k]]
    if unstable:
        print("\n  UNSTABLE pairs — the same input decided differently between runs:")
        for (a, b), m, n in sorted(unstable):
            print(f"    {a}+{b}  merged {m}/{n}")
    else:
        print("  every pair decided the same way in every trial.")
    return 1 if fps else 0


def _one_run(*, keep: bool, quiet: bool = False) -> dict[str, Any]:
    """One full pass. Returns {pairs, precision, recall, false_merges}.

    `pairs` is [(case_a, case_b, same_truth, merged)] so a trials run can ask how often a
    given pair decided the same way, which is the only honest question for the adjudicated
    band.
    """
    from app.db import service_client
    from app.reco_subject import ground_reco_subject, normalize_subject_name

    sb = service_client()
    users = _users(sb, len(CASES))
    seeded: list[str] = []
    results: list[dict[str, Any]] = []

    # PRE-FLIGHT RESET. Subjects outlive signals (a subject is the thing many tips point
    # at), so a previous run's subjects sit in the table as candidates for this one — which
    # is how a case that correctly created its own subject came back the next run as
    # "blocked 1.00" against a ghost. Scoped to this harness's own names, never a global wipe.
    names = sorted({c[3] for c in CASES})
    keys = sorted({normalize_subject_name(n) for n in names})
    for row in (sb.table("local_signals").select("id").in_("reco_name", names)
                .execute().data or []):
        sb.table("local_signals").delete().eq("id", row["id"]).execute()
    for row in (sb.table("reco_subjects").select("id").in_("subject_key", keys)
                .execute().data or []):
        try:
            sb.table("reco_subjects").delete().eq("id", row["id"]).execute()
        except Exception:  # noqa: BLE001 — still referenced by a signal we do not own
            pass

    for (cid, truth, rtype, name, category, desc, place_based), user_id in zip(CASES, users):
        sig = str(uuid.uuid4())
        sb.table("local_signals").insert({
            "id": sig, "user_id": user_id, "intent": "tip_share", "status": "listening",
            "block_id": BLOCK, "detail_text": f"{name} · {category} · {desc}",
            "reco_name": name, "reco_description": desc, "reco_type": rtype,
            "category": category,
        }).execute()
        seeded.append(sig)
        subject = ground_reco_subject(
            _jwt_for(user_id), signal_id=sig,
            draft={"name": name, "reco_type": rtype, "category": category,
                   "locality": "Lake Nona", "place_based": place_based},
            zip_code=ZIP, block_id=BLOCK, user_id=user_id,
        )
        row = (sb.table("local_signals").select("subject_method, subject_confidence")
               .eq("id", sig).single().execute().data or {})
        results.append({"id": cid, "truth": truth, "name": name, "subject": subject,
                        "method": row.get("subject_method"),
                        "conf": row.get("subject_confidence")})
        if not quiet:
            conf = f" {row['subject_confidence']:.2f}" if row.get("subject_confidence") else ""
            print(f"  {cid:3s} {name[:38]:40s} -> "
                  f"{str(subject)[:8] if subject else 'NONE':8s} "
                  f"[{row.get('subject_method')}{conf}]")

    tp = fp = tn = fn = 0
    pairs: list[tuple[str, str, bool, bool]] = []
    errors: list[str] = []
    for i, a in enumerate(results):
        for b in results[i + 1:]:
            same_truth = a["truth"] == b["truth"]
            merged = bool(a["subject"]) and a["subject"] == b["subject"]
            pairs.append((a["id"], b["id"], same_truth, merged))
            if same_truth and merged:
                tp += 1
            elif same_truth and not merged:
                fn += 1
                errors.append(f"MISSED  {a['id']}+{b['id']}  "
                              f"{a['name'][:28]!r} / {b['name'][:28]!r}")
            elif not same_truth and merged:
                fp += 1
                errors.append(f"FALSE   {a['id']}+{b['id']}  "
                              f"{a['name'][:28]!r} / {b['name'][:28]!r}")
            else:
                tn += 1

    if not keep:
        subjects = {r["subject"] for r in results if r["subject"]}
        for sig in seeded:
            sb.table("local_signals").delete().eq("id", sig).execute()
        for sid in subjects:
            try:
                sb.table("reco_subjects").delete().eq("id", sid).execute()
            except Exception:  # noqa: BLE001
                pass

    return {
        "pairs": pairs, "errors": errors, "false_merges": fp, "results": results,
        "precision": tp / (tp + fp) if (tp + fp) else 1.0,
        "recall": tp / (tp + fn) if (tp + fn) else 1.0,
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--keep", action="store_true", help="leave the seeded rows behind")
    ap.add_argument("--trials", type=int, default=1,
                    help="repeat the whole run N times and report merge RATES. The "
                         "adjudicated band is nondeterministic even at temperature 0 — the "
                         "same pair merged on one run and was refused on the next — so a "
                         "single run is an anecdote, not a measurement.")
    args = ap.parse_args()
    _load_env()
    if args.trials > 1:
        return _trials(args)

    print(f"{len(CASES)} cases · REAL ground_reco_subject\n")
    r = _one_run(keep=args.keep)
    print(f"\n  merges: {r['tp']} correct, {r['fp']} FALSE · missed {r['fn']} · "
          f"correct separations {r['tn']}")
    print(f"  precision {r['precision']:.0%}   recall {r['recall']:.0%}")
    for e in r["errors"]:
        print(f"    {e}")
    print("\n  A FALSE merge is the failure that matters — it invents corroboration."
          if r["fp"] else "\n  No false merges.")
    return 1 if r["fp"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
