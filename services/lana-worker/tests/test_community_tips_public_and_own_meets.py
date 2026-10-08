"""Two visibility rules, decided 2026-10-08.

1. A recommendation shared INTO a community is open to anyone looking, not only members
   (20270126120000). The worker must neither refuse a visitor nor promise sharers privacy.
2. A host sees their own meets on every events surface, marked "you're hosting" — the older
   activities preview still dropped them after browse stopped doing so (#217).
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

GYM = "00000000-0000-0000-0000-00000000f001"


class ShareConfirmationTests(unittest.TestCase):
    def _reply(self, circle):
        from app import tip_share

        with patch.object(tip_share, "compose_reply", side_effect=lambda **kw: kw) as cr:
            tip_share._tip_ready_reply("Coach Dana · personal trainer", circle)
        return cr.call_args.kwargs

    def test_a_community_tip_is_never_promised_as_private(self) -> None:
        kw = self._reply("CF Fitness")
        text = " ".join([kw["goal"], kw["fallback"], *kw["facts"]]).lower()
        for promise in ("only there", "stays inside", "only, not to the", "goes to cf fitness only"):
            self.assertNotIn(promise, text)
        self.assertIn("visible to anyone looking, members or not", " ".join(kw["facts"]))
        self.assertIn("never say it stays private", kw["goal"].lower())

    def test_an_untagged_tip_keeps_its_neighbourhood_wording(self) -> None:
        kw = self._reply("")
        self.assertIn("a neighbor asks", kw["goal"])
        self.assertEqual(kw["facts"], ["Tip ready: Coach Dana · personal trainer"])


class CommunityTipFeedTests(unittest.TestCase):
    def test_a_visitor_reads_a_communitys_tips_without_a_403(self) -> None:
        from app import main

        auth = SimpleNamespace(user_id="visitor", is_anonymous=False, home_block_id=None)
        body = main.TipFeedBody(place_id=GYM)
        with patch.object(main, "verify_auth", return_value=auth), patch(
            "app.community_surface.caller_affiliation_at", return_value=None
        ), patch("app.tip_feed.recent_tips", return_value=[]) as recent, patch(
            "app.tip_rec_line.attach_fit", side_effect=lambda tips, *a, **k: tips
        ):
            out = main.post_tips_recent(body=body, authorization="Bearer x")
        self.assertEqual(recent.call_args.kwargs["circle_place_id"], GYM)
        self.assertEqual(out["tips"], [])


class OwnMeetsInActivitiesPreviewTests(unittest.TestCase):
    def _preview(self, events, verified=True):
        from app import discovery_route as dr

        with patch.object(dr, "fetch_preview_events_on_block", return_value=events) as fetch:
            reply, ctx, _routing, _p = dr._show_activities_preview(
                ctx_base={}, block_id="blk1", block_label="Lake Nona", msg="what's happening",
                phone_verified=verified, user_id="me",
            )
        return reply, ctx, fetch

    def _ev(self, i, host):
        return {"id": f"e{i}", "title": f"Meet {i}", "host_id": host,
                "starts_at": "2026-10-10T18:00:00+00:00", "has_time": True}

    def test_own_meets_are_fetched_shown_and_marked(self) -> None:
        reply, ctx, fetch = self._preview([self._ev(1, "me"), self._ev(2, "someone")])
        self.assertNotIn("exclude_host_id", fetch.call_args.kwargs)
        marked = {p["activity_id"]: p.get("hosted_by_you") for p in ctx["activity_previews"]}
        self.assertEqual(marked, {"e1": True, "e2": False})
        self.assertIn("You're hosting 1 of these.", reply)

    def test_when_every_card_is_theirs_there_is_no_rsvp_invitation(self) -> None:
        reply, _ctx, _f = self._preview([self._ev(1, "me")])
        self.assertIn("This one's yours", reply)
        self.assertNotIn("RSVP", reply)


if __name__ == "__main__":
    unittest.main()
