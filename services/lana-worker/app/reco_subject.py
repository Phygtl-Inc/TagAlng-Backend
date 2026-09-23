"""Grounding a recommendation to its SUBJECT — the thing recommended, apart from the
recommending (docs/LANA_RECO_SUBJECT_MERGE.md, Stage 1).

Three neighbours recommending Dr. Sarah write three rows today, and an ask returns three
people. Stamping each of those rows with the same `subject_ref` is what will later let one
card say "3 vouched" instead. Nothing reads the column yet; this only fills it.

EVERY TYPE SHARES A SUBJECT. What differs is how its contributions may be COMBINED, and
that turns on whether a type's captured fields are OBSERVATIONS ABOUT a shared referent or
the ARTIFACT ITSELF:

    professional   gentle · walk-in · takes insurance     three witnesses to one dentist
    recipe         ingredients · steps · 45 min · easy    this IS the recipe

So a subject carries a `merge_mode`. "aggregate" may be summarised — the Pareto majority
of neighbours saying much the same thing, with outliers surfaced separately rather than
blended into a sentence nobody wrote ("great doctor · huge parking lot"). "collection"
may not: three banana breads share the subject "banana bread" and a count, and are shown
side by side, because merging their steps discards one author's work and credits the rest
to everyone. A recipe is always on the minority side, so it is always standalone.

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

# Types whose contributions ARE the artifact, one per author: a recipe, a DIY method, and
# the `other` grab-bag ("a bus route, a Facebook group, a broker"). They share a SUBJECT
# and a count, and are rendered side by side — never blended into each other, because
# merging two banana breads discards one author's ingredients and attributes the remainder
# to both. The Pareto framing from standup 2026-09-22: a recipe is always on the minority
# side, so it is always standalone.
COLLECTION_TYPES = frozenset({"recipe", "diy", "other"})

# Everything merges now. What differs is HOW contributions may be combined (merge_mode),
# not whether they share a subject. `product` is here and not in GROUNDABLE_TYPES because
# a SKU ("Cosori gooseneck") is a shared referent that is not a map point.
MERGEABLE_TYPES = GROUNDABLE_TYPES | frozenset({"product"}) | COLLECTION_TYPES


def merge_mode_for(reco_type: Any) -> str:
    """'collection' when the contributions ARE the thing, else 'aggregate'.

    Settled from the TYPE, so a subject cannot be observations for one neighbour and
    artifacts for the next. Stage 3 reads this to decide whether a card may summarise its
    contributions at all — the read path must never blend a collection.
    """
    from app.reco_question_sets import normalize_type

    rtype = normalize_type(reco_type) or "other"
    return "collection" if rtype in COLLECTION_TYPES else "aggregate"

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
    user_jwt: str, *, key: str, category: str | None, locality: str | None,
    mode: str = "aggregate", limit: int = 10,
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
                # A recipe must never be a candidate for a plumber that shares a word:
                # what may be combined is part of what the subject IS.
                "p_merge_mode": mode,
            },
        )
    except Exception:  # noqa: BLE001
        logger.info("reco_subject.candidates_unavailable")
        return []
    return [r for r in (raw or []) if isinstance(r, dict) and r.get("id")]


_VERDICT_TO_BOOL = {"same": True, "different": False, "unsure": None}
_BOOL_TO_VERDICT = {True: "same", False: "different", None: "unsure"}


def pair_signature(a_key: str, a_cat: Any, b_key: str, b_cat: Any) -> str:
    """Fingerprint of a comparison, order-independent.

    "Is A the same as B" and "is B the same as A" are one question, so the sides are
    sorted before hashing — two entries free to disagree would reintroduce exactly the
    instability the cache exists to remove. Category is part of the key because it is part
    of the question (20261223120000).
    """
    import hashlib

    sides = sorted([
        f"{normalize_subject_name(a_key)}|{normalize_subject_name(a_cat)}",
        f"{normalize_subject_name(b_key)}|{normalize_subject_name(b_cat)}",
    ])
    return hashlib.sha256("||".join(sides).encode()).hexdigest()[:40]


def _cached_verdict(sig: str) -> tuple[bool, bool | None]:
    """(hit, verdict). A miss and a broken cache are the same thing: ask the model."""
    try:
        from app.db import service_client

        rows = (service_client().table("reco_adjudications").select("verdict")
                .eq("pair_sig", sig).limit(1).execute().data or [])
    except Exception:  # noqa: BLE001
        return False, None
    if not rows:
        return False, None
    return True, _VERDICT_TO_BOOL.get(str(rows[0].get("verdict") or ""), None)


def _store_verdict(sig: str, verdict: bool | None, a: tuple[str, Any], b: tuple[str, Any]) -> None:
    try:
        from app.db import service_client

        service_client().table("reco_adjudications").upsert({
            "pair_sig": sig, "verdict": _BOOL_TO_VERDICT[verdict],
            "a_key": normalize_subject_name(a[0]), "a_category": a[1],
            "b_key": normalize_subject_name(b[0]), "b_category": b[1],
        }, on_conflict="pair_sig").execute()
    except Exception:  # noqa: BLE001 — failing to cache must not fail the turn
        logger.info("reco_subject.store_verdict_failed sig=%s", sig)


def _adjudicate(
    name: Any, category: Any, locality: Any, candidate: dict[str, Any]
) -> bool | None:
    """One model call on ONE pair. True / False / None, where None means "cannot tell".

    Runs only inside the ambiguous band, so it never sees the corpus and never sees the
    pairs that are already obvious in either direction. No canned fallback: when the model
    is unconfigured or the call fails, the answer is None and the pair stays unmerged,
    which is the same outcome as the model saying it cannot tell.

    CACHED BY PAIR, because the model is not deterministic even at temperature 0 — measured
    at 2/5 merges on one identical pair — and the verdict is persisted into subject_ref at
    capture. Without the cache, whether two neighbours share a card is decided by chance.
    """
    cand_name = candidate.get("display_name") or candidate.get("subject_key")
    sig = pair_signature(str(name or ""), category, str(cand_name or ""), candidate.get("category"))
    hit, cached = _cached_verdict(sig)
    if hit:
        logger.info("reco_subject.adjudicate_cached sig=%s verdict=%s", sig, cached)
        return cached

    verdict = _ask_model(name, category, locality, candidate)
    _store_verdict(sig, verdict, (str(name or ""), category),
                   (str(cand_name or ""), candidate.get("category")))
    return verdict


def _ask_model(
    name: Any, category: Any, locality: Any, candidate: dict[str, Any]
) -> bool | None:
    """The call itself. Separated so the cache above reads as policy, not plumbing."""
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


# "Nothing was close enough" — distinct from a refused near-miss, which carries a candidate.
_NO_MATCH: dict[str, Any] = {"subject": None, "ambiguous": None}


def _create_subject(
    user_jwt: str,
    *,
    signal_id: str,
    name: str,
    category: str | None,
    locality: str | None,
    found: dict[str, Any] | None,
    mode: str = "aggregate",
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
                "p_merge_mode": mode,
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
    mode: str = "aggregate",
) -> dict[str, Any]:
    """Stage 2: attach this tip to an existing NON-PLACE subject, or report why not.

    Returns {"subject": id | None, "ambiguous": (candidate_id, score) | None}. A dict and
    not a bare id because "did not attach" has two meanings that must not collapse: nothing
    was close enough (nothing to record), or something WAS close and we refused it (a
    near-miss worth keeping). The caller records the refusal after creating this tip's own
    subject — see the comment at the return site.
    """
    from app.supabase_rpc import call_rpc

    key = normalize_subject_name(name)
    if not key:
        return _NO_MATCH
    rows = _candidates(
        user_jwt, key=key, category=category, locality=locality, mode=mode
    )
    if not rows:
        return _NO_MATCH

    scored = sorted(
        ((score_candidate(name, category, r), r) for r in rows),
        key=lambda pair: pair[0],
        reverse=True,
    )
    best_score, best = scored[0]
    if best_score < ADJUDICATE_FLOOR:
        return _NO_MATCH

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
            # NOT marked here. The caller still has to CREATE this tip's own subject, and
            # set_signal_subject rewrites subject_method/candidate_ref unconditionally —
            # so marking now would be overwritten microseconds later, which is exactly what
            # happened on the first real run (method read back as 'new', the near-miss
            # gone). The refusal is returned and the caller records it AFTER creating.
            return {"subject": None, "ambiguous": (str(best["id"]), float(best_score))}

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
        return _NO_MATCH
    return {"subject": str(best["id"]), "ambiguous": None}


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
    if rtype not in MERGEABLE_TYPES:
        return None
    mode = merge_mode_for(rtype)

    category = str((draft or {}).get("category") or "").strip() or None
    locality = str((draft or {}).get("locality") or "").strip() or None

    # ── The place identity space. A google_place_id is a FACT: exact, no threshold. ──
    # Skipped entirely for `product`, whose subject is a SKU — searching Places for a
    # kettle grounds it to whichever shop stocks it.
    # IS THIS SUBJECT A PLACE AT ALL? The capture flow already decided: subject_is_place()
    # gives a restaurant or a location the Places picker always, and a professional or a
    # service only when the extractor's `place_based` read says there is a storefront — "a
    # barber shop is, a plumber is not". Grounding must honour that verdict rather than
    # re-litigate it with a search, because a search ALWAYS finds something: "Mike Plumber"
    # resolved to a real plumbing company the neighbour never named, and then could not
    # merge with "Mike the Plumber", who had no listing (eval_reco_resolution, 2026-09-23).
    # Attaching a recommendation to a business nobody mentioned is the same error as
    # grounding a recipe to whichever bakery sells something like it.
    from app.reco_question_sets import subject_is_place

    is_place = subject_is_place(rtype, place_based=bool((draft or {}).get("place_based")))

    # Strongest first, and they are genuinely ranked, not merely ordered:
    #   1. the carousel pick   an id the user chose off a map — certain
    #   2. the chat-fork chip  an id we offered and she answered with — certain
    #   3. the search          our guess at what she typed — a judgement, floored at 0.82
    found = None
    if rtype in GROUNDABLE_TYPES and mode == "aggregate":
        # A PICK is user action and outranks the classifier: she chose this place off a
        # map, which is stronger evidence than any read of whether the subject "is a
        # place". (The picker only appears when subject_is_place already said yes, so this
        # is belt-and-braces — but the day those disagree, the human is right.)
        found = _picked(draft) or _tapped(name, (draft or {}).get("subject_place_options"))
        # A SEARCH is our guess, and it is the one that must defer: searching always finds
        # something, so running it for a subject the flow said is not a place is how a
        # plumber gets attached to a plumbing company nobody named.
        if not found and is_place:
            found = _searched(
                name, category=category, locality=locality,
                zip_code=zip_code, block_id=block_id, user_id=user_id,
            )

    # ── The second identity space. Everything here is a JUDGEMENT. ──
    # Reached by a product always, and by a professional/service/place that Google could
    # not settle — a plumber, a nanny, "Chef Ana meal prep" have no storefront to find.
    if not found:
        outcome = resolve_identity_subject(
            user_jwt, signal_id=signal_id, name=name, category=category,
            locality=locality, mode=mode,
        )
        if outcome["subject"]:
            return outcome["subject"]
        # Nothing to attach to: this tip creates its own subject, so the NEXT neighbour to
        # name the same thing has something to find.
        created = _create_subject(
            user_jwt, signal_id=signal_id, name=name,
            category=category, locality=locality, found=None, mode=mode,
        )
        # ORDER MATTERS. set_signal_subject stamps method='new' and clears the candidate,
        # so a refusal recorded before it is silently erased. Record it after.
        if outcome["ambiguous"]:
            candidate, score = outcome["ambiguous"]
            try:
                from app.supabase_rpc import call_rpc

                call_rpc(
                    user_jwt,
                    "mark_signal_subject_ambiguous",
                    {"p_signal_id": signal_id, "p_candidate": candidate,
                     "p_confidence": score},
                )
            except Exception:  # noqa: BLE001
                logger.info("reco_subject.mark_ambiguous_failed signal=%s", signal_id)
        return created

    return _create_subject(
        user_jwt, signal_id=signal_id, name=name,
        category=category, locality=locality, found=found, mode=mode,
    )
