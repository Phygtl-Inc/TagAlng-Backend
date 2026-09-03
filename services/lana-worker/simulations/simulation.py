"""
simulation.py
Drives a mock-user LLM agent through a real Lana conversation.
For each (persona, seed) pair:
  1. Seed the mock user's identity claims into Supabase
  2. Open a Lana session
  3. Loop: LLM generates a user turn → POST to Lana → capture response
  4. Complete the session
  5. Return a transcript dict consumed by evaluation.py
"""

import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

# Force UTF-8 console output — LLM responses can contain non-ASCII characters
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
from openai import OpenAI
from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Config — stub values replaced once Asjid provisions test accounts
# ---------------------------------------------------------------------------

LANA_BASE_URL = os.environ.get("LANA_BASE_URL", "http://localhost:8000")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

# Shared password for all 6 sim accounts — store in .env.local / GitHub secret, never commit.
# Read here only for reporting/back-compat; the actual login lives in sim_auth (which re-reads
# it at call time and falls back to the service-role magic link when it is empty).
SIM_PASSWORD = os.environ.get("SIM_PASSWORD", "")

MOCK_USER_MODEL = "gpt-4o"
MAX_TURNS = 8  # safety ceiling — most scenarios resolve in 3–5 turns

SIMULATIONS_DIR = Path(__file__).parent
if str(SIMULATIONS_DIR) not in sys.path:
    sys.path.insert(0, str(SIMULATIONS_DIR))
from local_guard import require_local  # noqa: E402
from sim_auth import jwt_for, sign_in  # noqa: E402

PERSONAS_PATH = SIMULATIONS_DIR / "personas.json"
SCENARIOS_PATH = SIMULATIONS_DIR / "scenarios.json"

# How many past runs to surface in the mock-user system prompt
PAST_RUN_CONTEXT_LIMIT = 3

# ---------------------------------------------------------------------------
# Pydantic models — personas.json
# ---------------------------------------------------------------------------

class IdentityClaim(BaseModel):
    concept: str
    label: str
    bucket: str
    confidence: float
    disclosure: str = "public"
    synonyms: list[str] = Field(default_factory=list)


class PersonaProfile(BaseModel):
    nickname: str
    home_block_id: str
    email: str
    user_id: str


class Persona(BaseModel):
    id: str
    name: str
    tech_comfort: str
    profile: PersonaProfile
    identity_claims: list[IdentityClaim] = Field(default_factory=list)
    character: str


# ---------------------------------------------------------------------------
# Pydantic models — scenarios.json
# ---------------------------------------------------------------------------

class Seed(BaseModel):
    label: str
    opening_line: str
    must_not: str

    @field_validator("opening_line")
    @classmethod
    def _opening_must_be_postable(cls, v: str) -> str:
        # Turn 1 is emitted VERBATIM to POST /lana/sessions/{id}/messages, whose body is
        # SendMessageRequest.message with min_length=1 (app/models.py). A blank opening_line is
        # therefore a guaranteed 422 -> the run raises -> runner.py buries it in `failures` and
        # still exits 0, i.e. a permanently unmeasured cell riding along in a green nightly.
        # Fail loudly at LOAD time instead, before any API/LLM spend.
        if not v.strip():
            raise ValueError(
                "opening_line must be non-blank — Lana's /messages endpoint requires min_length=1. "
                "True silence is not expressible against this API; use a content-free proxy turn."
            )
        return v


class Bucket(BaseModel):
    bucket: str
    description: str
    pass_criteria: str
    seeds: list[Seed]
    # Must-have buckets that gate a PR (critical-impact if they regress at launch: refusals/safety,
    # PII/privacy, core function, correct routing). The rest run only in the nightly. runner.py --pr
    # filters to these. Default False so any un-flagged bucket is nightly-only.
    pr_gate: bool = False


# ---------------------------------------------------------------------------
# Pydantic model — mock-user LLM structured output
# ---------------------------------------------------------------------------

class UserTurn(BaseModel):
    message: str = Field(
        description="The exact message this character sends to Lana right now."
    )
    reasoning: str = Field(
        description="One sentence: why this character says this given the conversation so far."
    )
    disengage: bool = Field(
        default=False,
        description="True if the character would naturally end the conversation at this point."
    )


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_personas() -> list[Persona]:
    data = json.loads(PERSONAS_PATH.read_text(encoding="utf-8"))
    return [Persona(**p) for p in data["personas"]]


