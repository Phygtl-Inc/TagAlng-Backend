"""search_cities: the "which city is it run from?" picker — towns and cities only."""

from __future__ import annotations

from typing import Any

import app.places as places

_RESPONSE = {
    "suggestions": [
        {"placePrediction": {
            "placeId": "ChIJOwg_06VPwokRYv534QaPC8g",
            "text": {"text": "New York, NY, USA"},
            "structuredFormat": {"mainText": {"text": "New York"},
                                 "secondaryText": {"text": "NY, USA"}}}},
        {"placePrediction": {
            "placeId": "p2",
            "text": {"text": "New York Mills, NY, USA"},
            "structuredFormat": {"mainText": {"text": "New York Mills"}}}},
        {"queryPrediction": {"text": {"text": "new york pizza"}}},
    ]
}


class _Client:
    def __init__(self, sent: list[dict[str, Any]], payload: Any) -> None:
        self.sent, self.payload = sent, payload

    def __enter__(self) -> "_Client":
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def post(self, url: str, *, headers: dict, json: dict) -> Any:
        self.sent.append({"url": url, "json": json})
        payload = self.payload

        class _Res:
            def json(self) -> Any:
                return payload

        return _Res()


def _run(monkeypatch: Any, payload: Any, *, centroid: Any = (40.8, -73.9),
         query: str = "New Yo") -> tuple[list[dict], list[dict]]:
    sent: list[dict[str, Any]] = []
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "k")
    monkeypatch.setattr(places, "_centroid", lambda *a, **k: centroid)
    monkeypatch.setattr(places.httpx, "Client", lambda **k: _Client(sent, payload))
    return places.search_cities(query=query, user_id="u1"), sent


def test_asks_google_for_cities_only_and_returns_full_labels(monkeypatch: Any) -> None:
    rows, sent = _run(monkeypatch, _RESPONSE)
    assert sent[0]["url"].endswith("places:autocomplete")
    assert sent[0]["json"]["includedPrimaryTypes"] == ["(cities)"]
    assert rows == [
        {"name": "New York", "address": "New York, NY, USA",
         "place_id": "ChIJOwg_06VPwokRYv534QaPC8g"},
        {"name": "New York Mills", "address": "New York Mills, NY, USA", "place_id": "p2"},
    ]


def test_biased_to_their_area_never_restricted(monkeypatch: Any) -> None:
    _, sent = _run(monkeypatch, _RESPONSE)
    assert "locationBias" in sent[0]["json"] and "locationRestriction" not in sent[0]["json"]
    _, sent = _run(monkeypatch, _RESPONSE, centroid=None)
    assert "locationBias" not in sent[0]["json"]  # still searches, just unbiased


def test_failures_and_short_queries_are_empty(monkeypatch: Any) -> None:
    assert _run(monkeypatch, {"error": {"message": "denied"}})[0] == []
    assert _run(monkeypatch, "not json")[0] == []
    rows, sent = _run(monkeypatch, _RESPONSE, query="N")
    assert rows == [] and sent == []


def test_endpoint_routes_city_kind_to_the_city_search(monkeypatch: Any) -> None:
    import app.main as main

    monkeypatch.setattr(main, "verify_auth", lambda a: type("A", (), {
        "home_block_id": None, "user_id": "u1"})())
    monkeypatch.setattr(places, "search_cities", lambda **k: [
        {"name": "Lisbon", "address": "Lisbon, Portugal", "place_id": "x"}])
    monkeypatch.setattr(places, "search_places", lambda **k: [{"name": "Café"}])
    from app.models import PlaceSearchRequest

    city = main.search_places_endpoint(PlaceSearchRequest(q="Lisb", kind="city"), "Bearer t")
    assert [r.name for r in city.results] == ["Lisbon"]
    assert city.results[0].address == "Lisbon, Portugal"
    plain = main.search_places_endpoint(PlaceSearchRequest(q="Lisb"), "Bearer t")
    assert [r.name for r in plain.results] == ["Café"]
