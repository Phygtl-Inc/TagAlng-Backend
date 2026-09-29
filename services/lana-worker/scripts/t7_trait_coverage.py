"""T7 · trait coverage — do recommenders have standing on heritage / language concepts?

The question app/reco_authority.py depends on: when an ask says "recommended by someone
from Spain", how many recommendations have an author who clears MIN_EXPLICIT_SCORE on a
heritage or language concept? Unlike t7_authority_coverage.py this needs no embedding
call (concepts are picked from identity_concepts directly), so it runs even when local
Vertex credentials do not.

Read-only. Touches nothing, writes nothing.

    python -m scripts.t7_trait_coverage

Requires the worker's env: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY.
"""
import os
from collections import Counter

from app.auth import service_client
from app.authority import MIN_EXPLICIT_SCORE, authority_for

sb = service_client()
print("db:", os.environ["SUPABASE_URL"])
concepts = sb.table("identity_concepts").select("id,concept,label,bucket").execute().data or []
print("concepts total:", len(concepts), dict(Counter(c["bucket"] for c in concepts)))
LANG = ("speak", "language", "lingual", "spanish", "urdu", "hindi", "arabic", "french", "italian")
trait = [c for c in concepts if c["bucket"] == "heritage"
         or any(k in (c["label"] + " " + c["concept"]).lower() for k in LANG)]
print("heritage/language concepts:", len(trait))
for c in trait[:40]:
    print("   ", c["bucket"], "|", c["label"])

tips = (sb.table("local_signals").select("id,user_id,created_at")
        .eq("intent", "tip_share").order("created_at", desc=True).limit(1000).execute().data or [])
authors = {}
for t in tips:
    authors.setdefault(str(t["user_id"]), []).append(t)
print(f"\ntip_share recos: {len(tips)}  distinct authors: {len(authors)}")

ids = [c["id"] for c in trait]
by_id = {c["id"]: c for c in trait}
strong = thin = 0
tips_strong = 0
hits = Counter()
for uid, ts in authors.items():
    now = authority_for(uid, ids)  # as of now
    best = max((r["score"] for r in now.values()), default=0.0)
    if best >= MIN_EXPLICIT_SCORE:
        strong += 1
        for cid, r in now.items():
            if r["score"] >= MIN_EXPLICIT_SCORE:
                hits[by_id[cid]["label"]] += 1
    elif best > 0:
        thin += 1
    # per-tip, as of the tip's moment (the rule the ranking would use)
    for t in ts:
        s = authority_for(uid, ids, as_of=str(t["created_at"]))
        if max((r["score"] for r in s.values()), default=0.0) >= MIN_EXPLICIT_SCORE:
            tips_strong += 1
n = max(len(authors), 1)
print(f"authors with strong standing (>= {MIN_EXPLICIT_SCORE}) on any trait: {strong}/{len(authors)} ({100*strong/n:.1f}%)")
print(f"authors with thin standing only: {thin}/{len(authors)} ({100*thin/n:.1f}%)")
print(f"recos backed by strong standing as of when posted: {tips_strong}/{len(tips)} ({100*tips_strong/max(len(tips),1):.1f}%)")
print("top traits:", hits.most_common(10))
