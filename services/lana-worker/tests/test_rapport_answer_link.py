"""A rapport answer must close what it answered — and everything else it answered.

Prod, 2026-10-03 (Tommaso, handle todiba): asked about his gym eight times since August,
answering every time. Three faults, each covered here:

1. The answer was linked to the user's NEWEST claim, not the one it wrote. When the answer
   merged into an existing thread, that was an unrelated row ("which gym?" → "Speaks
   Brazilian Portuguese"), so the gym thread never read as covered.
2. One answer settles several queued questions worded differently; nothing closed them.
3. The question writer never saw what was already known (his confirmed gym community).
"""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service")

from app import rapport_gaps, rapport_synth  # noqa: E402
from app.claims_persist import ClaimExtractResult, _richest_first, upsert_claims  # noqa: E402
from app.models import ExtractedClaim  # noqa: E402


def _claim(concept="gym_goer", conf=0.9) -> ExtractedClaim:
    return ExtractedClaim(
        concept=concept, label="Goes to a gym", tone=None, confidence=conf,
        disclosure="public", synonyms=["gym"], details=[], source_quote="Fitness CF",
        bucket="activity", transient=False,
    )


def _sb(existing: list[dict], inserted: list[dict]):
    sb, table = MagicMock(), MagicMock()
    sb.table.return_value = table
    chain = MagicMock()
    table.select.return_value = chain
    for m in ("eq", "is_", "limit", "or_", "contains", "ilike"):
        getattr(chain, m).return_value = chain
    chain.execute.return_value = MagicMock(data=existing)
    table.update.return_value.eq.return_value.execute.return_value = MagicMock(data=[])
    table.insert.return_value.execute.return_value = MagicMock(data=inserted)
    return sb


def _run_upsert(existing, inserted, claims):
    written: list[tuple[str, float]] = []
    with patch("app.claims_persist.service_client", return_value=_sb(existing, inserted)), patch(
        "app.claims_persist._embed_claim", return_value=[0.5] * 768
    ), patch("app.claims_persist.reconcile_heritage_claims"), patch(
        "app.profile_portrait.schedule_portrait_refresh"
    ), patch.dict(os.environ, {"IDENTITY_CONCEPT_LINK_ENABLED": "0"}, clear=False):
        upsert_claims("user-1", claims, written=written)
    return written


class TestAnswerLinksTheClaimItWrote(unittest.TestCase):
    def test_merge_into_existing_thread_reports_that_thread(self):
        """The todiba case: the answer merges into the old gym claim — that id, not the newest."""
        stored = {"id": "gym-claim", "confidence": 0.8, "synonyms": ["gym"], "details": [],
                  "label": "Goes to a gym", "source_quote": "gym", "bucket": "activity"}
        written = _run_upsert([stored], [], [_claim()])
        self.assertEqual([w[0] for w in written], ["gym-claim"])

    def test_new_claim_reports_its_inserted_id(self):
        written = _run_upsert([], [{"id": "new-claim"}], [_claim()])
        self.assertEqual([w[0] for w in written], ["new-claim"])

    def test_richest_first_and_once(self):
        self.assertEqual(_richest_first([("a", 0.5), ("b", 0.9), ("a", 0.7)]), ["b", "a"])
        self.assertEqual(ClaimExtractResult(claim_ids=["b", "a"]).primary_claim_id, "b")
        self.assertIsNone(ClaimExtractResult().primary_claim_id)

    def test_record_answer_links_primary_not_newest(self):
        """End of the chain: the endpoint stores the claim the answer wrote."""
        from app import main

        res = ClaimExtractResult(saved=1, claim_ids=["gym-claim"])
        marked = {}
        with patch.object(main, "verify_auth", return_value=SimpleNamespace(user_id="u1", is_anonymous=False)), \
             patch("app.rapport_gaps.get_gap_row", return_value={"question": "Which gym do you go to?"}), \
             patch.object(main, "try_upsert_claims_from_message", return_value=res), \
             patch.object(main, "rapport_reconcile_gaps"), \
             patch("app.claims_persist.claim_ids_created_since", return_value=[]), \
             patch("app.circles_flow.tag_claim_place_from_gap"), \
             patch.object(main, "rapport_mark_answered", side_effect=lambda g, answer_claim_id=None: marked.update(id=answer_claim_id)), \
             patch.object(main, "amplitude_track"), \
             patch("app.claims_persist.latest_claim_id", return_value="speaks-portuguese"):
            bg = MagicMock()
            main.post_rapport_record_answer(
                main.RapportAnswerBody(gap_row_id="g1", text="Fitness CF in St. Cloud"), bg, "Bearer x"
            )
        self.assertEqual(marked.get("id"), "gym-claim")
        task = bg.add_task.call_args[0]
        self.assertIs(task[0], main.rapport_after_answer)
        self.assertEqual(task[1:], ("u1", "g1", "Which gym do you go to?", "Fitness CF in St. Cloud", "gym-claim"))


