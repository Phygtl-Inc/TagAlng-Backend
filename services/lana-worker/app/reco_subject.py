"""Grounding a recommendation to its SUBJECT — the thing recommended, apart from the
recommending (docs/LANA_RECO_SUBJECT_MERGE.md, Stage 1).

Three neighbours recommending Dr. Sarah write three rows today, and an ask returns three
people. Stamping each of those rows with the same `subject_ref` is what will later let one
card say "3 vouched" instead. Nothing reads the column yet; this only fills it.

WHICH TYPES GROUND, AND WHY NOT THE REST. The line is not "can Google find it" — that is
only the cheapest mechanism. It is whether a type's captured fields are OBSERVATIONS ABOUT
a shared referent or the ARTIFACT ITSELF:

    professional   gentle · walk-in · takes insurance     three witnesses to one dentist
    recipe         ingredients · steps · 45 min · easy    this IS the recipe

Two banana-bread recommendations are two different recipes; merging them would discard one
author's ingredients and then claim both vouched for the survivor. So `recipe`, `diy` and
`other` never ground, permanently. `product` merges honestly but a SKU is not a map point,
so it waits for Stage 2's identity space rather than being forced through this one.

TWO PATHS, AND THE FIRST IS NEARLY FREE.

  A. She TAPPED a place. The subject step already renders a Places picker and we already
     call Google to fill it, so the id is in hand before she answers — it was simply being
     thrown away by a name-only field mask. An exact id needs no threshold, no fuzzy match
     and cannot be wrong.

  B. She TYPED a name. Search, and accept only above a floor. Re-deriving by search what
     path A had exactly is strictly worse, which is why it is the fallback and not the
     design. A wrong merge is worse than no merge: it invents corroboration, and the vouch
     count is the one number a stranger is meant to trust (20261107120000).

Never raises. A tip that posted must never fail on its subject.
"""

from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher
from typing import Any

logger = logging.getLogger(__name__)

# Types whose fields are observations about a shared referent AND whose subject is a point
# on the map. Mirrors reco_question_sets._PLACE_SUBJECT_TYPES / _PLACE_CAPABLE_TYPES: a
# restaurant or a location always is one, a professional or a service may be (a barber shop
# is, a plumber is not — the extractor's `place_based` read decides, and an ungrounded one
# simply stays null for Stage 2).
GROUNDABLE_TYPES = frozenset({"professional", "restaurant", "location", "service"})

# Never merge at all, whatever the model says. `recipe`/`diy` fields ARE the artifact;
# `other` is the escape hatch bucket ("a bus route, a Facebook group, a broker") with no
# shared shape. This set is the one thing in this module that is a product decision rather
# than a tuning knob — see the migration headers before touching it.
NEVER_GROUND_TYPES = frozenset({"recipe", "diy", "other"})

# Everything that merges. The place types plus `product`: a SKU ("Cosori gooseneck") is a
# shared referent two neighbours can independently point at, it simply is not a map point,
# so it never takes the place path and goes straight to the identity space below.
MERGEABLE_TYPES = GROUNDABLE_TYPES | frozenset({"product"})

# How close a TYPED name must be to a Google result before we call them the same place.
# Deliberately high. Below it we store nothing and the backfill can try again later with
# better normalization; above it we are asserting two neighbours meant one business.
MATCH_FLOOR = 0.82

# The identity space (Stage 2), where there is no place id and every answer is a judgement.
# Three bands, because "how sure are we" has three useful answers and not two:
#   >= AUTO        near-identical names in one locality. Merge without asking a model.
#   >= ADJUDICATE  close enough to be worth ONE model call on a shortlist.
#   below          not a candidate. Its own subject, merged with nothing.
AUTO_MERGE_FLOOR = 0.93
ADJUDICATE_FLOOR = 0.80

