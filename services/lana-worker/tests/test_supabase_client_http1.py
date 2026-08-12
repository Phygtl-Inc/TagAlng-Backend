"""The Supabase client must not speak HTTP/2.

postgrest hardcodes http2=True. httpcore's HTTP/2 `has_expired()` only checks the
keepalive timer, while HTTP/1.1 additionally probes the socket ("idle but readable" =
server hung up) and evicts it. So an h2 connection closed by Supabase while a slow LLM
turn ran stayed in the pool and blew up on reuse with
`ReadError: [Errno 35] Resource temporarily unavailable` — losing an already-computed
reply. Regression guard: assert we inject an HTTP/1.1 client, and that it is still a
reused singleton (keepalive is why the cache exists).
"""

import unittest
from unittest.mock import patch

import app.auth as auth

# app.auth binds SUPABASE_* at import, and another test module may import it first —
# so patch the module attributes rather than the environment.
_URL = "https://example.supabase.co"
_KEY = "test-service-key"


class TestSupabaseClientIsHttp11(unittest.TestCase):
    def setUp(self):
        auth._cached_service_client.cache_clear()

    def tearDown(self):
        auth._cached_service_client.cache_clear()

    def test_postgrest_pool_has_http2_disabled(self):
        pool = auth._supabase(_URL, _KEY).postgrest.session._transport._pool
        self.assertFalse(
            pool._http2,
            "postgrest re-enabled HTTP/2 — stale pooled connections will fail on reuse",
        )

    def test_client_is_still_a_reused_singleton(self):
        with patch.object(auth, "SUPABASE_URL", _URL), patch.object(
            auth, "SUPABASE_SERVICE_ROLE_KEY", _KEY
        ):
            self.assertIs(auth.service_client(), auth.service_client())


if __name__ == "__main__":
    unittest.main()
