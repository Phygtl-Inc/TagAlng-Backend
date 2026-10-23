"""Community knowledge documents — conversion, sections, the quote check, the answer loop.

The quote check is the hallucination guard, so it gets the most cases: a test that only
proved "a correct quote passes" would pass with the check deleted. The answer-loop tests
drive answer_from_community_docs with a scripted model and assert on what reaches the
user: a fabricated quote must never survive into the reply.
"""

import io
import unittest
import zipfile
from typing import Any
from unittest.mock import patch

import app.community_docs as cd
import app.community_docs_answer as cda
from app.community_docs_convert import (
    ConvertError,
    detect_kind,
    split_sections,
    to_markdown,
)

PLACE = "44444444-4444-4444-4444-444444444444"
ME = "11111111-1111-1111-1111-111111111111"
DOC = "55555555-5555-5555-5555-555555555555"


# ── conversion ──────────────────────────────────────────────────────────────────


def _docx(paragraphs: list[tuple[str | None, str]]) -> bytes:
    """A minimal real .docx: (style_id or None, text) per paragraph."""
    body = "".join(
        "<w:p>"
        + (f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else "")
        + f"<w:r><w:t xml:space=\"preserve\">{text}</w:t></w:r></w:p>"
        for style, text in paragraphs
    )
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    files = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
            "</Types>"
        ),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            "</Relationships>"
        ),
        "word/_rels/document.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            "</Relationships>"
        ),
        "word/styles.xml": (
            f'<?xml version="1.0" encoding="UTF-8"?><w:styles {ns}>'
            '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/></w:style>'
            '<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/></w:style>'
            "</w:styles>"
        ),
        "word/document.xml": f'<?xml version="1.0" encoding="UTF-8"?><w:document {ns}><w:body>{body}</w:body></w:document>',
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, content in files.items():
            z.writestr(name, content)
    return buf.getvalue()


class DetectKindTest(unittest.TestCase):
    def test_declared_type_wins(self):
        self.assertEqual(detect_kind("x.bin", "application/pdf"), "pdf")

    def test_extension_when_type_is_generic(self):
        # Browsers send .md as '' or octet-stream.
        self.assertEqual(detect_kind("Rules.MD", "application/octet-stream"), "md")
        self.assertEqual(detect_kind("guide.docx", ""), "docx")

    def test_unknown_is_refused(self):
        self.assertIsNone(detect_kind("photo.png", "image/png"))


class ToMarkdownTest(unittest.TestCase):
    def test_md_is_cleaned_not_changed(self):
        md = to_markdown("﻿# Rules\r\n\r\n\r\n\r\nBe kind.  \r\n".encode(), "md")
        self.assertEqual(md, "# Rules\n\nBe kind.")

    def test_docx_keeps_headings_and_drops_escapes(self):
        raw = _docx([("Heading1", "Membership"), (None, "Dues are $40.50 a year (students free).")])
        md = to_markdown(raw, "docx")
        self.assertIn("# Membership", md)
        # mammoth escapes "." and "(" — the quote check compares against this text.
        self.assertIn("Dues are $40.50 a year (students free).", md)

    def test_broken_docx_is_a_named_failure(self):
        with self.assertRaises(ConvertError) as ctx:
            to_markdown(b"not a zip", "docx")
        self.assertEqual(ctx.exception.code, "docx_unreadable")

    def test_empty_file(self):
        with self.assertRaises(ConvertError) as ctx:
            to_markdown(b"", "md")
        self.assertEqual(ctx.exception.code, "empty_file")

    def test_pdf_uses_gemini_text_per_batch(self):
        from pypdf import PdfWriter

        w = PdfWriter()
        for _ in range(5):
            w.add_blank_page(width=200, height=200)
        buf = io.BytesIO()
        w.write(buf)
        calls: list[int] = []

        def fake_batch(pdf_bytes: bytes) -> str:
            calls.append(len(pdf_bytes))
            return f"## Part {len(calls)}\n\nText {len(calls)}."

        with patch("app.orchestrator.llm.vertex_configured", return_value=True), patch(
            "app.community_docs_convert._pdf_batch_via_gemini", side_effect=fake_batch
        ):
            md = to_markdown(buf.getvalue(), "pdf")
        self.assertEqual(len(calls), 2)  # 5 pages at 4 per call
        self.assertIn("## Part 1", md)
        self.assertIn("## Part 2", md)

    def test_pdf_with_no_text_anywhere_fails(self):
        from pypdf import PdfWriter

        w = PdfWriter()
        w.add_blank_page(width=200, height=200)
        buf = io.BytesIO()
        w.write(buf)
        with patch("app.orchestrator.llm.vertex_configured", return_value=False):
            with self.assertRaises(ConvertError) as ctx:
                to_markdown(buf.getvalue(), "pdf")
        self.assertEqual(ctx.exception.code, "no_text_found")


