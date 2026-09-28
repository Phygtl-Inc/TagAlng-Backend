import unittest
from unittest.mock import patch

import app.reco_body as body
import app.reco_synthesis as syn


class TestBodyFacts(unittest.TestCase):
    def test_description_first_then_answered_steps(self):
        got = body.facts_for({
            "description": "great prices and they deliver",
            "reco_fields": [
                {"label": "Delivery", "answer": "On time"},
                {"label": "Best for", "answer": "Families"},
                {"label": "Skipped", "answer": "   "},
            ],
        })
        self.assertEqual(got, ["great prices and they deliver",
                               "Delivery: On time", "Best for: Families"])

    def test_detail_text_is_never_a_fact(self):
        # It is a joined recap of everything else — feeding it in lets the model echo the
        # recap back as if it were prose.
        got = body.facts_for({"detail_text": "X · Y · Z", "description": "nice"})
        self.assertEqual(got, ["nice"])

    def test_basis_changes_when_an_answer_changes(self):
        a = body.facts_for({"description": "d", "reco_fields": [{"label": "L", "answer": "1"}]})
        b = body.facts_for({"description": "d", "reco_fields": [{"label": "L", "answer": "2"}]})
        self.assertNotEqual(body.basis_sig(a), body.basis_sig(b))


class TestBodyGrounding(unittest.TestCase):
    """A fluent paragraph that quietly gains a price reads exactly as true."""

    FACTS = ["great prices and they deliver", "Delivery: On time", "Best for: Families"]

    def test_an_invented_price_is_rejected(self):
        self.assertFalse(body._grounded("Bedroom sets from $499, delivered on time.", self.FACTS))

    def test_an_invented_time_is_rejected(self):
        self.assertFalse(body._grounded("Open until 9pm and they deliver.", self.FACTS))

    def test_prose_with_no_numbers_passes(self):
        self.assertTrue(body._grounded("Delivered on time, and a sensible stop for families.",
                                       self.FACTS))

    def test_a_number_that_IS_in_the_facts_passes(self):
        self.assertTrue(body._grounded("Around $60.", ["where to buy: Amazon, about $60"]))

    def test_a_thin_capture_composes_nothing(self):
        with patch.object(body, "_compose") as compose:
            self.assertIsNone(body.body_for("sig", {"description": "nice"}))
        compose.assert_not_called()

    def test_an_ungrounded_body_is_dropped_not_stored(self):
        with patch.object(body, "_cached", return_value=None), \
             patch.object(body, "_compose", return_value="Bedroom sets from $499."), \
             patch.object(body, "_store") as store:
            out = body.body_for("sig", {"description": "great prices",
                                        "reco_fields": [{"label": "Delivery", "answer": "On time"}]})
        self.assertIsNone(out)
        store.assert_not_called()

    def test_the_list_never_composes(self):
        with patch.object(body, "_cached", return_value=None), \
             patch.object(body, "_compose") as compose:
            body.body_for("sig", {"description": "great prices",
                                  "reco_fields": [{"label": "D", "answer": "On time"}]},
                          allow_compose=False)
        compose.assert_not_called()


def _contribs(n=6):
    return [{"signal_id": f"s{i}", "text": f"note {i}"} for i in range(n)]


