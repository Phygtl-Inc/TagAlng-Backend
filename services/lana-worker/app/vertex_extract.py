import os
import re
from datetime import datetime, timezone
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
- A fact about the user's CHILD is a claim about the child: subject "child", subject_name when they name them, subject_age when they state it. The label describes the ACTIVITY, never the child ("Does karate", not "Sara does karate")
- Faith, religion, sobriety, recovery, LGBTQ+: disclosure MUST be "mutual"

Transcript:
"""

INCREMENTAL_EXTRACT_PROMPT = """You extract identity threads from ONE user message in a block-based \
neighborhood app chat, \
and you stay curious — after capturing what they said, you propose ONE warm follow-up that draws out more.

Output ONLY valid JSON (no markdown):
{
  "nickname": "first name neighbors should use, or null",
  "nickname_quote": "verbatim words from THIS message where they state it, or null",
  "nickname_is_rename": false,
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
      "transient": false,
      "subject": "self | child — who the claim is ABOUT, default self",
      "subject_name": "the child's first name IF they said it, else null",
      "subject_age": null
    }
  ],
  "subject_updates": [{"name": "child's first name", "age": 9}],
  "retracted_concepts": ["exact concept slug from ALREADY ON PROFILE the user is walking back — usually empty"],
  "circle_candidates": [
    {
      "circle_type": "fitness",
      "circle_key": "spin_class",
      "raw_phrase": "my Tuesday spin class",
      "place_name": "the venue name they SAID, or null",
      "noun": "2-3 word noun for what this community IS",
      "emoji": "ONE emoji for it",
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
  ],
  "followup_question": "one warm question that adds a NEW MATCHABLE facet (never backstory), or null",
  "followup_topic": "a short grammatical teaser for the question, 2-5 words, e.g. 'about your reading…', 'about the World Cup…', 'about your running…' — or null when followup_question is null"
}