class SplitSectionsTest(unittest.TestCase):
    MD = (
        "Welcome to the club.\n\n"
        "# Membership\n\n"
        "## Dues\n\nDues are $40 a year.\n\n"
        "## Refunds\n\nRefunds within 14 days.\n\n"
        "# Code\n\n```\n# not a heading\n```\n"
    )

    def test_heading_paths(self):
        secs = split_sections(self.MD, "Handbook")
        paths = [s.heading_path for s in secs]
        self.assertEqual(
            paths,
            [
                ["Handbook"],
                ["Handbook", "Membership", "Dues"],
                ["Handbook", "Membership", "Refunds"],
                ["Handbook", "Code"],
            ],
        )

    def test_heading_with_no_text_is_not_a_section(self):
        secs = split_sections(self.MD, "Handbook")
        self.assertNotIn(["Handbook", "Membership"], [s.heading_path for s in secs])

    def test_heading_inside_code_fence_is_text(self):
        code = split_sections(self.MD, "Handbook")[-1]
        self.assertIn("# not a heading", code.body)

    def test_ords_are_sequential(self):
        self.assertEqual([s.ord for s in split_sections(self.MD, "H")], [0, 1, 2, 3])

    def test_long_section_splits_at_paragraphs(self):
        para = "word " * 200  # ~1000 chars
        md = "# Big\n\n" + "\n\n".join([para.strip()] * 8)
        secs = split_sections(md, "Doc", max_chars=2500)
        self.assertGreater(len(secs), 1)
        self.assertTrue(all(len(s.body) <= 2500 for s in secs))
        self.assertEqual(secs[0].heading_path[-1], f"Big (1/{len(secs)})")
        # Nothing lost.
        self.assertEqual(sum(s.body.count("word") for s in secs), 1600)


# ── the quote check ─────────────────────────────────────────────────────────────

BODY = (
    "**Annual dues** are $40 per member.\n"
    "- Students pay “half price” — $20.\n"
    "| Day | Time |\n| Tuesday | 7pm |"
)


class QuoteCheckTest(unittest.TestCase):
    def test_exact_passes(self):
        self.assertTrue(cda.quote_in_section("are $40 per member", BODY))

    def test_markup_and_case_are_ignored(self):
        self.assertTrue(cda.quote_in_section("Annual dues are $40 per member.", BODY))

    def test_smart_quotes_dashes_and_bullets(self):
        self.assertTrue(cda.quote_in_section('- Students pay "half price" - $20.', BODY))

    def test_table_cells(self):
        self.assertTrue(cda.quote_in_section("Tuesday 7pm", BODY))

    def test_changed_number_fails(self):
        self.assertFalse(cda.quote_in_section("Annual dues are $45 per member", BODY))

    def test_paraphrase_fails(self):
        self.assertFalse(cda.quote_in_section("membership costs forty dollars", BODY))

    def test_too_short_fails(self):
        self.assertFalse(cda.quote_in_section("$4", BODY))

    def test_wrong_section_fails(self):
        a = cda.DocSection("S1", "a", "d", "Doc", ["Doc"], "", BODY, 10)
        b = cda.DocSection("S2", "b", "d", "Doc", ["Doc"], "", "Meetings are on Fridays.", 5)
        good, bad = cda.verify_claims(
            [{"fact": "dues", "section": "S2", "quote": "are $40 per member"}],
            {"S1": a, "S2": b},
        )
        self.assertEqual(good, [])
        self.assertEqual(len(bad), 1)

    def test_unknown_section_fails(self):
        good, bad = cda.verify_claims([{"section": "S9", "quote": "anything at all"}], {})
        self.assertEqual((len(good), len(bad)), (0, 1))


# ── the answer loop ─────────────────────────────────────────────────────────────


def _big_collection() -> list[cda.DocSection]:
    """Over the full-mode budget the realistic way: many ordinary-size sections."""
    filler = [
        cda.DocSection(f"S{i}", f"sec-{i}", "doc-2", "Archive", ["Archive", f"Year {i}"], "old minutes",
                       "Minutes of a past meeting.", 1500)
        for i in range(3, 60)
    ]
    return _sections() + filler


def _sections(n_tokens: int = 50) -> list[cda.DocSection]:
    return [
        cda.DocSection("S1", "sec-1", "doc-1", "Handbook", ["Handbook", "Dues"], "annual dues amount",
                       "Annual dues are $40 per member.", n_tokens),
        cda.DocSection("S2", "sec-2", "doc-1", "Handbook", ["Handbook", "Meetings"], "meeting day and time",
                       "We meet every Tuesday at 7pm in Room 4.", n_tokens),
    ]


