"""The admitted fetch: one semantic query, distance as data, the rule decides (C3).

Two properties are load-bearing. `truncated` is read off the RAW page before anything is
dropped, so a full page still says it was cut —
"nothing further out matched" is never concluded from a page that was merely full. And a
dead embedding model returns None, never ([], False): the caller falls back rather than
telling the user their area is empty.

Everything is patched at its source module: the fetch imports lazily, so the service
client, the embed helper and the centroid are stubbed where they live.
"""

import inspect
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from app.activity_browse import _fetch_admitted_events

_LITERAL = "[0.1,0.2,0.3]"
_CENTROID = (28.36, -81.25)
_LIMIT = inspect.signature(_fetch_admitted_events).parameters["limit"].default


def _ev(eid, *, similarity=0.9, distance_meters=1000.0, host_id="host", **kw):
    row = {
        "id": eid,
        "title": f"meet {eid}",
        "starts_at": "2026-09-19T18:00:00+00:00",
        "has_time": True,
        "venue_name": "The Field",
        "cohort_tags": [],
        "host_id": host_id,
        "similarity": similarity,
        "distance_meters": distance_meters,
    }
    row.update(kw)
    return row


def _sb(rpc_rows=None, extra_rows=None, rpc_exc=None):
    """A service client whose .rpc() answers the search and whose .table() answers the
    recurrence / circle_place_ref lookup."""
    sb = MagicMock()
    rpc = MagicMock()
    if rpc_exc is not None:
        rpc.execute.side_effect = rpc_exc
    else:
        rpc.execute.return_value = MagicMock(data=rpc_rows or [])
    sb.rpc.return_value = rpc
    chain = MagicMock()
    for m in ("select", "in_"):
        getattr(chain, m).return_value = chain
    chain.execute.return_value = MagicMock(data=extra_rows or [])
    sb.table.return_value = chain
    return sb


class _FetchCase(unittest.TestCase):
    def _fetch(self, rows, *, extra_rows=None, embed=(0.1, 0.2, 0.3), me="me",
               interest="cricket", weekend_only=False, rpc_exc=None, **kw):
        sb = _sb(rows, extra_rows, rpc_exc)
        embed_spy = MagicMock(return_value=list(embed) if embed is not None else None)
        with patch("app.event_embed.embed_event_query", embed_spy), patch(
            "app.auth.service_client", return_value=sb
        ), patch("app.auth.jwt_user_id", return_value=me), patch(
            "app.discovery_route.block_centroid", return_value=_CENTROID
        ), patch("app.event_publish.roll_recurring_events"):
            result = _fetch_admitted_events(
                "jwt", "zip-32827", interest=interest, weekend_only=weekend_only, **kw
            )
        return result, sb, embed_spy

    def _rpc_args(self, sb):
        """The WIDE read — the first search. A floor-free local read follows it (4b)."""
        name, args = sb.rpc.call_args_list[0][0]
        self.assertEqual(name, "search_events_semantic")
        return args


class QueryEmbeddingTests(_FetchCase):
    def test_the_interest_text_is_what_gets_embedded(self):
        _result, _sb_, embed_spy = self._fetch([_ev("a")], interest="  cricket  ")
        embed_spy.assert_called_once_with("cricket")

    def test_the_rpc_receives_the_literal_the_radius_the_window_and_the_clamp(self):
        with patch.dict("os.environ", {"LANA_ACTIVITY_WINDOW_DAYS": "45"}):
            _result, sb, _spy = self._fetch([_ev("a")])
        args = self._rpc_args(sb)
        self.assertEqual(args["p_query_embedding"], _LITERAL)
        self.assertEqual(args["p_radius_meters"], 200_000.0)
        self.assertEqual(args["p_window"], "45 days")
        self.assertIsNone(args["p_circle_place_id"])
        self.assertEqual(args["p_lat"], _CENTROID[0])
        self.assertEqual(args["p_lng"], _CENTROID[1])
        # Ranked, never cut (20270127120000): no floor, and the distance trade-off is an ORDER.
        from app.activity_browse import _RANK_DISTANCE_BASE_M, _RANK_DISTANCE_K

        self.assertEqual(args["p_min_similarity"], 0.0)
        self.assertEqual(args["p_distance_k"], _RANK_DISTANCE_K)
        self.assertEqual(args["p_distance_base_m"], _RANK_DISTANCE_BASE_M)

    def test_the_page_size_is_the_keyword_default_and_overridable(self):
        _result, sb, _spy = self._fetch([_ev("a")])
        self.assertEqual(self._rpc_args(sb)["p_limit"], _LIMIT)
        _result, sb, _spy = self._fetch([_ev("a")], limit=25)
        self.assertEqual(self._rpc_args(sb)["p_limit"], 25)


