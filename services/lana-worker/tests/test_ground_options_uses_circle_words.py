"""Grounding searches for the activity the person named, not its coarse bucket.

circle_type has ~10 values, so _TYPE_SEARCH mapped EVERY sport to "gym": a
`table_tennis_group` was offered Crunch Fitness, Lp Fit and FIT 407 Lake Nona
(2026-08-06). The same includedType=gym also narrowed the user's own TYPED search,
so "Search another spot" returned nothing and read as broken.
"""

from __future__ import annotations

import pytest

from app import circles_flow


@pytest.fixture
def searches(monkeypatch):
    """Capture every search_places call ground_options makes."""
    calls: list[dict] = []

    def _fake(**kw):
        calls.append(kw)
        return []

    monkeypatch.setattr(circles_flow, "search_places", _fake, raising=False)
    import app.places

    monkeypatch.setattr(app.places, "search_places", _fake)
    monkeypatch.setattr(circles_flow, "_resolve_place_name", lambda *_a, **_k: None)
    return calls


def _aff(key: str, ctype: str = "fitness") -> dict:
    return {"circle_key": key, "circle_type": ctype, "id": "aff-1"}


@pytest.mark.parametrize(
    "key,ctype,expected",
    [
        ("table_tennis_group", "fitness", "table tennis"),
        ("futsal", "fitness", "futsal"),
        ("crossfit_athlete", "fitness", "crossfit"),
        ("running_enthusiast", "fitness", "running"),
        ("church_attendee", "faith", "church"),
        ("violin", "hobby", "violin"),
    ],
)
def test_the_activity_is_searched_not_the_bucket(searches, key, ctype, expected) -> None:
    circles_flow.ground_options("u1", _aff(key, ctype), block_id="zip-32832")
    assert searches, "no search was attempted"
    assert searches[0]["query"] == expected
    assert searches[0]["included_type"] is None, (
        "includedType comes from the coarse bucket and filters out the very venue "
        "the words describe (a table-tennis hall is not a 'gym')"
    )


def test_a_generic_key_keeps_its_type_restriction(searches) -> None:
    """"gym" adds nothing over the type keyword, so the typed restriction stays —
    it is genuinely useful there."""
    circles_flow.ground_options("u1", _aff("gym", "fitness"), block_id="zip-32832")
    assert searches[0]["query"] == "gym"
    assert searches[0]["included_type"] == "gym"


def test_a_typed_search_is_never_narrowed_by_the_bucket(searches) -> None:
    """The person being specific must not be filtered by our guess at their type."""
    circles_flow.ground_options(
        "u1", _aff("gym", "fitness"), block_id="zip-32832", query="Orlando Table Tennis Hall"
    )
    assert searches, "typed search never ran"
    assert all(c["included_type"] is None for c in searches), (
        "a typed query was narrowed by includedType, which is why 'Search another "
        "spot' returned nothing"
    )
    assert searches[0]["query"] == "Orlando Table Tennis Hall"


def test_person_words_alone_fall_back_to_the_type_keyword(searches) -> None:
    """Strip the descriptors and nothing is left — use the bucket rather than ''."""
    circles_flow.ground_options("u1", _aff("member", "faith"), block_id="zip-32832")
    assert searches[0]["query"] == "church mosque synagogue temple"


# --- a typed search behaves like a search, not a name-matcher --------------


def _rows(*names):
    return [
        {"name": n, "address": f"{i} Main St", "place_id": f"p{i}"}
        for i, n in enumerate(names, 1)
    ]


@pytest.fixture
def serving(monkeypatch):
    """search_places returns fixed rows; capture the kwargs too."""
    calls: list[dict] = []
    rows: list[dict] = []

    def _fake(**kw):
        calls.append(kw)
        return rows

    import app.places

    monkeypatch.setattr(app.places, "search_places", _fake)
    monkeypatch.setattr(circles_flow, "_resolve_place_name", lambda *_a, **_k: None)
    return calls, rows


def test_typed_search_matches_on_the_name(serving) -> None:
    """A typed query is still verified against the place NAME — typing "Fitness CF"
    must not come back as "Crunch Fitness" (test_typed_search_never_falls_back_to_
    nearby_spots in test_circle_grounding.py owns that rule). What the includedType
    fix changes is that the search is no longer narrowed to the circle's bucket, so
    a venue whose name DOES carry the words can now be found at all."""
    calls, rows = serving
    rows.extend(_rows("Orlando Table Tennis Center", "Crunch Fitness - Lake Nona"))
    out = circles_flow.ground_options(
        "u1", _aff("table_tennis_group"), block_id="zip-32832", query="table tennis"
    )
    assert [o["name"] for o in out] == ["Orlando Table Tennis Center"]
    assert all(c["included_type"] is None for c in calls)


def test_an_inferred_name_is_still_verified(serving) -> None:
    """Unchanged where it matters: a name WE inferred must actually be borne by the
    place, or we would pin someone somewhere they never mentioned."""
    calls, rows = serving
    rows.extend(_rows("Crunch Fitness - Lake Nona"))
    monkey_name = "Fitness CF"
    import app.circles_flow as cf

    cf._resolve_place_name = lambda *_a, **_k: monkey_name  # inferred, not typed
    out = cf.ground_options("u1", _aff("fitness_cf"), block_id="zip-32832")
    assert all(o.get("suggested") for o in out), (
        "'Fitness CF' must not be accepted as 'Crunch Fitness' — only offered as a "
        "suggestion/consolation"
    )


def test_typed_search_returns_more_than_three(serving) -> None:
    calls, rows = serving
    circles_flow.ground_options(
        "u1", _aff("gym"), block_id="zip-32832", query="table tennis hall"
    )
    assert calls[0]["limit"] == 6, "a typed search should show more than the 3 chips"


# --- noun and emoji come from the community, not its grouping bucket -------


def test_stored_noun_beats_the_type_map() -> None:
    """circle_type "fitness" maps to "gym", so a table-tennis club was called "your
    gym" — in the question text and the place tag (2026-08-07)."""
    from app.circles_flow import place_relation_noun

    assert place_relation_noun("fitness", "table tennis club") == "table tennis club"
    assert place_relation_noun("faith", "small group") == "small group"


def test_the_type_map_is_still_the_fallback() -> None:
    """Rows captured before 20261008 have no noun — behaviour must not change."""
    from app.circles_flow import place_relation_noun

    assert place_relation_noun("fitness", None) == "gym"
    assert place_relation_noun("fitness", "", "table_tennis_group") == "gym"
    assert place_relation_noun(None, None) == "spot"


def test_stored_emoji_beats_the_type_map() -> None:
    from app.circles_flow import place_relation_emoji

    assert place_relation_emoji("fitness", "🏓") == "🏓"
    assert place_relation_emoji("fitness", None) == "🏋️"
    assert place_relation_emoji("nonsense", None) == "📍"


def test_a_noun_is_rejected_when_it_is_not_a_noun() -> None:
    """It renders as "your <noun>", so a clause there reads as gibberish — and a
    venue name would leak the place the relation deliberately hides."""
    from app.circles_capture import _clean_noun

    assert _clean_noun("table tennis club") == "table tennis club"
    assert _clean_noun("Small Group") == "small group"
    assert _clean_noun("your gym at fitness cf st cloud please") == ""
    assert _clean_noun(None) == ""


def test_only_one_emoji_survives() -> None:
    from app.circles_capture import _clean_emoji

    assert _clean_emoji("🏓") == "🏓"
    assert _clean_emoji("not an emoji") == ""
