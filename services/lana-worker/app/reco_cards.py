"""One card per recommended SUBJECT, built from the tip rows an ask returned.

find_neighbor_tips answers with rows — one per neighbour who recommended something. Three
neighbours who all recommend Dr. Sarah produce three rows, and until now the surface
rendered three cards and left the reader to notice they were one dentist. This groups them:
one card, carrying how many neighbours stand behind it and what each of them said.

WHAT THE CARD MAY CLAIM, exactly:
- `vouch_count` — how many neighbours recommended this thing. Computed in SQL across every
  visible live contribution, not over this page (see 20261222120000), and it INCLUDES the
  reader's own recommendation when they have one, flagged by `i_contributed` so the copy
  can say "you and 2 neighbours" rather than crediting her voice to strangers.
- `contributors` — who they are and what each wrote, never blended.
- `themes` — the Pareto read (app/reco_cluster.py), and ONLY for aggregate subjects. A
  collection lists its contributions and never summarises across them.

WHAT IT MAY NOT. The subject has no affinity of its own: Dr. Sarah is not Turkish because
a Turkish neighbour recommended her. Everything that ranks a card is attributed to a
CONTRIBUTOR or to the ask, never to the thing — the same rule app/tip_rec_cascade.py states
for peer rows, and the reason that module refuses stars and bands.

UNGROUNDED ROWS STILL WORK. A tip whose subject was never resolved (typed a name, below
the floor, an older row the backfill has not reached) becomes a card of its own with
`vouch_count` 1 — which is exactly how the surface behaves today, so nothing regresses
while grounding coverage climbs.
"""

from __future__ import annotations

import logging
from typing import Any

from app.ui_actions import peer_card_nudge_action

logger = logging.getLogger(__name__)

# Shared-circle first, then same-block, then the rest — the order the result headings
# render in, so the surface groups by walking the list. Same table as
# tip_rec_cascade._RANK and deliberately not a second opinion about it.
_GROUP_RANK = {"circle": 0, "block": 1, "nearby": 2}