class AnswerLoopTest(unittest.TestCase):
    def setUp(self):
        self.calls: list[dict[str, Any]] = []
        self.audits: list[Any] = []
        self.patches = [
            patch.object(cda, "docs_enabled", return_value=True),
            patch("app.orchestrator.llm.llm_configured", return_value=True),
            patch.object(cda, "docs_role", return_value="member"),
            patch.object(cda, "_live_facts", return_value=[]),
            patch.object(cda, "_audit", side_effect=lambda *a: self.audits.append(a)),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def _run(self, replies: list[dict[str, Any]], sections=None):
        script = iter(replies)

        def fake_call(payload):
            self.calls.append(payload)
            return next(script)

        with patch.object(cda, "_load_sections", return_value=sections or _sections()), patch.object(
            cda, "_call", side_effect=fake_call
        ):
            return cda.answer_from_community_docs(
                user_id=ME, place_id=PLACE, question="how much are dues?", community_name="Chess Club"
            )

    def test_verified_answer_goes_out_unchanged(self):
        a = self._run([
            {"action": "answer", "reply": "Dues are $40 a year.", "coverage": "full",
             "claims": [{"fact": "dues $40", "section": "S1", "quote": "Annual dues are $40 per member."}]},
        ])
        self.assertEqual(a.reply, "Dues are $40 a year.")
        self.assertEqual(a.coverage, "full")
        self.assertFalse(a.retried)
        self.assertEqual(a.sources, [{"document": "Handbook", "section": "Dues"}])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["mode"], "full")
        self.assertEqual(len(self.audits), 1)

    def test_fabricated_quote_is_retried_with_the_failure_named(self):
        a = self._run([
            {"action": "answer", "reply": "Dues are $45.", "coverage": "full",
             "claims": [{"fact": "dues $45", "section": "S1", "quote": "Annual dues are $45"}]},
            {"action": "answer", "reply": "Dues are $40.", "coverage": "full",
             "claims": [{"fact": "dues $40", "section": "S1", "quote": "Annual dues are $40"}]},
        ])
        self.assertEqual(a.reply, "Dues are $40.")
        self.assertTrue(a.retried)
        self.assertEqual(a.claims_dropped, 0)
        failed = self.calls[1]["verification_failed"]
        self.assertEqual(failed[0]["quote"], "Annual dues are $45")

    def test_fabrication_twice_never_reaches_the_user(self):
        a = self._run([
            {"action": "answer", "reply": "Dues are $45.", "coverage": "full",
             "claims": [{"fact": "dues $45", "section": "S1", "quote": "dues are $45"}]},
            {"action": "answer", "reply": "Dues are $45, really.", "coverage": "full",
             "claims": [{"fact": "dues $45", "section": "S1", "quote": "dues are $45 per"}]},
            {"action": "answer", "reply": "The handbook doesn't confirm the dues amount.", "coverage": "none",
             "claims": []},
        ])
        self.assertNotIn("$45", a.reply)
        self.assertEqual(a.coverage, "none")
        self.assertEqual(self.calls[2]["mode"], "verified_only")
        self.assertEqual(self.calls[2]["verified_facts"], [])
        self.assertEqual(a.claims_dropped, 1)

    def test_partial_survivors_cap_coverage_at_partial(self):
        a = self._run([
            {"action": "answer", "reply": "$40, Tuesdays at 8pm.", "coverage": "full",
             "claims": [{"section": "S1", "quote": "Annual dues are $40"},
                        {"section": "S2", "quote": "Tuesday at 8pm"}]},
            {"action": "answer", "reply": "$40, Tuesdays at 8pm.", "coverage": "full",
             "claims": [{"section": "S1", "quote": "Annual dues are $40"},
                        {"section": "S2", "quote": "Tuesday at 8pm"}]},
            {"action": "answer", "reply": "Dues are $40.", "coverage": "full", "claims": []},
        ])
        self.assertEqual(a.coverage, "partial")
        self.assertEqual([f["quote"] for f in self.calls[2]["verified_facts"]], ["Annual dues are $40"])

    def test_factual_answer_without_claims_is_not_trusted(self):
        a = self._run([
            {"action": "answer", "reply": "Dues are $40.", "coverage": "full", "claims": []},
            {"action": "answer", "reply": "Dues are $40.", "coverage": "full",
             "claims": [{"section": "S1", "quote": "Annual dues are $40"}]},
        ])
        self.assertTrue(a.retried)
        self.assertEqual(a.coverage, "full")

    def test_honest_none_needs_no_claims(self):
        a = self._run([{"action": "answer", "reply": "The documents don't say.", "coverage": "none", "claims": []}])
        self.assertEqual(a.coverage, "none")
        self.assertFalse(a.retried)

    def test_large_collection_navigates_and_only_reads_what_it_opened(self):
        big = _big_collection()
        a = self._run(
            [
                {"action": "read", "sections": ["S1"]},
                {"action": "answer", "reply": "Dues are $40.", "coverage": "full",
                 "claims": [{"section": "S1", "quote": "Annual dues are $40"}]},
            ],
            sections=big,
        )
        self.assertEqual(a.mode, "navigate")
        self.assertEqual(self.calls[0]["mode"], "navigate")
        self.assertEqual(self.calls[0]["opened_sections"], [])
        self.assertNotIn("text", self.calls[0]["section_index"][0])
        self.assertEqual([s["section"] for s in self.calls[1]["opened_sections"]], ["S1"])
        self.assertEqual(a.sections_read, ["sec-1"])

    def test_navigate_is_forced_to_answer_after_max_steps(self):
        big = _big_collection()
        replies = [{"action": "find", "query": "dues"}] * cda._MAX_STEPS + [
            {"action": "answer", "reply": "The documents don't say.", "coverage": "none", "claims": []}
        ]
        with patch.object(cda, "_find", return_value=[]):
            a = self._run(replies, sections=big)
        self.assertTrue(self.calls[-1]["must_answer_now"])
        self.assertEqual(len(self.calls), cda._MAX_STEPS + 1)
        self.assertEqual(a.coverage, "none")

    def test_ignoring_the_last_step_wastes_no_lookup_and_falls_back(self):
        big = _big_collection()
        replies = [{"action": "find", "query": "dues"}] * (cda._MAX_STEPS + 1)
        with patch.object(cda, "_find", return_value=[]) as find:
            a = self._run(replies, sections=big)
        self.assertEqual(find.call_count, cda._MAX_STEPS)
        self.assertIsNone(a)

    def test_no_readable_documents_falls_back(self):
        with patch.object(cda, "_load_sections", return_value=[]):
            self.assertIsNone(
                cda.answer_from_community_docs(user_id=ME, place_id=PLACE, question="dues?")
            )

    def test_flag_off_falls_back(self):
        with patch.object(cda, "docs_enabled", return_value=False):
            self.assertIsNone(
                cda.answer_from_community_docs(user_id=ME, place_id=PLACE, question="dues?")
            )


