import unittest
from unittest.mock import patch

from app.profile_portrait import _portrait_cached, portrait_from_claims


class TestPortraitFromClaims(unittest.TestCase):
    def setUp(self) -> None:
        _portrait_cached.cache_clear()

    def test_no_claims_asks_nothing(self) -> None:
        with patch("app.profile_portrait._portrait_cached") as cached:
            self.assertIsNone(portrait_from_claims([]))
            cached.assert_not_called()

    def test_facts_are_deduped_and_a_childs_thread_is_marked_without_their_name(self) -> None:
        """The parent is the only reader, but a child's name still never reaches the
        model — the portrait can't spend what it was never given."""
        claims = [
            {"label": "Plays badminton"},
            {"label": "plays badminton"},  # same thread, different casing
            {
                "label": "Does karate",
                "subject_kind": "child",
                "subject_name": "Ayla",
                "subject_age": 6,
            },
        ]
        with patch("app.profile_portrait._portrait_cached", return_value="x") as cached:
            portrait_from_claims(claims, area="Lake Nona")
        facts = cached.call_args.kwargs["facts"]
        self.assertEqual(facts, ("Plays badminton", "Does karate (about their child)"))
        self.assertNotIn("Ayla", " ".join(facts))
        self.assertEqual(cached.call_args.kwargs["area"], "Lake Nona")

    def test_without_an_llm_there_is_no_canned_line(self) -> None:
        with patch("app.orchestrator.llm.llm_configured", return_value=False):
            self.assertIsNone(portrait_from_claims([{"label": "Plays badminton"}]))


class TestDashboardBackfill(unittest.TestCase):
    """One implementation for every reader: the drawer's route and the chat claims card
    both come through fetch_identity_dashboard."""

    def test_summary_is_composed_only_when_the_rpc_has_none(self) -> None:
        from app import layer1_handlers

        dash = {"claims": [{"label": "Plays badminton"}], "profile": {"home_zip": "32827"}}
        with (
            patch.object(layer1_handlers, "call_rpc", return_value=dict(dash)),
            patch("app.profile_portrait.portrait_from_claims", return_value="A badminton player."),
        ):
            self.assertEqual(
                layer1_handlers.fetch_identity_dashboard("jwt")["mapped_summary"],
                "A badminton player.",
            )

        intake = {**dash, "mapped_summary": "From the intake."}
        with (
            patch.object(layer1_handlers, "call_rpc", return_value=dict(intake)),
            patch("app.profile_portrait.portrait_from_claims", return_value="composed") as comp,
        ):
            self.assertEqual(
                layer1_handlers.fetch_identity_dashboard("jwt")["mapped_summary"],
                "From the intake.",
            )
            comp.assert_not_called()


class TestPublicPortraitDisclosure(unittest.TestCase):
    """The peer line is written from public, self-subject claims and nothing else."""

    CLAIMS = [
        {"label": "Plays badminton", "disclosure": "public", "subject_kind": "self"},
        {"label": "Attends St. Luke's", "disclosure": "mutual", "subject_kind": "self"},
        {"label": "Does karate", "disclosure": "public", "subject_kind": "child"},
        {"label": "Runs at dawn", "disclosure": "public", "subject_kind": "self"},
    ]

    def test_only_public_self_threads_reach_the_model(self) -> None:
        from app.profile_portrait import _facts_for_public

        # A mutual-only thread is not for this audience, and a child's thread is not
        # about them — dropped outright rather than trusted to a prompt rule.
        self.assertEqual(
            _facts_for_public(self.CLAIMS), ["Plays badminton", "Runs at dawn"]
        )

    def test_the_owners_line_still_sees_everything(self) -> None:
        from app.profile_portrait import _facts_for_self

        facts = _facts_for_self(self.CLAIMS)
        self.assertIn("Does karate (about their child)", facts)
        self.assertIn("Attends St. Luke's", facts)

    def test_the_two_lines_never_share_a_fingerprint(self) -> None:
        from app.profile_portrait import _facts_for_public, _facts_for_self, _fingerprint

        self.assertNotEqual(
            _fingerprint("self", _facts_for_self(self.CLAIMS)),
            _fingerprint("public", _facts_for_public(self.CLAIMS)),
        )

    def test_identical_fact_sets_still_key_apart(self) -> None:
        """One public self-thread makes both lists identical — the scope keeps the
        keys distinct so nobody can compare the wrong pair."""
        from app.profile_portrait import _fingerprint

        facts = ["Plays badminton"]
        self.assertNotEqual(_fingerprint("self", facts), _fingerprint("public", facts))


class TestStoredPortraits(unittest.TestCase):
    from unittest.mock import MagicMock  # noqa: PLC0415 — local to the fixture below

    def _run(self, stored, claims):
        from unittest.mock import MagicMock

        from app import profile_portrait as pp

        sb = MagicMock()
        with patch.object(pp, "_stored", return_value=stored), patch.object(
            pp, "_claims_for", return_value=claims
        ), patch.object(pp, "_compose", side_effect=lambda prompt, facts, **kw: "LINE"),             patch("app.auth.service_client", return_value=sb):
            pp.refresh_portraits("u1")
        return sb

    def test_matching_fingerprints_write_nothing(self) -> None:
        from app.profile_portrait import _facts_for_public, _facts_for_self, _fingerprint

        claims = [{"label": "Plays badminton", "disclosure": "public", "subject_kind": "self"}]
        stored = {
            "portrait": "old",
            "portrait_key": _fingerprint("self", _facts_for_self(claims), None),
            "public_portrait": "old",
            "public_portrait_key": _fingerprint("public", _facts_for_public(claims)),
        }
        sb = self._run(stored, claims)
        sb.table.assert_not_called()  # no model call, no write

    def test_a_new_thread_rewrites_both_lines(self) -> None:
        claims = [{"label": "Plays badminton", "disclosure": "public", "subject_kind": "self"}]
        sb = self._run({"portrait_key": "stale", "public_portrait_key": "stale"}, claims)
        patch_arg = sb.table.return_value.update.call_args.args[0]
        self.assertEqual(patch_arg["portrait"], "LINE")
        self.assertEqual(patch_arg["public_portrait"], "LINE")
        self.assertNotEqual(patch_arg["portrait_key"], patch_arg["public_portrait_key"])

    def test_private_only_threads_leave_the_public_line_alone(self) -> None:
        claims = [{"label": "Sees a therapist", "disclosure": "private", "subject_kind": "self"}]
        sb = self._run({"portrait_key": "stale", "public_portrait_key": "stale"}, claims)
        patch_arg = sb.table.return_value.update.call_args.args[0]
        self.assertIn("portrait", patch_arg)
        self.assertNotIn("public_portrait", patch_arg)


class TestRetractionClearsFirst(unittest.TestCase):
    def test_a_retraction_nulls_both_lines_then_refreshes(self) -> None:
        from unittest.mock import MagicMock

        from app import profile_portrait as pp

        sb = MagicMock()
        with patch("app.auth.service_client", return_value=sb), patch.object(
            pp, "schedule_portrait_refresh"
        ) as sched:
            pp.clear_portraits("u1")
        patch_arg = sb.table.return_value.update.call_args.args[0]
        # False, not merely stale: it names a thread they just took back.
        self.assertEqual(set(patch_arg.values()), {None})
        self.assertEqual(
            set(patch_arg), {"portrait", "portrait_key", "public_portrait", "public_portrait_key"}
        )
        sched.assert_called_once_with("u1")


if __name__ == "__main__":
    unittest.main()
