import unittest
from unittest.mock import patch

import app.reco_subject as mod


def _rpc(*, candidates, subject_id="sub-new", attach=True):
    """Stand-in for call_rpc, dispatching on the RPC NAME.

    Stage 2 makes one call site talk to four different functions, and a bare MagicMock
    answers all four with the same truthy sentinel — so a test can neither tell which one
    ran nor trust what came back.
    """
    def dispatch(_jwt, fn, args):
        if fn == "reco_subject_candidates":
            return candidates
        if fn == "set_signal_subject":
            return subject_id
        if fn == "attach_signal_subject":
            return attach
        if fn == "mark_signal_subject_ambiguous":
            return True
        raise AssertionError(f"unexpected rpc {fn}")
    return dispatch


def _call_of(rpc_mock, fn):
    """The args dict of the one call made to `fn`, or fail loudly."""
    for call in rpc_mock.call_args_list:
        if call[0][1] == fn:
            return call[0][2]
    raise AssertionError(f"{fn} was never called; saw {[c[0][1] for c in rpc_mock.call_args_list]}")


class TestNormalizeSubjectName(unittest.TestCase):
    """The identity string two recommendations are compared on. Deliberately more
    aggressive than local_signals.reco_subject_key(), which is case/whitespace only —
    that key stays as it is because the agree-row tallies are built on it."""

    def test_titles_and_case(self):
        self.assertEqual(mod.normalize_subject_name("Dr. Sarah Chen"), "sarah chen")
        self.assertEqual(mod.normalize_subject_name("dr sarah chen"), "sarah chen")
        self.assertEqual(mod.normalize_subject_name("  DR.   Sarah  Chen "), "sarah chen")

    def test_legal_suffixes_stack(self):
        self.assertEqual(mod.normalize_subject_name("Sarah Chen, DDS"), "sarah chen")
        self.assertEqual(mod.normalize_subject_name("Sarah Chen, DDS, PA"), "sarah chen")
        self.assertEqual(mod.normalize_subject_name("Narcoossee Kids Clinic LLC"),
                         "narcoossee kids clinic")

    def test_punctuation_becomes_space_not_nothing(self):
        # "kids'clinic" must not collapse into one word while "Kids' Clinic" makes two.
        self.assertEqual(mod.normalize_subject_name("Narcoossee Kids' Clinic"),
                         "narcoossee kids clinic")
        self.assertEqual(mod.normalize_subject_name("Kids'Clinic"), "kids clinic")

    def test_empty(self):
        self.assertEqual(mod.normalize_subject_name(None), "")
        self.assertEqual(mod.normalize_subject_name("   "), "")


class TestNameMatchScore(unittest.TestCase):
    def test_exact_after_normalization(self):
        self.assertEqual(mod.name_match_score("Dr. Sarah Chen", "Sarah Chen, DDS"), 1.0)

    def test_containment_beats_raw_ratio(self):
        # The case the floor exists for: a user types what she calls her, Google answers
        # with the full listing. A raw sequence ratio reads ~0.55 and would lose it.
        score = mod.name_match_score("Dr. Sarah", "Dr. Sarah Chen, DDS")
        self.assertGreaterEqual(score, mod.MATCH_FLOOR)

    def test_short_strings_do_not_match_by_containment(self):
        # "CF" is contained in a great many names; 4-char floor keeps it honest.
        self.assertLess(mod.name_match_score("CF", "CF Fitness Lake Nona"), 0.9)

    def test_different_places_stay_below_floor(self):
        self.assertLess(mod.name_match_score("Dr. Sarah", "Dr. Ahmed"), mod.MATCH_FLOOR)
        self.assertLess(
            mod.name_match_score("Narcoossee Kids Clinic", "Lake Nona Pediatric Dentistry"),
            mod.MATCH_FLOOR,
        )

    def test_missing_side_is_zero(self):
        self.assertEqual(mod.name_match_score("", "Dr. Sarah"), 0.0)
        self.assertEqual(mod.name_match_score("Dr. Sarah", None), 0.0)


