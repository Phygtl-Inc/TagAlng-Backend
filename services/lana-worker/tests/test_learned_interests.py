"""Learned interests (migration 20270204120000, app/learned_interests.py).

Asjid, 2026-10-07: Tommaso searched AI meets and "find people into AI" found nobody —
only things people SAY about themselves became claims. Searches now teach Lana; people
search shows learned matches below stated ones, saying what they did.
"""

from __future__ import annotations

from unittest import mock

import app.learned_interests as li
from app.layer1_handlers import fetch_peers_by_attr_filter, peers_to_match_rows
from tests.test_widen_related_and_location import _AI, _BOOKS, _fresh, _turn


class _Res:
    def __init__(self, data):
        self.data = data


def _client(record_row=None, mention=True, boom=False):
    sb = mock.Mock()

    def rpc(name, args):
        call = mock.Mock()
        if boom:
            call.execute.side_effect = RuntimeError("db down")
        elif name == "record_learned_interest":
            call.execute.return_value = _Res([record_row] if record_row else [])
        else:
            call.execute.return_value = _Res(mention)
        return call

    sb.rpc.side_effect = rpc
    return sb


# ── topic_label ────────────────────────────────────────────────────────────────


def test_topic_label_keeps_a_topic_and_refuses_a_sentence() -> None:
    assert li.topic_label("  AI ") == "AI"
    assert li.topic_label("board   games") == "board games"
    assert li.topic_label("x") is None
    assert li.topic_label("are there any fun things for my kids this weekend") is None
    assert li.topic_label(None) is None


# ── record ─────────────────────────────────────────────────────────────────────


def test_record_returns_the_label_to_mention_when_newly_learned() -> None:
    sb = _client({"learned": True, "newly_learned": True, "label": "AI", "topic": "ai"})
    with mock.patch("app.auth.service_client", return_value=sb):
        assert li.record("u1", "AI", "event_search") == "AI"
    names = [c.args[0] for c in sb.rpc.call_args_list]
    assert names == ["record_learned_interest", "claim_learned_mention"]


def test_record_mentions_nothing_when_a_mention_was_made_this_week() -> None:
    sb = _client({"learned": True, "newly_learned": True, "label": "AI", "topic": "ai"},
                 mention=False)
    with mock.patch("app.auth.service_client", return_value=sb):
        assert li.record("u1", "AI", "event_search") is None


def test_record_mentions_nothing_before_or_after_learning() -> None:
    sb = _client({"learned": False, "newly_learned": False, "label": "AI", "topic": "ai"})
    with mock.patch("app.auth.service_client", return_value=sb):
        assert li.record("u1", "AI", "event_search") is None
    assert [c.args[0] for c in sb.rpc.call_args_list] == ["record_learned_interest"]


def test_record_never_raises_into_the_turn() -> None:
    with mock.patch("app.auth.service_client", return_value=_client(boom=True)):
        assert li.record("u1", "AI", "event_search") is None


def test_record_is_off_with_the_flag_and_without_a_user() -> None:
    sb = _client({"newly_learned": True, "label": "AI", "topic": "ai"})
    with mock.patch("app.auth.service_client", return_value=sb):
        with mock.patch.dict("os.environ", {"LANA_LEARNED_INTERESTS": "0"}):
            assert li.record("u1", "AI", "event_search") is None
        assert li.record(None, "AI", "event_search") is None
        assert li.record("u1", None, "people_search") is None
    sb.rpc.assert_not_called()


# ── mention ────────────────────────────────────────────────────────────────────


def test_mention_rides_after_the_answer_once() -> None:
    ctx = {"_learned_mention": "AI"}
    out = li.append_mention("Here are two AI meets.", ctx)
    assert out.startswith("Here are two AI meets.\n\n")
    assert "into AI" in out and "remove it" in out
    assert ctx["_learned_mention"] is None  # cleared with None, never popped
    assert li.append_mention("Next answer.", ctx) == "Next answer."


# ── people search ──────────────────────────────────────────────────────────────


