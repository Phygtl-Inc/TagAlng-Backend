import unittest

from app.orchestrator.memory import (
    apply_core_patch,
    derive_session_state,
    merge_core_blocks,
    strip_ephemeral,
)


class MemoryTests(unittest.TestCase):
    def test_merge_keeps_persisted_last_topic(self):
        persisted = {"session": {"last_topic": "tutor search", "current_goal": "find help"}}
        fresh = {"session": {"last_topic": None, "current_goal": "learn identity"}}
        merged = merge_core_blocks(persisted, fresh)
        self.assertEqual(merged["session"]["last_topic"], "tutor search")
        self.assertEqual(merged["session"]["current_goal"], "find help")

    def test_apply_core_patch_whitelist(self):
        core = {"session": {}, "active_signals": {}}
        patch = {
            "session": {
                "last_topic": "coffee host",
                "state": "hacked",
            }
        }
        out = apply_core_patch(core, patch)
        self.assertEqual(out["session"]["last_topic"], "coffee host")
        self.assertNotIn("state", out["session"])

    def test_strip_ephemeral(self):
        core = {"session": {}, "_prefetch": [{"content": "x"}]}
        self.assertNotIn("_prefetch", strip_ephemeral(core))

    def test_derive_session_state_greeting(self):
        self.assertEqual(derive_session_state("profile_intake", {}, []), "greeting")

    def test_derive_session_state_acting(self):
        ctx = {"event_draft": {"title": "Coffee"}}
        self.assertEqual(
            derive_session_state("event_draft", ctx, [{"role": "user", "content": "hi"}]),
            "acting",
        )


if __name__ == "__main__":
    unittest.main()
