"""The authored "why this neighbour" line on a fellows row.

The card used to render trait chips; it now renders one AI-written sentence. What must
hold: the line is authored from the PROVEN overlap only, a reload serves the cached line
(no LLM call), a NEW overlap authors a new one, and a failed compose leaves the row exactly
as it was before — with its tags — rather than a canned sentence.

The Supabase client is faked (no network), same pattern as test_lana_feedback.
"""

import unittest
from unittest.mock import patch

from app import peer_rec_line
from app.peer_rec_line import _basis, _basis_sig, _clean_chips, attach_rec_lines


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, table, store):
        self.table, self.store = table, store
        self._op = None
        self._payload = None

    def select(self, *a, **k):
        self._op = "select"
        return self

    def upsert(self, payload, **k):
        self._op = "upsert"
        self._payload = payload
        return self

    def eq(self, *a, **k):
        return self

    def in_(self, *a, **k):
        return self

    def execute(self):
        if self._op == "select":
            return _Result(list(self.store["rows"]))
        if self._op == "upsert":
            self.store["upserts"].append(self._payload)
            return _Result(
                [
                    {**row, "id": f"rec-{i}"}
                    for i, row in enumerate(self._payload, start=1)
                ]
            )
        return _Result([])


class _Supabase:
    def __init__(self, store):
        self.store = store

    def table(self, name):
        return _Query(name, self.store)


def _peer(n, shared):
    return {"peer_user_id": f"p-{n}", "shared_labels": shared}


def _row(shared):
    return {"trait_tags": list(shared), "matching_peer_label": "You both: " + " · ".join(shared)}


class TestAttachRecLines(unittest.TestCase):
    def _run(self, rows, peers, *, cached=None, llm=None, lang="en"):
        store = {"rows": cached or [], "upserts": []}
        data = {"lines": llm} if llm is not None else {}
        with (
            patch.object(peer_rec_line, "service_client", return_value=_Supabase(store)),
            patch("app.lang_pref.get_user_preferred_language", return_value=lang),
            patch("app.orchestrator.llm.llm_configured", return_value=True),
            patch("app.orchestrator.llm.composer_model", return_value="test-model"),
            patch("app.orchestrator.llm.llm_json", return_value=data) as call,
        ):
            attach_rec_lines("u-1", rows, peers)
        return store, call

    def test_authors_one_line_per_row_in_one_call_and_caches_it(self):
        # ONE batched call for the whole screen — never one per row — and the stored row
        # is what a later 👍/👎 hangs off, so rec_id must ride out with the line.
        rows = [_row(["Runs at dawn"]), _row(["Reads sci-fi"])]
        store, call = self._run(
            rows,
            [_peer(1, ["Runs at dawn"]), _peer(2, ["Reads sci-fi"])],
            llm=["You're both up before the sun.", "You both live in sci-fi."],
        )
        self.assertEqual(call.call_count, 1)
        self.assertEqual(rows[0]["rec_line"], "You're both up before the sun.")
        self.assertEqual(rows[1]["rec_line"], "You both live in sci-fi.")
        self.assertEqual([r["rec_id"] for r in rows], ["rec-1", "rec-2"])
        (payload,) = store["upserts"]
        self.assertEqual([p["peer_user_id"] for p in payload], ["p-1", "p-2"])
        self.assertTrue(all(p["lang"] == "en" and p["basis_sig"] for p in payload))

    def test_cached_line_costs_no_llm_call(self):
        shared = ["Runs at dawn"]
        sig = _basis_sig(_basis(_peer(1, shared), _row(shared)))
        rows = [_row(shared)]
        store, call = self._run(
            rows,
            [_peer(1, shared)],
            cached=[
                {
                    "id": "rec-old",
                    "peer_user_id": "p-1",
                    "basis_sig": sig,
                    "line": "You're both up before the sun.",
                    "chips": ["Runs at dawn"],
                }
            ],
        )
        self.assertEqual(call.call_count, 0)
        self.assertEqual(rows[0]["rec_chips"], ["Runs at dawn"])
        self.assertEqual(store["upserts"], [])
        self.assertEqual(rows[0]["rec_id"], "rec-old")

    def test_a_new_shared_claim_authors_a_new_line(self):
        # The cached row was written from one overlap; the pair now share two. Serving the
        # old line would describe a smaller thing than the card is standing on.
        old_sig = _basis_sig(_basis(_peer(1, ["Runs at dawn"]), _row(["Runs at dawn"])))
        shared = ["Runs at dawn", "Reads sci-fi"]
        rows = [_row(shared)]
        _store_, call = self._run(
            rows,
            [_peer(1, shared)],
            cached=[
                {
                    "id": "rec-old",
                    "peer_user_id": "p-1",
                    "basis_sig": old_sig,
                    "line": "You're both up before the sun.",
                }
            ],
            llm=["You're both dawn runners with a sci-fi shelf."],
        )
        self.assertEqual(call.call_count, 1)
        self.assertEqual(rows[0]["rec_line"], "You're both dawn runners with a sci-fi shelf.")
        self.assertEqual(rows[0]["rec_id"], "rec-1")

    def test_a_bad_compose_leaves_the_row_on_its_tags(self):
        # Wrong number of lines back: nothing is assigned, so the card falls back to the
        # chips it always had. No canned "you have a lot in common" sentence.
        rows = [_row(["Runs at dawn"]), _row(["Reads sci-fi"])]
        store, _call = self._run(
            rows, [_peer(1, ["Runs at dawn"]), _peer(2, ["Reads sci-fi"])], llm=["only one"]
        )
        self.assertNotIn("rec_line", rows[0])
        self.assertNotIn("rec_line", rows[1])
        self.assertEqual(store["upserts"], [])

    def test_a_row_with_no_overlap_is_skipped_entirely(self):
        rows = [{"trait_tags": []}]
        store, call = self._run(rows, [{"peer_user_id": "p-9"}])
        self.assertEqual(call.call_count, 0)
        self.assertNotIn("rec_line", rows[0])
        self.assertEqual(store["upserts"], [])

    def test_non_english_reader_gets_the_line_authored_in_their_language(self):
        # Authored straight into the reader's language (one call), and cached under it —
        # a Spanish reader must never be served the English row.
        rows = [_row(["Runs at dawn"])]
        store, call = self._run(
            rows, [_peer(1, ["Runs at dawn"])], llm=["Ambos madrugan."], lang="es"
        )
        self.assertIn("Spanish", call.call_args.kwargs["system"])
        self.assertEqual(store["upserts"][0][0]["lang"], "es")

    def test_the_prompt_carries_only_the_overlap(self):
        # Grounding + PII: the model sees the shared labels and nothing else — no nickname,
        # no concept slug (the one field redaction never covers), no private claims.
        rows = [_row(["Runs at dawn"])]
        _store_, call = self._run(
            rows,
            [
                {
                    **_peer(1, ["Runs at dawn"]),
                    "nickname": "Sofia",
                    "matching_peer_concept": "fitness.running.dawn",
                }
            ],
            llm=["You're both up before the sun."],
        )
        payload = call.call_args.kwargs["user_payload"]
        self.assertIn("Runs at dawn", payload)
        self.assertNotIn("Sofia", payload)
        self.assertNotIn("fitness.running.dawn", payload)

    def test_kids_claims_stay_the_kids(self):
        rows = [_row([])]
        peers = [{"peer_user_id": "p-1", "shared_child_labels": ["Same grade at Laureate"]}]
        _store_, call = self._run(rows, peers, llm=["Your kids are in the same grade."])
        self.assertIn("kids_shared", call.call_args.kwargs["user_payload"])


