"""The candidate pool must not be cut by start date.

The bug: the location query hands over the 50 nearest events, and
fetch_preview_events_on_block then ran `.order("starts_at").limit(40)`. The date sort
sat in front of the limit, so it did not merely order the rows — it decided which ten
were DELETED, and the ten deleted were always the furthest in the future. Nothing
downstream ever saw them, including the topic matcher. A violin recital three miles away
six weeks out disappeared and browse reported nothing nearby.

That is the same shape as the distance cut it sits next to: a silent drop taken before
anything knows what the user asked for. Sorting by date is fine; deciding what to drop by
date is not.

The fix: a caller that passes `pool` filters downstream, so it now gets every candidate
the location query found, and presentation order is restored in Python afterwards.
"""

import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from app.discovery_route import _NEARBY_CANDIDATE_CAP, fetch_preview_events_on_block


def _rows(n):
    """n candidates, STRICTLY ascending by date — 'e0' is soonest, 'e{n-1}' furthest.

    One day apart with no wrap-around, so "the soonest five" is unambiguously e0..e4.
    An earlier version cycled the day-of-month and produced ties that made the expected
    order depend on the helper rather than on the code under test.
    """
    base = datetime(2026, 10, 1, 18, 0, 0)
    return [
        {
            "id": f"e{i}",
            "title": f"Event {i}",
            "starts_at": (base + timedelta(days=i)).strftime("%Y-%m-%dT%H:%M:%S"),
        }
        for i in range(n)
    ]


class _Harness(unittest.TestCase):
    def _fetch(self, rows, *, distances=None, **kwargs):
        """Run the real fetch with the DB stubbed. Returns (result, query chain)."""
        chain = MagicMock()
        for m in ("select", "eq", "gte", "in_", "neq", "order", "limit"):
            getattr(chain, m).return_value = chain
        # Model what Postgres actually does: apply .order() BEFORE .limit(). Without
        # this the harness cannot tell the fix from the bug — a mock that ignores
        # ordering passes either way, and the whole point is that the order decided
        # which rows survived the limit.
        def _execute():
            data = list(rows)
            if chain.order.called:
                key = chain.order.call_args[0][0]
                data.sort(key=lambda r: str(r.get(key) or ""))
            return MagicMock(data=data[: chain.limit.call_args[0][0]])

        chain.execute.side_effect = _execute
        sb = MagicMock()
        sb.table.return_value = chain
        near = distances if distances is not None else {r["id"]: 1000.0 for r in rows}
        with patch("app.discovery_route.service_client", return_value=sb), patch(
            "app.discovery_route.event_distances_near_block", return_value=near
        ), patch("app.event_publish.roll_recurring_events"):
            out = fetch_preview_events_on_block("zip-90001", **kwargs)
        return out, chain


class TheDateCutIsGoneTests(_Harness):
    def test_a_near_event_far_in_the_future_still_reaches_the_matcher(self):
        """THE bug, pinned. 50 candidates, and the one three miles away is dead last by
        date. Under the old code it was one of the ten deleted at the SQL limit and the
        matcher never saw it; browse then said nothing was nearby."""
        rows = _rows(_NEARBY_CANDIDATE_CAP)
        rows[-1] = {
            "id": "violin",
            "title": "Violin recital at the community hall",
            # Six weeks out — the furthest of the 50, and the first to be dropped.
            "starts_at": "2026-12-31T19:00:00",
        }
        distances = {r["id"]: 90000.0 for r in rows}
        distances["violin"] = 4828.0  # three miles

        out, _chain = self._fetch(rows, distances=distances, limit=40, pool=40)

        titles = [r["title"] for r in out]
        self.assertIn("Violin recital at the community hall", titles)
        self.assertEqual(len(out), _NEARBY_CANDIDATE_CAP)
        # And it is still marked as the near one, so a widened pass cannot claim it.
        violin = next(r for r in out if r["id"] == "violin")
        self.assertEqual(violin["distance_meters"], 4828.0)

    def test_the_database_is_not_asked_to_order_by_date(self):
        """The mechanism. `.order("starts_at")` in front of `.limit()` is what turned a
        sort into a deletion; re-adding it would restore the bug silently."""
        _out, chain = self._fetch(_rows(50), limit=40, pool=40)
        chain.order.assert_not_called()

    def test_a_pool_caller_asks_for_the_whole_candidate_set(self):
        _out, chain = self._fetch(_rows(50), limit=40, pool=40)
        self.assertEqual(chain.limit.call_args[0][0], _NEARBY_CANDIDATE_CAP)

    def test_a_small_pool_is_still_widened_to_the_cap(self):
        """`pool` means "I filter downstream", not "give me exactly this many" — a caller
        that asks for 10 would otherwise re-introduce the same silent drop."""
        _out, chain = self._fetch(_rows(50), limit=10, pool=10)
        self.assertEqual(chain.limit.call_args[0][0], _NEARBY_CANDIDATE_CAP)


