"""Deterministic PII redaction for text captured from user utterances.

This is a heuristic backstop, not a guarantee. Arbitrary first names can't be caught by
regex — the extractor prompt ("never capture a child's name/age/school") is the first line
of defense. This pass plugs the *deterministic* leak vectors before a claim's label /
source_quote / synonyms land in the database:

    emails, phone numbers, street addresses, school names, child names introduced by a
    kinship word ("my daughter Sara" → "my daughter [kid]"), and child ages tied to a
    child ("Emma is 4" → "[kid] is [child_stage:prek]" — a stage BAND, never the age).

Stage bands (the only child datum we ever persist):
    0–1 baby · 1–3 toddler · 3–5 prek · 5+ school

It is applied in claims_persist.clean_claims_for_persist and at every other durable
write of user-derived text (latent signals, local signals, inquiry capture, rapport gap
questions, feature requests, moderation flags). It deliberately never touches `concept`
(a lowercase slug — brackets would break its format CHECK) or the user's own chosen
nickname. In-session use of a child's name is allowed; the boundary is persistence.

This module also owns the conversational trust guard: Lana must never CLAIM to store a
child's name/school/age (see enforce_child_pii_nonstorage) — the Profile promise is
"I never collect a child's name, age, photo, or school", and replies must match it.
"""

from __future__ import annotations

import re

_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")

# US-style phone: optional +1, 3-3-4 with common separators. Guarded so it won't eat
# arbitrary short number runs ("married 10 years").
_PHONE = re.compile(
    r"(?<!\d)(?:\+?1[\s.\-]?)?(?:\(?\d{3}\)?[\s.\-]?)\d{3}[\s.\-]?\d{4}(?!\d)"
)

# Street address: number + 1-3 Capitalized words + a street-type suffix.
_ADDRESS = re.compile(
    r"\b\d{1,6}\s+(?:[A-Z][a-zA-Z]+\.?\s){1,3}"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|"
    r"Court|Ct|Way|Place|Pl|Terrace|Ter)\b\.?",
    re.IGNORECASE,
)

# School: 1-3 Capitalized words followed by a school-type keyword. The leading capitalized
# word is required, so "elementary school teacher" (a job) is NOT redacted.
# Preschool-family names generalize to "a local preschool" (per the child-PII promise);
# other school types keep the neutral "[school]" placeholder.
_SCHOOL = re.compile(
    r"\b(?:[A-Z][a-zA-Z]+\s){1,3}"
    r"(Elementary|Middle|High|Preschool|Pre-?K|Academy|Montessori|Daycare|Kindergarten)"
    r"(?:\s+School)?\b"
)

_PRESCHOOL_TYPES = frozenset({"preschool", "prek", "daycare", "kindergarten"})


def _school_sub(m: "re.Match[str]") -> str:
    kind = re.sub(r"[^a-z]", "", m.group(1).lower())
    if kind in _PRESCHOOL_TYPES:
        return "a local preschool"
    return "[school]"

# Child name after a kinship word. The kinship word matches case-insensitively (scoped
# inline flag), but the NAME must be genuinely Capitalized so we don't eat "my daughter loves".
_KIN_WORDS = r"(?i:son|daughter|kid|kiddo|child|baby|toddler|little one)"
_KIN_NAME = re.compile(
    rf"\b({_KIN_WORDS})s?"
    r"(,?\s+(?:named|called)\s+|,?\s+)([A-Z][a-z]{1,20})\b"
)

# "named Sara" / "call her Sara" without a preceding kinship word. Capture the phrase so
# it's preserved (only the name is dropped).
_NAMED = re.compile(r"\b((?:named|call(?:ed)?\s+(?:him|her))\s+)([A-Z][a-z]{1,20})\b")

# ── child ages → stage bands ─────────────────────────────────────────────────
# We never persist a child's age; only a coarse stage band: 0-1 baby · 1-3 toddler ·
# 3-5 prek · 5+ school. Ages are only banded when tied to a child subject — a kinship
# word or the [kid] placeholder the kin-name pass just left — so "married 10 years"
# or an adult's age never turns into a stage token.