LEVELS = {
    "amb": {"id": "amb", "name": "Ambassador", "rank": 1},
    "lead": {"id": "lead", "name": "Lead", "rank": 2},
    "eb": {"id": "eb", "name": "E-board", "rank": 3},
}


def _acc(role="member", rank=0, approve=False, log=False) -> cd.DocsAccess:
    return cd.DocsAccess(
        role=role, rank=rank, can_upload=rank >= 0, can_approve=approve, can_read_log=log
    )


class _Writes:
    """A client that records inserts, updates, storage uploads and deletes."""

    def __init__(self, doc: dict[str, Any] | None = None):
        self.inserted: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.uploaded: list[str] = []
        self.deleted = False
        self.doc = doc
        outer = self

        class _T:
            def __init__(self, name):
                self.name = name
                self.rows = [outer.doc] if (name == "community_documents" and outer.doc) else []

            def insert(self, row):
                outer.inserted.append(row)
                self.rows = [{"id": row.get("id"), **row}]
                return self

            def update(self, patch_):
                outer.updated.append(patch_)
                self.rows = [{**(outer.doc or {}), **patch_}]
                return self

            def delete(self):
                outer.deleted = True
                return self

            def __getattr__(self, _n):
                return lambda *a, **k: self

            def execute(self):
                return type("R", (), {"data": self.rows, "count": 0})()

        class _Bucket:
            def upload(self, path, *_a, **_k):
                outer.uploaded.append(path)

            def list(self, *_a, **_k):
                return []

            def remove(self, *_a, **_k):
                return None

        self._T = _T
        self.storage = type("S", (), {"from_": staticmethod(lambda _b: _Bucket())})()

    def table(self, name):
        return self._T(name)


class LevelRankTest(unittest.TestCase):
    def test_audiences_map_onto_the_ladder(self):
        self.assertEqual(cd.doc_rank({"visibility": "public"}, LEVELS), -1)
        self.assertEqual(cd.doc_rank({"visibility": "members"}, LEVELS), 0)
        self.assertEqual(cd.doc_rank({"visibility": "members", "min_level_id": "lead"}, LEVELS), 2)
        self.assertEqual(cd.doc_rank({"visibility": "owners"}, LEVELS), 1000)

    def test_a_missing_level_reads_as_owners_only(self):
        self.assertEqual(cd.doc_rank({"visibility": "members", "min_level_id": "gone"}, LEVELS), 1000)

    def test_a_level_means_members_and_must_belong_here(self):
        self.assertEqual(cd._target("public", "lead", LEVELS), ("members", "lead"))
        self.assertEqual(cd._target(None, None, LEVELS), ("members", None))
        self.assertEqual(cd._target("owners", None, LEVELS), ("owners", None))
        with self.assertRaises(cd.DocsError) as e:
            cd._target("members", "elsewhere", LEVELS)
        self.assertEqual(e.exception.code, "level_not_found")
        with self.assertRaises(cd.DocsError) as e:
            cd._target("everyone", None, LEVELS)
        self.assertEqual(e.exception.code, "invalid_visibility")


