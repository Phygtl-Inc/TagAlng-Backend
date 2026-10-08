"""Backend asks §35(b), §35(d), §47(2), §30(d), §30(e) — the recommendation read, the
place id on a place answer, the edit route, the community block on a fallback place, and
the ask-draft chip re-ask. The SQL halves (§40, §47(1)/(2) in set_signal_reco) are
exercised against a real Postgres, not here."""

from __future__ import annotations

import unittest
from unittest import mock
from unittest.mock import patch

from fastapi import HTTPException

from app.auth import AuthSession

_FIELDS = [
    {"field": "where", "label": "Where", "question": "Where is it?", "kind": "place",
     "answer": "Lake Nona Smiles · 1 Main St", "google_place_id": "ChIJsmiles"},
    {"field": "insurance", "label": "Insurance", "question": "Insurance?", "kind": "text",
     "answer": "Aetna"},
]


def _tip(**over) -> dict:
    row = {
        "signal_id": "sig-1",
        "detail_text": "Dr. Reyes · pediatric dentist · Where: Lake Nona Smiles",
        "category": "pediatric dentist",
        "match_strength": 0.9,
        "neighbor_label": "Marisol",
        "peer_user_id": "peer-1",
        "affinity_tags": [],
        "reco_fields": _FIELDS,
        "reco_type": "professional",
    }
    row.update(over)
    return row


class TestCascadeRowCarriesTheCard(unittest.TestCase):
    """§35(b): the rec-cascade row (peer_matches) carries reco_fields + reco_type."""

    def test_fields_as_stored_and_type(self) -> None:
        from app.tip_rec_cascade import peer_rows_from_neighbor_tips

        row = peer_rows_from_neighbor_tips([_tip()])[0]
        self.assertEqual(row["reco_type"], "professional")
        self.assertEqual([f["field"] for f in row["reco_fields"]], ["where", "insurance"])
        self.assertEqual(row["reco_fields"][0]["google_place_id"], "ChIJsmiles")
        self.assertEqual(
            set(row["reco_fields"][1]), {"field", "label", "question", "kind", "answer"}
        )

    def test_prose_only_tip_is_null_not_empty_or_error(self) -> None:
        from app.tip_rec_cascade import peer_rows_from_neighbor_tips

        # find_neighbor_tips coalesces a prose-only row to [] — the wire says null.
        for raw in ([], None, "garbage"):
            row = peer_rows_from_neighbor_tips([_tip(reco_fields=raw, reco_type=None)])[0]
            self.assertIsNone(row["reco_fields"])
            self.assertIsNone(row["reco_type"])

    def test_wire_projection_keeps_them(self) -> None:
        from app.main import _peer_matches_from_ctx
        from app.tip_rec_cascade import peer_rows_from_neighbor_tips

        rows = peer_rows_from_neighbor_tips([_tip(), _tip(signal_id="s2", peer_user_id="p2", reco_fields=[])])
        out = _peer_matches_from_ctx({"peer_matches": rows})
        by_id = {r.tip_signal_id: r for r in out}
        self.assertEqual(by_id["sig-1"].reco_type, "professional")
        self.assertEqual(by_id["sig-1"].reco_fields[0]["google_place_id"], "ChIJsmiles")
        self.assertIsNone(by_id["s2"].reco_fields)
        dumped = by_id["s2"].model_dump()
        self.assertIn("reco_fields", dumped)
        self.assertIsNone(dumped["reco_fields"])

    def test_feed_row_keeps_place_id(self) -> None:
        from app.tip_feed import _row

        row = _row({"signal_id": "s", "reco_name": "Dr. Reyes", "reco_fields": _FIELDS})
        self.assertEqual(row["fields"][0]["google_place_id"], "ChIJsmiles")
        self.assertNotIn("google_place_id", row["fields"][1])