class TestSynthesisRefusals(unittest.TestCase):
    """The line makes a counted claim about a disagreement between identifiable people.
    Every test here is about what it must REFUSE to say."""

    TEXTS = {
        "s0": "freezes well, no problem", "s1": "froze beautifully",
        "s2": "freezes fine", "s3": "it went watery, I froze it cooked",
        "s4": "watery after freezing, mine was frozen cooked", "s5": "lovely",
    }

    def _payload(self, **over):
        p = {
            "majority": {"label": "freezes well", "n": 3, "ids": ["s0", "s1", "s2"]},
            "minority": {"label": "went watery", "n": 2, "ids": ["s3", "s4"]},
            "shared_trait": "froze cooked",
            "line": "Three say it freezes well, two say it went watery — both froze it cooked.",
        }
        p.update(over)
        return p

    def _run(self, payload):
        contribs = [{"signal_id": k, "text": v} for k, v in self.TEXTS.items()]
        with patch("app.orchestrator.llm.llm_configured", return_value=True), \
             patch("app.orchestrator.llm.llm_json", return_value=payload), \
             patch("app.orchestrator.llm.composer_model", return_value="m"):
            return syn.synthesis_for(contribs)

    def test_the_happy_path_survives(self):
        got = self._run(self._payload())
        self.assertIsNotNone(got)
        self.assertEqual(got["majority"]["n"], 3)
        self.assertEqual(got["minority"]["n"], 2)
        self.assertEqual(got["shared_trait"], "froze cooked")

    def test_a_count_the_evidence_does_not_support_is_refused(self):
        # "n: 6" over three cited rows — the claim a reader cannot check.
        self.assertIsNone(self._run(self._payload(
            majority={"label": "freezes well", "n": 6, "ids": ["s0", "s1", "s2"]})))

    def test_a_number_in_the_prose_that_contradicts_the_evidence_is_refused(self):
        self.assertIsNone(self._run(self._payload(
            line="Six say it freezes well, two say it went watery — both froze it cooked.")))

    def test_invented_ids_are_refused(self):
        self.assertIsNone(self._run(self._payload(
            minority={"label": "watery", "n": 2, "ids": ["ghost1", "ghost2"]})))

    def test_a_single_dissenter_is_not_a_faction(self):
        self.assertIsNone(self._run(self._payload(
            minority={"label": "watery", "n": 1, "ids": ["s3"]},
            line="Three say it freezes well, one says it went watery.")))

    def test_overlapping_sides_are_refused(self):
        # One neighbour cannot be both the consensus and the dissent.
        self.assertIsNone(self._run(self._payload(
            minority={"label": "watery", "n": 2, "ids": ["s2", "s3"]})))

    def test_an_untraceable_shared_trait_is_refused(self):
        # Nobody in the minority mentioned a slow cooker.
        self.assertIsNone(self._run(self._payload(shared_trait="used a slow cooker")))

    def test_a_trait_only_ONE_dissenter_mentioned_is_refused(self):
        texts = dict(self.TEXTS, s4="watery after freezing")  # no "cooked"
        contribs = [{"signal_id": k, "text": v} for k, v in texts.items()]
        with patch("app.orchestrator.llm.llm_configured", return_value=True), \
             patch("app.orchestrator.llm.llm_json", return_value=self._payload()), \
             patch("app.orchestrator.llm.composer_model", return_value="m"):
            self.assertIsNone(syn.synthesis_for(contribs))

    def test_no_disagreement_means_no_line(self):
        self.assertIsNone(self._run({}))

    def test_too_few_voices_never_reaches_the_model(self):
        with patch("app.orchestrator.llm.llm_json") as call:
            self.assertIsNone(syn.synthesis_for(_contribs(3)))
        call.assert_not_called()

    def test_a_failed_call_is_silent(self):
        with patch("app.orchestrator.llm.llm_configured", return_value=True), \
             patch("app.orchestrator.llm.llm_json", side_effect=RuntimeError("down")), \
             patch("app.orchestrator.llm.composer_model", return_value="m"):
            self.assertIsNone(syn.synthesis_for(_contribs(6)))


class TestNumberChecking(unittest.TestCase):
    def test_number_words_are_checked_too(self):
        # "Six say…" is the shape the line actually takes, so digits alone would miss it.
        self.assertFalse(syn._numbers_agree("Six say it freezes well", {3, 2}))
        self.assertTrue(syn._numbers_agree("Three say it freezes well", {3, 2}))

    def test_digits_are_checked(self):
        self.assertFalse(syn._numbers_agree("6 say it freezes well", {3, 2}))
        self.assertTrue(syn._numbers_agree("2 went watery", {3, 2}))

    def test_a_line_with_no_numbers_passes(self):
        self.assertTrue(syn._numbers_agree("Most say it freezes well", {3, 2}))


if __name__ == "__main__":
    unittest.main()


# ── The wiring: do these reads actually reach a card, and do they stay cheap? ──────────

import app.reco_cards as cards_mod  # noqa: E402
from tests.test_reco_cards import tip  # noqa: E402


def _rich(n, **over):
    """n contributions to one subject, each with enough answers to have a body."""
    return [
        tip(
            signal_id=f"s{i}", peer_user_id=f"p{i}",
            reco_description=f"thing {i}",
            reco_fields=[{"label": "Delivery", "answer": "On time"}],
            **over,
        )
        for i in range(n)
    ]