def _strength(row: dict[str, Any]) -> float:
    try:
        return float(row.get("match_strength") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _clean_circles(raw: Any, *, limit: int = 3) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in raw if isinstance(raw, list) else []:
        if not isinstance(c, dict):
            continue
        name, pid = str(c.get("name") or "").strip(), str(c.get("place_id") or "").strip()
        if not name or not pid:
            continue
        out.append({
            "place_id": pid,
            "name": name,
            "circle_type": str(c.get("circle_type") or "").strip() or None,
        })
        if len(out) >= limit:
            break
    return out


def _group_of(circles: list[dict[str, Any]], same_block: bool) -> dict[str, Any]:
    if circles:
        return {"group_key": circles[0]["place_id"], "group_label": circles[0]["name"],
                "group_kind": "circle"}
    if same_block:
        return {"group_key": "block", "group_label": None, "group_kind": "block"}
    return {"group_key": "nearby", "group_label": None, "group_kind": "nearby"}


def _contributor(row: dict[str, Any], *, phone_verified: bool) -> dict[str, Any] | None:
    """One neighbour's voice on the card. None when there is nobody to attribute it to —
    a contribution with no author cannot be replied to or stood behind."""
    peer_id = str(row.get("peer_user_id") or "").strip()
    signal_id = str(row.get("signal_id") or "").strip()
    if not peer_id or not signal_id:
        return None
    nickname = str(row.get("neighbor_label") or "").strip() or "A neighbor"
    out: dict[str, Any] = {
        "signal_id": signal_id,
        "peer_user_id": peer_id,
        "nickname": nickname,
        "avatar_url": str(row.get("avatar_url") or "").strip() or None,
        # Their own words. detail_text is the legacy joined recap and is carried only as a
        # fallback for rows captured before the card fields existed.
        "description": str(row.get("reco_description") or "").strip() or None,
        "detail_text": str(row.get("detail_text") or "").strip() or None,
        # Their recommendation written out as prose, from THEIR OWN description and
        # answers (app/reco_body.py). Attached below; None whenever there was nothing
        # honest to compose, and the card falls back to the one-liner above.
        "body": None,
        "reco_fields": row.get("reco_fields") if isinstance(row.get("reco_fields"), list) else [],
        "match_strength": _strength(row),
        # How far THIS neighbour lives — distinct from the subject's distance on the card.
        "distance_text": str(row.get("distance_text") or "").strip() or None,
        "shared_circles": _clean_circles(row.get("shared_circles")),
        "same_block": bool(row.get("same_block")),
        "helpful_count": int(row.get("helpful_count") or 0),
        "created_at": row.get("created_at"),
    }
    if phone_verified:
        out["actions"] = [peer_card_nudge_action(nickname=nickname, peer_user_id=peer_id)]
    return out


def _card_from_group(
    rows: list[dict[str, Any]], *, phone_verified: bool
) -> dict[str, Any] | None:
    contributors = [c for c in (_contributor(r, phone_verified=phone_verified) for r in rows) if c]
    if not contributors:
        return None
    # Strongest match first, then oldest, then id — fully deterministic, so two reads of
    # one card cannot disagree about whose voice leads it.
    contributors.sort(key=lambda c: (-c["match_strength"], str(c["created_at"] or ""), c["signal_id"]))
    head = rows[0]
    lead = contributors[0]

    subject_ref = str(head.get("subject_ref") or "").strip() or None
    # An ungrounded row is its own subject of one: the count is the single contribution in
    # front of us, never a guess at how many others might exist.
    vouch = int(head.get("subject_vouch_count") or 0) if subject_ref else 1
    vouch = max(vouch, len(contributors))

    # Best provenance across contributors, not the first one's. A subject recommended from
    # a shared circle AND from nearby belongs under the circle heading.
    group = min(
        (_group_of(c["shared_circles"], c["same_block"]) for c in contributors),
        key=lambda g: _GROUP_RANK.get(g["group_kind"], 3),
    )

    title = (
        str(head.get("subject_name") or "").strip()
        or str(head.get("reco_name") or "").strip()
        or lead["nickname"]
    )
    card: dict[str, Any] = {
        "subject_ref": subject_ref,
        "title": title,
        "category": str(head.get("subject_category") or head.get("category") or "").strip() or None,
        "locality": str(head.get("subject_locality") or "").strip() or None,
        # The SUBJECT's distance where it is grounded; the lead contributor's otherwise, so
        # a card never shows a distance to a place we could not locate.
        "distance_text": (
            str(head.get("subject_distance_text") or "").strip() or lead["distance_text"]
        ),
        "distance_is_subject": bool(str(head.get("subject_distance_text") or "").strip()),
        "merge_mode": str(head.get("subject_merge_mode") or "aggregate"),
        "reco_type": str(head.get("reco_type") or "").strip() or None,
        "vouch_count": vouch,
        "i_contributed": bool(head.get("i_contributed")),
        # Ranking signals, kept apart and never multiplied into one score: the best match
        # among the voices, and how many voices there are, are two different facts.
        "match_strength": max(c["match_strength"] for c in contributors),
        "contributors": contributors,
        "themes": None,
        # "Six say it freezes well, two say it went watery — both froze it cooked."
        # Aggregate subjects only, and None whenever the voices simply agree — which is
        # the common case. A synthesis that manufactures tension to have something to say
        # is inventing the most damaging thing it could (app/reco_synthesis.py).
        "synthesis": None,
        # Proven overlap between the reader and SEVERAL contributors — "4 of these 8 have
        # toddlers, like you". Never a claim about the subject: four toddler parents
        # recommended her, and that is all it says.
        "cohorts": [],
        # The ask's own facets, echoed so the card can show WHY it is an answer to this
        # question. Not authored and not inferred — they are what the reader asked for.
        "fit_chips": [],
        "tip_rec": True,
        **group,
    }
    return card


def subject_cards_from_tips(
    tips: list[dict[str, Any]],
    *,
    phone_verified: bool = True,
    lang: str = "en",
    allow_compose: bool = False,
    reader_id: str | None = None,
    ask_chips: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Tip rows in, subject cards out.

    `allow_compose` is False by default and that is the important default: a results LIST
    renders from cached digests only, so a five-card turn costs zero model calls. The
    detail view — one subject, deliberately opened — passes True.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for row in tips:
        if not isinstance(row, dict):
            continue
        ref = str(row.get("subject_ref") or "").strip()
        # Ungrounded rows never share a bucket, even with each other: two tips we could not
        # resolve are not thereby the same thing.
        key = f"s:{ref}" if ref else f"u:{row.get('signal_id')}"
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(row)

    cards: list[dict[str, Any]] = []
    for key in order:
        card = _card_from_group(grouped[key], phone_verified=phone_verified)
        if card:
            cards.append(card)

    _attach_reads(cards, lang=lang, allow_compose=allow_compose)
    _attach_cohorts(cards, reader_id=reader_id)
    if ask_chips:
        chips = [c for c in (str(x or "").strip() for x in ask_chips) if c][:4]
        for card in cards:
            card["fit_chips"] = chips

    # Provenance, then best match, then most voices, then title. Title last so the order is
    # total — a float tie must not leave two cards swapping places between reads.
    cards.sort(key=lambda c: (
        _GROUP_RANK.get(str(c.get("group_kind")), 3),
        -c["match_strength"],
        -c["vouch_count"],
        c["title"].casefold(),
    ))
    logger.info(
        "reco_cards rows=%d cards=%d merged=%d themed=%d synth=%d bodies=%d compose=%s",
        len(tips), len(cards),
        sum(1 for c in cards if c["vouch_count"] > 1),
        sum(1 for c in cards if c.get("themes")),
        sum(1 for c in cards if c.get("synthesis")),
        sum(1 for c in cards for x in c["contributors"] if x.get("body")),
        allow_compose,
    )
    return cards


def _attach_cohorts(cards: list[dict[str, Any]], *, reader_id: str | None) -> None:
    """Who among each card's recommenders is like the reader.

    One claims read per contributor, shared across every card in the turn by the caller's
    own memoisation inside reco_cohort — a five-card page of familiar neighbours is a
    handful of reads, not one per card per person.

    Best-effort: a card without a cohort is a complete card. The cohort is the extra a
    reader gets when the people behind a recommendation happen to be like her.
    """
    if not reader_id:
        return
    from app.reco_cohort import cohort_within, cohorts_for

    for card in cards:
        peers = [c["peer_user_id"] for c in card["contributors"]]
        try:
            found = cohorts_for(reader_id, peers)
        except Exception:  # noqa: BLE001
            logger.warning("reco_cards.cohort_failed subject=%s", card["subject_ref"])
            continue
        card["cohorts"] = found
        if not found or not card.get("themes"):
            continue
        # And inside each theme: of the people who said THIS, how many are like her.
        by_signal = {c["signal_id"]: c["peer_user_id"] for c in card["contributors"]}
        for theme in card["themes"]:
            said_it = [by_signal[s] for s in theme.get("signal_ids") or [] if s in by_signal]
            narrowed = cohort_within(found[0], said_it)
            if narrowed:
                theme["cohort"] = narrowed


def _attach_reads(cards: list[dict[str, Any]], *, lang: str, allow_compose: bool) -> None:
    """Every AI-authored read on a card, and one background warm for all of them.

    Three reads, all optional and all cache-first: the themes across a subject, the
    synthesis that reconciles agreement with dissent, and each contributor's own body. A
    card renders completely without any of them — they are what a reader gets once enough
    neighbours have spoken, never what the card needs to exist.
    """
    subjects = _attach_digests(cards, lang=lang, allow_compose=allow_compose)
    bodies = _attach_bodies(cards, lang=lang, allow_compose=allow_compose)
    if subjects or bodies:
        _warm(subjects, bodies, lang=lang)


def _attach_bodies(
    cards: list[dict[str, Any]], *, lang: str, allow_compose: bool
) -> list[tuple[str, dict[str, Any]]]:
    """Each contributor's recommendation as prose. Returns what needs warming.

    PER CONTRIBUTION, which is the whole safety of it: a body is composed from ONE
    person's words and answers, so a merged card stacks several bodies rather than
    blending its neighbours into a description none of them wrote.
    """
    from app.reco_body import MIN_FACTS, body_for, facts_for

    pending: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for card in cards:
        for c in card["contributors"]:
            row = {"description": c["description"], "reco_fields": c["reco_fields"]}
            # A lone one-liner IS the body already. Composing from it could only pad it,
            # so these are never warmed and never retried.
            if len(facts_for(row)) < MIN_FACTS:
                continue
            try:
                c["body"] = body_for(
                    c["signal_id"], row, lang=lang, allow_compose=allow_compose
                )
            except Exception:  # noqa: BLE001 — a card without bodies is still a card
                logger.warning("reco_cards.body_failed signal=%s", c["signal_id"])
                continue
            if c["body"] is None and not allow_compose and c["signal_id"] not in seen:
                seen.add(c["signal_id"])
                pending.append((c["signal_id"], row))
    return pending


def _attach_digests(
    cards: list[dict[str, Any]], *, lang: str, allow_compose: bool
) -> list[tuple[str, str, list[dict[str, Any]]]]:
    """The Pareto read and the synthesis, for the aggregate cards that have the voices.

    Best-effort and per card: a digest that fails leaves that card listing its
    contributors, which is a complete card already — themes are the extra a reader gets
    when several neighbours agree, never the thing the card needs to render.
    """
    from app.reco_cluster import MIN_FOR_DIGEST, contribution_text, digest_for_subject
    from app.reco_synthesis import MIN_CONTRIBUTIONS as MIN_FOR_SYNTHESIS
    from app.reco_synthesis import synthesis_for_subject

    pending: list[tuple[str, str, list[dict[str, Any]]]] = []
    for card in cards:
        if (
            not card["subject_ref"]
            or card["merge_mode"] == "collection"
            or len(card["contributors"]) < MIN_FOR_DIGEST
        ):
            continue
        contributions = [
            {
                "signal_id": c["signal_id"],
                "text": contribution_text(
                    {"reco_description": c["description"], "reco_fields": c["reco_fields"]}
                ),
            }
            for c in card["contributors"]
        ]
        want_warm = False
        try:
            digest = digest_for_subject(
                card["subject_ref"], contributions,
                lang=lang, merge_mode=card["merge_mode"], allow_compose=allow_compose,
            )
        except Exception:  # noqa: BLE001 — a card without themes is still a card
            logger.warning("reco_cards.digest_failed subject=%s", card["subject_ref"])
            digest = None
        if digest:
            card["themes"] = digest["themes"]
        elif not allow_compose:
            want_warm = True

        # The same contributions, read for what they DISAGREE about. Keyed on the same
        # basis_sig, so themes and synthesis on one card are always built from the same
        # set of voices.
        try:
            syn = synthesis_for_subject(
                card["subject_ref"], contributions,
                lang=lang, merge_mode=card["merge_mode"], allow_compose=allow_compose,
            )
        except Exception:  # noqa: BLE001
            logger.warning("reco_cards.synthesis_failed subject=%s", card["subject_ref"])
            # Not None, which would schedule a warm on a call that just threw, and not {},
            # which would put an empty object on the wire. Nothing is persisted, so the
            # next turn asks again.
            syn = {"absent": True}
        # None means UNDECIDED, so it is worth warming — but ONLY where a synthesis is
        # possible at all. Below the floor there is no "most neighbours" to speak of and
        # None is the permanent answer, so warming it would re-ask an unanswerable
        # question on every render forever. A stored `absent` is the same trap one step
        # later: the question WAS asked, and these neighbours simply agreed.
        if syn is None:
            want_warm = want_warm or len(contributions) >= MIN_FOR_SYNTHESIS
        elif not syn.get("absent"):
            card["synthesis"] = syn

        if want_warm:
            # Cache miss on a list render. Warm it behind the turn so the NEXT read of
            # this card has its themes, instead of either blocking the reader now or
            # leaving the digest permanently uncomposed because nothing ever asks for it.
            pending.append((card["subject_ref"], card["merge_mode"], contributions))

    return pending


# At most this many subjects warmed per turn. A results page is five cards; warming every
# one of a widened twelve-row fetch would put twelve model calls behind one question.
_MAX_WARM = 3
# Bodies are per CONTRIBUTION, so one card of four neighbours is four calls on its own.
# A slightly wider budget than subjects, still far below "every voice on the page".
_MAX_WARM_BODIES = 6


def _warm(
    subjects: list[tuple[str, str, list[dict[str, Any]]]],
    bodies: list[tuple[str, dict[str, Any]]],
    *,
    lang: str,
) -> None:
    """Compose the missing reads off the turn, on a daemon thread.

    Same shape as signal_match_notify: the reader's turn never waits on work that only
    improves the NEXT read. Bounded and best-effort — a warm that fails leaves the card
    exactly as it renders today, listing its contributors.
    """
    import threading

    from app.reco_body import body_for
    from app.reco_cluster import digest_for_subject
    from app.reco_synthesis import synthesis_for_subject

    def _run() -> None:
        for subject_ref, mode, contributions in subjects[:_MAX_WARM]:
            # Themes FIRST: the synthesis caches into the digest row, and updates nothing
            # when that row does not exist yet.
            try:
                digest_for_subject(
                    subject_ref, contributions, lang=lang,
                    merge_mode=mode, allow_compose=True,
                )
            except Exception:  # noqa: BLE001
                logger.info("reco_cards.warm_failed subject=%s", subject_ref)
            try:
                synthesis_for_subject(
                    subject_ref, contributions, lang=lang,
                    merge_mode=mode, allow_compose=True,
                )
            except Exception:  # noqa: BLE001
                logger.info("reco_cards.warm_synthesis_failed subject=%s", subject_ref)
        for signal_id, row in bodies[:_MAX_WARM_BODIES]:
            try:
                body_for(signal_id, row, lang=lang, allow_compose=True)
            except Exception:  # noqa: BLE001
                logger.info("reco_cards.warm_body_failed signal=%s", signal_id)

    try:
        threading.Thread(target=_run, daemon=True, name="reco-read-warm").start()
    except Exception:  # noqa: BLE001
        pass
