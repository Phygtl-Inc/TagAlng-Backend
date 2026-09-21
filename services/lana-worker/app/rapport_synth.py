"""Backfill rapport gaps from what we already know.

Rapport gaps are normally opened per-turn from a chat message (the extractor's warm
follow-up). Once a user stops chatting, that queue drains and the home "By the way…"
tile goes silent — nothing left on the plate to keep building their profile.

This module refills the plate: given their existing identity claims, it asks the model
for a couple of fresh follow-ups that DEEPEN the profile with matchable facets, then
opens them as semantic gaps. Called by the ranker only when no gap is queued (and the
cadence cap is already clear), so it stays off the home-render hot path in the common
case. One Flash-class call; silent no-op on any failure.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from app.auth import service_client
from app.rapport_gaps import open_semantic_gap, recent_gap_questions
from app.vec_util import to_pgvector

logger = logging.getLogger(__name__)

# Coverage is now exact (a question records which claim it's about — deepens_concept). This knob
# only controls how tightly answer-spawned sub-claims ("runs mornings") fold into their parent
# theme ("running") via claim↔claim similarity, so they don't resurface as new topics. Env-tunable.
_CLUSTER_SIMILARITY = float(os.environ.get("LANA_RAPPORT_CLUSTER_SIMILARITY", "0.8"))

# Keep the plate small — one to show plus one in reserve is plenty. A larger buffer just
# risks stale questions the user never reaches. _MAX_NEW caps a single synth call; _BUFFER_TARGET
# is how many OPEN gaps we try to keep queued ahead so the tile is never caught empty.
_MAX_NEW = 2
# Five, not two. The repetition windows in rapport_ranker hold a served gap back for 24h and
# a skipped one for 72h, which only works if there is something else to show — ration
# repetition, never availability. A floor of two plus those windows is the September empty-card
# bug again, which is why the floor moves first and the windows read from it.
_BUFFER_TARGET = 5

# Lateral seeding stops when there is nothing left worth asking — which is SUPPLY, not
# bucket coverage. A bucket gate was tried at 5 of the 7 CLAIM_BUCKETS and measured wrong:
# a handful of varied answers lands in 5-6 buckets, so a real user went permanently silent
# after ~7 answers with 8 unused local-supply concepts still available. 7 buckets is far too
# coarse a taxonomy to mean "covered the map". Kept only as a backstop at FULL coverage.
from app.rapport_gap_tree import CLAIM_BUCKETS

_BREADTH_COVERED = len(CLAIM_BUCKETS)

# Coalesce burst calls: the ranker attempts a backfill whenever the plate is empty, and a
# richly-profiled user can yield no new questions — without this, rapid home re-renders would
# each fire an LLM call. In-process only (best-effort across worker instances), keyed by user.
_SYNTH_COOLDOWN_S = 120.0
_last_attempt: dict[str, float] = {}
# Seeding keeps its OWN budget. Both production refill paths run
# synthesize_gaps_from_claims and then, only if it made nothing, seed_cold_start —
# rapport_synth.ensure_gap_buffer and rapport_ranker._backfill_from_claims. Sharing one
# map meant the synth that just ran stamped the key milliseconds before seeding checked
# it, so seed_cold_start returned 0 at the guard and NEVER reached a single tier in
# production. The guard exists to stop repeated LLM bursts per user; synth and seed each
# make their own call, so a budget each preserves that and unblocks the lateral path.
_last_seed_attempt: dict[str, float] = {}


def _cooling_down(user_id: str, store: dict[str, float] | None = None) -> bool:
    store = _last_attempt if store is None else store
    now = time.monotonic()
    last = store.get(user_id)
    if last is not None and (now - last) < _SYNTH_COOLDOWN_S:
        return True
    # Prune stale entries before recording — keeps the map bounded on a long-lived worker.
    if len(store) > 5000:
        for uid in [u for u, t in store.items() if now - t >= _SYNTH_COOLDOWN_S]:
            store.pop(uid, None)
    store[user_id] = now
    return False

SYNTH_PROMPT = """You are Lana, a warm neighborhood concierge in a block-based neighborhood app where \
neighbors connect with nearby neighbors. You keep their profile growing by asking ONE well-timed \
follow-up at a time on their home screen ("By the way…").