def _stated(n: int) -> list[dict]:
    return [{"peer_user_id": f"s{i}", "nickname": f"S{i}", "matching_peer_label": "Works in AI"}
            for i in range(n)]


def _learned_rpc(rows):
    return mock.patch("app.layer1_handlers._call_peer_rpc", return_value=rows)


def test_learned_matches_come_after_stated_ones_and_say_what_they_did() -> None:
    learned_rows = [
        {"peer_user_id": "s0", "nickname": "S0", "matching_peer_label": "AI", "learned_kind": "events"},
        {"peer_user_id": "t1", "nickname": "Tommaso", "matching_peer_label": "AI", "learned_kind": "events"},
        {"peer_user_id": "p1", "nickname": "Pouya", "matching_peer_label": "AI", "learned_kind": "people"},
    ]
    with mock.patch("app.layer1_handlers._fetch_peers_stated", return_value=_stated(1)), \
            _learned_rpc(learned_rows) as call:
        peers = fetch_peers_by_attr_filter("jwt", "ai", limit=5)
    assert [p["nickname"] for p in peers] == ["S0", "Tommaso", "Pouya"]  # stated first, no repeat
    assert peers[1]["matching_peer_label"] == "Has been checking out AI meetups"
    assert peers[2]["matching_peer_label"] == "Has been looking for people into AI"
    assert peers[1]["learned"] and not peers[0].get("learned")
    name, args = call.call_args.args[1], call.call_args.args[2]
    assert name == "find_peers_by_learned_interest" and args["p_terms"] == ["ai"]
    assert args["p_limit"] == 4  # only the room left


def test_a_full_page_of_stated_matches_skips_the_learned_tier() -> None:
    with mock.patch("app.layer1_handlers._fetch_peers_stated", return_value=_stated(5)), \
            _learned_rpc([]) as call:
        peers = fetch_peers_by_attr_filter("jwt", "ai", limit=5)
    assert len(peers) == 5
    call.assert_not_called()


def test_learned_tier_failure_keeps_the_stated_results() -> None:
    with mock.patch("app.layer1_handlers._fetch_peers_stated", return_value=_stated(1)), \
            mock.patch("app.layer1_handlers._call_peer_rpc", side_effect=RuntimeError("x")):
        assert [p["nickname"] for p in fetch_peers_by_attr_filter("jwt", "ai")] == ["S0"]


def test_learned_card_has_no_badge_and_keeps_its_sentence() -> None:
    row = {"peer_user_id": "t1", "nickname": "Tommaso", "learned": True,
           "matching_peer_label": "Has been checking out AI meetups"}
    # The enricher WOULD badge it and recompose it as a claim; a learned row skips it.
    claimish = {"match_badge": "STRONG", "matching_peer_label": "Works in AI"}
    with mock.patch("app.peer_discovery_surface.enrich_peer_match_row", return_value=claimish):
        out = peers_to_match_rows([row], phone_verified=True)[0]
        stated = peers_to_match_rows([{**row, "learned": False}], phone_verified=True)[0]
    assert stated["match_badge"] == "STRONG"  # the patch is live for stated rows
    assert out["learned"] is True
    assert out["match_badge"] is None
    assert out["matching_peer_label"] == "Has been checking out AI meetups"


# ── event search records once per topic per browse ─────────────────────────────


def test_browse_records_a_topic_search_once_and_skips_open_asks() -> None:
    with mock.patch("app.learned_interests.record", return_value=None) as rec:
        ctx = _fresh()
        _turn("find a meet about AI", ctx, events=[_BOOKS], slots=_AI)
        _turn("Widen the search", ctx, events=[_BOOKS])
    # The widen tap re-runs the same search under its short label: still one search.
    assert rec.call_count == 1
    assert rec.call_args.args[2] == "event_search"


def test_browse_never_records_an_open_ask() -> None:
    with mock.patch("app.learned_interests.record", return_value=None) as rec:
        # The classifier's topic slot, so the open-ask guard is what decides.
        _turn("anything", _fresh(), events=[_BOOKS], slots={"activity_topic": "anything"})
    rec.assert_not_called()