class TestPlaceOptionsMap(unittest.TestCase):
    def test_keys_on_normalized_label(self):
        out = mod.place_options_map([
            {"name": "Dr. Sarah Chen", "place_id": "ChIJ_a", "lat": 1.0, "lng": 2.0},
        ])
        self.assertIn("sarah chen", out)
        self.assertEqual(out["sarah chen"]["place_id"], "ChIJ_a")

    def test_skips_rows_without_an_id(self):
        out = mod.place_options_map([
            {"name": "No Id Place"},
            {"name": "Good", "place_id": "  "},
            {"place_id": "ChIJ_orphan"},
            "not a dict",
        ])
        self.assertEqual(out, {})

    def test_tolerates_garbage(self):
        self.assertEqual(mod.place_options_map(None), {})
        self.assertEqual(mod.place_options_map("nope"), {})


class TestGroundRecoSubject(unittest.TestCase):
    """Stage 1: stamp subject_ref, or leave it null, but never raise."""

    TAPPED = [{"name": "Dr. Sarah Chen", "place_id": "ChIJ_tapped", "lat": 28.4, "lng": -81.2}]

    def _draft(self, **over):
        draft = {
            "name": "Dr. Sarah Chen",
            "reco_type": "professional",
            "category": "pediatric dentist",
            "locality": "Lake Nona",
            # A clinic is walked into; the extractor reads it as a place, and grounding
            # now honours that rather than searching for every professional.
            "place_based": True,
        }
        draft.update(over)
        return draft

    def test_tapped_place_grounds_with_no_search(self):
        with patch.object(mod, "__name__", mod.__name__), \
             patch("app.supabase_rpc.call_rpc", return_value="sub-1") as rpc, \
             patch("app.places.search_places") as search:
            out = mod.ground_reco_subject(
                "jwt", signal_id="sig-1",
                draft=self._draft(subject_place_options=self.TAPPED),
            )
        self.assertEqual(out, "sub-1")
        search.assert_not_called()
        args = rpc.call_args[0]
        self.assertEqual(args[1], "set_signal_subject")
        self.assertEqual(args[2]["p_google_place_id"], "ChIJ_tapped")
        # Author casing for the card title; the normalized key for grouping.
        self.assertEqual(args[2]["p_display_name"], "Dr. Sarah Chen")
        self.assertEqual(args[2]["p_subject_key"], "sarah chen")
        self.assertEqual(args[2]["p_lat"], 28.4)

    def test_typed_name_falls_back_to_search(self):
        hit = [{"name": "Dr. Sarah Chen, DDS", "place_id": "ChIJ_found", "lat": 1.0, "lng": 2.0}]
        with patch("app.places.search_places", return_value=hit), \
             patch("app.supabase_rpc.call_rpc", return_value="sub-2") as rpc:
            out = mod.ground_reco_subject("jwt", signal_id="sig-2", draft=self._draft())
        self.assertEqual(out, "sub-2")
        self.assertEqual(rpc.call_args[0][2]["p_google_place_id"], "ChIJ_found")

    def test_search_below_floor_never_grounds_to_the_wrong_place(self):
        # A wrong merge invents corroboration. The tip still gets a subject of its own —
        # that is what gives the next neighbour naming the same dentist something to find —
        # but it must NOT carry the place id of a different practice.
        miss = [{"name": "Lake Nona Pediatric Dentistry", "place_id": "ChIJ_other"}]
        with patch("app.places.search_places", return_value=miss), \
             patch("app.supabase_rpc.call_rpc", side_effect=_rpc(candidates=[])) as rpc:
            out = mod.ground_reco_subject("jwt", signal_id="sig-3", draft=self._draft())
        self.assertEqual(out, "sub-new")
        created = _call_of(rpc, "set_signal_subject")
        self.assertIsNone(created["p_google_place_id"])

    def test_search_failure_still_creates_its_own_subject(self):
        with patch("app.places.search_places", side_effect=RuntimeError("google down")), \
             patch("app.supabase_rpc.call_rpc", side_effect=_rpc(candidates=[])) as rpc:
            out = mod.ground_reco_subject("jwt", signal_id="sig-3b", draft=self._draft())
        self.assertEqual(out, "sub-new")
        self.assertIsNone(_call_of(rpc, "set_signal_subject")["p_google_place_id"])

    def test_recipe_shares_a_subject_but_never_a_place(self):
        # Standup 2026-09-22: a recipe DOES share a subject ("banana bread"), it is simply
        # always on the Pareto minority side, so its contributions stay standalone. What it
        # must never do is take the place path — a recipe is not a point on a map.
        with patch("app.places.search_places") as search, \
             patch("app.supabase_rpc.call_rpc", side_effect=_rpc(candidates=[])) as rpc:
            out = mod.ground_reco_subject(
                "jwt", signal_id="sig-4",
                draft=self._draft(reco_type="recipe", name="Banana bread"),
            )
        self.assertEqual(out, "sub-new")
        search.assert_not_called()
        created = _call_of(rpc, "set_signal_subject")
        self.assertIsNone(created["p_google_place_id"])
        # The rail that stops Stage 3 blending two authors' ingredients.
        self.assertEqual(created["p_merge_mode"], "collection")

    def test_diy_and_other_are_collections_too(self):
        for rtype in ("diy", "other"):
            with self.subTest(rtype=rtype), \
                 patch("app.places.search_places") as search, \
                 patch("app.supabase_rpc.call_rpc", side_effect=_rpc(candidates=[])) as rpc:
                out = mod.ground_reco_subject(
                    "jwt", signal_id="sig-5", draft=self._draft(reco_type=rtype),
                )
                self.assertEqual(out, "sub-new")
                search.assert_not_called()
                self.assertEqual(
                    _call_of(rpc, "set_signal_subject")["p_merge_mode"], "collection"
                )

    def test_aggregate_types_are_marked_aggregate(self):
        with patch("app.places.search_places", return_value=[]), \
             patch("app.supabase_rpc.call_rpc", side_effect=_rpc(candidates=[])) as rpc:
            mod.ground_reco_subject("jwt", signal_id="sig-5b", draft=self._draft())
        self.assertEqual(_call_of(rpc, "set_signal_subject")["p_merge_mode"], "aggregate")

    def test_product_skips_places_and_uses_the_identity_space(self):
        # A SKU is a shared referent but not a map point: searching Places for a kettle
        # would ground it to whichever shop stocks it.
        with patch("app.places.search_places") as search, \
             patch("app.supabase_rpc.call_rpc", side_effect=_rpc(candidates=[])) as rpc:
            out = mod.ground_reco_subject(
                "jwt", signal_id="sig-6",
                draft=self._draft(reco_type="product", name="Cosori gooseneck"),
            )
        search.assert_not_called()
        self.assertEqual(out, "sub-new")
        self.assertIsNone(_call_of(rpc, "set_signal_subject")["p_google_place_id"])

    def test_rpc_failure_is_swallowed(self):
        # A tip that posted must never fail on its subject.
        with patch("app.places.search_places", return_value=[]), \
             patch("app.supabase_rpc.call_rpc", side_effect=RuntimeError("boom")):
            out = mod.ground_reco_subject(
                "jwt", signal_id="sig-7",
                draft=self._draft(subject_place_options=self.TAPPED),
            )
        self.assertIsNone(out)

    def test_no_name_no_grounding(self):
        with patch("app.supabase_rpc.call_rpc") as rpc:
            self.assertIsNone(
                mod.ground_reco_subject("jwt", signal_id="sig-9", draft=self._draft(name=""))
            )
            self.assertIsNone(
                mod.ground_reco_subject("jwt", signal_id="", draft=self._draft())
            )
        rpc.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class TestPublishWiring(unittest.TestCase):
    """The wiring, not the logic: _save_tip must actually reach grounding, and must still
    post the tip when grounding blows up. This is the join most likely to go quietly
    missing — tag_local_signal's own comment next door says exactly that."""

    def _draft(self, **over):
        draft = {
            "name": "Dr. Sarah Chen",
            "reco_type": "professional",
            "category": "pediatric dentist",
            "locality": "Lake Nona",
            "trait": "gentle with toddlers",
            "answers": {"profession": "Pediatric dentist", "helped_with": "a cleaning"},
        }
        draft.update(over)
        return draft

    def _save(self, draft):
        import app.tip_share as ts

        with patch("app.local_signals.save_local_signal",
                   return_value={"id": "sig-1", "signal_id": "sig-1", "matches_created": 0}), \
             patch("app.tip_tags.tags_for_tip", return_value=[]), \
             patch("app.auth.jwt_user_id", return_value="u-1"), \
             patch("app.reco_subject.ground_reco_subject") as ground:
            saved, err = ts._save_tip(
                draft=draft, user_jwt="jwt", block_id="b1", zip_code=None,
            )
        return saved, err, ground

    def test_publish_grounds_the_subject(self):
        saved, err, ground = self._save(self._draft())
        self.assertEqual(err, "")
        self.assertEqual(saved["id"], "sig-1")
        ground.assert_called_once()
        kwargs = ground.call_args.kwargs
        self.assertEqual(kwargs["signal_id"], "sig-1")
        self.assertEqual(kwargs["draft"]["name"], "Dr. Sarah Chen")

    def test_tip_still_posts_when_grounding_raises(self):
        import app.tip_share as ts

        with patch("app.local_signals.save_local_signal",
                   return_value={"id": "sig-2", "signal_id": "sig-2", "matches_created": 0}), \
             patch("app.tip_tags.tags_for_tip", return_value=[]), \
             patch("app.auth.jwt_user_id", return_value="u-1"), \
             patch("app.reco_subject.ground_reco_subject",
                   side_effect=RuntimeError("places on fire")):
            saved, err = ts._save_tip(
                draft=self._draft(), user_jwt="jwt", block_id="b1", zip_code=None,
            )
        # The recommendation is the thing; the subject is a nice-to-have.
        self.assertEqual(err, "")
        self.assertEqual(saved["id"], "sig-2")