Below are THREADS TO ASK ABOUT — identity threads of theirs we have NOT covered yet — plus the questions \
you've ALREADY ASKED. Propose up to {max_new} NEW follow-up questions, each SHARPENING a DIFFERENT one of \
the uncovered threads. Spread across threads — do NOT ask two questions about the same thread, and do NOT \
ask about anything already covered by the ALREADY ASKED list.

A good question adds a CONNECTION-MATCHABLE facet — something that helps them MEET or RELATE to nearby \
neighbors: shared activities/hobbies, family/life stage, local spots they go, cultural or community ties, \
their weekly rhythm, skill/level, doing-it-with-others, kids' involvement, teach-vs-learn.

Every question must clear THREE bars — drop any that fail even one:
1. RELEVANT — it sharpens one of the UNCOVERED THREADS listed below, not a generic survey question out of nowhere.
2. IMPORTANT — the answer meaningfully helps match THEM to nearby neighbors. Apply the test: "would this change who they connect with on the block?" If not, drop it.
3. FRESH — you have NOT asked it before. Scan ALREADY ASKED and skip anything you've asked or would just be rewording.

HARD RULES on shape:
- NEVER a yes/no question. If it starts with "Do you", "Are you", "Have you", "Would you" — rewrite it to ask for a SPECIFIC thing (a time, a place, a genre, an activity, a cadence) or drop it. Bad: "Do you and your spouse share any hobbies?" Good: "What's a spot nearby you love for a run?"
- Keep THE USER the subject. You MAY sharpen their OWN life-stage facet (e.g. "married" → how long they've been married; "new to the block" → how long they've lived here). But NEVER ask about their partner or the relationship itself ("do you and your spouse…", "what does your husband…") — that's not a facet neighbors match on. Prefer their own activities, local spots, rhythm, and community ties; if a thread has no matchable angle left, skip it.
- The answer should be a concrete facet another neighbor could share — a place, a time, a genre, an activity, a level — not a sentiment.

FORBIDDEN — never ask an opinion, feeling, or origin-story question (anything asking why, how you \
started, what you enjoy/love most, or what "caught your interest"); those add no matchable facet. Never \
ask about generic consumer/brand/device/product preferences (no neighbor connects over that). NEVER \
touch a sensitive topic — health/medical, grief, divorce/relationship trouble, money/debt, \
legal/immigration, mental health.

Write ONLY the question itself — NO "By the way" prefix, no greeting. Short (<120 chars), warm, OPEN, \
and reference what you know. Write question, teaser, and label in ENGLISH regardless of the language of \
any quotes below — questions are stored English-canonical and AI-rendered into the user's preferred \
language at display time. Quality over quantity: if only one question clears every bar, return just \
one; if none do, return an empty list — silence beats a filler, yes/no, or repeat question.

SPECIAL THREAD — when the list includes `languages_spoken`, cover it FIRST: casually ask which \
language(s) they're comfortable chatting in, or feel most at home in (open, never yes/no). NEVER name, \
guess, or assume any language they haven't stated themselves — no "besides X" or "other than X" framing \
unless one of their threads explicitly states they speak X. Their answer lets Lana speak their language \
and match them with neighbors who share it.

Each uncovered thread below is shown as `concept | Label [bucket] — "quote"`. For every question
you write, echo back the `concept` of the thread it deepens (copy it EXACTLY from the list) so we
can record what we asked about.

