"""A meet's fit with the viewer: score, proven threads, and the authored line (§50, §54).

What must hold:
- fit_score is read off the SQL intersection (score_events_fit_for_user) and passed
  through untouched — None stays None (unscored), never coerced to 0.
- /lana/circles/profile's upcoming_events carry that score, so a community-scoped meet is
  scored like a nearby one.
- The line is composed over the MATCHED threads only — never the meet's title or its
  other tags — served from cache when the overlap is unchanged, and absent (not canned)
  when nothing is shared or the compose fails.
- The migration that is the definition in force for the preview pair carries the honest
  distance formatter (§52) — the regression that reverted it came from a stale body.

No test here touches a database or a model.
"""

import os
import re
import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from app import event_fit
from app.auth import AuthSession

AUTH = "Bearer test-token"

_MIGRATIONS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "supabase", "migrations")


def _rpc_rows(rows):
    sb = MagicMock()
    sb.rpc.return_value.execute.return_value = MagicMock(data=rows)
    return sb


ROW_E1 = {
    "event_id": "e1",
    "affinity_matched_tags": ["Trail running", "Sourdough"],
    "affinity_match_count": 2,
    "affinity_total_count": 5,
    "fit_score": 0.8,
}
ROW_UNSCORED = {
    "event_id": "e2",
    "affinity_matched_tags": [],
    "affinity_match_count": 0,
    "affinity_total_count": 3,
    "fit_score": None,
}


class TestFetchEventFit(unittest.TestCase):
    def test_reads_the_batch_rpc_and_keeps_unscored_as_none(self) -> None:
        sb = _rpc_rows([ROW_E1, ROW_UNSCORED])
        with patch.object(event_fit, "service_client", return_value=sb):
            out = event_fit.fetch_event_fit("u1", ["e1", "e2"])
        sb.rpc.assert_called_once_with(
            "score_events_fit_for_user", {"p_user_id": "u1", "p_event_ids": ["e1", "e2"]}
        )
        self.assertEqual(out["e1"]["fit_score"], 0.8)
        self.assertEqual(out["e1"]["affinity_matched_tags"], ["Trail running", "Sourdough"])
        self.assertIsNone(out["e2"]["fit_score"])

    def test_zero_is_a_real_score(self) -> None:
        sb = _rpc_rows([{**ROW_UNSCORED, "fit_score": 0}])
        with patch.object(event_fit, "service_client", return_value=sb):
            self.assertEqual(event_fit.fetch_event_fit("u1", ["e2"])["e2"]["fit_score"], 0.0)

    def test_failure_is_an_unscored_list_not_an_error(self) -> None:
        sb = MagicMock()
        sb.rpc.side_effect = RuntimeError("boom")
        with patch.object(event_fit, "service_client", return_value=sb):
            self.assertEqual(event_fit.fetch_event_fit("u1", ["e1"]), {})

    def test_no_viewer_never_reads(self) -> None:
        with patch.object(event_fit, "service_client") as sc:
            self.assertEqual(event_fit.fetch_event_fit("", ["e1"]), {})
            sc.assert_not_called()


class TestAttachEventFit(unittest.TestCase):
    def test_rows_carry_the_served_score(self) -> None:
        rows = [{"event_id": "e1"}, {"event_id": "e2"}, {"event_id": "e9"}]
        with patch.object(event_fit, "service_client", return_value=_rpc_rows([ROW_E1, ROW_UNSCORED])):
            event_fit.attach_event_fit("u1", rows)
        self.assertEqual([r["fit_score"] for r in rows], [0.8, None, None])


class TestCommunityProfileRowsAreScored(unittest.TestCase):
    """§54 acceptance: a community-scoped meet carries the score it carries by radius."""

    def test_profile_rows_carry_fit_score_for_the_viewer(self) -> None:
        from app import community_surface as cs

        raw = [
            {"id": "e1", "title": "Trail + bread", "starts_at": "2099-01-01T10:00:00+00:00"},
            {"id": "e2", "title": "Chess", "starts_at": "2099-01-02T10:00:00+00:00"},
        ]
        with (
            patch.object(cs, "_events_at_place", return_value=raw),
            patch.object(cs, "_going_counts", return_value={}),
            patch.object(event_fit, "fetch_event_fit", return_value={"e1": {"fit_score": 0.8}}) as fetch,
        ):
            rows, _week = cs._event_rows_for_profile("p1", "viewer-1")
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.args[0], "viewer-1")
        by_id = {r["event_id"]: r for r in rows}
        self.assertEqual(by_id["e1"]["fit_score"], 0.8)
        self.assertIsNone(by_id["e2"]["fit_score"])

    def test_profile_route_puts_fit_score_on_the_wire(self) -> None:
        from app.main import CommunityProfileBody, post_circles_profile

        data = {
            "place_id": "p1",
            "upcoming_events": [
                {"event_id": "e1", "title": "Trail + bread", "fit_score": 0.6},
                {"event_id": "e2", "title": "Chess", "fit_score": None},
            ],
        }
        auth = AuthSession(user_id="u1", is_anonymous=False, phone_verified=True, home_block_id=None)
        with (
            patch("app.main.verify_auth", return_value=auth),
            patch("app.community_surface.community_profile", return_value=data),
        ):
            res = post_circles_profile(CommunityProfileBody(place_id="p1"), authorization=AUTH)
        self.assertEqual([e.fit_score for e in res.upcoming_events], [0.6, None])