class UploadByLevelTest(unittest.TestCase):
    def _upload(self, acc, **kw):
        client = _Writes()
        with patch.object(cd, "docs_access", return_value=acc), patch.object(
            cd, "_levels", return_value=LEVELS
        ), patch.object(cd, "service_client", return_value=client):
            row = cd.upload_document(
                user_id=ME, place_id=PLACE, filename="tips.md", content_type="text/markdown",
                raw=b"# Tips", **kw,
            )
        return row, client

    def test_a_member_upload_waits_for_an_approver(self):
        row, client = self._upload(_acc(rank=1), level_id="amb")
        self.assertEqual(client.inserted[0]["review_status"], "pending")
        self.assertEqual(client.inserted[0]["min_level_id"], "amb")
        self.assertEqual(client.inserted[0]["visibility"], "members")
        self.assertEqual(row["review_status"], "pending")
        self.assertTrue(row["mine"])

    def test_an_approver_upload_is_live(self):
        _, client = self._upload(_acc(rank=2, approve=True), level_id="lead")
        self.assertEqual(client.inserted[0]["review_status"], "approved")

    def test_nobody_uploads_above_their_own_level(self):
        with self.assertRaises(cd.DocsError) as e:
            self._upload(_acc(rank=1), level_id="lead")
        self.assertEqual(e.exception.code, "level_too_high")

    def test_a_member_cannot_upload_owners_only(self):
        with self.assertRaises(cd.DocsError) as e:
            self._upload(_acc(rank=3, approve=True), visibility="owners")
        self.assertEqual(e.exception.code, "level_too_high")

    def test_nothing_is_stored_when_refused(self):
        client = _Writes()
        with patch.object(cd, "docs_access", return_value=_acc(rank=1)), patch.object(
            cd, "_levels", return_value=LEVELS
        ), patch.object(cd, "service_client", return_value=client):
            with self.assertRaises(cd.DocsError):
                cd.upload_document(user_id=ME, place_id=PLACE, filename="a.md", content_type="text/markdown",
                                   raw=b"# A", level_id="eb")
        self.assertEqual(client.uploaded, [])
        self.assertEqual(client.inserted, [])

    def test_a_visitor_cannot_upload(self):
        with self.assertRaises(cd.DocsError) as e:
            self._upload(_acc(role="visitor", rank=-1), visibility="public")
        self.assertEqual(e.exception.code, "not_member")

    def test_only_owners_replace_a_document(self):
        with self.assertRaises(cd.DocsError) as e:
            self._upload(_acc(rank=3, approve=True), replace_doc_id="d1")
        self.assertEqual(e.exception.code, "not_manager")


class ReviewTest(unittest.TestCase):
    def _review(self, acc, doc, approve=True):
        client = _Writes(doc)
        with patch.object(cd, "docs_access", return_value=acc), patch.object(
            cd, "_levels", return_value=LEVELS
        ), patch.object(cd, "service_client", return_value=client):
            row = cd.review_document(user_id=ME, doc_id=DOC, approve=approve)
        return row, client

    def _doc(self, **kw):
        return {"id": DOC, "place_id": PLACE, "title": "Tips", "visibility": "members",
                "min_level_id": "amb", "review_status": "pending", "uploaded_by": "someone", **kw}

    def test_an_approver_publishes(self):
        row, client = self._review(_acc(rank=2, approve=True), self._doc())
        self.assertEqual(client.updated[0]["review_status"], "approved")
        self.assertEqual(client.updated[0]["reviewed_by"], ME)

    def test_reject(self):
        _, client = self._review(_acc(rank=2, approve=True), self._doc(), approve=False)
        self.assertEqual(client.updated[0]["review_status"], "rejected")

    def test_a_member_who_cannot_approve_is_refused(self):
        with self.assertRaises(cd.DocsError) as e:
            self._review(_acc(rank=3), self._doc())
        self.assertEqual(e.exception.code, "not_approver")

    def test_an_approver_cannot_publish_above_their_level(self):
        with self.assertRaises(cd.DocsError) as e:
            self._review(_acc(rank=2, approve=True), self._doc(min_level_id="eb"))
        self.assertEqual(e.exception.code, "not_approver")

    def test_only_pending_documents_are_reviewed(self):
        with self.assertRaises(cd.DocsError) as e:
            self._review(_acc(role="manager", rank=1000, approve=True), self._doc(review_status="approved"))
        self.assertEqual(e.exception.code, "not_pending")


class WithdrawTest(unittest.TestCase):
    def _delete(self, acc, doc):
        client = _Writes(doc)
        with patch.object(cd, "docs_access", return_value=acc), patch.object(cd, "service_client", return_value=client):
            cd.delete_document(user_id=ME, doc_id=DOC)
        return client

    def test_uploader_withdraws_their_pending_upload(self):
        client = self._delete(_acc(rank=1), {"id": DOC, "place_id": PLACE, "uploaded_by": ME, "review_status": "pending"})
        self.assertTrue(client.deleted)

    def test_a_live_document_is_the_owners_to_remove(self):
        with self.assertRaises(cd.DocsError) as e:
            self._delete(_acc(rank=1), {"id": DOC, "place_id": PLACE, "uploaded_by": ME, "review_status": "approved"})
        self.assertEqual(e.exception.code, "not_manager")

    def test_nobody_withdraws_someone_elses(self):
        with self.assertRaises(cd.DocsError):
            self._delete(_acc(rank=3, approve=True), {"id": DOC, "place_id": PLACE, "uploaded_by": "other", "review_status": "pending"})


class HintLevelTest(unittest.TestCase):
    def test_level_restricted_and_pending_documents_are_never_advertised(self):
        client = _Client({
            "community_documents": [
                {"id": "h", "title": "Handbook", "visibility": "members"},
                {"id": "a", "title": "Applications", "visibility": "members", "min_level_id": "eb"},
                {"id": "o", "title": "Owner notes", "visibility": "owners"},
                {"id": "p", "title": "Pending tips", "visibility": "public", "review_status": "pending"},
            ],
            "community_doc_sections": [
                {"doc_id": d, "ord": 0, "heading_path": [d, "Part"], "covers": f"{d} topic"} for d in "haop"
            ],
        })
        with patch.object(cd, "docs_enabled", return_value=True), patch.object(cd, "service_client", return_value=client):
            line = cd.docs_cover_line(PLACE)
        self.assertIn("Handbook", line)
        for hidden in ("Applications", "Owner notes", "Pending tips"):
            self.assertNotIn(hidden, line)


