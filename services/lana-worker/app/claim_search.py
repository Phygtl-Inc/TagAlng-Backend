"""Structured claim filters for discovery.find_by_attrs (AND across dimensions)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.layer1_intents import attr_filter_tokens, normalize_attr_filter_text

# bucket → canonical terms + synonyms for query parsing
_HERITAGE: dict[str, list[str]] = {
    "american": ["american", "america", "usa"],
    "british": ["british", "britain", "uk", "english"],
    "canadian": ["canadian", "canada"],
    "pakistani": ["pakistani", "pakistan"],
    "brazilian": ["brazilian", "brazil", "latina", "latino"],
    "italian": ["italian", "italy"],
    "mexican": ["mexican", "mexico"],
    "indian": ["indian", "india"],
    "chinese": ["chinese", "china"],
    "korean": ["korean", "korea"],
    "colombian": ["colombian", "colombia"],
}

_STAGE: dict[str, list[str]] = {
    "mom": ["mom", "mother", "mama", "mum"],
    "dad": ["dad", "father", "papa"],
    "parent": ["parent", "parents"],
    "toddler": ["toddler", "toddlers"],
    "baby": ["baby", "infant", "newborn"],
    "preschool": ["preschool", "prek", "pre-k"],
}

_LANGUAGE: dict[str, list[str]] = {
    "portuguese": ["portuguese", "portugal"],
    "spanish": ["spanish", "espanol", "español"],
    "english": ["english"],
    "french": ["french", "france"],
    "hindi": ["hindi"],
    "urdu": ["urdu"],
}

_HERITAGE_FLAT: dict[str, str] = {}
for key, syns in _HERITAGE.items():
    for s in syns:
        _HERITAGE_FLAT[s] = key

_STAGE_FLAT: dict[str, str] = {}
for key, syns in _STAGE.items():
    for s in syns:
        _STAGE_FLAT[s] = key

_LANGUAGE_FLAT: dict[str, str] = {}
for key, syns in _LANGUAGE.items():
    for s in syns:
        _LANGUAGE_FLAT[s] = key


@dataclass
class ClaimFilter:
    bucket: str | None
    terms: list[str] = field(default_factory=list)

    def to_rpc(self) -> dict[str, Any]:
        return {
            "bucket": self.bucket,
            "terms": list(dict.fromkeys(t.lower() for t in self.terms if t)),
        }


def _filters_from_ai_terms(slots: dict[str, Any] | None) -> list[ClaimFilter]:
    """Classifier-authored requirement groups: AND across groups, OR within (word forms)."""
    raw = (slots or {}).get("attr_terms")
    if not isinstance(raw, list):
        return []
    filters: list[ClaimFilter] = []
    for group in raw[:4]:
        if not isinstance(group, list):
            continue
        terms = [
            str(t).strip().lower()
            for t in group[:6]
            if isinstance(t, str) and 2 <= len(str(t).strip()) <= 40
        ]
        if terms:
            filters.append(ClaimFilter(bucket=None, terms=terms))
    return filters


def attr_display_filter(filter_text: str, slots: dict[str, Any] | None = None) -> str:
    """What Lana echoes back: the substantive traits, not the raw sentence — 'No one
    lists "people in my block that likes paragliding"' reads broken; 'paragliding' doesn't."""
    raw = (slots or {}).get("attr_terms")
    heads: list[str] = []
    if isinstance(raw, list):
        for group in raw[:4]:
            if isinstance(group, list) and group:
                head = str(group[0]).strip()
                if head:
                    heads.append(head)
    return " + ".join(heads) if heads else str(filter_text or "").strip()


def parse_claim_filters(query: str, slots: dict[str, Any] | None = None) -> list[ClaimFilter]:
    """Turn natural language into bucket-scoped AND filters."""
    # The AI already decided what's substantive vs filler — its groups win outright.
    ai_filters = _filters_from_ai_terms(slots)
    if ai_filters:
        return ai_filters
    raw = normalize_attr_filter_text(query, slots)
    tokens = attr_filter_tokens(raw) or re.findall(r"[a-z0-9]+", raw.lower())

    heritage_terms: list[str] = []
    stage_terms: list[str] = []
    language_terms: list[str] = []
    general_terms: list[str] = []

    for tok in tokens:
        if tok in _HERITAGE_FLAT:
            key = _HERITAGE_FLAT[tok]
            heritage_terms.extend(_HERITAGE[key])
        elif tok in _STAGE_FLAT:
            key = _STAGE_FLAT[tok]
            stage_terms.extend(_STAGE[key])
        elif tok in _LANGUAGE_FLAT:
            key = _LANGUAGE_FLAT[tok]
            language_terms.extend(_LANGUAGE[key])
        elif len(tok) >= 3:
            general_terms.append(tok)

    filters: list[ClaimFilter] = []
    if heritage_terms:
        filters.append(ClaimFilter(bucket="heritage", terms=heritage_terms))
    if stage_terms:
        filters.append(ClaimFilter(bucket="stage", terms=stage_terms))
    if language_terms:
        # language often stored as interest/general — search any bucket
        filters.append(ClaimFilter(bucket=None, terms=language_terms))
    if general_terms and not filters:
        # One filter per token (AND) — a single OR'd group let "likes to swim" match any
        # "Likes X" claim. Singular/plural forms of a word stay together as one requirement.
        groups: list[list[str]] = []
        for tok in general_terms:
            if groups and (tok == groups[-1][0] + "s" or groups[-1][0] == tok + "s"):
                groups[-1].append(tok)
            else:
                groups.append([tok])
        filters.extend(ClaimFilter(bucket=None, terms=g) for g in groups)

    return filters


def filters_to_rpc_payload(filters: list[ClaimFilter]) -> list[dict[str, Any]]:
    return [f.to_rpc() for f in filters if f.terms]


def heritage_terms_in_text(text: str) -> set[str]:
    low = str(text or "").lower()
    found: set[str] = set()
    for key, syns in _HERITAGE.items():
        if any(re.search(rf"\b{re.escape(s)}s?\b", low) for s in syns):
            found.add(key)
    return found


def peer_heritage_key(peer: dict[str, Any]) -> str | None:
    blob = " ".join(
        str(peer.get(k) or "")
        for k in ("matching_peer_label", "matching_peer_concept")
    ).lower()
    for key, syns in _HERITAGE.items():
        if any(s in blob for s in syns):
            return key
    return None


def peer_matches_identity_snippet(peer: dict[str, Any], identity_snippet: str | None) -> bool:
    """True when intro nudge is semantically fair (heritage must align if both present)."""
    snippet = str(identity_snippet or "").strip()
    if not snippet:
        return True
    user_heritage = heritage_terms_in_text(snippet)
    peer_h = peer_heritage_key(peer)
    if user_heritage and peer_h and peer_h not in user_heritage:
        return False
    label = str(peer.get("matching_peer_label") or "").lower()
    if not label:
        return True
    # Require at least one substantive token overlap for explicit identity queries
    snippet_tokens = set(attr_filter_tokens(snippet))
    label_tokens = set(re.findall(r"[a-z0-9]+", label))
    if snippet_tokens and not (snippet_tokens & label_tokens):
        # Embedding-matched peers surfaced against this exact ask — semantically fair
        # even with zero lexical overlap ("gymmer" vs "Gym enthusiast").
        if peer.get("semantic_match"):
            return True
        # stage/heritage synonym overlap counts
        for t in snippet_tokens:
            if t in _HERITAGE_FLAT and peer_h == _HERITAGE_FLAT[t]:
                return True
            if t in _STAGE_FLAT and any(s in label for s in _STAGE[_STAGE_FLAT[t]]):
                return True
        # No lexical, heritage, or life-stage overlap between what the user said and
        # why this peer surfaced. Refuse the match rather than auto-claim a shared
        # trait the user never mentioned (e.g. a bare name reply "tony" vs a peer
        # labelled "married 10 years"). Explicit intro requests bypass this gate.
        return False
    return True