# "<kin/[kid]> is 4", "my son just turned 2 years old", "daughter turning 5",
# and month forms ("our baby is 8 months old").
_CHILD_AGE_VERB = re.compile(
    rf"((?:\b{_KIN_WORDS}s?\b|\[kid\])(?:,?\s+\w+)??[,\s]+"
    r"(?i:is|are|just\s+turned|turned|turning|turns)\s+)"
    r"(\d{1,2})"
    r"(?:\s*(?i:(months?|mos?)|years?|yrs?|y\.?o\.?))?(?i:\s+old)?\b"
    # a number that quantifies something else ("kids are 2 blocks away") is not an age
    r"(?!\s*(?i:blocks?|miles?|minutes?|mins?|hours?|days?|weeks?|dollars?|bucks?|kids?|children)\b)",
)

# Attributive form in a child context: "a 4-year-old", "my 18-month-old".
_ATTR_AGE = re.compile(
    r"\b(\d{1,2})[\s-]*(?i:(months?|mos?)|years?|yrs?)[\s-]*(?i:old)\b"
)

# Pronoun form, only honored when the text has explicit child context elsewhere:
# "she's 4", "he just turned 2 years old".
_PRONOUN_AGE = re.compile(
    r"\b((?i:she|he)(?:['’]s|\s+is|\s+just\s+turned|\s+turned)\s+)"
    r"(\d{1,2})(?:\s*(?i:(months?|mos?)|years?|yrs?))?(?i:\s+old)?\b"
)

# "[kid] (4)" — a parenthesized age glued to the kid placeholder by an extractor label.
_KID_PAREN_AGE = re.compile(r"(\[kid\]\s*)\(\s*(\d{1,2})\s*(?i:(months?|mos?))?\s*\)")

_CHILD_CONTEXT = re.compile(rf"\b{_KIN_WORDS}s?\b|\[kid\]|\[child_stage:")

_MAX_CHILD_YEARS = 17


def child_stage_band(age_years: float) -> str:
    """Map an age in years to the coarse stage band we're allowed to keep."""
    if age_years < 1:
        return "baby"
    if age_years < 3:
        return "toddler"
    if age_years < 5:
        return "prek"
    return "school"


def _band_from_match(num: str, month_unit: str | None) -> str | None:
    try:
        age = float(num)
    except ValueError:
        return None
    if month_unit:
        age = age / 12.0
    if age > _MAX_CHILD_YEARS:
        return None  # not plausibly a child's age — leave it alone
    return child_stage_band(age)


def _child_age_verb_sub(m: "re.Match[str]") -> str:
    band = _band_from_match(m.group(2), m.group(3))
    if band is None:
        return m.group(0)
    return f"{m.group(1)}[child_stage:{band}]"


def _age_only_sub(m: "re.Match[str]") -> str:
    band = _band_from_match(m.group(1), m.group(2))
    if band is None:
        return m.group(0)
    return f"[child_stage:{band}]"


def _pronoun_age_sub(m: "re.Match[str]") -> str:
    band = _band_from_match(m.group(2), m.group(3))
    if band is None:
        return m.group(0)
    return f"{m.group(1)}[child_stage:{band}]"


def detect_child_stage(text: str | None) -> str | None:
    """First stage band implied by a child age in `text`, or None.

    Used to keep the structured stage fact ("you've got a pre-K kiddo") after the
    raw age has been redacted away.
    """
    if not text:
        return None
    m = _CHILD_AGE_VERB.search(text)
    if m:
        return _band_from_match(m.group(2), m.group(3))
    if _CHILD_CONTEXT.search(text):
        m = _ATTR_AGE.search(text)
        if m:
            return _band_from_match(m.group(1), m.group(2))
        m = _PRONOUN_AGE.search(text)
        if m:
            return _band_from_match(m.group(2), m.group(3))
    m = re.search(r"\[child_stage:(baby|toddler|prek|school)\]", str(text))
    if m:
        return m.group(1)
    return None


def has_child_pii(text: str | None) -> bool:
    """True when `text` carries a child's name, school, or age (the promise's scope)."""
    if not text:
        return False
    if _KIN_NAME.search(text) or _SCHOOL.search(text):
        return True
    return detect_child_stage(text) is not None


def _kin_sub(m: "re.Match[str]") -> str:
    # keep the kinship word + separator, drop the name
    return f"{m.group(1)}{m.group(2)}[kid]"


