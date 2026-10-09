#!/usr/bin/env python3
"""Held-out, real-model eval of the community / events / recommendation slot readers.

WHY THIS EXISTS

A prompt example copied from the QA bug it fixes proves nothing: the model passes the QA
sentence because the QA sentence is in its prompt. Every case here is meant to be ABSENT
from the prompts — the `qa` group is the real QA transcripts (held out on purpose; if one
of them ever shows up in a prompt again, this eval stops meaning anything), and the other
groups vary the community kind (initials, nicknames, churches, gyms, PTAs, hospitals,
neighbourhood associations, non-English names), the ask shape and the language, plus
negatives. Keep it that way: when a prompt needs an example, use an entity that is NOT in
this file.

WHAT IT CALLS — the same functions the worker calls, nothing re-implemented:
  router   app.discovery_slots.ai_parse_discovery_turn
  draft    app.tip_ask_draft.build_ask_draft             (kind_named → kind_options)
  expand   app.community_discovery._alias_expansion_rows  (DB read stubbed: records names)
  alias    app.community_discovery._ai_alias_match
  capture  app.community_capture._extract_fields          (parent)

No database is touched. Only the LLM env is read.

USAGE

    # key from the environment, or from an env file (only the LLM keys are read from it)
    .venv/bin/python scripts/eval_community_slots.py --env-file ../../deploy/lana-worker-prod.env
    # compare against an older copy of app/ (e.g. `git archive <rev> services/lana-worker/app`)
    .venv/bin/python scripts/eval_community_slots.py --app-root /tmp/old/services/lana-worker
    # options: --repeats 2  --group qa,events  --case q1  --json out.json

Exit code is 0 when every case passed on every repeat.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Any

_ENV_KEYS = (
    "OPENAI_API_KEY",
    "LANA_LLM_PROVIDER",
    "OPENAI_ROUTER_MODEL",
    "OPENAI_LANA_ROUTER_MODEL",
    "OPENAI_SYNTH_MODEL",
    "LANA_DISCOVERY_MODEL",
    "OPENAI_TIMEOUT_SEC",
)

# ---- matchers -------------------------------------------------------------------------
# None        value must be null/empty
# "x" / bool  equal (strings case-insensitive)
# ("has", s)  value contains s (case-insensitive); s may be a list = any of them
# ("in", [..]) value (lowercased) is one of them
# ("not_in", [..])
# ("opt_has", s) like "has", but passes when the key does not exist on this branch
# ("opt_none",)  like None, but passes when the key does not exist on this branch


def _match(val: Any, want: Any, *, present: bool = True) -> bool:
    if isinstance(want, tuple):
        op = want[0]
        if op.startswith("opt_"):
            if not present:
                return True
            op = op[4:]
            if op == "none":
                return not val
        if op == "has":
            subs = want[1] if isinstance(want[1], list) else [want[1]]
            hay = json.dumps(val, ensure_ascii=False).lower() if not isinstance(val, str) else val.lower()
            return bool(val) and any(s.lower() in hay for s in subs)
        if op == "in":
            return str(val or "").strip().lower() in [str(w).lower() for w in want[1]]
        if op == "not_in":
            return str(val or "").strip().lower() not in [str(w).lower() for w in want[1]]
        raise ValueError(op)
    if want is None:
        return not val
    if isinstance(want, str):
        return str(val or "").strip().lower() == want.lower()
    return val == want


def _route(s: dict[str, Any]) -> str:
    li = str(s.get("linear_intent") or "")
    if li == "discovery.communities":
        return "communities"
    if li in ("discovery.find_activities", "looking.meet") or s.get("goal") == "activities":
        return "activities"
    if li == "looking.tip" or s.get("signal_intent") == "tip_seek":
        return "tip"
    if s.get("goal") == "peers" or li.startswith("discovery.find_peers"):
        return "peers"
    return li or str(s.get("goal") or "none")


_ASKED: dict[str, list[str]] = {}

ACT = "activities"
COM = "communities"


def R(id_, group, text, expect, *, active=None, history=None):
    return {"id": id_, "group": group, "kind": "router", "text": text, "expect": expect,
            "active": active, "history": history}


def D(id_, group, text, kind_named):
    return {"id": id_, "group": group, "kind": "draft", "text": text,
            "expect": {"kind_named": kind_named}}


def E(id_, group, said, expect):
    return {"id": id_, "group": group, "kind": "expand", "text": said, "expect": expect}


def A(id_, group, said, pool, index):
    return {"id": id_, "group": group, "kind": "alias", "text": said, "pool": pool,
            "expect": {"match": index}}


def C(id_, group, text, parent):
    return {"id": id_, "group": group, "kind": "capture", "text": text,
            "expect": {"parent": parent}}


_FAR_OFFER = [
    {"role": "user", "content": "language exchange events in San Jose"},
    {"role": "assistant", "content": "I found two language exchanges in San Jose, about 40 "
     "miles from your home. Want me to look near home instead, or keep San Jose?"},
]

CASES: list[dict[str, Any]] = [
    # ---- held-out QA transcripts (must NOT appear in any prompt) ----------------------
    R("q1", "qa", "at sjsu what events are going on this week",
      {"route": ACT, "community_name": ("has", "sjsu"), "activity_topic": None}),
    R("q2", "qa", "Any clubs at SJSU focused on AI ethics?",
      {"route": COM, "community_ask": "chapters", "community_name": ("has", "sjsu"),
       "community_topic": ("has", "ethic")}),
    R("q3", "qa", "Any clubs at San Jose State University focused on AI ethics?",
      {"route": COM, "community_ask": "chapters", "community_name": ("has", "san jose state"),
       "community_topic": ("has", "ethic")}),
    R("q4", "qa", "what's going on at SJSU this week?",
      {"route": ACT, "community_name": ("has", "sjsu"), "activity_topic": None}),
    R("q5", "qa", "I have a free hour, what's going on at SJSU this week?",
      {"route": ACT, "community_name": ("has", "sjsu"), "activity_topic": None}),
    R("q6", "qa", "are there any events going on this saturday related to alumni?",
      {"route": ACT, "community_name": None, "activity_topic": ("has", "alumni")}),
    R("q7", "qa", "do you have recommendations at SJSU", {"route": "tip"}),
    D("q7d", "qa", "do you have recommendations at SJSU", False),
    R("q8", "qa", "keep San Jose",
      {"linear_intent": ("not_in", ["settings.change_zip"]),
       "_place": ("has", "san jose")}, history=_FAR_OFFER),
    E("q9", "qa", "SJSU", {"names": ("has", "san jose state")}),
    A("q10", "qa", "SJSU", ["San Jose Fitness", "San Jose State University",
                            "Santa Clara University"], 1),
    C("q11", "qa", "I want to start a community for RCC, a club inside SJSU", ("has", "sjsu")),
    R("q12", "qa", "tell me about RCC",
      {"route": COM, "community_ask": "about", "community_name": ("has", "rcc")}),
    R("q13", "qa", "put RCC under SJSU",
      {"community_ask": "manage", "chapter_action": "attach",
       "community_name": ("has", "rcc"), "community_parent": ("has", "sjsu")}),

    # ---- clubs / groups INSIDE one named community --------------------------------------
    R("c1", "chapters", "what clubs does UCLA have?",
      {"route": COM, "community_ask": "chapters", "community_name": ("has", "ucla"),
       "community_topic": None}),
    R("c2", "chapters", "any robotics clubs at NYU?",
      {"route": COM, "community_ask": "chapters", "community_name": ("has", "nyu"),
       "community_topic": ("has", "robot")}),
    R("c3", "chapters", "Are there groups at St. Mary's for young adults?",
      {"route": COM, "community_ask": "chapters", "community_name": ("has", "mary"),
       "community_topic": ("has", "young")}),
    R("c4", "chapters", "which ministries does Iglesia Cristo Rey have?",
      {"route": COM, "community_ask": "chapters", "community_name": ("has", "cristo rey")}),
    R("c5", "chapters", "what groups are in this community?",
      {"route": COM, "community_ask": "chapters", "community_name": ("has", "hyde park")},
      active='"Hyde Park Neighborhood Association" (neighborhood)'),
    R("c6", "chapters", "clubs at the U about hiking",
      {"route": COM, "community_ask": "chapters", "community_name": ("in", ["the u", "u"]),
       "community_topic": ("has", "hik")}),
    R("c7", "chapters", "does UT Austin have any esports clubs",
      {"route": COM, "community_ask": "chapters", "community_name": ("has", "ut"),
       "community_topic": ("has", "esport")}),
    R("c8", "chapters", "Is there a club at Georgia Tech that works on sustainability?",
      {"route": COM, "community_ask": "chapters", "community_name": ("has", "georgia tech"),
       "community_topic": ("has", "sustainab")}),

    # ---- a search ACROSS communities ----------------------------------------------------
    R("x1", "across", "any chess clubs?",
      {"route": COM, "community_name": None, "community_ask": None,
       "community_topic": ("has", "chess")}),
    R("x2", "across", "is there a hiking group near me",
      {"route": COM, "community_name": None, "community_topic": ("has", "hik")}),
    R("x3", "across", "any communities for new dads nearby?",
      {"route": COM, "community_name": None, "community_ask": None,
       "community_topic": ("has", "dad")},
      active='"Gold\'s Gym Downtown" (gym)'),
    R("x4", "across", "communities for Muslim students?",
      {"route": COM, "community_name": None, "community_topic": ("has", "muslim")}),
    # A null-named 'chapters' is answered as the across search (community_discovery), so
    # only the missing name is load-bearing here.
    R("x5", "across", "what clubs are there?", {"route": COM, "community_name": None}),

    # ---- about / people of ONE named community ------------------------------------------
    R("a1", "about", "tell me about the Black Student Union",
      {"route": COM, "community_ask": "about", "community_name": ("has", "black student union")}),
    R("a2", "about", "what is the Bruin Climbing Club?",
      {"route": COM, "community_ask": "about", "community_name": ("has", "climbing")}),
    R("a3", "about", "what kind of place is the Y?",
      {"route": COM, "community_ask": "about", "community_name": ("has", ["y", "ymca"])}),
    R("a4", "about", "who's in the St. Mary's choir?",
      {"route": COM, "community_ask": "people", "community_name": ("has", "choir")}),

    # ---- manage / attach / detach -------------------------------------------------------
    R("m1", "manage", "put the Chess Club under NYU",
      {"community_ask": "manage", "chapter_action": "attach",
       "community_name": ("has", "chess"), "community_parent": ("has", "nyu")}),
    R("m2", "manage", "make the youth ministry part of Iglesia Cristo Rey",
      {"community_ask": "manage", "chapter_action": "attach",
       "community_name": ("has", "youth"), "community_parent": ("has", "cristo rey")}),
    R("m3", "manage", "the debate team is a club of UT, link them",
      {"community_ask": "manage", "chapter_action": "attach",
       "community_name": ("has", "debate"), "community_parent": ("has", "ut")}),
    R("m4", "manage", "remove the Running Crew from Gold's Gym",
      {"community_ask": "manage", "chapter_action": "detach",
       "community_name": ("has", "running")}),
    R("m5", "manage", "make the PTA book club standalone",
      {"community_ask": "manage", "chapter_action": "detach",
       "community_name": ("has", "book club")}),
    R("m6", "manage", "I need to change the address of my Hyde Park Neighborhood Association community",
      {"community_ask": "manage", "chapter_action": None, "community_name": ("has", "hyde park")}),

    # ---- events: at a named place vs no place -------------------------------------------
    R("e1", "events", "what's happening at NYU this weekend?",
      {"route": ACT, "community_name": ("has", "nyu"), "activity_topic": None}),
    R("e2", "events", "any events at St. Mary's on Sunday?",
      {"route": ACT, "community_name": ("has", "mary"), "activity_topic": None}),
    R("e3", "events", "what's going on at the Y tonight",
      {"route": ACT, "community_name": ("has", ["y", "ymca"]), "activity_topic": None}),
    R("e4", "events", "any basketball games at UCLA this week?",
      {"route": ACT, "community_name": ("has", "ucla"), "activity_topic": ("has", "basketball")}),
    R("e5", "events", "what's on this weekend?",
      {"route": ACT, "community_name": None, "activity_topic": None}),
    R("e6", "events", "any salsa dancing events friday?",
      {"route": ACT, "community_name": None, "activity_topic": ("has", "salsa")}),
    R("e7", "events", "what events are happening at UCF for graduates this month",
      {"route": ACT, "community_name": ("has", "ucf"), "activity_topic": ("has", "grad")}),
    R("e8", "events", "whats goin on at nyu tmrw",
      {"route": ACT, "community_name": ("has", "nyu")}),
    R("e9", "events", "I'm visiting Portland next weekend, anything fun going on there?",
      {"route": ACT, "community_name": None, "search_place": ("has", "portland")}),
    R("e10", "events", "got a couple hours to kill, anything happening at the Y?",
      {"route": ACT, "community_name": ("has", ["y", "ymca"]), "activity_topic": None}),

    # ---- recommendations: kind-less vs specific -----------------------------------------
    R("r1", "recs", "any good dentist near UCLA?", {"route": "tip"}),
    R("r2", "recs", "do you have recommendations at UT", {"route": "tip"}),
    D("r3", "recs", "any good dentist near UCLA?", True),
    D("r4", "recs", "do you have recommendations near the Y", False),
    D("r5", "recs", "any recommendations?", False),
    D("r6", "recs", "what do people recommend around St. Mary's?", False),
    D("r7", "recs", "somewhere to eat near NYU", True),
    D("r8", "recs", "¿alguna recomendación cerca de la UNAM?", False),
    D("r9", "recs", "recommend a good mechanic", True),
    D("r10", "recs", "kya UT ke paas koi acha chai ka dhaba hai?", True),

    # ---- other languages ------------------------------------------------------------------
    R("l1", "lang", "¿Qué clubes tiene la UNAM?",
      {"route": COM, "community_ask": "chapters", "community_name": ("has", "unam")}),
    R("l2", "lang", "UCLA mein koi cricket club hai?",
      {"route": COM, "community_ask": "chapters", "community_name": ("has", "ucla"),
       "community_topic": ("has", "cricket")}),
    R("l3", "lang", "¿Qué eventos hay en la Iglesia San Juan este domingo?",
      {"route": ACT, "community_name": ("has", "san juan")}),
    R("l4", "lang", "kya is weekend koi event hai?",
      {"route": ACT, "community_name": None}),

    # ---- negatives ------------------------------------------------------------------------
    R("n1", "negative", "I'm so tired today",
      {"route": ("not_in", [COM, ACT, "tip"]), "community_name": None}),
    R("n2", "negative", "find me neighbors who like tennis",
      {"route": "peers", "community_name": None}),
    E("n3", "negative", "XQZT", {"names": None}),
    E("n4", "negative", "Planet Fitness", {"names": None}),
    A("n5", "negative", "XQZT", ["Xavier University", "Quincy Park"], None),
    A("n6", "negative", "Fitness", ["Anytime Fitness", "Gold's Gym"], None),
    A("n7", "negative", "BSU", ["Boise State University", "Ball State University"], None),

    # ---- short forms: spelled out, then matched ------------------------------------------
    E("s1", "alias", "UCLA", {"names": ("has", "los angeles")}),
    E("s2", "alias", "NYU", {"names": ("has", "new york university")}),
    E("s3", "alias", "the Y", {"names": ("has", ["ymca", "young men"])}),
    E("s4", "alias", "MIT", {"names": ("has", "massachusetts institute")}),
    A("s5", "alias", "UCLA", ["UC Berkeley", "University of California, Los Angeles",
                              "LA Fitness"], 1),
    A("s6", "alias", "the Y", ["Yoga Loft", "YMCA of Greater Seattle", "Yellow Cab Co-op"], 1),
    A("s7", "alias", "St. Mary's", ["St. Mary's Catholic Church", "Mary's Diner"], 0),

    # ---- community create: the parent it sits inside ------------------------------------
    C("p1", "capture", "start a community for the Chess Club, it's a club inside NYU", ("has", "nyu")),
    C("p2", "capture", "add our youth choir as a community, it's part of St. Mary's Church",
      ("has", "mary")),
    C("p3", "capture", "create a community for my running group, we meet at Riverside Park", None),
    C("p4", "capture", "make a community for the Hyde Park PTA", None),

    # ---- where they are (current_place, fix/oct7-zip) vs change_zip vs search_place ------
    # current_place checks pass vacuously on a branch without that slot.
    R("z1", "zip", "I'm in Tucson right now, what's happening?",
      {"linear_intent": ("not_in", ["settings.change_zip"]), "current_place": ("opt_has", "tucson")}),
    R("z2", "zip", "change my zip to 30307",
      {"linear_intent": "settings.change_zip", "zip": "30307", "current_place": ("opt_none",)}),
    R("z3", "zip", "I'm staying in Boise this week",
      {"linear_intent": ("not_in", ["settings.change_zip"]), "current_place": ("opt_has", "boise")}),
    R("z4", "zip", "use Albuquerque for now",
      {"linear_intent": ("not_in", ["settings.change_zip"]),
       "current_place": ("opt_has", "albuquerque")}),
    R("z5", "zip", "my new home zip is 60614",
      {"linear_intent": "settings.change_zip", "zip": "60614"}),
    R("z6", "zip", "estoy en Monterrey esta semana, ¿qué hay para hacer?",
      {"linear_intent": ("not_in", ["settings.change_zip"]),
       "current_place": ("opt_has", "monterrey")}),
    R("z7", "zip", "what's on in Savannah? I'm going there next month",
      {"route": ACT, "search_place": ("has", "savannah"), "current_place": ("opt_none",)}),
]


# ---- runners --------------------------------------------------------------------------

def _run_router(case: dict[str, Any]) -> dict[str, Any]:
    from app.discovery_slots import ai_parse_discovery_turn

    ctx: dict[str, Any] = {"_eval_active": case.get("active")}
    s = ai_parse_discovery_turn(
        case["text"], routing_phase="listening", history=case.get("history"),
        has_block=True, has_identity=True, phone_verified=True, session_ctx=ctx,
    )
    out = dict(s)
    out["route"] = _route(s)
    out["_place"] = " ".join(str(s.get(k) or "") for k in ("search_place", "current_place"))
    if not s.get("linear_intent") and not s.get("goal") and not s.get("confidence"):
        out["_error"] = "empty slots (call failed or swallowed)"
    return out


def _run_draft(case: dict[str, Any]) -> dict[str, Any]:
    from app.tip_ask_draft import build_ask_draft

    d = build_ask_draft(msg=case["text"], detail=case["text"])
    return {"kind_named": "kind_options" not in d, "kind_options": d.get("kind_options"),
            "title": d.get("title")}


def _run_expand(case: dict[str, Any]) -> dict[str, Any]:
    import app.community_discovery as cd

    # The lookup is stubbed in main() to record each spelled-out name under the user id.
    uid = f"eval-{id(case)}-{threading.get_ident()}"
    _ASKED[uid] = []
    cd._alias_expansion_rows(uid, case["text"])
    return {"names": _ASKED.pop(uid)}


def _run_alias(case: dict[str, Any]) -> dict[str, Any]:
    from app.community_discovery import _ai_alias_match

    pool = [{"place_id": f"p{i}", "place_name": n, "place_address": ""}
            for i, n in enumerate(case["pool"])]
    hit = _ai_alias_match(case["text"], [pool])
    return {"match": int(hit["place_id"][1:]) if hit else None,
            "name": hit["place_name"] if hit else None}


def _run_capture(case: dict[str, Any]) -> dict[str, Any]:
    from app.community_capture import _extract_fields

    prev = {"step_set": [{"field": "what_you_do", "question": "What do people do there?"}]}
    f = _extract_fields(history=[], user_message=case["text"], prev=prev)
    return {"parent": f.get("parent"), "name": f.get("name")}


_RUNNERS = {"router": _run_router, "draft": _run_draft, "expand": _run_expand,
            "alias": _run_alias, "capture": _run_capture}


def _judge(case: dict[str, Any], got: dict[str, Any]) -> list[str]:
    misses = []
    if got.get("_error"):
        return [got["_error"]]
    for key, want in case["expect"].items():
        if not _match(got.get(key), want, present=key in got):
            misses.append(f"{key}={got.get(key)!r}")
    return misses


def _load_env(path: str | None) -> None:
    if path:
        for line in pathlib.Path(path).read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if k in _ENV_KEYS and not os.environ.get(k):
                os.environ[k] = v.strip().strip('"').strip("'")
    os.environ["LANA_LLM_FALLBACK"] = "0"  # measure the configured model, not a failover
    os.environ.setdefault("LANA_DISCOVERY_AI_SLOTS", "1")
    if not os.environ.get("OPENAI_API_KEY") and not os.environ.get("GCP_VERTEX_PROJECT"):
        sys.exit("no LLM configured: set OPENAI_API_KEY or pass --env-file")


def main() -> int:
    here = pathlib.Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--app-root", default=str(here), help="directory containing app/")
    ap.add_argument("--env-file", default=os.environ.get("LANA_EVAL_ENV_FILE"))
    ap.add_argument("--repeats", type=int, default=2)
    ap.add_argument("--group", default="", help="comma-separated groups")
    ap.add_argument("--case", default="", help="comma-separated case ids")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--json", default="", help="write per-case results here")
    args = ap.parse_args()

    _load_env(args.env_file)
    sys.path.insert(0, str(pathlib.Path(args.app_root).resolve()))
    import logging

    logging.disable(logging.CRITICAL)
    import app.community_opening as co

    # The router's context line for the chat's community, without a DB read.
    co.active_community_prompt_line = lambda sc: (sc or {}).get("_eval_active") or "none"
    import app.community_discovery as cd

    # No DB: the alias expansion's anywhere-by-name lookup records what it was asked.
    cd.discover_communities_anywhere = lambda uid, full, **_k: _ASKED[uid].append(full) or []

    groups = {g for g in args.group.split(",") if g}
    ids = {c for c in args.case.split(",") if c}
    cases = [c for c in CASES if (not groups or c["group"] in groups) and (not ids or c["id"] in ids)]
    jobs = [(c, r) for c in cases for r in range(args.repeats)]

    def run(job):
        case, rep = job
        try:
            got = _RUNNERS[case["kind"]](case)
        except Exception as exc:  # noqa: BLE001
            got = {"_error": f"{type(exc).__name__}: {exc}"}
        return case, rep, got, _judge(case, got)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(run, jobs))

    per_group: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    per_case: dict[str, list[tuple[list[str], dict[str, Any]]]] = defaultdict(list)
    for case, _rep, got, misses in results:
        per_group[case["group"]][0] += not misses
        per_group[case["group"]][1] += 1
        per_case[case["id"]].append((misses, got))

    for case in cases:
        runs = per_case[case["id"]]
        bad = [m for m, _ in runs if m]
        if bad:
            tag = "FLAKY" if len(bad) < len(runs) else "FAIL "
            print(f"{tag} {case['id']:<4} [{case['group']}] {case['text']!r}: {'; '.join(bad[0])}")
    total = [sum(v[0] for v in per_group.values()), sum(v[1] for v in per_group.values())]
    print("\ngroup        pass/runs")
    for g in dict.fromkeys(c["group"] for c in cases):
        p, n = per_group[g]
        print(f"  {g:<10} {p:>3}/{n:<3} {100 * p / n:5.1f}%")
    print(f"  {'TOTAL':<10} {total[0]:>3}/{total[1]:<3} {100 * total[0] / max(total[1], 1):5.1f}%")
    if args.json:
        slim = [{"id": c["id"], "rep": r, "misses": m,
                 "got": {k: v for k, v in g.items() if k in set(c["expect"]) | {"_error", "linear_intent"}}}
                for c, r, g, m in results]
        pathlib.Path(args.json).write_text(json.dumps(slim, ensure_ascii=False, indent=0))
    return 0 if total[0] == total[1] else 1


if __name__ == "__main__":
    raise SystemExit(main())
