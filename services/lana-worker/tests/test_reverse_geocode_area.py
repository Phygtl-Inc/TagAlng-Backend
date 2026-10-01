"""The "Around me" pill names the neighborhood and city, not the street (Tommaso,
2026-10-01): the geocoder's address_components were read for nothing but the first
comma-chunk of formatted_address."""

import unittest
from unittest.mock import MagicMock, patch

from app.places import reverse_geocode


def _comp(name: str, *types: str) -> dict:
    return {"long_name": name, "short_name": name, "types": list(types)}


STREET = {
    "formatted_address": "13000 Lake Nona Blvd, Orlando, FL 32827, USA",
    "place_id": "p1",
    "address_components": [
        _comp("13000", "street_number"),
        _comp("Lake Nona Boulevard", "route"),
        _comp("Lake Nona", "neighborhood", "political"),
        _comp("Orlando", "locality", "political"),
        _comp("Florida", "administrative_area_level_1", "political"),
        _comp("32827", "postal_code"),
    ],
}


def _run(results: list[dict]) -> dict | None:
    res = MagicMock(status_code=200)
    res.json.return_value = {"results": results}
    client = MagicMock()
    client.__enter__.return_value.get.return_value = res
    with patch.dict("os.environ", {"GOOGLE_MAPS_API_KEY": "k"}), patch(
        "app.places.httpx.Client", return_value=client
    ):
        return reverse_geocode(28.4, -81.2)


class ReverseGeocodeAreaTests(unittest.TestCase):
    def test_neighborhood_and_city_are_returned(self) -> None:
        row = _run([STREET])
        self.assertEqual(row["area_label"], "Lake Nona, Orlando")
        self.assertEqual(row["neighborhood"], "Lake Nona")
        self.assertEqual(row["city"], "Orlando")

    def test_the_street_still_names_the_pin(self) -> None:
        # "Use my current location" on a meet pins at the street — unchanged.
        self.assertEqual(_run([STREET])["name"], "13000 Lake Nona Blvd")

    def test_a_neighborhood_only_on_a_later_result_is_still_found(self) -> None:
        top = dict(STREET, address_components=[_comp("Orlando", "locality")])
        later = {"formatted_address": "x", "address_components": [_comp("Lake Nona", "neighborhood")]}
        self.assertEqual(_run([top, later])["area_label"], "Lake Nona, Orlando")

    def test_no_neighborhood_falls_back_to_the_city_alone(self) -> None:
        top = dict(STREET, address_components=[_comp("Orlando", "locality")])
        self.assertEqual(_run([top])["area_label"], "Orlando")

    def test_a_neighborhood_named_like_its_city_is_not_said_twice(self) -> None:
        top = dict(
            STREET,
            address_components=[_comp("Orlando", "sublocality"), _comp("Orlando", "locality")],
        )
        self.assertEqual(_run([top])["area_label"], "Orlando")

    def test_nothing_parseable_leaves_the_label_empty(self) -> None:
        top = dict(STREET, address_components=[])
        self.assertIsNone(_run([top])["area_label"])
