import unittest
from unittest.mock import patch

import app.reco_cards as mod
import app.reco_cluster as cluster


def tip(**over):
    """One find_neighbor_tips v8 row."""
    row = {
        "signal_id": "sig-1",
        "peer_user_id": "peer-1",
        "neighbor_label": "coral88",
        "avatar_url": None,
        "detail_text": "Dr. Sarah · pediatric dentist · gentle",
        "reco_name": "Dr. Sarah Chen",
        "reco_description": "so gentle with the toddlers",
        "reco_fields": [],
        "reco_type": "professional",
        "category": "pediatric dentist",
        "match_strength": 0.8,
        "distance_text": "0.4 mi away",
        "shared_circles": [],
        "same_block": False,
        "helpful_count": 0,
        "created_at": "2026-09-01T10:00:00+00:00",
        "subject_ref": "sub-sarah",
        "subject_name": "Dr. Sarah Chen, DDS",
        "subject_category": "pediatric dentist",
        "subject_locality": "Lake Nona",
        "subject_distance_text": "0.2 mi",
        "subject_merge_mode": "aggregate",
        "subject_vouch_count": 3,
        "i_contributed": False,
    }
    row.update(over)
    return row


class TestGrouping(unittest.TestCase):
    def test_three_tips_one_subject_become_one_card(self):
        rows = [
            tip(signal_id="s1", peer_user_id="p1"),
            tip(signal_id="s2", peer_user_id="p2", neighbor_label="sunny"),
            tip(signal_id="s3", peer_user_id="p3", neighbor_label="maple"),
        ]
        cards = mod.subject_cards_from_tips(rows)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["vouch_count"], 3)
        self.assertEqual(len(cards[0]["contributors"]), 3)
        self.assertEqual(cards[0]["title"], "Dr. Sarah Chen, DDS")

    def test_different_subjects_stay_apart(self):
        rows = [tip(), tip(signal_id="s2", peer_user_id="p2", subject_ref="sub-ahmed",
                           subject_name="Dr. Ahmed", subject_vouch_count=1)]
        self.assertEqual(len(mod.subject_cards_from_tips(rows)), 2)

    def test_ungrounded_rows_never_share_a_bucket(self):
        # Two tips we could not resolve are not thereby the same thing.
        rows = [
            tip(signal_id="s1", peer_user_id="p1", subject_ref=None, subject_name=None),
            tip(signal_id="s2", peer_user_id="p2", subject_ref=None, subject_name=None),
        ]
        cards = mod.subject_cards_from_tips(rows)
        self.assertEqual(len(cards), 2)
        self.assertTrue(all(c["vouch_count"] == 1 for c in cards))
        self.assertTrue(all(c["title"] == "Dr. Sarah Chen" for c in cards))

    def test_a_contributor_without_an_author_is_dropped(self):
        self.assertEqual(mod.subject_cards_from_tips([tip(peer_user_id="")]), [])

    def test_count_survives_a_partial_page(self):
        # SQL says 3 exist; this page shows 1. The card must not say 1.
        cards = mod.subject_cards_from_tips([tip(subject_vouch_count=3)])
        self.assertEqual(cards[0]["vouch_count"], 3)

    def test_count_never_undershoots_the_voices_on_screen(self):
        # A stale or zero count must not contradict rows the reader can see.
        rows = [tip(signal_id="s1", peer_user_id="p1", subject_vouch_count=0),
                tip(signal_id="s2", peer_user_id="p2", subject_vouch_count=0)]
        self.assertEqual(mod.subject_cards_from_tips(rows)[0]["vouch_count"], 2)


