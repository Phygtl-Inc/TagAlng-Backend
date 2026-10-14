"""The per-turn `intent_decision` audit row (app/intent_audit.py).

Observation only: these tests pin what gets RECORDED — the model's real pick and
confidence, every keyword rewrite of it, one row per turn on every path — and that
recording can never change or break a turn."""

from __future__ import annotations

from typing import Any

import pytest

from app import intent_audit
from app import lana_unified_pipeline as pipeline
from app.layer1_intents import (
    enrich_slots,
    reconcile_hosting_peer_slot_conflict,
    reconcile_swap_seek_signal,
    reconcile_tip_seek_signal,
    reconcile_tip_share_signal,
)

UNSURE_PEERS = {"linear_intent": "discovery.find_peers", "goal": "peers", "confidence": 0.4}


def _overrides_from(fn, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
    token = intent_audit.begin_turn()
    try:
        fn(*args, **kwargs)
    finally:
        return intent_audit.end_turn(token)


# ── record_override ──────────────────────────────────────────────────────────


def test_nothing_is_recorded_outside_a_turn() -> None:
    intent_audit.record_override("r", from_label="a", from_conf=0.1, to_label="b", to_conf=0.9)
    assert _overrides_from(lambda: None) == []


def test_a_rewrite_is_recorded_once_and_a_no_op_not_at_all() -> None:
    def _turn() -> None:
        for _ in range(3):  # enrich_slots runs several times per turn
            intent_audit.record_override(
                "r", from_label="a", from_conf=0.4, to_label="b", to_conf=0.85
            )
        intent_audit.record_override("same", from_label="a", from_conf=0.4, to_label="a", to_conf=0.4)

    assert _overrides_from(_turn) == [
        {"rule": "r", "from": "a", "from_conf": 0.4, "to": "b", "to_conf": 0.85}
    ]


# ── each keyword rule reports its rewrite ────────────────────────────────────


@pytest.mark.parametrize(
    ("reconcile", "msg", "rule", "to"),
    [
        (reconcile_tip_share_signal, "Dr Smith is a great dentist", "reconcile_tip_share", "sharing.tip"),
        (reconcile_tip_seek_signal, "can you recommend a good dentist", "reconcile_tip_seek", "looking.tip"),
        (reconcile_swap_seek_signal, "looking for a stroller", "reconcile_swap_seek", "looking.swap"),
        (reconcile_hosting_peer_slot_conflict, "I want to host a picnic saturday", "reconcile_hosting", "sharing.host"),
    ],
)
def test_each_reconciler_reports_the_models_real_pick(reconcile, msg, rule, to) -> None:
    out = dict(UNSURE_PEERS)
    overrides = _overrides_from(reconcile, out, msg=msg)
    assert overrides == [
        {"rule": rule, "from": "discovery.find_peers", "from_conf": 0.4, "to": to, "to_conf": 0.85}
    ]


def test_a_reconciler_that_defers_reports_nothing() -> None:
    confident = {"linear_intent": "identity.add_claim", "confidence": 0.9}
    assert _overrides_from(reconcile_tip_share_signal, confident, msg="Dr Smith is a great dentist") == []


def test_the_phrase_fallback_reports_its_rewrite() -> None:
    unsure = {"linear_intent": "discovery.find_peers", "confidence": 0.3}
    overrides = _overrides_from(enrich_slots, unsure, msg="change my name to Sam")
    assert {"rule": "phrase_fallback", "from": "discovery.find_peers", "from_conf": 0.3,
            "to": "settings.change_name", "to_conf": 0.85} in overrides


def test_the_community_create_keyword_reports_its_rescue() -> None:
    unsure_host = {"linear_intent": "sharing.host", "goal": "save_signal", "confidence": 0.5}
    overrides = _overrides_from(
        pipeline._turn_is_community_create, unsure_host, "i want to create a community"
    )
    assert {"rule": "community_create_keyword", "from": "sharing.host", "from_conf": 0.5,
            "to": "sharing.community", "to_conf": 0.5} in overrides


def test_the_tip_share_keyword_reports_its_rescue() -> None:
    overrides = _overrides_from(pipeline._turn_is_tip_share, None, "Dr Smith is a great dentist")
    assert [o["rule"] for o in overrides] == ["tip_share_keyword"]


def test_recording_never_changes_a_decision() -> None:
    a, b = dict(UNSURE_PEERS), dict(UNSURE_PEERS)
    reconcile_tip_share_signal(a, msg="Dr Smith is a great dentist")  # no turn open
    _overrides_from(reconcile_tip_share_signal, b, msg="Dr Smith is a great dentist")
    assert a == b


# ── the record ───────────────────────────────────────────────────────────────


def test_the_record_holds_the_raw_classifier_read() -> None:
    ctx = {
        "_discovery_slots": {"linear_intent": "looking.tip", "goal": "save_signal", "confidence": 0.42},
        "_discovery_slots_for": "any good dentist?",
        "last_routing": {"outcome": "A", "intent_class": "discovery", "tool_called": "tip_search"},
        "routing_phase": "listening",
    }
    rec = intent_audit.build_record(
        session_ctx=ctx, user_message=" any good dentist? ", routing_phase_in="",
        overrides=[], elapsed_ms=12,
    )
    assert rec["classifier"] == {
        "ran": True, "linear_intent": "looking.tip", "goal": "save_signal",
        "clarify": False, "confidence": 0.42,
    }
    assert rec["final"] == {"outcome": "A", "intent_class": "discovery", "tool_called": "tip_search"}


def test_a_stale_classifier_read_is_not_logged_as_this_turns() -> None:
    ctx = {"_discovery_slots": {"linear_intent": "looking.tip", "confidence": 0.9},
           "_discovery_slots_for": "the previous message"}
    rec = intent_audit.build_record(
        session_ctx=ctx, user_message="yes please", routing_phase_in="", overrides=[], elapsed_ms=1
    )
    assert rec["classifier"] == {"ran": False}


def test_a_chip_tap_is_not_logged_as_a_model_decision() -> None:
    ctx = {"_discovery_slots": {"linear_intent": "looking.tip", "confidence": 1.0, "_forced_kind": "tip"},
           "_discovery_slots_for": "show all"}
    rec = intent_audit.build_record(
        session_ctx=ctx, user_message="show all", routing_phase_in="", overrides=[], elapsed_ms=1
    )
    assert rec["classifier"]["ran"] is False
    assert rec["classifier"]["forced_kind"] == "tip"


# ── a failed classifier call is loud, not silent ─────────────────────────────


class _AuthError(Exception):
    status_code = 401


def _classify_with(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> dict[str, Any]:
    from app import discovery_slots as ds

    def _raise(**kw: Any):
        raise exc

    monkeypatch.setattr(ds, "discovery_ai_enabled", lambda: True)
    monkeypatch.setattr(ds, "llm_json", _raise)
    return ds.ai_parse_discovery_turn(
        "can you recommend a good dentist", routing_phase="listening", history=[],
        has_block=True, has_identity=True, phone_verified=True,
    )


def test_a_failed_api_call_is_logged_without_the_providers_message(monkeypatch, caplog) -> None:
    """2026-10-14: a revoked key routed every turn by keyword with nothing in the logs."""
    caplog.set_level("WARNING", logger="app.discovery_slots")
    slots = _classify_with(monkeypatch, _AuthError("Incorrect API key provided: sk-secret-1oUA"))
    assert slots["linear_intent"] is None  # the turn still degrades exactly as before
    assert slots["_classifier_error"] == "_AuthError"
    assert "discovery_classifier_failed error=_AuthError status=401" in caplog.text
    assert "sk-secret" not in caplog.text  # a provider message can echo the key


def test_a_code_error_in_the_classifier_is_logged_with_its_traceback(monkeypatch, caplog) -> None:
    caplog.set_level("WARNING", logger="app.discovery_slots")
    slots = _classify_with(monkeypatch, KeyError("linear_intent"))
    assert slots["_classifier_error"] == "KeyError"
    assert "discovery_classifier_failed error=KeyError" in caplog.text
    assert "Traceback" in caplog.text


def test_the_audit_row_records_a_failure_not_an_unsure_model() -> None:
    ctx = {"_discovery_slots": {"linear_intent": None, "confidence": 0.0,
                                "_classifier_error": "AuthenticationError"},
           "_discovery_slots_for": "any good dentist?"}
    rec = intent_audit.build_record(
        session_ctx=ctx, user_message="any good dentist?", routing_phase_in="", overrides=[], elapsed_ms=1
    )
    assert rec["classifier"] == {"ran": False, "error": "AuthenticationError"}


def test_the_report_counts_failures_separately() -> None:
    from scripts.intent_audit_report import summarize

    out = summarize([
        {"classifier": {"ran": False, "error": "AuthenticationError"}, "overrides": []},
        {"classifier": {"ran": True, "confidence": 0.9}, "overrides": []},
    ])
    assert "classifier FAILED:     1 (50%)  {'AuthenticationError': 1}" in out
    assert "classifier skipped:    0 (0%)" in out


# ── the pipeline wrapper: one row per turn, never in the way ─────────────────

_KW = dict(
    user_id="u1", session_id="s1", history=[], user_message="hi", session_ctx={},
    user_jwt="", phone_verified=True, home_block_id=None, is_anonymous=False,
)


@pytest.fixture
def rows(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(intent_audit, "log_intent_decision", lambda **kw: captured.append(kw))
    monkeypatch.setenv("LANA_INTENT_AUDIT", "on")
    return captured


def test_every_turn_writes_exactly_one_row(monkeypatch, rows) -> None:
    def fake(**kw: Any):
        intent_audit.record_override("r", from_label="a", from_conf=0.4, to_label="b", to_conf=0.85)
        return "reply", "continue", {"last_routing": {"outcome": "A"}}, {}, None

    monkeypatch.setattr(pipeline, "_run_lana_unified_pipeline", fake)
    out = pipeline.run_lana_unified_pipeline(**_KW)
    assert out[0] == "reply"
    assert len(rows) == 1
    assert rows[0]["record"]["overrides"][0]["rule"] == "r"
    assert rows[0]["record"]["final"]["outcome"] == "A"


def test_a_crashing_turn_still_logs_and_still_raises(monkeypatch, rows) -> None:
    def boom(**kw: Any):
        raise RuntimeError("x")

    monkeypatch.setattr(pipeline, "_run_lana_unified_pipeline", boom)
    with pytest.raises(RuntimeError):
        pipeline.run_lana_unified_pipeline(**_KW)
    assert rows[0]["record"]["error"] == "RuntimeError"


def test_a_failing_audit_never_breaks_the_reply(monkeypatch) -> None:
    monkeypatch.setenv("LANA_INTENT_AUDIT", "on")
    monkeypatch.setattr(pipeline, "_run_lana_unified_pipeline",
                        lambda **kw: ("reply", "continue", {}, {}, None))

    def broken(**kw: Any):
        raise RuntimeError("db down")

    monkeypatch.setattr(intent_audit, "log_intent_decision", broken)
    assert pipeline.run_lana_unified_pipeline(**_KW)[0] == "reply"


def test_off_by_default(monkeypatch, rows) -> None:
    monkeypatch.delenv("LANA_INTENT_AUDIT")
    monkeypatch.setattr(pipeline, "_run_lana_unified_pipeline",
                        lambda **kw: ("reply", "continue", {}, {}, None))
    pipeline.run_lana_unified_pipeline(**_KW)
    assert rows == []