class TestBodyReachesTheCard(unittest.TestCase):
    def test_a_cached_body_lands_on_its_own_contributor(self):
        with patch.object(body, "_cached", side_effect=lambda sid, l, s: f"prose for {sid}"), \
             patch.object(syn, "_cached", return_value={"absent": True}), \
             patch("app.reco_cluster._cached", return_value=None), \
             patch("app.reco_cards._warm"):
            out = cards_mod.subject_cards_from_tips(_rich(2), allow_compose=False)
        got = {c["signal_id"]: c["body"] for c in out[0]["contributors"]}
        self.assertEqual(got, {"s0": "prose for s0", "s1": "prose for s1"})

    def test_a_body_is_never_blended_across_neighbours(self):
        # The whole safety of composing per contribution: two voices, two bodies, and the
        # card stacks them rather than writing a description neither of them wrote.
        seen = []
        with patch.object(body, "_cached", side_effect=lambda sid, l, s: seen.append(sid) or None), \
             patch.object(syn, "_cached", return_value={"absent": True}), \
             patch("app.reco_cluster._cached", return_value=None), \
             patch("app.reco_cards._warm"):
            cards_mod.subject_cards_from_tips(_rich(2), allow_compose=False)
        self.assertEqual(sorted(seen), ["s0", "s1"])

    def test_the_list_render_never_composes_a_body(self):
        with patch.object(body, "_cached", return_value=None), \
             patch.object(body, "_compose") as compose, \
             patch.object(syn, "_cached", return_value={"absent": True}), \
             patch("app.reco_cluster._cached", return_value=None), \
             patch("app.reco_cards._warm"):
            cards_mod.subject_cards_from_tips(_rich(2), allow_compose=False)
        compose.assert_not_called()

    def test_a_thin_capture_is_never_warmed(self):
        # One fact is already the best text available; warming it could only pad it.
        thin = [tip(signal_id=f"s{i}", peer_user_id=f"p{i}", reco_fields=[]) for i in range(2)]
        with patch.object(body, "_cached", return_value=None) as cached, \
             patch.object(syn, "_cached", return_value={"absent": True}), \
             patch("app.reco_cluster._cached", return_value=None), \
             patch("app.reco_cards._warm") as warm:
            cards_mod.subject_cards_from_tips(thin, allow_compose=False)
        cached.assert_not_called()
        self.assertEqual(warm.call_args[0][1], [])  # no bodies pending


class TestSynthesisReachesTheCard(unittest.TestCase):
    READING = {
        "line": "Three say it freezes well, two say it went watery.",
        "majority": {"label": "freezes well", "n": 3, "signal_ids": ["s0", "s1", "s2"]},
        "minority": {"label": "went watery", "n": 2, "signal_ids": ["s3", "s4"]},
        "shared_trait": None,
    }

    def _run(self, cached, n=5, themes=None):
        # Bodies are cache HITS here: this class measures whether the SYNTHESIS schedules
        # a warm, and a missing body would schedule one of its own.
        with patch.object(syn, "_cached", return_value=cached), \
             patch.object(body, "_cached", return_value="prose"), \
             patch("app.reco_cluster._cached", return_value=themes), \
             patch("app.reco_cards._warm") as warm:
            out = cards_mod.subject_cards_from_tips(_rich(n), allow_compose=False)
        return out[0], warm

    def test_a_stored_reading_lands_on_the_card(self):
        card, _ = self._run(self.READING)
        self.assertEqual(card["synthesis"]["line"], self.READING["line"])

    def test_absent_is_a_decision_not_a_line(self):
        card, _ = self._run({"absent": True})
        self.assertIsNone(card["synthesis"])

    def test_absent_is_never_warmed_again(self):
        # The common outcome is that neighbours AGREE. Without the negative entry this
        # re-asks the model the same question on every render, forever.
        _, warm = self._run({"absent": True}, themes={"themes": [], "total": 5})
        warm.assert_not_called()

    def test_an_undecided_synthesis_is_warmed(self):
        _, warm = self._run(None, themes={"themes": [], "total": 5})
        warm.assert_called_once()

    def test_below_the_floor_is_never_warmed(self):
        # Three voices can never produce a synthesis, so "undecided" is permanent here and
        # warming it would re-ask an unanswerable question every time.
        _, warm = self._run(None, n=3, themes={"themes": [], "total": 3})
        warm.assert_not_called()

    def test_a_collection_is_never_reconciled(self):
        # Three banana breads that disagree are three recipes, not a dispute.
        with patch.object(syn, "_cached") as cached, \
             patch.object(body, "_cached", return_value=None), \
             patch("app.reco_cluster._cached", return_value=None), \
             patch("app.reco_cards._warm"):
            out = cards_mod.subject_cards_from_tips(
                _rich(5, subject_merge_mode="collection"), allow_compose=False)
        self.assertIsNone(out[0]["synthesis"])
        cached.assert_not_called()

    def test_a_failing_synthesis_never_costs_the_card(self):
        with patch.object(syn, "synthesis_for_subject", side_effect=RuntimeError("down")), \
             patch.object(body, "_cached", return_value="prose"), \
             patch("app.reco_cluster._cached", return_value=None), \
             patch("app.reco_cards._warm"):
            out = cards_mod.subject_cards_from_tips(_rich(5), allow_compose=False)
        self.assertEqual(len(out), 1)
        self.assertIsNone(out[0]["synthesis"])


