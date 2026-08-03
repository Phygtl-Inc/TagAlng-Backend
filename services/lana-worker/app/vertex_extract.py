import os
import re
from typing import Any

from app.lana_ui import normalize_bucket, parse_mapped_spans
from app.models import ExtractedClaim, MappedSpan

EXTRACT_PROMPT = """You are an identity extraction model for a block-based neighborhood social app.

Read the full conversation transcript between Lana and the user. Extract identity "threads" — by meaning, not keyword matching.

Output ONLY valid JSON (no markdown):
{
  "mapped_summary": "One warm sentence summarizing the user",
  "spans": [
    {
      "text": "exact phrase from user words",
      "bucket": "heritage",
      "claim_concept": "latino_heritage"
    }
  ],
  "claims": [
    {
      "concept": "snake_case_slug",
      "label": "Short UI card title",
      "tone": "optional",
      "confidence": 0.85,
      "disclosure": "public",
      "synonyms": ["tag1"],
      "source_quote": "exact short quote from user",
      "bucket": "heritage",
      "transient": false
    }
  ],
  "assistant_message": "Short warm closing line"
}

Allowed bucket values: heritage, stage, vicinity, faith, activity, interest, general.
Allowed disclosure: public, mutual, private.

Rules:
- Max 8 claims; only what the user expressed or clearly implied
- ONE claim per distinct thread — NEVER emit the same thread twice with different wording, bucket, or synonyms (e.g. do NOT list "Interested in Hosting Neighbor Meetings" five times). Merge them into a single claim.
- NEVER emit a claim that is only a bare topic label ("Health", "Wellness", "Lifestyle", "General") or that expresses uncertainty ("Unsure what to call", "Not sure about time"). Skip these entirely — they are not threads.
- "transient": true for TEMPORARY states that are NOT durable identity — an injury or illness ("sprained ankle"), an upcoming trip/vacation, a passing mood. Durable identity (heritage, life stage, ongoing interests, occupation, faith) is transient=false. When in doubt, false.
- Capture languages spoken as one claim (bucket "interest", e.g. "Speaks 7 languages")
- Every claim MUST have source_quote (verbatim or tight paraphrase from user) and bucket
- synonyms: 3-6 lowercase tags per claim — include broader/related terms, not just the literal word (e.g. "sicilian" → ["sicilian","italian","mediterranean"])
- spans: 3-8 phrases covering the mapped_summary for frontend color highlights
- concept must match ^[a-z][a-z0-9_]{1,63}$
- NEVER extract race, exact age, sex/gender demographics, street address
- NEVER make parenting/kids into a claim, and never capture a child's name, age, or school
- Faith, religion, sobriety, recovery, LGBTQ+: disclosure MUST be "mutual"

Transcript:
"""

