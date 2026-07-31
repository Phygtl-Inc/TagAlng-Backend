"""Circles §D.2 — discovery gating by ZIP unlock state.

The rule under test: unlock gates CONSUMPTION of others' supply, never creation,
and it is supply-aware — real events are never hidden; only EMPTY states change.
  · off  — no gating signal at all
  · soft — framing dict for empty states, blocked=False everywhere
  · hard — blocked=True for peers only; browse still framing-only
Fail-open: any lookup error → None (a gating bug must never lock discovery).
"""

import os
import unittest
from unittest.mock import MagicMock, patch

from app.zip_unlock import discovery_zip_gate, gate_framing_facts, gate_mode


def _sb(rows):
    m = MagicMock()
    chain = MagicMock()
    for meth in ("select", "eq", "limit", "order"):
        getattr(chain, meth).return_value = chain
    chain.execute.return_value = MagicMock(data=rows)
    m.table.return_value = chain
    return m


_PROFILE = {"home_zip": "32827"}
_WARMING = {"zip5": "32827", "unlock_state": "warming", "verified_active_count": 7, "unlock_threshold": 10}
_OPEN = {**_WARMING, "unlock_state": "open"}


class TestGateMode(unittest.TestCase):
    def test_default_is_soft_and_junk_is_soft(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LANA_ZIP_UNLOCK_GATE", None)
            self.assertEqual(gate_mode(), "soft")
        with patch.dict(os.environ, {"LANA_ZIP_UNLOCK_GATE": "banana"}):
            self.assertEqual(gate_mode(), "soft")
        with patch.dict(os.environ, {"LANA_ZIP_UNLOCK_GATE": "HARD"}):
            self.assertEqual(gate_mode(), "hard")


class TestDiscoveryZipGate(unittest.TestCase):
    def _gate(self, mode, unlock_row, surface):
        with patch.dict(os.environ, {"LANA_ZIP_UNLOCK_GATE": mode}), \
             patch("app.zip_unlock._user_home_zip", return_value=dict(_PROFILE)), \
             patch("app.zip_unlock.service_client", return_value=_sb([unlock_row] if unlock_row else [])), \
             patch("app.zip_unlock.recount_zip", return_value=None):
            return discovery_zip_gate("u1", surface=surface)

    def test_off_mode_returns_none(self) -> None:
        self.assertIsNone(self._gate("off", _WARMING, "peers"))

    def test_open_area_returns_none(self) -> None:
        self.assertIsNone(self._gate("hard", _OPEN, "peers"))

    def test_soft_frames_peers_without_blocking(self) -> None:
        frame = self._gate("soft", _WARMING, "peers")
        self.assertIsNotNone(frame)
        self.assertFalse(frame["blocked"])
        self.assertEqual(frame["count"], 7)
        self.assertEqual(frame["threshold"], 10)

    def test_browse_blocked_pre_open_in_every_mode(self) -> None:
        # §D.2 as amended by the rapport-bridge spec (2026-07-30): others' events
        # are never surfaced before the area opens — soft AND hard.
        self.assertTrue(self._gate("soft", _WARMING, "browse")["blocked"])
        self.assertTrue(self._gate("hard", _WARMING, "browse")["blocked"])

    def test_hard_blocks_peers(self) -> None:
        self.assertTrue(self._gate("hard", _WARMING, "peers")["blocked"])

    def test_unknown_zip_fails_open(self) -> None:
        # No unlock row AND the recount fails → None (never lock discovery on a gap).
        self.assertIsNone(self._gate("hard", None, "peers"))

    def test_no_user_returns_none(self) -> None:
        with patch.dict(os.environ, {"LANA_ZIP_UNLOCK_GATE": "hard"}):
            self.assertIsNone(discovery_zip_gate(None, surface="peers"))

    def test_lookup_error_fails_open(self) -> None:
        with patch.dict(os.environ, {"LANA_ZIP_UNLOCK_GATE": "hard"}), \
             patch("app.zip_unlock._user_home_zip", side_effect=RuntimeError("boom")):
            self.assertIsNone(discovery_zip_gate("u1", surface="peers"))


class TestFramingFacts(unittest.TestCase):
    def test_facts_are_lingo_clean_and_carry_progress(self) -> None:
        facts = gate_framing_facts({"count": 7, "threshold": 10})
        joined = " ".join(facts).lower()
        self.assertIn("7 of 10", joined)
        for banned in ("block", "circle", "zip 3", "mom"):
            self.assertNotIn(banned, joined)

    def test_no_threshold_still_frames(self) -> None:
        facts = gate_framing_facts({"count": 0, "threshold": None})
        self.assertTrue(any("coming alive" in f for f in facts))


class TestPeersGateTurn(unittest.TestCase):
    def _turn(self, frame):
        from app.discovery_route import _zip_gate_peers_turn

        with patch("app.zip_unlock.discovery_zip_gate", return_value=frame), \
             patch("app.reply_compose.compose_reply", side_effect=lambda **kw: kw["fallback"]):
            return _zip_gate_peers_turn({}, user_id="u1", block_id="b1")

    def test_blocked_frame_returns_seed_turn(self) -> None:
        turn = self._turn({"blocked": True, "count": 7, "threshold": 10, "state": "warming"})
        self.assertIsNotNone(turn)
        reply, ctx, routing, rows = turn
        self.assertIn("coming alive", reply)
        self.assertEqual(ctx["suggestions"], ["Host a meet"])
        self.assertEqual(routing["tool_to_call"], "zip_gate_peers")
        self.assertEqual(rows, [])

    def test_soft_frame_proceeds(self) -> None:
        self.assertIsNone(self._turn({"blocked": False, "count": 7, "threshold": 10}))

    def test_no_frame_proceeds(self) -> None:
        self.assertIsNone(self._turn(None))


if __name__ == "__main__":
    unittest.main()