class _GapsTable:
    """Just enough of rapport_gaps for close_gaps_answered_by: one select, row updates."""

    def __init__(self, rows):
        self.rows, self.updates = rows, []

    def table(self, _name):
        return self

    def select(self, *_a, **_k):
        self._mode = "select"
        return self

    def update(self, patch_):
        self._mode, self._patch = "update", patch_
        return self

    def eq(self, col, val):
        if self._mode == "update" and col == "gap_row_id":
            self._target = val
        return self

    def in_(self, *_a):
        return self

    @property
    def not_(self):
        return self

    def is_(self, *_a):
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a):
        return self

    def execute(self):
        if self._mode == "update":
            self.updates.append((self._target, self._patch))
            return SimpleNamespace(data=[])
        return SimpleNamespace(data=self.rows)


class TestOneAnswerClosesItsSiblings(unittest.TestCase):
    ROWS = [
        {"gap_row_id": "g-spot", "question": "Which Fitness CF spot do you usually go to?"},
        {"gap_row_id": "g-time", "question": "What time do you usually train?"},
        {"gap_row_id": "g-self", "question": "Which gym do you go to?"},
    ]

    def _run(self, verdict):
        db = _GapsTable([dict(r) for r in self.ROWS])
        with patch.object(rapport_gaps, "service_client", return_value=db), \
             patch.object(rapport_gaps, "_siblings_verdict", return_value=verdict) as judge:
            n = rapport_gaps.close_gaps_answered_by("u1", "g-self", "Which gym do you go to?", "Fitness CF", "gym-claim")
        return n, db, judge

    def test_closes_only_what_the_model_says_was_answered(self):
        n, db, judge = self._run([1])
        self.assertEqual(n, 1)
        self.assertEqual([u[0] for u in db.updates], ["g-spot"])
        self.assertEqual(db.updates[0][1]["status"], "answered")
        self.assertEqual(db.updates[0][1]["answer_claim_id"], "gym-claim")
        # The answered gap itself is never offered back to the judge.
        self.assertNotIn("Which gym do you go to?", judge.call_args[0][2])

    def test_nothing_closed_when_nothing_answered(self):
        n, db, _ = self._run([])
        self.assertEqual((n, db.updates), (0, []))

    def test_empty_answer_never_calls_the_model(self):
        with patch.object(rapport_gaps, "_siblings_verdict") as judge:
            self.assertEqual(rapport_gaps.close_gaps_answered_by("u1", "g", "q", "  "), 0)
        judge.assert_not_called()

    def test_verdict_ignores_out_of_range_and_junk(self):
        with patch("app.orchestrator.llm.llm_configured", return_value=True), \
             patch("app.orchestrator.llm.router_model", return_value="m"), \
             patch("app.orchestrator.llm.llm_json", return_value={"answered": [2, 9, "x", 2, 0]}):
            self.assertEqual(rapport_gaps._siblings_verdict("q", "a", ["one", "two"]), [2])


class TestWriterSeesWhatIsKnown(unittest.TestCase):
    def test_known_block_lists_claims_and_confirmed_communities(self):
        claims = [{"label": "Goes to a gym", "details": ["CrossFit"]}]
        circles = [{"circle_key": "fitness_cf", "place_name": None, "places": {"name": "Fitness CF - St. Cloud"}},
                   {"circle_key": "book_club", "place_name": None, "places": None}]
        sb = MagicMock()
        q = sb.table.return_value.select.return_value
        for m in ("eq", "is_", "order", "limit"):
            getattr(q, m).return_value = q
        q.execute.side_effect = [SimpleNamespace(data=claims), SimpleNamespace(data=circles)]
        with patch.object(rapport_synth, "service_client", return_value=sb):
            block = rapport_synth._known_block("u1")
        self.assertIn("- Goes to a gym (CrossFit)", block)
        self.assertIn("- belongs to Fitness CF - St. Cloud", block)
        self.assertIn("- belongs to book club", block)

    def test_known_block_reaches_the_writer(self):
        seen = {}

        def fake_generate(uncovered, asked, max_new, known="(nothing yet)"):
            seen["known"] = known
            return {"questions": []}

        with patch.object(rapport_synth, "_uncovered_claims", return_value=[{"concept": "gym_goer", "label": "Goes to a gym"}]), \
             patch.object(rapport_synth, "_language_thread_needed", return_value=False), \
             patch.object(rapport_synth, "_backfill_question_embeddings"), \
             patch.object(rapport_synth, "recent_gap_questions", return_value=[]), \
             patch.object(rapport_synth, "_attach_reco_subjects"), \
             patch.object(rapport_synth, "_cooling_down", return_value=False, create=True), \
             patch.object(rapport_synth, "_known_block", return_value="- belongs to Fitness CF - St. Cloud"), \
             patch.object(rapport_synth, "_generate", side_effect=fake_generate):
            rapport_synth.synthesize_gaps_from_claims("u1")
        self.assertEqual(seen.get("known"), "- belongs to Fitness CF - St. Cloud")

    def test_after_answer_closes_siblings_before_refilling(self):
        order = []
        with patch("app.rapport_gaps.close_gaps_answered_by", side_effect=lambda *a, **k: order.append("close")), \
             patch.object(rapport_synth, "ensure_gap_buffer", side_effect=lambda *a, **k: order.append("refill")):
            rapport_synth.after_answer("u1", "g", "q", "a", "c")
        self.assertEqual(order, ["close", "refill"])


if __name__ == "__main__":
    unittest.main()
