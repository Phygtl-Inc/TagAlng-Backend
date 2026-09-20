"""Directed neighbor asks — the rules that stop this becoming spam, and the copy binding.

Two classes of failure are covered here, because both are invisible in a single happy turn:

  · the anti-spam predicates only misbehave ACROSS asks (one neighbor absorbing every one),
  · and a receipt only lies when the outcome and the sentence disagree, which no
    end-to-end test asserts unless it is written down.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from app.tip_ask_route import _pick, eligible_recipients
from app.tip_surface import build_ask_receipt


def _ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


class _Query:
    """Chainable supabase-py stub: every builder call is a no-op, execute() returns rows."""

    def __init__(self, rows, raises=False):
        self._rows, self._raises = rows, raises

    def __getattr__(self, _name):
        return lambda *a, **k: self

    def execute(self):
        if self._raises:
            raise RuntimeError("db down")
        return SimpleNamespace(data=self._rows)


def _client(*, mutes=(), history=(), mutes_raise=False):
    tables = {
        "tip_ask_mutes": _Query([{"user_id": u} for u in mutes], raises=mutes_raise),
        "tip_ask_recipients": _Query(list(history)),
    }
    return SimpleNamespace(table=lambda name: tables[name])


# ── anti-spam ────────────────────────────────────────────────────────────────


def test_muted_neighbor_is_never_asked():
    with patch("app.tip_ask_route.service_client", lambda: _client(mutes=["u1"])):
        assert eligible_recipients(["u1", "u2"]) == {"u2"}


def test_cooldown_excludes_someone_asked_this_week():
    history = [{"recipient_user_id": "u1", "status": "sent", "created_at": _ago(2)}]
    with patch("app.tip_ask_route.service_client", lambda: _client(history=history)):
        assert eligible_recipients(["u1", "u2"]) == {"u2"}


def test_cooldown_releases_after_the_window():
    """The rotation property: last week's best pick is askable again later, which is what
    keeps the 4th-best neighbor from never getting a turn."""
    history = [{"recipient_user_id": "u1", "status": "sent", "created_at": _ago(30)}]
    with patch("app.tip_ask_route.service_client", lambda: _client(history=history)):
        assert eligible_recipients(["u1"]) == {"u1"}


def test_two_unanswered_asks_stop_the_asking():
    history = [
        {"recipient_user_id": "u1", "status": "sent", "created_at": _ago(30)},
        {"recipient_user_id": "u1", "status": "sent", "created_at": _ago(60)},
    ]
    with patch("app.tip_ask_route.service_client", lambda: _client(history=history)):
        assert eligible_recipients(["u1"]) == set()


def test_answering_clears_the_fatigue_streak():
    """Someone who helped must not be retired as a non-responder — the reason
    save_local_signal marks answered at the single save point."""
    history = [
        {"recipient_user_id": "u1", "status": "answered", "created_at": _ago(30)},
        {"recipient_user_id": "u1", "status": "sent", "created_at": _ago(60)},
    ]
    with patch("app.tip_ask_route.service_client", lambda: _client(history=history)):
        assert eligible_recipients(["u1"]) == {"u1"}


def test_unreadable_mutes_fail_closed():
    """A mute we cannot read must never become a send."""
    with patch("app.tip_ask_route.service_client", lambda: _client(mutes_raise=True)):
        assert eligible_recipients(["u1"]) == set()


def test_no_service_client_sends_to_nobody():
    with patch("app.tip_ask_route.service_client", lambda: None):
        assert eligible_recipients(["u1"]) == set()


# ── the pick ─────────────────────────────────────────────────────────────────


def _candidates(n):
    return [{"user_id": f"u{i}", "name": f"N{i}", "quote": "q"} for i in range(n)]


def _llm(picks):
    return patch("app.orchestrator.llm.llm_json", lambda **k: {"picks": picks}), patch(
        "app.orchestrator.llm.llm_configured", lambda: True
    )


def test_pick_ignores_ids_that_were_never_shortlisted():
    """A hallucinated user id must not become mail to a real person."""
    p1, p2 = _llm([{"user_id": "ghost", "reason": "r"}, {"user_id": "u1", "reason": "r"}])
    with p1, p2:
        got = _pick("good books", _candidates(3))
    assert [g["user_id"] for g in got] == ["u1"]


def test_pick_never_exceeds_three():
    p1, p2 = _llm([{"user_id": f"u{i}", "reason": "r"} for i in range(6)])
    with p1, p2:
        assert len(_pick("good books", _candidates(6))) == 3


def test_pick_returns_nothing_when_the_model_declines():
    """Choosing nobody is a first-class answer: emailing nobody costs one turn, emailing
    the wrong five costs the channel."""
    p1, p2 = _llm([])
    with p1, p2:
        assert _pick("good books", _candidates(3)) == []


def test_pick_returns_nothing_when_the_llm_is_down():
    with patch("app.orchestrator.llm.llm_configured", lambda: False):
        assert _pick("good books", _candidates(3)) == []


# ── copy binding: the card promise is a function of the outcome ──────────────


def test_card_promises_nothing_when_nobody_was_asked():
    """No outreach_copy means the frontend renders its localized listening floor. The bug
    this replaces was the opposite: a hardcoded promise with no backend behind it."""
    card = build_ask_receipt(detail_text="any good coffee shop", outcome=None)
    assert card["outreach_copy"] is None
    assert card["status_label"] == "Listening nearby"


def test_card_states_the_real_number_when_neighbors_were_asked():
    outcome = {"recipients": [{"user_id": "u1"}, {"user_id": "u2"}]}
    card = build_ask_receipt(detail_text="any good coffee shop", outcome=outcome)
    assert "2 neighbors" in card["outreach_copy"]
    assert card["status_label"] == "Asking 2 neighbors"


def test_card_never_promises_a_text_message():
    """There is no SMS in this app; the lane shipped 'I'll text you' in three languages."""
    outcome = {"recipients": [{"user_id": "u1"}]}
    for card in (
        build_ask_receipt(detail_text="a dentist", outcome=outcome),
        build_ask_receipt(detail_text="a dentist", outcome=None),
    ):
        assert "text you" not in (card.get("outreach_copy") or "").lower()


