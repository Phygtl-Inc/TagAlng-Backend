import re

# Crisis (self-harm / DV / emotional distress) is AI-detected — the discovery classifier
# flags goal=crisis by meaning and authors the empathetic response itself (see
# discovery_slots.py CRISIS + discovery_route._respond_crisis). There is deliberately no
# keyword list here: the old regex rail missed everything phrased outside its five patterns
# ("I cry every night" got a ZIP ask) while the classifier reads infinite phrasings.

# Deterministic backstop for inappropriate/abusive content. The AI router (system.unsafe)
# is the primary detector; these high-precision patterns catch egregious cases even if the
# classifier is bypassed or wrong. Kept narrow to avoid false positives on benign chat.
_UNSAFE_PATTERNS: list[tuple[str, str]] = [
    (r"\b(sex doll|blow ?job|porn|nudes?|dick pic|sext|horny|escort|hooker|onlyfans)\b", "sexual"),
    (r"\bsend (?:me )?(?:your )?nudes?\b", "sexual"),
    (r"\b(fuck you|cunt|bitch|motherfucker|retard|faggot|nigger|kys)\b", "abuse"),
    (r"\b(buy|sell|score|get me)\s+(?:some\s+)?(?:cocaine|heroin|meth|crack|fentanyl)\b", "illegal"),
    (r"\b(build|make)\s+(?:a\s+)?(?:bomb|explosive)\b", "illegal"),
]


def utterance_is_unsafe(utterance: str) -> tuple[bool, str | None]:
    """Regex backstop for inappropriate/abusive content. Returns (matched, kind). Narrow by
    design — the AI router is the main path; this only rescues obvious misses."""
    lower = str(utterance or "").lower()
    for pattern, kind in _UNSAFE_PATTERNS:
        if re.search(pattern, lower, re.I):
            return True, kind
    return False, None


_PII_PHONE = re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b")
_PII_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")


def scrub_pii(text: str) -> str:
    out = _PII_PHONE.sub("[phone redacted]", text)
    out = _PII_SSN.sub("[ssn redacted]", out)
    return out


def check_refusal_without_capture(response: str, capture_fired: bool) -> bool:
    """Returns True if check passes (ok to send)."""
    if capture_fired:
        return True
    refusal_markers = (
        "can't help",
        "cannot help",
        "outside my scope",
        "don't have access",
        "not something i",
        "i'm not able to",
    )
    lower = response.lower()
    if any(m in lower for m in refusal_markers):
        return False
    return True