Output ONLY valid JSON (no markdown):
{{
  "questions": [
    {{
      "question": "the question itself, <120 chars",
      "teaser": "2-5 word grammatical lead-in ending with …, e.g. 'about your running…'",
      "label": "short thread name this deepens, e.g. 'running'",
      "bucket": "heritage | stage | vicinity | faith | activity | interest | general",
      "deepens_concept": "the exact concept from the uncovered-threads list this question is about"
    }}
  ]
}}"""


def _backfill_question_embeddings(user_id: str) -> None:
    """Embed any of the user's existing gap questions that predate the embedding column, so
    coverage + dedup see the full history (incl. questions asked before this feature shipped)."""
    try:
        rows = (
            service_client()
            .table("rapport_gaps")
            .select("gap_row_id, question")
            .eq("user_id", user_id)
            .is_("question_embedding", "null")
            .neq("status", "skipped")
            .limit(40)
            .execute()
        ).data or []
    except Exception:
        logger.exception("rapport-synth: backfill load failed for %s", user_id)
        return
    for row in rows:
        q = str(row.get("question") or "").strip()
        if not q:
            continue
        literal = to_pgvector(_embed_question(q))
        if not literal:
            continue
        try:
            service_client().table("rapport_gaps").update(
                {"question_embedding": literal}
            ).eq("gap_row_id", row["gap_row_id"]).execute()
        except Exception:
            logger.debug("rapport-synth: backfill update failed for a gap of %s", user_id)


def _embed_question(text: str) -> list[float] | None:
    try:
        from app.vertex_extract import vertex_embed

        return vertex_embed(text)
    except Exception:
        logger.debug("rapport-synth: embed failed")
        return None


# Standing pseudo-thread: which language(s) they are comfortable chatting in. Not a claim (so the
# coverage RPC never returns it) — injected once per user so Lana ASKS rather than relying on a
# Settings toggle. deepens_concept marks it asked forever after; semantic dedup is the backstop.
LANGUAGE_CONCEPT = "languages_spoken"
_LANGUAGE_THREAD: dict[str, Any] = {
    "concept": LANGUAGE_CONCEPT,
    "label": "Languages they're comfortable chatting in",
    "bucket": "heritage",
    "source_quote": "",
}


def _language_thread_needed(user_id: str) -> bool:
    """True until a language question has been opened for this user (any status —
    answered, skipped, or still queued all count as covered; we never nag)."""
    try:
        res = (
            service_client()
            .table("rapport_gaps")
            .select("gap_row_id")
            .eq("user_id", user_id)
            .eq("deepens_concept", LANGUAGE_CONCEPT)
            .limit(1)
            .execute()
        )
        return not res.data
    except Exception:
        logger.exception("rapport-synth: language-thread check failed for %s", user_id)
        return False  # fail closed — a missed ask beats a repeated one


def _uncovered_claims(user_id: str) -> list[dict[str, Any]]:
    """Identity threads we have NOT yet asked about (least-covered first), via the coverage RPC.
    An empty list means every known thread already has a question — time to go quiet."""
    try:
        res = service_client().rpc(
            "rapport_uncovered_claims",
            {"p_user_id": user_id, "p_cluster_threshold": _CLUSTER_SIMILARITY, "p_limit": 8},
        ).execute()
        return res.data or []
    except Exception:
        logger.exception("rapport-synth: uncovered-claims RPC failed for %s", user_id)
        return []


def _uncovered_block(claims: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for c in claims:
        label = str(c.get("label") or "").strip()
        concept = str(c.get("concept") or "").strip()
        if not label or not concept:
            continue
        bucket = str(c.get("bucket") or "general").strip()
        quote = str(c.get("source_quote") or "").strip()
        line = f"- {concept} | {label} [{bucket}]"
        if quote:
            line += f' — "{quote[:120]}"'
        lines.append(line)
    return "\n".join(lines)


def _asked_block(questions: list[str]) -> str:
    if not questions:
        return "(none yet)"
    return "\n".join(f"- {q}" for q in questions)


def _parse_questions(data: Any, max_new: int = _MAX_NEW) -> list[dict[str, str]]:
    if not isinstance(data, dict):
        return []
    raw = data.get("questions")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or "").strip()[:200]
        if not question:
            continue
        # Models sometimes echo the whole thread line ("concept | Label [bucket]") into
        # deepens_concept despite "copy it EXACTLY" — keep only the concept token, or the
        # exact-match coverage gates (e.g. languages_spoken once-per-user) silently break.
        concept = str(item.get("deepens_concept") or "").split("|")[0].strip()[:64]
        # answer_options: cold-start questions attach one-tap answers (tapping beats
        # typing for a first question). The deepening prompt never emits them, so an
        # absent key is normal — open_semantic_gap treats [] as "free text only".
        raw_opts = item.get("answer_options")
        options = (
            [" ".join(str(o).split())[:48] for o in raw_opts if str(o or "").strip()][:3]
            if isinstance(raw_opts, list)
            else []
        )
        out.append(
            {
                "question": question,
                "teaser": str(item.get("teaser") or "").strip()[:80],
                "label": str(item.get("label") or "").strip()[:80],
                "bucket": str(item.get("bucket") or "general").strip()[:32] or "general",
                "deepens_concept": concept,
                "answer_options": options,
            }
        )
        # The CALLER's cap, not the module default. Breaking on _MAX_NEW meant
        # seed_cold_start(max_new=3) silently kept 2, so raising _BUFFER_TARGET could never
        # fill the queue faster than the deepening path — the floor of 5 was unreachable.
        if len(out) >= max(1, max_new):
            break
    return out


def _generate(uncovered: str, asked: str, max_new: int) -> Any:
    system = SYNTH_PROMPT.format(max_new=max_new)
    user_payload = f"THREADS TO ASK ABOUT (uncovered):\n{uncovered}\n\nALREADY ASKED:\n{asked}"
    try:
        from app.orchestrator.llm import llm_configured, llm_json, router_model

        if llm_configured():
            return llm_json(
                model=router_model(),
                system=system,
                user_payload=user_payload,
                max_tokens=512,
                temperature=0.4,
            )
    except Exception:
        logger.exception("rapport-synth: orchestrator llm failed")

    # Vertex Gemini fallback — same 512-token budget and timeout as the OpenAI
    # call above, via llm.gemini_config().
    try:
        from app.orchestrator.llm import vertex_generate_json

        return vertex_generate_json(
            model=os.environ.get("VERTEX_EXTRACT_MODEL", "gemini-2.5-flash"),
            system=system,
            user_payload=user_payload,
            max_tokens=512,
            temperature=0.4,
        )
    except Exception:
        logger.exception("rapport-synth: vertex fallback failed")
        return None


def synthesize_gaps_from_claims(user_id: str, max_new: int = _MAX_NEW) -> int:
    """Open up to ``max_new`` fresh rapport gaps from the user's claims. Returns how many
    were opened. Never raises. Duplicate topics are deduped by ``open_semantic_gap``'s
    slug key, so re-running is safe."""
    if not user_id or _cooling_down(user_id):
        return 0
    # Make sure older questions (pre-embedding) count toward coverage before we decide.
    _backfill_question_embeddings(user_id)
    uncovered = _uncovered_claims(user_id)
    # Lana asks the user's language herself (replacing the Settings toggle): until a language
    # question exists, it rides FIRST in the thread list so it lands within the first tiles.
    if _language_thread_needed(user_id):
        uncovered.insert(0, dict(_LANGUAGE_THREAD))
    logger.info(
        "rapport-synth[%s]: uncovered threads = %s",
        user_id,
        [c.get("label") for c in uncovered] or "(none)",
    )
    if not uncovered:
        # Every known thread already has a question — go quiet rather than invent filler.
        logger.info("rapport-synth: all threads covered for %s — nothing to ask", user_id)
        return 0
    # Pass the FULL ask history (not just the recent few) so we never repeat or reword a question
    # from any point in the past, however old.
    asked = recent_gap_questions(user_id, limit=60)
    data = _generate(_uncovered_block(uncovered), _asked_block(asked), max_new)
    questions = _parse_questions(data, max_new)
    logger.info(
        "rapport-synth[%s]: generated = %s",
        user_id,
        [(q["deepens_concept"], q["question"]) for q in questions] or "(none)",
    )
    if not questions:
        return 0
    opened = 0
    for q in questions:
        try:
            if open_semantic_gap(
                user_id,
                None,
                q["question"],
                label=q["label"] or None,
                bucket=q["bucket"],
                teaser=q["teaser"] or None,
                deepens_concept=q["deepens_concept"] or None,
            ):
                opened += 1
        except Exception:
            logger.exception("rapport-synth: open_semantic_gap failed for %s", user_id)
    logger.info("rapport-synth: opened %d new gap(s) from claims for %s", opened, user_id)
    return opened


def _open_gap_count(user_id: str) -> int:
    try:
        res = (
            service_client()
            .table("rapport_gaps")
            .select("gap_row_id", count="exact")
            .eq("user_id", user_id)
            .eq("status", "open")
            .execute()
        )
        return res.count if getattr(res, "count", None) is not None else len(res.data or [])
    except Exception:
        logger.exception("rapport-synth: open-count failed for %s", user_id)
        return _BUFFER_TARGET  # fail closed — assume full, don't over-synthesize on a read error


def ensure_gap_buffer(user_id: str, target: int = _BUFFER_TARGET) -> int:
    """Top the open-gap pool back up toward ``target`` so the tile always has a question queued
    ahead. Only synthesizes the shortfall; a no-op when the buffer is already full. Best-effort —
    intended to run as a background task after a tile answer closes a gap."""
    if not user_id:
        return 0
    # Grounding questions for ungrounded circle affiliations refill first (they carry
    # real matcher value and are capped/idempotent inside); they count toward the
    # buffer via the open-gap count below.
    try:
        from app.circles_flow import ensure_grounding_gaps

        ensure_grounding_gaps(user_id)
    except Exception:  # noqa: BLE001 — the claims synth must still run
        logger.exception("rapport: grounding refill failed for %s", user_id)
    need = target - _open_gap_count(user_id)
    if need <= 0:
        return 0
    made = synthesize_gaps_from_claims(user_id, max_new=min(need, _MAX_NEW))
    # Seed when the queue is STILL short, not only when deepening made nothing.
    #
    # `if made: return made` is where the loop Tommaso reported actually lived. Deepening
    # sources topics only from the user's own claims, so from their first claim it always
    # made something — and lateral seeding, the one path that can introduce a topic they
    # have never mentioned, was never reached again. Measured: a user with one claim got
    # five renders alternating two facets of that claim, 0/5 lateral. Fixing the gate
    # inside seed_cold_start was necessary and not sufficient; this is the other half.
    still = need - made
    if still > 0:
        made += seed_cold_start(user_id, max_new=min(still, 3))
    return made


# ── Cold start ───────────────────────────────────────────────────────────────
#
# Everything above deepens threads the user already has. A zero-claim user has
# none, so synthesize_gaps_from_claims correctly returns 0 and the tile stays
# empty — the emptiest state in the product, and the one most internal testing
# goes through.
#
# Seeding it with invented questions would be the obvious fix and the wrong one.
# score_onion_candidates_for_user awards +1 per shared PUBLIC concept and drops
# any pair scoring 0, so a shared interest only counts IF A NEIGHBOR NEARBY HOLDS
# IT TOO: "I collect vinyl" is a true, warm answer worth nothing to the matcher if
# nobody within reach shares it. So the seeds are drawn from real local claim
# supply (rapport_local_supply, 20261123120000) — every answer then has a
# guaranteed counterpart, because the concept was read off the very people we
# would be matching them with.

SEED_PROMPT = """You are Lana, a warm neighborhood concierge in a block-based neighborhood app where \
neighbors connect with nearby neighbors. You are meeting someone who JUST joined — you know \
NOTHING about them yet, so every question here is their first.

