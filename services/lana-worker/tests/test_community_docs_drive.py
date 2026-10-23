"""Google Drive links for community documents — parsing, the guarded fetch, refresh.

The fetch tests run the real fetch_drive_file over an httpx MockTransport, so status codes,
Google's sign-in page, the size cap and the redirect host guard are exercised as Google
would answer them — nothing here reaches the network.
"""

import unittest
from typing import Any
from unittest.mock import patch

import httpx

import app.community_docs as cd
import app.community_docs_drive as drive
from app.community_docs_drive import DriveError, DriveRef, csv_to_markdown, export_url, parse_drive_link

PLACE = "44444444-4444-4444-4444-444444444444"
OWNER = cd.DocsAccess(role="manager", rank=1000, can_upload=True, can_approve=True, can_read_log=True)
MEMBER = cd.DocsAccess(role="member", rank=0, can_upload=True)
VISITOR = cd.DocsAccess(role="visitor", rank=-1)
ME = "11111111-1111-1111-1111-111111111111"
DOC_ID = "1u0giMYMA9rZHU6lkj5w69IjQ9LLA0o7EQo-LrND11bY"


class ParseTest(unittest.TestCase):
    def test_google_doc_links(self):
        for url in (
            f"https://docs.google.com/document/d/{DOC_ID}/edit",
            f"https://docs.google.com/document/u/0/d/{DOC_ID}/edit?usp=sharing",
            f"docs.google.com/document/d/{DOC_ID}",
        ):
            self.assertEqual(parse_drive_link(url), DriveRef("doc", DOC_ID), url)

    def test_sheet_slides_and_files(self):
        self.assertEqual(parse_drive_link(f"https://docs.google.com/spreadsheets/d/{DOC_ID}/edit#gid=0").kind, "sheet")
        self.assertEqual(parse_drive_link(f"https://docs.google.com/presentation/d/{DOC_ID}/edit").kind, "slides")
        self.assertEqual(parse_drive_link(f"https://drive.google.com/file/d/{DOC_ID}/view").kind, "file")
        self.assertEqual(parse_drive_link(f"https://drive.google.com/open?id={DOC_ID}"), DriveRef("file", DOC_ID))
        self.assertEqual(parse_drive_link(f"https://drive.google.com/uc?id={DOC_ID}&export=download").kind, "file")

    def test_anything_else_is_invalid(self):
        for url in (
            "", "not a link",
            f"https://docs.google.com.evil.com/document/d/{DOC_ID}",
            f"https://evil.com/docs.google.com/document/d/{DOC_ID}",
            "https://docs.google.com/forms/d/abcdefghijklmnop/viewform",
            "https://drive.google.com/drive/folders/abcdefghijklmnop",
        ):
            with self.assertRaises(DriveError, msg=url) as ctx:
                parse_drive_link(url)
            self.assertEqual(ctx.exception.code, "invalid_link")

    def test_export_urls_are_built_from_the_id_only(self):
        self.assertEqual(export_url(DriveRef("doc", DOC_ID)), f"https://docs.google.com/document/d/{DOC_ID}/export?format=docx")
        self.assertTrue(export_url(DriveRef("sheet", DOC_ID)).endswith("/export?format=csv"))
        self.assertTrue(export_url(DriveRef("slides", DOC_ID)).endswith("/export/pdf"))
        self.assertTrue(export_url(DriveRef("file", DOC_ID)).startswith("https://drive.usercontent.google.com/download?id="))


class CsvTest(unittest.TestCase):
    def test_sheet_becomes_a_markdown_table(self):
        md = csv_to_markdown("Plan,Price\nStudio,$12\n,\nStudent,$6\n", "Fees")
        self.assertEqual(
            md, "# Fees\n\n| Plan | Price |\n| --- | --- |\n| Studio | $12 |\n| Student | $6 |\n"
        )

    def test_pipes_and_ragged_rows(self):
        md = csv_to_markdown('a,b,c\n"x|y"\n', "T")
        self.assertIn("| x\\|y |  |  |", md)


def _client_with(handler):
    real = httpx.Client

    def factory(*args, **kwargs):
        return real(*args, transport=httpx.MockTransport(handler), **kwargs)

    return patch.object(drive.httpx, "Client", side_effect=factory)


