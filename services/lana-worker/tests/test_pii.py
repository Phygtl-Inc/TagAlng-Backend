"""PII redaction backstop (app/pii.py)."""

import unittest

from app.pii import (
    child_pii_ack_line,
    child_stage_band,
    detect_child_stage,
    enforce_child_pii_nonstorage,
    has_child_pii,
    redact_child_attributes,
    redact_pii,
    reply_asserts_child_pii_storage,
)

# The production QA finding (2026-07-08): this exact sentence must never persist
# with the child's name, school name, or exact age.
QA_SENTENCE = "my daughter Emma is 4, she goes to Sunshine Preschool on Narcoossee Rd"


class TestRedactPii(unittest.TestCase):
    def test_passthrough(self):
        self.assertIsNone(redact_pii(None))
        self.assertEqual(redact_pii(""), "")
        self.assertEqual(redact_pii("Running Enthusiast"), "Running Enthusiast")

    def test_email_and_phone(self):
        self.assertEqual(redact_pii("reach me at jane@doe.com"), "reach me at [email]")
        self.assertEqual(redact_pii("call 407-555-0134"), "call [phone]")
        self.assertEqual(redact_pii("+1 (407) 555 0134"), "[phone]")

    def test_address(self):
        self.assertEqual(redact_pii("we live at 123 Maple Street"), "we live at [address]")
        self.assertIn("[address]", redact_pii("42 Lake Nona Blvd is home"))

    def test_school(self):
        self.assertEqual(redact_pii("kindergarten at Lincoln Elementary"),
                         "kindergarten at [school]")
        self.assertIn("[school]", redact_pii("she's at Windermere High School"))

    def test_child_name_after_kinship(self):
        self.assertEqual(redact_pii("my daughter Sara loves the park"),
                         "my daughter [kid] loves the park")
        self.assertEqual(redact_pii("my son named Leo"), "my son named [kid]")

    def test_named_without_kinship(self):
        self.assertEqual(redact_pii("her name, we call her Mia"), "her name, we call her [name]")

    # ── no over-redaction of ordinary text ───────────────────────────────────
    def test_does_not_touch_job_title(self):
        self.assertEqual(redact_pii("elementary school teacher"), "elementary school teacher")

    def test_does_not_touch_common_lowercase_after_kinship(self):
        self.assertEqual(redact_pii("my daughter loves karate"), "my daughter loves karate")

    def test_does_not_touch_short_number_runs(self):
        self.assertEqual(redact_pii("married 10 years, 2 kids"), "married 10 years, 2 kids")


class TestChildStageBands(unittest.TestCase):
    """0-1 baby · 1-3 toddler · 3-5 prek · 5+ school."""

    def test_band_mapping(self):
        self.assertEqual(child_stage_band(0.5), "baby")
        self.assertEqual(child_stage_band(1), "toddler")
        self.assertEqual(child_stage_band(2), "toddler")
        self.assertEqual(child_stage_band(3), "prek")
        self.assertEqual(child_stage_band(4), "prek")
        self.assertEqual(child_stage_band(5), "school")
        self.assertEqual(child_stage_band(9), "school")

    def test_qa_sentence_redacts_to_stage_band(self):
        out = redact_pii(QA_SENTENCE)
        self.assertNotIn("Emma", out)
        self.assertNotIn("Sunshine", out)
        self.assertNotIn(" 4", out)  # exact age gone
        self.assertIn("[child_stage:prek]", out)
        self.assertIn("a local preschool", out)

    def test_child_age_variants(self):
        self.assertIn("[child_stage:toddler]", redact_pii("my son is 2"))
        self.assertIn("[child_stage:baby]", redact_pii("our baby is 8 months old"))
        self.assertIn("[child_stage:school]", redact_pii("my kid just turned 6 years old"))
        self.assertIn("[child_stage:prek]", redact_pii("my daughter Emma, she's 4"))

    def test_attributive_age_in_child_context(self):
        self.assertIn("[child_stage:prek]", redact_pii("my kid, a 4-year-old, loves the park"))

    def test_adult_ages_untouched(self):
        self.assertEqual(redact_pii("I am 35 years old"), "I am 35 years old")
        self.assertEqual(
            redact_pii("my kid loves my 40-year-old sister"),
            "my kid loves my 40-year-old sister",
        )

    def test_quantities_near_kids_untouched(self):
        self.assertEqual(redact_pii("my kids are 2 blocks away"), "my kids are 2 blocks away")

    def test_detect_child_stage(self):
        self.assertEqual(detect_child_stage(QA_SENTENCE), "prek")
        self.assertEqual(detect_child_stage("my son is 18 months old"), "toddler")
        self.assertIsNone(detect_child_stage("I run marathons"))
        self.assertIsNone(detect_child_stage("married 10 years, 2 kids"))

    def test_school_generalization(self):
        self.assertIn("a local preschool", redact_pii("she's at Bright Start Daycare"))
        # Non-preschool types keep the neutral placeholder.
        self.assertIn("[school]", redact_pii("he attends Windermere High School"))

    def test_has_child_pii(self):
        self.assertTrue(has_child_pii(QA_SENTENCE))
        self.assertTrue(has_child_pii("my son Leo starts soon"))
        self.assertTrue(has_child_pii("enrolled at Sunshine Preschool"))
        self.assertFalse(has_child_pii("I love hiking and coffee"))
        self.assertFalse(has_child_pii("married 10 years, 2 kids"))


