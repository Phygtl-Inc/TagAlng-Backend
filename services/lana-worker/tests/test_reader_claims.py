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


def test_kept_only_when_the_quote_shows_it_and_the_line_makes_the_link_plain():
    from unittest import mock

    from app import reader_claims as rc

    verdicts = {"items": [
        {"key": "good", "claim": "Plays guitar", "supports": True, "clear": True},
        {"key": "unproven", "claim": "Plays guitar", "supports": False, "clear": True},
        {"key": "unclear", "claim": "Plays guitar", "supports": True, "clear": False},
        {"key": "missing", "claim": "Plays guitar", "supports": True},
        # Filed under the wrong key: the verdict names a claim this pair is not about.
        {"key": "misfiled", "claim": "Sicilian heritage", "supports": True, "clear": True},
    ]}
    pairs = [{"key": k, "claim": "Plays guitar", "look_for": "live music", "line": "l", "quote": "q"}
             for k in ("good", "unproven", "unclear", "missing", "misfiled")]
    with mock.patch("app.orchestrator.llm.llm_configured", return_value=True), \
            mock.patch("app.orchestrator.llm.llm_json", return_value=verdicts):
        assert rc.judge_for_you(pairs) == {"good"}


def test_a_page_never_repeats_a_claim_and_the_first_card_picks_first():
    from app import reader_claims as rc

    late, guitar, kids = ({"claim_id": c, "line": c} for c in ("c1", "c2", "c3"))
    items = [{"for_you": [late, guitar]}, {"for_you": [late, guitar, kids]},
             {"for_you": [late]}, {"for_you": [guitar, kids]}]
    rc.one_claim_per_card(items)
    assert [[f["claim_id"] for f in i["for_you"]] for i in items] == [["c1"], ["c2"], [], ["c3"]]


def test_the_judge_sees_what_the_claim_was_expanded_to_and_the_line():
    from unittest import mock

    from app import reader_claims as rc

    seen = {}
    items = [{"for_you": [{"claim_id": "c1", "line": "You play guitar — live music Fridays.",
                           "quotes": [{"text": "a jazz trio on Fridays"}]}]}]
    claims = [{"id": "c1", "label": "Plays guitar", "look_for": "live music"}]
    with mock.patch.object(rc, "judge_for_you", side_effect=lambda pairs: seen.update(p=pairs) or {"0.0.0"}):
        rc.keep_judged(items, claims, "steakhouses")
    assert seen["p"][0]["look_for"] == "live music"
    assert seen["p"][0]["line"] == "You play guitar — live music Fridays." and items[0]["for_you"]


def test_a_claim_never_expanded_is_looked_for_as_itself():
    from unittest import mock

    from app import reader_claims as rc

    seen = {}
    with mock.patch("app.orchestrator.llm.llm_configured", return_value=True), \
            mock.patch("app.orchestrator.llm.llm_json",
                       side_effect=lambda **kw: seen.update(p=kw["user_payload"]) or {"items": []}):
        rc.judge_for_you([{"key": "0", "claim": "Has a dog", "look_for": "", "line": "l", "quote": "q"}])
    assert '"look_for": "Has a dog"' in seen["p"]


EXPAND_CLAIMS = [
    {"id": "c1", "label": "Plays guitar", "bucket": "hobby", "sayable": True, "about": "self"},
    {"id": "c2", "label": "Loves pizza", "bucket": "food", "sayable": True, "about": "self"},
    {"id": "c3", "label": "Muslim", "bucket": "faith", "sayable": False, "about": "self"},
]


def test_expansion_keeps_only_claims_with_a_link_and_a_quiet_one_carries_no_because():
    from unittest import mock

    from app import reader_claims as rc

    reply = {"claims": [
        {"id": "c1", "look_for": "live music", "because": "", "by_hours": "yes", "you": "You play guitar"},
        {"id": "c2", "look_for": None, "because": ""},
        {"id": "c3", "look_for": "halal food", "because": "given your faith", "by_hours": True,
         "you": "You are Muslim"},
        {"id": "c9", "look_for": "invented", "because": ""},
    ]}
    with mock.patch("app.orchestrator.llm.llm_configured", return_value=True), \
            mock.patch("app.orchestrator.llm.llm_json", return_value=reply):
        out = rc.expand_for_ask(EXPAND_CLAIMS, "pizza")
    assert [(c["id"], c["look_for"], c["because"], c["by_hours"], c["you"]) for c in out] == [
        ("c1", "live music", "", False, "You play guitar"), ("c3", "halal food", "", True, "")]


def test_expansion_fails_closed():
    from unittest import mock

    from app import reader_claims as rc

    with mock.patch("app.orchestrator.llm.llm_configured", return_value=True), \
            mock.patch("app.orchestrator.llm.llm_json", side_effect=RuntimeError("down")):
        assert rc.expand_for_ask(EXPAND_CLAIMS, "pizza") == []
    assert rc.expand_for_ask([], "pizza") == []


def test_the_line_opens_with_the_reader_and_the_link_then_what_the_quote_shows():
    from app.reader_claims import assemble_line

    guitar = {"sayable": True, "you": "You play guitar", "because": ""}
    ironman = {"sayable": True, "you": "You're training for an Ironman", "because": "so fuel matters"}
    faith = {"sayable": False, "you": "You are Muslim", "because": "x"}
    assert assemble_line(guitar, "Reviewers say a live band plays here.") == \
        "You play guitar — reviewers say a live band plays here."
    assert assemble_line(ironman, "reviewers say the bowls are packed with protein") == \
        "You're training for an Ironman, so fuel matters — reviewers say the bowls are packed with protein."
    # Never names the reader for a quiet claim, nor for one the expansion gave no opening.
    assert assemble_line(faith, "everything here is halal") == "Everything here is halal."
    assert assemble_line({"sayable": True}, "reviewers say it's dog friendly") == "Reviewers say it's dog friendly."
    assert assemble_line(guitar, "  ") == ""