class TestIdentitySpace(unittest.TestCase):
    """Stage 2 — the second identity space: subjects that merge but are not map points.

    Everything here is a JUDGEMENT, so the test that matters most is not "does it merge"
    but "does it refuse when it should". A wrong merge invents corroboration, and the
    vouch count is the one number a stranger is meant to trust.
    """

    PLUMBER = {
        "id": "sub-mike", "subject_key": "mike the plumber",
        "display_name": "Mike the Plumber", "category": "plumber",
        "locality": "Lake Nona", "signal_count": 2,
    }

    def _draft(self, **over):
        draft = {
            "name": "Mike the Plumber",
            "reco_type": "service",
            "category": "plumber",
            "locality": "Lake Nona",
        }
        draft.update(over)
        return draft

    def _run(self, draft, candidates, verdict="unused"):
        """Identity path only: Google finds nothing, so the place space is exhausted."""
        with patch("app.places.search_places", return_value=[]), \
             patch("app.reco_subject._adjudicate", return_value=verdict) as adj, \
             patch("app.supabase_rpc.call_rpc",
                   side_effect=_rpc(candidates=candidates)) as rpc:
            out = mod.ground_reco_subject("jwt", signal_id="sig-x", draft=draft)
        return out, rpc, adj

    def test_near_identical_name_merges_without_a_model_call(self):
        out, rpc, adj = self._run(self._draft(name="Mike the Plumber"), [self.PLUMBER])
        self.assertEqual(out, "sub-mike")
        adj.assert_not_called()
        args = _call_of(rpc, "attach_signal_subject")
        self.assertEqual(args["p_subject_id"], "sub-mike")
        self.assertEqual(args["p_method"], "blocked")

    def test_ambiguous_band_asks_the_model_and_merges_on_yes(self):
        # "Mike Plumber" vs "Mike the Plumber" scores 0.857 — close, not close enough to
        # assert. (Band membership is measured, not assumed: see test_bands_are_real.)
        out, rpc, adj = self._run(
            self._draft(name="Mike Plumber"), [self.PLUMBER], verdict=True,
        )
        adj.assert_called_once()
        self.assertEqual(out, "sub-mike")
        self.assertEqual(_call_of(rpc, "attach_signal_subject")["p_method"], "adjudicated")

    def test_model_says_different_so_nothing_merges(self):
        out, rpc, adj = self._run(
            self._draft(name="Mike Plumber"), [self.PLUMBER], verdict=False,
        )
        adj.assert_called_once()
        # Its own subject, and the near-miss remembered for a later pass.
        self.assertEqual(out, "sub-new")
        self.assertEqual(_call_of(rpc, "mark_signal_subject_ambiguous")["p_candidate"], "sub-mike")
        with self.assertRaises(AssertionError):
            _call_of(rpc, "attach_signal_subject")

    def test_model_cannot_tell_so_nothing_merges(self):
        # None and False do the same thing on purpose: neither is a yes.
        out, rpc, _ = self._run(
            self._draft(name="Mike Plumber"), [self.PLUMBER], verdict=None,
        )
        self.assertEqual(out, "sub-new")
        _call_of(rpc, "mark_signal_subject_ambiguous")
        with self.assertRaises(AssertionError):
            _call_of(rpc, "attach_signal_subject")

    def test_unrelated_candidate_is_not_even_ambiguous(self):
        # Below the adjudicate floor: no model call, and nothing worth remembering.
        out, rpc, adj = self._run(self._draft(name="Chef Ana meal prep"), [self.PLUMBER])
        self.assertEqual(out, "sub-new")
        adj.assert_not_called()
        with self.assertRaises(AssertionError):
            _call_of(rpc, "mark_signal_subject_ambiguous")

    def test_category_clash_blocks_an_auto_merge(self):
        # Same name, different trade — the collision the cap exists for. The name alone
        # would auto-merge; the disagreeing category forces the model to look.
        barber = {**self.PLUMBER, "category": "barber", "display_name": "Mike the Plumber"}
        out, _, adj = self._run(self._draft(name="Mike the Plumber"), [barber], verdict=False)
        adj.assert_called_once()
        self.assertEqual(out, "sub-new")

    def test_no_candidates_creates_its_own_subject(self):
        out, rpc, adj = self._run(self._draft(), [])
        self.assertEqual(out, "sub-new")
        adj.assert_not_called()
        self.assertIsNone(_call_of(rpc, "set_signal_subject")["p_google_place_id"])

    def test_best_candidate_wins_not_the_first(self):
        weak = {**self.PLUMBER, "id": "sub-weak", "display_name": "Mike's Landscaping",
                "subject_key": "mikes landscaping", "category": "landscaping"}
        out, rpc, _ = self._run(
            self._draft(name="Mike the Plumber"), [weak, self.PLUMBER],
        )
        self.assertEqual(out, "sub-mike")
        self.assertEqual(_call_of(rpc, "attach_signal_subject")["p_subject_id"], "sub-mike")

    def test_two_banana_breads_share_one_subject(self):
        # The change the standup asked for: the SUBJECT merges, the artifacts do not.
        bread = {
            "id": "sub-bread", "subject_key": "banana bread",
            "display_name": "Banana bread", "category": "baking",
            "locality": None, "merge_mode": "collection", "signal_count": 1,
        }
        with patch("app.places.search_places") as search, \
             patch("app.reco_subject._adjudicate") as adj, \
             patch("app.supabase_rpc.call_rpc", side_effect=_rpc(candidates=[bread])) as rpc:
            out = mod.ground_reco_subject(
                "jwt", signal_id="sig-r",
                draft=self._draft(reco_type="recipe", name="Banana bread", category="baking"),
            )
        self.assertEqual(out, "sub-bread")
        search.assert_not_called()
        adj.assert_not_called()          # identical names: no model call needed
        self.assertEqual(_call_of(rpc, "attach_signal_subject")["p_method"], "blocked")
        # Candidates were asked for collections only — a plumber is not a banana bread.
        self.assertEqual(
            _call_of(rpc, "reco_subject_candidates")["p_merge_mode"], "collection"
        )

    def test_a_word_collision_does_not_merge_a_recipe_into_a_business(self):
        # The blocker is recall-biased: "Bread Street Plumbing" and "Banana bread" share
        # the word "bread", so the shortlist legitimately contains it. The SCORER is what
        # refuses (0.303, far below ADJUDICATE_FLOOR) — verified, not assumed.
        bread = {
            "id": "sub-bread", "subject_key": "banana bread",
            "display_name": "Banana bread", "category": "baking",
            "locality": None, "merge_mode": "collection", "signal_count": 1,
        }
        self.assertLess(
            mod.score_candidate("Bread Street Plumbing", "plumber", bread),
            mod.ADJUDICATE_FLOOR,
        )