EVERY key above is REQUIRED in your output, every turn. "claims" and "circle_candidates" are \
two SEPARATE ledgers filled by two SEPARATE decisions — never let one stand in for the other, and \
never omit a key to mean empty: write [] or null explicitly. Decide circle_candidates BEFORE you \
write followup_question.

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
- If no identity content (greetings, "ok", ZIP, phone), return {"nickname": null, "nickname_quote": null, "nickname_is_rename": false, "kids_count": null, "claims": [], "circle_candidates": [], "place_feature_candidates": [], "followup_question": null}
- Split distinct threads — capture EACH one, do not collapse (e.g. "pakistani dad, married 10 years, speak 5 languages, do triathlon" → pakistani_heritage + multilingual + married_ten_years + triathlon; "dad" and kid count go to role/kids_count, never a claim)
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
- EVERY claim must be a facet a NEARBY NEIGHBOR COULD SHARE. Apply the same test the followup_question must pass: "would knowing this change who they connect with?" Activities, hobbies, teams, cuisines, heritage, faith, languages, life stage, occupation, local spots, weekly rhythm — all pass. Pure aesthetic or consumer preferences do NOT: a favorite color, brand, phone, app, streaming service, car ("my favorite color is blue", "I'm an iPhone person" → capture NOTHING). Nobody meets a neighbor over a color, and a stored fact with no matchable angle can only ever produce a pointless question later. When the preference is attached to something DOABLE, capture the doable thing instead: "I collect blue pottery" → pottery_collector; "I paint, mostly blues" → painter; "I love steakhouses" → steak_lover (food is a real local facet — a color is not).
- Do NOT emit the SAME thread twice with different wording — one claim per distinct thread
- "details": short third-person sub-facts (2-6 words each, max 3 per claim) that ADD texture beyond the label — rhythm, level, setting, sub-type (e.g. label "State-level swimmer" + details ["Swims every weekend"]). Empty [] when the label already says everything. Never restate the label as a detail.
- kids_count: an integer ONLY when the user states HOW MANY children they have ("2 sons" → 2, "three kids" → 3). null otherwise. Private, never a claim. It is a COUNT only — the child's name and age ride on the claim itself (see "subject" below), never here.
- subject / subject_name / subject_age — WHO a claim is about. Default "self". Use "child" for anything the user says about their kid, and carry what they told you: "my 7-year-old does karate" → subject "child", subject_age 7, subject_name null; "my daughter Sara swims Tuesdays" → subject "child", subject_name "Sara", subject_age null; "Sara is 7 and does karate" → both. ONE claim per child-fact — a second child doing the same thing is a SEPARATE claim with that child's own name. HARD RULES: (a) the label and source_quote must NOT contain the child's name — the name lives ONLY in subject_name (write label "Does karate", quote "does karate"); (b) subject_age is the age they STATED, a plain integer 0-25, null when they didn't say it — never guess it from a school grade or a photo; (c) still NEVER capture a child's school as a claim label — schools go to circle_candidates, exactly as before; (d) a fact about anyone else (spouse, parent, neighbour) stays subject "self" only when it genuinely describes the USER, otherwise skip it.
- role: the user's household role ONLY when they state it about THEMSELVES: "parent" ("my kids", "I'm a dad"), "expecting" ("baby on the way"), "grandparent" ("my grandkids"), "caregiver" ("the family I care for"), "guardian", "relative" ("my nephew lives with us"). null otherwise — never infer from what they search for. Private, never a claim.
- grammatical_gender: "feminine" or "masculine" ONLY from the user's own gendered SELF-reference in a gendered language ("estoy cansada" → feminine, "estou animado" → masculine) or an explicit self-label ("I'm his mom" → feminine, "I'm their dad" → masculine). null otherwise — NEVER guess from a name, and never from a third party. Used only for grammatical agreement, never shown.
- NEVER extract race, exact age, sex/gender demographics, street address
- NEVER extract negative or exclusion claims ("not Brazilian", "no Italian", "without X")
- RETRACTIONS — the user can take something back, and we must let them. When this message says a thread on their profile is NO LONGER TRUE ("blue isn't really my favorite anymore", "I stopped playing squash", "we moved away from Lake Nona", "I don't do triathlon these days", "actually I'm not a teacher, I'm a nurse"), put the EXACT concept slug from ALREADY ON PROFILE into "retracted_concepts". Do NOT emit it as a claim, and do NOT emit a negated claim about it. A correction that also states the NEW truth does both: retract the old slug AND emit the new claim ("not a teacher, I'm a nurse" → retracted_concepts ["teacher"] + a nurse claim). Only ever list slugs that appear in ALREADY ON PROFILE, copied exactly — never invent one, and never retract on a mere doubt ("not sure I still like it"), only on a plain statement that it is over.
- The user's OWN parenting status is not a claim (that is `role` + kids_count). What a CHILD does IS a claim — with subject "child".
- subject_updates: a child's name/age with NO activity attached ("Sara is 9", "the little one's name is Tom", "my oldest just turned 12"). These are NOT claims — a child is not an interest. Emit {"name": …, "age": …} here (either field may be null) and leave "claims" empty for that fact. NEVER attach the age to an unrelated thread the child happens to have. Empty [] when the message states no such thing.
- ONLY extract first-person identity ("I am", "I'm", "my heritage") — NOT who they search for ("find Brazilian mom", "looking for Pakistani neighbors")
- Faith, religion, sobriety, recovery, LGBTQ+: disclosure MUST be "mutual"
- nickname — ONLY when the user is telling you what THEY want to be called ("I'm Brinda", "call me \
Sam", "my name is brigade"). A name is the one fact the user hears in EVERY reply, so a wrong one is \
insulting and obvious. You are the only thing that can change it — be strict:
  · A CORRECTION is still a statement of their name, and you MUST resolve it to the name they \
AFFIRM — never the name they deny, and never the negation word itself: "my name is not Orlando but \
Tom" → "Tom" (NOT "not", NOT "Orlando"); "it's Tom, not Orlando" → "Tom"; "wrong, I'm Tom" → "Tom".
  · A denial with NO replacement is null, not a name: "I'm not Joe" → null, "that's not my name" → null.
  · When LANA JUST ASKED what to call them ("what should neighbors call you?", "what's your \
name?"), a bare word IS the answer to that — "Tommaso" → "Tommaso". Take it.
  · For ANY OTHER question in LANA JUST ASKED, a bare word is an ANSWER to that question, not a \
name. A city, venue, gym, church, dish, team, or time is never a nickname — asked "which Lagoinha \
location?", the message "Orlando" is a place and nickname is null.
  · null when the message merely CONTAINS a name-shaped word (a place, a brand, a saint, a team) \
without the user claiming it for themselves, and null for anyone else's name ("my son Marco" → null).
  · null when it matches CURRENTLY SAVED NAME — there is nothing to change.
