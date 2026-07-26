"""End-to-end QA of the circles grounding-on-rapport-tile flow against DEV.

Seeds a suggested ungrounded affiliation for a sim user, then walks the real
funnel with the real modules (Google Places, OpenAI, dev Supabase):
  capture -> ensure_grounding_gaps -> next_ask (tile payload with chips)
  -> free-text answer -> confirm chip -> grounded affiliation + enrichment gap
  -> cadence check. Cleans up every row it created.

Run from services/lana-worker:  ./.venv/bin/python <this file> ../../.env.local
"""

import json
import os
import sys

env_path = sys.argv[1]
for line in open(env_path):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

# Deterministic tile serving for this run only (process-local).
os.environ["LANA_RAPPORT_MIN_HOURS"] = "0"
os.environ["LANA_RAPPORT_MAX_PER_7D"] = "0"

from app.auth import service_client  # noqa: E402

sb = service_client()
assert "rjlcy" in os.environ["SUPABASE_URL"], "refusing to run against non-dev"

created = {"gap_rows": [], "affiliation": None, "place": None}


def step(msg):
    print(f"\n\033[1m== {msg}\033[0m")


try:
    # ── 0 · pick a sim user with a home block and NO pending tile ask ──────────
    step("pick sim user")
    users = (
        sb.table("users")
        .select("id, nickname, home_block_id")
        .not_.is_("home_block_id", "null")
        .order("created_at", desc=True)
        .limit(15)
        .execute()
    ).data
    user = None
    for u in users:
        pending = (
            sb.table("rapport_gaps")
            .select("gap_row_id", count="exact")
            .eq("user_id", u["id"])
            .eq("status", "asked")
            .execute()
        )
        if not (pending.count or 0):
            user = u
            break
    assert user, "no sim user without a pending ask found"
    uid = user["id"]
    print(f"user: {user.get('nickname')} ({uid[:8]}…) block={str(user['home_block_id'])[:8]}…")

    # ── 1 · seed a captured-but-ungrounded affiliation (what the extractor makes) ──
    step("seed suggested affiliation (circle_type=fitness, detail='my gym')")
    aff = (
        sb.table("circle_affiliations")
        .insert(
            {
                "user_id": uid,
                "circle_type": "fitness",
                "circle_key": "qa_ground_gym",
                "status": "suggested",
                "source": "chat_extraction",
                "confidence": 0.9,
                "detail": "my gym",
            }
        )
        .execute()
    ).data[0]
    created["affiliation"] = aff["id"]
    print(f"affiliation {aff['id'][:8]}… status={aff['status']} place_ref={aff['place_ref']}")

    # ── 2 · synthesis: the capture hook should open ONE grounding gap ──────────
    step("ensure_grounding_gaps (runs after capture / in buffer refill)")
    from app.circles_flow import ensure_grounding_gaps

    opened = ensure_grounding_gaps(uid)
    gaps = (
        sb.table("rapport_gaps")
        .select("gap_row_id, gap_id, question, why_frame, unlock_score, status")
        .eq("user_id", uid)
        .eq("gap_id", f"ground:{aff['id']}")
        .execute()
    ).data
    assert opened >= 1 and gaps, f"grounding gap not opened (opened={opened})"
    gap = gaps[0]
    created["gap_rows"].append(gap["gap_row_id"])
    print(f"opened={opened}  question: “{gap['question']}”  teaser: “{gap['why_frame']}”")
    print(f"score={gap['unlock_score']} status={gap['status']}")

    # Make it the sure winner over the sim user's other open gaps for this QA run.
    sb.table("rapport_gaps").update({"unlock_score": 5.0}).eq(
        "gap_row_id", gap["gap_row_id"]
    ).execute()

    # ── 3 · the tile serves it, with real Google chips ─────────────────────────
    step("next_ask → tile payload")
    from app.rapport_ranker import next_ask

    ask = next_ask(uid)
    assert ask, "next_ask returned nothing"
    print(json.dumps({k: ask.get(k) for k in ("kind", "question", "affiliation_id")}, indent=2))
    for o in ask.get("options") or []:
        print(f"  chip: {o['label']!r}  → ground {o['google_place_id'][:20]}…  ({o.get('address')})")
    assert ask.get("kind") == "place_grounding" and ask.get("affiliation_id") == aff["id"]

    # ── 4 · free-text answer (never auto-grounds → confirm chips) ──────────────
    step("free-text answer: 'orangetheory' → confirm chips")
    from app.rapport_gaps import get_gap_row, mark_answered
    from app.circles_flow import handle_grounding_answer, handle_grounding_confirmation

    gap_row = get_gap_row(gap["gap_row_id"])
    mark_answered(gap["gap_row_id"])
    result = handle_grounding_answer(uid, gap_row, "orangetheory")
    print(f"Lana: “{result['reply']}”")
    for o in result["options"]:
        print(f"  chip: {o['label']!r} → send {o['send']!r}")
    aff_now = sb.table("circle_affiliations").select("place_ref, status").eq("id", aff["id"]).execute().data[0]
    assert aff_now["place_ref"] is None, "MUST NOT auto-ground from free text"
    print(f"affiliation still ungrounded after free text ✓ (pending attempts={result['pending']['attempts'] if result['pending'] else None})")

    # ── 5 · confirm chip tap → grounds for real ────────────────────────────────
    if result["pending"]:
        step("tap first confirm chip")
        send = result["pending"]["candidates"][0]["send"]
        confirm = handle_grounding_confirmation(uid, result["pending"], send)
        print(f"Lana: “{confirm['reply']}”  grounded={confirm['grounded']}")
        aff_now = (
            sb.table("circle_affiliations")
            .select("place_ref, status, detail")
            .eq("id", aff["id"])
            .execute()
        ).data[0]
        print(f"affiliation: status={aff_now['status']} place_ref={str(aff_now['place_ref'])[:8]}…")
        assert aff_now["status"] == "confirmed" and aff_now["place_ref"], "grounding did not land"
        created["place"] = aff_now["place_ref"]
        place = sb.table("places").select("name, address, google_place_id, created_by").eq("id", aff_now["place_ref"]).execute().data[0]
        print(f"canonical place: {place['name']} — {place['address']}")

        enrich = (
            sb.table("rapport_gaps")
            .select("gap_row_id, question, place_ref")
            .eq("user_id", uid)
            .eq("place_ref", aff_now["place_ref"])
            .execute()
        ).data
        for e in enrich:
            created["gap_rows"].append(e["gap_row_id"])
            print(f"§4.3 enrichment queued: “{e['question']}”")
    else:
        step("no Google candidates in this area — answer kept as detail (also valid)")

    # ── 6 · cadence: next tile question must NOT be another circle ask ─────────
    step("cadence check: next ask after a circle ask")
    ask2 = next_ask(uid)
    if ask2:
        created_kind = ask2.get("kind") or "rapport"
        print(f"next tile question kind={created_kind}: “{ask2.get('question')}”")
        assert created_kind != "place_grounding", "cadence guard failed — two circle asks in a row"
        # Un-mark it so we don't leave the sim user's real gap consumed by QA.
        sb.table("rapport_gaps").update({"status": "open", "asked_at": None}).eq(
            "gap_row_id", ask2["gap_row_id"]
        ).execute()
    else:
        print("no other open gaps for this user (fine — guard is suppress-only)")

    print("\n\033[1;32mALL CHECKS PASSED\033[0m")

finally:
    step("cleanup")
    for gid in created["gap_rows"]:
        sb.table("rapport_gaps").delete().eq("gap_row_id", gid).execute()
        print(f"deleted gap {gid[:8]}…")
    if created["affiliation"]:
        sb.table("circle_affiliations").delete().eq("id", created["affiliation"]).execute()
        print(f"deleted affiliation {created['affiliation'][:8]}…")
    if created["place"]:
        owned = (
            sb.table("places")
            .select("id, created_by")
            .eq("id", created["place"])
            .execute()
        ).data
        members = (
            sb.table("circle_affiliations")
            .select("id", count="exact")
            .eq("place_ref", created["place"])
            .execute()
        )
        if owned and not (members.count or 0):
            sb.table("places").delete().eq("id", created["place"]).execute()
            print(f"deleted canonical place {created['place'][:8]}… (no other members)")
        else:
            print("kept canonical place (other members reference it)")