class FetchTest(unittest.TestCase):
    DOC = f"https://docs.google.com/document/d/{DOC_ID}/edit"

    def _fetch(self, handler, url=None, max_bytes=10_000):
        with _client_with(handler):
            return drive.fetch_drive_file(url or self.DOC, max_bytes=max_bytes)

    def _code(self, handler, **kw):
        with self.assertRaises(DriveError) as ctx:
            self._fetch(handler, **kw)
        return ctx.exception.code

    def test_public_doc_is_named_from_google(self):
        def h(req):
            self.assertIn("/export?format=docx", str(req.url))
            return httpx.Response(200, content=b"PK\x03\x04docx",
                                  headers={"content-type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                           "content-disposition": "attachment; filename=\"x\"; filename*=UTF-8''Club%20FAQ.docx"})
        f = self._fetch(h)
        self.assertEqual(f.filename, "Club FAQ.docx")
        self.assertEqual(f.raw, b"PK\x03\x04docx")

    def test_private_doc_signin_page_is_not_public(self):
        self.assertEqual(self._code(lambda r: httpx.Response(200, content=b"<html>Sign in</html>",
                                                             headers={"content-type": "text/html; charset=utf-8"})),
                         "link_not_public")

    def test_status_codes(self):
        self.assertEqual(self._code(lambda r: httpx.Response(404)), "link_not_found")
        self.assertEqual(self._code(lambda r: httpx.Response(403)), "link_not_public")
        self.assertEqual(self._code(lambda r: httpx.Response(500)), "link_unreachable")

    def test_size_cap_while_streaming(self):
        self.assertEqual(self._code(lambda r: httpx.Response(200, content=b"x" * 20_000,
                                                             headers={"content-type": "application/pdf"})),
                         "file_too_large")

    def test_size_cap_without_a_declared_length(self):
        # Chunked, no content-length: only the running count can stop it.
        def h(req):
            return httpx.Response(200, content=iter([b"x" * 6000, b"x" * 6000]),
                                  headers={"content-type": "application/pdf"})
        self.assertEqual(self._code(h), "file_too_large")

    def test_redirect_off_google_is_refused(self):
        def h(req):
            if req.url.host == "docs.google.com":
                return httpx.Response(302, headers={"location": "https://attacker.example/steal"})
            return httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"})
        self.assertEqual(self._code(h), "link_unreachable")

    def test_redirect_within_google_is_followed(self):
        def h(req):
            if req.url.host == "docs.google.com":
                return httpx.Response(307, headers={"location": "https://doc-0s-1c-docs.googleusercontent.com/export/x"})
            return httpx.Response(200, content=b"PKdocx", headers={"content-type": "application/octet-stream"})
        self.assertEqual(self._fetch(h).raw, b"PKdocx")

    def test_sheet_is_converted(self):
        f = self._fetch(
            lambda r: httpx.Response(200, content=b"Day,Time\nTue,7pm\n",
                                     headers={"content-type": "text/csv", "content-disposition": 'attachment; filename="Schedule.csv"'}),
            url=f"https://docs.google.com/spreadsheets/d/{DOC_ID}/edit",
        )
        self.assertEqual((f.filename, f.content_type), ("Schedule.md", "text/markdown"))
        self.assertIn(b"| Tue | 7pm |", f.raw)


ROW = {"id": "d1", "place_id": PLACE, "source_kind": "drive", "status": "ready",
       "source_url": f"https://docs.google.com/document/d/{DOC_ID}/edit", "content_hash": None}


class _Q:
    def __init__(self, log, table):
        self.log, self.table = log, table

    def update(self, patch):
        self.log.append((self.table, patch))
        return self

    def __getattr__(self, _):
        return lambda *a, **k: self

    def execute(self):
        return type("R", (), {"data": []})()


class _Client:
    def __init__(self):
        self.log: list[Any] = []

    def table(self, name):
        return _Q(self.log, name)