class TestChips(unittest.TestCase):
    """The new card leads with 2-3 facets and keeps the sentence under them."""

    def test_chips_ride_out_with_the_line_and_are_cached(self):
        rows = [_row(["Runs at dawn", "Reads sci-fi"])]
        store, _call = TestAttachRecLines._run(
            TestAttachRecLines(),
            rows,
            [_peer(1, ["Runs at dawn", "Reads sci-fi"])],
            llm=[
                {
                    "chips": ["Runs at dawn", "Sci-fi shelf"],
                    "line": "You're both dawn runners with a sci-fi shelf.",
                }
            ],
        )
        self.assertEqual(rows[0]["rec_chips"], ["Runs at dawn", "Sci-fi shelf"])
        self.assertEqual(rows[0]["rec_line"], "You're both dawn runners with a sci-fi shelf.")
        self.assertEqual(store["upserts"][0][0]["chips"], ["Runs at dawn", "Sci-fi shelf"])

    def test_a_line_authored_before_chips_existed_is_reauthored_once(self):
        # chips IS NULL on every row written before the column: serving it would render
        # the new card with an empty facet strip.
        shared = ["Runs at dawn"]
        sig = _basis_sig(_basis(_peer(1, shared), _row(shared)))
        rows = [_row(shared)]
        store, call = TestAttachRecLines._run(
            TestAttachRecLines(),
            rows,
            [_peer(1, shared)],
            cached=[
                {
                    "id": "rec-old",
                    "peer_user_id": "p-1",
                    "basis_sig": sig,
                    "line": "You're both up before the sun.",
                    "chips": None,
                }
            ],
            llm=[{"chips": ["Runs at dawn"], "line": "You're both up before the sun."}],
        )
        self.assertEqual(call.call_count, 1)
        self.assertEqual(rows[0]["rec_chips"], ["Runs at dawn"])
        self.assertEqual(store["upserts"][0][0]["chips"], ["Runs at dawn"])

    def test_only_chip_shaped_facets_survive(self):
        # A sentence, a duplicate and a fourth facet are not chips — the strip is small.
        self.assertEqual(
            _clean_chips(
                [
                    "Runs at dawn.",
                    "runs at dawn",
                    "You are both people who get up very early",
                    "Author talks",
                    "Twin toddlers",
                    "Book club",
                ]
            ),
            ["Runs at dawn", "Author talks", "Twin toddlers"],
        )
        self.assertEqual(_clean_chips(None), [])


if __name__ == "__main__":
    unittest.main()