class TestBands(unittest.TestCase):
    """The thresholds are the whole design, so pin what actually lands in each band.

    Written from measured scores, not guessed ones — the first draft of the tests above
    used a pair that reads as 0.69 while asserting it would be adjudicated.
    """

    BASE = "Mike the Plumber"

    def _band(self, name):
        score = mod.name_match_score(name, self.BASE)
        if score >= mod.AUTO_MERGE_FLOOR:
            return "auto"
        return "adjudicate" if score >= mod.ADJUDICATE_FLOOR else "below"

    def test_trivial_variants_merge_without_a_model(self):
        for name in ("Mike The Plumber", "Mike the Plumber LLC", "Mike the Plumber Inc"):
            with self.subTest(name=name):
                self.assertEqual(self._band(name), "auto")

    def test_a_typo_still_merges(self):
        self.assertEqual(self._band("Mike the Plumbr"), "auto")

    def test_close_but_arguable_goes_to_the_model(self):
        for name in ("Mike Plumber", "Mike the Plumbers", "Mike the Plumbing"):
            with self.subTest(name=name):
                self.assertEqual(self._band(name), "adjudicate")

    def test_unrelated_never_reaches_the_model(self):
        for name in ("Chef Ana meal prep", "Mikes Landscaping"):
            with self.subTest(name=name):
                self.assertEqual(self._band(name), "below")

    def test_known_recall_gap_is_a_deliberate_miss(self):
        # "Mike Plumbing" reads 0.69 and will get its OWN subject, though a human would
        # likely call it the same plumber. Recorded, not hidden: the conservative failure
        # is two rows where there might be one, and that is the cheaper mistake. A real
        # corpus is what should move the floor, not intuition.
        self.assertEqual(self._band("Mike Plumbing"), "below")


