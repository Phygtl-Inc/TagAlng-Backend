"""A reply that names a DIFFERENT subject after an empty search is a new search, never a widen.

Prod 2026-10-08: after "nothing on jazz… want related topics?", "Any informative event" was read
as widen (5/5) and re-ran the jazz search twice; the topic change never reached the search.
"""

from unittest import mock

from app import browse_followup_ai as bf


def _read(raw, msg="Any informative event"):
    with mock.patch.object(bf, "llm_configured", return_value=True), \
            mock.patch.object(bf, "llm_json", return_value=raw) as llm:
        out = bf.read_browse_followup(lana_said="Nothing on jazz near you.", topic="jazz", msg=msg)
    return out, llm


def test_a_widen_that_names_a_different_subject_is_a_new_search():
    out, _ = _read({"verdict": "widen", "different_subject": "informative events"})
    assert out == "new"


def test_a_plain_widen_stays_widen():
    assert _read({"verdict": "widen", "different_subject": None}, "anything similar?")[0] == "widen"
    assert _read({"verdict": "widen"}, "Widen the search")[0] == "widen"


def test_a_null_spelled_as_text_is_still_null():
    for v in ("null", "None", "  "):
        assert _read({"verdict": "widen", "different_subject": v}, "anything similar?")[0] == "widen"


def test_other_verdicts_are_untouched_by_the_subject():
    for verdict in ("accept", "decline", "more", "new", "other"):
        assert _read({"verdict": verdict, "different_subject": "yoga"})[0] == verdict


def test_the_prompt_asks_for_the_subject_and_defines_it():
    _out, llm = _read({"verdict": "new"})
    system = llm.call_args.kwargs["system"]
    assert '"different_subject": <string or null>' in system
    assert "DIFFERENT subject from the topic being searched" in system