def load_buckets() -> list[Bucket]:
    data = json.loads(SCENARIOS_PATH.read_text(encoding="utf-8"))
    return [Bucket(**b) for b in data["buckets"]]


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _jwt_for_persona(persona: Persona) -> str:
    """
    Logs in as the sim account and returns a fresh JWT. Tokens last ~1h — well within a run.

    Delegated to sim_auth.jwt_for so that a failure NAMES its cause. The raw call this used to
    make ended in `raise_for_status()`, and a Supabase password grant answers `400` identically
    for "wrong project", "SIM_PASSWORD empty/wrong" and "account was never seeded" — three
    mistakes with three different fixes, all of which are live at once on a fresh local stack.
    sim_auth also carries the service-role magic-link fallback for when SIM_PASSWORD is absent,
    which is the normal shape of a local stack (LOCAL_STACK.md).
    """
    return jwt_for(persona.profile.email)


# ---------------------------------------------------------------------------
# Claims seeding
# ---------------------------------------------------------------------------

def _effective_user_id(persona: Persona) -> str:
    """The user_id of the account we ACTUALLY authenticate as — not the one personas.json asserts.

    WHY THIS EXISTS. Every entry point logs in BY EMAIL (`_jwt_for_persona` -> sim_auth), while
    claims were seeded to `persona.profile.user_id` from personas.json. On the shared dev project
    those happen to be the same row, so the discrepancy was invisible. On a LOCAL stack they are
    not: `supabase/seed_sim_accounts.sql` creates the personas with its own fixed UUIDs
    (51000001-0001-4000-8000-000000000001 …) while personas.json carries the dev project's ids
    (3ad1c73f-… etc.). Seeding by the json id and conversing as the email id means the claims land
    on one user and the conversation runs as another — every claim-dependent scenario then measures
    an empty profile, and it does so SILENTLY, scoring the run as if it were real.

    Resolving from the authenticated session removes the class entirely: whoever we can log in as
    is definitionally the user whose claims matter. personas.json's id becomes a fallback for the
    offline/no-credential path, and a mismatch is reported rather than swallowed.
    """
    declared = persona.profile.user_id
    try:
        session = sign_in(persona.profile.email)
    except Exception as exc:  # offline / no creds — keep the old behaviour, don't fail the run here
        print(f"  [auth] could not resolve {persona.id}'s user_id from the session "
              f"({type(exc).__name__}); falling back to personas.json {declared}")
        return declared
    actual = str(((session or {}).get("user") or {}).get("id") or "").strip()
    if not actual:
        return declared
    if actual != declared:
        # Loud on purpose: on a local stack this is EXPECTED (different seed UUIDs) and correct to
        # follow; on dev it would mean personas.json has drifted from the real accounts.
        print(f"  [auth] {persona.id}: authenticated as {actual}, personas.json says {declared} — "
              f"using the authenticated id (see _effective_user_id)")
    return actual


def _seed_claims(persona: Persona) -> None:
    """
    Wipe then re-insert the persona's identity claims via Supabase REST.
    Uses the service role key to bypass RLS — runs server-side only, never in the browser.
    Ensures each run starts from a known clean state with no drift from prior runs.
    P6 (Diane) has zero claims by design — the DELETE still runs to clear any accumulation.
    """
    user_id = _effective_user_id(persona)
    # LOCAL-ONLY GATE. The DELETE below removes EVERY user_identity_claims row for this user,
    # and the sim personas live in the SHARED dev project — a run pointed at dev silently wipes
    # whatever a teammate seeded, and succeeds while doing it. require_local refuses unless
    # SUPABASE_URL is a local stack (escape hatch: SIM_ALLOW_NONLOCAL_WRITES=1, which prints a
    # banner). See simulations/LOCAL_STACK.md.
    require_local(f"DELETE + reseed user_identity_claims for {persona.id} ({user_id})")
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }

    with httpx.Client(timeout=15) as http:
        # Wipe existing claims for this user
        resp = http.delete(
            f"{SUPABASE_URL}/rest/v1/user_identity_claims",
            params={"user_id": f"eq.{user_id}"},
            headers=headers,
        )
        resp.raise_for_status()
        print(f"  [claims] wiped existing claims for {persona.id}")

        if not persona.identity_claims:
            print(f"  [claims] {persona.id} has zero claims by design — skipping insert")
            return

        rows = [
            {
                "user_id": user_id,
                "concept": c.concept,
                "label": c.label,
                "bucket": c.bucket,
                "confidence": c.confidence,
                "disclosure": c.disclosure,
                "synonyms": c.synonyms,
            }
            for c in persona.identity_claims
        ]
        resp = http.post(
            f"{SUPABASE_URL}/rest/v1/user_identity_claims",
            json=rows,
            headers=headers,
        )
        resp.raise_for_status()
        print(f"  [claims] seeded {len(rows)} claims for {persona.id}")