class TestHonestClaims(unittest.TestCase):
    def test_subject_distance_wins_and_is_flagged(self):
        card = mod.subject_cards_from_tips([tip()])[0]
        self.assertEqual(card["distance_text"], "0.2 mi")
        self.assertTrue(card["distance_is_subject"])

    def test_falls_back_to_the_recommenders_distance_when_ungrounded(self):
        card = mod.subject_cards_from_tips([tip(subject_distance_text=None)])[0]
        self.assertEqual(card["distance_text"], "0.4 mi away")
        self.assertFalse(card["distance_is_subject"])

    def test_i_contributed_is_carried(self):
        card = mod.subject_cards_from_tips([tip(i_contributed=True)])[0]
        self.assertTrue(card["i_contributed"])

    def test_best_provenance_wins_not_the_first_row(self):
        # One contributor is only nearby, another shares a circle: the card belongs under
        # the circle heading.
        rows = [
            tip(signal_id="s1", peer_user_id="p1"),
            tip(signal_id="s2", peer_user_id="p2",
                shared_circles=[{"place_id": "pl-mary", "name": "St Mary's Church"}]),
        ]
        card = mod.subject_cards_from_tips(rows)[0]
        self.assertEqual(card["group_kind"], "circle")
        self.assertEqual(card["group_label"], "St Mary's Church")

    def test_strength_is_the_max_not_the_mean(self):
        # Extra voices must never dilute a subject's rank.
        rows = [tip(signal_id="s1", peer_user_id="p1", match_strength=0.9),
                tip(signal_id="s2", peer_user_id="p2", match_strength=0.1)]
        self.assertEqual(mod.subject_cards_from_tips(rows)[0]["match_strength"], 0.9)

    def test_ordering_is_deterministic(self):
        rows = [
            tip(signal_id="s1", peer_user_id="p1", subject_ref="a", subject_name="Aaa"),
            tip(signal_id="s2", peer_user_id="p2", subject_ref="b", subject_name="Bbb"),
            tip(signal_id="s3", peer_user_id="p3", subject_ref="c", subject_name="Ccc"),
        ]
        first = [c["title"] for c in mod.subject_cards_from_tips(rows)]
        for _ in range(5):
            self.assertEqual([c["title"] for c in mod.subject_cards_from_tips(rows)], first)


class TestDigestPolicy(unittest.TestCase):
    """The rails around the Pareto read."""

    def _rows(self, n=3, **over):
        return [
            tip(signal_id=f"s{i}", peer_user_id=f"p{i}",
                reco_description=f"thing number {i}", **over)
            for i in range(n)
        ]

    def test_the_list_never_composes_inside_the_turn(self):
        # Five cards must not be five model calls in the user's latency budget. The warm
        # thread may compose afterwards — that is the point — so it is stubbed out here to
        # isolate the SYNCHRONOUS path, which is the one the reader waits on.
        with patch("app.reco_cluster._compose") as compose, \
             patch("app.reco_cluster._cached", return_value=None), \
             patch("app.reco_cards._warm"):
            mod.subject_cards_from_tips(self._rows(), allow_compose=False)
        compose.assert_not_called()

    def test_the_list_still_uses_a_cached_digest(self):
        hit = {"themes": [{"label": "gentle", "n": 3, "total": 3, "signal_ids": ["s0"],
                           "quote": None}], "total": 3}
        with patch("app.reco_cluster._cached", return_value=hit), \
             patch("app.reco_cluster._compose") as compose:
            cards = mod.subject_cards_from_tips(self._rows(), allow_compose=False)
        compose.assert_not_called()
        self.assertEqual(cards[0]["themes"][0]["label"], "gentle")

    def test_the_detail_view_composes(self):
        raw = [{"label": "gentle with kids", "ids": ["s0", "s1"], "quote": "thing number 0"}]
        with patch("app.reco_cluster._cached", return_value=None), \
             patch("app.reco_cluster._compose", return_value=raw) as compose, \
             patch("app.reco_cluster._store"):
            cards = mod.subject_cards_from_tips(self._rows(), allow_compose=True)
        compose.assert_called_once()
        self.assertEqual(cards[0]["themes"][0]["n"], 2)
        self.assertEqual(cards[0]["themes"][0]["total"], 3)

    def test_a_collection_is_never_clustered(self):
        with patch("app.reco_cluster._cached") as cached, \
             patch("app.reco_cluster._compose") as compose:
            cards = mod.subject_cards_from_tips(
                self._rows(subject_merge_mode="collection", reco_type="recipe"),
                allow_compose=True,
            )
        compose.assert_not_called()
        cached.assert_not_called()
        self.assertIsNone(cards[0]["themes"])
        # It still merges: three banana breads, one subject, three standalone voices.
        self.assertEqual(cards[0]["vouch_count"], 3)
        self.assertEqual(len(cards[0]["contributors"]), 3)

    def test_a_single_voice_is_not_clustered(self):
        with patch("app.reco_cluster._compose") as compose:
            cards = mod.subject_cards_from_tips(self._rows(1), allow_compose=True)
        compose.assert_not_called()
        self.assertIsNone(cards[0]["themes"])

    def test_a_failing_digest_still_yields_a_card(self):
        with patch("app.reco_cluster._cached", return_value=None), \
             patch("app.reco_cluster._compose", side_effect=RuntimeError("model down")), \
             patch("app.reco_cluster._store"):
            cards = mod.subject_cards_from_tips(self._rows(), allow_compose=True)
        self.assertEqual(len(cards), 1)
        self.assertIsNone(cards[0]["themes"])
        self.assertEqual(len(cards[0]["contributors"]), 3)