def extract_child_names(text: str | None) -> set[str]:
    """Names identified as a child's via kinship/naming context ("my daughter Emma").

    Once a name is known to be a child's, every other occurrence in the same text —
    "rain boots for Emma", "Emma's preschool" — is scrubbable via redact_names.
    """
    if not text:
        return set()
    names = {m.group(3) for m in _KIN_NAME.finditer(str(text))}
    names |= {m.group(2) for m in _NAMED.finditer(str(text))}
    return names


def redact_names(text: str | None, names: set[str]) -> str | None:
    """Scrub every occurrence of the given child names (incl. possessives) from text."""
    if not text or not names:
        return text
    pattern = re.compile(
        r"\b(?:" + "|".join(re.escape(n) for n in sorted(names)) + r")(['’]s)?\b"
    )
    return pattern.sub(lambda m: "[kid]" + (m.group(1) or ""), str(text))


def redact_pii(text: str | None, *, known_child_names: set[str] | None = None) -> str | None:
    """Replace emails / phones / addresses / schools / child names+ages with placeholders.

    Child ages become stage-band tokens ("Emma is 4" → "[kid] is [child_stage:prek]");
    preschool-family school names generalize to "a local preschool". A name introduced
    with kinship context anywhere in the text is scrubbed at every occurrence; callers
    holding sibling fields (e.g. a claim's label next to its source_quote) can pass
    `known_child_names` gathered across all fields so the name never survives in a
    field that lacks the kinship context itself.
    Returns the input unchanged when there's nothing to redact (and passes None through).
    """
    if not text:
        return text
    names = extract_child_names(text) | (known_child_names or set())
    out = _EMAIL.sub("[email]", text)
    out = _PHONE.sub("[phone]", out)
    out = _ADDRESS.sub("[address]", out)
    out = _SCHOOL.sub(_school_sub, out)
    out = _KIN_NAME.sub(_kin_sub, out)
    out = _NAMED.sub(r"\1[name]", out)
    out = redact_names(out, names) or out
    out = _CHILD_AGE_VERB.sub(_child_age_verb_sub, out)
    out = _KID_PAREN_AGE.sub(
        lambda m: (
            f"{m.group(1)}([child_stage:{_band_from_match(m.group(2), m.group(3))}])"
            if _band_from_match(m.group(2), m.group(3))
            else m.group(0)
        ),
        out,
    )
    if _CHILD_CONTEXT.search(out):
        out = _ATTR_AGE.sub(_age_only_sub, out)
        out = _PRONOUN_AGE.sub(_pronoun_age_sub, out)
    return out


# ── structured attributes (latent signals etc.) ─────────────────────────────

# Attribute keys that may carry a child's name / school — always dropped.
_ATTR_DROP_KEYS = re.compile(
    r"(?:^|_)(?:name|names|school|preschool|daycare|kindergarten|teacher)(?:_|$)", re.I
)
# Attribute keys that carry an age — converted to a stage band.
_ATTR_AGE_KEYS = re.compile(r"(?:^|_)(?:age|ages)(?:_|$)|^age$", re.I)


def redact_child_attributes(attrs: dict | None, *, subject: str = "unknown") -> dict:
    """Sanitize a structured attribute dict before persistence.

    Child ages (``child_age``, ``age``…) become ``child_stage`` bands; name/school-ish
    keys are dropped when the subject is a child; every string value goes through
    :func:`redact_pii`. Non-child subjects keep their attributes (minus text redaction).
    """
    if not isinstance(attrs, dict):
        return {}
    is_child = str(subject or "").lower() == "child"
    out: dict = {}
    for key, value in attrs.items():
        k = str(key)
        child_key = is_child or k.lower().startswith(("child", "kid", "son", "daughter"))
        if child_key and _ATTR_AGE_KEYS.search(k):
            band: str | None = None
            v = value[0] if isinstance(value, list) and value else value
            try:
                band = child_stage_band(float(v))
            except (TypeError, ValueError):
                band = detect_child_stage(f"kid is {v}") if v is not None else None
            if band:
                out["child_stage"] = band
            continue
        if child_key and _ATTR_DROP_KEYS.search(k):
            continue
        if isinstance(value, str):
            out[k] = redact_pii(value) or value
        else:
            out[k] = value
    return out


# ── conversational trust guard ───────────────────────────────────────────────
# The Profile promise: "I never collect a child's name, age, photo, or school."
# A reply must never assert the opposite ("I keep Emma's name and school private…").
# This deterministic guard rewrites any storage-asserting sentence into the
# non-storage acknowledgment, regardless of what the model produced.

