import unittest
from unittest.mock import MagicMock, patch

from app.i18n import _STRINGS, t

# Every notification string must exist in all three launch languages. A missing
# es/pt entry silently degrades that recipient to English, which is the exact
# bug this suite exists to prevent — and it fails quietly, so assert it.
_LANGS = ("en", "es", "pt")


class TestCatalogCompleteness(unittest.TestCase):
    def test_every_notify_key_has_all_languages(self) -> None:
        keys = [k for k in _STRINGS if k.startswith("notify.")]
        self.assertGreater(len(keys), 10, "notification catalog looks empty")
        for key in keys:
            for lang in _LANGS:
                self.assertIn(lang, _STRINGS[key], f"{key} missing '{lang}'")
                self.assertTrue(str(_STRINGS[key][lang]).strip(), f"{key}[{lang}] blank")

    def test_placeholders_match_across_languages(self) -> None:
        """A translation that drops {title} renders a sentence about nothing;
        one that invents a placeholder raises at format time."""
        import re

        for key in [k for k in _STRINGS if k.startswith("notify.")]:
            en = set(re.findall(r"\{(\w+)\}", _STRINGS[key]["en"]))
            for lang in ("es", "pt"):
                got = set(re.findall(r"\{(\w+)\}", _STRINGS[key][lang]))
                self.assertEqual(en, got, f"{key}[{lang}] placeholders {got} != en {en}")


class TestRendering(unittest.TestCase):
    """No LLM configured in tests, so t() returns the hand-written table entry —
    which is exactly the path a push takes when the LLM is down."""

    def test_cancellation_localizes(self) -> None:
        self.assertEqual(
            t("notify.cancelled.body", "es"),
            "El anfitrión canceló este plan. Lo sentimos — ojalá a la próxima.",
        )
        self.assertEqual(
            t("notify.cancelled.body", "pt"),
            "O anfitrião cancelou este encontro. Sentimos muito — quem sabe na próxima.",
        )

    def test_event_title_is_never_translated(self) -> None:
        """The title is the host's own words. t() formats placeholders AFTER
        picking the language, so it survives verbatim in every locale."""
        for lang in _LANGS:
            out = t("notify.cancelled.title", lang, title="Pizza Playdate")
            self.assertIn("Pizza Playdate", out, lang)

    def test_unknown_locale_falls_back_to_english(self) -> None:
        self.assertEqual(
            t("notify.cancelled.body", "fr"),
            _STRINGS["notify.cancelled.body"]["en"],
        )

    def test_none_locale_is_english(self) -> None:
        self.assertEqual(
            t("notify.area_open.title", None), "Your neighborhood just came alive"
        )


class TestRecipientLang(unittest.TestCase):
    @patch("app.notifications.service_client")
    def test_reads_users_locale(self, sb) -> None:
        from app.notifications import recipient_lang

        sb.return_value.table.return_value.select.return_value.eq.return_value.single.return_value.execute.return_value = MagicMock(
            data={"locale": "es"}
        )
        self.assertEqual(recipient_lang("u1"), "es")

    @patch("app.notifications.service_client")
    def test_failure_degrades_to_english_not_an_exception(self, sb) -> None:
        from app.notifications import recipient_lang

        sb.return_value.table.side_effect = RuntimeError("db down")
        # None => English. A locale lookup must never cost someone their
        # notification entirely.
        self.assertIsNone(recipient_lang("u1"))

    @patch("app.notifications.service_client")
    def test_roster_lookup_is_one_query(self, sb) -> None:
        from app.notifications import recipient_langs

        chain = sb.return_value.table.return_value.select.return_value.in_.return_value
        chain.execute.return_value = MagicMock(
            data=[{"id": "a", "locale": "es"}, {"id": "b", "locale": None}]
        )
        out = recipient_langs(["a", "b"])
        self.assertEqual(out, {"a": "es", "b": None})
        # One table() call for the whole roster — the area-open push fans out to
        # 500 people and must not issue 500 lookups.
        self.assertEqual(sb.return_value.table.call_count, 1)

    def test_empty_roster_short_circuits(self) -> None:
        from app.notifications import recipient_langs

        self.assertEqual(recipient_langs([]), {})


if __name__ == "__main__":
    unittest.main()
