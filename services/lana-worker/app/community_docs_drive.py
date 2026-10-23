"""Google Drive links for community knowledge documents.

An owner pastes a link shared "anyone with the link can view". Nothing here signs in to
Google: the file comes through Google's own public export / download URLs, built from the
file id — the pasted URL itself is never fetched, so a link can only ever reach Google.

  Google Doc     → exported as DOCX (keeps headings for the section split)
  Google Sheet   → first tab exported as CSV, turned into a Markdown table
  Google Slides  → exported as PDF (read like any uploaded PDF)
  file in Drive  → downloaded as is (PDF, DOCX, MD, TXT)

A link that is not shared publicly answers with Google's sign-in page instead of the
file; that is reported as link_not_public, never stored.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import urllib.parse
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT_S = 30.0
_GOOGLE_HOSTS = ("docs.google.com", "drive.google.com")
# Where Google may redirect a public download to. Anything else is refused.
_ALLOWED_FINAL_HOSTS = (
    "docs.google.com",
    "drive.google.com",
    "drive.usercontent.google.com",
    "googleusercontent.com",
)
_ID = r"([A-Za-z0-9_-]{10,})"
_PATH_KINDS = (
    (re.compile(rf"^/document/(?:u/\d+/)?d/{_ID}"), "doc"),
    (re.compile(rf"^/spreadsheets/(?:u/\d+/)?d/{_ID}"), "sheet"),
    (re.compile(rf"^/presentation/(?:u/\d+/)?d/{_ID}"), "slides"),
    (re.compile(rf"^/file/(?:u/\d+/)?d/{_ID}"), "file"),
)


class DriveError(Exception):
    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class DriveRef:
    kind: str  # doc | sheet | slides | file
    file_id: str


@dataclass
class DriveFile:
    filename: str
    content_type: str
    raw: bytes


def parse_drive_link(url: str | None) -> DriveRef:
    """The file a pasted Google Docs / Drive link points at. Raises invalid_link for
    anything that is not a Google Docs or Drive file link."""
    text = (url or "").strip()
    if not text:
        raise DriveError("invalid_link")
    if "://" not in text:
        text = "https://" + text
    try:
        parsed = urllib.parse.urlparse(text)
    except ValueError as exc:
        raise DriveError("invalid_link") from exc
    host = (parsed.hostname or "").lower()
    if host not in _GOOGLE_HOSTS:
        raise DriveError("invalid_link")
    for pattern, kind in _PATH_KINDS:
        m = pattern.match(parsed.path or "")
        if m:
            return DriveRef(kind, m.group(1))
    # drive.google.com/open?id=… and …/uc?id=… — a plain Drive file.
    if host == "drive.google.com":
        ids = urllib.parse.parse_qs(parsed.query or "").get("id") or []
        if ids and re.fullmatch(_ID, ids[0]):
            return DriveRef("file", ids[0])
    raise DriveError("invalid_link")


def export_url(ref: DriveRef) -> str:
    fid = urllib.parse.quote(ref.file_id, safe="")
    if ref.kind == "doc":
        return f"https://docs.google.com/document/d/{fid}/export?format=docx"
    if ref.kind == "sheet":
        return f"https://docs.google.com/spreadsheets/d/{fid}/export?format=csv"
    if ref.kind == "slides":
        return f"https://docs.google.com/presentation/d/{fid}/export/pdf"
    return f"https://drive.usercontent.google.com/download?id={fid}&export=download"


_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_EXPORT_TYPES = {"doc": (_DOCX, ".docx"), "slides": ("application/pdf", ".pdf")}


def _filename(resp: httpx.Response) -> str | None:
    cd = resp.headers.get("content-disposition") or ""
    m = re.search(r"filename\*\s*=\s*UTF-8''([^;]+)", cd, flags=re.I)
    if m:
        return urllib.parse.unquote(m.group(1)).strip().strip('"')
    m = re.search(r'filename\s*=\s*"([^"]+)"', cd, flags=re.I) or re.search(
        r"filename\s*=\s*([^;]+)", cd, flags=re.I
    )
    return m.group(1).strip() if m else None


def _host_allowed(url: httpx.URL) -> bool:
    host = (url.host or "").lower()
    return any(host == h or host.endswith("." + h) for h in _ALLOWED_FINAL_HOSTS)


def csv_to_markdown(text: str, title: str) -> str:
    """A sheet's first tab as a Markdown table under its title, empty rows dropped."""
    rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [[c.replace("|", "\\|").replace("\n", " ").strip() for c in r] + [""] * (width - len(r)) for r in rows]
    lines = [f"# {title}", "", "| " + " | ".join(rows[0]) + " |", "|" + " --- |" * width]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines) + "\n"


def fetch_drive_file(url: str, *, max_bytes: int) -> DriveFile:
    """Download what a shared link points at. Errors: invalid_link, link_not_public,
    link_not_found, file_too_large, link_unreachable."""
    ref = parse_drive_link(url)
    target = export_url(ref)

    def _guard(request: httpx.Request) -> None:
        if not _host_allowed(request.url):
            raise DriveError("link_unreachable")

    try:
        with httpx.Client(
            follow_redirects=True,
            timeout=_TIMEOUT_S,
            max_redirects=8,
            event_hooks={"request": [_guard]},
            headers={"User-Agent": "Lana-Docs/1.0"},
        ) as client, client.stream("GET", target) as resp:
            if resp.status_code == 404:
                raise DriveError("link_not_found", 404)
            if resp.status_code in (401, 403):
                raise DriveError("link_not_public", 403)
            if resp.status_code >= 400:
                raise DriveError("link_unreachable", 502)
            ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
            declared = int(resp.headers.get("content-length") or 0)
            if declared > max_bytes:
                raise DriveError("file_too_large", 413)
            chunks: list[bytes] = []
            size = 0
            for chunk in resp.iter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise DriveError("file_too_large", 413)
                chunks.append(chunk)
            raw = b"".join(chunks)
            name = _filename(resp)
            final_url = str(resp.url)
    except DriveError:
        raise
    except httpx.HTTPError as exc:
        logger.warning("community_docs_drive.fetch_failed kind=%s err=%s", ref.kind, exc)
        raise DriveError("link_unreachable", 502) from exc

    # A private file answers 200 with Google's sign-in page (or a "request access" page);
    # a big public file answers with the virus-scan interstitial. Neither is the file.
    if ctype == "text/html" or "accounts.google.com" in final_url:
        if b"virus scan warning" in raw[:20000].lower():
            raise DriveError("file_too_large", 413)
        raise DriveError("link_not_public", 403)
    if not raw:
        raise DriveError("link_not_found", 404)

    if ref.kind == "sheet":
        title = re.sub(r"\.csv$", "", name or "Sheet", flags=re.I).strip() or "Sheet"
        md = csv_to_markdown(raw.decode("utf-8", errors="replace"), title)
        if not md:
            raise DriveError("link_empty")
        return DriveFile(filename=f"{title}.md", content_type="text/markdown", raw=md.encode())
    if ref.kind in _EXPORT_TYPES:
        mime, ext = _EXPORT_TYPES[ref.kind]
        stem = (name or "Document").rsplit(".", 1)[0] if name else "Document"
        return DriveFile(filename=f"{stem}{ext}", content_type=mime, raw=raw)
    return DriveFile(filename=name or "document", content_type=ctype, raw=raw)