# ── the configuration guard ──────────────────────────────────────────────────


def test_routing_stays_off_without_a_working_unsubscribe(monkeypatch):
    """A half-configured deploy must not start emailing people about a stranger's ask with
    no way out. The flag alone is not enough to send."""
    from app import tip_ask_route

    monkeypatch.setenv("LANA_ASK_ROUTING", "1")
    monkeypatch.delenv("SIGNAL_SWEEP_TOKEN", raising=False)
    monkeypatch.delenv("LANA_WORKER_PUBLIC_URL", raising=False)
    assert tip_ask_route.enabled() is False

    monkeypatch.setenv("SIGNAL_SWEEP_TOKEN", "s3cret")
    assert tip_ask_route.enabled() is False, "a secret with no public URL yields no link"

    monkeypatch.setenv("LANA_WORKER_PUBLIC_URL", "https://worker.example")
    assert tip_ask_route.enabled() is True
    assert tip_ask_route.mute_link("u1").startswith("https://worker.example/asks/mute?")


def test_mute_token_is_per_user_and_verified(monkeypatch):
    from app import tip_ask_route

    monkeypatch.setenv("SIGNAL_SWEEP_TOKEN", "s3cret")
    token = tip_ask_route.mute_token("u1")
    assert tip_ask_route.verify_mute("u1", token)
    assert not tip_ask_route.verify_mute("u2", token), "one link must not unsubscribe another"
    assert not tip_ask_route.verify_mute("u1", "deadbeef")


def test_disabled_routing_reports_an_outcome_not_a_send(monkeypatch):
    """Flag off is a real outcome the receipt knows how to describe — never an exception,
    never a silent success."""
    from app import tip_ask_route

    monkeypatch.setenv("LANA_ASK_ROUTING", "0")
    outcome = tip_ask_route.route_tip_ask(
        "jwt", signal_id="sig-1", asker_user_id="me", ask_text="good books"
    )
    assert outcome == {
        "recipients": [],
        "none_qualified": True,
        "error": False,
        "thin_standing": False,
    }