def _draft(rtype: str = "professional", *, answers: dict | None = None, steps=None) -> dict:
    return {
        "name": "Dr. Reyes",
        "reco_type": rtype,
        "step_set": steps
        or [
            {"field": "subject", "label": "Who", "question": "Who is it?", "kind": "text",
             "required": True},
            {"field": "where", "label": "Where", "question": "Where is it?", "kind": "place"},
            {"field": "insurance", "label": "Insurance", "question": "Insurance?",
             "kind": "text"},
        ],
        "answers": answers if answers is not None else {
            "subject": "Dr. Reyes", "where": "Lake Nona Smiles · 1 Main St",
            "insurance": "Aetna",
        },
    }


class TestPlaceIdOnPlaceAnswer(unittest.TestCase):
    """§35(d): a kind:"place" step's answer row carries google_place_id."""

    def test_carousel_pick_lands_on_the_stored_row(self) -> None:
        from app.tip_share import _reco_fields, pin_place_answer

        draft = _draft()
        field = pin_place_answer(draft, google_place_id="ChIJsmiles", answers=draft["answers"])
        self.assertEqual(field, "where")
        rows = {r["field"]: r for r in _reco_fields(draft)}
        self.assertEqual(rows["where"]["google_place_id"], "ChIJsmiles")
        self.assertNotIn("google_place_id", rows["insurance"])

    def test_a_clinic_pick_does_not_ground_the_dentist(self) -> None:
        from app.tip_share import pin_place_answer

        draft = _draft()
        pin_place_answer(draft, google_place_id="ChIJsmiles", answers=draft["answers"])
        self.assertNotIn("subject_google_place_id", draft)

    def test_a_subject_pick_still_grounds_the_subject(self) -> None:
        from app.tip_share import _reco_fields, pin_place_answer, place_ids_of

        steps = [
            {"field": "subject", "label": "Place", "question": "Which place?", "kind": "place",
             "required": True},
            {"field": "dish", "label": "Dish", "question": "What to order?", "kind": "text"},
        ]
        draft = _draft("restaurant", steps=steps, answers={"subject": "Pausa", "dish": "pasta"})
        self.assertEqual(
            pin_place_answer(draft, google_place_id="ChIJpausa", answers=draft["answers"]),
            "subject",
        )
        self.assertEqual(draft["subject_google_place_id"], "ChIJpausa")
        self.assertEqual(place_ids_of(draft), {"subject": "ChIJpausa"})
        # The subject never rides in reco_fields — it is the grounding, not an answer row.
        self.assertEqual([r["field"] for r in _reco_fields(draft)], ["dish"])

    def test_a_typed_over_answer_drops_the_stale_id(self) -> None:
        from app.tip_share import _reco_fields, pin_place_answer

        draft = _draft()
        pin_place_answer(draft, google_place_id="ChIJsmiles", answers=draft["answers"])
        draft["answers"]["where"] = "somewhere else entirely"
        rows = {r["field"]: r for r in _reco_fields(draft)}
        self.assertNotIn("google_place_id", rows["where"])

    def test_chat_fork_tap_on_an_offered_place(self) -> None:
        from app.tip_share import place_ids_of

        draft = _draft()
        draft["subject_place_options"] = [
            {"name": "Lake Nona Smiles", "place_id": "ChIJoffered", "lat": 1, "lng": 2}
        ]
        self.assertEqual(place_ids_of(draft), {"where": "ChIJoffered"})

    def test_typed_answer_nobody_offered_gets_no_id(self) -> None:
        from app.tip_share import place_ids_of

        draft = _draft(answers={"where": "my cousin's place"})
        draft["subject_place_options"] = [{"name": "Lake Nona Smiles", "place_id": "x"}]
        self.assertEqual(place_ids_of(draft), {})

    def test_reco_step_carries_it_on_the_wire(self) -> None:
        from app.models import TipDraft

        draft = TipDraft(steps=[{"field": "where", "label": "Where", "question": "?",
                                 "kind": "place", "answer": "x", "google_place_id": "g1"}])
        self.assertEqual(draft.model_dump()["steps"][0]["google_place_id"], "g1")