class TestEventFitLine(unittest.TestCase):
    def _run(self, hit, *, cached=None, composed=None):
        calls = {}

        def _compose(basis_list, lang, system=None):
            calls["basis"] = basis_list
            calls["system"] = system
            return composed if composed is not None else []

        with (
            patch.object(event_fit, "fetch_event_fit", return_value={"e1": hit} if hit else {}),
            patch.object(event_fit, "_cached", return_value=cached),
            patch.object(event_fit, "_store", return_value="line-1") as store,
            patch.object(event_fit, "_compose", side_effect=_compose),
            patch("app.lang_pref.get_user_preferred_language", return_value="en"),
        ):
            out = event_fit.event_fit_line("u1", "e1")
        return out, calls, store

    def test_line_is_composed_over_the_matched_threads_only(self) -> None:
        out, calls, store = self._run(
            {"affinity_matched_tags": ["Trail running", "Sourdough"], "fit_score": 0.8},
            # The model's own chip ("Trails") is ignored: the pills are the proven threads.
            composed=[("You run trails and bake bread, and that's this one.", ["Trails"])],
        )
        # The evidence is the intersection and nothing else: no title, no other tags.
        self.assertEqual(calls["basis"], [{"shared": ["Trail running", "Sourdough"]}])
        self.assertIs(calls["system"], event_fit._SYSTEM)
        self.assertEqual(out["rec_line"], "You run trails and bake bread, and that's this one.")
        self.assertEqual(out["rec_chips"], ["Trail running", "Sourdough"])
        self.assertEqual(out["rec_chips"], out["affinity_matched_tags"])
        self.assertEqual(out["fit_score"], 0.8)
        self.assertEqual(out["rec_id"], "line-1")
        store.assert_called_once()

    def test_nothing_shared_means_no_line_and_no_model_call(self) -> None:
        out, calls, store = self._run({"affinity_matched_tags": [], "fit_score": 0.0})
        self.assertNotIn("basis", calls)
        self.assertIsNone(out["rec_line"])
        self.assertEqual(out["fit_score"], 0.0)
        store.assert_not_called()

    def test_cache_hit_costs_no_model_call(self) -> None:
        out, calls, _store = self._run(
            {"affinity_matched_tags": ["Sourdough"], "fit_score": 0.6},
            cached={"id": "c1", "line": "Cached line.", "chips": ["Bread baking"]},
        )
        self.assertNotIn("basis", calls)
        self.assertEqual((out["rec_line"], out["rec_id"]), ("Cached line.", "c1"))
        # A row cached under the old prompt carries a paraphrased chip; never served.
        self.assertEqual(out["rec_chips"], ["Sourdough"])

    def test_failed_compose_leaves_no_canned_line(self) -> None:
        out, _calls, store = self._run(
            {"affinity_matched_tags": ["Sourdough"], "fit_score": 0.6}, composed=[]
        )
        self.assertIsNone(out["rec_line"])
        self.assertEqual(out["affinity_matched_tags"], ["Sourdough"])
        # No sentence, but the pills still show the proven thread.
        self.assertEqual(out["rec_chips"], ["Sourdough"])
        store.assert_not_called()

    def test_prompt_has_no_template_to_copy(self) -> None:
        # Every e2e line ended "…, and that's what this one is about." — the example frame
        # the prompt itself quoted. The prompt must not hand the model a sentence to fill.
        self.assertNotIn("that's what this one is about\",", event_fit._SYSTEM)
        self.assertNotIn("You're into…", event_fit._SYSTEM)
        self.assertNotIn("CHIPS", event_fit._SYSTEM)
        self.assertIn("Name ONLY threads in \"shared\"", event_fit._SYSTEM)

    def test_new_prompt_does_not_reuse_lines_cached_under_the_old_one(self) -> None:
        from app.peer_rec_line import _basis_sig

        seen = {}

        def _cached(user_id, event_id, lang, sig):
            seen["sig"] = sig
            return None

        with (
            patch.object(event_fit, "fetch_event_fit", return_value={"e1": {"affinity_matched_tags": ["Sourdough"], "fit_score": 0.6}}),
            patch.object(event_fit, "_cached", side_effect=_cached),
            patch.object(event_fit, "_compose", return_value=[]),
            patch("app.lang_pref.get_user_preferred_language", return_value="en"),
        ):
            event_fit.event_fit_line("u1", "e1")
        self.assertNotEqual(seen["sig"], _basis_sig({"shared": ["Sourdough"]}))

    def test_unreadable_meet_is_an_empty_block(self) -> None:
        out, calls, _store = self._run(None)
        self.assertEqual(
            (out["affinity_matched_tags"], out["fit_score"], out["rec_line"]), ([], None, None)
        )
        self.assertNotIn("basis", calls)

    def test_route_validates_the_id_and_serves_the_block(self) -> None:
        from app.main import EventFitLineBody, post_event_fit_line

        auth = AuthSession(user_id="u1", is_anonymous=False, phone_verified=True, home_block_id=None)
        with patch("app.main.verify_auth", return_value=auth):
            with self.assertRaises(HTTPException) as ctx:
                post_event_fit_line(EventFitLineBody(event_id="nope"), authorization=AUTH)
            self.assertEqual(ctx.exception.status_code, 400)
            eid = "00000000-0000-0000-0000-0000000000e1"
            block = {
                "event_id": eid,
                "affinity_matched_tags": ["Sourdough"],
                "fit_score": 0.6,
                "rec_line": "A line.",
                "rec_chips": ["Sourdough"],
                "rec_id": "r1",
            }
            with patch("app.event_fit.event_fit_line", return_value=block) as fn:
                res = post_event_fit_line(EventFitLineBody(event_id=eid), authorization=AUTH)
        fn.assert_called_once_with("u1", eid)
        self.assertEqual(res.rec_line, "A line.")
        self.assertEqual(res.affinity_matched_tags, ["Sourdough"])