class TestThemeGrounding(unittest.TestCase):
    """A theme's count and its quote are the whole claim, so both are checked."""

    BY_ID = {"a": "she let my two-year-old hold the mirror, zero tears",
             "b": "huge car park, never had to circle"}

    def test_invented_ids_are_dropped(self):
        themes = cluster._clean_themes(
            [{"label": "gentle", "ids": ["a", "ghost"], "quote": None}], self.BY_ID, 2)
        self.assertEqual(themes[0]["n"], 1)

    def test_a_theme_with_no_real_ids_is_dropped_entirely(self):
        self.assertEqual(
            cluster._clean_themes([{"label": "x", "ids": ["ghost"], "quote": None}], self.BY_ID, 2),
            [],
        )

    def test_an_invented_quote_is_dropped_but_the_theme_survives(self):
        themes = cluster._clean_themes(
            [{"label": "gentle", "ids": ["a"], "quote": "she is simply wonderful"}],
            self.BY_ID, 2)
        self.assertEqual(themes[0]["n"], 1)
        self.assertIsNone(themes[0]["quote"])

    def test_a_real_quote_is_kept(self):
        themes = cluster._clean_themes(
            [{"label": "gentle", "ids": ["a"], "quote": "hold the mirror, zero tears"}],
            self.BY_ID, 2)
        self.assertEqual(themes[0]["quote"], "hold the mirror, zero tears")

    def test_a_quote_from_another_contribution_is_not_this_themes_evidence(self):
        themes = cluster._clean_themes(
            [{"label": "gentle", "ids": ["a"], "quote": "huge car park"}], self.BY_ID, 2)
        self.assertIsNone(themes[0]["quote"])

    def test_themes_are_ordered_by_count_then_stably(self):
        raw = [
            {"label": "zeta", "ids": ["a"], "quote": None},
            {"label": "alpha", "ids": ["a"], "quote": None},
            {"label": "big", "ids": ["a", "b"], "quote": None},
        ]
        got = [t["label"] for t in cluster._clean_themes(raw, self.BY_ID, 2)]
        self.assertEqual(got, ["big", "alpha", "zeta"])

    def test_duplicate_labels_collapse(self):
        raw = [{"label": "gentle", "ids": ["a"], "quote": None},
               {"label": "Gentle", "ids": ["b"], "quote": None}]
        self.assertEqual(len(cluster._clean_themes(raw, self.BY_ID, 2)), 1)

    def test_basis_sig_changes_when_an_author_edits(self):
        a = [{"signal_id": "s1", "text": "gentle"}]
        b = [{"signal_id": "s1", "text": "gentle and quick"}]
        self.assertNotEqual(cluster.basis_sig(a), cluster.basis_sig(b))

    def test_basis_sig_is_order_independent(self):
        a = [{"signal_id": "s1", "text": "x"}, {"signal_id": "s2", "text": "y"}]
        self.assertEqual(cluster.basis_sig(a), cluster.basis_sig(list(reversed(a))))


if __name__ == "__main__":
    unittest.main()