class RefreshTest(unittest.TestCase):
    def setUp(self):
        self.client = _Client()
        self.p = [patch.object(cd, "service_client", return_value=self.client)]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def _file(self, raw=b"PKnew"):
        return drive.DriveFile("FAQ.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", raw)

    def test_unchanged_file_only_bumps_the_sync_time(self):
        import hashlib

        row = {**ROW, "content_hash": hashlib.sha256(b"PKsame").hexdigest()}
        with patch.object(cd, "_doc", return_value=row), patch.object(drive, "fetch_drive_file", return_value=self._file(b"PKsame")), \
                patch.object(cd, "_replace_file") as replace:
            self.assertEqual(cd.refresh_document("d1"), "unchanged")
        replace.assert_not_called()
        self.assertEqual(self.client.log[-1][1]["sync_error"], None)
        self.assertIn("last_synced_at", self.client.log[-1][1])

    def test_changed_file_is_stored_as_a_new_version_of_the_same_doc(self):
        with patch.object(cd, "_doc", return_value=ROW), patch.object(drive, "fetch_drive_file", return_value=self._file()), \
                patch.object(cd, "_replace_file") as replace:
            self.assertEqual(cd.refresh_document("d1"), "updated")
        self.assertEqual(replace.call_args.args[0]["id"], "d1")
        self.assertEqual(replace.call_args.kwargs["raw"], b"PKnew")

    def test_replaced_file_keeps_audience_approval_and_uploader(self):
        uploads: list[Any] = []

        class _Bucket:
            def upload(self, path, raw, file_options):
                uploads.append(path)

        self.client.storage = type("St", (), {"from_": lambda _s, _b: _Bucket()})()
        row = {**ROW, "version": 2, "visibility": "members", "min_level_id": "lv1",
               "review_status": "pending", "uploaded_by": ME}
        cd._replace_file(row, filename="FAQ.docx",
                         content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                         raw=b"PK", digest="h")
        patch_ = self.client.log[-1][1]
        self.assertEqual(uploads, [f"{PLACE}/d1/v3.docx"])
        self.assertEqual((patch_["version"], patch_["status"], patch_["content_hash"]), (3, "uploaded", "h"))
        for kept in ("visibility", "min_level_id", "review_status", "uploaded_by", "title"):
            self.assertNotIn(kept, patch_)

    def test_unshared_link_keeps_the_old_version_and_says_why(self):
        with patch.object(cd, "_doc", return_value=ROW), \
                patch.object(drive, "fetch_drive_file", side_effect=DriveError("link_not_public", 403)), \
                patch.object(cd, "_replace_file") as replace:
            self.assertEqual(cd.refresh_document("d1"), "failed")
        replace.assert_not_called()
        self.assertEqual(self.client.log[-1][1], {"sync_error": "link_not_public"})

    def test_manual_refresh_is_for_owners_and_whoever_added_it(self):
        someone_else = {**ROW, "uploaded_by": "99999999-9999-9999-9999-999999999999"}
        with patch.object(cd, "_doc", return_value=someone_else), patch.object(cd, "docs_access", return_value=MEMBER):
            with self.assertRaises(cd.DocsError) as ctx:
                cd.refresh_document("d1", user_id=ME)
        self.assertEqual(ctx.exception.code, "not_manager")
        mine = {**ROW, "uploaded_by": ME}
        with patch.object(cd, "_doc", return_value=mine), patch.object(cd, "docs_access", return_value=MEMBER), \
                patch.object(drive, "fetch_drive_file", side_effect=DriveError("link_not_found", 404)):
            self.assertEqual(cd.refresh_document("d1", user_id=ME), "failed")

    def test_uploads_are_not_refreshable(self):
        with patch.object(cd, "_doc", return_value={**ROW, "source_kind": "upload", "source_url": None}):
            with self.assertRaises(cd.DocsError) as ctx:
                cd.refresh_document("d1")
        self.assertEqual(ctx.exception.code, "not_a_link")

    def test_link_goes_through_the_upload_rules_then_marks_the_row(self):
        with patch.object(cd, "docs_access", return_value=MEMBER), \
                patch.object(drive, "fetch_drive_file", return_value=self._file()), \
                patch.object(cd, "upload_document", return_value={"doc_id": "d9", "review_status": "pending"}) as up:
            row = cd.link_document(user_id=ME, place_id=PLACE, url=ROW["source_url"], level_id="lv1")
        kw = up.call_args.kwargs
        self.assertEqual((kw["filename"], kw["level_id"], kw["user_id"], kw["raw"]), ("FAQ.docx", "lv1", ME, b"PKnew"))
        marked = self.client.log[-1][1]
        self.assertEqual((marked["source_kind"], marked["source_url"]), ("drive", ROW["source_url"]))
        self.assertEqual(len(marked["content_hash"]), 64)
        self.assertEqual((row["review_status"], row["source_kind"]), ("pending", "drive"))

    def test_upload_refusals_apply_to_links(self):
        with patch.object(cd, "docs_access", return_value=MEMBER), \
                patch.object(drive, "fetch_drive_file", return_value=self._file()), \
                patch.object(cd, "upload_document", side_effect=cd.DocsError("level_too_high", 403)):
            with self.assertRaises(cd.DocsError) as ctx:
                cd.link_document(user_id=ME, place_id=PLACE, url=ROW["source_url"], level_id="lv9")
        self.assertEqual(ctx.exception.code, "level_too_high")
        self.assertEqual(self.client.log, [])  # nothing marked when the upload was refused

    def test_visitors_cannot_link_and_nothing_is_fetched(self):
        with patch.object(cd, "docs_access", return_value=VISITOR), patch.object(drive, "fetch_drive_file") as fetch:
            with self.assertRaises(cd.DocsError) as ctx:
                cd.link_document(user_id=ME, place_id=PLACE, url=ROW["source_url"])
        self.assertEqual(ctx.exception.code, "not_member")
        fetch.assert_not_called()

    def test_link_errors_become_docs_errors(self):
        with patch.object(cd, "docs_access", return_value=OWNER), \
                patch.object(drive, "fetch_drive_file", side_effect=DriveError("link_not_public", 403)):
            with self.assertRaises(cd.DocsError) as ctx:
                cd.link_document(user_id=ME, place_id=PLACE, url="x")
        self.assertEqual((ctx.exception.code, ctx.exception.status), ("link_not_public", 403))