# ---------------------------------------------------------------------------
# Past-run context (corpus feedback loop)
# ---------------------------------------------------------------------------

def _fetch_past_runs(bucket: str, seed_label: str) -> list[dict[str, Any]]:
    """
    Queries Supabase for the most recent PAST_RUN_CONTEXT_LIMIT runs of this
    bucket + seed_label combination, returning lightweight dicts with just the
    fields needed to inform the mock-user system prompt.

    Returns [] if Supabase creds are absent (first run, dry run, or local dev
    without creds) — the prompt degrades gracefully with no history section.
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return []

    try:
        with httpx.Client(timeout=10) as http:
            resp = http.get(
                f"{SUPABASE_URL}/rest/v1/simulations",
                params={
                    "select": "run_id,weighted_score,scores_json,judge_summary,created_at",
                    "bucket": f"eq.{bucket}",
                    "seed_label": f"eq.{seed_label}",
                    "order": "created_at.desc",
                    "limit": str(PAST_RUN_CONTEXT_LIMIT),
                },
                headers={
                    "apikey": SUPABASE_SERVICE_ROLE_KEY,
                    "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                },
            )
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as exc:
        print(f"  [history] fetch failed, running without past context: {exc}")
        return []


def _format_past_runs(past_runs: list[dict[str, Any]]) -> str:
    """
    Formats past run records into a concise block for injection into the
    mock-user system prompt. Empty string if no history.
    """
    if not past_runs:
        return ""

    lines = ["PAST RUNS ON THIS SCENARIO (most recent first)"]
    for r in past_runs:
        date = (r.get("created_at") or "")[:10]
        score = r.get("weighted_score", "?")
        summary = r.get("judge_summary") or "no summary"

        failed_axes = []
        for axis in r.get("scores_json") or []:
            if axis.get("verdict") in ("SOFT_FAIL", "HARD_FAIL"):
                failed_axes.append(f"{axis['axis']}={axis['verdict']}")

        failure_note = f" | failures: {', '.join(failed_axes)}" if failed_axes else " | all axes PASS"
        lines.append(f"- {date} score={score:.3f}{failure_note}")
        lines.append(f"  {summary}")

    lines += [
        "",
        "Use this history to vary your approach:",
        "- If past runs mostly passed: come at the scenario from a different angle so you don't replay the same conversation.",
        "- If past runs exposed a failure: probe that same weak spot again, but phrase it differently to confirm whether it is a systematic gap or a one-off.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Lana API calls
# ---------------------------------------------------------------------------

def _create_session(jwt: str, client: httpx.Client) -> str:
    resp = client.post(
        f"{LANA_BASE_URL}/lana/sessions",
        json={"purpose": "lana", "force_new": True},
        headers={"Authorization": f"Bearer {jwt}"},
    )
    resp.raise_for_status()
    return resp.json()["session_id"]


def _send_message(
    session_id: str, message: str, jwt: str, client: httpx.Client
) -> dict[str, Any]:
    resp = client.post(
        f"{LANA_BASE_URL}/lana/sessions/{session_id}/messages",
        json={"message": message},
        headers={"Authorization": f"Bearer {jwt}"},
    )
    resp.raise_for_status()
    return resp.json()


def _complete_session(session_id: str, jwt: str, client: httpx.Client) -> None:
    client.post(
        f"{LANA_BASE_URL}/lana/sessions/{session_id}/complete",
        json={"force": True, "publish": False},
        headers={"Authorization": f"Bearer {jwt}"},
    )


# ---------------------------------------------------------------------------
# Mock-user LLM
# ---------------------------------------------------------------------------

def _build_system_prompt(
    persona: Persona,
    bucket: Bucket,
    seed: Seed,
    past_runs_block: str = "",
) -> str:
    claims_text = "\n".join(
        f"  - {c.label} ({c.bucket}, confidence {c.confidence})"
        for c in persona.identity_claims
    ) or "  (none — you are a zero-claims user, Lana knows nothing about you yet)"

    history_section = f"\n{past_runs_block}\n" if past_runs_block else ""

    language_rule = (
        "- This scenario specifically tests a mid-conversation language switch. After turn 2, switch to your character's native language and stay in it."
        if "locale switch" in seed.label
        else "- Conduct this entire conversation in English only, regardless of your character's background. Do not switch languages."
    )

    return f"""You are roleplaying as a real user in a chat with Lana, an AI neighborhood concierge.