class TestSurfaceWiring(unittest.TestCase):
    """The new surface rides ALONGSIDE peer_matches, never instead of it."""

    def _tips(self):
        return [tip(signal_id="s1", peer_user_id="p1"),
                tip(signal_id="s2", peer_user_id="p2", neighbor_label="sunny")]

    def _stamp(self, env):
        import app.tip_rec_cascade as trc
        ctx: dict = {}
        with patch.dict("os.environ", env, clear=False), \
             patch("app.reco_cluster._cached", return_value=None), \
             patch("app.reco_cards._warm"), \
             patch("app.reco_cluster._compose") as compose:
            trc.stamp_tip_peer_surface(ctx, self._tips(), phone_verified=True)
        return ctx, compose

    def test_off_by_default(self):
        ctx, _ = self._stamp({"LANA_RECO_CARDS": "0"})
        self.assertNotIn("reco_cards", ctx)
        # The existing surface is untouched either way — that is the whole point.
        self.assertEqual(len(ctx["peer_matches"]), 2)

    def test_on_adds_cards_without_removing_peer_matches(self):
        ctx, _ = self._stamp({"LANA_RECO_CARDS": "1"})
        self.assertEqual(len(ctx["peer_matches"]), 2)
        self.assertEqual(len(ctx["reco_cards"]), 1)
        self.assertEqual(ctx["reco_cards"][0]["vouch_count"], 3)

    def test_the_results_turn_never_calls_the_model(self):
        # Synchronously, in the turn. Warming happens off-thread and is stubbed in _stamp.
        _, compose = self._stamp({"LANA_RECO_CARDS": "1"})
        compose.assert_not_called()

    def test_a_broken_card_build_never_costs_the_answer(self):
        import app.tip_rec_cascade as trc
        ctx: dict = {}
        with patch.dict("os.environ", {"LANA_RECO_CARDS": "1"}, clear=False), \
             patch("app.reco_cards.subject_cards_from_tips", side_effect=RuntimeError("boom")):
            shown = trc.stamp_tip_peer_surface(ctx, self._tips(), phone_verified=True)
        self.assertEqual(len(shown), 2)
        self.assertEqual(len(ctx["peer_matches"]), 2)
        self.assertNotIn("reco_cards", ctx)


class TestDigestWarming(unittest.TestCase):
    """A cache miss on a list render is composed off the turn, never in it."""

    def _rows(self, n=3):
        return [tip(signal_id=f"s{i}", peer_user_id=f"p{i}", reco_description=f"thing {i}")
                for i in range(n)]

    def test_a_list_miss_schedules_a_warm(self):
        with patch("app.reco_cluster._cached", return_value=None), \
             patch("app.reco_cards._warm") as warm:
            mod.subject_cards_from_tips(self._rows(), allow_compose=False)
        warm.assert_called_once()
        pending = warm.call_args[0][0]
        self.assertEqual(pending[0][0], "sub-sarah")

    def test_a_cache_hit_schedules_nothing(self):
        hit = {"themes": [{"label": "x", "n": 2, "total": 3, "signal_ids": ["s0"], "quote": None}],
               "total": 3}
        with patch("app.reco_cluster._cached", return_value=hit), \
             patch("app.reco_cards._warm") as warm:
            mod.subject_cards_from_tips(self._rows(), allow_compose=False)
        warm.assert_not_called()

    def test_the_detail_view_composes_inline_and_does_not_warm(self):
        raw = [{"label": "a theme", "ids": ["s0"], "quote": None}]
        with patch("app.reco_cluster._cached", return_value=None), \
             patch("app.reco_cluster._compose", return_value=raw), \
             patch("app.reco_cluster._store"), \
             patch("app.reco_cards._warm") as warm:
            mod.subject_cards_from_tips(self._rows(), allow_compose=True)
        warm.assert_not_called()

    def test_warming_is_bounded(self):
        # A widened fetch must not put a dozen model calls behind one question.
        rows = []
        for sub in range(6):
            rows += [tip(signal_id=f"s{sub}-{i}", peer_user_id=f"p{sub}-{i}",
                         subject_ref=f"sub-{sub}", subject_name=f"Subject {sub}",
                         reco_description=f"thing {sub}-{i}") for i in range(2)]
        composed: list = []
        with patch("app.reco_cluster._cached", return_value=None), \
             patch("app.reco_cluster._compose", side_effect=lambda c, l: composed.append(1) or None), \
             patch("app.reco_cluster._store"):
            mod.subject_cards_from_tips(rows, allow_compose=False)
            import time; time.sleep(0.3)
        self.assertLessEqual(len(composed), mod._MAX_WARM)

    def test_a_failing_warm_is_silent(self):
        with patch("app.reco_cluster._cached", return_value=None), \
             patch("app.reco_cluster.digest_for_subject", side_effect=RuntimeError("boom")):
            cards = mod.subject_cards_from_tips(self._rows(), allow_compose=False)
            import time; time.sleep(0.2)
        self.assertEqual(len(cards), 1)