Below are things NEIGHBORS NEAR THEM already have on their own profiles, with how many \
neighbors each. Write up to {max_new} opening questions that find out whether any of these are \
theirs too. Because these come from real nearby neighbors, an answer of "yes, that's me" is an \
immediate introduction — that is the whole point, so stay close to the list and do NOT wander \
off it into topics nobody nearby mentioned.

Every question must clear these bars:
1. ANSWERABLE COLD — they have told you nothing, so never reference a fact about them. No "you \
mentioned…", no "your running…". Ask openly.
2. ONE TOPIC EACH — spread across DIFFERENT threads from the list, never two on the same one. \
Prefer threads from different buckets over several from the strongest bucket: a first question in \
an untouched area of their life is worth more than a second in the same area.
3. THE ANSWER MUST RESOLVE TO SOMETHING MATCHABLE — a place, a time, an activity, a level, a \
cadence, or a plain yes/no to the thread itself ("are you a fan?" resolves to fan-of-that-sport, \
which is exactly what the matcher scores). What is banned is an answer nothing can be done with: \
a feeling, an origin story, an essay — "what got you into…", "what do you love most about…".
4. SAY WHY YOU ARE ASKING, THEN ASK. Open on the neighborhood, then put the question to them:
     "There are a lot of dog people around here — do you have one?"
     "This neighborhood is big on soccer. Are you a fan?"
   This is the shape, and it does two jobs. It is honest about where the topic came from, so
   the question does not arrive out of nowhere. And it CANNOT PRESUPPOSE: "Where do you take \
your rescue dog?" assumes they own one, which breaks rule 1 and is how a neighbor's dog became \
a claim that THIS person has a rescue dog, feeding the matcher a fact they never stated. A "no" \
must always be an easy, natural answer. A bare yes/no is fine HERE — the neighborhood opener is \
what stops it being a flat interrogation — but prefer a question whose answer also names \
something ("...do you have one? What's yours like?" is two questions; don't). If a thread only \
works by presuming it is already true of them, drop it.
5. NOT A SURVEY. Warm, curious, like a neighbor asking, under 120 characters. Never stack two \
questions into one sentence.