def test_browse_puts_a_fresh_mention_on_the_session() -> None:
    with mock.patch("app.learned_interests.record", return_value="AI"):
        ctx = _fresh()
        _turn("find a meet about AI", ctx, events=[_BOOKS], slots=_AI)
    assert ctx["_learned_mention"] == "AI"


# ── one search counts once ───────────────────────────────────────────────────


def test_the_same_topic_straight_after_counts_once_and_a_new_one_counts() -> None:
    ctx: dict = {}
    with mock.patch("app.learned_interests.record", return_value=None) as rec:
        li.record_turn(ctx, "u1", "AI", "people_search")
        li.record_turn(ctx, "u1", "ai", "people_search")  # a refine landing on the same topic
        li.record_turn(ctx, "u1", "AI", "event_search")  # a different kind of search
        li.record_turn(ctx, "u1", "chess", "event_search")
        li.record_turn(ctx, "u1", "AI", "event_search")  # back again later: counts
    assert [c.args[1:] for c in rec.call_args_list] == [
        ("AI", "people_search"), ("AI", "event_search"),
        ("chess", "event_search"), ("AI", "event_search"),
    ]


def test_a_sentence_is_never_recorded_as_a_topic() -> None:
    ctx: dict = {}
    with mock.patch("app.learned_interests.record") as rec:
        li.record_turn(ctx, "u1", "are there any fun things for my kids", "event_search")
    rec.assert_not_called()
    assert "_learned_last" not in ctx


def test_browse_without_the_classifier_topic_records_nothing() -> None:
    # No slots: browse falls back to the whole sentence as its topic — not a topic to learn.
    with mock.patch("app.learned_interests.record") as rec:
        _turn("is there anything fun happening for my kids this weekend", _fresh(),
              events=[_BOOKS])
    rec.assert_not_called()


# ── people search records only interests ───────────────────────────────────────


def _jwt(sub: str) -> str:
    import base64
    import json

    body = base64.urlsafe_b64encode(json.dumps({"sub": sub}).encode()).decode().rstrip("=")
    return f"h.{body}.s"


def _people_turn(slots: dict, peers: list[dict] | None = None):
    import app.discovery_route as dr

    slots = {"linear_intent": "discovery.find_by_attrs", "confidence": 0.95, **slots}
    with mock.patch("app.learned_interests.record", return_value="AI") as rec, \
            mock.patch.object(dr, "fetch_peers_by_attr_filter", return_value=peers or []), \
            mock.patch.object(dr, "summarize_partial_claim_matches", return_value=None), \
            mock.patch.object(dr, "format_attr_peers_reply", return_value="reply"):
        out = dr._try_layer1_intent_turn(
            msg="find people into AI", slots=slots, session_ctx={"phone_verified": True},
            user_jwt=_jwt("u-asker"), phone_verified=True, home_block_id="b1",
            phase="listening", user_id="u-asker",
        )
    return out, rec


def test_an_interest_people_search_is_recorded_for_the_searcher() -> None:
    (_reply, ctx, _r, _rows), rec = _people_turn(
        {"attr_filter": "AI", "attr_terms": [["ai"]], "attr_is_interest": True}
    )
    rec.assert_called_once_with("u-asker", "ai", "people_search")
    assert ctx["_learned_mention"] == "AI"


def test_an_identity_people_search_teaches_nothing_about_the_searcher() -> None:
    for flag in (False, None):
        _out, rec = _people_turn(
            {"attr_filter": "brazilian moms", "attr_terms": [["brazilian"], ["mom"]],
             "attr_is_interest": flag}
        )
        rec.assert_not_called()


def test_the_classifier_flag_is_true_only_when_the_model_says_true() -> None:
    from app import discovery_slots as ds

    def parse(value):
        raw = {"linear_intent": "discovery.find_by_attrs", "attr_filter": "AI",
               "attr_terms": [["ai"]], "attr_is_interest": value, "confidence": 0.9}
        with mock.patch.object(ds, "discovery_ai_enabled", return_value=True), \
                mock.patch.object(ds, "llm_json", return_value=raw):
            return ds.ai_parse_discovery_turn(
                "find people into AI", routing_phase="listening", history=[],
                has_block=True, has_identity=True,
            )["attr_is_interest"]

    assert parse(True) is True
    assert parse("true") is False and parse(None) is False and parse(False) is False