INCREMENTAL_EXTRACT_PROMPT = """You extract identity threads from ONE user message in a block-based \
neighborhood app chat, \
and you stay curious — after capturing what they said, you propose ONE warm follow-up that draws out more.

Output ONLY valid JSON (no markdown):
{
  "nickname": "first name neighbors should use, or null",
  "kids_count": null,
  "role": null,
  "grammatical_gender": null,
  "claims": [
    {
      "concept": "snake_case_slug",
      "label": "Short UI card title",
      "tone": "optional",
      "confidence": 0.85,
      "disclosure": "public",
      "synonyms": ["tag1", "tag2", "tag3"],
      "details": [],
      "source_quote": "exact short quote from this message",
      "bucket": "heritage",
      "vague": false,
      "transient": false
    }
  ],
  "followup_question": "one warm question that adds a NEW MATCHABLE facet (never backstory), or null",
  "followup_topic": "a short grammatical teaser for the question, 2-5 words, e.g. 'about your reading…', 'about the World Cup…', 'about your running…' — or null when followup_question is null",
  "circle_candidates": [
    {
      "circle_type": "fitness",
      "circle_key": "spin_class",
      "raw_phrase": "my Tuesday spin class",
      "place_name": "the venue name they SAID, or null",
      "confidence": 0.9
    }
  ],
  "place_feature_candidates": [
    {
      "circle_key": "spin_class",
      "key": "has_pool",
      "value": "true",
      "sub_group": "",
      "confidence": 0.8
    }
  ]
}

Allowed bucket values: heritage, stage, vicinity, faith, activity, interest, general.
Allowed disclosure: public, mutual, private.

Rules:
- The user may write in ANY language (Spanish, Portuguese, Urdu, ...). Understand it, but ALWAYS \
output concept/label/synonyms/followup_topic in ENGLISH — the DB is English-canonical so every \
neighbor matches on the same terms ("me gusta jugar al cricket" → concept "cricket_player", label \
"Plays cricket"). source_quote stays the exact original-language quote. followup_question and \
followup_topic are ALSO English — they are stored canonically and AI-rendered into the user's preferred \
language at display time, so a later language switch re-renders the whole queue.
- Max 6 claims from this message only
- If no identity content (greetings, "ok", ZIP, phone), return {"nickname": null, "kids_count": null, "claims": [], "followup_question": null}
- Split distinct threads — capture EACH one, do not collapse (e.g. "pakistani dad, married 10 years, speak 5 languages, do triathlon" → pakistani_heritage + multilingual + married_ten_years + triathlon; "dad" and kid count go to kids_count, never a claim)
- Capture LANGUAGES spoken as one claim, bucket "interest" (e.g. "speak 7 languages" → concept "multilingual", label "Speaks 7 languages")
- Capture RELATIONSHIP status as a claim, bucket "stage" (e.g. "married 10 years" → concept "long_married", label "Married 10 years")
- Capture occupation/work as a claim, bucket "activity" or "interest" (e.g. "work in tech" → tech_worker). Mark it "vague": true when it is coarse and a specific would help (e.g. "tech worker", "athlete", "in finance")
- Every claim MUST have source_quote from this message and bucket
- concept must match ^[a-z][a-z0-9_]{1,63}$
- synonyms: 3-6 lowercase tags per claim — include BROADER and RELATED terms, not just the literal word (e.g. "sicilian" → ["sicilian","italian","mediterranean","sicily"]; "triathlon" → ["triathlon","endurance","running","cycling","swimming"]). These power match discovery.
- "vague": true when the claim is coarse enough that a follow-up would sharpen it — e.g. "tech worker", "athlete", "in finance", OR a COUNT without specifics ("speaks 5 languages" → vague until they name them, "plays sports" → which). false when already specific.
- "transient": true for TEMPORARY states that are NOT durable identity — an injury or illness ("sprained ankle", "got the flu"), an upcoming trip/vacation, a one-off plan, a passing mood ("feeling low-key this week"). Durable identity (heritage, life stage, ongoing interests, occupation, faith) is transient=false. When in doubt, false.
- READ THROUGH EMOTION TO THE INTEREST. Enthusiasm, disappointment, or loyalty about a NAMED thing (a team, player, game, artist, show, hobby, place) reveals a durable interest — capture THAT, not the feeling. E.g. "sad Ronaldo lost, wanted him to win the World Cup" → soccer_fan + ronaldo_fan (NOT "sad"); "gutted Real Madrid lost" → real_madrid_fan; "so hyped for the new Zelda" → gamer / zelda_fan; "loved the Taylor Swift concert" → taylor_swift_fan. The emotion is transient; the named interest is identity. Only when a concrete thing is named — a vague mood with no entity ("rough day", "feeling off") is nothing, skip it.
- NEVER make a claim from grief, loss, crisis, health, or relationship trouble, even when phrased emotionally — bereavement ("my friend passed away"), divorce/separation, mental-health ("I'm depressed", "anxious"), illness/diagnosis, money/legal distress. These are sensitive and are NOT identity; capture nothing and set followup_question null. "Sad my team lost" (an interest) and "sad someone passed" (grief) are different — one names a hobby, the other a loss.
- NEVER emit a claim that is only a bare topic label ("Health", "Wellness", "Lifestyle", "General") or that expresses uncertainty ("Unsure what to call", "Not sure about time", "don't know"). Skip these entirely — they are not threads.
- Do NOT emit the SAME thread twice with different wording — one claim per distinct thread
- "details": short third-person sub-facts (2-6 words each, max 3 per claim) that ADD texture beyond the label — rhythm, level, setting, sub-type (e.g. label "State-level swimmer" + details ["Swims every weekend"]). Empty [] when the label already says everything. Never restate the label as a detail.
- kids_count: an integer ONLY when the user states HOW MANY children they have ("2 sons" → 2, "three kids" → 3). null otherwise. This is private and never a claim. NEVER capture a child's name, age, gender, school, or photo — only the count.
- role: the user's household role ONLY when they state it about THEMSELVES: "parent" ("my kids", "I'm a dad"), "expecting" ("baby on the way"), "grandparent" ("my grandkids"), "caregiver" ("the family I care for"), "guardian", "relative" ("my nephew lives with us"). null otherwise — never infer from what they search for. Private, never a claim.
- grammatical_gender: "feminine" or "masculine" ONLY from the user's own gendered SELF-reference in a gendered language ("estoy cansada" → feminine, "estou animado" → masculine) or an explicit self-label ("I'm his mom" → feminine, "I'm their dad" → masculine). null otherwise — NEVER guess from a name, and never from a third party. Used only for grammatical agreement, never shown.
- NEVER extract race, exact age, sex/gender demographics, street address
- NEVER extract negative or exclusion claims ("not Brazilian", "no Italian", "without X")
- NEVER make parenting/kids into a claim — only kids_count carries it
- ONLY extract first-person identity ("I am", "I'm", "my heritage") — NOT who they search for ("find Brazilian mom", "looking for Pakistani neighbors")
- Faith, religion, sobriety, recovery, LGBTQ+: disclosure MUST be "mutual"
- nickname only when user states their name ("I'm Brinda", "call me Sam", "my name is brigade")
- followup_question — becomes a "By the way…" tile on their home screen; their answer is stored as an identity claim used to match them with nearby neighbors. Warm neighborhood-concierge tone, not an interviewer; ask only what genuinely helps them connect locally. Propose ONE only if it adds a CONNECTION-MATCHABLE facet — something that would help them MEET or RELATE to nearby neighbors: shared activities/hobbies, kids or family stage, local spots they go, cultural or community ties, their weekly rhythm. Generic consumer/brand/device/product preferences are NOT connection facets — which phone, which apps, gadgets, streaming services, operating system → return null (no neighbor connects over that). Reason about what you ALREADY know to hit a real GAP. Two shapes: (1) SHARPEN a "vague": true claim — vague tech_worker → "What kind of tech — engineering, product, design?"; "speaks 5 languages" → "Which five?". (2) FILL a matchable dimension you don't yet know. VARY the dimension to fit the topic — do NOT default to "solo or with others" for everything (that has become repetitive). Choose the ONE most natural from a range: sub-type/genre (books → "Any genres you gravitate to?"), frequency/rhythm (running → "Mornings or weekends?"), setting or local spot ("A local place you like for it?"), skill/level, doing-it-with-others, kids' involvement, teach-vs-learn. FORBIDDEN — never ask an opinion, feeling, or origin-story question (anything asking why, how you started, what you enjoy/love most, or what "caught your interest"); those add NO matchable facet — replace with a concrete one or return null. Do NOT repeat a question shape listed in ALREADY ASKED above; if the only fitting angle was already asked, return null or pick a different dimension. Write ONLY the question itself — NO "By the way", no greeting or lead-in phrase (the tile shows its own "By the way…" framing; a prefix just doubles it). Short (<120 char), warm, OPEN, reference what they said. Return null when nothing is vague AND no fresh matchable dimension fits — silence beats filler. HARD RULE: null for any sensitive / help-seeking topic — divorce or relationship trouble, health/medical, mental health/safety, money/debt, legal/immigration — and when the message is a question aimed at you.
- followup_topic: a 2-5 word grammatical lead-in that names the thread for the tile, ending with "…" — e.g. "about your reading…", "about the World Cup…", "about your Portuguese…". Write natural English; NEVER glue a raw label ("about your interested in books…" is wrong). null whenever followup_question is null.
- circle_candidates: real-world COMMUNITIES the user BELONGS TO or attends recurringly — their gym or fitness class, faith community, a school community, a hobby/sports club, a support group, a heritage/cultural association, a friend group ("my gym", "our church", "my Tuesday spin class", "my book club"). Habitual attendance at a place counts ("I go to my gym every weekend" → {fitness, gym}); so does plain recurring attendance without "my" ("I go to the gym every weekend"). A RECURRING ACTIVITY DONE WITH OTHERS also counts even when no club or venue is named — the regular group IS the community and the place gets asked later ("I play squash with friends every week" → {fitness, squash_group}; "we play futsal on Sundays" → {fitness, futsal_group}; "I play futsal regularly on the ground" → {fitness, futsal_group}). First-person membership/attendance only — NOT one-off visits ("tried a yoga class once"), aspirations ("thinking of joining a gym"), a bare liking with no attendance ("I like watching football"), a solo habit with no group and no place ("I run every morning"), occasional play with no rhythm ("I play squash sometimes"), or who/what they're SEARCHING for ("any yoga classes nearby?", "show me cycling activities" → NEVER a circle). The search exclusion applies ONLY to the thing being searched for — NEVER to background the user gives about themselves inside the same message. A search ask often carries a community mention in its "because…" context, and that mention MUST still be captured ("any weekday events? I'm busy weekends because I go to the gym every weekend" → the searched-for events are NOT a circle, but {fitness, gym} IS; "looking for a Saturday playdate since our church group meets Sundays" → {faith, church_group}). circle_type MUST be one of: school, faith, fitness, kids_activity, neighborhood, hobby, support, heritage, friends, other. circle_key: snake_case slug for the community (same format as concept). IMPORTANT — circles are a SEPARATE ledger from claims: the ALREADY ON PROFILE dedupe rule does NOT apply here. Re-state the circle_candidate EVERY time the community is mentioned, even when a matching claim/thread is already on the profile and even when you emitted it before (repeats are harmless corroboration; staying silent loses the circle). A community can ALSO yield a normal claim (spin class → does_spin) — emit both. Empty [] only when the message truly names no community they attend. Never capture a child's name in raw_phrase.
- place_name (inside circle_candidates): the venue/business/organization name they actually SAID for that community, verbatim and nothing else — "I go to the gym at Fitness CF" → "Fitness CF"; "our church is St. Luke's" → "St. Luke's"; "my kids are at Lake Nona Middle" → "Lake Nona Middle". null whenever they named only the activity or the KIND of place ("my gym", "we play futsal on Sundays", "my Tuesday spin class", "our church"). NEVER invent, complete, or guess a name they did not say — a wrong name gets them attached to the wrong place. Do not put the activity word in it ("gym", "church" alone are not names).
- place_feature_candidates: ONLY when the user volunteers an OBJECTIVE attribute of a community place from circle_candidates (or one they clearly already told you about) — "we swim there" → has_pool, "they watch the kids while I train" → has_childcare, "there's a sauna" → has_sauna. key: snake_case (has_pool, has_childcare, has_kids_area, has_sauna, has_classes, stroller_friendly, or a new one in the same style). value: "true"/"false" or a short freeform ("50m lap pool"). sub_group: a program/room inside the place ("spin", "toddler swim"), else "". circle_key MUST match the community it is about. Subjective opinions ("I love it there") are NOT features. Empty [] when none.

User message:
"""