class PresentationOrderIsUnchangedTests(_Harness):
    def test_rows_come_back_soonest_first(self):
        """The sort moved to Python; it did not disappear. Card order on screen is the
        same as before."""
        shuffled = [
            {"id": "c", "title": "C", "starts_at": "2026-10-30T10:00:00"},
            {"id": "a", "title": "A", "starts_at": "2026-10-01T10:00:00"},
            {"id": "b", "title": "B", "starts_at": "2026-10-15T10:00:00"},
        ]
        out, _chain = self._fetch(shuffled, limit=40, pool=40)
        self.assertEqual([r["id"] for r in out], ["a", "b", "c"])

    def test_a_missing_or_unparseable_date_sorts_last_without_raising(self):
        rows = [
            {"id": "bad", "title": "Bad", "starts_at": None},
            {"id": "good", "title": "Good", "starts_at": "2026-10-01T10:00:00"},
            {"id": "junk", "title": "Junk"},
        ]
        out, _chain = self._fetch(rows, limit=40, pool=40)
        self.assertEqual(out[0]["id"], "good")
        self.assertEqual({r["id"] for r in out[1:]}, {"bad", "junk"})


class NoChangeBelowTheCapTests(_Harness):
    """The pilot today. Nothing to truncate means nothing to change."""

    def test_a_handful_of_events_is_untouched(self):
        rows = _rows(5)
        out, _chain = self._fetch(rows, limit=40, pool=40)
        self.assertEqual([r["id"] for r in out], ["e0", "e1", "e2", "e3", "e4"])

    def test_exactly_forty_candidates_all_come_back(self):
        out, _chain = self._fetch(_rows(40), limit=40, pool=40)
        self.assertEqual(len(out), 40)


class CallersWithoutAPoolAreUnaffectedTests(_Harness):
    """discovery_route.py:5803 (block preview) and :9219 (RSVP-by-utterance) pass neither
    `limit` nor `pool`. They present rows directly rather than filtering them, so "the 5
    soonest" is their product decision, not a silent drop — and it must not change."""

    def test_the_default_call_still_takes_the_soonest_five(self):
        out, chain = self._fetch(_rows(30))
        self.assertEqual(chain.limit.call_args[0][0], 15)  # limit * 3, as before
        self.assertEqual(len(out), 5)
        self.assertEqual([r["id"] for r in out], ["e0", "e1", "e2", "e3", "e4"])

    def test_an_explicit_limit_without_a_pool_is_unchanged(self):
        out, chain = self._fetch(_rows(30), limit=3)
        self.assertEqual(chain.limit.call_args[0][0], 9)
        self.assertEqual([r["id"] for r in out], ["e0", "e1", "e2"])

    def test_they_keep_their_date_order_in_sql_where_it_selects(self):
        """For a caller that renders rows directly, "the soonest few" IS the product
        decision, so the order has to stay in SQL. Removing it there would hand them an
        arbitrary slice of the candidates instead of the soonest ones — the same silent
        drop this commit removes, one table over. Caught by running the suite against
        the pre-fix code, not by review."""
        _out, chain = self._fetch(_rows(30))
        chain.order.assert_called_once_with("starts_at")


class WeekendFilterStillAppliesAfterTheSortTests(_Harness):
    def test_weekend_only_narrows_the_returned_rows(self):
        rows = [
            {"id": "sat", "title": "Sat", "starts_at": "2026-10-03T18:00:00"},
            {"id": "tue", "title": "Tue", "starts_at": "2026-10-06T18:00:00"},
            {"id": "sun", "title": "Sun", "starts_at": "2026-10-04T18:00:00"},
        ]
        out, _chain = self._fetch(rows, limit=40, pool=40, weekend_only=True)
        self.assertEqual([r["id"] for r in out], ["sat", "sun"])


if __name__ == "__main__":
    unittest.main()