# Possessive child reference: "Emma's name", "her school", "their ages",
# "your daughter's name". "your name" (the user's own, which we DO store) is excluded.
_CHILD_POSSESSIVE = (
    r"(?:[A-Z][a-z]{1,20}['’]s|(?i:her|his|their)|"
    rf"(?i:your|my|our)\s+{_KIN_WORDS}s?['’]?s?)"
)
_PII_NOUN = r"(?i:name|school|preschool|daycare|age)s?"
_STORAGE_VERB = (
    r"(?i:keep(?:s|ing)?|kept|sav(?:e|es|ed|ing)|stor(?:e|es|ed|ing)|"
    r"remember(?:s|ed|ing)?|not(?:e|es|ed|ing)|record(?:s|ed|ing)?|"
    r"hold(?:s|ing)?(?:\s+on\s*to)?|jot(?:s|ted|ting)?(?:\s+down)?)"
)

_STORAGE_ASSERT = re.compile(
    # "keep Emma's name … private" / "I've saved her school" / "I'll remember their ages"
    rf"\b{_STORAGE_VERB}\b[^.!?]{{0,60}}?\b{_CHILD_POSSESSIVE}\s+{_PII_NOUN}\b"
    rf"|\b{_CHILD_POSSESSIVE}\s+{_PII_NOUN}\b[^.!?]{{0,40}}?\b(?i:private|safe|on\s+file|saved|stored)\b"
)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")

# A negated storage claim ("I don't keep her name", "we never store their ages") is the
# CORRECT behavior — it must not trip the guard that exists to insert exactly that line.
_NEGATION_BEFORE = re.compile(
    r"(?i)\b(?:don['’]?t|do\s+not|doesn['’]?t|does\s+not|didn['’]?t|did\s+not|"
    r"won['’]?t|will\s+not|would\s+never|never|not|no\s+longer|without)\s*$"
)


def _match_is_negated(text: str, start: int) -> bool:
    return bool(_NEGATION_BEFORE.search(text[max(0, start - 24):start]))

_STAGE_PHRASE = {
    "baby": "baby",
    "toddler": "toddler",
    "prek": "pre-K",
    "school": "school-age",
}


def child_pii_ack_line(user_message: str | None = None, reply: str | None = None) -> str:
    """The non-storage acknowledgment that matches the Profile promise."""
    blob = f"{user_message or ''} {reply or ''}"
    if re.search(r"\b(?i:daughter|girl)\b|\bher\s+name\b", blob):
        pron = "her"
    elif re.search(r"\b(?i:son|boy)\b|\bhis\s+name\b", blob):
        pron = "his"
    else:
        pron = "their"
    stage = detect_child_stage(user_message) or detect_child_stage(reply)
    if stage:
        return (
            f"I don't keep {pron} name — just that you've got a "
            f"{_STAGE_PHRASE[stage]} kiddo, which helps me match you."
        )
    return (
        f"I don't keep {pron} name or school — just your family stage, "
        "which helps me match you."
    )


def reply_asserts_child_pii_storage(reply: str | None) -> bool:
    """True when a reply claims to keep/save a child's name, school, or age.

    Negated claims ("I don't keep her name…") are the promise-compliant phrasing and
    do not count.
    """
    if not reply:
        return False
    text = str(reply)
    for m in _STORAGE_ASSERT.finditer(text):
        if not _match_is_negated(text, m.start()):
            return True
    return False


def enforce_child_pii_nonstorage(reply: str | None, user_message: str | None = None) -> str | None:
    """Rewrite storage-asserting sentences to the non-storage acknowledgment.

    In-session use of a child's name is fine ("Emma sounds like a sweetheart" passes
    untouched); only sentences claiming we KEEP the name/school/age are replaced. The
    replacement appears once; further offending sentences are dropped.
    """
    if not reply or not reply_asserts_child_pii_storage(reply):
        return reply
    ack = child_pii_ack_line(user_message, reply)
    out: list[str] = []
    replaced = False
    for sentence in _SENTENCE_SPLIT.split(str(reply)):
        if reply_asserts_child_pii_storage(sentence):
            if not replaced:
                out.append(ack)
                replaced = True
            continue
        out.append(sentence)
    return " ".join(s for s in out if s).strip() or ack