def _latest_definition(fn: str) -> tuple[str, str]:
    """(file, body) of the newest migration that defines public.<fn>(."""
    pat = re.compile(rf"create (or replace )?function public\.{re.escape(fn)}\(", re.I)
    hits = []
    for name in sorted(os.listdir(_MIGRATIONS)):
        if not name.endswith(".sql"):
            continue
        text = open(os.path.join(_MIGRATIONS, name), encoding="utf-8").read()
        for m in pat.finditer(text):
            end = text.find("\n$$;", m.end())
            hits.append((name, text[m.start() : end]))
    assert hits, fn
    return hits[-1]


class TestDefinitionsInForce(unittest.TestCase):
    """Static guards on the SQL in force — the class of bug §52 was (a stale body copied
    over a fix) is invisible to every other test in this repo."""

    def test_previews_use_the_honest_distance_formatter(self) -> None:
        for fn in ("get_event_preview", "get_event_preview_authed"):
            _name, body = _latest_definition(fn)
            self.assertIn("humanize_distance_text(v_distance, p_locale)", body, fn)
            self.assertNotIn("' min walk'", body, fn)

    def test_previews_ship_the_intersection_and_score(self) -> None:
        _n, authed = _latest_definition("get_event_preview_authed")
        self.assertIn("'affinity_matched_tags'", authed)
        self.assertIn("public.event_viewer_fit(", authed)
        _n, anon = _latest_definition("get_event_preview")
        self.assertIn("'affinity_matched_tags', '[]'::jsonb", anon)
        self.assertIn("'fit_score', null", anon)

    def test_nearby_scores_through_the_same_helper(self) -> None:
        _n, body = _latest_definition("get_nearby_activities_authed")
        self.assertIn("fit_score numeric", body)
        self.assertIn("public.event_viewer_fit(e.cohort_tags, v_caller)", body)

    def test_meet_payloads_name_their_community(self) -> None:
        for fn in ("get_my_contributions", "get_peer_profile"):
            _n, body = _latest_definition(fn)
            self.assertIn("public.event_community(e.circle_place_ref, e.host_id)", body, fn)

    def test_the_helper_is_not_client_callable(self) -> None:
        name, _body = _latest_definition("event_viewer_fit")
        text = open(os.path.join(_MIGRATIONS, name), encoding="utf-8").read()
        self.assertIn(
            "revoke all on function public.event_viewer_fit(text[], uuid) from public, anon, authenticated;",
            text,
        )


if __name__ == "__main__":
    unittest.main()
