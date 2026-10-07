#!/usr/bin/env python3
"""End-to-end chat harness for the Lana worker — Oct-7 SJSU/community reports + generality.

WHAT IT DOES
  Drives REAL chat turns (real OpenAI LLM via the worker, real Postgres via LOCAL Supabase)
  against a running lana-worker, records every reply + surface (activity_previews,
  google_reco_cards, place_suggestions, community_discovery, ui_actions), and asserts the
  expected-after-fix behaviour. Assertions are string/structure checks; a small gpt-4.1-mini
  judge (strict yes/no rubric) is used only where meaning must be judged.

SETUP (local only — the seed refuses non-local URLs)
  1. Local Supabase from the repo root:  npx supabase start   (or reuse a running stack)
     Migrations:                          npx supabase db reset --local  (or migration up)
     NOTE: needs free disk — the CLI may pull new images; check `df -h` / `docker system df`.
  2. Env file OUTSIDE the repo: deploy/lana-worker.localstack.env shape, with
     SUPABASE_URL=http://127.0.0.1:54321 + keys from `npx supabase status -o env`, and
     OPENAI_API_KEY / GOOGLE_MAPS_API_KEY / LANA_* flags copied from the prod env.
  3. Worker:  cd services/lana-worker && set -a && . <env> && set +a &&
              PYTHONPATH=. .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8093
  4. Run:     services/lana-worker/.venv/bin/python scripts/e2e/sjsu_oct7.py \
                --base-url http://127.0.0.1:8093 --env <env> [--out transcript.json] [--only S1,S3]

  Exit code 0 = all PASS. The JSON transcript holds every turn, surface and check.

KNOWN GAP
  Vertex embeddings (text-embedding-005) need gcloud ADC. Without it, places.blurb_embedding /
  events.embedding stay NULL, so meaning search (community_meaning_search, event semantic
  rank) falls back to text. The harness records `embeddings_present` in the transcript.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def load_env(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.split(" #")[0].strip().strip('"').strip("'")
        out[k.strip()] = v
    return out


# ── transport ───────────────────────────────────────────────────────────────────────────
class Chat:
    def __init__(self, base: str, user: dict, community_id: str | None = None):
        self.base = base.rstrip("/")
        self.user = user
        self.community_id = community_id
        self.http = httpx.Client(timeout=300)
        self.turns: list[dict] = []
        self.sid: str | None = None

    def _h(self) -> dict:
        return {"Authorization": f"Bearer {self.user['jwt']}", "Content-Type": "application/json"}

    def open(self) -> dict:
        body: dict[str, Any] = {"purpose": "lana", "force_new": True}
        if self.community_id is not None:
            body["community_id"] = self.community_id
        r = self.http.post(f"{self.base}/lana/sessions", headers=self._h(), json=body)
        r.raise_for_status()
        d = r.json()
        self.sid = d["session_id"]
        self.turns.append({"user": None, "opening": True, "status": r.status_code, **_slim(d)})
        return d

    def say(self, text: str, **extra) -> dict:
        assert self.sid, "open() first"
        body: dict[str, Any] = {"message": text, **extra}
        if self.community_id is not None and "community_id" not in body:
            body["community_id"] = self.community_id   # the PWA sends its pill every turn
        t = time.time()
        r = self.http.post(f"{self.base}/lana/sessions/{self.sid}/messages", headers=self._h(), json=body)
        secs = round(time.time() - t, 1)
        if r.status_code != 200:
            d = {"assistant_message": f"<HTTP {r.status_code}> {r.text[:600]}"}
        else:
            d = r.json()
        rec = {"user": text, "sent": {k: v for k, v in body.items() if k != "message"},
               "status": r.status_code, "secs": secs, **_slim(d)}
        self.turns.append(rec)
        if d.get("community_released") is not None:
            self.community_id = ""   # the app moves its pill back to the area
        return rec

    def tap(self, rec: dict, pattern: str) -> dict | None:
        """Tap the ui_action whose label matches `pattern` — posts what the chip posts."""
        for a in rec.get("ui_actions") or []:
            if re.search(pattern, a.get("label") or "", re.I):
                return self.say(a.get("message") or a.get("label"))
        return None


def _slim(d: dict) -> dict:
    keys = ("assistant_message", "ui_intent", "active_intent", "routing_phase", "activity_previews",
            "google_reco_cards", "place_suggestions", "reco_cards", "community_discovery",
            "communities", "ui_actions", "community_released", "home_block_assigned",
            "look_draft", "ask_draft", "grounding", "routing")
    return {k: d.get(k) for k in keys if d.get(k) not in (None, [], {})}


# ── judge (only where meaning must be judged) ───────────────────────────────────────────
class Judge:
    def __init__(self, key: str | None):
        self.key = key
        self.http = httpx.Client(timeout=60)

    def yes(self, question: str, reply: str, context: str = "") -> tuple[bool, str]:
        if not self.key:
            return False, "no OPENAI_API_KEY for judge"
        sys_p = ("You are a strict QA judge. Answer ONLY with JSON {\"answer\": \"yes\"|\"no\", "
                 "\"why\": \"<one short sentence>\"}. Judge the assistant reply literally; do not "
                 "give credit for things it did not actually say.")
        user_p = f"CONTEXT: {context}\n\nASSISTANT REPLY:\n{reply}\n\nQUESTION: {question}"
        r = self.http.post("https://api.openai.com/v1/chat/completions",
                           headers={"Authorization": f"Bearer {self.key}"},
                           json={"model": "gpt-4.1-mini", "temperature": 0,
                                 "response_format": {"type": "json_object"},
                                 "messages": [{"role": "system", "content": sys_p},
                                              {"role": "user", "content": user_p}]})
        r.raise_for_status()
        j = json.loads(r.json()["choices"][0]["message"]["content"])
        return str(j.get("answer", "")).lower() == "yes", str(j.get("why", ""))


# ── generic per-reply checker (S11) ─────────────────────────────────────────────────────
INTERNAL_KEYS = re.compile(
    r"\b(intent_hint|ui_intent|active_intent|tool_args|tool_to_call|activity_previews|"
    r"place_suggestions|google_reco_cards|community_id|place_ref|circle_place_ref|look_meet|"
    r"find_peers|tip_seek|decide_turn|routing_phase|slot[s]?\s*[:=]|session_ctx|"
    r"home_block_id|block_id|undefined)\b")
SNAKE = re.compile(r"(?<![/\w.@-])[a-z]+(?:_[a-z0-9]+){1,}(?![\w.@/-])")
JSONISH = re.compile(r"[{\[]\s*\"[a-z_]+\"\s*:")
VENDOR = re.compile(r"(openai|vertex|gemini|anthropic|rate.?limit|traceback|exception|"
                    r"APIError|status code \d{3}|\b(4\d\d|5\d\d) (error|internal)|timed? ?out|"
                    r"vertex_not_configured|HTTP \d{3})", re.I)
NEARBY_CLAIM = re.compile(r"(here(?:'|’)?s what(?:'|’)?s (nearby|coming up|on)|here are (some|a few)|"
                          r"coming up near you|i found (some|a few|these)|take a look at these)", re.I)
NONE_CLAIM = re.compile(r"(couldn(?:'|’)?t find any|could not find any|(aren(?:'|’)?t|are not) any|no (events|meets|activities|"
                        r"results|matches)\b|nothing (is )?(coming up|on|scheduled|found))", re.I)
EVENT_CLAIM = re.compile(r"\b(here(?:'|’)?s|here are|there are (?:some|a few|several)|i found|coming up|check out)[^.?!]{0,60}"
                         r"\b(events?|meets?|meetups?|activities|happening)", re.I)
NAME_ASK = re.compile(r"(your (first )?name|what should i call you|what(?:'|’)?s your name)", re.I)


def generic_checks(rec: dict) -> list[str]:
    """Cheap LLM-free invariants every reply must satisfy. Returns failure strings."""
    t = rec.get("assistant_message") or ""
    fails: list[str] = []
    if rec.get("status") not in (200, None):
        fails.append(f"http_{rec.get('status')}")
    if not t.strip():
        fails.append("empty_reply")
    if JSONISH.search(t):
        fails.append("raw_json_in_reply")
    m = INTERNAL_KEYS.search(t)
    if m:
        fails.append(f"internal_key:{m.group(0)}")
    for s in SNAKE.findall(re.sub(r"https?://\S+", "", t)):
        fails.append(f"snake_case_token:{s}")
        break
    v = VENDOR.search(t)
    if v:
        fails.append(f"vendor_error_text:{v.group(0)}")
    if NAME_ASK.search(t) and re.search(r"neighbou?rs?", t, re.I):
        fails.append("neighbors_in_name_prompt")
    if NEARBY_CLAIM.search(t) and NONE_CLAIM.search(t):
        fails.append("contradiction_nearby_and_none")
    acts = rec.get("activity_previews") or []
    other_cards = (rec.get("google_reco_cards") or rec.get("place_suggestions")
                   or rec.get("reco_cards") or rec.get("community_discovery"))
    if acts and re.search(r"(couldn(?:'|’)?t find any|no) (events|meets|activities|meetups)", t, re.I):
        fails.append("text_denies_returned_event_cards")
    if not acts and not other_cards and EVENT_CLAIM.search(t) and not NONE_CLAIM.search(t):
        fails.append("text_claims_cards_not_returned")
    return fails


# ── scenario plumbing ───────────────────────────────────────────────────────────────────
@dataclass
class Result:
    sid: str
    title: str
    ok: bool = True
    checks: list[dict] = field(default_factory=list)
    transcript: list[dict] = field(default_factory=list)
    error: str | None = None

    def check(self, name: str, cond: bool, detail: str = "") -> bool:
        self.checks.append({"check": name, "pass": bool(cond), "detail": detail[:500]})
        if not cond:
            self.ok = False
        return bool(cond)


def titles(rec: dict) -> list[str]:
    return [a.get("title", "") for a in rec.get("activity_previews") or []]


def community_names(rec: dict) -> list[str]:
    cd = rec.get("community_discovery") or {}
    return [c.get("place_name") or "" for c in cd.get("communities") or []]


def has_title(rec: dict, needle: str) -> bool:
    return any(needle.lower() in t.lower() for t in titles(rec))


def ctx_has(rec: dict, pat: str) -> bool:
    blob = (rec.get("assistant_message") or "") + " " + " ".join(community_names(rec))
    return bool(re.search(pat, blob, re.I))


SJSU_MEETS = ["Language Exchange Club", "Career Networking Mixer", "Alumni Garden Volunteer Day"]


class Harness:
    def __init__(self, base: str, env: dict, seed: dict, judge: Judge, seeder):
        self.base, self.env, self.seed, self.judge, self.seeder = base, env, seed, judge, seeder

    def U(self, k: str) -> dict:
        return self.seed["users"][k]

    def chat(self, k: str, community: str | None = None) -> Chat:
        u = self.U(k)
        self.seeder.reset_user(u["id"], u["nickname"], u["home_zip"])
        c = Chat(self.base, u, community)
        c.open()
        return c

    def home_zip(self, k: str) -> str:
        return self.seeder.sql(f"select coalesce(home_zip,'') from public.users where id='{self.U(k)['id']}'")

    # S1 ─────────────────────────────────────────────────────────────────────────────────
    def s1(self, r: Result):
        c = self.chat("member")
        t = c.say("at sjsu what events are going on this week")
        r.transcript = c.turns
        for m in SJSU_MEETS:
            r.check(f"card:{m}", has_title(t, m), f"cards={titles(t)}")
        r.check("no_far_pausa_card", not has_title(t, "Pausa"), f"cards={titles(t)}")

    def s2(self, r: Result):
        c = self.chat("newbie")
        t = c.say("what's going on at SJSU this week?")
        r.transcript = c.turns
        r.check("sjsu_meets_shown", sum(has_title(t, m) for m in SJSU_MEETS) >= 2, f"cards={titles(t)}")
        r.check("no_unknown_community_claim",
                not re.search(r"(no|couldn(?:'|’)?t find (a|any)) community (named|called)", t["assistant_message"] or "", re.I),
                t.get("assistant_message", ""))

    def _club(self, r: Result, user: str, ask: str, pat: str, label: str):
        c = self.chat(user)
        t = c.say(ask)
        r.transcript = c.turns
        r.check(f"names_{label}", ctx_has(t, pat),
                f"reply={t.get('assistant_message')!r} communities={community_names(t)}")

    def s3(self, r): self._club(r, "newbie", "Any clubs at SJSU focused on AI ethics?", r"RCC|Responsible Computing", "RCC")
    def s4(self, r): self._club(r, "newbie", "Any clubs at San Jose State focused on AI ethics?", r"RCC|Responsible Computing", "RCC")

    def s5(self, r: Result):
        c = self.chat("host", community=self.seed["places"]["sjsu"])
        t = c.say("are there any events going on this saturday related to alumni?")
        r.transcript = c.turns
        row = next((a for a in t.get("activity_previews") or [] if "Alumni Garden" in a.get("title", "")), None)
        r.check("garden_day_shown", row is not None, f"cards={titles(t)}")
        r.check("hosted_by_you", bool(row and row.get("hosted_by_you")), json.dumps(row)[:300] if row else "")

    def _beyond(self, r: Result, user: str, community: str, ask: str, beyond_pat: str):
        c = self.chat(user, community=community)
        t1 = c.say(ask)
        chips = [a.get("label") for a in t1.get("ui_actions") or []]
        offered = r.check("beyond_chip_offered", any(re.search(beyond_pat, x or "", re.I) for x in chips),
                          f"chips={chips} reply={t1.get('assistant_message')!r}")
        if offered:
            t2 = c.tap(t1, beyond_pat)
            chips2 = [a.get("label") for a in t2.get("ui_actions") or []]
            r.check("no_reoffer_loop", not any(re.search(beyond_pat, x or "", re.I) for x in chips2),
                    f"chips after tap={chips2}")
            r.check("reply_changed", (t2.get("assistant_message") or "") != (t1.get("assistant_message") or ""),
                    t2.get("assistant_message", ""))
            # The seed has NO meet on these topics (chess / salsa) anywhere, so any card here
            # is an off-topic meet passed off as "the closest" — the topic was lost on the
            # tap (2026-10-08: draft interest became "this weekend").
            r.check("no_off_topic_cards_after_beyond", not t2.get("activity_previews"),
                    f"cards={titles(t2)} reply={t2.get('assistant_message')!r}")
            ok, why = self.judge.yes(
                "Does this reply report the result of searching BEYOND the community (more widely / "
                "the wider area), WITHOUT contradicting itself and WITHOUT asking the user again "
                "whether to look beyond the community?",
                t2.get("assistant_message") or "",
                f"The user previously asked: {ask!r} while scoped to a community; the assistant offered "
                f"to look beyond it and the user tapped that offer.")
            r.check("judge_searched_beyond_no_loop", ok, f"{why} | reply={t2.get('assistant_message')!r}")
        r.transcript = c.turns

    def s6(self, r): self._beyond(r, "member", self.seed["places"]["sjsu"], "any chess meetups this weekend?",
                                  r"beyond|outside|wider|more widely")

    def s7(self, r: Result):
        c = self.chat("member")
        t1 = c.say("any pottery classes this weekend?")
        chips = [a.get("label") for a in t1.get("ui_actions") or []]
        r.check("widen_chip_offered", any(re.search(r"widen|wider|further|expand", x or "", re.I) for x in chips),
                f"chips={chips} reply={t1.get('assistant_message')!r}")
        t2 = c.tap(t1, r"widen|wider|further|expand")
        if t2:
            txt = t2.get("assistant_message") or ""
            claims = bool(re.search(r"coming up near you|near you|here(?:'|’)?s what|here are", txt, re.I)) \
                and not NONE_CLAIM.search(txt)
            if claims:
                r.check("claimed_events_have_cards", bool(t2.get("activity_previews")), f"reply={txt!r}")
            far = has_title(t2, "Pausa")
            r.check("far_card_has_distance_framing",
                    (not far) or bool(re.search(r"\b(mi|miles|km|away|far|florida|travel)\b", txt, re.I)),
                    f"cards={titles(t2)} reply={txt!r}")
        r.transcript = c.turns

    def s8(self, r: Result):
        c = self.chat("newbie")
        t1 = c.say("do you have recommendations at SJSU")
        txt1 = t1.get("assistant_message") or ""
        r.check("asks_what_kind", "?" in txt1 and not (t1.get("google_reco_cards") or t1.get("place_suggestions")),
                f"reply={txt1!r} google={len(t1.get('google_reco_cards') or [])}")
        r.check("no_google_leadin_yet", not re.search(r"google|nearby spots|here(?:'|’)?s what(?:'|’)?s nearby", txt1, re.I), txt1)
        t2 = c.say("coffee")
        txt2 = t2.get("assistant_message") or ""
        r.check("google_cards_rendered", bool(t2.get("google_reco_cards") or t2.get("place_suggestions")
                                              or t2.get("reco_cards")), f"reply={txt2!r}")
        r.check("no_nearby_plus_none", not (NEARBY_CLAIM.search(txt2) and NONE_CLAIM.search(txt2)), txt2)
        r.transcript = c.turns

    def _zip(self, r: Result, target: str, first: str):
        c = self.chat("newbie")
        c.say(first)
        applied_at = None
        for i in range(2):
            t = c.say(target if i == 0 else "yes")
            if self.home_zip("newbie") == target:
                applied_at = i + 1
                break
        r.check(f"zip_{target}_applied_within_2", applied_at is not None,
                f"home_zip now={self.home_zip('newbie')!r} last={c.turns[-1].get('assistant_message')!r}")
        last = c.turns[-1].get("assistant_message") or ""
        # A re-ask is a QUESTION about the ZIP. "Your ZIP is now 95192. What would you like
        # to find?" confirms it and asks something else — judge each question on its own.
        questions = re.findall(r"[^.!?]*\?", last)
        r.check("no_repeat_zip_ask", not any(
            re.search(r"\bzip\b", q, re.I) and re.search(r"(what|which|share|enter|tell me)", q, re.I)
            for q in questions), last)
        r.transcript = c.turns

    def s9(self, r): self._zip(r, "95192", "change my zip")
    def s9b(self, r): self._zip(r, "94404", "I'd like to update my zip code")

    def s10(self, r: Result):
        c = self.chat("newbie")
        c.say("I'm in San Jose")
        t = c.say("any alumni events?")
        txt = t.get("assistant_message") or ""
        r.check("sj_alumni_meets_shown", has_title(t, "Alumni") or has_title(t, "Career Networking"),
                f"cards={titles(t)}")
        r.check("not_measured_from_minneapolis", not re.search(r"minneapolis|\b1[,.]?[5-9]\d\d\s*(mi|miles|km)", txt, re.I), txt)
        r.transcript = c.turns

    # ── generality (coordinator ask 2026-10-08): different community/abbrev/topic ───────
    def g1(self, r: Result):
        c = self.chat("newbie")
        t = c.say("anything happening at UCF this week?")
        r.transcript = c.turns
        r.check("ucf_meets_shown", has_title(t, "Robot Build Night") or has_title(t, "Knights Alumni Tailgate"),
                f"cards={titles(t)}")
        r.check("no_sjsu_cards", not any(has_title(t, m) for m in SJSU_MEETS), f"cards={titles(t)}")

    def g2(self, r): self._club(r, "newbie", "is there a robotics club at Central Florida?", r"Knights Robotics|Robotics Club", "Knights_Robotics")
    def g3(self, r): self._club(r, "member", "who at UCF builds robots? any group for that?", r"Knights Robotics|Robotics Club", "Knights_Robotics")

    def g4(self, r: Result):
        c = self.chat("ucf_member", community=self.seed["places"]["ucf"])
        t = c.say("any alumni things on saturday?")
        row = next((a for a in t.get("activity_previews") or [] if "Tailgate" in a.get("title", "")), None)
        r.check("tailgate_shown", row is not None, f"cards={titles(t)}")
        r.check("hosted_by_you", bool(row and row.get("hosted_by_you")), json.dumps(row)[:300] if row else "")
        r.check("no_sjsu_cards", not any(has_title(t, m) for m in SJSU_MEETS), f"cards={titles(t)}")
        r.transcript = c.turns

    def _miss(self, r: Result, user: str, ask: str, what: str):
        c = self.chat(user)
        t = c.say(ask)
        txt = t.get("assistant_message") or ""
        seeded = SJSU_MEETS + ["Robot Build Night", "Knights Alumni Tailgate", "Pausa"]
        shown = [x for x in seeded if has_title(t, x)]
        ok, why = self.judge.yes(
            f"The user asked about {what}, which does not exist in this app's data. Is the reply an "
            "honest miss — it says it has nothing for that (or asks to clarify) and does NOT present "
            "any other club, community or event as if it were the one asked about?",
            txt, f"Event cards shown alongside the reply: {titles(t)}; community cards: {community_names(t)}")
        r.check("judge_honest_miss", ok, f"{why} | reply={txt!r}")
        r.check("no_unrelated_cards_as_match", not shown or bool(NONE_CLAIM.search(txt)) or "beyond" in txt.lower(),
                f"cards={shown}")
        r.check("no_invented_club_name", not re.search(r"(RCC|Responsible Computing|Knights Robotics)", txt), txt)
        r.transcript = c.turns

    def n1(self, r): self._miss(r, "newbie", "Any clubs at XQZU focused on underwater basket weaving?", "a club at 'XQZU'")
    def n2(self, r): self._miss(r, "member", "what's going on at MIT this week?", "events at MIT")

    def g5(self, r): self._beyond(r, "ucf_member", self.seed["places"]["ucf"], "any salsa dancing nights this week?",
                                  r"beyond|outside|wider|more widely")


SCENARIOS: list[tuple[str, str, str]] = [
    ("S1", "s1", "MEMBER no scope: SJSU events this week -> 3 SJSU meets"),
    ("S2", "s2", "NEWBIE: what's going on at SJSU -> SJSU meets"),
    ("S3", "s3", "NEWBIE: clubs at SJSU on AI ethics -> RCC"),
    ("S4", "s4", "NEWBIE: clubs at San Jose State on AI ethics -> RCC"),
    ("S5", "s5", "HOST scoped: alumni this saturday -> Garden Day hosted_by_you"),
    ("S6", "s6", "MEMBER scoped: no match -> Look beyond chip, no loop"),
    ("S7", "s7", "MEMBER: no match -> Widen chip -> cards match claims"),
    ("S8", "s8", "NEWBIE: recos at SJSU -> asks kind; coffee -> Google cards"),
    ("S9", "s9", "NEWBIE: change zip -> 95192 applied <=2 turns"),
    ("S9b", "s9b", "NEWBIE: update zip -> 94404 applied <=2 turns"),
    ("S10", "s10", "NEWBIE: I'm in San Jose -> alumni events from SJ"),
    ("G1", "g1", "NEWBIE: anything at UCF this week -> UCF meets"),
    ("G2", "g2", "NEWBIE: robotics club at Central Florida -> Knights Robotics"),
    ("G3", "g3", "MEMBER(CA): who at UCF builds robots -> Knights Robotics"),
    ("G4", "g4", "UCF member scoped: alumni saturday -> Tailgate hosted_by_you"),
    ("G5", "g5", "UCF member scoped: no match -> beyond chip, no loop"),
    ("N1", "n1", "NEG: club at XQZU -> honest miss"),
    ("N2", "n2", "NEG: events at MIT -> honest miss"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8093")
    ap.add_argument("--env", required=True)
    ap.add_argument("--db-url", default="postgresql://postgres:postgres@127.0.0.1:54322/postgres")
    ap.add_argument("--out", default=None, help="JSON transcript path")
    ap.add_argument("--only", default="", help="comma list of scenario ids")
    ap.add_argument("--no-seed", action="store_true")
    a = ap.parse_args()

    from seed_oct7 import Seeder

    env = load_env(a.env)
    seeder = Seeder(env, a.db_url)
    h0 = httpx.get(f"{a.base_url}/health", timeout=10)
    print(f"worker {a.base_url} health={h0.status_code}")
    seed = seeder.run()
    emb = seeder.sql("select count(*) filter (where blurb_embedding is not null) || '/' || count(*) "
                     "from public.places where google_place_id like 'e2e:%' or google_place_id like 'creator:san_jose%'")
    print(f"seeded: dates={seed['dates']} place embeddings present={emb}")
    harness = Harness(a.base_url, env, seed, Judge(env.get("OPENAI_API_KEY")), seeder)
    only = {x.strip().upper() for x in a.only.split(",") if x.strip()}

    results: list[Result] = []
    for sid, fn, title in SCENARIOS:
        if only and sid.upper() not in only:
            continue
        r = Result(sid, title)
        t0 = time.time()
        try:
            getattr(harness, fn)(r)
        except Exception as e:  # noqa: BLE001
            r.ok = False
            r.error = f"{type(e).__name__}: {e}"
            r.checks.append({"check": "exception", "pass": False, "detail": traceback.format_exc()[-800:]})
        # S11: generic invariants on EVERY reply of every scenario
        for i, turn in enumerate(r.transcript):
            if turn.get("opening"):
                continue
            for f in generic_checks(turn):
                r.check(f"S11:{f}", False, f"turn{i}: {turn.get('assistant_message')!r}")
        status = "PASS" if r.ok else "FAIL"
        print(f"{sid:4} {status}  {title}  ({time.time()-t0:.0f}s)")
        for ck in r.checks:
            if not ck["pass"]:
                print(f"       x {ck['check']}: {ck['detail'][:300]}")
        results.append(r)

    out = a.out or f"e2e_oct7_{dt.datetime.now():%Y%m%d_%H%M%S}.json"
    with open(out, "w") as fh:
        json.dump({"base_url": a.base_url, "dates": seed["dates"], "place_embeddings": emb,
                   "results": [r.__dict__ for r in results]}, fh, indent=1, default=str)
    print("\n" + "-" * 72)
    print(f"{'ID':5}{'RESULT':8}SCENARIO")
    for r in results:
        print(f"{r.sid:5}{'PASS' if r.ok else 'FAIL':8}{r.title}")
    n_pass = sum(r.ok for r in results)
    print(f"\n{n_pass}/{len(results)} passed; transcript: {out}")
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
