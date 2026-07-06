"""PII redaction backstop (app/pii.py)."""

import unittest

from app.pii import redact_pii


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


if __name__ == "__main__":
    unittest.main()