MUTUAL_CONCEPT_MARKERS = (
    "faith",
    "catholic",
    "muslim",
    "jewish",
    "christian",
    "church",
    "mosque",
    "synagogue",
    "sober",
    "recovery",
    "lgbtq",
    "queer",
)


def _vertex_client():
    """Delegating shim — kept because several modules import it by name. Returns
    the single cached client so every Vertex call inherits gemini_http_options()
    (timeout + transport retries) instead of building a fresh, unbounded client."""
    from app.orchestrator.llm import _gemini_client

    return _gemini_client()


def _parse_claims(data: Any) -> list[ExtractedClaim]:
    if not isinstance(data, dict):
        return []
    raw = data.get("claims", [])
    if not isinstance(raw, list):
        return []
    out: list[ExtractedClaim] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        concept = str(item.get("concept", "")).strip().lower()
        if not re.match(r"^[a-z][a-z0-9_]{1,63}$", concept):
            continue
        label = str(item.get("label", concept)).strip()[:120]
        disclosure = str(item.get("disclosure", "public"))
        if disclosure not in ("public", "mutual", "private"):
            disclosure = "public"
        if any(m in concept for m in MUTUAL_CONCEPT_MARKERS):
            disclosure = "mutual"
        conf = max(0.0, min(1.0, float(item.get("confidence", 0.8))))
        syns = item.get("synonyms", [])
        if not isinstance(syns, list):
            syns = []
        syns = [str(s)[:80] for s in syns[:4]]
        details = item.get("details", [])
        if not isinstance(details, list):
            details = []
        details = [str(d).strip()[:80] for d in details[:3] if str(d).strip()]
        tone = item.get("tone")
        source_quote = str(item.get("source_quote", "")).strip()[:160] or None
        bucket = normalize_bucket(item.get("bucket"))
        transient = bool(item.get("transient", False))
        vague = bool(item.get("vague", False))
        out.append(
            ExtractedClaim(
                concept=concept,
                label=label,
                tone=str(tone)[:40] if tone else None,
                confidence=conf,
                disclosure=disclosure,
                synonyms=syns,
                source_quote=source_quote,
                bucket=bucket,
                transient=transient,
                vague=vague,
                details=details,
            )
        )
    return out[:8]