class RoutingHintTest(unittest.TestCase):
    def test_docs_is_an_allowed_community_ask(self):
        from app.discovery_slots import slots_community_ask

        self.assertEqual(slots_community_ask({"community_ask": "docs"}), "docs")
        self.assertEqual(slots_community_ask({"community_ask": "bogus"}), "about")

    def test_prompt_line_carries_what_documents_cover(self):
        from app.community_opening import active_community_prompt_line

        facts = {"name": "Chess Club", "kind": "community", "docs_cover": "Handbook: annual dues amount"}
        with patch("app.community_opening.active_community_facts", return_value=facts):
            line = active_community_prompt_line({})
        self.assertIn("its documents cover: Handbook: annual dues amount", line)


if __name__ == "__main__":
    unittest.main()


class DispatchTest(unittest.TestCase):
    """A 'docs' verdict inside the active community reaches the answer engine — for a
    guest too (visibility is decided per document) — and None falls back to the about-turn."""

    CTX = {"active_community": {"place_id": PLACE, "name": "Chess Club"}}

    def _turn(self, *, verified: bool, ask: str | None = "docs", named: str | None = "Chess Club"):
        from app.discovery_route import _try_layer1_intent_turn

        return _try_layer1_intent_turn(
            msg="how much are dues?",
            slots={"linear_intent": "discovery.communities", "community_ask": ask,
                   "community_name": named},
            session_ctx=dict(self.CTX),
            user_jwt="jwt",
            phone_verified=verified,
            home_block_id=None,
            phase="listening",
            user_id=ME,
            history=[{"role": "user", "content": "hi"}],
        )

    @patch("app.discovery_route.intent_confidence_met", return_value=True)
    @patch("app.discovery_route.slots_linear_intent", return_value="discovery.communities")
    @patch("app.community_scope.active_community", return_value={"place_id": PLACE, "name": "Chess Club"})
    @patch("app.community_discovery.communities_chat_turn", return_value="about-turn")
    @patch("app.community_docs_answer.answer_from_community_docs")
    def test_docs_answer_ships_with_its_routing(self, answer, about, *_):
        answer.return_value = cda.DocsAnswer(
            reply="Dues are $40.", coverage="full",
            sources=[{"document": "Handbook", "section": "Dues"}], mode="full",
        )
        reply, ctx, routing, _ = self._turn(verified=False)
        self.assertEqual(reply, "Dues are $40.")
        self.assertEqual(routing["tool_to_call"], "community_docs_answer")
        self.assertEqual(routing["docs_coverage"], "full")
        self.assertEqual(ctx["last_routing"], routing)
        kwargs = answer.call_args.kwargs
        self.assertEqual(kwargs["place_id"], PLACE)
        self.assertEqual(kwargs["history"], [{"role": "user", "content": "hi"}])
        about.assert_not_called()

    @patch("app.discovery_route.intent_confidence_met", return_value=True)
    @patch("app.discovery_route.slots_linear_intent", return_value="discovery.communities")
    @patch("app.community_scope.active_community", return_value={"place_id": PLACE, "name": "Chess Club"})
    @patch("app.community_discovery.communities_chat_turn", return_value="about-turn")
    @patch("app.community_docs_answer.answer_from_community_docs", return_value=None)
    def test_nothing_to_answer_from_falls_back_to_about(self, answer, about, *_):
        reply, _, _, _ = self._turn(verified=True)
        self.assertEqual(reply, "about-turn")
        self.assertEqual(about.call_args.kwargs["community_ask"], "about")

    @patch("app.discovery_route.intent_confidence_met", return_value=True)
    @patch("app.discovery_route.slots_linear_intent", return_value="discovery.communities")
    @patch("app.community_scope.active_community", return_value={"place_id": PLACE, "name": "Chess Club"})
    @patch("app.community_discovery.communities_chat_turn", return_value="about-turn")
    @patch("app.community_docs_answer.answer_from_community_docs")
    def test_people_asks_never_touch_the_documents(self, answer, about, *_):
        self._turn(verified=True, ask="people")
        answer.assert_not_called()

    @patch("app.discovery_route.intent_confidence_met", return_value=True)
    @patch("app.discovery_route.slots_linear_intent", return_value="discovery.communities")
    @patch("app.community_scope.active_community", return_value={"place_id": PLACE, "name": "Chess Club"})
    @patch("app.community_discovery.communities_chat_turn", return_value="about-turn")
    @patch("app.community_docs_answer.answer_from_community_docs")
    def test_about_this_community_goes_to_the_documents_first(self, answer, about, *_):
        answer.return_value = cda.DocsAnswer(reply="Free parking after 6pm.", coverage="full")
        reply, _, routing, _ = self._turn(verified=True, ask="about")
        self.assertEqual(reply, "Free parking after 6pm.")
        self.assertEqual(routing["tool_to_call"], "community_docs_answer")
        about.assert_not_called()

    @patch("app.discovery_route.intent_confidence_met", return_value=True)
    @patch("app.discovery_route.slots_linear_intent", return_value="discovery.communities")
    @patch("app.community_scope.active_community", return_value={"place_id": PLACE, "name": "Chess Club"})
    @patch("app.community_discovery.communities_chat_turn", return_value="about-turn")
    @patch("app.community_docs_answer.answer_from_community_docs")
    def test_documents_not_covering_it_fall_back_to_about(self, answer, about, *_):
        answer.return_value = cda.DocsAnswer(reply="The documents don't say.", coverage="none")
        reply, _, _, _ = self._turn(verified=True, ask="about")
        self.assertEqual(reply, "about-turn")

    @patch("app.discovery_route.intent_confidence_met", return_value=True)
    @patch("app.discovery_route.slots_linear_intent", return_value="discovery.communities")
    @patch("app.community_scope.active_community", return_value={"place_id": PLACE, "name": "Chess Club"})
    @patch("app.community_discovery.communities_chat_turn", return_value="about-turn")
    @patch("app.community_docs_answer.answer_from_community_docs")
    def test_a_browse_never_touches_the_documents(self, answer, about, *_):
        # An empty label is a browse of other communities; slots_community_ask would
        # have turned it into 'about'.
        self._turn(verified=True, ask=None, named=None)
        answer.assert_not_called()

    @patch("app.discovery_route.intent_confidence_met", return_value=True)
    @patch("app.discovery_route.slots_linear_intent", return_value="discovery.communities")
    @patch("app.community_scope.active_community", return_value={"place_id": PLACE, "name": "Chess Club"})
    @patch("app.community_discovery.communities_chat_turn", return_value="about-turn")
    @patch("app.community_docs_answer.answer_from_community_docs")
    def test_a_browse_naming_this_community_never_touches_the_documents(self, answer, about, *_):
        self._turn(verified=True, ask=None, named="Chess Club")
        answer.assert_not_called()

    @patch("app.discovery_route.intent_confidence_met", return_value=True)
    @patch("app.discovery_route.slots_linear_intent", return_value="discovery.communities")
    @patch("app.community_scope.active_community", return_value={"place_id": PLACE, "name": "Chess Club"})
    @patch("app.community_discovery.communities_chat_turn", return_value="about-turn")
    @patch("app.community_docs_answer.answer_from_community_docs")
    def test_about_another_community_never_reads_this_ones_documents(self, answer, about, *_):
        self._turn(verified=True, ask="about", named="Westlake Robotics Team")
        answer.assert_not_called()

    @patch("app.discovery_route.intent_confidence_met", return_value=True)
    @patch("app.discovery_route.slots_linear_intent", return_value="discovery.communities")
    @patch("app.community_scope.active_community", return_value=None)
    @patch("app.community_discovery.communities_chat_turn", return_value="about-turn")
    @patch("app.community_docs_answer.answer_from_community_docs")
    def test_docs_outside_a_community_is_an_about_question(self, answer, about, *_):
        reply, _, _, _ = self._turn(verified=True)
        answer.assert_not_called()
        self.assertEqual(about.call_args.kwargs["community_ask"], "about")