# ── what Lana says about a learned match ───────────────────────────────────────


def _reply(peers):
    from app.layer1_handlers import format_attr_peers_reply

    with mock.patch("app.layer1_handlers.compose_reply", side_effect=lambda **k: k):
        return format_attr_peers_reply(peers, filter_text="AI")


def test_learned_only_matches_are_never_called_people_who_said_it() -> None:
    out = _reply([{"peer_user_id": "t1", "learned": True}])
    facts = " | ".join(out["facts"])
    assert "People who said it about themselves: 0" in facts
    assert "searching for it lately: 1" in facts
    assert "mention" not in out["fallback"] and "in their own words" not in out["fallback"]
    assert "has been looking into" in out["fallback"]


def test_mixed_matches_name_both_groups() -> None:
    out = _reply([{"peer_user_id": "s1"}, {"peer_user_id": "t1", "learned": True}])
    assert "People who said it about themselves: 1" in out["facts"]
    assert "1 says so in their own words" in out["fallback"]


def test_stated_only_matches_keep_their_old_reply() -> None:
    out = _reply([{"peer_user_id": "s1"}])
    assert "Neighbors whose own claims match that search: 1" in out["facts"]


# ── the mention reaches the reply at the choke point ─────────────────────────


def test_main_appends_the_mention_before_localizing() -> None:
    import inspect

    import app.main as main

    src = inspect.getsource(main)
    assert src.index("append_mention(reply, session_ctx)") < src.index(
        "reply = finalize_reply_language(reply, session_ctx)"
    )


# ── profile endpoints ──────────────────────────────────────────────────────────


def _client_app():
    from fastapi.testclient import TestClient

    import app.main as main

    auth = mock.Mock(user_id="u-me")
    return TestClient(main.app), mock.patch.object(main, "verify_auth", return_value=auth)


def test_list_returns_only_the_callers_learned_interests() -> None:
    client, auth = _client_app()
    rows = [{"id": "i1", "label": "AI", "evidence": "Has been checking out AI meetups",
             "learned_at": "2026-10-09"}]
    with auth, mock.patch("app.learned_interests.list_for_user", return_value=rows) as lst:
        res = client.post("/lana/learned-interests/list", headers={"Authorization": "Bearer x"})
    assert res.status_code == 200 and res.json() == {"interests": rows}
    lst.assert_called_once_with("u-me")


def test_remove_is_scoped_to_the_caller_and_says_when_nothing_was_removed() -> None:
    client, auth = _client_app()
    iid = "11111111-1111-1111-1111-111111111111"
    with auth, mock.patch("app.learned_interests.remove", return_value=True) as rm:
        ok = client.post("/lana/learned-interests/remove", json={"interest_id": iid})
    assert ok.status_code == 200
    rm.assert_called_once_with("u-me", iid)
    with auth, mock.patch("app.learned_interests.remove", return_value=False):
        gone = client.post("/lana/learned-interests/remove", json={"interest_id": iid})
    assert gone.status_code == 404
    with auth, mock.patch("app.learned_interests.remove") as rm:
        bad = client.post("/lana/learned-interests/remove", json={"interest_id": "nope"})
    assert bad.status_code == 400
    rm.assert_not_called()


def test_the_learned_flag_reaches_the_response() -> None:
    # The response copies fields one by one: a flag missing from that list arrives False.
    from app.main import _peer_matches_from_ctx

    rows = _peer_matches_from_ctx({"peer_matches": [
        {"peer_user_id": "t1", "learned": True,
         "matching_peer_label": "Has been checking out AI meetups"},
        {"peer_user_id": "s1"},
    ]})
    assert [r.learned for r in rows] == [True, False]
    assert rows[0].model_dump()["learned"] is True