class TestSynthesisNeverBreaksTheThemes(unittest.TestCase):
    """The synthesis shares a row with the themes. Writing it must not orphan them."""

    def test_it_updates_and_never_upserts(self):
        # An upsert would create a digest row carrying a synthesis and NO themes, which
        # reco_cluster._cached reads as a hit — so the subject would render its synthesis
        # and permanently lose its themes.
        from unittest.mock import MagicMock
        client = MagicMock()
        with patch("app.db.service_client", return_value=client):
            syn._store("sub-1", "en", "sig", {"absent": True})
        table = client.table.return_value
        table.update.assert_called_once()
        table.upsert.assert_not_called()

    def test_it_is_keyed_exactly_like_the_themes(self):
        # Two readings of the SAME contributions. A different key would let a card show
        # themes built from five voices beside a synthesis built from four.
        from app.reco_cluster import basis_sig
        contribs = [{"signal_id": f"s{i}", "text": f"note {i}"} for i in range(5)]
        seen = {}
        with patch.object(syn, "_cached",
                          side_effect=lambda ref, l, s: seen.update(sig=s) or {"absent": True}):
            syn.synthesis_for_subject("sub-1", contribs, allow_compose=False)
        self.assertEqual(seen["sig"], basis_sig(contribs))


class TestNarrativeReachesTheWire(unittest.TestCase):
    """Stamping a field onto a card dict is not shipping it — the response is built from
    an explicit allowlist, which is exactly how `reco_cards` itself was silently dropped
    for the whole of Stage 3."""

    READING = {
        "line": "Three say it freezes well, two say it went watery.",
        "majority": {"label": "freezes well", "n": 3, "signal_ids": ["s0", "s1", "s2"]},
        "minority": {"label": "went watery", "n": 2, "signal_ids": ["s3", "s4"]},
        "shared_trait": "froze cooked",
    }

    def _card(self):
        return {
            "title": "Dr. Sarah Chen", "subject_ref": "sub-1", "vouch_count": 5,
            "merge_mode": "aggregate", "themes": None, "synthesis": self.READING,
            "contributors": [{
                "signal_id": "s0", "peer_user_id": "p0", "nickname": "coral88",
                "description": "so gentle", "body": "Gentle with anxious toddlers.",
            }],
        }

    def test_body_and_synthesis_survive_the_last_hop(self):
        from app.main import _reco_cards_from_ctx
        rows = _reco_cards_from_ctx({"reco_cards": [self._card()]})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].contributors[0].body, "Gentle with anxious toddlers.")
        self.assertEqual(rows[0].synthesis.line, self.READING["line"])
        self.assertEqual(rows[0].synthesis.minority.n, 2)
        self.assertEqual(rows[0].synthesis.shared_trait, "froze cooked")

    def test_the_evidence_travels_with_the_claim(self):
        # The line asserts a count about identifiable people. A client that cannot get
        # back to who said what cannot show the reader why to believe it.
        from app.main import _reco_cards_from_ctx
        rows = _reco_cards_from_ctx({"reco_cards": [self._card()]})
        self.assertEqual(rows[0].synthesis.majority.signal_ids, ["s0", "s1", "s2"])

    def test_a_card_with_neither_still_ships(self):
        from app.main import _reco_cards_from_ctx
        card = self._card()
        card["synthesis"] = None
        card["contributors"][0]["body"] = None
        rows = _reco_cards_from_ctx({"reco_cards": [card]})
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].synthesis)
        self.assertIsNone(rows[0].contributors[0].body)

    def test_a_malformed_synthesis_never_costs_the_card(self):
        # The recommendation is the answer; the line about it is the extra. Losing a real
        # recommendation because its narrative came out wrong is the wrong trade.
        from app.main import _reco_cards_from_ctx
        card = self._card()
        card["synthesis"] = {"line": "half a reading"}  # no sides
        rows = _reco_cards_from_ctx({"reco_cards": [card]})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].title, "Dr. Sarah Chen")
        self.assertIsNone(rows[0].synthesis)
        # ...and the bodies beside it survive, because they were never the problem.
        self.assertEqual(rows[0].contributors[0].body, "Gentle with anxious toddlers.")

    def test_a_malformed_body_never_costs_the_card_either(self):
        from app.main import _reco_cards_from_ctx
        card = self._card()
        card["contributors"][0]["body"] = {"not": "a string"}
        rows = _reco_cards_from_ctx({"reco_cards": [card]})
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].contributors[0].body)

    def test_a_card_broken_beyond_the_narrative_is_still_dropped(self):
        # The retry must not become a way for genuinely malformed cards to get through.
        from app.main import _reco_cards_from_ctx
        card = self._card()
        card["vouch_count"] = "three"
        rows = _reco_cards_from_ctx({"reco_cards": [card]})
        self.assertEqual(rows, [])