class TestPlaceSuggestionCommunity(unittest.TestCase):
    """§30(d): the community block was stamped and passed through, then dropped by the
    response model. It now reaches the wire."""

    def test_community_block_survives_the_model(self) -> None:
        from app.main import _place_suggestions_from_ctx
        from app.models import PlaceSuggestionRow

        ctx = {"google_place_suggestions": [
            {"name": "Orlando Public Library", "address": "101 E Central", "place_id": "g1",
             "community": {"place_id": "p1", "member_count": 3, "is_member": False,
                           "activity_labels": ["Weekly reading session"],
                           "matched_label": "Weekly reading session"}},
            {"name": "Some Cafe", "address": "", "place_id": "g2"},
        ]}
        rows = [PlaceSuggestionRow(**r) for r in _place_suggestions_from_ctx(ctx)]
        dumped = [r.model_dump() for r in rows]
        self.assertEqual(dumped[0]["community"]["member_count"], 3)
        self.assertEqual(dumped[0]["community"]["matched_label"], "Weekly reading session")
        self.assertIsNone(dumped[1]["community"])


_AUTHOR = AuthSession(user_id="author-1", is_anonymous=False, phone_verified=True,
                      home_block_id="block-a")
_AUTH = "Bearer jwt"


class TestTipUpdate(unittest.TestCase):
    """§47(2): an edit through the worker re-embeds and re-matches."""

    def test_update_tip_writes_rpc_then_vector_then_refresh(self) -> None:
        from app import local_signals

        calls: list[tuple[str, dict]] = []

        def rpc(_jwt, name, payload):
            calls.append((name, payload))
            return 2 if name == "refresh_my_signal_matches" else None

        table = mock.MagicMock()
        table.table.return_value.select.return_value.eq.return_value.limit.return_value \
            .execute.return_value.data = [{
                "intent": "tip_share", "detail_text": "Tony's Pizza · good pizza",
                "category": "food", "reco_name": "Tony's Pizza", "reco_place": None,
                "reco_description": "good pizza", "affinity_tags": ["pizza"],
            }]
        with patch.object(local_signals, "call_rpc", side_effect=rpc), \
             patch("app.auth.service_client", return_value=table), \
             patch("app.layer1_handlers._embed_attr_filter", return_value=[0.1] * 768) as emb:
            out = local_signals.update_tip(
                "jwt", signal_id="s1",
                edit={"reco_name": "Tony's Pizza", "reco_place": None, "reco_fields": []},
            )
        self.assertEqual([c[0] for c in calls],
                         ["set_signal_reco", "set_signal_embedding", "refresh_my_signal_matches"])
        self.assertEqual(calls[0][1], {"p_signal_id": "s1", "p_reco_name": "Tony's Pizza",
                                       "p_reco_fields": []})
        # Embedded from the EDITED row the RPC re-joined, not from what the client sent.
        self.assertIn("Tony's Pizza", emb.call_args.args[0])
        self.assertEqual(out, {"embedded": True, "rematched": 2})

    def test_no_vector_no_refresh(self) -> None:
        from app import local_signals

        names: list[str] = []
        table = mock.MagicMock()
        table.table.return_value.select.return_value.eq.return_value.limit.return_value \
            .execute.return_value.data = [{"intent": "tip_share", "detail_text": "x y"}]
        with patch.object(local_signals, "call_rpc",
                          side_effect=lambda _j, n, _p: names.append(n)), \
             patch("app.auth.service_client", return_value=table), \
             patch("app.layer1_handlers._embed_attr_filter", return_value=None):
            out = local_signals.update_tip("jwt", signal_id="s1", edit={"reco_name": "x"})
        self.assertEqual(names, ["set_signal_reco"])
        self.assertEqual(out, {"embedded": False, "rematched": 0})

    def test_route_refuses_a_tip_that_is_not_yours(self) -> None:
        from app.main import TipUpdateBody, post_tips_update

        with patch("app.main.verify_auth", return_value=_AUTHOR), \
             patch("app.tip_feed.tip_by_id", return_value={"peer_user_id": "someone-else"}), \
             patch("app.local_signals.update_tip") as upd:
            with self.assertRaises(HTTPException) as err:
                post_tips_update(TipUpdateBody(signal_id="s1", reco_name="x"), authorization=_AUTH)
        self.assertEqual(err.exception.status_code, 404)
        upd.assert_not_called()

    def test_route_edits_and_returns_the_new_row(self) -> None:
        from app.main import TipUpdateBody, post_tips_update

        reads = [{"peer_user_id": "author-1", "name": "Old"},
                 {"peer_user_id": "author-1", "name": "New"}]
        with patch("app.main.verify_auth", return_value=_AUTHOR), \
             patch("app.tip_feed.tip_by_id", side_effect=reads), \
             patch("app.local_signals.update_tip",
                   return_value={"embedded": True, "rematched": 0}) as upd:
            res = post_tips_update(
                TipUpdateBody(signal_id="s1", reco_name="New", reco_place=None),
                authorization=_AUTH,
            )
        self.assertEqual(upd.call_args.kwargs["edit"], {"reco_name": "New"})
        self.assertEqual(res, {"tip": {"peer_user_id": "author-1", "name": "New"},
                               "embedded": True})

    def test_route_maps_rpc_validation_to_422(self) -> None:
        from app.main import TipUpdateBody, post_tips_update

        with patch("app.main.verify_auth", return_value=_AUTHOR), \
             patch("app.tip_feed.tip_by_id", return_value={"peer_user_id": "author-1"}), \
             patch("app.local_signals.update_tip",
                   side_effect=HTTPException(status_code=502, detail="... invalid_reco_type ...")):
            with self.assertRaises(HTTPException) as err:
                post_tips_update(TipUpdateBody(signal_id="s1", reco_type="bogus"),
                                 authorization=_AUTH)
        self.assertEqual(err.exception.status_code, 422)


