"""Change-ZIP must SAVE the ZIP it asks for (prod 2026-10-07).

A fresh account with a Minneapolis profile ZIP asked to change it, then sent 95192 four
times and 94404 once; every reply asked to confirm the 5-digit ZIP and then said Lana
"can't update the ZIP from here". The arm only knew how to ask, and nothing wrote a new
ZIP for someone who already had a home block. These pin the answer turn.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

ARMED = {"active_intent": "settings.change_zip", "routing_phase": "need_zip"}


def _composed(**kw):
    return "COMPOSED:" + kw["goal"]


class ChangeZipApplyTests(unittest.TestCase):
    def _turn(
        self,
        *,
        msg="95192",
        slots=None,
        linear=None,
        phase="need_zip",
        ctx=None,
        coverage=({"block_id": "sj1", "display_name": "San Jose (95192)"}, "created"),
        phone_verified=True,
        rpc=None,
    ):
        from app.discovery_route import _try_layer1_intent_turn

        rpc = rpc or unittest.mock.MagicMock(return_value={"home_block_id": "sj1"})
        with patch("app.discovery_route.slots_linear_intent", return_value=linear), patch(
            "app.discovery_route.intent_confidence_met", return_value=bool(linear)
        ), patch("app.discovery_route.compose_reply", side_effect=_composed), patch(
            "app.discovery_route.resolve_zip_coverage", return_value=coverage
        ) as cov, patch("app.discovery_route.call_rpc", rpc), patch(
            "app.discovery_route.log_feature_request"
        ):
            out = _try_layer1_intent_turn(
                msg=msg,
                slots=slots or {},
                session_ctx=dict(ARMED if ctx is None else ctx),
                user_jwt="jwt",
                phone_verified=phone_verified,
                home_block_id="mpls1",
                phase=phase,
                user_id="u1",
            )
        return out, rpc, cov

    def test_bare_zip_answering_the_armed_ask_is_saved(self) -> None:
        # The classifier gives a bare ZIP no linear intent at all — it is still the answer.
        (reply, ctx, routing, _), rpc, _ = self._turn(slots={"zip": "95192"}, linear=None)
        rpc.assert_called_once_with(
            "jwt", "assign_home_block", {"p_block_id": "sj1", "p_home_zip": "95192"}
        )
        self.assertIn("it is saved", reply)
        self.assertEqual(ctx["routing_phase"], "listening")
        self.assertEqual(ctx["preview_block_id"], "sj1")
        self.assertIsNone(ctx["chat_area"])
        self.assertIn("settings_change_zip_saved", str(routing))

    def test_any_valid_zip_and_any_phrasing_is_saved_while_armed(self) -> None:
        # Other states, a campus ZIP, a PO-box-only ZIP, a leading-zero ZIP, and replies
        # worded in other languages: every one is the answer to the armed ask.
        cases = [
            ("10001", "10001"),
            ("my new one is 60614", "60614"),
            ("es 33130", "33130"),
            ("mera zip 77005 hai", "77005"),
            ("02138", "02138"),
            ("20013", "20013"),  # PO-box-only ZIP
            ("47907", "47907"),  # university campus ZIP
        ]
        for msg, z in cases:
            with self.subTest(msg=msg):
                (_, ctx, routing, _), rpc, _ = self._turn(
                    msg=msg,
                    slots={},
                    linear=None,
                    coverage=({"block_id": "b-" + z, "display_name": z}, "covered"),
                )
                rpc.assert_called_once_with(
                    "jwt", "assign_home_block", {"p_block_id": "b-" + z, "p_home_zip": z}
                )
                self.assertEqual(ctx["preview_zip"], z)
                self.assertIn("settings_change_zip_saved", str(routing))

    def test_digits_count_while_armed_even_without_the_zip_slot(self) -> None:
        (reply, _, routing, _), rpc, _ = self._turn(msg="94404", slots={}, linear="settings.change_zip")
        self.assertEqual(rpc.call_args.args[2]["p_home_zip"], "94404")
        self.assertIn("settings_change_zip_saved", str(routing))

    def test_zip_in_the_request_itself_is_saved_without_a_second_ask(self) -> None:
        (_, _, routing, _), rpc, _ = self._turn(
            msg="change my zip to 94404",
            slots={"zip": "94404"},
            linear="settings.change_zip",
            phase="listening",
            ctx={},
        )
        rpc.assert_called_once()
        self.assertIn("settings_change_zip_saved", str(routing))

    def test_unarmed_digits_are_not_taken_for_a_zip(self) -> None:
        # Not armed and no AI zip verdict: the arm asks, it does not write.
        (reply, ctx, _, _), rpc, cov = self._turn(
            msg="change my zip, 12345 is my old one",
            slots={},
            linear="settings.change_zip",
            phase="listening",
            ctx={},
        )
        rpc.assert_not_called()
        cov.assert_not_called()
        self.assertEqual(ctx["routing_phase"], "need_zip")

    def test_unarmed_bare_zip_without_intent_is_not_this_arms(self) -> None:
        out, rpc, _ = self._turn(slots={"zip": "95192"}, linear=None, phase="listening", ctx={})
        self.assertIsNone(out)
        rpc.assert_not_called()

    def test_not_a_real_zip_gets_one_honest_answer(self) -> None:
        (reply, ctx, routing, _), rpc, _ = self._turn(
            msg="00000", slots={"zip": "00000"}, coverage=(None, "invalid")
        )
        rpc.assert_not_called()
        self.assertIn("00000", reply)
        self.assertNotIn("COMPOSED:You already asked", reply)
        self.assertIn("settings_change_zip_invalid", str(routing))

    def test_unplaceable_zip_releases_the_ask(self) -> None:
        (reply, ctx, routing, _), rpc, _ = self._turn(slots={"zip": "95192"}, coverage=(None, "uncovered"))
        rpc.assert_not_called()
        self.assertIn("Do not ask for the ZIP again", reply)
        self.assertEqual(ctx["routing_phase"], "listening")
        self.assertEqual(ctx["pending_zip"], "95192")
        self.assertIn("settings_change_zip_unplaced", str(routing))

    def test_failed_save_is_never_reported_as_saved(self) -> None:
        from fastapi import HTTPException

        boom = unittest.mock.MagicMock(side_effect=HTTPException(status_code=400, detail="x"))
        (reply, ctx, routing, _), _, _ = self._turn(slots={"zip": "95192"}, rpc=boom)
        self.assertNotIn("it is saved", reply)
        self.assertIn("settings_change_zip_unplaced", str(routing))

    def test_guest_gets_the_area_for_the_session_without_a_write(self) -> None:
        (reply, ctx, routing, _), rpc, _ = self._turn(slots={"zip": "95192"}, phone_verified=False)
        rpc.assert_not_called()
        self.assertEqual(ctx["preview_zip"], "95192")
        self.assertIn("once they verify", reply)


if __name__ == "__main__":
    unittest.main()