# A disagreeing category cannot by itself refuse a merge — "dentist" and "pediatric
# dentist" are the same person described twice — but it must stop a name alone from
# auto-merging. Capping just under AUTO_MERGE_FLOOR sends those to the model instead.
_CATEGORY_MISMATCH_CAP = 0.92

# Honorifics and legal suffixes carry no identity: "Dr. Sarah Chen" and "Sarah Chen, DDS"
# are one person, and leaving these in makes an exact match look like a 0.6.
_TITLES = r"^(dr|doctor|mr|mrs|ms|miss|prof|professor)\.?\s+"
_SUFFIXES = (
    r"[,\s]+(dds|dmd|md|do|phd|rn|np|pa|esq|cpa|llc|l\.l\.c|inc|incorporated|"
    r"corp|co|ltd|pllc|pc|pa)\.?$"
)


def normalize_subject_name(raw: Any) -> str:
    """The identity string two recommendations are compared on.

    Strips honorifics, legal suffixes and punctuation, lowercases, collapses whitespace.
    Strictly more aggressive than local_signals.reco_subject_key(), which is case and
    whitespace only and so keeps "Dr Sarah" and "Dr. Sarah" apart — that key stays as it
    is because the agree-row tallies are built on it and loosening it underneath them would
    silently re-bucket existing counts.
    """
    name = " ".join(str(raw or "").split()).lower()
    if not name:
        return ""
    name = re.sub(_TITLES, "", name)
    # Repeat: "Sarah Chen, DDS, PA" carries two.
    for _ in range(3):
        stripped = re.sub(_SUFFIXES, "", name)
        if stripped == name:
            break
        name = stripped
    # Punctuation to spaces rather than deleted: "kids'clinic" must not become one word
    # while "Kids' Clinic" becomes two.
    name = re.sub(r"[^\w\s]", " ", name)
    return " ".join(name.split())[:120]


def name_match_score(typed: Any, candidate: Any) -> float:
    """0-1 on whether two names denote the same thing. 0 when either is missing.

    Containment scores high on purpose: a user types "Dr. Sarah" and Google answers
    "Dr. Sarah Chen, DDS" — the same dentist, but a raw sequence ratio reads ~0.55 and
    would throw the match away. Requires 4+ characters so "CF" is not held to be contained
    in every string that happens to have those letters.
    """
    a, b = normalize_subject_name(typed), normalize_subject_name(candidate)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) >= 4 and shorter in longer:
        return 0.9
    return SequenceMatcher(None, a, b).ratio()


def place_options_map(options: Any) -> dict[str, dict[str, Any]]:
    """{normalized label: {place_id, lat, lng}} from nearby_place_options() rows.

    Keyed on the NORMALIZED label so a user who retypes what she saw ("dr sarah chen"
    for "Dr. Sarah Chen") still lands on path A instead of falling through to a search.
    """
    out: dict[str, dict[str, Any]] = {}
    for opt in options if isinstance(options, list) else []:
        if not isinstance(opt, dict):
            continue
        key = normalize_subject_name(opt.get("name"))
        pid = str(opt.get("place_id") or "").strip()
        if key and pid and key not in out:
            out[key] = {"place_id": pid, "lat": opt.get("lat"), "lng": opt.get("lng")}
    return out


def _picked(draft: dict[str, Any]) -> dict[str, Any] | None:
    """Path A, strongest form — the carousel's map search, where the user tapped an actual
    Google result and the client sent its id back on the setup call.

    This outranks everything else in the module: it is not a match, it is the place itself.
    lat/lng are absent here (the pick travels as a bare id) and are filled in from the
    place row at upsert time, or left null — a coordinate is worth less than a certain id.
    """
    pid = str((draft or {}).get("subject_google_place_id") or "").strip()
    return {"place_id": pid, "lat": None, "lng": None} if pid else None


def _tapped(name: Any, options: Any) -> dict[str, Any] | None:
    """Path A, chat fork — the answer is one of the place chips we offered, matched back to
    the id by its label, since the chat fork posts an answer as text rather than as a pick."""
    key = normalize_subject_name(name)
    return place_options_map(options).get(key) if key else None