Attach 2-3 one-tap ANSWER OPTIONS to each question when the list makes obvious ones (in THEIR \
voice, first person, under 40 characters) — tapping is much easier than typing for a first \
question. Options carry the CONTENT a bare yes or no would not: "Yes — a rescue beagle", not \
"Yes". And because rule 4's shape invites a no, ONE option must always be that no, in their own \
voice — "No, not a dog person", "Not really my thing". A no is what stops a polite guess becoming \
a fact on their profile, so it is never the option you leave out.

NEVER touch a sensitive topic — health/medical, grief, divorce/relationship trouble, money/debt, \
legal/immigration, mental health, faith. Never ask about their gender, their name, or their age. \
Name the NEIGHBORHOOD, never a number and never a person. "There are a lot of dog people \
around here" is context that explains the question. "3 neighbors nearby also have dogs" is \
surveillance — a count that small is guessable, and it reports what other people privately told \
you. No counts, no "someone on your block", no names, no "a neighbor of yours".

Write question, teaser and label in ENGLISH regardless of the language of any quotes — questions \
are stored English-canonical and rendered into the user's language at display time. Quality over \
quantity: return only the questions that clear every bar, or an empty list.

Each thread below is `concept | Label [bucket] — N neighbors`. Echo back the `concept` your \
question came from, copied EXACTLY.