class TestCarouselPick(unittest.TestCase):
    """The carousel's map search: the user tapped an actual Google result, and the client
    sent its id. Certain — so it must outrank the chip match and the search, and must not
    trigger a search at all."""

    def _draft(self, **over):
        draft = {
            "name": "Dr. Sarah Chen",
            "reco_type": "professional",
            "category": "pediatric dentist",
            "locality": "Lake Nona",
            # A clinic is walked into; the extractor reads it as a place, and grounding
            # now honours that rather than searching for every professional.
            "place_based": True,
        }
        draft.update(over)
        return draft

    def test_picked_id_is_used_and_no_search_runs(self):
        with patch("app.places.search_places") as search, \
             patch("app.supabase_rpc.call_rpc", return_value="sub-p") as rpc:
            out = mod.ground_reco_subject(
                "jwt", signal_id="sig-p",
                draft=self._draft(subject_google_place_id="ChIJ_picked"),
            )
        self.assertEqual(out, "sub-p")
        search.assert_not_called()
        self.assertEqual(_call_of(rpc, "set_signal_subject")["p_google_place_id"], "ChIJ_picked")

    def test_pick_outranks_a_disagreeing_search(self):
        # The search would have found a different practice; the tap is not a guess.
        other = [{"name": "Dr. Sarah Chen, DDS", "place_id": "ChIJ_searched", "lat": 1, "lng": 2}]
        with patch("app.places.search_places", return_value=other), \
             patch("app.supabase_rpc.call_rpc", return_value="sub-p") as rpc:
            mod.ground_reco_subject(
                "jwt", signal_id="sig-p2",
                draft=self._draft(subject_google_place_id="ChIJ_picked"),
            )
        self.assertEqual(_call_of(rpc, "set_signal_subject")["p_google_place_id"], "ChIJ_picked")

    def test_pick_outranks_the_chat_chip(self):
        chips = [{"name": "Dr. Sarah Chen", "place_id": "ChIJ_chip", "lat": 9, "lng": 9}]
        with patch("app.supabase_rpc.call_rpc", return_value="sub-p") as rpc:
            mod.ground_reco_subject(
                "jwt", signal_id="sig-p3",
                draft=self._draft(
                    subject_google_place_id="ChIJ_picked", subject_place_options=chips,
                ),
            )
        self.assertEqual(_call_of(rpc, "set_signal_subject")["p_google_place_id"], "ChIJ_picked")

    def test_blank_pick_falls_through_to_the_search(self):
        hit = [{"name": "Dr. Sarah Chen, DDS", "place_id": "ChIJ_searched", "lat": 1, "lng": 2}]
        with patch("app.places.search_places", return_value=hit), \
             patch("app.supabase_rpc.call_rpc", return_value="sub-s") as rpc:
            mod.ground_reco_subject(
                "jwt", signal_id="sig-p4", draft=self._draft(subject_google_place_id="   "),
            )
        self.assertEqual(_call_of(rpc, "set_signal_subject")["p_google_place_id"], "ChIJ_searched")

    def test_a_recipe_pick_is_still_ignored(self):
        # A recipe is not a point on a map, so a tapped place is not evidence about it —
        # grounding "banana bread" to whichever bakery she happened to tap would attribute
        # her recipe to a business nobody named.
        with patch("app.supabase_rpc.call_rpc", side_effect=_rpc(candidates=[])) as rpc:
            out = mod.ground_reco_subject(
                "jwt", signal_id="sig-p5",
                draft=self._draft(
                    reco_type="recipe", name="Banana bread",
                    subject_google_place_id="ChIJ_picked",
                ),
            )
        self.assertEqual(out, "sub-new")
        created = _call_of(rpc, "set_signal_subject")
        self.assertIsNone(created["p_google_place_id"])
        self.assertEqual(created["p_merge_mode"], "collection")