class TestReachesTheWire(unittest.TestCase):
    """Stamping a surface onto ctx is not shipping it.

    The response is built from an explicit allowlist of fields, not from ctx — so
    `ctx["reco_cards"]` was written and silently dropped for the whole of Stage 3, and
    every test passed because they all stopped at the stamp. These assert the last hop.
    """

    def _card(self, **over):
        c = {"title": "Dr. Sarah Chen", "subject_ref": "sub-1", "vouch_count": 3,
             "merge_mode": "aggregate", "contributors": [], "themes": None}
        c.update(over)
        return c

    def test_the_response_model_carries_the_field(self):
        from app.models import CreateSessionResponse, SendMessageResponse
        for model in (SendMessageResponse, CreateSessionResponse):
            with self.subTest(model=model.__name__):
                self.assertIn("reco_cards", model.model_fields)

    def test_cards_survive_serialisation(self):
        from app.main import _reco_cards_from_ctx
        rows = _reco_cards_from_ctx({"reco_cards": [self._card()]})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].title, "Dr. Sarah Chen")
        self.assertEqual(rows[0].vouch_count, 3)

    def test_a_malformed_card_is_dropped_not_fatal(self):
        # One bad card must never cost the reader the whole answer.
        from app.main import _reco_cards_from_ctx
        rows = _reco_cards_from_ctx({"reco_cards": [
            self._card(), {"no_title": True}, "not a dict",
            self._card(title="  ", subject_ref="sub-2"),
        ]})
        self.assertEqual([r.title for r in rows], ["Dr. Sarah Chen"])

    def test_absent_ctx_is_empty_not_an_error(self):
        from app.main import _reco_cards_from_ctx
        self.assertEqual(_reco_cards_from_ctx({}), [])
        self.assertEqual(_reco_cards_from_ctx({"reco_cards": "nope"}), [])

    def test_themes_and_contributors_round_trip(self):
        from app.main import _reco_cards_from_ctx
        card = self._card(
            contributors=[{"signal_id": "s1", "peer_user_id": "p1", "nickname": "maple",
                           "description": "so gentle"}],
            themes=[{"label": "gentle with kids", "n": 2, "total": 3,
                     "quote": "so gentle", "signal_ids": ["s1"]}],
        )
        row = _reco_cards_from_ctx({"reco_cards": [card]})[0]
        self.assertEqual(row.contributors[0].nickname, "maple")
        self.assertEqual(row.themes[0].n, 2)
        self.assertEqual(row.themes[0].total, 3)
        # The count is the claim — it must survive the hop intact.
        self.assertEqual(row.model_dump()["themes"][0]["quote"], "so gentle")


