"""Three costs every list screen paid before doing any work (2026-10-20).

1. The "why Lana sees a fit" lines: a feed or discovery page waited on the model whenever a
   row had evidence but no cached line. With `defer_fit` the page answers from the cache
   and says `fit_pending`; the client asks again without the flag to have them authored.
2. verify_auth asked Supabase Auth over the network before every endpoint. Tokens signed
   with the project's asymmetric key are now checked locally; Auth is still the judge of
   anything the local check cannot settle.
3. call_rpc opened a fresh connection (TCP + TLS) per call; it now reuses one.
"""

import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import jwt
from cryptography.hazmat.primitives.asymmetric import ec

from app import auth, community_fit_line, supabase_rpc
from app.community_fit_line import attach_fit_lines
from tests.test_tip_rec_line import _tip

URL = "https://example.supabase.co"
USER = "11111111-1111-1111-1111-111111111111"


class DeferredTipFitTests(unittest.TestCase):
    def _run(self, rows, *, compose, cached=None):
        from app import peer_rec_line, tip_rec_line
        from tests.test_peer_rec_line import _Supabase

        store = {"rows": cached or [], "upserts": []}
        with (
            patch.object(peer_rec_line, "service_client", return_value=_Supabase(store)),
            patch("app.claims_persist.fetch_active_claim_labels", return_value=["Soccer on Saturdays"]),
            patch("app.lang_pref.get_user_preferred_language", return_value="en"),
            patch("app.orchestrator.llm.llm_configured", return_value=True),
            patch("app.orchestrator.llm.composer_model", return_value="test-model"),
            patch("app.orchestrator.llm.llm_json", return_value={"lines": ["Right after class."]}) as call,
        ):
            pending = tip_rec_line.attach_fit("u-1", rows, compose=compose)
        return pending, call, store

    def test_deferred_feed_never_calls_the_model_and_says_lines_are_pending(self):
        row = _tip()
        pending, call, store = self._run([row], compose=False)
        self.assertTrue(pending)
        self.assertEqual(call.call_count, 0)
        self.assertEqual(store["upserts"], [])
        self.assertIsNone(row["rec_line"])

    def test_the_follow_up_call_authors_them(self):
        row = _tip()
        pending, call, _ = self._run([row], compose=True)
        self.assertFalse(pending)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(row["rec_line"], "Right after class.")

    def test_cached_lines_are_served_and_nothing_is_pending(self):
        first = _tip()
        _, _, store = self._run([first], compose=True)
        cached = [{**store["upserts"][0][0], "id": "rec-1"}]
        again = _tip()
        pending, call, _ = self._run([again], compose=False, cached=cached)
        self.assertFalse(pending)
        self.assertEqual(call.call_count, 0)
        self.assertEqual(again["rec_line"], "Right after class.")


class DeferredCommunityFitTests(unittest.TestCase):
    def _rows(self):
        return [{"place_id": "p1", "_fit_basis": {"shared": ["Trail running"], "members": 12}}]

    def _run(self, *, compose, cached=None):
        rows = self._rows()
        with (
            patch.object(community_fit_line, "_store"),
            patch.object(
                community_fit_line, "_compose", return_value=[("They run your trails.", ["Trail running"])]
            ) as comp,
            patch.object(community_fit_line, "_cached", return_value=cached or {}),
            patch("app.lang_pref.get_user_preferred_language", return_value="en"),
        ):
            pending = attach_fit_lines("u-caller", rows, compose=compose)
        return rows, pending, comp

    def test_deferred_discovery_skips_the_model(self):
        rows, pending, comp = self._run(compose=False)
        self.assertTrue(pending)
        comp.assert_not_called()
        self.assertIsNone(rows[0]["fit_line"])
        self.assertNotIn("_fit_basis", rows[0])

    def test_without_the_flag_it_still_authors_in_one_call(self):
        rows, pending, comp = self._run(compose=True)
        self.assertFalse(pending)
        comp.assert_called_once()
        self.assertEqual(rows[0]["fit_line"], "They run your trails.")


class DeferFitEndpointTests(unittest.TestCase):
    def _auth(self):
        return SimpleNamespace(user_id="u-1", is_anonymous=False, home_block_id=None)

    def _feed(self, **body):
        from app import main

        with (
            patch.object(main, "verify_auth", return_value=self._auth()),
            patch("app.tip_feed.recent_tips", return_value=[_tip()]),
            patch("app.tip_rec_line.attach_fit", return_value=True) as fit,
            patch("app.peer_discovery_surface.stamp_connection_state"),
        ):
            out = main.post_tips_recent(body=main.TipFeedBody(**body), authorization="Bearer x")
        return out, fit

    def test_the_feed_passes_the_flag_through_and_reports_pending(self):
        out, fit = self._feed(defer_fit=True)
        self.assertIs(fit.call_args.kwargs["compose"], False)
        self.assertIs(out["fit_pending"], True)

    def test_an_old_client_still_gets_every_line_in_one_call(self):
        _, fit = self._feed()
        self.assertIs(fit.call_args.kwargs["compose"], True)

    def test_discovery_passes_the_flag_through_and_reports_pending(self):
        from app import main

        with (
            patch.object(main, "verify_auth", return_value=self._auth()),
            patch("app.community_discovery.discover_communities", return_value=[]),
            patch("app.community_discovery.radius_meters", return_value=8000),
            patch("app.community_discovery.attach_description_and_area"),
            patch("app.community_affinity.attach_affinity"),
            patch("app.community_fit_line.attach_fit_lines", return_value=True) as fit,
        ):
            out = main.post_circles_discover(
                body=main.CommunityDiscoverBody(defer_fit=True), authorization="Bearer x"
            )
        self.assertIs(fit.call_args.kwargs["compose"], False)
        self.assertTrue(out.fit_pending)