CHARACTER
Name: {persona.profile.nickname}
Tech comfort: {persona.tech_comfort}
Personality: {persona.character}

YOUR IDENTITY (what Lana has on file about you)
{claims_text}

YOUR GOAL IN THIS CONVERSATION
You want exactly one thing: {seed.opening_line}
Stay on this topic. Do not drift to other requests or topics no matter what Lana says.

HARD CONSTRAINT — never do this in your messages:
{seed.must_not}

CONTEXT (background only — do not repeat this to Lana)
This tests: {bucket.description}
What success looks like: {bucket.pass_criteria}
{history_section}
ABSOLUTE RULES — these override everything else. Violating any of them corrupts the run.
1. You are ONE PERSON in a live chat: {persona.profile.nickname}. Output ONLY the words she would
   type into the message box, nothing else.
2. NEVER write or imagine Lana's side. Do not write "Lana:", do not quote or paraphrase a reply she
   has not actually sent, and do not continue as if she answered something she did not.
3. Respond ONLY to what Lana ACTUALLY said in the messages above. If she has not replied yet, do
   not act as though she has. Never open a message with "Oh, I understand", "Thanks for that",
   "I see", or any acknowledgement of a reply that is not literally present above.
4. NEVER write a script or dialogue for both sides, stage directions (*sighs*, [pauses]), narration
   about what "the user" does, or commentary about what Lana should do next.
5. NEVER mention testing, simulation, roleplay, personas, scenarios, prompts, or that you are an AI.
6. Output plain chat text only — no speaker labels, no turn numbers, no markdown headers, no quotes
   around your whole message.