- nickname_quote: the VERBATIM words from THIS message where they state their name ("my name is not \
Orlando but Tom" → "but Tom"; "call me Sam" → "call me Sam"). It MUST appear character-for-character \
in the message. null whenever nickname is null. A nickname whose quote is absent from the message is \
DISCARDED, so quote exactly.
- nickname_is_rename: true ONLY when CURRENTLY SAVED NAME is present AND this message tells you to \
stop using it and use a different one instead. This is a HIGH bar — it takes an explicit correction \
or request ("my name is not Orlando but Tom", "call me Tom from now on", "stop calling me Orlando"). \
Mentioning a name in passing, answering a question, or being unsure is NOT a rename: leave it false \
and we keep the saved name. false when there is no saved name (that is a first fill, not a rename).
- circle_candidates — DECIDE THIS BEFORE followup_question. A circle is a real-world COMMUNITY the \
user belongs to or attends recurringly. It is a SEPARATE ledger from claims and is judged separately: \
emitting the claim does NOT discharge this field, and most activity messages produce BOTH ("I play \
pickleball regularly with friends" → claim plays_pickleball AND circle {fitness, pickleball_group}). \
Run this three-step test on anything the user says about their OWN activities, places, or groups:
  STEP 1 — is it about THEMSELVES? First-person membership or attendance. Not who/what they are \
searching for, not someone else's.
  STEP 2 — is there a RHYTHM or a MEMBERSHIP? Any of: "regularly", "every week", "on weekends", \
"on Sundays", "twice a week", "always", a named class/club/team, or a possessive that implies \
belonging ("my gym", "our church").
  STEP 3 — is there a GROUP or a PLACE? EITHER is enough on its own. A group = other people \
("with friends", "with my crew", "with my team", "we play…"). A place = a venue or even just the \
KIND of venue ("the gym", "the court", "the ground").
  All three pass → EMIT. A missing venue name is NEVER a reason to skip: the regular group IS the \
community, and the place gets asked later through a separate grounding step. Confidence 0.8+ when \
the rhythm is stated outright.
- circle_candidates EMIT list — every one of these MUST produce a circle, place_name null:
  "i play pickleball regularly with friends" → {fitness, pickleball_group}
  "I play pickleball on weekends with friends" → {fitness, pickleball_group}
  "I play squash with friends every week" → {fitness, squash_group}
  "we play futsal on Sundays" → {fitness, futsal_group}
  "I play futsal regularly on the ground" → {fitness, futsal_group}
  "I play table tennis regularly" → {fitness, table_tennis_group}
  "I go to the gym every weekend" / "my gym" → {fitness, gym}
  "my Tuesday spin class" → {fitness, spin_class}
  "our church" → {faith, church_group}
  "my book club" → {hobby, book_club}
- circle_candidates SKIP list — and ONLY these:
  a one-off ("tried a yoga class once"), an aspiration ("thinking of joining a gym"), watching \
without doing ("I like watching football"), solo with NO group AND NO place ("I run every morning"), \
no rhythm AND no group ("I play squash sometimes"), or the thing they are SEARCHING for ("any yoga \
classes nearby?", "show me cycling activities").
  When a message looks like it could sit on either list, the EMIT list WINS. "With friends" or \
