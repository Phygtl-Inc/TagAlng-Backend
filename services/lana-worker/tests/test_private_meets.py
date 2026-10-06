"""§29 private meets: the Privacy card is stamped, persisted, and actually hides the meet.

(a) event-setup stamps is_private onto event_draft AND the event_settings mirror, as its
    own key — never folded into allow_attendee_share.
(b) publish writes it into create_event's p_fields.
(c) every worker-side discovery read of public.events filters it out, and the
    publish-time community mail-out is skipped for it. (The SQL readers are covered by
    the migration's own scenario run, not here.)
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.auth import AuthSession
from app.models import EventDraft, EventSetupRequest


class _Q:
    """A PostgREST builder stand-in: every chained call is recorded and returns self."""

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[tuple[str, tuple]] = []

    def __getattr__(self, name: str):
        def _m(*args, **kwargs):
            self.calls.append((name, args))
            return self

        return _m

    def execute(self):
        return SimpleNamespace(data=self.rows, count=len(self.rows))

    def filtered_private(self) -> bool:
        return ("eq", ("is_private", False)) in self.calls


class _SB:
    def __init__(self, tables: dict[str, _Q]) -> None:
        self.tables = tables

    def table(self, name: str) -> _Q:
        return self.tables.setdefault(name, _Q())

    def rpc(self, *_a, **_k):
        return _Q()


# ── (a) event-setup ────────────────────────────────────────────────────────────


class TestEventSetupStampsPrivacy(unittest.TestCase):
    def _submit(self, body: EventSetupRequest, ctx: dict | None = None) -> dict:
        from app import main

        saved: dict = {}
        auth = AuthSession(
            user_id="u-host", is_anonymous=False, phone_verified=True, home_block_id=None
        )
        with (
            patch.object(main, "verify_auth", return_value=auth),
            patch.object(
                main, "get_session_for_user", return_value={"context": dict(ctx or {})}
            ),
            patch.object(
                main,
                "update_session_context",
                side_effect=lambda _sid, c: saved.update(c),
            ),
        ):
            out = main.set_event_setup("s-1", body, authorization="Bearer t")
        self.assertEqual(out, {"ok": True})
        return saved

    def test_private_on_is_stamped_on_draft_and_mirror(self) -> None:
        ctx = self._submit(EventSetupRequest(is_private=True, allow_attendee_share=True))
        self.assertIs(ctx["event_draft"]["is_private"], True)
        self.assertIs(ctx["event_settings"]["is_private"], True)
        # Distinct from the Sharing card: a private meet whose attendees may forward it.
        self.assertIs(ctx["event_draft"]["allow_attendee_share"], True)
        self.assertIs(ctx["event_settings"]["allow_attendee_share"], True)

    def test_private_off_is_stamped_false(self) -> None:
        ctx = self._submit(
            EventSetupRequest(is_private=False),
            ctx={"event_draft": {"is_private": True}},
        )
        self.assertIs(ctx["event_draft"]["is_private"], False)
        self.assertIs(ctx["event_settings"]["is_private"], False)

    def test_absent_leaves_the_draft_alone(self) -> None:
        ctx = self._submit(EventSetupRequest(), ctx={"event_draft": {"is_private": True}})
        self.assertIs(ctx["event_draft"]["is_private"], True)

    def test_sharing_off_does_not_make_it_private(self) -> None:
        ctx = self._submit(EventSetupRequest(allow_attendee_share=False))
        self.assertIs(ctx["event_draft"]["allow_attendee_share"], False)
        self.assertNotIn("is_private", ctx["event_draft"])


class TestDraftCarriesPrivacy(unittest.TestCase):
    def test_seed_defaults_public(self) -> None:
        from app.lana_unified_pipeline import _seed_setup_defaults

        ed: dict = {}
        _seed_setup_defaults(ed)
        self.assertIs(ed["is_private"], False)
        ed = {"is_private": True}
        _seed_setup_defaults(ed)
        self.assertIs(ed["is_private"], True)

    def test_publish_whitelist_keeps_it(self) -> None:
        from app.lana_unified_pipeline import _EVENT_DRAFT_FIELDS

        self.assertIn("is_private", _EVENT_DRAFT_FIELDS)

    def test_echoed_on_event_draft(self) -> None:
        self.assertIs(EventDraft(title="x", is_private=True).is_private, True)
        self.assertIsNone(EventDraft(title="x").is_private)

    def test_a_redraw_never_drops_or_sets_privacy(self) -> None:
        from app.lana_ui import merge_event_drafts

        kept = merge_event_drafts({"title": "Picnic", "is_private": True}, {"title": "Picnic 2"})
        self.assertIs(kept["is_private"], True)
        # A model's output cannot flip it — only the setup card writes it.
        self.assertNotIn(
            "is_private", merge_event_drafts({"title": "Picnic"}, {"is_private": True})
        )

    def test_the_word_private_never_sets_privacy(self) -> None:
        # The Sharing card's chip parser predates §29; it must not grow into this one.
        from app.lana_unified_pipeline import _parse_event_settings

        settings: dict = {}
        _parse_event_settings("keep it private", settings)
        self.assertNotIn("is_private", settings)


# ── (b) publish ────────────────────────────────────────────────────────────────


class TestPublishWritesPrivacy(unittest.TestCase):
    def _fields(self, draft: EventDraft) -> dict:
        from app.event_publish import build_create_event_fields

        with (
            patch("app.event_publish.resolve_event_location", return_value=(1.0, 2.0, "b")),
            patch("app.event_publish._valid_purpose_ids", return_value=set()),
            patch("app.event_publish._ai_event_description", return_value=None),
            patch("app.event_publish._ai_cover_emoji", return_value=None),
        ):
            return build_create_event_fields("u-1", draft)

    def test_private_true_reaches_create_event(self) -> None:
        f = self._fields(EventDraft(title="Book club", is_private=True))
        self.assertIs(f["is_private"], True)

    def test_private_false_reaches_create_event(self) -> None:
        f = self._fields(EventDraft(title="Book club", is_private=False))
        self.assertIs(f["is_private"], False)

    def test_legacy_draft_uses_the_column_default(self) -> None:
        self.assertNotIn("is_private", self._fields(EventDraft(title="Book club")))


# ── (c) discovery reads + community mail ───────────────────────────────────────


class TestDiscoveryReadsFilterPrivate(unittest.TestCase):
    def test_block_browse_and_look_flow(self) -> None:
        from app import discovery_route

        sb = _SB({})
        with (
            patch.object(discovery_route, "service_client", return_value=sb),
            patch("app.event_publish.roll_recurring_events"),
            patch.object(discovery_route, "event_distances_near_block", return_value={"e1": 10.0}),
        ):
            discovery_route.fetch_preview_events_on_block("b1")
        self.assertTrue(sb.tables["events"].filtered_private())

    def test_community_scope_feed(self) -> None:
        from app import community_scope

        sb = _SB({})
        with (
            patch("app.auth.service_client", return_value=sb),
            patch("app.event_publish.roll_recurring_events"),
        ):
            community_scope.community_events("11111111-1111-1111-1111-111111111111")
        self.assertTrue(sb.tables["events"].filtered_private())

    def test_community_profile_and_cards(self) -> None:
        from app import community_surface

        for pid in ("11111111-1111-1111-1111-111111111111", "ChIJ-google-ref"):
            sb = _SB({})
            with patch.object(community_surface, "service_client", return_value=sb):
                community_surface._events_at_place(pid, limit=5)
            self.assertTrue(sb.tables["events"].filtered_private(), pid)

    def test_upcoming_count_in_zip_ask(self) -> None:
        from app import activity_browse

        sb = _SB({})
        with patch("app.auth.service_client", return_value=sb):
            activity_browse._count_upcoming_events_anywhere()
        self.assertTrue(sb.tables["events"].filtered_private())


class TestCommunityMailSkipsPrivate(unittest.TestCase):
    def _stamp(self, is_private: bool) -> tuple[int, list]:
        from app import event_place

        sb = _SB({
            "circle_affiliations": _Q([{"id": "a1"}]),
            "places": _Q([{"name": "OrangeTheory"}]),
            "events": _Q([{
                "starts_at": "2027-01-20T18:00:00+00:00",
                "has_time": True,
                "venue_name": "Gym",
                "cover_emoji": "🏋️",
                "is_private": is_private,
            }]),
        })
        mailed: list = []
        with (
            patch.object(event_place, "service_client", return_value=sb),
            patch("app.notifications._user_contact", return_value=(None, "Ana")),
            patch(
                "app.notifications.mail_community_members",
                side_effect=lambda *a, **k: mailed.append(a) or 3,
            ),
        ):
            sent = event_place.stamp_event_community("e1", "p1", "u-host", "Spin")
        # The meet is tagged with its community either way — only the announcement differs.
        self.assertIn(("update", ({"circle_place_ref": "p1"},)), sb.tables["events"].calls)
        return sent, mailed

    def test_private_meet_mails_nobody(self) -> None:
        sent, mailed = self._stamp(True)
        self.assertEqual(sent, 0)
        self.assertEqual(mailed, [])

    def test_public_meet_still_mails_members(self) -> None:
        sent, mailed = self._stamp(False)
        self.assertEqual(sent, 3)
        self.assertEqual(len(mailed), 1)


if __name__ == "__main__":
    unittest.main()