class LocalTokenCheckTests(unittest.TestCase):
    def setUp(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        signing = SimpleNamespace(key=self.key.public_key())
        jwks = MagicMock()
        jwks.get_signing_key_from_jwt.return_value = signing
        self.patches = [
            patch.object(auth, "SUPABASE_URL", URL),
            patch.object(auth, "SUPABASE_ANON_KEY", "anon"),
            patch.object(auth, "SUPABASE_SERVICE_ROLE_KEY", "service"),
            patch.object(auth, "_jwks_client", return_value=jwks),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def _token(self, *, key=None, alg="ES256", **over):
        claims = {
            "sub": USER,
            "aud": "authenticated",
            "iss": f"{URL}/auth/v1",
            "exp": int(time.time()) + 600,
            "is_anonymous": False,
        }
        claims.update(over)
        if alg == "HS256":
            return jwt.encode(claims, "x" * 32, algorithm="HS256")
        return jwt.encode(claims, key or self.key, algorithm=alg)

    def test_a_valid_token_is_accepted(self):
        self.assertEqual(auth._verified_claims(self._token())["sub"], USER)

    def test_anything_unsettled_is_left_to_auth(self):
        stranger = ec.generate_private_key(ec.SECP256R1())
        for token in (
            self._token(exp=int(time.time()) - 5),
            self._token(aud="somebody-else"),
            self._token(iss="https://evil.example/auth/v1"),
            self._token(key=stranger),
            self._token(alg="HS256"),
            "not-a-jwt",
        ):
            self.assertIsNone(auth._verified_claims(token), token[:40])

    def _verify(self, token, profile):
        with (
            patch.object(auth, "_load_user_profile", return_value=profile) as load,
            patch.object(
                auth, "_fetch_auth_user", return_value={"id": USER, "is_anonymous": False}
            ) as remote,
        ):
            session = auth.verify_auth(f"Bearer {token}")
        return session, remote, load

    def test_a_verified_account_never_asks_auth(self):
        session, remote, load = self._verify(
            self._token(), {"email_verified_at": "2026-09-01T00:00:00Z", "home_block_id": "b1"}
        )
        remote.assert_not_called()
        load.assert_called_once()
        self.assertEqual(session.user_id, USER)
        self.assertTrue(session.phone_verified)
        self.assertEqual(session.home_block_id, "b1")

    def test_a_guest_never_asks_auth(self):
        session, remote, _ = self._verify(self._token(is_anonymous=True), {"home_block_id": None})
        remote.assert_not_called()
        self.assertTrue(session.is_anonymous)
        self.assertFalse(session.phone_verified)

    def test_a_real_account_not_yet_stamped_asks_auth_for_its_confirmation(self):
        _, remote, load = self._verify(self._token(), {"home_block_id": None})
        remote.assert_called_once()
        load.assert_called_once()

    def test_no_users_row_asks_auth(self):
        _, remote, _ = self._verify(self._token(), {})
        remote.assert_called_once()

    def test_a_guest_with_no_users_row_asks_auth(self):
        # A deleted account's token still verifies; the missing row is what sends it to Auth.
        _, remote, _ = self._verify(self._token(is_anonymous=True), {})
        remote.assert_called_once()

    def test_a_bad_token_asks_auth_which_refuses_it(self):
        from fastapi import HTTPException

        with (
            patch.object(auth, "_load_user_profile") as load,
            patch.object(
                auth, "_fetch_auth_user", side_effect=HTTPException(401, "invalid_session")
            ),
        ):
            with self.assertRaises(HTTPException):
                auth.verify_auth("Bearer " + self._token(exp=int(time.time()) - 5))
        load.assert_not_called()


class RpcConnectionReuseTests(unittest.TestCase):
    def test_one_client_serves_every_call(self):
        supabase_rpc._http.cache_clear()
        ok = MagicMock(status_code=200, text='{"ok": true}')
        ok.json.return_value = {"ok": True}
        try:
            with (
                patch.object(supabase_rpc, "SUPABASE_URL", URL),
                patch.object(supabase_rpc, "SUPABASE_ANON_KEY", "anon"),
                patch.object(supabase_rpc.httpx, "Client") as client_cls,
            ):
                client_cls.return_value.post.return_value = ok
                supabase_rpc.call_rpc("jwt", "a", {})
                supabase_rpc.call_rpc("jwt", "b", {})
            self.assertEqual(client_cls.call_count, 1)
            self.assertEqual(client_cls.return_value.post.call_count, 2)
        finally:
            supabase_rpc._http.cache_clear()


if __name__ == "__main__":
    unittest.main()
