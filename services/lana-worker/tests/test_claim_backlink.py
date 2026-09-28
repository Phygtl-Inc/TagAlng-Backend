"""Tests for claim_backlink — the creator proof-of-control path.

Owner: @pouya. These cover matching, the three ways a creator actually puts the link up,
and the SSRF guard on the hop. Network and database are not exercised on purpose: a
verification test that needs Instagram to be up fails for reasons unrelated to us.

The cases worth the most are the NEGATIVES:
  * a login wall must not read as "no link"        (an honest creator would be rejected)
  * "not up yet" must not read as "rejected"       (that is the common path, not a failure)
  * someone else's Lana link must not read as proof (a copied link would verify)
  * a bio containing 169.254.169.254 must not be followed (SSRF)
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
    assert cb._backlink_pattern("run-with-maya").search(body)


@pytest.mark.parametrize(
    "body",
    [
        "https://get.lana.help/run-with-maya-official",
        "https://get.lana.help/runwithmaya",
        "https://lana.help/run-with-maya",   # marketing host, not the app
        "https://get.lana.help/",
        "run with maya",
        "",
    ],
)
def test_rejects_near_misses(body):
    assert not cb._backlink_pattern("run-with-maya").search(body)


def test_handle_must_match_exactly_not_as_a_prefix():
    """`/maya` must not be satisfied by `/maya-running`."""
    assert not cb._backlink_pattern("maya").search("https://get.lana.help/maya-running")


# ── case 1: link already up ─────────────────────────────────────────────────

def test_finds_link_on_the_profile_itself(monkeypatch):
    monkeypatch.setattr(
        cb, "_fetch_public",
        lambda url, is_hop=False: ("bio: https://get.lana.help/run-with-maya", None),
    )
    matched, where, reason = cb.find_backlink("https://linktr.ee/maya", "run-with-maya")
    assert matched and reason is None
    assert where == "https://linktr.ee/maya"


# ── case 3: the link lives one level down ───────────────────────────────────

def test_follows_one_hop_into_a_linktree(monkeypatch):
    """Our own copy says 'add it to your existing Linktree', so the Instagram bio holds a
    Linktree URL and OUR link is inside THAT. One fetch of the profile finds nothing."""
    pages = {
        "https://example.com/profile": '<a href="https://linktr.ee/maya">links</a>',
        "https://linktr.ee/maya": "https://get.lana.help/run-with-maya",
    }
    monkeypatch.setattr(cb, "_fetch_public",
                        lambda url, is_hop=False: (pages.get(url), None if url in pages else cb.R_FETCH_FAILED))
    monkeypatch.setattr(cb, "_safe_host", lambda _u: True)

    matched, where, reason = cb.find_backlink("https://example.com/profile", "run-with-maya")
    assert matched and reason is None
    assert where == "https://linktr.ee/maya"


def test_never_hops_twice(monkeypatch):
    """Two hops is a crawler, not a verification."""
    pages = {
        "https://example.com/a": '<a href="https://b.example.com/b">next</a>',
        "https://b.example.com/b": '<a href="https://c.example.com/c">next</a>',
        "https://c.example.com/c": "https://get.lana.help/run-with-maya",
    }
    monkeypatch.setattr(cb, "_fetch_public",
                        lambda url, is_hop=False: (pages.get(url), None if url in pages else cb.R_FETCH_FAILED))
    monkeypatch.setattr(cb, "_safe_host", lambda _u: True)

    matched, _, reason = cb.find_backlink("https://example.com/a", "run-with-maya")
    assert matched is None
    assert reason == cb.R_NOT_FOUND
    assert cb.MAX_HOPS == 1


def test_aggregators_are_tried_first():
    body = ('<a href="https://random.example.com/x">x</a>'
            '<a href="https://linktr.ee/maya">links</a>')
    candidates = cb._hop_candidates(body, "https://example.com/profile")
    assert candidates[0] == "https://linktr.ee/maya"


# ── SSRF ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "url",
    [
        "https://169.254.169.254/latest/meta-data/",  # cloud metadata
        "https://127.0.0.1/admin",
        "https://localhost/",
        "https://10.0.0.5/internal",
        "https://192.168.1.1/",
        "http://linktr.ee/maya",                      # hops are https-only
    ],
)
def test_ssrf_guard_blocks_non_public_targets(url):
    """We follow a link found on somebody ELSE'S page, so the target is
    attacker-influenced. A bio containing a metadata address must never be fetched."""
    assert cb._safe_host(url) is False


def test_hop_refuses_unsafe_target(monkeypatch):
    monkeypatch.setattr(cb, "_safe_host", lambda _u: False)
    body, reason = cb._fetch_public("https://169.254.169.254/", is_hop=True)
    assert body is None
    assert reason == cb.R_BAD_URL


# ── case 2: "I'll add it later" ─────────────────────────────────────────────

def test_not_found_is_pending_not_rejected(monkeypatch):
    """The common path is claim the handle, THEN edit the bio — so the first check runs
    before the link exists. Rendering that as a rejection kills the funnel."""
    monkeypatch.setattr(cb, "find_backlink",
                        lambda *_a, **_k: (None, "https://linktr.ee/maya", cb.R_NOT_FOUND))
    monkeypatch.setattr(cb, "_update", lambda *_a, **_k: None)

    out = cb._run_check(
        {"id": "i1", "canonical_url": "https://linktr.ee/maya", "check_attempts": 0},
        "run-with-maya",
    )
    assert out["verified"] is False
    assert out["reason"] == cb.R_PENDING
    assert out["attempts"] == 1


def test_gives_up_after_the_attempt_cap(monkeypatch):
    monkeypatch.setattr(cb, "find_backlink",
                        lambda *_a, **_k: (None, None, cb.R_NOT_FOUND))
    monkeypatch.setattr(cb, "_update", lambda *_a, **_k: None)

    out = cb._run_check(
        {"id": "i1", "canonical_url": "https://x", "check_attempts": cb.MAX_ATTEMPTS - 1},
        "run-with-maya",
    )
    assert out["reason"] == cb.R_GAVE_UP


def test_backoff_stretches_then_stops():
    assert cb._next_check_after(1) is not None
    assert cb._next_check_after(cb.MAX_ATTEMPTS) is None


# ── failure taxonomy ────────────────────────────────────────────────────────

def test_login_wall_is_not_a_missing_link(monkeypatch):
    def _raise(*_a, **_k):
        raise urllib.error.HTTPError("https://x", 403, "Forbidden", {}, None)

    monkeypatch.setattr(cb.urllib.request, "urlopen", _raise)
    body, reason = cb._fetch_public("https://linktr.ee/maya")
    assert body is None
    assert reason == cb.R_NOT_PUBLIC


def test_someone_elses_lana_link_is_its_own_reason(monkeypatch):
    monkeypatch.setattr(
        cb, "_fetch_public",
        lambda url, is_hop=False: ("https://get.lana.help/someone-else", None),
    )
    matched, _, reason = cb.find_backlink("https://linktr.ee/maya", "run-with-maya")
    assert matched is None
    assert reason == cb.R_WRONG_HANDLE


def test_non_url_is_rejected_before_any_network_call():
    for bad in ("javascript:alert(1)", "file:///etc/passwd", "not a url", ""):
        body, reason = cb._fetch_public(bad)
        assert body is None
        assert reason == cb.R_BAD_URL


def test_walled_provider_routes_to_review_not_rejection():
    assert "instagram" not in cb.PUBLIC_RENDER_PROVIDERS
    assert "linktree" in cb.PUBLIC_RENDER_PROVIDERS


def test_identity_count_fails_closed(monkeypatch):
    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(cb, "service_client", _boom)
    assert cb._identity_count("place-1") >= cb.MAX_IDENTITIES_PER_PLACE


def test_no_reservation_short_circuits_before_fetching(monkeypatch):
    """Never fetch a third-party profile for someone who has not reserved the handle."""
    monkeypatch.setattr(cb, "_active_reservation", lambda *_a, **_k: None)

    def _should_not_run(*_a, **_k):
        raise AssertionError("fetched without a reservation")

    monkeypatch.setattr(cb, "find_backlink", _should_not_run)

    out = cb.start_backlink_claim(
        place_id="p1", handle="run-with-maya", provider="linktree",
        canonical_url="https://linktr.ee/maya", username="maya", user_id="u1",
    )
    assert out["verified"] is False
    assert out["reason"] == cb.R_NO_RESERVATION


def test_result_always_carries_a_reason_code():
    for ok, reason in ((True, cb.R_OK), (False, cb.R_PENDING)):
        out = cb._result(ok, reason)
        assert out["verified"] is ok
        assert out["reason"] == reason
        assert out["policy"] == "backlink-v2"