class FallbackSignalTests(_FetchCase):
    def test_embed_returning_none_yields_none_and_warns(self):
        with self.assertLogs("app.activity_browse", level="WARNING") as captured:
            result, sb, _spy = self._fetch([_ev("a")], embed=None)
        self.assertIsNone(result)
        sb.rpc.assert_not_called()
        self.assertTrue(any("embed_unavailable" in line for line in captured.output))

    def test_none_is_distinct_from_an_empty_page(self):
        result, _sb_, _spy = self._fetch([])
        self.assertEqual(result, ([], False))

    def test_no_block_or_no_interest_is_none_without_embedding(self):
        embed_spy = MagicMock(return_value=[0.1])
        with patch("app.event_embed.embed_event_query", embed_spy):
            self.assertIsNone(_fetch_admitted_events("jwt", None, interest="cricket"))
            self.assertIsNone(_fetch_admitted_events("jwt", "zip-32827", interest="  "))
        embed_spy.assert_not_called()

    def test_an_unplaceable_block_is_none(self):
        with patch("app.event_embed.embed_event_query", return_value=[0.1]), patch(
            "app.discovery_route.block_centroid", return_value=None
        ), patch("app.auth.service_client", return_value=_sb([_ev("a")])) as sc:
            self.assertIsNone(_fetch_admitted_events("jwt", "zip-99999", interest="cricket"))
        sc.return_value.rpc.assert_not_called()

    def test_an_rpc_failure_is_none_not_empty(self):
        result, _sb_, _spy = self._fetch([], rpc_exc=RuntimeError("PGRST202"))
        self.assertIsNone(result)


class TruncationTests(_FetchCase):
    def test_a_full_page_is_truncated(self):
        (_rows, truncated), _sb_, _spy = self._fetch([_ev(f"e{i}") for i in range(_LIMIT)])
        self.assertTrue(truncated)

    def test_one_short_of_a_page_is_not(self):
        (_rows, truncated), _sb_, _spy = self._fetch(
            [_ev(f"e{i}") for i in range(_LIMIT - 1)]
        )
        self.assertFalse(truncated)

    def test_a_full_page_with_own_meets_is_still_truncated(self):
        rows = [_ev(f"e{i}", host_id="me" if i % 2 else "host") for i in range(_LIMIT)]
        (pool, truncated), _sb_, _spy = self._fetch(rows, me="me")
        self.assertTrue(truncated)
        # The page says how much there was; the matcher still reads only its pool.
        from app.activity_browse import _BROWSE_POOL

        self.assertEqual(len(pool), _BROWSE_POOL)

    def test_truncation_follows_an_overridden_limit(self):
        (_rows, truncated), _sb_, _spy = self._fetch([_ev("a"), _ev("b")], limit=2)
        self.assertTrue(truncated)