def _searched(
    name: Any, *, category: Any, locality: Any,
    zip_code: str | None, block_id: str | None, user_id: str | None,
) -> dict[str, Any] | None:
    """Path B — she typed a name we never offered. Search, and accept only above the floor.

    The query carries the locality when there is one, and the search is centroid-biased
    regardless (_places_search_text refuses to run unbiased at all), so "Lake Nona Smiles"
    cannot resolve to a same-named practice three states away.
    """
    from app.places import search_places

    query = " ".join(
        str(p).strip() for p in (name, category, locality) if str(p or "").strip()
    )
    try:
        results = search_places(
            query=query, zip_code=zip_code, block_id=block_id, user_id=user_id, limit=5,
        )
    except Exception:  # noqa: BLE001 — grounding is never worth failing a post over
        logger.info("reco_subject.search_failed query=%r", query)
        return None

    best, best_score = None, 0.0
    for r in results or []:
        score = name_match_score(name, r.get("name"))
        if score > best_score:
            best, best_score = r, score
    if not best or best_score < MATCH_FLOOR or not str(best.get("place_id") or "").strip():
        logger.info(
            "reco_subject.below_floor name=%r best=%r score=%.2f",
            str(name)[:60], str((best or {}).get("name"))[:60], best_score,
        )
        return None
    return {
        "place_id": str(best["place_id"]).strip(),
        "lat": best.get("lat"),
        "lng": best.get("lng"),
    }


_ADJUDICATE_SYSTEM = (
    "You decide whether two neighbourhood recommendations name the SAME real-world thing.\n"
    "You are given a pair: what one neighbour called it, and what another already called "
    "it, each with a category and a neighbourhood where they gave one.\n"
    "Answer true only if a local would say these are one and the same provider, business "
    "or product. Answer false if they are two different ones. Answer null if you genuinely "
    "cannot tell from what is here.\n"
    "Same thing described differently is TRUE: 'Dr Sarah' / 'Dr. Sarah Chen, DDS'; "
    "'dentist' / 'pediatric dentist'; an abbreviation of the same business.\n"
    "Different things that merely sound alike is FALSE: two plumbers both called Mike, a "
    "chain's two branches, a parent company and one of its shops.\n"
    "Never guess to be helpful. null is a real answer and costs nothing; a wrong true "
    "merges two different businesses into one and invents agreement that does not exist.\n"
    'Reply with JSON only: {"same": true|false|null}'
)


def _category_agrees(a: Any, b: Any) -> bool:
    """Loose: one category containing the other counts ("dentist" / "pediatric dentist").
    Only a real disagreement should hold a name match back."""
    x, y = normalize_subject_name(a), normalize_subject_name(b)
    if not x or not y:
        return True  # nothing said is not a disagreement
    return x == y or x in y or y in x


def score_candidate(
    name: Any, category: Any, candidate: dict[str, Any]
) -> float:
    """How alike an incoming subject and an existing one are, on IDENTITY alone.

    The name carries the decision; the category can only hold it back. A category that
    disagrees caps the score below the auto-merge floor so the pair goes to the model
    rather than merging on a name collision ("Mike the plumber" vs "Mike the barber").
    """
    score = name_match_score(name, candidate.get("display_name") or candidate.get("subject_key"))
    if not _category_agrees(category, candidate.get("category")):
        score = min(score, _CATEGORY_MISMATCH_CAP)
    return score


def _candidates(
    user_jwt: str, *, key: str, category: str | None, locality: str | None, limit: int = 10
) -> list[dict[str, Any]]:
    from app.supabase_rpc import call_rpc

    try:
        raw = call_rpc(
            user_jwt,
            "reco_subject_candidates",
            {
                "p_subject_key": key,
                "p_category": category,
                "p_locality": locality,
                "p_limit": int(limit),
            },
        )
    except Exception:  # noqa: BLE001
        logger.info("reco_subject.candidates_unavailable")
        return []
    return [r for r in (raw or []) if isinstance(r, dict) and r.get("id")]