class TestAskDraftChipReask(unittest.TestCase):
    """§30(e): a tapped ask-draft chip posts `fix:<field>` and re-asks that one part."""

    def setUp(self) -> None:
        from tests.test_tip_rec_cascade import _tip as cascade_tip, _tip_slots

        self.save = mock.patch("app.discovery_route.save_local_signal").start()
        self.tips = mock.patch(
            "app.discovery_route.find_neighbor_tips", return_value=[cascade_tip()]
        ).start()
        mock.patch("app.discovery_route._search_tip_places", return_value=[]).start()
        mock.patch("app.discovery_route.discovery_ai_enabled", return_value=True).start()
        mock.patch(
            "app.discovery_route.discovery_slots_for_turn", return_value=_tip_slots()
        ).start()
        # The AI reader must never be what decides a protocol payload.
        self.reader = mock.patch(
            "app.tip_ask_ai.interpret_ask_draft_reply", return_value="other"
        ).start()
        self.addCleanup(mock.patch.stopall)

    @staticmethod
    def _drafted() -> dict:
        return {
            "routing_phase": "listening",
            "ask_draft_pending": {
                "title": "Gentle pediatric dentist",
                "detail": "gentle pediatric dentist in lake nona",
                "category": "health",
                "chips": [
                    {"label": "pediatric dentist", "field": "category"},
                    {"label": "Lake Nona", "field": "locality"},
                    {"label": "gentle", "field": "qualifier"},
                ],
            },
        }

    def _turn(self, msg, ctx):
        from tests.test_tip_rec_cascade import _turn

        return _turn(msg, ctx)

    def test_fix_category_reasks_that_field(self) -> None:
        _reply, ctx, routing, _ = self._turn("fix:category", self._drafted())

        self.save.assert_not_called()
        self.reader.assert_not_called()
        self.assertEqual(routing.get("tool_to_call"), "tip_ask_tweak")
        self.assertEqual(ctx["tip_tweak_pending"]["field"], "category")
        self.assertEqual(ctx["tip_tweak_pending"]["was"], "pediatric dentist")

    def test_the_correction_replaces_that_part(self) -> None:
        _r, ctx, _routing, _ = self._turn("fix:locality", self._drafted())
        with patch("app.tip_ask_draft.merge_ask_correction",
                   return_value="gentle pediatric dentist in winter park") as merge:
            _r2, _c2, routing2, _ = self._turn(
                "winter park", {"routing_phase": "listening", **ctx}
            )
        self.assertEqual(merge.call_args.kwargs["field"], "locality")
        self.assertEqual(merge.call_args.kwargs["was"], "Lake Nona")
        self.assertEqual(routing2.get("tool_to_call"), "tip_seek_neighbor_tip")
        self.assertIn("winter park", self.tips.call_args.kwargs["query"])
        self.save.assert_not_called()

    def test_the_kind_gate_reads_the_merged_ask_not_the_fix_alone(self) -> None:
        # e2e (capture/e30b): "one who takes Cigna instead" has no subject, so the kind
        # gate read no kind and let a pediatric dentist through on an orthodontist ask.
        _r, ctx, _routing, _ = self._turn("fix:qualifier", self._drafted())
        merged = "gentle orthodontist in lake nona who takes cigna"
        with patch("app.tip_ask_draft.merge_ask_correction", return_value=merged), \
             patch("app.reco_aspects.split_query_full",
                   return_value={"subject_kind": "orthodontist"}) as split, \
             patch("app.reco_kind_gate.keep_asked_kind",
                   side_effect=lambda rows, *_a, **_k: rows) as gate:
            self._turn("one who takes Cigna instead", {"routing_phase": "listening", **ctx})
        self.assertTrue(split.called)
        for call in split.call_args_list:
            self.assertEqual(call.args[0], merged)
        self.assertEqual(gate.call_args.args[1], "orthodontist")
        # The whole merged ask rides along, so a kind cut too short cannot reject the answer.
        self.assertEqual(gate.call_args.args[2], merged)

    def test_a_kindless_ask_is_still_gated_on_the_ask_itself(self) -> None:
        # Prod QA 2026-10-07: "gaming laptop" parsed with no subject_kind, the gate got
        # None, failed open, and a furniture store was offered as the answer.
        _r, ctx, _routing, _ = self._turn("fix:qualifier", self._drafted())
        merged = "gaming laptop"
        with patch("app.tip_ask_draft.merge_ask_correction", return_value=merged), \
             patch("app.reco_aspects.split_query_full",
                   return_value={"subject_kind": None}), \
             patch("app.reco_kind_gate.keep_asked_kind",
                   side_effect=lambda rows, *_a, **_k: rows) as gate:
            self._turn("a gaming one", {"routing_phase": "listening", **ctx})
        self.assertEqual(gate.call_args.args[1], merged)

    def test_unknown_field_is_not_a_chip(self) -> None:
        _reply, _ctx, routing, _ = self._turn("fix:password", self._drafted())
        self.assertNotEqual(routing.get("tool_to_call"), "tip_ask_tweak")

    def test_merge_prompt_names_the_part(self) -> None:
        from app import tip_ask_draft

        with patch("app.orchestrator.llm.llm_configured", return_value=True), \
             patch("app.orchestrator.llm.llm_json", return_value={"ask": "merged"}) as llm:
            out = tip_ask_draft.merge_ask_correction(
                prior_detail="dentist in lake nona", correction="winter park",
                field="locality", was="Lake Nona",
            )
        self.assertEqual(out, "merged")
        self.assertIn('"part_being_corrected": "locality (was: Lake Nona)"',
                      llm.call_args.kwargs["user_payload"])

    def test_stamp_records_the_chips(self) -> None:
        from app import tip_ask_draft

        ctx: dict = {}
        with patch("app.orchestrator.llm.llm_configured", return_value=False):
            tip_ask_draft.stamp_ask_draft(ctx, msg="dentist?", detail="dentist", category="health")
        self.assertEqual(ctx["ask_draft_pending"]["chips"],
                         [{"label": "health", "field": "category"}])


if __name__ == "__main__":
    unittest.main()
