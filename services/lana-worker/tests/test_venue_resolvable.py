import unittest
from unittest import mock

from app.lana_unified_pipeline import (
    _PLACE_SUGGESTIONS,
    _SEARCH_PLACE_OPTION,
    _is_generic_place,
    _where_step_chips,
)


class TestVenueResolvable(unittest.TestCase):
    def test_block_local_answers_are_generic(self) -> None:
        # These resolve to the host's block — no exact Google pin required.
        for v in ["My place", "the park", "Playground", "somewhere on the block",
                  "my backyard", "home", "The Clubhouse"]:
            self.assertTrue(_is_generic_place(v), v)

    def test_named_venues_are_not_generic(self) -> None:
        # A business / named place must be pinned via place-search (place_id), so it
        # is NOT generic and the where-step keeps asking until pinned.
        for v in ["KFC", "Foxtail Coffee", "Tampines Park", "123 Main St", "Joyland"]:
            self.assertFalse(_is_generic_place(v), v)

    def test_handles_punctuation_and_blank(self) -> None:
        self.assertTrue(_is_generic_place("my place."))
        self.assertFalse(_is_generic_place(""))
        self.assertFalse(_is_generic_place(None))

    def test_default_where_chips_are_all_resolvable(self) -> None:
        # Every default where-step chip (except the Search sentinel) must be a generic
        # place, so the host-flow's generic-place capture resolves it the moment it's
        # tapped — otherwise tapping the chip re-enters the "Which X exactly?" loop
        # (the "My place" disambiguation bug). Guards against adding a non-pinnable
        # named venue to the default chip list.
        for chip in _PLACE_SUGGESTIONS:
            self.assertTrue(_is_generic_place(chip), chip)
        self.assertNotEqual(_SEARCH_PLACE_OPTION, "")

    def test_nearby_chips_stash_pins_for_auto_pin(self) -> None:
        # A nearby Google suggestion is a real place WITH a pin. The where-step must
        # stash those pins keyed by the chip label (lowercased) so tapping the chip can
        # be stamped directly instead of bouncing into "Which X exactly?".
        rows = [
            {"name": "Randal Park Community Center", "place_id": "pid_1",
             "lat": 28.36, "lng": -81.25, "address": "123 Randal Ave"},
            {"name": "Laureate Park", "place_id": "pid_2",
             "lat": 28.37, "lng": -81.26, "address": "Laureate Blvd"},
            {"name": "No Pin Place", "place_id": "", "lat": None, "lng": None},
        ]
        turn_ctx: dict = {}
        with mock.patch(
            "app.lana_unified_pipeline._nearby_host_place_rows", return_value=rows
        ):
            chips = _where_step_chips({}, "blk", "user", turn_ctx)
        # Named chips + "My place" + Search; the pinless row is dropped.
        self.assertIn("Randal Park Community Center", chips)
        self.assertIn("Laureate Park", chips)
        self.assertNotIn("No Pin Place", chips)
        self.assertIn("My place", chips)
        self.assertEqual(chips[-1], _SEARCH_PLACE_OPTION)
        # Pins stashed for the next turn's auto-pin, keyed by the lowercased label.
        cand = turn_ctx["event_place_candidates"]
        self.assertEqual(cand["randal park community center"]["place_id"], "pid_1")
        self.assertNotIn("no pin place", cand)

    def test_where_chips_fall_back_to_generics_with_no_nearby(self) -> None:
        turn_ctx: dict = {}
        with mock.patch(
            "app.lana_unified_pipeline._nearby_host_place_rows", return_value=[]
        ):
            chips = _where_step_chips({}, None, None, turn_ctx)
        self.assertEqual(chips, list(_PLACE_SUGGESTIONS) + [_SEARCH_PLACE_OPTION])
        self.assertNotIn("event_place_candidates", turn_ctx)


if __name__ == "__main__":
    unittest.main()