class TestAmbiguousSurvives(unittest.TestCase):
    """Regression: the refusal must outlive the subject creation that follows it.

    Found by scripts/eval_reco_resolution.py on a real stack, not here — the mocks passed
    while the database read back method='new' with the near-miss gone, because
    set_signal_subject stamps method/candidate unconditionally and ran AFTER the marking.
    An ordering bug is invisible to a test that only asserts each call happened.
    """

    CANDIDATE = {
        "id": "sub-plumber", "subject_key": "mike the plumber",
        "display_name": "Mike the Plumber", "category": "plumber",
        "locality": "Lake Nona", "merge_mode": "aggregate", "signal_count": 2,
    }

    def _run(self, verdict):
        calls: list[str] = []

        def rpc(_jwt, fn, args):
            calls.append(fn)
            if fn == "reco_subject_candidates":
                return [self.CANDIDATE]
            if fn == "set_signal_subject":
                return "sub-own"
            return True

        with patch("app.places.search_places", return_value=[]), \
             patch("app.reco_subject._adjudicate", return_value=verdict), \
             patch("app.supabase_rpc.call_rpc", side_effect=rpc):
            out = mod.ground_reco_subject(
                "jwt", signal_id="sig-amb",
                draft={"name": "Mike the Plumber", "reco_type": "service",
                       "category": "barber", "locality": "Lake Nona"},
            )
        return out, calls

    def test_the_mark_comes_after_the_create(self):
        out, calls = self._run(False)
        self.assertEqual(out, "sub-own")
        self.assertIn("mark_signal_subject_ambiguous", calls)
        self.assertLess(
            calls.index("set_signal_subject"),
            calls.index("mark_signal_subject_ambiguous"),
            f"refusal recorded before the create that erases it: {calls}",
        )

    def test_unsure_is_recorded_the_same_way(self):
        _, calls = self._run(None)
        self.assertLess(calls.index("set_signal_subject"),
                        calls.index("mark_signal_subject_ambiguous"))

    def test_nothing_close_records_no_near_miss(self):
        # Below the floor there is no candidate worth remembering.
        with patch("app.places.search_places", return_value=[]), \
             patch("app.supabase_rpc.call_rpc", side_effect=_rpc(candidates=[])) as rpc:
            mod.ground_reco_subject(
                "jwt", signal_id="sig-far",
                draft={"name": "Chef Ana meal prep", "reco_type": "service",
                       "category": "catering", "locality": "Lake Nona"},
            )
        self.assertNotIn("mark_signal_subject_ambiguous",
                         [c[0][1] for c in rpc.call_args_list])