def parse_profile_extract_data(
    data: Any,
) -> tuple[list[ExtractedClaim], str, str | None, list[MappedSpan]]:
    if not isinstance(data, dict):
        raise ValueError("invalid_extract_json")
    claims = _parse_claims(data)
    if not claims:
        raise ValueError("model_returned_no_valid_claims")
    closing = str(data.get("assistant_message", "")).strip()[:800]
    if not closing:
        closing = "Your profile threads are ready — neighbors on your block can get to know the real you."
    mapped_summary = str(data.get("mapped_summary", "")).strip()[:800] or None
    span_dicts = parse_mapped_spans(data.get("spans"))
    spans = [MappedSpan(**s) for s in span_dicts if s.get("text")]
    if not mapped_summary and spans:
        mapped_summary = ", ".join(s.text for s in spans[:6])
    return claims, closing, mapped_summary, spans


def _parse_kids_count(raw: Any) -> int | None:
    """An integer 1-20 only; None for anything else (never trust ages/years here)."""
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    if 1 <= n <= 20:
        return n
    return None


def _parse_followup_question(raw: Any) -> str | None:
    q = str(raw or "").strip()
    if not q or q.lower() in ("none", "null", "n/a"):
        return None
    return q[:160]


def parse_incremental_claims_data(
    data: Any,
) -> tuple[str | None, list[ExtractedClaim], int | None, str | None]:
    if not isinstance(data, dict):
        return None, [], None, None
    nickname_raw = data.get("nickname")
    nickname = str(nickname_raw).strip()[:30] if nickname_raw else None
    if nickname and len(nickname) < 2:
        nickname = None
    claims = _parse_claims(data)
    kids_count = _parse_kids_count(data.get("kids_count"))
    followup = _parse_followup_question(data.get("followup_question"))
    return nickname, claims[:6], kids_count, followup


