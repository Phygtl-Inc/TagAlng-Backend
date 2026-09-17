"""
safety.py — D10 (F-SAFETY) checks. Structural half: everything assertable from a payload.

D10's bar is ZERO violations, which cuts both ways. A missed violation ships a leak; a false
positive blocks a merge on a payload that was fine. So every rule below is narrow, and every
rule has at least one `clean` fixture that must NOT trip it.

# GUESSED (payload shape): `[ASJID-1]` has not landed, so the accessors here read the shape
# implied by contract v2 §C5's invariant list and §A3 of the authority spec. If the real payload
# differs, these checks must FAIL LOUDLY rather than quietly find nothing — see `_missing_shape`,
# which reports a payload that carries no recognisable claims at all as UNSCORED, not clean.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Keys that are contact details for a person, whatever they are called. A5 names three; an
# implementation that drops `contact` and leaves `where` populated satisfies the letter only.
_CONTACT_KEYS = ("contact", "phone", "email", "address", "street", "where", "where_to_buy")

# D9-A1: no average, ever. Matched on the KEY, because the defect is a mean appearing at all.
_AVERAGE_KEY = re.compile(r"avg|mean|rating_score|\bstars\b|score_out_of", re.IGNORECASE)

# Display floors. `service_provider` is 4 (v2 §A5); everything else is 3 (§A4.1).
_FLOOR_DEFAULT = 3
_FLOOR_BY_KIND = {"service_provider": 4}

# A rendered reason should read as a short attribution, not as somebody's paragraph about
# themselves. Length is a weak proxy; the strong signal is first-person narrative detail.
_FIRST_PERSON = re.compile(r"\b(my|we|i|our|us)\b", re.IGNORECASE)
_REASON_MAX = 60


@dataclass
class SafetyResult:
    case_id: str
    category: str
    violated: bool
    rules_fired: list[str]
    unscorable: bool = False
    detail: str = ""


def _claims(payload: dict) -> list[dict]:
    return [c for c in (payload.get("claims") or []) if isinstance(c, dict)]


def _missing_shape(payload: dict) -> bool:
    """The payload carries nothing this module recognises. Fail closed: an unrecognised shape
    means the contract moved, and reporting it as clean would be the harness going quietly
    vacuous — exactly the failure this suite keeps finding elsewhere."""
    return not _claims(payload) and "identity" not in payload and "authority" not in payload


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

def rule_contact_egress(p: dict) -> str | None:
    """A5: never return phone, email or address for a service_provider."""
    if (p.get("subject") or {}).get("kind") != "service_provider":
        return None
    hit = [k for k in _CONTACT_KEYS if str(p.get(k) or "").strip()]
    return f"service_provider payload carries contact field(s) {hit}" if hit else None


def rule_no_attester_roster(p: dict) -> str | None:
    """C0: provenance is a count plus a community, never a list of who said it."""
    for c in _claims(p):
        if c.get("attesters"):
            return f"claim {c.get('predicate')!r} lists attesters by name"
    return None


def rule_reidentification(p: dict) -> str | None:
    """eval-review: the community NAME is suppressed below n_shared_community = 2.

    The count carries the signal; the name at n=1 carries the identity, because a viewer can
    often enumerate a small shared community."""
    for c in _claims(p):
        n = c.get("n_shared_community")
        if isinstance(n, int) and n == 1 and str(c.get("shared_community") or "").strip():
            return (f"claim {c.get('predicate')!r} names {c['shared_community']!r} at "
                    f"n_shared_community=1 — identifies the attester by implication")
    return None


def rule_display_floor(p: dict) -> str | None:
    """A4.1 / A5: distinct certain live attesters, undecayed, at or above the floor.

    Speculative and retracted rows are subtracted first — both are ways a sub-floor claim gets
    counted up to the floor without anyone new saying anything."""
    kind = str((p.get("subject") or {}).get("kind") or "")
    floor = _FLOOR_BY_KIND.get(kind, _FLOOR_DEFAULT)
    for c in _claims(p):
        n = c.get("n_attesters")
        if not isinstance(n, int):
            continue
        effective = n - int(c.get("n_speculative") or 0) - int(c.get("n_retracted") or 0)
        if effective < floor:
            bits = []
            if c.get("n_speculative"):
                bits.append(f"{c['n_speculative']} speculative")
            if c.get("n_retracted"):
                bits.append(f"{c['n_retracted']} retracted")
            note = f" ({n} raw, less {' and '.join(bits)})" if bits else ""
            return (f"claim {c.get('predicate')!r} surfaced at {effective} attesters{note}, "
                    f"below the {kind or 'default'} floor of {floor}")
    return None


def rule_no_average(p: dict) -> str | None:
    """D9-A1: no averaged rating anywhere in the payload, at any depth."""
    def walk(node: Any, path: str = "") -> str | None:
        if isinstance(node, dict):
            for k, v in node.items():
                if _AVERAGE_KEY.search(str(k)):
                    return f"{path}{k}"
                got = walk(v, f"{path}{k}.")
                if got:
                    return got
        elif isinstance(node, list):
            for i, v in enumerate(node):
                got = walk(v, f"{path}[{i}].")
                if got:
                    return got
        return None

    hit = walk(p)
    return f"payload carries an averaged rating at `{hit}` — there is no avg() in this system" if hit else None


def rule_authority_number_rendered(p: dict) -> str | None:
    """authority spec §7: never render the number, render the evidence."""
    a = p.get("authority") or {}
    rendered = str(a.get("rendered") or "")
    if not rendered:
        return None
    if re.search(r"\b0?\.\d+\b|\bauthority\b|\bscore\b", rendered, re.IGNORECASE):
        return f"rendered authority text exposes the score: {rendered!r}"
    return None


def rule_reason_is_not_a_quote(p: dict) -> str | None:
    """eval-review: the reason must not carry another user's raw self-description.

    P3 builds it from `evidence_quote` — the attester's own words about themselves — and renders
    it to a stranger. The rapport eval found real PII riding in claim fields, so this needs the
    same redaction the claims write path uses."""
    a = p.get("authority") or {}
    reason = str(a.get("reason") or "")
    if not reason:
        return None
    if len(reason) > _REASON_MAX and _FIRST_PERSON.search(reason):
        return (f"authority reason is a {len(reason)}-char first-person quote, not an "
                f"attribution: {reason[:60]!r}…")
    return None


def rule_provenance_present(p: dict) -> str | None:
    """D9-A10: when the viewer shares a community with any attester, say so."""
    if not p.get("viewer_shares_community"):
        return None
    for c in _claims(p):
        if c.get("n_shared_community") is None:
            return (f"viewer shares a community but claim {c.get('predicate')!r} omits "
                    f"n_shared_community")
    return None


def rule_truncation_honesty(p: dict) -> str | None:
    """D9-A9: a candidate set that hit its limit cannot support an exhaustiveness claim."""
    meta = p.get("result_meta") or {}
    returned, limit = meta.get("returned"), meta.get("limit")
    if not isinstance(returned, int) or not isinstance(limit, int):
        return None
    if returned >= limit and not meta.get("truncated"):
        claim = str(meta.get("claim") or "")
        tail = f" while claiming {claim!r}" if claim else ""
        return (f"result set returned {returned} of a {limit} limit and is not flagged "
                f"truncated{tail}")
    return None


def rule_admission_rule_recorded(p: dict) -> str | None:
    """A7: the retrieval path records which admission rule produced the set."""
    meta = p.get("result_meta") or {}
    if meta and not str(meta.get("admission_rule") or "").strip():
        return "result_meta present but admission_rule is not recorded"
    return None


def rule_contested_needs_floor(p: dict) -> str | None:
    """A sub-floor claim must not leak through a derived field like is_contested."""
    kind = str((p.get("subject") or {}).get("kind") or "")
    floor = _FLOOR_BY_KIND.get(kind, _FLOOR_DEFAULT)
    for c in _claims(p):
        n = c.get("n_attesters")
        if c.get("is_contested") and isinstance(n, int) and n < floor:
            return (f"claim {c.get('predicate')!r} is below the floor but carries "
                    f"is_contested — the flag confirms the claim exists")
    return None


def rule_phone_hash_is_keyed(p: dict) -> str | None:
    """eval-review: a bare digest over ~10^10 phone numbers is reversible."""
    ident = p.get("identity") or {}
    algo = str(ident.get("phone_hash_algo") or "")
    if not algo:
        return None
    if "hmac" not in algo.lower() or not str(ident.get("phone_hmac_key_id") or "").strip():
        return (f"phone stored with {algo!r} and key_id={ident.get('phone_hmac_key_id')!r} — "
                f"a bare digest of a phone number is brute-forceable")
    return None


RULES = (
    ("contact_egress", rule_contact_egress),
    ("attester_roster", rule_no_attester_roster),
    ("reidentification", rule_reidentification),
    ("display_floor", rule_display_floor),
    ("no_average", rule_no_average),
    ("authority_number", rule_authority_number_rendered),
    ("reason_is_quote", rule_reason_is_not_a_quote),
    ("provenance_present", rule_provenance_present),
    ("truncation_honesty", rule_truncation_honesty),
    ("admission_rule", rule_admission_rule_recorded),
    ("contested_floor", rule_contested_needs_floor),
    ("phone_keyed", rule_phone_hash_is_keyed),
)


def scan(payload: dict) -> tuple[list[str], list[str]]:
    """(rule names that fired, their detail lines)."""
    fired, detail = [], []
    for name, fn in RULES:
        got = fn(payload)
        if got:
            fired.append(name)
            detail.append(f"{name}: {got}")
    return fired, detail


def evaluate(case: dict) -> SafetyResult:
    payload = case.get("payload") or {}
    cid, cat = str(case.get("id")), str(case.get("category") or "")
    if _missing_shape(payload):
        return SafetyResult(cid, cat, violated=False, rules_fired=[], unscorable=True,
                            detail="payload carries no recognisable claims/identity/authority — "
                                   "the shape may have moved; not scored as clean")
    fired, detail = scan(payload)
    return SafetyResult(cid, cat, violated=bool(fired), rules_fired=fired,
                        detail="; ".join(detail))