def _adjudicate(
    name: Any, category: Any, locality: Any, candidate: dict[str, Any]
) -> bool | None:
    """One model call on ONE pair. True / False / None, where None means "cannot tell".

    Runs only inside the ambiguous band, so it never sees the corpus and never sees the
    pairs that are already obvious in either direction. No canned fallback: when the model
    is unconfigured or the call fails, the answer is None and the pair stays unmerged,
    which is the same outcome as the model saying it cannot tell.
    """
    try:
        import json

        from app.orchestrator.llm import composer_model, llm_configured, llm_json

        if not llm_configured():
            return None
        payload = {
            "one": {
                "name": str(name or ""),
                "category": str(category or "") or None,
                "neighbourhood": str(locality or "") or None,
            },
            "other": {
                "name": str(candidate.get("display_name") or ""),
                "category": candidate.get("category"),
                "neighbourhood": candidate.get("locality"),
            },
        }
        data = llm_json(
            model=composer_model(),
            system=_ADJUDICATE_SYSTEM,
            user_payload=json.dumps(payload, ensure_ascii=False),
            max_tokens=40,
            # Deterministic: the same pair must not merge on Tuesday and split on Wednesday.
            temperature=0.0,
        )
    except Exception:  # noqa: BLE001
        logger.info("reco_subject.adjudicate_failed")
        return None
    same = (data or {}).get("same") if isinstance(data, dict) else None
    return same if isinstance(same, bool) else None


def _create_subject(
    user_jwt: str,
    *,
    signal_id: str,
    name: str,
    category: str | None,
    locality: str | None,
    found: dict[str, Any] | None,
) -> str | None:
    """This tip's own subject row. `found` set = grounded to a place (method 'google');
    `found` None = the second identity space found nothing to join (method 'new').

    The 'new' case is not a dead end — it is what gives the NEXT neighbour to name the same
    thing something to find, which is how an ungrounded subject ever accumulates voices.
    """
    from app.supabase_rpc import call_rpc

    try:
        subject_id = call_rpc(
            user_jwt,
            "set_signal_subject",
            {
                "p_signal_id": signal_id,
                "p_subject_key": normalize_subject_name(name),
                # Author casing, not the normalized key — the card titles itself with this.
                "p_display_name": name,
                "p_google_place_id": (found or {}).get("place_id"),
                "p_category": category,
                "p_locality": locality,
                "p_lat": (found or {}).get("lat"),
                "p_lng": (found or {}).get("lng"),
            },
        )
    except Exception:  # noqa: BLE001
        logger.warning("set_signal_subject_failed signal_id=%s", signal_id)
        return None
    return str(subject_id).strip() or None if subject_id else None