class TestNonStorageAcknowledgment(unittest.TestCase):
    """Chat must never assert storage the Profile promise denies."""

    QA_BAD_REPLY = (
        "Of course! I keep Emma's name and school private unless you choose to "
        "share them directly with neighbors you connect with."
    )

    def test_detects_storage_assertion(self):
        self.assertTrue(reply_asserts_child_pii_storage(self.QA_BAD_REPLY))
        self.assertTrue(reply_asserts_child_pii_storage("I've saved her school to your profile."))
        self.assertTrue(reply_asserts_child_pii_storage("I'll remember their ages for you!"))

    def test_ignores_benign_replies(self):
        self.assertFalse(reply_asserts_child_pii_storage("Love that — welcome to the block!"))
        # In-session use of the name is allowed.
        self.assertFalse(
            reply_asserts_child_pii_storage("Emma sounds like a sweetheart — what does she love to do?")
        )
        # The user's OWN name is stored — saying so is fine.
        self.assertFalse(reply_asserts_child_pii_storage("I keep your name on your profile."))
        # The non-storage line itself must not re-trigger the guard.
        self.assertFalse(reply_asserts_child_pii_storage(child_pii_ack_line(QA_SENTENCE)))

    def test_rewrites_to_non_storage_line(self):
        out = enforce_child_pii_nonstorage(self.QA_BAD_REPLY, QA_SENTENCE)
        self.assertNotIn("keep Emma's name", out)
        self.assertIn("I don't keep her name", out)
        self.assertIn("pre-K kiddo", out)
        self.assertIn("helps me match you", out)

    def test_rewrite_keeps_surrounding_sentences(self):
        reply = "So glad you shared that! " + self.QA_BAD_REPLY + " What do you hope to find nearby?"
        out = enforce_child_pii_nonstorage(reply, QA_SENTENCE)
        self.assertIn("So glad you shared that!", out)
        self.assertIn("What do you hope to find nearby?", out)
        self.assertIn("I don't keep her name", out)

    def test_leaves_clean_reply_untouched(self):
        reply = "Emma sounds like a sweetheart — what does she love to do?"
        self.assertEqual(enforce_child_pii_nonstorage(reply, QA_SENTENCE), reply)

    def test_ack_line_pronoun_and_stage(self):
        line = child_pii_ack_line("my son Leo is 2")
        self.assertIn("his name", line)
        self.assertIn("toddler kiddo", line)
        generic = child_pii_ack_line("do you store my kids' info?")
        self.assertIn("I don't keep", generic)

    def test_sanitize_assistant_message_wires_the_guard(self):
        from app.lana_ui import sanitize_assistant_message

        out = sanitize_assistant_message(self.QA_BAD_REPLY, user_message=QA_SENTENCE)
        self.assertIn("I don't keep her name", out)
        self.assertIn("pre-K kiddo", out)


class TestRedactChildAttributes(unittest.TestCase):
    def test_child_age_becomes_stage_band(self):
        out = redact_child_attributes({"child_age": 4, "frequency": "weekly"}, subject="child")
        self.assertEqual(out, {"child_stage": "prek", "frequency": "weekly"})

    def test_name_and_school_keys_dropped(self):
        out = redact_child_attributes(
            {"name": "Emma", "school_name": "Sunshine Preschool", "age": 4},
            subject="child",
        )
        self.assertEqual(out, {"child_stage": "prek"})

    def test_child_keyed_attrs_sanitized_even_for_other_subjects(self):
        out = redact_child_attributes({"child_age": 8}, subject="self")
        self.assertEqual(out, {"child_stage": "school"})

    def test_adult_attrs_kept_with_text_redaction(self):
        out = redact_child_attributes(
            {"place": "we met at Sunshine Preschool", "frequency": "weekly"},
            subject="self",
        )
        self.assertEqual(out["frequency"], "weekly")
        self.assertNotIn("Sunshine", out["place"])

    def test_garbage_input(self):
        self.assertEqual(redact_child_attributes(None, subject="child"), {})
        self.assertEqual(redact_child_attributes("nope", subject="child"), {})


if __name__ == "__main__":
    unittest.main()