class _Q:
    def __init__(self, rows):
        self._rows = rows

    def __getattr__(self, _name):
        return lambda *a, **k: self

    def execute(self):
        return type("R", (), {"data": self._rows})()


class _Client:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        return _Q(self.tables[name])


class DocsCoverLineTest(unittest.TestCase):
    def test_only_documents_with_sections_are_advertised(self):
        client = _Client({
            "community_documents": [{"id": "d1", "title": "Handbook"}, {"id": "d2", "title": "New upload"}],
            "community_doc_sections": [{"doc_id": "d1", "covers": "dues amount"}, {"doc_id": "d1", "covers": None}],
        })
        with patch.object(cd, "docs_enabled", return_value=True), patch.object(cd, "service_client", return_value=client):
            line = cd.docs_cover_line(PLACE)
        self.assertEqual(line, "Handbook: dues amount")

    def test_nothing_answerable_is_none(self):
        client = _Client({"community_documents": [{"id": "d2", "title": "New upload"}], "community_doc_sections": []})
        with patch.object(cd, "docs_enabled", return_value=True), patch.object(cd, "service_client", return_value=client):
            self.assertIsNone(cd.docs_cover_line(PLACE))


class DocsHintBudgetTest(unittest.TestCase):
    """2026-10-19 on dev: a 400-char cap filled by two documents cut the handbook off at
    'Club', so every dues / refund / age question routed away from the documents."""

    def _line(self, docs, sections):
        client = _Client({"community_documents": docs, "community_doc_sections": sections})
        with patch.object(cd, "docs_enabled", return_value=True), patch.object(cd, "service_client", return_value=client):
            return cd.docs_cover_line(PLACE)

    def test_a_long_document_cannot_push_the_others_out(self):
        long_covers = "parking, " * 80
        docs = [{"id": "v", "title": "Venue guide"}, {"id": "c", "title": "Conduct"}, {"id": "h", "title": "Club handbook"}]
        sections = (
            [{"doc_id": "v", "ord": i, "heading_path": ["Venue guide", f"Area {i}"], "covers": long_covers} for i in range(6)]
            + [{"doc_id": "c", "ord": 0, "heading_path": ["Conduct", "Phones"], "covers": "phone rule"}]
            + [{"doc_id": "h", "ord": i, "heading_path": ["Club handbook", h], "covers": "x " * 60}
               for i, h in enumerate(["Dues", "Refunds", "Meetings", "Juniors"])]
        )
        line = self._line(docs, sections)
        self.assertLessEqual(len(line), cd.DOCS_HINT_CHARS)
        for title in ("Venue guide", "Conduct", "Club handbook"):
            self.assertIn(title, line)
        # Squeezed, the handbook still names every topic.
        for heading in ("Dues", "Refunds", "Meetings", "Juniors"):
            self.assertIn(heading, line)
        self.assertIn("Phones (phone rule)", line)

    def test_spare_room_goes_to_the_long_document_as_detail(self):
        docs = [{"id": "c", "title": "Conduct"}, {"id": "h", "title": "Club handbook"}]
        detail = "d" * 290
        sections = [{"doc_id": "c", "ord": 0, "heading_path": ["Conduct", "Phones"], "covers": "phone rule"}] + [
            {"doc_id": "h", "ord": i, "heading_path": ["Club handbook", h], "covers": detail}
            for i, h in enumerate(["Dues", "Refunds", "Meetings", "Juniors"])
        ]
        line = self._line(docs, sections)
        self.assertLessEqual(len(line), cd.DOCS_HINT_CHARS)
        # All four topics named; detail on as many as fit — three, only because the short
        # Conduct entry handed its unused share to the handbook.
        for h in ("Dues", "Refunds", "Meetings", "Juniors"):
            self.assertIn(h, line)
        self.assertIn("Meetings (", line)
        self.assertNotIn("Juniors (", line)

    def test_squeezing_never_cuts_a_word(self):
        sections = [{"doc_id": "h", "ord": i, "heading_path": ["H", f"Heading number {i}"], "covers": ""} for i in range(200)]
        line = self._line([{"id": "h", "title": "H"}], sections)
        self.assertLessEqual(len(line), cd.DOCS_HINT_CHARS)
        self.assertTrue(line.endswith(tuple(f"Heading number {i}" for i in range(200))))


