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
import uuid
from pathlib import Path
from typing import Any

import httpx
from openai import OpenAI
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Config — stub values replaced once Asjid provisions test accounts
# ---------------------------------------------------------------------------

LANA_BASE_URL = os.environ.get("LANA_BASE_URL", "http://localhost:8000")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

# Shared password for all 6 sim accounts — store in .env.local / GitHub secret, never commit
SIM_PASSWORD = os.environ.get("SIM_PASSWORD", "")

MOCK_USER_MODEL = "gpt-4o"
MAX_TURNS = 8  # safety ceiling — most scenarios resolve in 3–5 turns

SIMULATIONS_DIR = Path(__file__).parent
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


class Bucket(BaseModel):
    bucket: str
    description: str
    pass_criteria: str
    seeds: list[Seed]


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
    Does a password-grant login for the sim account and returns a fresh JWT.
    Tokens are valid for 1 hour — well within a single run's lifetime.
    Credentials: email from persona.profile, shared SIM_PASSWORD from env.
    """
    resp = httpx.post(
        f"{SUPABASE_URL}/auth/v1/token?grant_type=password",
        headers={"apikey": SUPABASE_ANON_KEY, "Content-Type": "application/json"},
        json={"email": persona.profile.email, "password": SIM_PASSWORD},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# Claims seeding
# ---------------------------------------------------------------------------

def _seed_claims(persona: Persona) -> None:
    """
    Wipe then re-insert the persona's identity claims via Supabase REST.
    Uses the service role key to bypass RLS — runs server-side only, never in the browser.
    Ensures each run starts from a known clean state with no drift from prior runs.
    P6 (Diane) has zero claims by design — the DELETE still runs to clear any accumulation.
    """
    user_id = persona.profile.user_id
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

    return f"""You are roleplaying as a real user testing an AI neighborhood concierge called Lana.

CHARACTER
Name: {persona.profile.nickname}
Tech comfort: {persona.tech_comfort}
Personality: {persona.character}

YOUR IDENTITY (what Lana has on file about you)
{claims_text}

SCENARIO BEING TESTED
Bucket: {bucket.bucket}
What this tests: {bucket.description}
What Lana must achieve: {bucket.pass_criteria}

YOUR OPENING LINE FOR THIS SCENARIO
"{seed.opening_line}"

HARD CONSTRAINT — never do this in your messages:
{seed.must_not}
{history_section}
RULES
- Stay in character at all times. Reply only as this user would naturally speak.
- Keep messages short — this is a chat window, not an email.
- React authentically to what Lana says. If Lana is helpful, lean in. If Lana stalls or misses the point, show it.
- If the character would realistically give up or walk away, set disengage=true.
- Do not mention that you are testing, roleplaying, or following instructions.
- Your very first message must be the opening line above, verbatim."""


def _generate_user_turn(
    openai_client: OpenAI,
    system_prompt: str,
    history: list[dict[str, str]],
) -> UserTurn:
    messages = [{"role": "system", "content": system_prompt}] + history
    completion = openai_client.beta.chat.completions.parse(
        model=MOCK_USER_MODEL,
        messages=messages,
        response_format=UserTurn,
        temperature=0.9,
    )
    return completion.choices[0].message.parsed


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

    with httpx.Client(timeout=30) as http:
        session_id = _create_session(jwt, http)
        print(f"  [lana] session {session_id}")

        for turn_num in range(1, MAX_TURNS + 1):
            user_turn = _generate_user_turn(openai_client, system_prompt, history)
            print(f"  [user {turn_num}] {user_turn.message[:100]}")

            lana_resp = _send_message(session_id, user_turn.message, jwt, http)
            lana_reply = lana_resp.get("assistant_message", "")
            routing = lana_resp.get("routing") or {}
            print(f"  [lana {turn_num}] {lana_reply[:100]}")

            turns.append({
                "turn_number": turn_num,
                "user_message": user_turn.message,
                "user_reasoning": user_turn.reasoning,
                "lana_reply": lana_reply,
                "intent_class": routing.get("intent_class"),
                "intent_confidence": routing.get("confidence"),
                "tool_called": routing.get("tool_called"),
                "ui_intent": lana_resp.get("ui_intent"),
                "ready_to_complete": lana_resp.get("ready_to_complete", False),
            })

            history.append({"role": "user", "content": user_turn.message})
            history.append({"role": "assistant", "content": lana_reply})

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
    }

    print(f"  [sim] done — {len(turns)} turns")
    return transcript
