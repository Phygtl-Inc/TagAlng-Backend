import unittest

from app.lana_unified_pipeline import _is_generic_place


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


if __name__ == "__main__":
    unittest.main()