class TestPlaceBasedIsHonoured(unittest.TestCase):
    """Grounding must not re-litigate whether the subject is a place.

    The capture flow already decided: subject_is_place() gives a restaurant the Places
    picker always, a professional/service only when the extractor read a storefront. This
    module used to ignore that and search anyway — and a search ALWAYS finds something, so
    "Mike Plumber" was attached to a real plumbing company the neighbour never named, and
    then could not merge with "Mike the Plumber", who had no listing. Found by
    scripts/eval_reco_resolution.py on a real stack.
    """

    def _draft(self, **over):
        d = {"name": "Mike the Plumber", "reco_type": "service", "category": "plumber",
             "locality": "Lake Nona", "place_based": False}
        d.update(over)
        return d

    def test_a_storefrontless_service_never_hits_google(self):
        with patch("app.places.search_places") as search, \
             patch("app.supabase_rpc.call_rpc", side_effect=_rpc(candidates=[])) as rpc:
            mod.ground_reco_subject("jwt", signal_id="s1", draft=self._draft())
        search.assert_not_called()
        self.assertIsNone(_call_of(rpc, "set_signal_subject")["p_google_place_id"])

    def test_a_storefront_service_does(self):
        hit = [{"name": "Nona Barbers", "place_id": "ChIJ_shop", "lat": 1, "lng": 2}]
        with patch("app.places.search_places", return_value=hit) as search, \
             patch("app.supabase_rpc.call_rpc", side_effect=_rpc(candidates=[])) as rpc:
            mod.ground_reco_subject(
                "jwt", signal_id="s2",
                draft=self._draft(name="Nona Barbers", category="barber", place_based=True),
            )
        search.assert_called_once()
        self.assertEqual(_call_of(rpc, "set_signal_subject")["p_google_place_id"], "ChIJ_shop")

    def test_a_restaurant_grounds_even_without_the_flag(self):
        # _PLACE_SUBJECT_TYPES: a restaurant's subject IS a map point, flag or no flag.
        hit = [{"name": "Boxi Park", "place_id": "ChIJ_boxi", "lat": 1, "lng": 2}]
        with patch("app.places.search_places", return_value=hit) as search, \
             patch("app.supabase_rpc.call_rpc", side_effect=_rpc(candidates=[])):
            mod.ground_reco_subject(
                "jwt", signal_id="s3",
                draft=self._draft(name="Boxi Park", reco_type="restaurant",
                                  category="food hall", place_based=False),
            )
        search.assert_called_once()

    def test_two_phrasings_of_one_storefrontless_provider_can_meet(self):
        # The regression in one assertion: both must reach the identity space, where they
        # can find each other, rather than one being captured by a Google listing.
        seen = []
        with patch("app.places.search_places") as search, \
             patch("app.supabase_rpc.call_rpc",
                   side_effect=lambda j, fn, a: seen.append(fn) or (
                       [] if fn == "reco_subject_candidates" else "sub-x")):
            for nm in ("Mike the Plumber", "Mike Plumber"):
                mod.ground_reco_subject("jwt", signal_id=f"s-{nm}", draft=self._draft(name=nm))
        search.assert_not_called()
        self.assertEqual(seen.count("reco_subject_candidates"), 2)


