"""Tests for claim_backlink — the creator proof-of-control path.

Owner: @pouya. These cover the matching logic and the failure taxonomy, which is where
the user-visible behaviour lives. The network and the database are not exercised here on
purpose: a verification test that needs Instagram to be up is a test that fails for
reasons that have nothing to do with us.

The cases worth the most are the two NEGATIVES:
  * a login wall must not read as "no link" (an honest creator would be rejected)
  * someone else's Lana link must not read as proof (a copied link would verify)
"""

from __future__ import annotations

import urllib.error

import pytest

from app import claim_backlink as cb


# ── matching ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "body",
    [
        "https://get.lana.help/run-with-maya",
        "http://get.lana.help/run-with-maya",
        "get.lana.help/run-with-maya",
        "https://www.get.lana.help/run-with-maya",
        "https://get.lana.help/run-with-maya/",
        "GET.LANA.HELP/RUN-WITH-MAYA",
        '<a href="https://get.lana.help/run-with-maya">my community</a>',
        "bio line one\nhttps://get.lana.help/run-with-maya?utm_source=ig\nline three",
    ],
)
def test_accepts_every_shape_a_bio_editor_produces(body):
    """A profile editor will mangle the link: scheme dropped, www added, slash appended,
    a tracking param bolted on. All of those are still the creator putting our link up."""
    assert any(p.search(body) for p in cb._backlink_patterns("run-with-maya"))


@pytest.mark.parametrize(
    "body",
    [
        "https://get.lana.help/run-with-maya-official",   # longer handle, not ours
        "https://get.lana.help/runwithmaya",              # hyphens are significant
        "https://lana.help/run-with-maya",                # marketing host, not the app
        "https://get.lana.help/",                         # no handle at all
        "run with maya",
        "",
    ],
)
def test_rejects_near_misses(body):
    assert not any(p.search(body) for p in cb._backlink_patterns("run-with-maya"))


def test_handle_must_match_exactly_not_as_a_prefix():
    """`/maya` must not be satisfied by `/maya-running`. Prefix matching would let one
    creator's link verify a different, shorter handle."""
    body = "https://get.lana.help/maya-running"
    assert not any(p.search(body) for p in cb._backlink_patterns("maya"))


# ── failure taxonomy ────────────────────────────────────────────────────────

def test_login_wall_is_not_a_missing_link(monkeypatch):
    """403 means we could not see the profile, NOT that the link is absent. Conflating
    the two rejects honest creators on platforms that wall anonymous visitors."""
    def _raise(*_a, **_k):
        raise urllib.error.HTTPError("https://x", 403, "Forbidden", {}, None)

    monkeypatch.setattr(cb.urllib.request, "urlopen", _raise)
    body, reason = cb._fetch_public("https://linktr.ee/maya")
    assert body is None
    assert reason == cb.R_NOT_PUBLIC


def test_rate_limit_is_also_not_a_missing_link(monkeypatch):
    def _raise(*_a, **_k):
        raise urllib.error.HTTPError("https://x", 429, "Too Many", {}, None)

    monkeypatch.setattr(cb.urllib.request, "urlopen", _raise)
    _, reason = cb._fetch_public("https://linktr.ee/maya")
    assert reason == cb.R_NOT_PUBLIC


def test_non_url_is_rejected_before_any_network_call():
    for bad in ("javascript:alert(1)", "file:///etc/passwd", "not a url", ""):
        body, reason = cb._fetch_public(bad)
        assert body is None
        assert reason == cb.R_BAD_URL


def test_walled_provider_routes_to_review_not_rejection():
    """Instagram serves a wall to logged-out visitors. The creator has done nothing
    wrong, so the answer is 'we cannot check this automatically', never 'no'."""
    assert "instagram" not in cb.PUBLIC_RENDER_PROVIDERS
    assert "linktree" in cb.PUBLIC_RENDER_PROVIDERS


def test_identity_count_fails_closed(monkeypatch):
    """A read error must not let a claimant attach past the cap."""
    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(cb, "service_client", _boom)
    assert cb._identity_count("place-1") >= cb.MAX_IDENTITIES_PER_PLACE


def test_result_always_carries_a_reason_code():
    """The UI renders these. An opaque failure is the thing we are not building."""
    for ok, reason in ((True, cb.R_OK), (False, cb.R_NOT_FOUND)):
        out = cb._result(ok, reason)
        assert out["verified"] is ok
        assert out["reason"] == reason
        assert out["policy"] == "backlink-v1"


def test_no_reservation_short_circuits_before_fetching(monkeypatch):
    """Never fetch a third-party profile for someone who has not reserved the handle —
    that is an unauthenticated request we can be asked to justify."""
    monkeypatch.setattr(cb, "_active_reservation", lambda *_a, **_k: None)

    def _should_not_run(*_a, **_k):
        raise AssertionError("fetched without a reservation")

    monkeypatch.setattr(cb, "_fetch_public", _should_not_run)

    out = cb.verify_backlink(
        place_id="p1", handle="run-with-maya", provider="linktree",
        canonical_url="https://linktr.ee/maya", username="maya", user_id="u1",
    )
    assert out["verified"] is False
    assert out["reason"] == cb.R_NO_RESERVATION