# ── the email ────────────────────────────────────────────────────────────────


def test_unsubscribe_is_a_link_in_the_footer_not_a_raw_url(monkeypatch):
    """A bare URL auto-links in the client and renders as three wrapped lines of visible
    link text, louder than the ask itself. It also belongs under the CTA, not above it."""
    from app import tip_ask_route
    from app.notifications import email_html

    monkeypatch.setenv("SIGNAL_SWEEP_TOKEN", "s3cret")
    monkeypatch.setenv("LANA_WORKER_PUBLIC_URL", "https://worker.example")
    link = tip_ask_route.mute_link("u1")
    html = email_html(
        "heading", "body", "Share what you know", "/chat",
        footer_note=f'<a href="{link}">Stop emailing me</a>',
    )
    assert f'<a href="{link}">' in html
    # The footer is after the CTA in the document, which is where an opt-out belongs.
    assert html.index("Share what you know") < html.index("Stop emailing me")
    # And the community footer must not appear on mail that has nothing to do with one.
    assert "you joined a community" not in html


def test_default_footer_survives_for_community_mail():
    from app.notifications import email_html

    assert "you joined a community" in email_html("h", "b", "CTA", "/chat")


def test_asker_name_falls_back_to_the_profile(monkeypatch):
    """"A neighbor nearby is looking for…" reads like a mailshot; the asker's own name
    reads like a neighbor, and they chose to ask."""
    from app import tip_ask_route

    monkeypatch.setattr("app.notifications._user_contact", lambda uid: ("e@x.com", "Dom"))
    assert tip_ask_route._asker_name(None, "u1") == "Dom"
    assert tip_ask_route._asker_name("  ", "u1") == "Dom"
    assert tip_ask_route._asker_name("Sam", "u1") == "Sam"
    monkeypatch.setattr("app.notifications._user_contact", lambda uid: (None, None))
    assert tip_ask_route._asker_name(None, "u1") == "A neighbor"


# ── the return path ──────────────────────────────────────────────────────────


class _PendingSB:
    """Stub for the two-step pending lookup: recipients row, then its signal."""

    def __init__(self, rows, signal):
        self.rows, self.signal, self.updated = rows, signal, []

    def table(self, name):
        outer = self

        class Q:
            def __init__(self):
                self._t = name

            def __getattr__(self, _n):
                return lambda *a, **k: self

            def update(self, payload):
                outer.updated.append(payload)
                return self

            def execute(self):
                data = outer.rows if self._t == "tip_ask_recipients" else outer.signal
                return SimpleNamespace(data=data)

        return Q()


def test_pending_ask_is_skipped_once_the_asker_took_it_down():
    """Answering a question somebody already withdrew spends the goodwill this feature
    exists to protect."""
    from app import tip_ask_route

    rows = [{"id": "r1", "signal_id": "s1", "reason": "you play badminton", "asker_user_id": "a1"}]
    sb = _PendingSB(rows, {"detail_text": "badminton court", "status": "closed"})
    with patch("app.tip_ask_route.service_client", lambda: sb):
        assert tip_ask_route.pending_ask_for("u1") is None


def test_pending_ask_returns_the_open_one():
    from app import tip_ask_route

    rows = [{"id": "r1", "signal_id": "s1", "reason": "you play badminton", "asker_user_id": "a1"}]
    sb = _PendingSB(rows, {"detail_text": "badminton court", "status": "listening"})
    with patch("app.tip_ask_route.service_client", lambda: sb), patch(
        "app.notifications._user_contact", lambda uid: ("e@x.com", "Dom")
    ):
        got = tip_ask_route.pending_ask_for("u1")
    assert got["ask"] == "badminton court"
    assert got["asker_name"] == "Dom"


def test_raising_an_ask_marks_it_surfaced_so_it_is_not_repeated():
    """Without the marker Lana either forgets it or nags on every single session open."""
    from app import tip_ask_route

    sb = _PendingSB([], {})
    with patch("app.tip_ask_route.service_client", lambda: sb):
        tip_ask_route.mark_surfaced("r1")
    assert sb.updated and "surfaced_at" in sb.updated[0]