def resolve_identity_subject(
    user_jwt: str,
    *,
    signal_id: str,
    name: str,
    category: str | None,
    locality: str | None,
) -> str | None:
    """Stage 2: attach this tip to an existing NON-PLACE subject, or leave it to make its own.

    Returns the subject id when it attached to one, else None — and None here does not mean
    "nothing happened": an ambiguous near-miss is recorded on the signal so it can be
    settled later without re-running the search and the model call that found it.
    """
    from app.supabase_rpc import call_rpc

    key = normalize_subject_name(name)
    if not key:
        return None
    rows = _candidates(user_jwt, key=key, category=category, locality=locality)
    if not rows:
        return None

    scored = sorted(
        ((score_candidate(name, category, r), r) for r in rows),
        key=lambda pair: pair[0],
        reverse=True,
    )
    best_score, best = scored[0]
    if best_score < ADJUDICATE_FLOOR:
        return None

    if best_score >= AUTO_MERGE_FLOOR:
        method, confidence = "blocked", best_score
    else:
        verdict = _adjudicate(name, category, locality, best)
        if verdict is True:
            method, confidence = "adjudicated", best_score
        else:
            # False and None part company here only in what they MEAN, not in what they do:
            # neither merges. Both are recorded as the near-miss they were, because a
            # "different Mike" today is exactly the pair a human reviewer wants to see.
            try:
                call_rpc(
                    user_jwt,
                    "mark_signal_subject_ambiguous",
                    {
                        "p_signal_id": signal_id,
                        "p_candidate": best["id"],
                        "p_confidence": float(best_score),
                    },
                )
            except Exception:  # noqa: BLE001
                logger.info("reco_subject.mark_ambiguous_failed signal=%s", signal_id)
            return None

    try:
        call_rpc(
            user_jwt,
            "attach_signal_subject",
            {
                "p_signal_id": signal_id,
                "p_subject_id": best["id"],
                "p_method": method,
                "p_confidence": float(confidence),
            },
        )
    except Exception:  # noqa: BLE001
        logger.warning("attach_signal_subject_failed signal_id=%s", signal_id)
        return None
    return str(best["id"])


def ground_reco_subject(
    user_jwt: str,
    *,
    signal_id: str,
    draft: dict[str, Any],
    zip_code: str | None = None,
    block_id: str | None = None,
    user_id: str | None = None,
) -> str | None:
    """Stamp `local_signals.subject_ref`. Returns the subject id, or None when the tip
    stays ungrounded — which is an ordinary outcome, not an error.

    Called after the insert beside tag_local_signal, for the reason stated there: threading
    a column through save_local_signal's 150 lines of dedupe/match/notify is how a
    behaviour goes missing in a copy-paste. It is also the only place the draft, and so the
    place the user tapped, is still in scope.
    """
    from app.reco_question_sets import normalize_type

    name = str((draft or {}).get("name") or "").strip()
    if not signal_id or not name:
        return None

    rtype = normalize_type((draft or {}).get("reco_type")) or "other"
    if rtype in NEVER_GROUND_TYPES or rtype not in MERGEABLE_TYPES:
        # Not a failure and not worth a warning: a recipe having no subject row is the
        # design, because its fields ARE the recipe.
        return None

    category = str((draft or {}).get("category") or "").strip() or None
    locality = str((draft or {}).get("locality") or "").strip() or None

    # ── The place identity space. A google_place_id is a FACT: exact, no threshold. ──
    # Skipped entirely for `product`, whose subject is a SKU — searching Places for a
    # kettle grounds it to whichever shop stocks it.
    # Strongest first, and they are genuinely ranked, not merely ordered:
    #   1. the carousel pick   an id the user chose off a map — certain
    #   2. the chat-fork chip  an id we offered and she answered with — certain
    #   3. the search          our guess at what she typed — a judgement, floored at 0.82
    found = None
    if rtype in GROUNDABLE_TYPES:
        found = (
            _picked(draft)
            or _tapped(name, (draft or {}).get("subject_place_options"))
            or _searched(
                name, category=category, locality=locality,
                zip_code=zip_code, block_id=block_id, user_id=user_id,
            )
        )

    # ── The second identity space. Everything here is a JUDGEMENT. ──
    # Reached by a product always, and by a professional/service/place that Google could
    # not settle — a plumber, a nanny, "Chef Ana meal prep" have no storefront to find.
    if not found:
        attached = resolve_identity_subject(
            user_jwt, signal_id=signal_id, name=name, category=category, locality=locality,
        )
        if attached:
            return attached
        # Nothing to attach to: fall through and let this tip create its own subject, so
        # the NEXT neighbour to name the same thing has something to find.
        return _create_subject(
            user_jwt, signal_id=signal_id, name=name,
            category=category, locality=locality, found=None,
        )

    return _create_subject(
        user_jwt, signal_id=signal_id, name=name,
        category=category, locality=locality, found=found,
    )