RULES
- Keep messages short — this is a chat app, not an email.
- React to what Lana actually said. If she helps, engage. If she stalls or deflects, push back once or twice then give up naturally.
- If the conversation loops or Lana clearly can't help, set disengage=true. Do NOT repeat the same message twice.
- If Lana has explicitly said she CANNOT do something two or more times (e.g. "that isn't something I can do"), set disengage=true. A real user would give up and move on.
- If Lana is asking clarifying questions to understand your request, keep engaging and answer them — she is trying to help, not refusing.
- Your opening message has ALREADY been sent for you (it appears as your first message above). Never
  re-send it, and never write a fresh opening — continue the conversation from where it actually is.
{language_rule}"""


def _generate_user_turn(
    openai_client: OpenAI,
    system_prompt: str,
    history: list[dict[str, str]],
    correction: str | None = None,
) -> UserTurn | None:
    """The next persona turn, or None when the model produced nothing usable.

    `.parsed` is None on a refusal or a failed structured parse, and the model occasionally returns
    a blank message. Both were previously fatal: `.parsed.message` raised
    "'NoneType' object has no attribute 'message'", and a blank message was POSTed to Lana, which
    rejects it (min_length=1). Returning None instead lets the caller end the conversation cleanly
    and mark it — an aborted run must be visible, never a silently short transcript.
    """
    messages = [{"role": "system", "content": system_prompt}] + history
    if correction:
        messages.append({"role": "system", "content": correction})
    completion = openai_client.beta.chat.completions.parse(
        model=MOCK_USER_MODEL,
        messages=messages,
        response_format=UserTurn,
        temperature=0.9,
    )
    parsed = completion.choices[0].message.parsed
    if parsed is None or not (parsed.message or "").strip():
        return None
    return parsed


# ---------------------------------------------------------------------------
# Out-of-character (OOC) guard for the mock user
# ---------------------------------------------------------------------------
# WHY THIS EXISTS: measured across the 102 transcripts of run_2026-08-14T14-45-21Z, 14 runs (13.7%)
# contained at least one USER turn written in Lana's voice — in the worst case the persona's message
# was one of Lana's own canned replies, verbatim. A human reviewer had already labelled this class
# "mock user got confused and start roleplaying both sides", and the judge then scored the resulting
# conversation as if it were real. That is worse than a wasted run: it silently corrupts the failure
# rate, and such a transcript can become SFT-eligible.
#
# The `ABSOLUTE RULES` block already forbids this in six numbered prohibitions and the drift still
# happens, so prompting alone cannot close it — instruction adherence decays as context grows. This
# guard runs BEFORE the message is sent to Lana, so an OOC turn never enters the conversation.
#
# TWO TIERS, deliberately. A blunt detector would invalidate good runs: "let me know if you have any
# links for that" is ordinary user speech. Only signals that are *impossible* for a persona to utter
# are treated as hard.
#   HARD  -> regenerate the turn once; if it recurs, the run is INVALID and is not scored.
#   SOFT  -> recorded on the turn for triage; never blocks, never invalidates.

# Actions only Lana can perform in this product. A persona cannot offer to poll the neighborhood.
_OOC_HARD_PATTERNS: list[tuple[str, str]] = [
    (r"\b(?:would you like me to|want me to|shall i|should i)\s+(?:ask|poll|check with|reach out to|"
     r"put out|see if|find|notify|text|introduce|set (?:it |this )?up)\b", "offers-lana-action"),
    # TUNED against the 102-run corpus: an earlier version accepted a bare "find someone", which
    # fired on "Thanks anyway! I'll see if I can find someone on my own" — a persona declining help
    # and doing it themselves, i.e. exactly right. The offer must be made ON THE OTHER PARTY'S
    # BEHALF to be Lana's voice.
    (r"\bi (?:can|could|will|'ll) (?:help you|assist you|keep an ear out|keep my ear out|"
     r"put out a request|ask (?:your |the )?neighbou?rs|find you (?:someone|neighbou?rs))\b",
     "speaks-as-lana"),
    (r"\bi(?:'ve| have) noted your request\b", "speaks-as-lana"),
    (r"\bthat'?s not something i can (?:help|assist)\b", "refuses-as-lana"),
    (r"^\s*(?:lana|assistant|user)\s*:", "speaker-label"),
]

# Stage directions are matched case-SENSITIVELY and separately: `[Your City]` is a placeholder in a
# quoted group name, not narration, and a case-insensitive bracket rule flagged it. Real stage
# directions are lowercase verbs.
_OOC_STAGE_DIRECTION_RE = re.compile(
    r"\*(?:sigh|sighs|pause|pauses|laugh|laughs|nods|shrugs|smiles|thinking)[a-z ]*\*"
    r"|\[(?:sigh|sighs|pause|pauses|laugh|laughs|nods|shrugs|smiles|thinking)[a-z ]*\]"
)

# Service register a real person *might* use — informative, never blocking.
_OOC_SOFT_PATTERNS: list[tuple[str, str]] = [
    (r"\blet me know if\b", "service-register"),
    (r"\bfeel free to (?:reach|ask|let)\b", "service-register"),
    (r"\bhappy to help\b", "service-register"),
    (r"\bis there something specific\b", "service-register"),
]

_OOC_ECHO_MIN_WORDS = 8  # shortest run of words worth calling an echo rather than a coincidence


def _word_ngrams(text: str, n: int) -> set[str]:
    words = re.findall(r"[a-z0-9']+", text.lower())
    return {" ".join(words[i:i + n]) for i in range(len(words) - n + 1)}


def ooc_violations(message: str, prior_lana_replies: list[str]) -> tuple[list[str], list[str]]:
    """Return (hard, soft) OOC reasons for a candidate mock-user message.

    Deterministic and cheap — no LLM. Reusable by selftest.py and by any retroactive scan of
    stored transcripts.
    """
    hard: list[str] = []
    soft: list[str] = []

    for pattern, label in _OOC_HARD_PATTERNS:
        if re.search(pattern, message, re.IGNORECASE | re.MULTILINE):
            hard.append(label)
    if _OOC_STAGE_DIRECTION_RE.search(message):
        hard.append("stage-direction")
    for pattern, label in _OOC_SOFT_PATTERNS:
        if re.search(pattern, message, re.IGNORECASE):
            soft.append(label)

    # Verbatim echo of something Lana already said. This is the unambiguous signal: a persona
    # reproducing Lana's own sentence is not paraphrase, it is role bleed.
    msg_grams = _word_ngrams(message, _OOC_ECHO_MIN_WORDS)
    if msg_grams:
        for reply in prior_lana_replies:
            if msg_grams & _word_ngrams(reply, _OOC_ECHO_MIN_WORDS):
                hard.append("echoes-lana-verbatim")
                break

    return sorted(set(hard)), sorted(set(soft))


_OOC_CORRECTION = (
    "STOP. Your previous draft was written in Lana's voice — you offered to do something only Lana "
    "can do, or repeated her words. You are the PERSON messaging Lana, not Lana. Rewrite it as one "
    "short chat message from your own point of view: react to what Lana just said, and ask for or "
    "say what YOU want. Never offer to contact neighbors, run a search, or make an introduction."
)


# ---------------------------------------------------------------------------
# Core simulation loop
# ---------------------------------------------------------------------------

def run(persona: Persona, bucket: Bucket, seed: Seed) -> dict[str, Any]:
    """
    Runs one (persona × seed) simulation.
    Returns a transcript dict ready for evaluation.py.
    """
    run_id = str(uuid.uuid4())
    print(f"\n[sim] {run_id} | {persona.id} × {bucket.bucket}/{seed.label}")

    _seed_claims(persona)

    past_runs = _fetch_past_runs(bucket.bucket, seed.label)
    past_runs_block = _format_past_runs(past_runs)
    if past_runs:
        print(f"  [history] {len(past_runs)} past run(s) injected into system prompt")

    jwt = _jwt_for_persona(persona)
    openai_client = OpenAI(api_key=OPENAI_API_KEY)
    system_prompt = _build_system_prompt(persona, bucket, seed, past_runs_block)

    turns: list[dict[str, Any]] = []
    # OpenAI message history for continuity across the mock user's turns
    history: list[dict[str, str]] = []

    with httpx.Client(timeout=120) as http:
        session_id = _create_session(jwt, http)
        print(f"  [lana] session {session_id}")

        last_user_message: str | None = None
        repeat_count = 0
        invalid_reason: str | None = None
        ooc_retries = 0

        for turn_num in range(1, MAX_TURNS + 1):
            if turn_num == 1:
                # Turn 1 is KNOWN BY CONSTRUCTION — emit the seed's opening line verbatim rather
                # than asking the model to reproduce it.
                # WHY: measured across the 787 stored runs, 63% did NOT open with the seed's
                # opening line despite the prompt demanding it verbatim. The model instead opened
                # mid-conversation ("Oh, I understand, but...") — i.e. replying to a Lana turn that
                # never happened. A human reviewer labelled exactly this "mock user got confused and
                # start roleplaying both sides", and those runs were then mis-scored by the judge.
                # Generating turn 1 deterministically removes that entire failure class, makes every
                # run start from the identical stimulus (a precondition for comparing runs at all),
                # and saves one LLM call per run.
                user_turn = UserTurn(
                    message=seed.opening_line,
                    reasoning="seed opening line, emitted verbatim (not model-generated)",
                    disengage=False,
                )
                turn_hard, turn_soft = [], []
            else:
                # OOC GUARD — validate BEFORE the message reaches Lana, so a corrupted turn never
                # enters the conversation. One regeneration, then the run is abandoned as invalid.
                prior_replies = [t["lana_reply"] for t in turns if t.get("lana_reply")]
                user_turn = _generate_user_turn(openai_client, system_prompt, history)
                if user_turn is None:
                    print(f"  [sim] no usable user turn at {turn_num} (refusal or blank) — ending run")
                    invalid_reason = f"mock user produced no usable message at turn {turn_num}"
                    break
                turn_hard, turn_soft = ooc_violations(user_turn.message, prior_replies)
                if turn_hard:
                    print(f"  [ooc] turn {turn_num} rejected ({', '.join(turn_hard)}) — regenerating")
                    user_turn = _generate_user_turn(
                        openai_client, system_prompt, history, correction=_OOC_CORRECTION
                    )
                    if user_turn is None:
                        print(f"  [ooc] retry at turn {turn_num} produced nothing usable — INVALID")
                        invalid_reason = (
                            f"mock user produced no usable message on the OOC retry at turn {turn_num}"
                        )
                        ooc_retries += 1
                        break
                    turn_hard, turn_soft = ooc_violations(user_turn.message, prior_replies)
                    if turn_hard:
                        # Do NOT send it. An OOC turn produces a conversation that looks real,
                        # scores like a real one, and measures nothing.
                        print(f"  [ooc] turn {turn_num} STILL out of character "
                              f"({', '.join(turn_hard)}) — marking run INVALID")
                        invalid_reason = (
                            f"mock user out of character at turn {turn_num} after 1 retry: "
                            f"{', '.join(turn_hard)}"
                        )
                        ooc_retries += 1
                        break
                    ooc_retries += 1
            print(f"  [user {turn_num}] {user_turn.message[:100]}")

            # Break out if the mock user is stuck repeating itself
            if user_turn.message.strip() == (last_user_message or "").strip():
                repeat_count += 1
                if repeat_count >= 2:
                    print(f"  [sim] mock user repeated same message {repeat_count}x — forcing disengage")
                    break
            else:
                repeat_count = 0
            last_user_message = user_turn.message

            t0 = time.monotonic()
            lana_resp = _send_message(session_id, user_turn.message, jwt, http)
            latency_ms = round((time.monotonic() - t0) * 1000)
            lana_reply = lana_resp.get("assistant_message", "")
            routing = lana_resp.get("routing") or {}
            print(f"  [lana {turn_num}] {lana_reply[:100]}")

            turns.append({
                "turn_number": turn_num,
                "user_message": user_turn.message,
                "user_reasoning": user_turn.reasoning,
                "lana_reply": lana_reply,
                "latency_ms": latency_ms,
                "intent_class": routing.get("intent_class"),
                "intent_confidence": routing.get("confidence"),
                "tool_called": routing.get("tool_called"),
                "outcome": routing.get("outcome"),
                # POLICY DECISION FIELDS — real as of app/policy/decide.py:59 (routing_dict).
                # These cost nothing (already in the response) and were being discarded. Until
                # decide_turn shipped there was no `kind` and no rationale to capture, so the
                # suite inferred both; now they are authoritative. `outcome == "decide_turn"`
                # marks a turn the policy engine decided, as opposed to the legacy path — which
                # matters because a decision-quality finding is only meaningful on the former.
                # `distress_turn` is load-bearing for false-positive control: _apply_distress_gate
                # clears chips and suppresses task-pushing DELIBERATELY, so a check that does not
                # know about it will flag correct behaviour.
                "decision_kind": routing.get("kind"),
                "decision_why": routing.get("why"),
                "goal_id": routing.get("goal_id"),
                "defer_goal_id": routing.get("defer_goal_id"),
                "pending_action": routing.get("pending_action"),
                "distress_turn": bool(routing.get("distress_turn")),
                "ui_intent": lana_resp.get("ui_intent"),
                "ready_to_complete": lana_resp.get("ready_to_complete", False),
                # Fuller raw-turn fields (mirrors qa/run1/harness/sim.mjs's extraction) — lets
                # the same mechanical checks (verify-wall, ZIP-loop, NY-bleed, leaks) that run
                # against qa/run1 transcripts also run against these Python-generated ones.
                # FULL rows, not just labels (widened 2026-09-02). UiActionRow is
                # {id, label, message, style, intro_id, peer_user_id} and its docstring is
                # explicit: "Tap → POST `message` to Lana (same contract as typing in chat)."
                # So a pill tap is drivable from this harness — send `message` as the next user
                # turn — but only if we keep it. Labels alone made taps unreachable, which is
                # what a re-anchor pill check needs ("tapping it must land on results, not a
                # second empty"). `label` stays first for the existing lingo scan over chip text.
                "ui_actions": [
                    {"id": a.get("id"), "label": a.get("label"), "message": a.get("message")}
                    for a in (lana_resp.get("ui_actions") or [])
                    if isinstance(a, dict)
                ],
                "activity_previews": [
                    {
                        "title": p.get("title"),
                        "when": p.get("starts_label") or p.get("starts_at"),
                        "where": p.get("venue_name"),
                    }
                    for p in (lana_resp.get("activity_previews") or [])
                ],
                # GOOGLE PLACES surfaced as tappable cards (app/main.py:1223 ->
                # _place_suggestions_from_ctx, fed by ctx["google_place_suggestions"]). Captured
                # 2026-08-25; it had been on the wire and discarded.
                #
                # Why it matters beyond completeness: qa_analyze._turn_is_sourced decides whether a
                # turn had ANY data behind it, and a named venue on a turn with no source is the
                # fabrication signal. Google-sourced places are a real source, so dropping this
                # field made the runtime look emptier than it was. `community` marks rows that came
                # from the user's own circles rather than Google — the surface groups on it
                # ("From your circles" vs "From Google · not a neighbor vouch"), so it is kept.
                "place_suggestions": [
                    {"name": p.get("name"), "community": bool(p.get("community"))}
                    for p in (lana_resp.get("place_suggestions") or [])
                    if isinstance(p, dict)
                ],
                "event_draft": (
                    {
                        "title": (lana_resp.get("event_draft") or {}).get("title"),
                        "starts_at": (lana_resp.get("event_draft") or {}).get("starts_at"),
                        "location": (lana_resp.get("event_draft") or {}).get("venue_name"),
                    }
                    if lana_resp.get("event_draft")
                    else None
                ),
                "peer_matches": len(lana_resp.get("peer_matches") or []),
                "signal_saved": lana_resp.get("signal_saved"),
                "requires_phone_verification": lana_resp.get("requires_phone_verification", False),
                # Soft OOC signals: recorded for triage, never blocking (see ooc_violations).
                "ooc_soft_flags": turn_soft,
            })

            # ROLE MAPPING: the mock user is the ASSISTANT of this sub-conversation — it is the
            # party this model is generating. Lana is its "user". Labelling them the other way round
            # (persona=user, Lana=assistant) asks the model to emit an assistant turn directly after
            # Lana's assistant turn, which invites it to continue in LANA's voice — a mechanical
            # contributor to the OOC drift the guard above catches.
            # Chronological: the persona speaks, then Lana answers. So the persona's message is
            # appended first, and the history always ends on Lana — leaving the model to produce
            # the next `assistant` turn, which is the persona's reply to what Lana just said.
            history.append({"role": "assistant", "content": user_turn.message})
            history.append({"role": "user", "content": lana_reply})

            if user_turn.disengage:
                print(f"  [sim] character disengaged at turn {turn_num}")
                break
            if lana_resp.get("ready_to_complete"):
                print(f"  [sim] Lana signalled ready_to_complete at turn {turn_num}")
                break

        _complete_session(session_id, jwt, http)

    transcript = {
        "run_id": run_id,
        "persona_id": persona.id,
        "persona_name": persona.name,
        "bucket": bucket.bucket,
        "seed_label": seed.label,
        "pass_criteria": bucket.pass_criteria,
        "must_not": seed.must_not,
        "turns": turns,
        "turn_count": len(turns),
        # Harness integrity, NOT a verdict on Lana. False means the conversation itself is not a
        # valid stimulus, so scoring it would measure the harness, not the product.
        "harness_valid": invalid_reason is None,
        "invalid_reason": invalid_reason,
        "ooc_retries": ooc_retries,
    }

    if invalid_reason:
        print(f"  [sim] INVALID — {invalid_reason} ({len(turns)} usable turns, not scored)")
    else:
        print(f"  [sim] done — {len(turns)} turns"
              + (f" ({ooc_retries} OOC retry/retries)" if ooc_retries else ""))
    return transcript
