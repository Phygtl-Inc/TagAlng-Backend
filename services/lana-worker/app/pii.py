"""Deterministic PII redaction for text captured from user utterances.

This is a heuristic backstop, not a guarantee. Arbitrary first names can't be caught by
regex — the extractor prompt ("never capture a child's name/age/school") is the first line
of defense. This pass plugs the *deterministic* leak vectors before a claim's label /
source_quote / synonyms land in the database:

    emails, phone numbers, street addresses, school names, and child names introduced by a
    kinship word ("my daughter Sara" → "my daughter [kid]").

It is applied in claims_persist.clean_claims_for_persist, so it covers every claim write.
It deliberately never touches `concept` (a lowercase slug — brackets would break its format
CHECK) or the user's own chosen nickname.
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
_SCHOOL = re.compile(
    r"\b(?:[A-Z][a-zA-Z]+\s){1,3}"
    r"(?:Elementary|Middle|High|Preschool|Pre-?K|Academy|Montessori|Daycare|Kindergarten)"
    r"(?:\s+School)?\b"
)

# Child name after a kinship word. The kinship word matches case-insensitively (scoped
# inline flag), but the NAME must be genuinely Capitalized so we don't eat "my daughter loves".
_KIN_NAME = re.compile(
    r"\b((?i:son|daughter|kid|kiddo|child|baby|toddler|little one))s?"
    r"(\s+(?:named|called)\s+|\s+)([A-Z][a-z]{1,20})\b"
)

# "named Sara" / "call her Sara" without a preceding kinship word. Capture the phrase so
# it's preserved (only the name is dropped).
_NAMED = re.compile(r"\b((?:named|call(?:ed)?\s+(?:him|her))\s+)([A-Z][a-z]{1,20})\b")


def _kin_sub(m: "re.Match[str]") -> str:
    # keep the kinship word + separator, drop the name
    return f"{m.group(1)}{m.group(2)}[kid]"


def redact_pii(text: str | None) -> str | None:
    """Replace emails / phones / addresses / schools / child-names with placeholders.

    Returns the input unchanged when there's nothing to redact (and passes None through).
    """
    if not text:
        return text
    out = _EMAIL.sub("[email]", text)
    out = _PHONE.sub("[phone]", out)
    out = _ADDRESS.sub("[address]", out)
    out = _SCHOOL.sub("[school]", out)
    out = _KIN_NAME.sub(_kin_sub, out)
    out = _NAMED.sub(r"\1[name]", out)
    return out
