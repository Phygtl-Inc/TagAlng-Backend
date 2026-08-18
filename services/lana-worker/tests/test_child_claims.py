"""Child-subject claims: "my 7-year-old does karate" is a claim about the child.

Covers the two things that silently go wrong: an age stored as an age (rots), and
two kids on one activity collapsing into a single row.
"""

from datetime import datetime, timezone

from app.claims_persist import _claim_row, clean_claims_for_persist, dedupe_claims
from app.models import ExtractedClaim
from app.vertex_extract import _parse_claims, _parse_subject


def _claim(**kw) -> ExtractedClaim:
    base = dict(concept="karate", label="Does karate", confidence=0.9, bucket="activity")
    base.update(kw)
    return ExtractedClaim(**base)


def test_stated_age_becomes_a_birth_year():
    year = datetime.now(timezone.utc).year
    assert _parse_subject({"subject": "child", "subject_age": 7}) == ("child", None, year - 7)


def test_self_claims_carry_no_subject_details():
    # The model sometimes fills subject_name on a self claim; the DB CHECK rejects
    # that row, so it must be dropped here rather than blowing up the write.
    assert _parse_subject({"subject": "self", "subject_name": "Sara", "subject_age": 7}) == (
        "self",
        None,
        None,
    )


def test_improvised_names_and_impossible_ages_are_dropped():
    assert _parse_subject({"subject": "child", "subject_name": "7yo"})[1] is None
    assert _parse_subject({"subject": "child", "subject_age": 99})[2] is None
    assert _parse_subject({"subject": "child", "subject_age": "not a number"})[2] is None


def test_extractor_output_reaches_the_row():
    claim = _parse_claims(
        {
            "claims": [
                {
                    "concept": "karate",
                    "label": "Does karate",
                    "confidence": 0.9,
                    "bucket": "activity",
                    "subject": "child",
                    "subject_name": "Sara",
                    "subject_age": 7,
                }
            ]
        }
    )[0]
    row = _claim_row("u1", claim, None)
    assert row["subject_kind"] == "child"
    assert row["subject_name"] == "Sara"
    assert row["subject_birth_year"] == datetime.now(timezone.utc).year - 7


def test_two_children_on_one_activity_are_two_claims():
    kept = dedupe_claims(
        [
            _claim(subject_kind="child", subject_name="Sara"),
            _claim(subject_kind="child", subject_name="Tom"),
        ]
    )
    assert len(kept) == 2


def test_a_childs_name_never_reaches_the_matchable_text():
    kept = clean_claims_for_persist(
        [
            _claim(
                label="my daughter Sara does karate",
                source_quote="my daughter Sara does karate",
                subject_kind="child",
                subject_name="Sara",
            )
        ]
    )
    assert len(kept) == 1
    assert "Sara" not in kept[0].label
    assert "Sara" not in (kept[0].source_quote or "")
    # ...but the owner-only field keeps it, or the feature does nothing.
    assert kept[0].subject_name == "Sara"


def test_owner_card_says_whose_thread_it_is():
    from app.layer1_handlers import claim_display_label

    assert claim_display_label({"label": "Runs regularly"}) == "Runs regularly"
    assert (
        claim_display_label(
            {"label": "Does karate", "subject_kind": "child", "subject_name": "Tom"}
        )
        == "Does karate (Tom)"
    )
    assert (
        claim_display_label(
            {"label": "Swims", "subject_kind": "child", "subject_name": "Sara", "subject_age": 9}
        )
        == "Swims (Sara, age 9)"
    )
    # Nothing stated about the child yet — still must not read as the parent's.
    assert (
        claim_display_label({"label": "Does karate", "subject_kind": "child"})
        == "Does karate (your child)"
    )


def test_two_children_on_one_activity_both_survive_the_card():
    from app.layer1_handlers import format_identity_profile_reply

    reply = format_identity_profile_reply(
        {
            "profile": {},
            "claims": [
                {"label": "Does karate", "bucket": "activity",
                 "subject_kind": "child", "subject_name": "Tom"},
                {"label": "Does karate", "bucket": "activity",
                 "subject_kind": "child", "subject_age": 7},
            ],
        }
    )
    assert "Does karate (Tom)" in reply and "Does karate (age 7)" in reply


if __name__ == "__main__":  # pragma: no cover
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok {name}")