"we" always satisfies STEP 3, so it can never fall under the solo exclusion.
- The search exclusion applies ONLY to the thing being searched for — NEVER to background the user \
gives about themselves in the same message ("any weekday events? I'm busy weekends because I go to \
the gym every weekend" → the events are NOT a circle, {fitness, gym} IS; "looking for a Saturday \
playdate since our church group meets Sundays" → {faith, church_group}).
- circle_type MUST be one of: school, faith, fitness, kids_activity, neighborhood, hobby, support, \
heritage, friends, other. circle_key: snake_case slug naming the COMMUNITY (same format as concept) \
— reuse the SAME slug you would pick for that community every time so re-mentions corroborate one \
row instead of creating a second ("Life Time" is always life_time, never regular_weekend_gym_goer).
- noun / emoji describe THIS community, and both are shown to the user. circle_type \
is a coarse grouping bucket where every sport is "fitness", so it cannot supply \
either: a table-tennis club came out as "your gym" with a 🏋️. Write what the thing \
actually is — "table tennis club" 🏓, "small group" ⛪, "spin class" 🚴, "book club" \
📚, "run club" 🏃. noun is lower-case, no venue name in it (it becomes "your <noun>", \
and naming the place there would leak it); emoji is exactly one character.
- The ALREADY ON PROFILE dedupe rule does NOT apply to circle_candidates. Re-state the circle EVERY \
time the community is mentioned, even when the matching claim is already on the profile and even when \
you emitted it before — repeats are harmless corroboration, silence loses the circle. Never capture a \
child's name in raw_phrase.
- place_name (inside circle_candidates): the venue/business/organization name they actually SAID for \
that community, verbatim and nothing else — "I go to the gym at Fitness CF" → "Fitness CF"; "our \
church is St. Luke's" → "St. Luke's"; "my kids are at Lake Nona Middle" → "Lake Nona Middle". null \
whenever they named only the activity or the KIND of place ("my gym", "we play futsal on Sundays", \
"my Tuesday spin class", "our church"). NEVER invent, complete, or guess a name they did not say — a \
wrong name gets them attached to the wrong place. Do not put the activity word in it ("gym", "church" \
alone are not names).
- INTERLOCK — if your followup_question asks WHERE or WHICH SPOT the user does a recurring activity, \
that activity MUST also appear in circle_candidates. Asking "which court do you play at?" while \
leaving circle_candidates empty is a contradiction: the answer would have nothing to attach to. \
Either emit the circle, or ask a different dimension.
- followup_question — becomes a "By the way…" tile on their home screen; their answer is stored as an identity claim used to match them with nearby neighbors. Warm neighborhood-concierge tone, not an interviewer; ask only what genuinely helps them connect locally. Propose ONE only if it adds a CONNECTION-MATCHABLE facet — something that would help them MEET or RELATE to nearby neighbors: shared activities/hobbies, kids or family stage, local spots they go, cultural or community ties, their weekly rhythm. Generic consumer/brand/device/product preferences are NOT connection facets — which phone, which apps, gadgets, streaming services, operating system → return null (no neighbor connects over that). Reason about what you ALREADY know to hit a real GAP. Two shapes: (1) SHARPEN a "vague": true claim — vague tech_worker → "What kind of tech — engineering, product, design?"; "speaks 5 languages" → "Which five?". (2) FILL a matchable dimension you don't yet know. VARY the dimension to fit the topic — do NOT default to "solo or with others" for everything (that has become repetitive). Choose the ONE most natural from a range: sub-type/genre (books → "Any genres you gravitate to?"), frequency/rhythm (running → "Mornings or weekends?"), setting or local spot ("A local place you like for it?" — allowed ONLY when that activity is also in circle_candidates, see INTERLOCK above), skill/level, doing-it-with-others, kids' involvement, teach-vs-learn. FORBIDDEN — never ask an opinion, feeling, or origin-story question (anything asking why, how you started, what you enjoy/love most, or what "caught your interest"); those add NO matchable facet — replace with a concrete one or return null. Do NOT repeat a question shape listed in ALREADY ASKED above; if the only fitting angle was already asked, return null or pick a different dimension. Write ONLY the question itself — NO "By the way", no greeting or lead-in phrase (the tile shows its own "By the way…" framing; a prefix just doubles it). Short (<120 char), warm, OPEN, reference what they said. Return null when nothing is vague AND no fresh matchable dimension fits — silence beats filler. HARD RULE: null for any sensitive / help-seeking topic — divorce or relationship trouble, health/medical, mental health/safety, money/debt, legal/immigration — and when the message is a question aimed at you.
- followup_topic: a 2-5 word grammatical lead-in that names the thread for the tile, ending with "…" — e.g. "about your reading…", "about the World Cup…", "about your Portuguese…". Write natural English; NEVER glue a raw label ("about your interested in books…" is wrong). null whenever followup_question is null.
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


_SUBJECT_KINDS = {"self", "child", "parent", "spouse", "sibling", "grandparent", "household", "other"}


def _parse_subject(item: dict[str, Any]) -> tuple[str, str | None, int | None]:
    """(subject_kind, subject_name, subject_birth_year) from one raw claim.

    A stated age is converted to a birth year here, once, against the year of
    capture — storing the age itself would silently rot every January.
    """
    kind = str(item.get("subject") or item.get("subject_kind") or "self").strip().lower()
    if kind not in _SUBJECT_KINDS:
        kind = "self"
    if kind == "self":
        # The DB CHECK enforces this too; drop it here so a stray name from the
        # model can't reach a row where nothing is expecting one.
        return "self", None, None
    raw_name = str(item.get("subject_name") or "").strip()[:40]
    # A name is a name — anything with digits or punctuation is the model
    # improvising ("my kid", "7yo"), and improvised names are worse than none.
    # Letters only (any script) plus apostrophe/hyphen/space: "7yo", "kid 2" and
    # "x1" are the model improvising, and an improvised name is worse than none.
    name = raw_name if raw_name and re.match(r"^[^\W\d_][^\d_]{0,39}$", raw_name, re.UNICODE) else None
    birth_year: int | None = None
    raw_age = item.get("subject_age")
    try:
        age = int(raw_age) if raw_age is not None and str(raw_age).strip() != "" else None
    except (TypeError, ValueError):
        age = None
    if age is not None and 0 <= age <= 25:
        birth_year = datetime.now(timezone.utc).year - age
    return kind, name, birth_year


