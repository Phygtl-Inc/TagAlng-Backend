"""app/reader_claims.py — which of the reader's claims a page may use, and name."""

from app.reader_claims import shape_claims


def test_private_transient_and_duplicate_claims_are_never_used():
    rows = [
        {"label": "Vegetarian", "bucket": "interest", "disclosure": "public"},
        {"label": "vegetarian", "bucket": "interest", "disclosure": "public"},
        {"label": "Secret", "bucket": "general", "disclosure": "private"},
        {"label": "Unknown sharing", "bucket": "general", "disclosure": None},
        {"label": "Passing mood", "bucket": "general", "disclosure": "public", "transient": True},
    ]
    assert [c["label"] for c in shape_claims(rows)] == ["Vegetarian"]


def test_faith_and_heritage_are_quiet_everyday_is_sayable():
    rows = [
        {"label": "Has two kids", "bucket": "stage", "disclosure": "mutual"},
        {"label": "Muslim", "bucket": "faith", "disclosure": "public"},
        {"label": "From Madrid", "bucket": "heritage", "disclosure": "public"},
    ]
    assert [(c["label"], c["sayable"]) for c in shape_claims(rows)] == [
        ("Has two kids", True), ("Muslim", False), ("From Madrid", False),
    ]



def test_only_confirmed_quotes_survive_and_an_empty_item_goes():
    from unittest import mock

    from app import reader_claims as rc

    claims = [{"id": "c1", "label": "Has kids"}]
    items = [{"for_you": [{"claim_id": "c1", "line": "x",
                           "quotes": [{"text": "so patient with our toddlers"},
                                      {"text": "the kids were hungry and agitated"}]}]},
             {"for_you": [{"claim_id": "c1", "line": "y", "quotes": [{"text": "kids burrito was bad"}]}]}]
    seen = {}

    def judge(pairs):
        seen["pairs"] = pairs
        return {"0.0.0"}

    with mock.patch.object(rc, "judge_for_you", side_effect=judge):
        rc.keep_judged(items, claims)
    assert items[0]["for_you"][0]["quotes"] == [{"text": "so patient with our toddlers"}]
    assert items[1]["for_you"] == []
    assert {p["claim"] for p in seen["pairs"]} == {"Has kids"}


def test_judge_fails_closed():
    from unittest import mock

    from app import reader_claims as rc

    with mock.patch("app.orchestrator.llm.llm_configured", return_value=True), \
            mock.patch("app.orchestrator.llm.llm_json", side_effect=RuntimeError("down")):
        assert rc.judge_for_you([{"key": "0", "claim": "Has kids", "quote": "q"}]) == set()
    assert rc.judge_for_you([]) == set()


def test_the_ask_reaches_the_judge_so_a_restated_ask_can_be_refused():
    from unittest import mock

    from app import reader_claims as rc

    seen = {}
    items = [{"for_you": [{"claim_id": "c1", "line": "x", "quotes": [{"text": "authentic lasagna"}]}]}]
    with mock.patch.object(rc, "judge_for_you", side_effect=lambda pairs: seen.update(p=pairs) or set()):
        rc.keep_judged(items, [{"id": "c1", "label": "Loves Italian food"}], "italian restaurants")
    assert seen["p"][0]["ask"] == "italian restaurants" and items[0]["for_you"] == []
