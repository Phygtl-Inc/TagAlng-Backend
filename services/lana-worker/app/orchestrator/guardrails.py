import re
from dataclasses import dataclass
from typing import Any


@dataclass
class InputRailResult:
    passed: bool
    safety_triggered: bool
    category: str | None = None
    severity: str | None = None
    flags: dict[str, Any] | None = None
    template_response: str | None = None


_SAFETY_PATTERNS: list[tuple[str, str, str]] = [
    (r"\b(kill myself|suicide|end my life|can't go on)\b", "mental_health", "high"),
    (r"\b(hurt myself|self[- ]harm)\b", "mental_health", "high"),
    (r"\b(throw(n)? something at me|hit me|abuse(d)? me)\b", "dv", "high"),
    (r"\b(are you safe|unsafe at home)\b", "dv", "medium"),
    (r"\b(child is missing|kidnapped)\b", "child_safety", "high"),
]

_SAFETY_TEMPLATES = {
    "mental_health": (
        "I'm here. *Are you safe right now?* "
        "Postpartum Support International is 1-800-944-4773 (24/7). "
        "If you need someone now, **988** connects to crisis support. "
        "I'll stay with you."
    ),
    "dv": (
        "That's not OK. *Are you and the kids safe right now?* "
        "National Domestic Violence Hotline: 1-800-799-7233 or text START to 88788. "
        "I won't share this with anyone on the block unless you ask."
    ),
    "child_safety": (
        "That sounds urgent. *Are the kids safe right now?* "
        "If anyone is in immediate danger, call **911**. "
        "I'm here while you figure out next steps."
    ),
    "crisis": (
        "I'm glad you said something. If you're in immediate danger, call **911**. "
        "Crisis line **988** is available 24/7. I'll stay with you."
    ),
}

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


def run_input_rails(utterance: str) -> InputRailResult:
    lower = utterance.lower()
    for pattern, category, severity in _SAFETY_PATTERNS:
        if re.search(pattern, lower, re.I):
            return InputRailResult(
                passed=False,
                safety_triggered=True,
                category=category,
                severity=severity,
                flags={"rail": "safety_keyword"},
                template_response=_SAFETY_TEMPLATES.get(category, _SAFETY_TEMPLATES["crisis"]),
            )
    return InputRailResult(passed=True, safety_triggered=False, flags={"rail": "ok"})


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