Output ONLY valid JSON (no markdown):
{{
  "questions": [
    {{
      "question": "the question itself, <120 chars",
      "teaser": "2-5 word grammatical lead-in ending with …, e.g. 'about your weekends…'",
      "label": "short thread name, e.g. 'running'",
      "bucket": "heritage | stage | vicinity | faith | activity | interest | general",
      "deepens_concept": "the exact concept from the list this question is about",
      "answer_options": ["first-person option", "another"]
    }}
  ]
}}"""


def _local_supply(user_id: str, min_holders: int = 2) -> list[dict[str, Any]]:
    """Public concepts held by >= min_holders neighbors near this user. [] on failure."""
    try:
        res = service_client().rpc(
            "rapport_local_supply",
            {"p_user_id": user_id, "p_limit": 8, "p_min_holders": int(min_holders)},
        ).execute()
        return res.data or []
    except Exception:
        # Pre-20261123 environments have no such RPC — fall through to the tree seeds.
        logger.info("rapport-seed: local-supply RPC unavailable for %s", user_id)
        return []


def _supply_block(rows: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for r in rows:
        concept = str(r.get("concept") or "").strip()
        label = str(r.get("label") or "").strip()
        if not concept or not label:
            continue
        bucket = str(r.get("bucket") or "general").strip()
        holders = int(r.get("holders") or 0)
        lines.append(f"- {concept} | {label} [{bucket}] — {holders} neighbors")
    return "\n".join(lines)


def seed_cold_start(user_id: str, max_new: int = 3) -> int:
    """Open lateral questions — about things the user has never mentioned. Returns how many.

    It used to stop at claim #1, which made this the only lateral source in a system whose
    other source (synthesize_gaps_from_claims) can only deepen topics the user already
    raised: from their first claim onward every question Lana could ask was a facet of
    something already said, which is what the looping felt like.

    What stops it now is SUPPLY — _local_supply returns concepts real neighbours hold, and
    when there is nothing new left it returns nothing and this returns 0. The bucket gate
    below is only a backstop for a genuinely complete profile; measured at 5 of 7 it fired
    after about seven answers and silenced the tile while supply remained. Three tiers,
    cheapest last:
      1. Local supply → AI-written questions about what neighbors nearby claim.
      2. Nothing nearby (first user in an area, or no location yet) → the
         no-prior-knowledge catalogue seeds, which need no supply at all.
    Never raises.
    """
    if not user_id or _cooling_down(user_id, _last_seed_attempt):
        return 0
    # None means the read failed, not "nothing covered" — an unreadable profile falls
    # through to asking rather than to silence, which is the failure users report.
    from app.rapport_priority import covered_buckets

    covered = covered_buckets(user_id)
    if covered is not None and len(covered) >= _BREADTH_COVERED:
        return 0

    supply = _local_supply(user_id)
    thin = False
    if not supply:
        # A thin area is not an empty one. The floor of two holders is a QUALITY
        # heuristic, not a matcher requirement: score_onion_candidates_for_user awards
        # +1 off a SINGLE shared public claim and only drops a pair scoring 0. So before
        # falling back to catalogue questions with no counterpart at all, ask again for
        # concepts exactly one neighbor holds — one real person to be introduced to
        # still beats a question nobody in reach can answer alongside you.
        supply = _local_supply(user_id, min_holders=1)
        thin = bool(supply)
    logger.info(
        "rapport-seed[%s]: local supply = %s%s",
        user_id,
        [(r.get("concept"), r.get("holders")) for r in supply] or "(none)",
        " (thin: min_holders=1)" if thin else "",
    )
    if supply:
        asked = recent_gap_questions(user_id, limit=60)
        data = _generate_seeds(_supply_block(supply), _asked_block(asked), max_new)
        questions = _parse_questions(data, max_new)
        opened = 0
        for q in questions:
            try:
                if open_semantic_gap(
                    user_id,
                    None,
                    q["question"],
                    label=q["label"] or None,
                    bucket=q["bucket"],
                    teaser=q["teaser"] or None,
                    deepens_concept=q["deepens_concept"] or None,
                    answer_options=q.get("answer_options") or None,
                    # Priority: this topic has a proven local counterpart, so an
                    # answer scores in the matcher instead of merely maybe scoring.
                    from_local_supply=True,
                ):
                    opened += 1
            except Exception:
                logger.exception("rapport-seed: open failed for %s", user_id)
        if opened:
            logger.info("rapport-seed: opened %d local-supply seed(s) for %s", opened, user_id)
            return opened
        # The model returned nothing usable — fall through rather than leave it empty.

    try:
        from app.rapport_gaps import open_cold_seed_gaps

        return open_cold_seed_gaps(user_id)
    except Exception:
        logger.exception("rapport-seed: tree seeds failed for %s", user_id)
        return 0


def _generate_seeds(supply: str, asked: str, max_new: int) -> Any:
    """One Flash-class call for cold-start questions. Mirrors _generate's failover."""
    system = SEED_PROMPT.format(max_new=max_new)
    user_payload = f"WHAT NEIGHBORS NEAR THEM CLAIM:\n{supply}\n\nALREADY ASKED:\n{asked}"
    try:
        from app.orchestrator.llm import llm_configured, llm_json, router_model

        if llm_configured():
            return llm_json(
                model=router_model(),
                system=system,
                user_payload=user_payload,
                max_tokens=640,
                temperature=0.4,
            )
    except Exception:
        logger.exception("rapport-seed: orchestrator llm failed")
    try:
        from app.orchestrator.llm import vertex_generate_json

        return vertex_generate_json(
            model=os.environ.get("VERTEX_EXTRACT_MODEL", "gemini-2.5-flash"),
            system=system,
            user_payload=user_payload,
            max_tokens=640,
            temperature=0.4,
        )
    except Exception:
        logger.exception("rapport-seed: vertex fallback failed")
        return None