class TestCohorts(unittest.TestCase):
    """"4 of these 8 have toddlers, like you" — proven overlap, never a claim about the
    subject, and never built from a private claim."""

    def _rows(self, n=3):
        return [tip(signal_id=f"s{i}", peer_user_id=f"p{i}", reco_description=f"said {i}")
                for i in range(n)]

    def _claims(self, mapping):
        return lambda uid: mapping.get(uid, {})

    def test_a_shared_claim_across_several_becomes_a_cohort(self):
        claims = {"reader": {"c-tod": "Toddler parent"},
                  "p0": {"c-tod": "Has a 2-year-old"},
                  "p1": {"c-tod": "Mum to a toddler"},
                  "p2": {}}
        with patch("app.reco_cohort._claims_by_concept", side_effect=self._claims(claims)), \
             patch("app.reco_cluster._cached", return_value=None), \
             patch("app.reco_cards._warm"):
            card = mod.subject_cards_from_tips(self._rows(), reader_id="reader")[0]
        self.assertEqual(len(card["cohorts"]), 1)
        self.assertEqual(card["cohorts"][0]["n"], 2)
        self.assertEqual(card["cohorts"][0]["total"], 3)
        # The READER's own wording, not a stranger's phrasing of something personal.
        self.assertEqual(card["cohorts"][0]["label"], "Toddler parent")

    def test_one_match_is_not_a_cohort(self):
        # One person sharing a claim is that person — rendering "1 of these 8" invites a
        # reader to work out who.
        claims = {"reader": {"c-tod": "Toddler parent"}, "p0": {"c-tod": "toddler"}}
        with patch("app.reco_cohort._claims_by_concept", side_effect=self._claims(claims)), \
             patch("app.reco_cluster._cached", return_value=None), \
             patch("app.reco_cards._warm"):
            card = mod.subject_cards_from_tips(self._rows(), reader_id="reader")[0]
        self.assertEqual(card["cohorts"], [])

    def test_no_reader_means_no_cohort(self):
        with patch("app.reco_cluster._cached", return_value=None), \
             patch("app.reco_cards._warm"):
            card = mod.subject_cards_from_tips(self._rows())[0]
        self.assertEqual(card["cohorts"], [])

    def test_a_theme_carries_its_own_narrowed_cohort(self):
        claims = {"reader": {"c-tod": "Toddler parent"},
                  "p0": {"c-tod": "x"}, "p1": {"c-tod": "y"}, "p2": {"c-tod": "z"}}
        digest = {"themes": [{"label": "gentle", "n": 2, "total": 3,
                              "signal_ids": ["s0", "s1"], "quote": None}], "total": 3}
        with patch("app.reco_cohort._claims_by_concept", side_effect=self._claims(claims)), \
             patch("app.reco_cluster._cached", return_value=digest), \
             patch("app.reco_cards._warm"):
            card = mod.subject_cards_from_tips(self._rows(), reader_id="reader")[0]
        theme = card["themes"][0]
        # Of the 2 who said it, both are like her — "2 of these 2", not "3 of these 3".
        self.assertEqual(theme["cohort"]["n"], 2)
        self.assertEqual(theme["cohort"]["total"], 2)

    def test_a_failing_cohort_never_costs_the_card(self):
        with patch("app.reco_cohort.cohorts_for", side_effect=RuntimeError("db down")), \
             patch("app.reco_cluster._cached", return_value=None), \
             patch("app.reco_cards._warm"):
            cards = mod.subject_cards_from_tips(self._rows(), reader_id="reader")
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["cohorts"], [])


class TestPrivateClaimsNeverLeak(unittest.TestCase):
    """A cohort is a public statement about identifiable people."""

    def test_only_public_claims_are_read(self):
        import app.reco_cohort as coh
        rows = [
            {"label": "Toddler parent", "concept": "c1", "disclosure": "public"},
            {"label": "In therapy", "concept": "c2", "disclosure": "private"},
            {"label": "No disclosure set", "concept": "c3", "disclosure": None},
        ]
        fake = type("T", (), {"data": rows})()
        chain = type("C", (), {
            "select": lambda s, *a: chain_obj, "eq": lambda s, *a: chain_obj,
            "is_": lambda s, *a: chain_obj, "limit": lambda s, *a: chain_obj,
            "execute": lambda s: fake,
        })()
        chain_obj = chain
        with patch("app.db.service_client",
                   return_value=type("S", (), {"table": lambda s, n: chain_obj})()):
            got = coh._claims_by_concept("u1")
        self.assertEqual(got, {"c1": "Toddler parent"})

    def test_fit_chips_are_the_asks_own_words(self):
        import app.tip_rec_cascade as trc
        chips = trc._ask_chips({"ask_draft": {"chips": [
            {"label": "pediatric dentist"}, {"label": "gentle"},
            {"label": "toddlers"}, {"label": "Lake Nona"}, {"label": "fifth"},
        ]}})
        self.assertEqual(chips, ["pediatric dentist", "gentle", "toddlers", "Lake Nona"])
        self.assertEqual(trc._ask_chips({}), [])