class StaleRefreshTest(unittest.TestCase):
    def setUp(self):
        cd._link_attempts.clear()

    def test_stale_links_refresh_and_changed_ones_reingest(self):
        with patch.object(cd, "_stale_links", return_value=[{"id": "a"}, {"id": "b"}]), \
                patch.object(cd, "refresh_document", side_effect=["updated", "unchanged"]), \
                patch.object(cd, "ingest_document") as ingest:
            counts = cd.refresh_stale_links(PLACE)
        self.assertEqual((counts["updated"], counts["unchanged"]), (1, 1))
        ingest.assert_called_once_with("a")

    def test_one_attempt_per_document_per_hour(self):
        with patch.object(cd, "_stale_links", return_value=[{"id": "a"}]), \
                patch.object(cd, "refresh_document", return_value="failed") as refresh, \
                patch.object(cd, "ingest_document"):
            cd.refresh_stale_links(PLACE)
            second = cd.refresh_stale_links(PLACE)
        self.assertEqual(refresh.call_count, 1)
        self.assertEqual(second["skipped"], 1)


class LinkEndpointTest(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient

        import app.main as main
        from app.auth import AuthSession

        self.main = main
        self.client = TestClient(main.app)
        self.auth = AuthSession(user_id=ME, is_anonymous=False, phone_verified=True, home_block_id=None)
        self.H = {"Authorization": "Bearer t"}

    def test_link_schedules_ingest(self):
        with patch.object(cd, "docs_enabled", return_value=True), patch.object(self.main, "verify_auth", return_value=self.auth), \
                patch.object(cd, "link_document", return_value={"doc_id": "d1", "status": "uploaded"}) as link, \
                patch.object(cd, "ingest_document") as ingest:
            r = self.client.post("/lana/communities/docs/link", headers=self.H,
                                 json={"place_id": PLACE, "url": ROW["source_url"], "visibility": "public"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(link.call_args.kwargs["url"], ROW["source_url"])
        ingest.assert_called_once_with("d1")

    def test_link_error_code_reaches_the_client(self):
        with patch.object(cd, "docs_enabled", return_value=True), patch.object(self.main, "verify_auth", return_value=self.auth), \
                patch.object(cd, "link_document", side_effect=cd.DocsError("link_not_public", 403)):
            r = self.client.post("/lana/communities/docs/link", headers=self.H, json={"place_id": PLACE, "url": "x"})
        self.assertEqual((r.status_code, r.json()["detail"]), (403, "link_not_public"))

    def test_refresh_reingests_only_when_updated(self):
        for outcome, expect in (("updated", 1), ("unchanged", 0), ("failed", 0)):
            with patch.object(cd, "docs_enabled", return_value=True), patch.object(self.main, "verify_auth", return_value=self.auth), \
                    patch.object(cd, "refresh_document", return_value=outcome), patch.object(cd, "ingest_document") as ingest:
                r = self.client.post("/lana/communities/docs/refresh", headers=self.H, json={"doc_id": "d1"})
            self.assertEqual(r.json()["outcome"], outcome)
            self.assertEqual(ingest.call_count, expect, outcome)

    def test_hook_fails_closed(self):
        with patch.dict("os.environ", {"SIGNAL_SWEEP_TOKEN": ""}):
            self.assertEqual(self.client.post("/hooks/community-docs-refresh").status_code, 403)
        with patch.dict("os.environ", {"SIGNAL_SWEEP_TOKEN": "s3cret"}):
            self.assertEqual(self.client.post("/hooks/community-docs-refresh", headers={"x-sweep-token": "nope"}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