class RowShapeTests(_FetchCase):
    def test_own_hosted_meets_are_kept_and_marked(self):
        # Pouya, 2026-10-07: the SJSU calendar imported under his account vanished for
        # him alone. Own meets stay in the answer; the card says "you're hosting".
        (admitted, _t), sb, _spy = self._fetch(
            [_ev("mine", host_id="me"), _ev("theirs", host_id="host")], me="me"
        )
        self.assertEqual({r["id"]: r["hosted_by_you"] for r in admitted},
                         {"mine": True, "theirs": False})
        self.assertIsNone(self._rpc_args(sb).get("p_exclude_host_id"))

    def test_signed_out_marks_nothing_as_yours(self):
        (admitted, _t), _sb_, _spy = self._fetch([_ev("a", host_id="")], me=None)
        self.assertFalse(admitted[0]["hosted_by_you"])

    def test_distance_and_similarity_are_stamped_as_floats_on_every_row(self):
        (admitted, _t), _sb_, _spy = self._fetch(
            [_ev("a", similarity=1, distance_meters=1000), _ev("b", similarity=None, distance_meters=None)]
        )
        by_id = {r["id"]: r for r in admitted}
        self.assertEqual(by_id["a"]["similarity"], 1.0)
        self.assertIsInstance(by_id["a"]["similarity"], float)
        self.assertEqual(by_id["a"]["distance_meters"], 1000.0)
        self.assertIsInstance(by_id["a"]["distance_meters"], float)
        self.assertIsNone(by_id["b"]["similarity"])
        self.assertEqual(by_id["b"]["distance_meters"], 0.0)

    def test_recurrence_and_circle_place_ref_are_reattached(self):
        (admitted, _t), sb, _spy = self._fetch(
            [_ev("a"), _ev("b")],
            extra_rows=[
                {"id": "a", "recurrence": "weekly", "circle_place_ref": "place-1"},
                {"id": "b", "recurrence": None, "circle_place_ref": None},
            ],
        )
        by_id = {r["id"]: r for r in admitted}
        self.assertEqual(by_id["a"]["recurrence"], "weekly")
        self.assertEqual(by_id["a"]["circle_place_ref"], "place-1")
        self.assertIsNone(by_id["b"]["circle_place_ref"])
        chain = sb.table.return_value
        self.assertEqual(sorted(chain.in_.call_args[0][1]), ["a", "b"])
        self.assertIn("circle_place_ref", chain.select.call_args[0][0])

    def test_a_failed_column_lookup_leaves_the_rows_rather_than_dropping_them(self):
        sb = _sb([_ev("a")])
        sb.table.return_value.execute.side_effect = RuntimeError("boom")
        with patch("app.event_embed.embed_event_query", return_value=[0.1]), patch(
            "app.auth.service_client", return_value=sb
        ), patch("app.auth.jwt_user_id", return_value="me"), patch(
            "app.discovery_route.block_centroid", return_value=_CENTROID
        ), patch("app.event_publish.roll_recurring_events"), self.assertLogs(
            "app.activity_browse", level="ERROR"
        ):
            admitted, _t = _fetch_admitted_events("jwt", "zip-32827", interest="cricket")
        self.assertEqual([r["id"] for r in admitted], ["a"])
        self.assertNotIn("recurrence", admitted[0])

    def test_weekend_only_keeps_saturday_and_sunday_in_the_event_timezone(self):
        # Friday 23:30 ET is Saturday in UTC — must stay OUT; Saturday 10:00 ET stays in.
        friday_et = "2026-09-18T23:30:00-04:00"
        saturday_et = "2026-09-19T10:00:00-04:00"
        (admitted, _t), _sb_, _spy = self._fetch(
            [_ev("fri", starts_at=friday_et), _ev("sat", starts_at=saturday_et)],
            weekend_only=True,
        )
        self.assertEqual([r["id"] for r in admitted], ["sat"])


class RankedPoolTests(_FetchCase):
    """No similarity floor: SQL orders by meaning less distance, the matcher judges the top
    of that list. Prod 2026-10-08: "music" vs a Latin Jazz Concert scored 0.41 and the old
    0.495 floor removed an exact match before anything could judge it."""

    def test_a_low_scoring_exact_match_reaches_the_matcher(self):
        (pool, _t), _sb_, _spy = self._fetch(
            [_ev("concert", similarity=0.41, distance_meters=5000.0)]
        )
        self.assertEqual([r["id"] for r in pool], ["concert"])

    def test_the_pool_keeps_the_rpc_rank_order(self):
        rows = [_ev("near-ok", similarity=0.45, distance_meters=2000.0),
                _ev("far-great", similarity=0.8, distance_meters=150_000.0),
                _ev("near-weak", similarity=0.30, distance_meters=1000.0)]
        (pool, _t), _sb_, _spy = self._fetch(rows)
        self.assertEqual([r["id"] for r in pool], ["near-ok", "far-great", "near-weak"])

    def test_the_matcher_reads_at_most_its_pool_of_ranked_meets(self):
        from app.activity_browse import _BROWSE_POOL

        rows = [_ev(f"e{i}", similarity=0.9 - i * 0.001) for i in range(_BROWSE_POOL + 10)]
        (pool, _t), _sb_, _spy = self._fetch(rows)
        self.assertEqual([r["id"] for r in pool], [f"e{i}" for i in range(_BROWSE_POOL)])

    def test_meets_without_a_vector_follow_nearest_first_and_are_never_hidden(self):
        rows = [_ev("ranked", similarity=0.5),
                _ev("fresh-far", similarity=None, distance_meters=90_000.0),
                _ev("fresh-near", similarity=None, distance_meters=800.0)]
        (pool, _t), _sb_, _spy = self._fetch(rows)
        self.assertEqual([r["id"] for r in pool], ["ranked", "fresh-near", "fresh-far"])

    def test_a_single_search_runs(self):
        _r, sb, _spy = self._fetch([_ev("a")])
        self.assertEqual(sb.rpc.call_count, 1)