class EndpointTest(unittest.TestCase):
    """The real routes through FastAPI: flag gate, auth, error-code mapping, background ingest."""

    def setUp(self):
        from fastapi.testclient import TestClient

        import app.main as main
        from app.auth import AuthSession

        self.main = main
        self.client = TestClient(main.app)
        self.auth = AuthSession(user_id=ME, is_anonymous=False, phone_verified=True, home_block_id=None)
        self.H = {"Authorization": "Bearer t"}

    def test_flag_off_is_404_not_enabled(self):
        with patch.object(cd, "docs_enabled", return_value=False):
            r = self.client.post("/lana/communities/docs/list", json={"place_id": PLACE}, headers=self.H)
        self.assertEqual((r.status_code, r.json()["detail"]), (404, "not_enabled"))

    def test_list_passes_the_caller(self):
        with patch.object(cd, "docs_enabled", return_value=True), patch.object(
            self.main, "verify_auth", return_value=self.auth
        ), patch.object(cd, "list_documents", return_value={"you": "member", "documents": []}) as lst:
            r = self.client.post("/lana/communities/docs/list", json={"place_id": PLACE}, headers=self.H)
        self.assertEqual(r.status_code, 200)
        lst.assert_called_once_with(user_id=ME, place_id=PLACE)

    def test_upload_refusal_keeps_its_code_and_status(self):
        with patch.object(cd, "docs_enabled", return_value=True), patch.object(
            self.main, "verify_auth", return_value=self.auth
        ), patch.object(cd, "upload_document", side_effect=cd.DocsError("not_manager", 403)):
            r = self.client.post(
                "/lana/communities/docs/upload", headers=self.H,
                data={"place_id": PLACE}, files={"file": ("a.md", b"# A\n\ntext", "text/markdown")},
            )
        self.assertEqual((r.status_code, r.json()["detail"]), (403, "not_manager"))

    def test_upload_schedules_ingest_with_the_new_doc(self):
        row = {"doc_id": "d1", "status": "uploaded"}
        with patch.object(cd, "docs_enabled", return_value=True), patch.object(
            self.main, "verify_auth", return_value=self.auth
        ), patch.object(cd, "upload_document", return_value=row) as up, patch.object(
            cd, "ingest_document"
        ) as ingest:
            r = self.client.post(
                "/lana/communities/docs/upload", headers=self.H,
                data={"place_id": PLACE, "visibility": "public"},
                files={"file": ("a.md", b"# A\n\ntext", "text/markdown")},
            )
        self.assertEqual(r.json(), row)
        self.assertEqual(up.call_args.kwargs["visibility"], "public")
        self.assertEqual(up.call_args.kwargs["place_id"], PLACE)
        ingest.assert_called_once_with("d1")

    def test_anonymous_guest_cannot_manage(self):
        from app.auth import AuthSession

        guest = AuthSession(user_id=ME, is_anonymous=True, phone_verified=False, home_block_id=None)
        with patch.object(cd, "docs_enabled", return_value=True), patch.object(self.main, "verify_auth", return_value=guest):
            r = self.client.post("/lana/communities/docs/delete", json={"doc_id": "d1"}, headers=self.H)
        self.assertEqual((r.status_code, r.json()["detail"]), (403, "sign_in_required"))