def _existing_claims_block(existing_labels: list[Any] | None) -> str:
    """Tell the extractor what's already on the profile so it ENRICHES, not duplicates.

    Accepts plain label strings or thread dicts ({concept, label, details}). With
    concepts present, the model can re-emit the SAME slug to enrich in place — that
    slug is the upsert merge key, so no near-duplicate thread is ever created.
    """
    lines: list[str] = []
    for item in existing_labels or []:
        if isinstance(item, dict):
            concept = str(item.get("concept") or "").strip()
            label = str(item.get("label") or "").strip()
            details = "; ".join(
                str(d).strip() for d in item.get("details") or [] if str(d).strip()
            )
            if not (concept or label):
                continue
            line = f"{concept or '?'} — {label}"
            if details:
                line += f" (details: {details})"
            lines.append(line)
        else:
            text = str(item).strip()
            if text:
                lines.append(text)
    if not lines:
        return ""
    return (
        "ALREADY ON PROFILE (concept — label). NEVER create a NEW claim that duplicates "
        "or overlaps any of these. But when this message ADDS to one of these threads — "
        "more specific level ('state level'), rhythm ('every weekend'), sub-type, setting "
        "— RE-EMIT that thread with its EXACT SAME concept slug, carrying: (a) the label "
        "UPGRADED if the new fact is a stronger identity statement (never downgraded), "
        "(b) the new fact as one short entry in \"details\" (skip facts already listed), "
        "(c) any new synonyms, (d) confidence reflecting that the user has now confirmed "
        "it again (repeat first-person statements approach 1.0). A plain RESTATEMENT "
        "with nothing new ('I swim' when swimmer is already listed) still counts: "
        "re-emit that concept unchanged (same label, empty details) — it is "
        "corroboration and raises confidence. Threads the message does not touch are "
        "NOT re-emitted. Only genuinely new topics get a new concept:\n"
        + "\n".join(lines[:40])
        + "\n\n"
    )


