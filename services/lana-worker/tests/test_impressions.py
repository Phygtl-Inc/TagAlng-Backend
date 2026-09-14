"""What Lana showed — the log that makes the pilot measurable (contract v2 §A7).

The one thing that must hold: rows carry a shared request_id and a real position, because
without those you can count what was shown but never ask whether an ask was answered.
"""

from unittest.mock import patch

from app.impressions import log_shown
from app.models import ActivityPreviewRow, PeerMatchRow


def _captured(**kw):
    rows: list = []
    with patch("app.impressions._insert_async", side_effect=lambda r: rows.extend(r)):
        log_shown(**{"user_id": "u1", "session_id": "s1", "block_id": None,
                     "query": None, "peers": [], "activities": [], **kw})
    return rows


def test_one_row_per_candidate_grouped_by_request_and_ordered():
    peers = [PeerMatchRow(peer_user_id=f"p{i}") for i in range(2)]
    acts = [ActivityPreviewRow(activity_id=f"e{i}", title="t") for i in range(2)]
    rows = _captured(peers=peers, activities=acts, query="coffee with other moms")

    assert len(rows) == 4, "one row per candidate, not one per turn"
    # One answer = one request_id. Without this, three candidates for one ask and three
    # failed asks are indistinguishable.
    assert len({r["metadata"]["request_id"] for r in rows}) == 1
    # Position is render order, which is what D4's nDCG measures.
    assert [r["metadata"]["position"] for r in rows] == [0, 1, 2, 3]
    assert [r["recommendation_type"] for r in rows] == ["neighbor"] * 2 + ["event"] * 2
    assert all(r["status"] == "shown" for r in rows)


def test_ids_are_stamped_on_the_payload():
    """The client cannot report a tap on a row whose id it never received."""
    peers = [PeerMatchRow(peer_user_id="p1")]
    acts = [ActivityPreviewRow(activity_id="e1", title="t")]
    rows = _captured(peers=peers, activities=acts)

    assert peers[0].impression_id == rows[0]["id"]
    assert acts[0].impression_id == rows[1]["id"]


def test_scores_and_admission_rule_come_from_ctx():
    acts = [ActivityPreviewRow(activity_id="e1", title="t")]
    rows = _captured(
        activities=acts,
        ctx={"browse_scores": {"e1": 0.73}, "browse_admission": "distance_floor_v1",
             "browse_truncated": True},
    )
    assert rows[0]["score"] == 0.73
    assert rows[0]["metadata"]["admission_rule"] == "distance_floor_v1"
    assert rows[0]["metadata"]["truncated"] is True


def test_query_is_redacted_before_storage():
    """This table gets read by people doing analytics. Raw user text does not belong."""
    rows = _captured(
        peers=[PeerMatchRow(peer_user_id="p1")],
        query="call me on 407-555-0134",
    )
    assert "407-555-0134" not in (rows[0]["query"] or "")


def test_nothing_written_when_nothing_shown():
    assert _captured() == []


def test_a_broken_insert_never_reaches_the_caller():
    """A logging failure must not cost the user their answer."""
    peers = [PeerMatchRow(peer_user_id="p1")]
    with patch("app.impressions.service_client", side_effect=RuntimeError("db down")):
        log_shown(user_id="u1", session_id="s1", block_id=None, query=None,
                  peers=peers, activities=[])
    assert peers[0].impression_id  # still stamped, so a later tap still reports


def test_a_peer_row_carrying_a_tip_logs_as_the_recommendation():
    """The ask was "know a good plumber?" — the answer is the tip, not the neighbour.

    Logging these as 'neighbor' would file every recommendation search under peer
    discovery and leave signal_id null on every row, so "was this rec shown 40 times and
    tapped twice" could never be asked.
    """
    rows = _captured(
        peers=[PeerMatchRow(peer_user_id="u-882", tip_signal_id="sig-4417")],
        ctx={"tip_scores": {"sig-4417": 0.81}},
        query="know a good plumber?",
    )

    assert len(rows) == 1
    assert rows[0]["recommendation_type"] == "local_signal"
    assert rows[0]["signal_id"] == "sig-4417"
    assert rows[0]["score"] == 0.81
    assert rows[0]["suggested_action"] == "view_tip"
    # Who vouched is kept too — both facts are true, and dropping the neighbour would
    # lose the shared-community provenance the whole product rests on.
    assert rows[0]["candidate_user_id"] == "u-882"


def test_a_peer_row_without_a_tip_is_still_a_peer():
    rows = _captured(peers=[PeerMatchRow(peer_user_id="u-882")])
    assert rows[0]["recommendation_type"] == "neighbor"
    assert rows[0]["candidate_user_id"] == "u-882"
    assert rows[0].get("signal_id") is None