def test_no_pending_ask_leaves_the_greeting_alone():
    from app import tip_ask_route

    sb = _PendingSB([], {})
    with patch("app.tip_ask_route.service_client", lambda: sb):
        assert tip_ask_route.opening_for_pending_ask("u1", {}) is None


# ── the anti-gaming floor is a tier, not a gate ──────────────────────────────


def _route_with(monkeypatch, scores: dict[str, float]):
    """Run route_tip_ask against a fixed set of candidates and their authority scores.

    Returns (outcome, shortlisted_user_ids) — what _pick was actually offered is the
    thing under test, since that is what the floor used to discard before Lana ever saw it.
    """
    from app import tip_ask_route

    monkeypatch.setenv("LANA_ASK_ROUTING", "1")
    # enabled() is ANDed with a working unsubscribe — without these it is a no-op turn.
    monkeypatch.setenv("SIGNAL_SWEEP_TOKEN", "s3cret")
    monkeypatch.setenv("LANA_WORKER_PUBLIC_URL", "https://worker.example")
    seen: dict[str, list] = {}

    def fake_pick(_ask, candidates):
        seen["shortlist"] = [c["user_id"] for c in candidates]
        return [{"user_id": c["user_id"], "name": "N", "reason": "r"} for c in candidates[:1]]

    with patch("app.layer1_handlers.fetch_peers_semantic",
               return_value=[{"peer_user_id": u, "peer_nickname": u} for u in scores]), \
         patch("app.authority.concepts_for_ask", return_value=["dentist"]), \
         patch("app.authority.best_authority",
               side_effect=lambda uid, _c, **_k: {"score": scores[uid], "quote": None}), \
         patch.object(tip_ask_route, "eligible_recipients", side_effect=lambda ids: set(ids)), \
         patch.object(tip_ask_route, "_pick", side_effect=fake_pick), \
         patch.object(tip_ask_route, "_record"), \
         patch.object(tip_ask_route, "_send_async"):
        outcome = tip_ask_route.route_tip_ask(
            "jwt", signal_id="sig-1", asker_user_id="me", ask_text="anyone know a good dentist?"
        )
    return outcome, seen.get("shortlist", [])


def test_thin_standing_is_never_mixed_with_proven_standing(monkeypatch):
    """A bare claim must not ride along beside a specific one — that is the gaming path."""
    outcome, shortlist = _route_with(monkeypatch, {"strong": 0.50, "thin": 0.10})
    assert shortlist == ["strong"]
    assert outcome["thin_standing"] is False


def test_thin_standing_is_asked_when_nobody_cleared_the_floor(monkeypatch):
    """The ordinary ask used to return nobody because most neighbours have a thin claim.
    Something beats nothing — and the outcome says so, so the reply can stay honest."""
    outcome, shortlist = _route_with(monkeypatch, {"thin": 0.10, "thinner": 0.25})
    assert set(shortlist) == {"thin", "thinner"}
    assert outcome["thin_standing"] is True
    assert outcome["recipients"]


def test_no_standing_at_all_is_still_not_a_candidate(monkeypatch):
    """Falling back to a thin claim is not the same as asking someone unconnected."""
    from app import tip_ask_route

    monkeypatch.setenv("LANA_ASK_ROUTING", "1")
    monkeypatch.setenv("SIGNAL_SWEEP_TOKEN", "s3cret")
    monkeypatch.setenv("LANA_WORKER_PUBLIC_URL", "https://worker.example")
    with patch("app.layer1_handlers.fetch_peers_semantic",
               return_value=[{"peer_user_id": "nobody", "peer_nickname": "N"}]), \
         patch("app.authority.concepts_for_ask", return_value=["dentist"]), \
         patch("app.authority.best_authority", return_value=None), \
         patch.object(tip_ask_route, "eligible_recipients", side_effect=lambda ids: set(ids)):
        outcome = tip_ask_route.route_tip_ask(
            "jwt", signal_id="sig-1", asker_user_id="me", ask_text="good dentist?"
        )
    assert outcome["none_qualified"] is True
    assert outcome["recipients"] == []