def parse_subject_updates(data: Any) -> list[dict[str, Any]]:
    """Name/age facts about a child that carry no activity of their own.

    Kept OUT of `claims` deliberately: a child is not an interest, and letting the
    model park an age on whichever thread it liked produced "Sara does karate" from
    a message that only said "Sara is 9". Returned as {name, birth_year} for the
    persist layer to apply to that child's existing rows.
    """
    if not isinstance(data, dict):
        return []
    raw = data.get("subject_updates")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw[:4]:
        if not isinstance(item, dict):
            continue
        _, name, birth_year = _parse_subject(
            {"subject": "child", "subject_name": item.get("name"), "subject_age": item.get("age")}
        )
        if name and birth_year:
            out.append({"name": name, "birth_year": birth_year})
    return out


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
        subject_kind, subject_name, birth_year = _parse_subject(item)
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
                subject_kind=subject_kind,
                subject_name=subject_name,
                subject_birth_year=birth_year,
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
            subject_kind = str(item.get("subject_kind") or "self")
            if subject_kind != "self":
                who = str(item.get("subject_name") or "").strip() or "unnamed"
                birth_year = item.get("subject_birth_year")
                age = (
                    f", age {datetime.now(timezone.utc).year - int(birth_year)}"
                    if birth_year
                    else ", age unknown"
                )
                line += f" [about their {subject_kind}: {who}{age}]"
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
        "NOT re-emitted. Only genuinely new topics get a new concept. Threads marked "
        "[about their child: …] belong to a child, not to the user — never re-emit one "
        "just because the message mentions that child; a bare name or age goes to "
        "subject_updates, never to claims:\n"
        + "\n".join(lines[:40])
        + "\n\n"
    )


def _name_context_block(current_nickname: str | None, asked_question: str | None) -> str:
    """The two facts the extractor needs before it is allowed to touch a name.

    Without the SAVED NAME it cannot tell a first fill from a rename, so every
    name-shaped word looked like an improvement. Without the QUESTION it cannot
    tell a name from an answer — a lone "Orlando" replying to "which Lagoinha
    location?" once overwrote a real name. Neither is derivable from the message.
    """
    name = str(current_nickname or "").strip()
    if name:
        lines = [
            f"CURRENTLY SAVED NAME: {name} — they are ALREADY called this. Emit nickname only to "
            "CHANGE it, and only with nickname_is_rename true. Otherwise leave nickname null."
        ]
    else:
        lines = ["CURRENTLY SAVED NAME: (none yet — a stated name here is a first fill, not a rename)"]
    q = str(asked_question or "").strip()
    if q:
        lines.append(
            f'LANA JUST ASKED: "{q}" — this message is most likely an ANSWER to that question. '
            "An answer is not a nickname unless the question asked what to call them."
        )
    return "\n".join(lines) + "\n\n"


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
    *,
    current_nickname: str | None = None,
    asked_question: str | None = None,
) -> Any:
    from app.orchestrator.llm import vertex_generate_json

    prompt = (
        INCREMENTAL_EXTRACT_PROMPT
        + _existing_claims_block(existing_labels)
        + _recent_questions_block(recent_questions)
        + _name_context_block(current_nickname, asked_question)
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
    *,
    current_nickname: str | None = None,
    asked_question: str | None = None,
) -> Any:
    """Extract claims via orchestrator LLM when configured; else Vertex."""
    import logging

    log = logging.getLogger(__name__)
    text = str(message or "").strip()
    system = (
        INCREMENTAL_EXTRACT_PROMPT
        + _existing_claims_block(existing_labels)
        + _recent_questions_block(recent_questions)
        + _name_context_block(current_nickname, asked_question)
    )
    try:
        from app.orchestrator.llm import extractor_model, llm_configured, llm_json

        if llm_configured():
            return llm_json(
                model=extractor_model(),
                system=system,
                user_payload=text,
                # Room for all six fields at once. At 512 a full turn (claims with
                # synonyms + details, followup, circles, features) has to choose
                # what to drop, and circles — last in the schema — lost.
                max_tokens=1024,
                temperature=0.2,
            )
    except Exception:
        log.exception("llm_incremental_claim_extract_failed")
    return vertex_extract_claims_from_utterance(
        text,
        existing_labels,
        recent_questions,
        current_nickname=current_nickname,
        asked_question=asked_question,
    )


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