def _recent_questions_block(recent_questions: list[str] | None) -> str:
    """Tell the extractor what it has recently asked, so its follow-up isn't a near-duplicate."""
    qs = [str(q).strip() for q in (recent_questions or []) if str(q).strip()]
    if not qs:
        return ""
    return (
        "ALREADY ASKED (your followup_question must NOT repeat or be a near-duplicate of these "
        "— same dimension or same shape counts as a duplicate; pick a DIFFERENT angle, or null): "
        + " | ".join(qs[:10])
        + "\n\n"
    )


def vertex_extract_claims_from_utterance(
    message: str,
    existing_labels: list[str] | None = None,
    recent_questions: list[str] | None = None,
) -> Any:
    from app.orchestrator.llm import vertex_generate_json

    prompt = (
        INCREMENTAL_EXTRACT_PROMPT
        + _existing_claims_block(existing_labels)
        + _recent_questions_block(recent_questions)
        + message.strip()
    )
    return vertex_generate_json(
        model=os.environ.get("VERTEX_EXTRACT_MODEL", "gemini-2.5-flash"),
        system=None,
        user_payload=prompt,
        max_tokens=512,  # parity with the llm_json call in incremental_claims_from_utterance
        temperature=0.2,
    )


def incremental_claims_from_utterance(
    message: str,
    existing_labels: list[str] | None = None,
    recent_questions: list[str] | None = None,
) -> Any:
    """Extract claims via orchestrator LLM when configured; else Vertex."""
    import logging

    log = logging.getLogger(__name__)
    text = str(message or "").strip()
    system = (
        INCREMENTAL_EXTRACT_PROMPT
        + _existing_claims_block(existing_labels)
        + _recent_questions_block(recent_questions)
    )
    try:
        from app.orchestrator.llm import llm_configured, llm_json, router_model

        if llm_configured():
            return llm_json(
                model=router_model(),
                system=system,
                user_payload=text,
                max_tokens=512,
                temperature=0.2,
            )
    except Exception:
        log.exception("llm_incremental_claim_extract_failed")
    return vertex_extract_claims_from_utterance(text, existing_labels, recent_questions)


def vertex_extract_from_transcript(
    transcript: str,
) -> tuple[list[ExtractedClaim], str, str | None, list[MappedSpan]]:
    from app.orchestrator.llm import vertex_generate_json

    data = vertex_generate_json(
        model=os.environ.get("VERTEX_EXTRACT_MODEL", "gemini-2.5-flash"),
        system=None,
        user_payload=EXTRACT_PROMPT + transcript.strip(),
        max_tokens=4096,  # parity with orchestrator/extract.py
        temperature=0.2,
    )
    return parse_profile_extract_data(data)


def vertex_embed(text: str, dim: int = 768) -> list[float]:
    client = _vertex_client()
    model = os.environ.get("VERTEX_EMBED_MODEL", "text-embedding-005")
    result = client.models.embed_content(model=model, contents=text)
    values = result.embeddings[0].values
    if len(values) != dim:
        raise ValueError(f"expected_{dim}_dims_got_{len(values)}")
    return list(values)