class TestAdjudicationCache(unittest.TestCase):
    """The model is not deterministic; the product must be.

    Measured on a real stack: one identical pair at an identical score merged on 2 of 5
    runs. Since the verdict is persisted into subject_ref at capture, an uncached
    adjudicator means two neighbours share a card by luck.
    """

    CAND = {"id": "sub-x", "subject_key": "mike the plumber",
            "display_name": "Mike the Plumber", "category": "plumber",
            "locality": "Lake Nona", "merge_mode": "aggregate", "signal_count": 1}

    def test_the_question_is_symmetric(self):
        self.assertEqual(
            mod.pair_signature("Mike the Plumber", "plumber", "Mike Plumber", "plumber"),
            mod.pair_signature("Mike Plumber", "plumber", "Mike the Plumber", "plumber"),
        )

    def test_category_is_part_of_the_question(self):
        self.assertNotEqual(
            mod.pair_signature("Mike the Plumber", "plumber", "Mike the Plumber", "plumber"),
            mod.pair_signature("Mike the Plumber", "plumber", "Mike the Plumber", "barber"),
        )

    def test_a_hit_never_asks_the_model(self):
        with patch.object(mod, "_cached_verdict", return_value=(True, False)), \
             patch.object(mod, "_ask_model") as ask:
            self.assertIs(mod._adjudicate("Mike Plumber", "plumber", "Lake Nona", self.CAND), False)
        ask.assert_not_called()

    def test_a_miss_asks_once_and_stores(self):
        with patch.object(mod, "_cached_verdict", return_value=(False, None)), \
             patch.object(mod, "_ask_model", return_value=True) as ask, \
             patch.object(mod, "_store_verdict") as store:
            self.assertIs(mod._adjudicate("Mike Plumber", "plumber", "Lake Nona", self.CAND), True)
        ask.assert_called_once()
        store.assert_called_once()

    def test_unsure_is_cached_like_any_other_answer(self):
        # Re-asking a question the model could not answer is how a refusal becomes a merge
        # on the third attempt.
        with patch.object(mod, "_cached_verdict", return_value=(False, None)), \
             patch.object(mod, "_ask_model", return_value=None), \
             patch.object(mod, "_store_verdict") as store:
            mod._adjudicate("Mike Plumber", "plumber", "Lake Nona", self.CAND)
        self.assertIsNone(store.call_args[0][1])

    def test_a_cached_none_is_not_mistaken_for_a_miss(self):
        with patch.object(mod, "_cached_verdict", return_value=(True, None)), \
             patch.object(mod, "_ask_model") as ask:
            self.assertIsNone(mod._adjudicate("Mike Plumber", "plumber", "Lake Nona", self.CAND))
        ask.assert_not_called()
